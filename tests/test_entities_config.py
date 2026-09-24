"""Regression tests for setup identity, entity states and PIN maintenance."""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from bleak import BleakError
from construct import Container
from nacl.public import PrivateKey
from homeassistant.const import CONF_NAME, CONF_PIN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import AbortFlow, FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import device_registry as dr
from pyNukiBT import NukiConst, NukiLockConst, NukiOpenerConst
from pyNukiBT.const import NukiErrorException

from custom_components.hass_nuki_bt.binary_sensor import NukiBinarySensor, SENSOR_TYPES_LOCK
from custom_components.hass_nuki_bt.config_flow import NukiFlowHandler, parse_security_pin, validate_pairing_data
from custom_components.hass_nuki_bt.const import (
    CONF_APP_ID, CONF_AUTH_ID, CONF_CLIENT_TYPE, CONF_DEVICE_ADDRESS,
    CONF_DEVICE_PUBLIC_KEY, CONF_PRIVATE_KEY, CONF_PUBLIC_KEY, DOMAIN,
)
from custom_components.hass_nuki_bt.lock import NukiLock, NukiOpener
from custom_components.hass_nuki_bt.migration import async_migrate_entry
from custom_components.hass_nuki_bt.sensor import NukiSensor
from custom_components.hass_nuki_bt.switch import NukiContinuousModeSwitch


def coordinator(device_type, state):
    """Provide protocol boundaries while using real HA entity classes."""
    return SimpleNamespace(
        base_unique_id="aabbccddeeff", device_name="Front door", available=True,
        ble_device=SimpleNamespace(address="AA:BB:CC:DD:EE:FF"),
        device=SimpleNamespace(
            device_type=device_type, config={"name": "Front door"},
            keyturner_state=Container(state), last_action_status=None,
            rssi=-60, battery_percentage=80, is_battery_critical=False,
            is_battery_charging=False,
        ),
        last_nuki_log_entry={},
    )


def make_flow(entries=()):
    """Use the real flow methods with in-memory config-entry boundaries."""
    flow = NukiFlowHandler()
    flow.hass = SimpleNamespace(data={}, config_entries=SimpleNamespace(
        async_entries=Mock(return_value=list(entries)),
        async_entry_for_domain_unique_id=Mock(return_value=None),
        flow=SimpleNamespace(async_progress_by_handler=Mock(return_value=[])),
        async_update_entry=Mock(return_value=True), async_schedule_reload=Mock(),
    ))
    flow.handler = DOMAIN
    flow.context = {"source": "user"}
    flow.flow_id = "test-flow"
    return flow


def imported_keys():
    """Generate consistent deterministic credentials for import validation."""
    private = PrivateKey(b"\x01" * 32)
    return {
        CONF_AUTH_ID: "01020304", CONF_PRIVATE_KEY: bytes(private).hex(),
        CONF_PUBLIC_KEY: bytes(private.public_key).hex(),
        CONF_DEVICE_PUBLIC_KEY: bytes(PrivateKey(b"\x02" * 32).public_key).hex(),
        CONF_APP_ID: "17",
    }


class EntityStatesTests(unittest.IsolatedAsyncioTestCase):
    """Validate HA's resulting state, including optional protocol fields."""

    async def test_lock_states_use_lock_constants_and_unknown(self):
        """Unlatching is opening, while undefined/calibrating is unknown."""
        states = NukiLockConst.LockState
        for status, expected in (
            (states.LOCKED, "locked"), (states.UNLOCKED, "unlocked"),
            (states.UNLATCHED, "open"), (states.UNLATCHING, "opening"),
            (states.UNDEFINED, None), (states.CALIBRATION, None),
            (states.BOOT_RUN, None), (states.UNCALIBRATED, None),
            (states.MOTOR_BLOCKED, "jammed"),
        ):
            with self.subTest(status=status):
                entity = NukiLock(coordinator(NukiConst.NukiDeviceType.SMARTLOCK_1_2, {"lock_state": status}))
                self.assertEqual(entity.state, expected)

    async def test_opener_unknown_is_never_unlocked(self):
        """Undefined and uncalibrated Opener states remain unknown."""
        states = NukiOpenerConst.LockState
        for status, expected in (
            (states.UNDEFINED, None), (states.UNCALIBRATED, None),
            (states.LOCKED, "locked"), (states.RTO_ACTIVE, "unlocked"),
            (states.OPEN, "open"), (states.OPENING, "opening"),
        ):
            entity = NukiOpener(coordinator(NukiConst.NukiDeviceType.OPENER, {"lock_state": status}))
            self.assertEqual(entity.state, expected)

    async def test_missing_optional_fields_do_not_break_binary_sensors(self):
        """Older lock packets still allow battery sensors to be constructed."""
        coord = coordinator(NukiConst.NukiDeviceType.SMARTLOCK_1_2, {"accessory_battery_state": None, "nightmode_active": None})
        entities = {desc.key: NukiBinarySensor(coord, desc) for desc in SENSOR_TYPES_LOCK}
        self.assertFalse(entities["battery_critical"].is_on)
        self.assertIsNone(entities["accessory_battery_state"].state)
        self.assertIsNone(entities["nightmode_active"].state)
        coord.device.keyturner_state.update({"accessory_battery_state": 2, "nightmode_active": 0})
        entities["accessory_battery_state"]._async_update_attrs()
        entities["nightmode_active"]._async_update_attrs()
        self.assertTrue(entities["accessory_battery_state"].is_on)
        self.assertFalse(entities["nightmode_active"].is_on)

    async def test_continuous_mode_uses_device_state_without_optimism(self):
        """A command alone cannot change the switch's confirmed state."""
        coord = coordinator(NukiConst.NukiDeviceType.OPENER, {"nuki_state": NukiConst.State.DOOR_MODE})
        entity = NukiContinuousModeSwitch(coord)
        entity.async_lock_action = AsyncMock()
        self.assertEqual(entity.state, "off")
        await entity.async_turn_on()
        entity.async_lock_action.assert_awaited_once_with(NukiOpenerConst.LockAction.ACTIVATE_CM)
        self.assertEqual(entity.state, "off")
        coord.device.keyturner_state.nuki_state = NukiConst.State.CONTINUOUS_MODE
        entity._async_update_attrs()
        self.assertEqual(entity.state, "on")
        await entity.async_turn_off()
        self.assertEqual(entity.state, "on")
        coord.device.keyturner_state.nuki_state = NukiConst.State.MAINTENANCE_MODE
        entity._async_update_attrs()
        self.assertIsNone(entity.state)

    async def test_enum_options_preserve_existing_raw_mode_strings(self):
        """Existing automation comparisons keep their uppercase state values."""
        coord = coordinator(NukiConst.NukiDeviceType.OPENER, {"nuki_state": NukiConst.State.CONTINUOUS_MODE})
        entity = NukiSensor(coord, "nuki_state")
        entity.platform_data = SimpleNamespace(domain="sensor", platform_name=DOMAIN, default_language_platform_translations={})
        self.assertEqual(entity.state, "CONTINUOUS_MODE")
        self.assertIn("DOOR_MODE", entity.options)
        self.assertIn("CONTINUOUS_MODE", entity.options)
        self.assertEqual(entity.unique_id, "aabbccddeeff-nuki_state")
        self.assertIsNone(NukiSensor(coord, "name").device_class)
        self.assertIsNone(NukiSensor(coord, "last_action_user").device_class)
        self.assertIsNone(NukiSensor(coord, "last_nuki_command_status").device_class)

    async def test_missing_optional_timestamp_is_unknown(self):
        """An absent or malformed device clock cannot abort sensor setup."""
        coord = coordinator(NukiConst.NukiDeviceType.SMARTLOCK_1_2, {"current_time": None, "timezone_offset": 60})
        self.assertIsNone(NukiSensor(coord, "last_state_timestamp").native_value)


class ConfigFlowTests(unittest.IsolatedAsyncioTestCase):
    """Validate configuration without real Bluetooth or Home Assistant setup."""

    async def configure_address(self, flow):
        """Complete the shared manual-address step."""
        return await flow.async_step_user({
            CONF_NAME: "Front door", CONF_DEVICE_ADDRESS: " aa:bb:cc:dd:ee:ff ",
            CONF_CLIENT_TYPE: "App", CONF_PIN: "0000",
        })

    async def test_manual_setup_gets_normalized_unique_id(self):
        """Importing keys keeps a stable identity shared with discovery."""
        flow = make_flow()
        result = await self.configure_address(flow)
        self.assertEqual(result["type"], FlowResultType.MENU)
        self.assertEqual(flow.unique_id, "aabbccddeeff")
        result = await flow.async_step_manual(imported_keys())
        self.assertEqual(result["type"], FlowResultType.CREATE_ENTRY)
        self.assertEqual(result["data"][CONF_DEVICE_ADDRESS], "AA:BB:CC:DD:EE:FF")
        self.assertEqual(result["data"][CONF_PIN], "0")

    async def test_legacy_duplicate_aborts_before_pairing(self):
        """Entries without unique IDs are still detected by their MAC."""
        existing = SimpleNamespace(data={CONF_DEVICE_ADDRESS: "aa:bb:cc:dd:ee:ff"}, unique_id=None)
        flow = make_flow([existing])
        with patch("custom_components.hass_nuki_bt.config_flow.SafeNukiDevice") as device:
            with self.assertRaises(AbortFlow) as error:
                await self.configure_address(flow)
            self.assertEqual(error.exception.reason, "already_configured")
            device.assert_not_called()

    async def test_invalid_fields_do_not_open_bluetooth(self):
        """An invalid PIN or address returns field errors before pairing."""
        flow = make_flow()
        with patch("custom_components.hass_nuki_bt.config_flow.SafeNukiDevice") as device:
            result = await flow.async_step_user({CONF_NAME: "Front door", CONF_DEVICE_ADDRESS: "invalid", CONF_CLIENT_TYPE: "App", CONF_PIN: "abc"})
            self.assertEqual(result["errors"], {CONF_DEVICE_ADDRESS: "invalid_address", CONF_PIN: "invalid_pin"})
            device.assert_not_called()

    async def test_import_rejects_bad_key_and_mismatched_keypair(self):
        """Reject unusable credentials before creating the entry."""
        data = imported_keys()
        self.assertEqual(validate_pairing_data(data), {})
        self.assertIn(CONF_AUTH_ID, validate_pairing_data({**data, CONF_AUTH_ID: "bad"}))
        self.assertIn(CONF_PUBLIC_KEY, validate_pairing_data({**data, CONF_PUBLIC_KEY: "00" * 32}))
        self.assertIn(CONF_APP_ID, validate_pairing_data({**data, CONF_APP_ID: "4294967296"}))

    async def test_pairing_errors_disconnect_and_return_form(self):
        """Both transport and protocol rejection paths clean up the device."""
        errors = (
            (NukiErrorException(NukiConst.ErrorCode.P_ERROR_NOT_PAIRING, NukiConst.NukiCommand.PUBLIC_KEY), "pairing"),
            (NukiErrorException(NukiConst.ErrorCode.P_ERROR_MAX_USER, NukiConst.NukiCommand.PUBLIC_KEY), "pairing_failed"),
            (BleakError("offline"), "connection"),
        )
        for error, expected in errors:
            flow = make_flow()
            await self.configure_address(flow)
            device = SimpleNamespace(device_type=NukiConst.NukiDeviceType.OPENER, connect=AsyncMock(), pair=AsyncMock(side_effect=error), disconnect=AsyncMock())
            with patch("custom_components.hass_nuki_bt.config_flow.SafeNukiDevice", return_value=device), patch("custom_components.hass_nuki_bt.config_flow.bluetooth.async_ble_device_from_address"):
                result = await flow.async_step_pair()
            self.assertEqual(result["errors"]["base"], expected)
            device.disconnect.assert_awaited_once()

    async def test_pairing_cancellation_disconnects_and_propagates(self):
        """Cancelling setup cannot continue into saving credentials."""
        flow = make_flow()
        await self.configure_address(flow)
        device = SimpleNamespace(device_type=NukiConst.NukiDeviceType.OPENER, connect=AsyncMock(), pair=AsyncMock(side_effect=asyncio.CancelledError()), disconnect=AsyncMock())
        with patch("custom_components.hass_nuki_bt.config_flow.SafeNukiDevice", return_value=device), patch("custom_components.hass_nuki_bt.config_flow.bluetooth.async_ble_device_from_address"), self.assertRaises(asyncio.CancelledError):
            await flow.async_step_pair()
        device.disconnect.assert_awaited_once()

    async def test_ultra_requires_pin_and_accepts_wider_pin(self):
        """Validate the detected model without excluding valid Ultra PINs."""
        self.assertEqual(parse_security_pin("123456", NukiConst.NukiDeviceType.SMARTLOCK_ULTRA), 123456)
        with self.assertRaises(ValueError):
            parse_security_pin("123456", NukiConst.NukiDeviceType.OPENER)
        flow = make_flow()
        await self.configure_address(flow)
        flow._data[CONF_PIN] = None
        device = SimpleNamespace(device_type=NukiConst.NukiDeviceType.SMARTLOCK_ULTRA, connect=AsyncMock(), pair=AsyncMock(), disconnect=AsyncMock())
        with patch("custom_components.hass_nuki_bt.config_flow.SafeNukiDevice", return_value=device), patch("custom_components.hass_nuki_bt.config_flow.bluetooth.async_ble_device_from_address"):
            result = await flow.async_step_pair()
        self.assertEqual(result["errors"]["base"], "pin_required")
        device.pair.assert_not_awaited()
        device.disconnect.assert_awaited_once()

    async def test_reconfigure_only_changes_pin_and_schedules_one_reload(self):
        """PIN maintenance retains credentials and does not pair again."""
        flow = make_flow()
        data = {**imported_keys(), CONF_DEVICE_ADDRESS: "AA:BB:CC:DD:EE:FF", CONF_PIN: "1111"}
        entry = SimpleNamespace(entry_id="entry", data=data, update_listeners=[])
        flow.context = {"source": "reconfigure", "entry_id": "entry"}
        flow.hass.config_entries.async_get_known_entry = Mock(return_value=entry)
        with patch("custom_components.hass_nuki_bt.config_flow.SafeNukiDevice") as device:
            result = await flow.async_step_reconfigure({CONF_PIN: "0022"})
        self.assertEqual(result["type"], FlowResultType.ABORT)
        updates = flow.hass.config_entries.async_update_entry.call_args.kwargs
        self.assertEqual(updates["data"], {**data, CONF_PIN: "22"})
        flow.hass.config_entries.async_schedule_reload.assert_called_once_with("entry")
        device.assert_not_called()


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    """Exercise real HA registry updates with no running integration."""

    async def asyncSetUp(self):
        """Build an isolated HA registry in a temporary directory."""
        self.directory = TemporaryDirectory()
        self.hass = HomeAssistant(str(Path(self.directory.name)))
        self.entry = SimpleNamespace(entry_id="entry", version=1, unique_id=None, title="Front door", pref_disable_new_entities=False, data={CONF_DEVICE_ADDRESS: "aa:bb:cc:dd:ee:ff", CONF_PRIVATE_KEY: "retained"})
        self.hass.config_entries = SimpleNamespace(
            async_entries=Mock(return_value=[self.entry]),
            async_get_entry=Mock(return_value=self.entry),
            async_update_entry=Mock(),
        )
        self.registry = er.EntityRegistry(self.hass)
        dr.async_setup(self.hass)
        await dr.async_load(self.hass, load_empty=True)
        await self.registry.async_load()
        self.registry.async_schedule_save = Mock()
        self.entity = self.registry.async_get_or_create(
            "lock", DOMAIN, "None-lock", config_entry=self.entry,
            suggested_object_id="my_existing_name",
        )

    async def asyncTearDown(self):
        """Release only the isolated registry's temporary storage."""
        self.directory.cleanup()

    async def test_migration_preserves_entity_id_and_credentials(self):
        """Registry identity changes without renaming automation targets."""
        with patch("custom_components.hass_nuki_bt.migration.er.async_get", return_value=self.registry):
            self.assertTrue(await async_migrate_entry(self.hass, self.entry))
        migrated = self.registry.async_get("lock.my_existing_name")
        self.assertEqual(migrated.entity_id, self.entity.entity_id)
        self.assertEqual(migrated.unique_id, "aabbccddeeff-lock")
        updates = self.hass.config_entries.async_update_entry.call_args.kwargs
        self.assertEqual(updates["data"][CONF_PRIVATE_KEY], "retained")
        self.assertEqual(updates["version"], 2)
        self.assertEqual(updates["unique_id"], "aabbccddeeff")

    async def test_migration_conflict_changes_nothing(self):
        """A target-ID collision is rejected before any entry is updated."""
        self.registry.async_get_or_create(
            "lock", DOMAIN, "aabbccddeeff-lock", suggested_object_id="other",
        )
        with patch("custom_components.hass_nuki_bt.migration.er.async_get", return_value=self.registry), self.assertLogs("custom_components.hass_nuki_bt", level="ERROR"):
            self.assertFalse(await async_migrate_entry(self.hass, self.entry))
        self.assertEqual(self.registry.async_get(self.entity.entity_id).unique_id, "None-lock")
        self.hass.config_entries.async_update_entry.assert_not_called()


if __name__ == "__main__":
    unittest.main()
