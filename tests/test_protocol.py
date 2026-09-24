"""Exercise actual pinned protocol serialization and notifications without BLE."""

import asyncio
from datetime import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bleak import BleakError
from construct import Container
from nacl.secret import SecretBox
from pyNukiBT import NukiConst, NukiOpenerConst
from pyNukiBT.const import NukiErrorException, crcCalc

from custom_components.hass_nuki_bt.protocol import SafeNukiDevice, async_lock_action


class FakeClient:
    """Represent one connection; every request still uses the real wire codec."""

    def __init__(self, hardware):
        """Store the deterministic device-side simulation."""
        self.hardware = hardware
        self.is_connected = True
        self.notify = None

    async def disconnect(self):
        """Close this simulated connection."""
        self.is_connected = False

    async def start_notify(self, characteristic, callback):
        """Remember the registered callback for the stale-response regression."""
        self.notify = callback

    async def write_gatt_char(self, characteristic, data, response):
        """Decode and process a single real encrypted request."""
        await self.hardware.write(data)


class Hardware:
    """Return serialized Nuki messages to the dependency notification parser."""

    def __init__(self, plans=()):
        """Create an authenticated in-memory Opener with no Bluetooth backend."""
        self.device = SafeNukiDevice(
            address="00:00:00:00:00:00", auth_id=b"\x01\x02\x03\x04",
            nuki_public_key=None, bridge_public_key=None, bridge_private_key=None,
            app_id=1, name="Protocol test",
        )
        self.device._const = NukiOpenerConst
        self.device._device_type = NukiConst.NukiDeviceType.OPENER
        self.device._box = SecretBox(bytes(32))
        self.device.command_response_timeout = 0.01
        self.device.config = {"configured": True}
        self.state = Container(
            nuki_state=NukiConst.State.CONTINUOUS_MODE,
            lock_state=NukiOpenerConst.LockState.LOCKED,
            trigger=0, current_time=datetime(2026, 9, 24), timezone_offset=0,
            critical_battery_state=0, config_update_count=0, ring_to_open_timer=0,
            last_lock_action=0, last_lock_action_trigger=0,
            last_lock_action_completion_status=0, door_sensor_state=0,
        )
        self.device.last_state = self.state.copy()
        self.device.connect = self.connect
        self.plans = list(plans)
        self.requests = []
        self.nonces = []
        self.used_nonces = set()
        self.clients = []
        self.responses = 0
        self.action_started = asyncio.Event()

    async def connect(self):
        """Allocate a fresh client after timeout-driven disconnection."""
        if self.device._client is None:
            client = FakeClient(self)
            self.clients.append(client)
            self.device._client = client

    async def respond(self, command, payload):
        """Deliver an authenticated response through the real notification code."""
        self.responses += 1
        device = self.device
        plaintext = device._const.NukiMessage.build(
            {"auth_id": device._auth_id, "command": command, "payload": payload}
        )
        nonce = self.responses.to_bytes(24, "little")
        encrypted = device._box.encrypt(plaintext, nonce)[24:]
        message = nonce + device._auth_id + len(encrypted).to_bytes(2, "little") + encrypted
        await device._notification_handler(SimpleNamespace(uuid=device._const.BLE_CHAR), message)

    async def write(self, encrypted_request):
        """Simulate completion, loss, or an explicit Nuki rejection."""
        device = self.device
        plaintext = device._decrypt_message(encrypted_request)
        outgoing_command = device._const.NukiCommand.parse(plaintext[4:6])
        if outgoing_command == device._const.NukiCommand.LOCK_ACTION:
            # The library's outgoing-only optional name suffix parser greedily
            # consumes a missing suffix from the nonce. Check the documented
            # action/nonce offsets and CRC instead of that asymmetric parser.
            assert crcCalc.calc(plaintext[:-2]) == int.from_bytes(plaintext[-2:], "little")
            request = Container(command=outgoing_command, payload=Container(
                lock_action=device._const.LockAction.parse(plaintext[6:7]),
                nonce=plaintext[-34:-2],
            ))
        else:
            request = device._const.NukiMessage.parse(plaintext)
        command, payload = request.command, request.payload
        self.requests.append(request)
        cmds = device._const.NukiCommand
        if command == cmds.REQUEST_DATA:
            if payload.command == cmds.CHALLENGE:
                nonce = (len(self.nonces) + 1).to_bytes(32, "little")
                self.nonces.append(nonce)
                await self.respond(cmds.CHALLENGE, {"nonce": nonce})
            else:
                await self.respond(cmds.KEYTURNER_STATES, self.state)
            return
        if command == cmds.VERIFY_SECURITY_PIN:
            await self.respond(cmds.STATUS, {"status": NukiConst.StatusCode.COMPLETED})
            return
        assert command == cmds.LOCK_ACTION
        assert payload.nonce == self.nonces[-1]
        assert payload.nonce not in self.used_nonces
        self.used_nonces.add(payload.nonce)
        self.action_started.set()
        plan = self.plans.pop(0) if self.plans else "completed"
        if plan == "write_error":
            raise BleakError("write acknowledgement lost")
        if plan in ("completed", "accepted_completed", "lost_applied"):
            if payload.lock_action == NukiOpenerConst.LockAction.DEACTIVATE_CM:
                self.state.nuki_state = NukiConst.State.DOOR_MODE
            elif payload.lock_action == NukiOpenerConst.LockAction.ACTIVATE_CM:
                self.state.nuki_state = NukiConst.State.CONTINUOUS_MODE
        if plan in ("accepted", "accepted_completed"):
            await self.respond(cmds.STATUS, {"status": NukiConst.StatusCode.ACCEPTED})
        if plan in ("completed", "accepted_completed"):
            await self.respond(cmds.STATUS, {"status": NukiConst.StatusCode.COMPLETED})
        elif plan not in ("accepted", "lost", "lost_applied"):
            await self.respond(cmds.ERROR_REPORT, {
                "error_code": plan, "command_identifier": cmds.LOCK_ACTION,
            })

    @property
    def actions(self):
        """Return actual action writes, excluding challenge and state requests."""
        return [r for r in self.requests if r.command == NukiOpenerConst.NukiCommand.LOCK_ACTION]


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    """Verify transport boundaries and action outcomes using actual wire types."""

    def setUp(self):
        """Keep retry pauses out of deterministic offline tests."""
        patcher = patch("custom_components.hass_nuki_bt.protocol.MODE_RETRY_DELAY", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_completed_and_completion_before_waiter(self):
        """Both direct completion and immediate ACCEPTED/COMPLETED are valid."""
        for plan in ("completed", "accepted_completed"):
            hardware = Hardware([plan])
            await async_lock_action(hardware.device, NukiOpenerConst.LockAction.DEACTIVATE_CM)
            self.assertEqual(len(hardware.actions), 1)
            self.assertFalse(hardware.device._callbacks)

    async def test_accepted_only_is_not_success_and_open_is_never_replayed(self):
        """A truthy ACCEPTED Container cannot confirm a physical strike action."""
        hardware = Hardware(["accepted"])
        with self.assertRaisesRegex(RuntimeError, "unconfirmed"):
            await async_lock_action(hardware.device, NukiOpenerConst.LockAction.ELECTRIC_STRIKE_ACTUATION)
        self.assertEqual(len(hardware.actions), 1)
        self.assertFalse(hardware.device._callbacks)

    async def test_timeout_diagnostics_do_not_keep_an_old_completion(self):
        """Both response and completion timeouts replace stale success status."""
        for plan in ("lost", "accepted"):
            hardware = Hardware([plan])
            hardware.device.last_action_status = NukiConst.StatusCode.COMPLETED
            with self.assertRaises(RuntimeError):
                await async_lock_action(hardware.device, NukiOpenerConst.LockAction.ELECTRIC_STRIKE_ACTUATION)
            self.assertIs(hardware.device.last_action_status, TimeoutError)
            self.assertIsNone(hardware.device.last_error_command)

    async def test_lost_completion_with_target_state_does_not_resend(self):
        """An already achieved mode confirms success despite a lost response."""
        hardware = Hardware(["lost_applied"])
        await async_lock_action(hardware.device, NukiOpenerConst.LockAction.DEACTIVATE_CM)
        self.assertEqual(len(hardware.actions), 1)
        self.assertEqual(hardware.device.last_state.nuki_state, NukiConst.State.DOOR_MODE)
        self.assertGreaterEqual(len(hardware.clients), 2)

    async def test_three_attempt_limit_and_fresh_challenges(self):
        """Repeated ambiguous failures terminate after three distinct requests."""
        hardware = Hardware(["lost", "lost", "lost"])
        with self.assertRaisesRegex(RuntimeError, "three attempts"):
            await async_lock_action(hardware.device, NukiOpenerConst.LockAction.DEACTIVATE_CM)
        self.assertEqual(len(hardware.actions), 3)
        self.assertEqual(len(set(hardware.nonces)), 3)
        self.assertFalse(hardware.device._operation_lock.locked())

    async def test_transient_errors_can_retry_mode_requests(self):
        """BUSY, BAD_NONCE and uncertain writes allow a bounded fresh attempt."""
        for failure in (NukiOpenerConst.ErrorCode.K_ERROR_BUSY, NukiOpenerConst.ErrorCode.K_ERROR_BAD_NONCE, "write_error"):
            hardware = Hardware([failure, "completed"])
            await async_lock_action(hardware.device, NukiOpenerConst.LockAction.DEACTIVATE_CM)
            self.assertEqual(len(hardware.actions), 2)
            self.assertNotEqual(hardware.actions[0].payload.nonce, hardware.actions[1].payload.nonce)

    async def test_permanent_rejections_are_not_retried(self):
        """Authorization failures and a physical cancel immediately terminate."""
        for error in (NukiOpenerConst.ErrorCode.K_ERROR_NOT_AUTHORIZED, NukiOpenerConst.ErrorCode.K_ERROR_BAD_PIN, NukiOpenerConst.ErrorCode.K_ERROR_CANCELED):
            hardware = Hardware([error])
            with self.assertRaises(NukiErrorException):
                await async_lock_action(hardware.device, NukiOpenerConst.LockAction.DEACTIVATE_CM)
            self.assertEqual(len(hardware.actions), 1)

    async def test_write_error_never_replays_a_physical_action(self):
        """The dependency's inner GATT retry loop cannot duplicate the write."""
        hardware = Hardware(["write_error", "completed"])
        hardware.device.send_retry = 10
        hardware.device.response_retry = 3
        with self.assertRaisesRegex(RuntimeError, "not repeated"):
            await async_lock_action(hardware.device, NukiOpenerConst.LockAction.ELECTRIC_STRIKE_ACTUATION)
        self.assertEqual(len(hardware.actions), 1)

    async def test_cancellation_before_connect_sends_nothing(self):
        """An automation cancelled during connection cannot send afterwards."""
        hardware = Hardware()
        connecting = asyncio.Event()

        async def blocked_connect():
            connecting.set()
            await asyncio.Event().wait()

        hardware.device.connect = blocked_connect
        task = asyncio.create_task(async_lock_action(hardware.device, NukiOpenerConst.LockAction.DEACTIVATE_CM))
        await asyncio.wait_for(connecting.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(hardware.requests)

    async def test_cancellation_during_response_and_completion_wait(self):
        """Both wait stages propagate cancellation and never start a retry."""
        for plan in ("lost", "accepted"):
            hardware = Hardware([plan])
            hardware.device.command_response_timeout = 10
            task = asyncio.create_task(async_lock_action(hardware.device, NukiOpenerConst.LockAction.DEACTIVATE_CM))
            await asyncio.wait_for(hardware.action_started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(len(hardware.actions), 1)
            self.assertFalse(hardware.device._operation_lock.locked())
            self.assertFalse(hardware.device._callbacks)

    async def test_newer_mode_request_runs_after_older_retries(self):
        """Serialized desired modes cannot be reversed by an older retry."""
        hardware = Hardware(["lost", "completed", "completed"])
        hardware.state.nuki_state = NukiConst.State.DOOR_MODE
        first = asyncio.create_task(async_lock_action(hardware.device, NukiOpenerConst.LockAction.ACTIVATE_CM))
        await asyncio.wait_for(hardware.action_started.wait(), 1)
        second = asyncio.create_task(async_lock_action(hardware.device, NukiOpenerConst.LockAction.DEACTIVATE_CM))
        await asyncio.gather(first, second)
        self.assertEqual([r.payload.lock_action for r in hardware.actions], [NukiOpenerConst.LockAction.ACTIVATE_CM, NukiOpenerConst.LockAction.ACTIVATE_CM, NukiOpenerConst.LockAction.DEACTIVATE_CM])
        self.assertEqual(hardware.state.nuki_state, NukiConst.State.DOOR_MODE)

    async def test_old_connection_notifications_are_discarded(self):
        """Late completion from a reset client cannot confirm the next command."""
        hardware = Hardware()
        await hardware.connect()
        old_client = hardware.device._client
        received = AsyncMock()
        await hardware.device._safe_start_notify("characteristic", received)
        await hardware.device.disconnect()
        await hardware.connect()
        await old_client.notify("sender", b"old response")
        received.assert_not_awaited()

    async def test_disconnect_failure_does_not_mask_cancellation(self):
        """Cleanup failure cannot turn an explicit cancel into a retry."""
        hardware = Hardware(["lost"])
        hardware.device.command_response_timeout = 10
        await hardware.connect()
        hardware.device._client.disconnect = AsyncMock(side_effect=RuntimeError("disconnect failed"))
        task = asyncio.create_task(async_lock_action(hardware.device, NukiOpenerConst.LockAction.DEACTIVATE_CM))
        await asyncio.wait_for(hardware.action_started.wait(), 1)
        with self.assertLogs("custom_components.hass_nuki_bt.protocol", level="WARNING"):
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(len(hardware.actions), 1)
        self.assertIsNone(hardware.device._client)
        self.assertFalse(hardware.device._operation_lock.locked())

    async def test_debug_logging_never_contains_security_pin(self):
        """Use real encrypted encoding while checking every debug log record."""
        hardware = Hardware()
        with self.assertLogs(level="DEBUG") as logs:
            await hardware.device._send_encrypted_command(
                NukiOpenerConst.NukiCommand.VERIFY_SECURITY_PIN,
                {"nonce": bytes(32), "security_pin": 54321},
                expected_response=NukiOpenerConst.NukiCommand.STATUS,
            )
        self.assertNotIn("54321", "\n".join(logs.output))
        self.assertNotIn("security_pin", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
