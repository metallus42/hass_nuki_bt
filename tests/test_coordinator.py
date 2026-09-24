"""Coordinator scheduling, doorbell timing and shutdown regressions."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from bleak import BleakError
from homeassistant.exceptions import HomeAssistantError
from pyNukiBT import NukiConst, NukiOpenerConst
from pyNukiBT.const import NukiErrorException

from custom_components.hass_nuki_bt.coordinator import NukiDataUpdateCoordinator
from custom_components.hass_nuki_bt.entity import NukiEntity


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    """Use the real coordinator with Bluetooth and HA scheduling boundaries mocked."""

    def make_coordinator(self):
        """Make coordinator."""
        coordinator = object.__new__(NukiDataUpdateCoordinator)
        coordinator.hass = SimpleNamespace(
            async_create_background_task=lambda coro, name: asyncio.create_task(coro, name=name)
        )
        coordinator.device = SimpleNamespace(
            async_update_state_only=AsyncMock(),
            update_config=AsyncMock(),
            _poll_needed_config=False,
            disconnect=AsyncMock(),
            set_ble_device=Mock(),
            parse_advertisement_data=Mock(),
            device_type=NukiConst.NukiDeviceType.OPENER,
            keyturner_state={
                "nuki_state": NukiConst.State.DOOR_MODE,
                "lock_state": NukiOpenerConst.LockState.LOCKED,
            },
            config={},
        )
        coordinator.device_name = "Test opener"
        coordinator.base_unique_id = "AA:BB:CC:DD:EE:FF"
        coordinator.ble_device = SimpleNamespace(address=coordinator.base_unique_id)
        coordinator._doorbell_callbacks = []
        coordinator._doorbell_candidate_state = None
        coordinator._doorbell_generation = 0
        coordinator._action_generation = 0
        coordinator._post_action_tasks = set()
        coordinator._active_actions = set()
        coordinator._state_refresh_task = None
        coordinator._log_task = None
        coordinator._stopped = False
        coordinator._started = False
        coordinator._disconnected = False
        coordinator._unsubscribe_nuki_callbacks = None
        coordinator._security_pin = 0
        coordinator._listeners = {}
        coordinator.last_nuki_log_entry = {"index": 17}
        coordinator._async_get_last_action_log_entry = AsyncMock()
        self.addAsyncCleanup(coordinator.async_shutdown)
        return coordinator

    def beacon(self, coordinator):
        """Deliver an actual status-change advertisement to the production handler."""
        info = SimpleNamespace(
            device=coordinator.ble_device,
            advertisement=SimpleNamespace(manufacturer_data={76: bytes([0x02, 0x01])}),
        )
        with patch("homeassistant.components.bluetooth.active_update_coordinator.ActiveBluetoothDataUpdateCoordinator._async_handle_bluetooth_event"):
            coordinator._async_handle_bluetooth_event(info, None)

    async def test_doorbell_is_emitted_before_slow_optional_logs(self):
        """Doorbell is emitted before slow optional logs."""
        coordinator = self.make_coordinator()
        rang = Mock()
        coordinator.async_add_doorbell_listener(rang)
        logs_started, release_logs = asyncio.Event(), asyncio.Event()

        async def logs():
            logs_started.set()
            await release_logs.wait()

        coordinator._async_get_last_action_log_entry.side_effect = logs
        self.beacon(coordinator)
        await coordinator._async_update()
        await logs_started.wait()
        rang.assert_called_once()
        self.assertFalse(coordinator._log_task.done())
        release_logs.set()
        await coordinator._log_task

    async def test_beacon_during_logs_requires_a_new_state_read(self):
        """Beacon during logs requires a new state read."""
        coordinator = self.make_coordinator()
        rang = Mock()
        coordinator.async_add_doorbell_listener(rang)
        logs_started, release_logs = asyncio.Event(), asyncio.Event()

        async def logs():
            logs_started.set()
            await release_logs.wait()

        coordinator._async_get_last_action_log_entry.side_effect = logs
        await coordinator._async_update()
        await logs_started.wait()
        self.beacon(coordinator)
        release_logs.set()
        await coordinator._log_task
        rang.assert_not_called()
        coordinator.device.keyturner_state["nuki_state"] = NukiConst.State.CONTINUOUS_MODE
        await coordinator._async_update()
        self.assertEqual(coordinator.device.async_update_state_only.await_count, 2)
        rang.assert_not_called()

    async def test_configuration_read_cannot_delay_doorbell_even_without_pin(self):
        """Configuration is refreshed separately even when logs are disabled."""
        coordinator = self.make_coordinator()
        coordinator._security_pin = None
        coordinator.device._poll_needed_config = True
        started, release = asyncio.Event(), asyncio.Event()
        rang = Mock()
        coordinator.async_add_doorbell_listener(rang)

        async def config():
            started.set()
            await release.wait()
            coordinator.device._poll_needed_config = False

        coordinator.device.update_config.side_effect = config
        self.beacon(coordinator)
        await coordinator._async_update()
        await started.wait()
        rang.assert_called_once()
        self.assertFalse(coordinator._log_task.done())
        release.set()
        await coordinator._log_task
        coordinator.device.update_config.assert_awaited_once()

    async def test_background_state_failure_is_contained(self):
        """A failed state read does not fail an already acknowledged command."""
        coordinator = self.make_coordinator()
        coordinator.device.async_update_state_only.side_effect = TimeoutError()
        with self.assertLogs("custom_components.hass_nuki_bt.coordinator", level="WARNING"):
            coordinator.async_refresh_after_action()
            await asyncio.gather(*coordinator._post_action_tasks)
        self.assertFalse(coordinator._pending_tasks())

    async def test_beacon_during_state_request_gets_its_own_fresh_response(self):
        """Beacon during state request gets its own fresh response."""
        coordinator = self.make_coordinator()
        rang = Mock()
        coordinator.async_add_doorbell_listener(rang)
        started, release = asyncio.Event(), asyncio.Event()
        reads = 0

        async def read_state():
            nonlocal reads
            reads += 1
            if reads == 1:
                started.set()
                await release.wait()
            else:
                coordinator.device.keyturner_state["nuki_state"] = NukiConst.State.CONTINUOUS_MODE

        coordinator.device.async_update_state_only.side_effect = read_state
        refresh = asyncio.create_task(coordinator._async_update())
        await started.wait()
        self.beacon(coordinator)
        release.set()
        await refresh
        self.assertEqual(reads, 2)
        rang.assert_not_called()
        self.assertIsNone(coordinator._doorbell_candidate_state)

    async def test_continuous_or_unlocked_states_are_not_doorbell_events(self):
        """Continuous or unlocked states are not doorbell events."""
        coordinator = self.make_coordinator()
        rang = Mock()
        coordinator.async_add_doorbell_listener(rang)
        for mode, lock_state in (
            (NukiConst.State.CONTINUOUS_MODE, NukiOpenerConst.LockState.LOCKED),
            (NukiConst.State.DOOR_MODE, NukiOpenerConst.LockState.OPEN),
            (NukiConst.State.DOOR_MODE, NukiOpenerConst.LockState.RTO_ACTIVE),
        ):
            coordinator.device.keyturner_state.update(nuki_state=mode, lock_state=lock_state)
            self.beacon(coordinator)
            await coordinator._async_update()
        rang.assert_not_called()

    async def test_repeated_advertisements_do_not_restart_refresh_or_repeat_ring(self):
        """Repeated advertisements do not restart refresh or repeat ring."""
        coordinator = self.make_coordinator()
        rang = Mock()
        coordinator.async_add_doorbell_listener(rang)
        started, release = asyncio.Event(), asyncio.Event()

        async def state():
            started.set()
            await release.wait()

        coordinator.device.async_update_state_only.side_effect = state
        self.beacon(coordinator)
        poll = asyncio.create_task(coordinator._async_update())
        await started.wait()
        for _ in range(20):
            self.beacon(coordinator)
        release.set()
        await poll
        coordinator.device.async_update_state_only.assert_awaited_once()
        rang.assert_called_once()

    async def test_action_requests_new_response_when_old_poll_is_in_flight(self):
        """Action requests new response when old poll is in flight."""
        coordinator = self.make_coordinator()
        started, release = asyncio.Event(), asyncio.Event()
        reads = 0

        async def state():
            nonlocal reads
            reads += 1
            if reads == 1:
                started.set()
                await release.wait()
            else:
                coordinator.device.keyturner_state["nuki_state"] = NukiConst.State.CONTINUOUS_MODE

        coordinator.device.async_update_state_only.side_effect = state
        poll = asyncio.create_task(coordinator._async_update())
        await started.wait()
        coordinator.async_refresh_after_action()
        release.set()
        await poll
        await asyncio.gather(*coordinator._post_action_tasks)
        self.assertEqual(reads, 2)
        self.assertEqual(coordinator.device.keyturner_state["nuki_state"], NukiConst.State.CONTINUOUS_MODE)

    async def test_poll_and_action_share_reads_and_slow_logs(self):
        """Poll and action share reads and slow logs."""
        coordinator = self.make_coordinator()
        state_started, state_release = asyncio.Event(), asyncio.Event()
        log_started, log_release = asyncio.Event(), asyncio.Event()

        async def state():
            state_started.set()
            await state_release.wait()

        async def logs():
            log_started.set()
            await log_release.wait()

        coordinator.device.async_update_state_only.side_effect = state
        coordinator._async_get_last_action_log_entry.side_effect = logs
        poll = asyncio.create_task(coordinator._async_update())
        await state_started.wait()
        coordinator.async_refresh_after_action()
        coordinator.async_refresh_after_action()
        self.assertEqual(len(coordinator._post_action_tasks), 1)
        await asyncio.sleep(0)
        state_release.set()
        await poll
        await asyncio.gather(*coordinator._post_action_tasks)
        await log_started.wait()
        self.assertEqual(coordinator.device.async_update_state_only.await_count, 2)
        coordinator._async_get_last_action_log_entry.assert_awaited_once()
        log_release.set()
        await coordinator._log_task

    async def test_log_errors_preserve_cache(self):
        """Log errors preserve cache."""
        coordinator = self.make_coordinator()
        for error in (
            TimeoutError(), BleakError("offline"),
            NukiErrorException(NukiOpenerConst.ErrorCode.K_ERROR_BAD_NONCE, NukiOpenerConst.NukiCommand.REQUEST_LOG_ENTRIES),
        ):
            coordinator._async_get_last_action_log_entry.side_effect = error
            with self.assertLogs("custom_components.hass_nuki_bt.coordinator", level="WARNING"):
                await coordinator.async_get_last_action_log_entry()
            self.assertEqual(coordinator.last_nuki_log_entry, {"index": 17})

    async def test_cancelled_log_read_propagates(self):
        """Cancelled log read propagates."""
        coordinator = self.make_coordinator()
        coordinator._async_get_last_action_log_entry.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await coordinator.async_get_last_action_log_entry()

    async def test_refresh_survives_automation_cancellation(self):
        """Refresh survives automation cancellation."""
        coordinator = self.make_coordinator()
        started, release = asyncio.Event(), asyncio.Event()

        async def refresh():
            started.set()
            await release.wait()

        coordinator.device.async_update_state_only.side_effect = refresh

        async def automation():
            coordinator.async_refresh_after_action()
            await asyncio.Event().wait()

        caller = asyncio.create_task(automation())
        await started.wait()
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.assertFalse(coordinator._state_refresh_task.done())
        release.set()
        await asyncio.gather(*coordinator._post_action_tasks)

    async def test_shutdown_awaits_cancellation_before_disconnect(self):
        """Shutdown awaits cancellation before disconnect."""
        coordinator = self.make_coordinator()
        started = asyncio.Event()
        order = []

        async def logs():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                order.append("read stopped")

        async def disconnect():
            order.append("disconnected")

        unsubscribe = Mock()
        coordinator._unsubscribe_nuki_callbacks = unsubscribe
        coordinator._async_get_last_action_log_entry.side_effect = logs
        coordinator.device.disconnect.side_effect = disconnect
        coordinator._ensure_log_refresh()
        await started.wait()
        await coordinator.async_shutdown()
        await coordinator.async_shutdown()
        self.assertEqual(order, ["read stopped", "disconnected"])
        unsubscribe.assert_called_once()
        self.assertFalse(coordinator._pending_tasks())
        coordinator.async_refresh_after_action()
        self.assertFalse(coordinator._post_action_tasks)

    async def test_entity_registers_only_one_listener(self):
        """Entity registers only one listener."""
        coordinator = self.make_coordinator()
        entity = NukiEntity(coordinator)
        entity.async_write_ha_state = Mock()
        entity._async_update_attrs = Mock()
        await entity.async_added_to_hass()
        self.assertEqual(len(coordinator._listeners), 1)
        coordinator.async_update_listeners()
        entity.async_write_ha_state.assert_called_once()
        entity._async_update_attrs.assert_called_once()

    async def test_new_doorbell_preempts_existing_optional_transaction(self):
        """A previous log read cannot retain the Bluetooth operation lock."""
        coordinator = self.make_coordinator()
        operation_lock = asyncio.Lock()
        logs_started = asyncio.Event()
        order = []

        async def logs():
            async with operation_lock:
                logs_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(0)
                    order.append("optional read stopped")

        async def state():
            async with operation_lock:
                order.append("state read")

        coordinator._async_get_last_action_log_entry.side_effect = logs
        coordinator.device.async_update_state_only.side_effect = state
        previous_log_task = coordinator._ensure_log_refresh()
        await logs_started.wait()
        self.beacon(coordinator)
        async with asyncio.timeout(1):
            await coordinator._async_update()
        self.assertTrue(previous_log_task.cancelled())
        self.assertEqual(order[:2], ["optional read stopped", "state read"])

    async def test_foreground_action_suspends_diagnostics_and_is_owned_until_unload(self):
        """Unloading cancels an action before disconnecting or allowing retries."""
        coordinator = self.make_coordinator()
        action_started = asyncio.Event()
        order = []

        async def action():
            async with coordinator.async_action():
                action_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(0)
                    order.append("action stopped")

        async def disconnect():
            order.append("disconnected")

        coordinator.device.disconnect.side_effect = disconnect
        task = asyncio.create_task(action())
        await action_started.wait()
        self.assertIn(task, coordinator._active_actions)
        self.assertIsNone(coordinator._ensure_log_refresh())
        await coordinator.async_shutdown()
        self.assertTrue(task.cancelled())
        self.assertFalse(coordinator._active_actions)
        self.assertEqual(order, ["action stopped", "disconnected"])
        with self.assertRaises(HomeAssistantError):
            async with coordinator.async_action():
                self.fail("An unloaded coordinator cannot send an action")

    async def test_cancelled_action_does_not_interrupt_optional_disconnect_cleanup(self):
        """Cancellation while pausing diagnostics must not cancel cleanup twice."""
        coordinator = self.make_coordinator()
        log_started, disconnect_started, release_disconnect = (
            asyncio.Event(), asyncio.Event(), asyncio.Event()
        )
        cleaned_up = asyncio.Event()

        async def logs():
            log_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                disconnect_started.set()
                await release_disconnect.wait()
                cleaned_up.set()

        async def action():
            async with coordinator.async_action():
                self.fail("Cancelled action must not reach the command")

        coordinator._async_get_last_action_log_entry.side_effect = logs
        log_task = coordinator._ensure_log_refresh()
        await log_started.wait()
        action_task = asyncio.create_task(action())
        await disconnect_started.wait()
        action_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await action_task
        self.assertFalse(log_task.done())
        release_disconnect.set()
        await coordinator.async_shutdown()
        self.assertTrue(cleaned_up.is_set())
