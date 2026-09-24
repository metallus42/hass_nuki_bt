"""Integration-scoped safeguards for the pinned pyNukiBT 0.0.20 protocol.

The dependency's private transport API is intentionally isolated here. Encoded
requests must never be replayed: their challenge may already have been consumed.
Only an entire explicitly retryable operation may obtain another challenge.
"""

from __future__ import annotations

import asyncio
import logging

from bleak import BleakError
from construct import Container
import nacl.utils
from pyNukiBT import NukiConst, NukiDevice, NukiOpenerConst
from pyNukiBT.const import NukiErrorException

_LOGGER = logging.getLogger(__name__)
MODE_ATTEMPTS = 3
MODE_RETRY_DELAY = 0.5
DISCONNECT_TIMEOUT = 5


def _raise_if_cancelled() -> None:
    """Do not send if a lower transport layer swallowed task cancellation."""
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError


def _retryable_error(device: NukiDevice, error: Exception) -> bool:
    if isinstance(error, (TimeoutError, BleakError)):
        return True
    return isinstance(error, NukiErrorException) and error.error_code in (
        device._const.ErrorCode.K_ERROR_BAD_NONCE,
        device._const.ErrorCode.K_ERROR_BUSY,
    )


class SafeNukiDevice(NukiDevice):
    """Keep cancellation, request identity and secrets safe at the transport."""

    async def _send_encrypted_command(
        self, cmd, payload, aggregate_messages=None, expected_response=None,
        response_retry=None, auth_id=None, characteristic=None,
    ):
        """Encode a fresh request without logging plaintext PINs or payloads."""
        if auth_id is None:
            auth_id = self._auth_id
        if characteristic is None:
            characteristic = self._const.BLE_CHAR
        unencrypted = self._const.NukiMessage.build(
            {"auth_id": auth_id, "command": cmd, "payload": payload}
        )
        nonce = nacl.utils.random(24)
        encrypted = self._box.encrypt(unencrypted, nonce)[24:]
        message = nonce + auth_id + len(encrypted).to_bytes(2, "little") + encrypted
        _LOGGER.debug("Sending Nuki command %s", cmd)
        return await self._send_command(
            characteristic, message, aggregate_messages, expected_response,
            response_retry,
        )

    async def _safe_start_notify(self, characteristic, callback):
        """Discard late notifications from a connection that was reset."""
        client = self._client

        async def current_connection_notification(sender, data):
            if self._client is client:
                await callback(sender, data)

        await super()._safe_start_notify(characteristic, current_connection_notification)

    async def disconnect(self):
        """Invalidate notification callbacks even if disconnecting fails."""
        client, self._client = self._client, None
        if client is None:
            return
        try:
            async with asyncio.timeout(DISCONNECT_TIMEOUT):
                await client.disconnect()
        except Exception as error:
            _LOGGER.warning("Could not close the Nuki connection: %s", error)

    async def _send_command(
        self, characteristic, command, aggregate_messages=None,
        expected_response=None, response_retry=None,
    ):
        """Write once; callers own fresh-challenge retries, including reads.

        ``response_retry`` is accepted for compatibility only. Neither a lost
        response nor an uncertain GATT write permits replaying encoded bytes.
        """
        async with self._send_cmd_lock:
            future = None
            try:
                _raise_if_cancelled()
                async with asyncio.timeout(self.connection_timeout):
                    await self.connect()
                _raise_if_cancelled()
                if expected_response is not None:
                    future = asyncio.get_running_loop().create_future()
                    self._notify_future = future
                    self._expected_response = expected_response
                    self._aggregate_messages = aggregate_messages
                    self._messages = []
                async with asyncio.timeout(self.command_response_timeout):
                    await self._client.write_gatt_char(characteristic, command, response=True)
                    _raise_if_cancelled()
                    if future is None:
                        return None
                    return await future
            except asyncio.CancelledError:
                await self.disconnect()
                raise
            except (BleakError, TimeoutError) as error:
                # Invalidate late STATUS packets before another operation runs.
                self.last_action_status = type(error)
                self.last_error_command = None
                await self.disconnect()
                raise
            finally:
                if future is not None:
                    if not future.done():
                        future.cancel()
                    elif not future.cancelled():
                        future.exception()  # Consume an error arriving during a failed write.
                self._notify_future = None
                self._expected_response = None
                self._aggregate_messages = None

    async def lock_action(
        self, action, new_lock_state=None, name_suffix=None, wait_for_completed=False,
    ):
        """Keep inherited convenience methods on the confirmed action path."""
        await async_lock_action(self, action, name_suffix)
        return Container(status=self._const.StatusCode.COMPLETED)

    async def update_config(self):
        """Retry a configuration read only with a fresh challenge."""
        if self._update_config_lock.locked():
            return
        async with self._operation_lock, self._update_config_lock:
            await async_read_config_locked(self)

    async def async_update_state_only(self):
        """Read fresh state without delaying events for optional configuration."""
        async with self._update_state_lock, self._operation_lock:
            self._last_update_state_successful = False
            await _read_state_locked(self)

    async def update_state(self):
        """Preserve the public startup API with safe, separate config fetching."""
        await self.async_update_state_only()
        if self._poll_needed_config:
            await self.update_config()


async def async_read_config_locked(device: NukiDevice):
    """Read config under the caller's operation lock, with one fresh retry."""
    command = device._const.NukiCommand
    for attempt in range(2):
        try:
            challenge = await device._send_encrypted_command(
                command.REQUEST_DATA, {"command": command.CHALLENGE},
                expected_response=command.CHALLENGE, response_retry=1,
            )
            result = await device._send_encrypted_command(
                command.REQUEST_CONFIG, {"nonce": challenge["nonce"]},
                expected_response=command.CONFIG, response_retry=1,
            )
            device.config = result
            device._poll_needed_config = False
            return result
        except (BleakError, TimeoutError, NukiErrorException) as error:
            if attempt or not _retryable_error(device, error):
                raise


async def _read_state_locked(device: NukiDevice):
    """Read only current state, without optional config/log transactions."""
    command = device._const.NukiCommand
    state = await device._send_encrypted_command(
        command.REQUEST_DATA, {"command": command.KEYTURNER_STATES},
        expected_response=command.KEYTURNER_STATES, response_retry=1,
    )
    previous = device.last_state
    if not device.config or not previous or previous.get("config_update_count") != state.get("config_update_count"):
        device._poll_needed_config = True
    device.last_state = state
    device._poll_needed = False
    device._last_update_state_successful = True
    return state


async def _action_once_locked(device: NukiDevice, action, name_suffix):
    """Send one action and buffer completion arriving before its waiter starts."""
    const = device._const
    command = const.NukiCommand
    challenge = await device._send_encrypted_command(
        command.REQUEST_DATA, {"command": command.CHALLENGE},
        expected_response=command.CHALLENGE, response_retry=1,
    )
    completion = asyncio.get_running_loop().create_future()

    def notification(received_command):
        if completion.done():
            return
        if received_command == command.STATUS and device.last_action_status == const.StatusCode.COMPLETED:
            completion.set_result(None)
        elif received_command == command.ERROR_REPORT and device.last_error_command == command.LOCK_ACTION:
            completion.set_exception(NukiErrorException(device.last_action_status, command.LOCK_ACTION))

    unsubscribe = device.subscribe(notification)
    try:
        result = await device._send_encrypted_command(
            command.LOCK_ACTION,
            {"lock_action": action, "app_id": device._app_id, "flags": 0,
             "name_suffix": name_suffix, "nonce": challenge["nonce"]},
            expected_response=command.STATUS, response_retry=1,
        )
        if result["status"] == const.StatusCode.COMPLETED:
            return
        if result["status"] != const.StatusCode.ACCEPTED:
            raise RuntimeError("Nuki returned an unknown action completion status")
        async with asyncio.timeout(device.command_response_timeout):
            await completion
    except asyncio.CancelledError:
        await device.disconnect()
        raise
    except TimeoutError as error:
        device.last_action_status = type(error)
        device.last_error_command = None
        await device.disconnect()
        raise
    finally:
        unsubscribe()
        if not completion.done():
            completion.cancel()
        elif not completion.cancelled():
            completion.exception()


async def async_lock_action(device: NukiDevice, action, name_suffix=None) -> None:
    """Confirm actions; retry only explicit idempotent Opener mode requests.

    A completed protocol response confirms success. After an ambiguous outcome,
    a fresh state read can confirm the requested mode without sending it again.
    Cancellation and permanent device errors always terminate the request.
    """
    target = None
    if device.device_type == NukiConst.NukiDeviceType.OPENER:
        if action == NukiOpenerConst.LockAction.ACTIVATE_CM:
            target = NukiConst.State.CONTINUOUS_MODE
        elif action == NukiOpenerConst.LockAction.DEACTIVATE_CM:
            target = NukiConst.State.DOOR_MODE
    attempts = MODE_ATTEMPTS if target is not None else 1
    async with device._operation_lock:
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(MODE_RETRY_DELAY)
            _raise_if_cancelled()
            try:
                await _action_once_locked(device, action, name_suffix)
                return
            except (BleakError, TimeoutError, NukiErrorException) as error:
                if not _retryable_error(device, error):
                    raise
                if target is None:
                    raise RuntimeError("Nuki action outcome is unconfirmed; the action was not repeated") from error
                last_error = error
            try:
                state = await _read_state_locked(device)
                if state["nuki_state"] == target:
                    return
            except (BleakError, TimeoutError, NukiErrorException) as error:
                if not _retryable_error(device, error):
                    raise
                last_error = error
            if attempt + 1 < attempts:
                _LOGGER.debug("Nuki mode not yet confirmed; starting attempt %s of %s", attempt + 2, attempts)
        raise RuntimeError("Nuki did not confirm the requested mode after three attempts") from last_error
