"""Regression tests for acknowledged actions and challenge-protected log reads."""

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from bleak import BleakError
from construct import Container
from homeassistant.exceptions import HomeAssistantError
from pyNukiBT import NukiConst, NukiOpenerConst
from pyNukiBT.const import NukiErrorException

from custom_components.hass_nuki_bt.button import NukiButton
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
            device=SimpleNamespace(),
            coordinator=SimpleNamespace(
                async_action=Mock(side_effect=nullcontext),
                async_get_last_action_log_entry=AsyncMock(side_effect=TimeoutError()),
                async_update_listeners=Mock(),
                async_refresh_after_action=Mock(),
            ),
        )

    async def test_completed_action_does_not_wait_for_logs(self):
        """A confirmed action returns even if the optional log is unavailable."""
        entity = self.make_entity()
        with patch("custom_components.hass_nuki_bt.entity.async_execute_lock_action", new_callable=AsyncMock) as execute:
            await NukiEntity.async_lock_action(entity, NukiOpenerConst.LockAction.ACTIVATE_CM)
        execute.assert_awaited_once_with(entity.device, NukiOpenerConst.LockAction.ACTIVATE_CM, name_suffix=None)
        entity.coordinator.async_get_last_action_log_entry.assert_not_awaited()
        entity.coordinator.async_refresh_after_action.assert_called_once()

    async def test_rejected_action_is_not_reported_as_success(self):
        """A false completion result must remain a service error."""
        entity = self.make_entity()
        with (
            patch("custom_components.hass_nuki_bt.entity.async_execute_lock_action", new_callable=AsyncMock, side_effect=RuntimeError("unconfirmed")),
            self.assertRaises(HomeAssistantError),
        ):
            await NukiEntity.async_lock_action(entity, NukiOpenerConst.LockAction.ACTIVATE_CM)
        entity.coordinator.async_refresh_after_action.assert_not_called()

    async def test_command_error_propagates(self):
        """A protocol rejection remains a service error HA can classify."""
        entity = self.make_entity()
        with (
            patch("custom_components.hass_nuki_bt.entity.async_execute_lock_action", new_callable=AsyncMock, side_effect=nuki_error(NukiOpenerConst.ErrorCode.K_ERROR_BAD_NONCE)),
            self.assertRaises(HomeAssistantError) as raised,
        ):
            await NukiEntity.async_lock_action(entity, NukiOpenerConst.LockAction.ACTIVATE_CM)
        self.assertIsInstance(raised.exception.__cause__, NukiErrorException)
        entity.coordinator.async_refresh_after_action.assert_not_called()

    async def test_button_transport_errors_can_be_continued_by_ha(self):
        """A failed state query must not abort HA's delayed retry sequence."""
        for error in (TimeoutError(), BleakError("offline"), nuki_error(NukiOpenerConst.ErrorCode.K_ERROR_BAD_NONCE)):
            with self.subTest(error=type(error).__name__):
                entity = SimpleNamespace(entity_description=SimpleNamespace(action_function=AsyncMock(side_effect=error)))
                with self.assertRaises(HomeAssistantError) as raised:
                    await NukiButton.async_press(entity)
                self.assertIs(raised.exception.__cause__, error)

    async def test_button_cancellation_is_not_a_retryable_error(self):
        """A newer automation request must still stop an older query."""
        entity = SimpleNamespace(entity_description=SimpleNamespace(action_function=AsyncMock(side_effect=asyncio.CancelledError())))
        with self.assertRaises(asyncio.CancelledError):
            await NukiButton.async_press(entity)

    async def test_timed_out_mode_command_is_ha_service_error(self):
        """A mode failure can be handled by an automation's retry steps."""
        entity = self.make_entity()
        with (
            patch("custom_components.hass_nuki_bt.entity.async_execute_lock_action", new_callable=AsyncMock, side_effect=TimeoutError()),
            self.assertRaises(HomeAssistantError) as raised,
        ):
            await NukiEntity.async_lock_action(entity, NukiOpenerConst.LockAction.DEACTIVATE_CM)
        self.assertIsInstance(raised.exception.__cause__, TimeoutError)
        entity.coordinator.async_refresh_after_action.assert_not_called()


if __name__ == "__main__":
    unittest.main()
