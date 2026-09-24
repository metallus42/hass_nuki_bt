"""Regression tests for acknowledged actions and challenge-protected log reads."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from bleak import BleakError
from construct import Container
from homeassistant.exceptions import HomeAssistantError
from pyNukiBT import NukiConst, NukiOpenerConst
from pyNukiBT.const import NukiErrorException

from custom_components.hass_nuki_bt.coordinator import NukiDataUpdateCoordinator
from custom_components.hass_nuki_bt.entity import NukiEntity
from custom_components.hass_nuki_bt.logs import async_request_log_entries


class FakeOpener:
    """Consume every request nonce, including requests whose response is lost."""

    _const = NukiOpenerConst

    def __init__(self, failures=()):
        """Configure transport failures without touching a physical device."""
        self._operation_lock = asyncio.Lock()
        self._messages = []
        self.failures = list(failures)
        self.nonces = []
        self.requests = []
        self.used = set()
        self.status = self._const.StatusCode.COMPLETED

    async def _send_encrypted_command(self, command, payload, **kwargs):
        assert self._operation_lock.locked()
        assert kwargs["response_retry"] == 1
        if command == self._const.NukiCommand.REQUEST_DATA:
            nonce = bytes([len(self.nonces) + 1]) * 32
            self.nonces.append(nonce)
            return Container(nonce=nonce)
        assert command == self._const.NukiCommand.REQUEST_LOG_ENTRIES
        assert payload["nonce"] == self.nonces[-1]
        assert payload["nonce"] not in self.used
        self.used.add(payload["nonce"])
        self.requests.append(payload.copy())
        # Exercise the serializer of the actual pinned protocol dependency.
        self._const.NukiMessage.build(
            {"auth_id": b"\x01\x02\x03\x04", "command": command, "payload": payload}
        )
        if self.failures:
            raise self.failures.pop(0)
        self._messages = [Container(index=17, type=NukiConst.LogEntryType.LOCK_ACTION)]
        return Container(status=self.status)


def nuki_error(code):
    """Construct the real library's error type."""
    return NukiErrorException(code, NukiOpenerConst.NukiCommand.REQUEST_LOG_ENTRIES)


class LogProtocolTests(unittest.IsolatedAsyncioTestCase):
    """Exercise fresh challenges, bounded retries and nonretryable errors."""

    async def test_timeout_gets_fresh_challenge(self):
        """A lost response must never replay the consumed nonce."""
        device = FakeOpener([TimeoutError()])
        result = await async_request_log_entries(device, 0, count=4, start_index=12)
        self.assertEqual(len(device.requests), 2)
        self.assertNotEqual(device.requests[0]["nonce"], device.requests[1]["nonce"])
        self.assertEqual(device.requests[1]["security_pin"], 0)
        self.assertEqual(device.requests[1]["count"], 4)
        self.assertEqual(device.requests[1]["start_index"], 12)
        device._messages.clear()
        self.assertEqual(result[0].index, 17)
        self.assertFalse(device._operation_lock.locked())

    async def test_bad_nonce_gets_fresh_challenge(self):
        """Recover once if the Opener explicitly rejects a challenge."""
        device = FakeOpener([nuki_error(NukiOpenerConst.ErrorCode.K_ERROR_BAD_NONCE)])
        await async_request_log_entries(device, 0)
        self.assertEqual(len(device.nonces), 2)

    async def test_retry_is_bounded(self):
        """Persistent timeouts terminate after two read attempts."""
        device = FakeOpener([TimeoutError(), TimeoutError(), TimeoutError()])
        with self.assertRaises(TimeoutError):
            await async_request_log_entries(device, 0)
        self.assertEqual(len(device.requests), 2)
        self.assertFalse(device._operation_lock.locked())

    async def test_bad_pin_is_not_retried(self):
        """An authentication rejection must not be retried."""
        device = FakeOpener([nuki_error(NukiOpenerConst.ErrorCode.K_ERROR_BAD_PIN)])
        with self.assertRaises(NukiErrorException):
            await async_request_log_entries(device, 0)
        self.assertEqual(len(device.requests), 1)

    async def test_cancel_is_not_retried(self):
        """Unloading must cancel the operation instead of starting a retry."""
        device = FakeOpener([asyncio.CancelledError()])
        with self.assertRaises(asyncio.CancelledError):
            await async_request_log_entries(device, 0)
        self.assertEqual(len(device.requests), 1)
        self.assertFalse(device._operation_lock.locked())

    async def test_incomplete_status_is_rejected(self):
        """Only completed log responses may update the cache."""
        device = FakeOpener()
        device.status = device._const.StatusCode.ACCEPTED
        with self.assertRaises(RuntimeError):
            await async_request_log_entries(device, 0)
        self.assertEqual(len(device.requests), 1)


class ActionTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the production entity method without sending door commands."""

    def make_entity(self):
        """Supply authentication and device boundaries only."""
        return SimpleNamespace(
            _context=SimpleNamespace(user_id=None),
            hass=SimpleNamespace(auth=SimpleNamespace(async_get_user=AsyncMock(return_value=None))),
            device=SimpleNamespace(lock_action=AsyncMock(return_value=True)),
            coordinator=SimpleNamespace(
                async_get_last_action_log_entry=AsyncMock(side_effect=TimeoutError()),
                async_update_listeners=Mock(),
                async_refresh_after_action=Mock(),
            ),
        )

    async def test_completed_action_does_not_wait_for_logs(self):
        """A confirmed action returns even if the optional log is unavailable."""
        entity = self.make_entity()
        await NukiEntity.async_lock_action(entity, NukiOpenerConst.LockAction.ACTIVATE_CM)
        entity.device.lock_action.assert_awaited_once()
        entity.coordinator.async_get_last_action_log_entry.assert_not_awaited()
        entity.coordinator.async_refresh_after_action.assert_called_once()

    async def test_rejected_action_is_not_reported_as_success(self):
        """A false completion result must remain a service error."""
        entity = self.make_entity()
        entity.device.lock_action.return_value = False
        with self.assertRaises(HomeAssistantError):
            await NukiEntity.async_lock_action(entity, NukiOpenerConst.LockAction.ACTIVATE_CM)
        entity.coordinator.async_refresh_after_action.assert_not_called()

    async def test_command_error_propagates(self):
        """Only optional reads are best effort; command failures still fail."""
        entity = self.make_entity()
        entity.device.lock_action.side_effect = nuki_error(NukiOpenerConst.ErrorCode.K_ERROR_BAD_NONCE)
        with self.assertRaises(NukiErrorException):
            await NukiEntity.async_lock_action(entity, NukiOpenerConst.LockAction.ACTIVATE_CM)
        entity.coordinator.async_refresh_after_action.assert_not_called()


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    """Exercise optional diagnostics, state updates and task lifecycle."""

    async def test_log_errors_preserve_cached_entry(self):
        """Communication failures must not replace the previous valid log."""
        for error in (TimeoutError(), BleakError("offline"), nuki_error(NukiOpenerConst.ErrorCode.K_ERROR_BAD_NONCE)):
            coordinator = SimpleNamespace(
                _async_get_last_action_log_entry=AsyncMock(side_effect=error),
                last_nuki_log_entry={"index": 17},
            )
            with self.assertLogs("custom_components.hass_nuki_bt.coordinator", level="WARNING"):
                await NukiDataUpdateCoordinator.async_get_last_action_log_entry(coordinator)
            self.assertEqual(coordinator.last_nuki_log_entry, {"index": 17})

    async def test_optional_log_failure_does_not_drop_doorbell(self):
        """An already read state must still be processed when logging fails."""
        rang = Mock()
        signature = (int(NukiConst.State.DOOR_MODE), int(NukiOpenerConst.LockState.LOCKED))
        coordinator = SimpleNamespace(
            device=SimpleNamespace(update_state=AsyncMock()),
            _async_get_last_action_log_entry=AsyncMock(side_effect=TimeoutError()),
            _doorbell_candidate_state=signature,
            _opener_state_signature=lambda: signature,
            _doorbell_callbacks=[rang],
        )
        coordinator.async_get_last_action_log_entry = lambda: NukiDataUpdateCoordinator.async_get_last_action_log_entry(coordinator)
        with self.assertLogs("custom_components.hass_nuki_bt.coordinator", level="WARNING"):
            await NukiDataUpdateCoordinator._async_update(coordinator)
        rang.assert_called_once()

    async def test_cancellation_propagates_from_log_read(self):
        """Do not swallow task cancellation as an optional diagnostic error."""
        coordinator = SimpleNamespace(_async_get_last_action_log_entry=AsyncMock(side_effect=asyncio.CancelledError()))
        with self.assertRaises(asyncio.CancelledError):
            await NukiDataUpdateCoordinator.async_get_last_action_log_entry(coordinator)

    async def test_background_refresh_failure_is_handled(self):
        """A read timeout cannot turn a completed command into an exception."""
        coordinator = SimpleNamespace(_async_update=AsyncMock(side_effect=TimeoutError()), async_update_listeners=Mock())
        with self.assertLogs("custom_components.hass_nuki_bt.coordinator", level="WARNING"):
            await NukiDataUpdateCoordinator._async_refresh_after_action(coordinator)
        coordinator.async_update_listeners.assert_called_once()

    async def test_refresh_survives_caller_cancellation_and_is_tracked(self):
        """Restarting the calling automation must not cancel its log read."""
        started, release = asyncio.Event(), asyncio.Event()

        async def refresh():
            started.set()
            await release.wait()

        coordinator = SimpleNamespace(
            hass=SimpleNamespace(async_create_background_task=lambda coro, name: asyncio.create_task(coro, name=name)),
            _async_refresh_after_action=refresh,
            _post_action_tasks=set(),
        )

        async def caller():
            NukiDataUpdateCoordinator.async_refresh_after_action(coordinator)
            await asyncio.Event().wait()

        caller_task = asyncio.create_task(caller())
        await started.wait()
        caller_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller_task
        refresh_task, = coordinator._post_action_tasks
        self.assertFalse(refresh_task.done())
        release.set()
        await refresh_task
        await asyncio.sleep(0)
        self.assertFalse(coordinator._post_action_tasks)

    async def test_unload_cancels_refresh(self):
        """Outstanding reads are cancelled when the integration unloads."""
        task = asyncio.create_task(asyncio.Event().wait())
        coordinator = object.__new__(NukiDataUpdateCoordinator)
        coordinator._post_action_tasks = {task}
        coordinator._unsubscribe_nuki_callbacks = Mock()
        with patch("homeassistant.components.bluetooth.active_update_coordinator.ActiveBluetoothDataUpdateCoordinator._async_stop"):
            coordinator._async_stop()
        with self.assertRaises(asyncio.CancelledError):
            await task
        coordinator._unsubscribe_nuki_callbacks.assert_called_once()


if __name__ == "__main__":
    unittest.main()
