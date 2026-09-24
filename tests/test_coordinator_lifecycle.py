"""Exercise integration setup/reload/unload entry points without hardware."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.const import CONF_PIN, EVENT_HOMEASSISTANT_STOP
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from pyNukiBT import NukiConst

from custom_components.hass_nuki_bt import (
    async_reload_entry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.hass_nuki_bt.const import (
    CONF_APP_ID,
    CONF_AUTH_ID,
    CONF_DEVICE_ADDRESS,
    CONF_DEVICE_PUBLIC_KEY,
    CONF_PRIVATE_KEY,
    CONF_PUBLIC_KEY,
    DOMAIN,
)


class EntryLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """Verify that HA owns reloads and every failed setup closes its connection."""

    def make_setup(self):
        """Make setup."""
        self.entry = SimpleNamespace(
            entry_id="test-entry",
            unique_id="AA:BB:CC:DD:EE:FF",
            data={
                CONF_DEVICE_ADDRESS: "AA:BB:CC:DD:EE:FF",
                CONF_AUTH_ID: "01020304",
                CONF_DEVICE_PUBLIC_KEY: "01" * 32,
                CONF_PUBLIC_KEY: "02" * 32,
                CONF_PRIVATE_KEY: "03" * 32,
                CONF_APP_ID: 42,
            },
            async_on_unload=Mock(),
            add_update_listener=Mock(return_value=Mock()),
        )
        self.hass = SimpleNamespace(
            data={},
            config_entries=SimpleNamespace(
                async_forward_entry_setups=AsyncMock(),
                async_unload_platforms=AsyncMock(return_value=True),
                async_reload=AsyncMock(return_value=True),
            ),
            bus=SimpleNamespace(async_listen_once=Mock(return_value=Mock())),
        )
        self.device = SimpleNamespace(
            connect=AsyncMock(), disconnect=AsyncMock(),
            device_type=NukiConst.NukiDeviceType.OPENER,
        )
        self.coordinator = SimpleNamespace(
            async_wait_ready=AsyncMock(return_value=True),
            async_start=Mock(),
            async_shutdown=AsyncMock(),
        )
        for target, value in (
            ("bluetooth.async_address_present", True),
            ("bluetooth.async_ble_device_from_address", SimpleNamespace(address=self.entry.unique_id)),
            ("SafeNukiDevice", self.device),
            ("NukiDataUpdateCoordinator", self.coordinator),
        ):
            patcher = patch(f"custom_components.hass_nuki_bt.{target}", return_value=value)
            boundary = patcher.start()
            if target == "NukiDataUpdateCoordinator":
                self.coordinator_factory = boundary
            self.addCleanup(patcher.stop)

    async def test_reload_uses_home_assistant_entry_lifecycle(self):
        """Reload uses home assistant entry lifecycle."""
        self.make_setup()
        await async_reload_entry(self.hass, self.entry)
        self.hass.config_entries.async_reload.assert_awaited_once_with(self.entry.entry_id)
        self.hass.config_entries.async_unload_platforms.assert_not_awaited()

    async def test_failed_initial_state_closes_connection(self):
        """Failed initial state closes connection."""
        self.make_setup()
        self.coordinator.async_wait_ready.return_value = False
        with self.assertRaises(ConfigEntryNotReady):
            await async_setup_entry(self.hass, self.entry)
        self.coordinator.async_shutdown.assert_awaited_once()
        self.assertNotIn(self.entry.entry_id, self.hass.data[DOMAIN])

    async def test_cancelled_connect_is_not_converted_to_retry(self):
        """Cancelled connect is not converted to retry."""
        self.make_setup()
        self.device.connect.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await async_setup_entry(self.hass, self.entry)
        self.device.disconnect.assert_awaited_once()
        self.coordinator_factory.assert_not_called()

    async def test_platform_setup_failure_closes_connection(self):
        """Platform setup failure closes connection."""
        self.make_setup()
        self.hass.config_entries.async_forward_entry_setups.side_effect = RuntimeError("platform setup failed")
        with self.assertRaises(RuntimeError):
            await async_setup_entry(self.hass, self.entry)
        self.coordinator.async_shutdown.assert_awaited_once()
        self.assertNotIn(self.entry.entry_id, self.hass.data[DOMAIN])

    async def test_success_registers_cleanup_and_ha_stop(self):
        """Success registers cleanup and ha stop."""
        self.make_setup()
        self.assertTrue(await async_setup_entry(self.hass, self.entry))
        self.coordinator.async_shutdown.assert_not_awaited()
        self.entry.async_on_unload.assert_any_call(self.coordinator.async_shutdown)
        self.hass.bus.async_listen_once.assert_called_once_with(
            EVENT_HOMEASSISTANT_STOP, self.coordinator.async_shutdown
        )

    async def test_unload_waits_for_connection_cleanup(self):
        """Unload waits for connection cleanup."""
        self.make_setup()
        self.hass.data[DOMAIN] = {self.entry.entry_id: self.coordinator}
        self.assertTrue(await async_unload_entry(self.hass, self.entry))
        self.coordinator.async_shutdown.assert_awaited_once()
        self.assertNotIn(self.entry.entry_id, self.hass.data[DOMAIN])

    async def test_failed_platform_unload_keeps_live_coordinator(self):
        """Failed platform unload keeps live coordinator."""
        self.make_setup()
        self.hass.data[DOMAIN] = {self.entry.entry_id: self.coordinator}
        self.hass.config_entries.async_unload_platforms.return_value = False
        self.assertFalse(await async_unload_entry(self.hass, self.entry))
        self.coordinator.async_shutdown.assert_not_awaited()
        self.assertIs(self.hass.data[DOMAIN][self.entry.entry_id], self.coordinator)

    async def test_invalid_opener_pin_disconnects_before_state_or_logs(self):
        """A legacy or offline-imported PIN must fit the connected device type."""
        self.make_setup()
        for invalid_pin in (65536, -1, True, "not-a-pin"):
            with self.subTest(pin=invalid_pin):
                self.entry.data[CONF_PIN] = invalid_pin
                with self.assertRaisesRegex(ConfigEntryError, "Reconfigure"):
                    await async_setup_entry(self.hass, self.entry)
                self.coordinator_factory.assert_not_called()
                self.coordinator.async_wait_ready.assert_not_awaited()
                self.device.disconnect.assert_awaited()
                self.assertNotIn(self.entry.entry_id, self.hass.data[DOMAIN])
        self.assertEqual(self.device.disconnect.await_count, 4)

    async def test_ultra_pin_keeps_full_32_bit_range(self):
        """Valid Ultra PINs are not rejected by the Opener's narrower bound."""
        self.make_setup()
        self.device.device_type = NukiConst.NukiDeviceType.SMARTLOCK_ULTRA
        self.entry.data[CONF_PIN] = 0xFFFFFFFF
        self.assertTrue(await async_setup_entry(self.hass, self.entry))
        self.assertEqual(self.coordinator_factory.call_args.kwargs["security_pin"], 0xFFFFFFFF)
        self.device.disconnect.assert_not_awaited()
