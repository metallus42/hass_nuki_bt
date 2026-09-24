"""Protocol regression tests, run with the pinned pyNukiBT dependency installed."""

import asyncio
import struct
import unittest

from pyNukiBT import NukiConst, NukiOpenerConst

from custom_components.hass_nuki_bt import pairing


class FakeOpener:
    """Simulate challenge-protected config operations and real wire encoding."""

    device_type = NukiConst.NukiDeviceType.OPENER
    _const = NukiOpenerConst

    def __init__(self):
        """Set up a device with distinct values for its writable settings."""
        self._operation_lock = asyncio.Lock()
        self._poll_needed_config = False
        self.config = {"pairing_enabled": 99}  # Stale cache must never be written.
        self.current = {
            "name": "Test Opener", "latitude": 0.0, "longitude": 0.0,
            "capabilities": 1, "pairing_enabled": 0, "button_enabled": 1,
            "led_enabled": 0, "timezone_offset": 60, "dst_mode": 1,
            "fob_action_1": 7, "fob_action_2": 3, "fob_action_3": 0,
            "operating_mode": 6, "advertising_mode": 1, "timezone_id": 37,
        }
        self.commands = []
        self.nonces = []
        self.sent = None
        self.persist = True
        self.change_other_setting = False
        self.status = NukiOpenerConst.StatusCode.COMPLETED
        self.config_reads = 0
        self.config_read_errors = {}
        self.used_nonces = set()

    async def _send_encrypted_command(self, command, payload, **kwargs):
        assert self._operation_lock.locked()
        self.commands.append(command)
        cmds = NukiOpenerConst.NukiCommand
        if command == cmds.REQUEST_DATA:
            nonce = bytes([len(self.nonces) + 1]) * 32
            self.nonces.append(nonce)
            return {"nonce": nonce}
        assert payload["nonce"] == self.nonces[-1]
        assert payload["nonce"] not in self.used_nonces
        self.used_nonces.add(payload["nonce"])
        if command == cmds.REQUEST_CONFIG:
            assert kwargs["response_retry"] == 1
            self.config_reads += 1
            if error := self.config_read_errors.get(self.config_reads):
                raise error
            return self.current.copy()
        if command == cmds.SET_CONFIG:
            assert kwargs["response_retry"] == 1
            self.sent = payload.copy()
            # Exercise the actual library message serializer, including CRC.
            self.wire = NukiOpenerConst.NukiMessage.build(
                {"auth_id": b"\x01\x02\x03\x04", "command": command, "payload": payload}
            )
            if self.persist:
                self.current["pairing_enabled"] = payload["pairing_enabled"]
            if self.change_other_setting:
                self.current["button_enabled"] = 0
            return {"status": self.status}
        raise AssertionError(f"Unexpected command: {command}")


class PairingTests(unittest.IsolatedAsyncioTestCase):
    """Check the protocol contract and failure handling."""

    async def test_enable_preserves_settings_and_wire_layout_with_pin_zero(self):
        """Use fresh settings and encode the documented byte offsets."""
        device = FakeOpener()
        before = device.current.copy()
        self.assertTrue(await pairing.async_set_opener_pairing_enabled(device, 0, True))
        self.assertEqual(device.current, before | {"pairing_enabled": 1})
        self.assertEqual(len(device.nonces), 3)
        self.assertEqual(device.sent["nonce"], bytes([2]) * 32)
        self.assertEqual(device.sent["security_pin"], 0)
        # Nuki Opener API: auth (4), command (2), config (54), nonce (32), PIN (2), CRC (2).
        self.assertEqual(len(device.wire), 96)
        self.assertEqual(device.wire[4:6], struct.pack("<H", 0x0013))
        self.assertEqual(device.wire[47], 1)  # pairing after name, lat/lon, capabilities
        self.assertEqual(device.wire[60:92], bytes([2]) * 32)
        self.assertEqual(device.wire[92:94], b"\x00\x00")
        self.assertFalse(device._poll_needed_config)

    async def test_already_enabled_does_not_write(self):
        """Avoid unnecessary writes when the permission already matches."""
        device = FakeOpener()
        device.current["pairing_enabled"] = 1
        self.assertTrue(await pairing.async_set_opener_pairing_enabled(device, 1234, True))
        self.assertIsNone(device.sent)

    async def test_disable(self):
        """Support restoring the original pairing restriction."""
        device = FakeOpener()
        device.current["pairing_enabled"] = 1
        self.assertFalse(await pairing.async_set_opener_pairing_enabled(device, 1234, False))
        self.assertEqual(device.current["pairing_enabled"], 0)

    async def test_invalid_input_never_sends_commands(self):
        """Reject invalid inputs before contacting the device."""
        for pin, enabled in [(None, True), (-1, True), (65536, True), (False, True), (0, 1)]:
            device = FakeOpener()
            with self.assertRaises(ValueError):
                await pairing.async_set_opener_pairing_enabled(device, pin, enabled)
            self.assertEqual(device.commands, [])

    async def test_lock_is_rejected(self):
        """Never use the Opener payload for a Smart Lock."""
        device = FakeOpener()
        device.device_type = NukiConst.NukiDeviceType.SMARTLOCK_1_2
        with self.assertRaises(ValueError):
            await pairing.async_set_opener_pairing_enabled(device, 0, True)
        self.assertEqual(device.commands, [])

    async def test_missing_field_never_writes(self):
        """Never invent default values for missing settings."""
        device = FakeOpener()
        del device.current["capabilities"]
        with self.assertRaises(KeyError):
            await pairing.async_set_opener_pairing_enabled(device, 0, True)
        self.assertIsNone(device.sent)

    async def test_readback_must_confirm_setting(self):
        """Require persistence rather than trusting an acknowledgement."""
        device = FakeOpener()
        device.persist = False
        with self.assertRaisesRegex(RuntimeError, "did not retain"):
            await pairing.async_set_opener_pairing_enabled(device, 0, True)

    async def test_other_config_change_is_reported(self):
        """Detect unexpected changes to unrelated settings."""
        device = FakeOpener()
        device.change_other_setting = True
        with self.assertRaisesRegex(RuntimeError, "Another Opener"):
            await pairing.async_set_opener_pairing_enabled(device, 0, True)

    async def test_rejected_status_is_not_success(self):
        """Propagate rejection and leave a config refresh pending."""
        device = FakeOpener()
        device.status = -1
        with self.assertRaisesRegex(RuntimeError, "did not confirm"):
            await pairing.async_set_opener_pairing_enabled(device, 0, True)
        self.assertTrue(device._poll_needed_config)

    async def test_lost_readback_uses_fresh_nonce_without_repeating_write(self):
        """A confirmed config write survives one lost verification response."""
        device = FakeOpener()
        device.config_read_errors = {2: TimeoutError()}
        self.assertTrue(await pairing.async_set_opener_pairing_enabled(device, 0, True))
        self.assertEqual(device.commands.count(NukiOpenerConst.NukiCommand.SET_CONFIG), 1)
        self.assertEqual(len(device.nonces), 4)
        self.assertEqual(len(device.used_nonces), 4)

    async def test_failed_readback_reports_confirmation_separately(self):
        """Do not claim an acknowledged setting failed or resend its write."""
        device = FakeOpener()
        device.config_read_errors = {2: TimeoutError(), 3: TimeoutError()}
        with self.assertRaisesRegex(RuntimeError, "confirmed.*verification failed"):
            await pairing.async_set_opener_pairing_enabled(device, 0, True)
        self.assertEqual(device.current["pairing_enabled"], 1)
        self.assertEqual(device.commands.count(NukiOpenerConst.NukiCommand.SET_CONFIG), 1)
        self.assertTrue(device._poll_needed_config)

    async def test_cancel_during_verification_is_not_retried(self):
        """Cancellation after the setting is acknowledged must still propagate."""
        device = FakeOpener()
        device.config_read_errors = {2: asyncio.CancelledError()}
        with self.assertRaises(asyncio.CancelledError):
            await pairing.async_set_opener_pairing_enabled(device, 0, True)
        self.assertEqual(device.config_reads, 2)
        self.assertFalse(device._operation_lock.locked())


if __name__ == "__main__":
    unittest.main()
