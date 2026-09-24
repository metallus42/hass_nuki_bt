"""Check duplicate identity against Home Assistant's real entry/flow indexes."""

from tempfile import TemporaryDirectory
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.const import CONF_NAME, CONF_PIN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.hass_nuki_bt.config_flow import NukiFlowHandler
from custom_components.hass_nuki_bt.const import CONF_CLIENT_TYPE, CONF_DEVICE_ADDRESS, DOMAIN


class FlowIdentityTests(unittest.IsolatedAsyncioTestCase):
    """Run actual HA flow lifecycle while substituting only integration loading."""

    async def asyncSetUp(self):
        """Create isolated managers with their actual indexes and filtering."""
        self.directory = TemporaryDirectory()
        self.hass = HomeAssistant(self.directory.name)
        self.entries = ConfigEntries(self.hass, {})
        self.hass.config_entries = self.entries

        async def create_flow(handler, *, context, data):
            flow = NukiFlowHandler()
            flow.init_step = context["source"]
            return flow

        patcher = patch.object(self.entries.flow, "async_create_flow", side_effect=create_flow)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The integration supports multiple devices. Substitute only the
        # manifest-loading boundary; identity and progress indexes remain real.
        metadata = patch("homeassistant.config_entries._support_single_config_entry_only", new=AsyncMock(return_value=False))
        metadata.start()
        self.addCleanup(metadata.stop)
        self.device_patcher = patch("custom_components.hass_nuki_bt.config_flow.SafeNukiDevice")
        self.device = self.device_patcher.start()
        self.addCleanup(self.device_patcher.stop)

    async def asyncTearDown(self):
        """Abort isolated forms without persisting or starting integrations."""
        for progress in self.entries.flow.async_progress():
            self.entries.flow.async_abort(progress["flow_id"])
        self.device.assert_not_called()
        self.directory.cleanup()

    async def add_entry(self, *, source="user", unique_id="aabbccddeeff"):
        """Add through HA's real lifecycle but skip integration setup and disk IO."""
        entry = ConfigEntry(
            domain=DOMAIN, title="Existing door", source=source, version=1,
            minor_version=1, unique_id=unique_id, options={}, subentries_data=[],
            discovery_keys=MappingProxyType({}),
            data={CONF_DEVICE_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        with patch.object(self.entries, "async_setup", new=AsyncMock(return_value=True)), patch.object(self.entries, "_async_schedule_save"), patch.object(self.entries, "async_update_issues"):
            await self.entries.async_add(entry)
        return entry

    async def manual_flow(self):
        """Submit the user address form through the real flow manager."""
        return await self.entries.flow.async_init(DOMAIN, context={"source": "user"}, data={
            CONF_NAME: "Front door", CONF_DEVICE_ADDRESS: " aa:bb:cc:dd:ee:ff ",
            CONF_CLIENT_TYPE: "App", CONF_PIN: "0000",
        })

    async def discovery_flow(self):
        """Submit a discovered device through the same identity index."""
        return await self.entries.flow.async_init(
            DOMAIN, context={"source": "bluetooth"},
            data=SimpleNamespace(address="AA:BB:CC:DD:EE:FF", name="Nuki_Test"),
        )

    async def test_manual_and_discovery_share_in_progress_identity(self):
        """Parallel discovery cannot pair the same device a second time."""
        first = await self.manual_flow()
        self.assertEqual(first["type"], FlowResultType.MENU)
        second = await self.discovery_flow()
        self.assertEqual(second["type"], FlowResultType.ABORT)
        self.assertEqual(second["reason"], "already_in_progress")
        progress, = self.entries.flow.async_progress()
        self.assertEqual(progress["flow_id"], first["flow_id"])
        self.assertEqual(progress["context"]["unique_id"], "aabbccddeeff")

    async def test_discovery_and_manual_share_in_progress_identity(self):
        """The reverse order also rejects a duplicate before any BLE access."""
        first = await self.discovery_flow()
        self.assertEqual(first["type"], FlowResultType.FORM)
        second = await self.manual_flow()
        self.assertEqual(second["reason"], "already_in_progress")
        self.assertEqual(len(self.entries.flow.async_progress()), 1)

    async def test_existing_entry_blocks_manual_and_discovery(self):
        """Both paths use HA's real domain/unique-ID index."""
        entry = await self.add_entry()
        self.assertIs(self.entries.async_entry_for_domain_unique_id(DOMAIN, "aabbccddeeff"), entry)
        for start in (self.manual_flow, self.discovery_flow):
            result = await start()
            self.assertEqual(result["reason"], "already_configured")
        self.assertEqual(self.entries.async_entries(DOMAIN), [entry])

    async def test_legacy_manual_entry_without_id_is_detected(self):
        """MAC fallback protects entries awaiting migration."""
        entry = await self.add_entry(unique_id=None)
        result = await self.manual_flow()
        self.assertEqual(result["reason"], "already_configured")
        self.assertIsNone(entry.unique_id)

    async def test_ignored_discovery_can_be_configured_manually(self):
        """Respect HA's user-source filtering of ignored discovery entries."""
        entry = await self.add_entry(source="ignore")
        result = await self.manual_flow()
        self.assertEqual(result["type"], FlowResultType.MENU)
        self.assertEqual(self.entries.async_entries(DOMAIN, include_ignore=False), [])
        self.assertIs(self.entries.async_entry_for_domain_unique_id(DOMAIN, "aabbccddeeff"), entry)


if __name__ == "__main__":
    unittest.main()
