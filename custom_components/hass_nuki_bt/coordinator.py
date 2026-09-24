"""DataUpdateCoordinator for hass_nuki_bt."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

import async_timeout

from bleak import BleakError

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.active_update_coordinator import (
    ActiveBluetoothDataUpdateCoordinator,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from pyNukiBT import NukiDevice, NukiConst, NukiOpenerConst
from pyNukiBT.const import NukiErrorException

from .logs import async_request_log_entries

if TYPE_CHECKING:
    from bleak.backends.device import BLEDevice

_LOGGER = logging.getLogger(__name__)

DEVICE_STARTUP_TIMEOUT = 300


class NukiDataUpdateCoordinator(ActiveBluetoothDataUpdateCoordinator[None]):
    """Class to manage fetching Nuki data."""

    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        ble_device: BLEDevice,
        device: NukiDevice,
        base_unique_id: str,
        device_name: str,
        connectable: bool,
        security_pin: int = None,
    ) -> None:
        """Initialize global nuki data updater."""
        super().__init__(
            hass=hass,
            logger=logger,
            address=ble_device.address,
            needs_poll_method=self._needs_poll,
            poll_method=self._async_update,
            mode=bluetooth.BluetoothScanningMode.PASSIVE,
            connectable=connectable,
        )
        self.ble_device = ble_device
        self.device = device
        self.device_name = device_name
        self.base_unique_id = base_unique_id
        self.model = None
        self.last_nuki_log_entry = {"index" : 0}
        self._security_pin = security_pin
        self._unsubscribe_nuki_callbacks = None
        self._doorbell_callbacks: list[Callable[[], None]] = []
        self._doorbell_candidate_state: tuple[int, int] | None = None
        self._doorbell_generation = 0
        self._action_generation = 0
        self._post_action_tasks: set[asyncio.Task] = set()
        self._active_actions: set[asyncio.Task] = set()
        self._state_refresh_task: asyncio.Task | None = None
        self._log_task: asyncio.Task | None = None
        self._stopped = False
        self._started = False
        self._disconnected = False

    @callback
    def _async_start(self) -> None:
        self._started = True
        self._unsubscribe_nuki_callbacks = self.device.subscribe(
            self._nuki_device_callback
        )
        return super()._async_start()

    @callback
    def _async_stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        for task in self._pending_tasks():
            if not task.cancelling():
                task.cancel()
        if self._unsubscribe_nuki_callbacks is not None:
            self._unsubscribe_nuki_callbacks()
            self._unsubscribe_nuki_callbacks = None
        if self._started:
            super()._async_stop()

    def _pending_tasks(self) -> set[asyncio.Task]:
        """Return outstanding reads and actions, excluding the current task."""
        return {
            task
            for task in (
                *self._post_action_tasks, *self._active_actions,
                self._state_refresh_task, self._log_task,
            )
            if task is not None and not task.done() and task is not asyncio.current_task()
        }

    async def async_shutdown(self, _event=None) -> None:
        """Stop callbacks and await cancelled reads before releasing Bluetooth."""
        self._async_stop()
        if tasks := self._pending_tasks():
            await asyncio.gather(*tasks, return_exceptions=True)
        if not self._disconnected:
            await self.device.disconnect()
            self._disconnected = True

    async def async_pause_optional_reads(self) -> None:
        """Release optional Bluetooth work before a state request or action."""
        task = self._log_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            if not task.cancelling():
                task.cancel()
            # A cancelled automation must not cancel the read a second time
            # while its transport is already disconnecting the old session.
            await asyncio.shield(asyncio.gather(task, return_exceptions=True))
        if self._log_task is task:
            self._log_task = None

    @contextlib.asynccontextmanager
    async def async_action(self):
        """Own a foreground action until it completes or the entry unloads."""
        if self._stopped:
            raise HomeAssistantError("The Nuki integration is unloading")
        task = asyncio.current_task()
        self._active_actions.add(task)
        try:
            await self.async_pause_optional_reads()
            if self._stopped:
                raise HomeAssistantError("The Nuki integration is unloading")
            yield
        finally:
            self._active_actions.discard(task)

    @callback
    def _nuki_device_callback(self, command: NukiConst.NukiCommand = None) -> None:
        if not self._stopped:
            self.async_update_listeners()

    @callback
    def _needs_poll(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        seconds_since_last_poll: float | None,
    ) -> bool:
        return self.device.poll_needed(seconds_since_last_poll)

    async def _async_update(
        self, service_info: bluetooth.BluetoothServiceInfoBleak = None
    ) -> None:
        """Share a state refresh; optional diagnostics never delay its result."""
        if self._stopped:
            return
        await self.async_pause_optional_reads()
        if self._stopped:
            return
        if service_info:
            self.device.set_ble_device(service_info.device)
        if self._state_refresh_task is None or self._state_refresh_task.done():
            self._state_refresh_task = self.hass.async_create_background_task(
                self._async_refresh_state(), "Nuki state refresh"
            )
        await asyncio.shield(self._state_refresh_task)
        if not self._stopped:
            self._ensure_log_refresh()

    async def _async_refresh_state(self) -> None:
        """Match a beacon only to a state request started after that beacon."""
        while not self._stopped:
            generation = self._doorbell_generation
            action_generation = self._action_generation
            candidate_state = self._doorbell_candidate_state
            await self.device.async_update_state_only()
            self.async_update_listeners()

            # A beacon received during this request needs a new state read.
            # Never consume it using the response of an earlier request.
            if (
                generation != self._doorbell_generation
                or action_generation != self._action_generation
            ):
                continue
            self._doorbell_candidate_state = None
            if (
                candidate_state
                and candidate_state == self._opener_state_signature()
                and candidate_state[0] == int(NukiConst.State.DOOR_MODE)
                and candidate_state[1] == int(NukiOpenerConst.LockState.LOCKED)
            ):
                _LOGGER.debug("Nuki Opener doorbell press detected")
                for listener in tuple(self._doorbell_callbacks):
                    listener()
            return

    @callback
    def async_add_doorbell_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register a listener for detected Nuki Opener doorbell presses."""
        self._doorbell_callbacks.append(callback)

        def _unsubscribe() -> None:
            self._doorbell_callbacks.remove(callback)

        return _unsubscribe

    def _opener_state_signature(self) -> tuple[int, int] | None:
        """Return the state fields which stay unchanged for a doorbell press."""
        state = self.device.keyturner_state
        if not state:
            return None
        return (int(state["nuki_state"]), int(state["lock_state"]))

    @callback
    def _async_handle_bluetooth_event(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Handle a Bluetooth event."""
        if self._stopped:
            return
        self.ble_device = service_info.device

        # pyNukiBT already recognizes this status-change bit and schedules a
        # state poll. Keep the state immediately before that poll; an Opener
        # doorbell press is the documented LOCKED -> LOCKED state transition.
        # Repeated advertisements for that pending change share one candidate.
        manufacturer_data = service_info.advertisement.manufacturer_data.get(76)
        if (
            self.device.device_type == NukiConst.NukiDeviceType.OPENER
            and manufacturer_data
            and manufacturer_data[0] == 0x02
            and manufacturer_data[-1] & 0x01
            and self._doorbell_candidate_state is None
        ):
            self._doorbell_candidate_state = self._opener_state_signature()
            self._doorbell_generation += 1
        self.device.parse_advertisement_data(
            service_info.device, service_info.advertisement
        )
        super()._async_handle_bluetooth_event(service_info, change)

    async def async_wait_ready(self) -> bool:
        """Wait for the device to be ready."""
        with contextlib.suppress(asyncio.TimeoutError):
            async with async_timeout.timeout(DEVICE_STARTUP_TIMEOUT):
                try:
                    # Entity setup needs model/configuration fields, unlike a
                    # doorbell refresh. Initial diagnostics remain optional.
                    await self.device.update_state()
                except (BleakError, NukiErrorException):
                    return False
                self._ensure_log_refresh()
                return True
        return False

    @callback
    def async_refresh_after_action(self) -> None:
        """Refresh independently of the automation that issued the command.

        Publishing the new state can restart that automation. Its cancellation
        must not interrupt a log transaction or change an acknowledged result.
        """
        if self._stopped:
            return
        self._action_generation += 1
        if any(not task.done() for task in self._post_action_tasks):
            return
        task = self.hass.async_create_background_task(
            self._async_refresh_after_action(), "Nuki state and action log refresh"
        )
        self._post_action_tasks.add(task)
        task.add_done_callback(self._post_action_tasks.discard)

    async def _async_refresh_after_action(self) -> None:
        """Read the current state before fetching optional log details."""
        try:
            while not self._stopped:
                generation = self._action_generation
                await self._async_update()
                if generation == self._action_generation:
                    break
        except (BleakError, asyncio.TimeoutError, NukiErrorException) as err:
            _LOGGER.warning("Nuki action completed, but state refresh failed: %s", err)
        finally:
            if not self._stopped:
                self.async_update_listeners()

    @callback
    def _ensure_log_refresh(self) -> asyncio.Task | None:
        """Share optional configuration and log reads outside state refreshes."""
        if self._stopped or self._active_actions or (
            self._state_refresh_task is not None and not self._state_refresh_task.done()
        ) or (
            self._security_pin is None and not self.device._poll_needed_config
        ):
            return None
        if self._log_task is None or self._log_task.done():
            self._log_task = self.hass.async_create_background_task(
                self._async_refresh_logs(), "Nuki configuration and action log refresh"
            )
        return self._log_task

    async def async_get_last_action_log_entry(self):
        """Join the shared optional log refresh without owning its lifetime."""
        if task := self._ensure_log_refresh():
            await asyncio.shield(task)

    async def _async_refresh_logs(self) -> None:
        """Keep optional log failures from breaking state updates or commands."""
        if self.device._poll_needed_config:
            try:
                await self.device.update_config()
            except (BleakError, asyncio.TimeoutError, NukiErrorException, RuntimeError) as err:
                _LOGGER.warning("Could not refresh optional Nuki configuration: %s", err)
        try:
            await self._async_get_last_action_log_entry()
        except (BleakError, asyncio.TimeoutError, NukiErrorException, RuntimeError) as err:
            _LOGGER.warning("Could not refresh optional Nuki action log: %s", err)
        finally:
            if not self._stopped:
                self.async_update_listeners()

    async def _async_get_last_action_log_entry(self):
        """Get the last action log entry while preserving the last valid cache."""
        if self._security_pin is not None: #security pin can be 0, so check for None
            # get the latest log entry
            # todo: check if Nuki logging is enabled
            logs = await async_request_log_entries(
                self.device,
                security_pin=self._security_pin, count=1
            )
            if logs:
                if logs[0].type in [NukiConst.LogEntryType.LOCK_ACTION, NukiConst.LogEntryType.KEYPAD_ACTION]:
                    # todo: handle other log types
                    self.last_nuki_log_entry = logs[0]
                elif logs[0].index > self.last_nuki_log_entry["index"]:
                    # if there are new log entries, get max 10 entries
                    logs = await async_request_log_entries(
                        self.device,
                        security_pin=self._security_pin,
                        count=min(10, logs[0].index - self.last_nuki_log_entry["index"]),
                        start_index=logs[0].index,
                    )
                    for log in logs:
                        if log.type in [NukiConst.LogEntryType.LOCK_ACTION, NukiConst.LogEntryType.KEYPAD_ACTION]:
                            self.last_nuki_log_entry = log
                            break
