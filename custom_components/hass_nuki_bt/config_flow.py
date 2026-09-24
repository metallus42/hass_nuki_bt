"""Configure a Nuki device without replacing an existing pairing."""

import random
import re
from typing import Any

from bleak import BleakError
from nacl.public import PrivateKey
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.const import CONF_NAME, CONF_PIN
from homeassistant.data_entry_flow import AbortFlow, FlowResult
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from pyNukiBT import NukiConst, NukiErrorException

from .const import (
    CONF_APP_ID,
    CONF_AUTH_ID,
    CONF_CLIENT_TYPE,
    CONF_DEVICE_ADDRESS,
    CONF_DEVICE_PUBLIC_KEY,
    CONF_PRIVATE_KEY,
    CONF_PUBLIC_KEY,
    DOMAIN,
    LOGGER,
)
from .protocol import SafeNukiDevice
from .validation import parse_security_pin

_SECRET = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
_KEY_LENGTHS = {
    CONF_AUTH_ID: 4,
    CONF_PRIVATE_KEY: 32,
    CONF_PUBLIC_KEY: 32,
    CONF_DEVICE_PUBLIC_KEY: 32,
}


def format_unique_id(address: str) -> str:
    """Use the same stable identifier for discovery and manual setup."""
    return address.strip().replace(":", "").lower()


def validate_address(address: str) -> bool:
    """Validate a Bluetooth MAC address."""
    return re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", address) is not None


def validate_pairing_data(data: dict[str, Any]) -> dict[str, str]:
    """Check manually imported credentials before persisting a config entry."""
    errors = {}
    for key, length in _KEY_LENGTHS.items():
        value = data.get(key, "")
        if not isinstance(value, str) or not re.fullmatch(rf"[0-9a-fA-F]{{{length * 2}}}", value):
            errors[key] = "invalid_key"
    value = data.get(CONF_APP_ID, "")
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)) or not 0 <= int(value) <= 0xFFFFFFFF:
        errors[CONF_APP_ID] = "invalid_app_id"
    if not errors:
        private_key = PrivateKey(bytes.fromhex(data[CONF_PRIVATE_KEY]))
        if bytes(private_key.public_key) != bytes.fromhex(data[CONF_PUBLIC_KEY]):
            errors[CONF_PUBLIC_KEY] = "key_mismatch"
    return errors


class NukiFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Nuki."""

    VERSION = 2

    def __init__(self) -> None:
        """Keep values within this flow only."""
        self._data: dict[str, Any] = {}

    async def _async_set_device_identity(self) -> None:
        """Prevent duplicate entries before any pairing command is sent."""
        address = self._data[CONF_DEVICE_ADDRESS].strip().upper()
        self._data[CONF_DEVICE_ADDRESS] = address
        await self.async_set_unique_id(format_unique_id(address))
        self._abort_if_unique_id_configured()
        # Older user-initiated entries had no unique ID. Also check their MAC.
        for entry in self._async_current_entries():
            if format_unique_id(entry.data.get(CONF_DEVICE_ADDRESS, "")) == self.unique_id:
                raise AbortFlow("already_configured")

    async def async_step_bluetooth(self, discovery_info: bluetooth.BluetoothServiceInfoBleak) -> FlowResult:
        """Handle Bluetooth discovery."""
        self._data[CONF_DEVICE_ADDRESS] = discovery_info.address.upper()
        self._data[CONF_NAME] = discovery_info.name
        await self._async_set_device_identity()
        self.context["title_placeholders"] = {
            "name": self._data[CONF_NAME],
            "address": self._data[CONF_DEVICE_ADDRESS],
        }
        return await self.async_step_step1()

    async def async_step_choose_method(self, user_input=None) -> FlowResult:
        """Let the user choose pairing or importing existing keys."""
        return self.async_show_menu(
            step_id="choose_method",
            menu_options=["pair", "manual"],
            description_placeholders={"name": self._data[CONF_NAME], "address": self._data[CONF_DEVICE_ADDRESS]},
        )

    async def async_step_step1(self, user_input=None) -> FlowResult:
        """Validate the address and PIN before connecting."""
        errors = {}
        if user_input is not None:
            self._data.update(user_input)
            address = self._data.get(CONF_DEVICE_ADDRESS, "").strip().upper()
            if not validate_address(address):
                errors[CONF_DEVICE_ADDRESS] = "invalid_address"
            if not self._data.get(CONF_NAME, "").strip():
                errors[CONF_NAME] = "invalid_name"
            try:
                pin = parse_security_pin(self._data.get(CONF_PIN))
            except ValueError:
                errors[CONF_PIN] = "invalid_pin"
            if not errors:
                self._data[CONF_PIN] = None if pin is None else str(pin)
                self._data[CONF_NAME] = self._data[CONF_NAME].strip()
                await self._async_set_device_identity()
                return await self.async_step_choose_method()
        return self.async_show_form(
            step_id="step1",
            data_schema=vol.Schema({
                vol.Required(CONF_NAME, default=self._data.get(CONF_NAME, "")): str,
                vol.Required(CONF_DEVICE_ADDRESS, default=self._data.get(CONF_DEVICE_ADDRESS, "")): str,
                vol.Optional(CONF_PIN): _SECRET,
                vol.Required(CONF_CLIENT_TYPE, default=self._data.get(CONF_CLIENT_TYPE, "Bridge")): SelectSelector(
                    SelectSelectorConfig(options=["Bridge", "App"], mode=SelectSelectorMode.DROPDOWN)
                ),
            }),
            errors=errors,
        )

    async def async_step_user(self, user_input=None) -> FlowResult:
        """Start user-initiated configuration."""
        return await self.async_step_step1(user_input)

    async def async_step_pair(self, user_input=None) -> FlowResult:
        """Pair once and always release the connection on error or cancellation."""
        if CONF_DEVICE_ADDRESS not in self._data or CONF_CLIENT_TYPE not in self._data:
            return await self.async_step_step1()
        await self._async_set_device_identity()
        if user_input is not None and CONF_PIN in user_input:
            self._data[CONF_PIN] = user_input[CONF_PIN]
        try:
            pin = parse_security_pin(self._data.get(CONF_PIN))
        except ValueError:
            return self._pairing_form("invalid_pin")

        keypair = PrivateKey.generate()
        app_id = random.getrandbits(32)
        address = self._data[CONF_DEVICE_ADDRESS]
        device = SafeNukiDevice(
            address=address,
            auth_id=None,
            nuki_public_key=None,
            bridge_public_key=bytes(keypair.public_key),
            bridge_private_key=bytes(keypair),
            app_id=app_id,
            name="HomeAssistant",
            client_type=NukiConst.NukiClientType.APP if self._data[CONF_CLIENT_TYPE] == "App" else NukiConst.NukiClientType.BRIDGE,
            ble_device=bluetooth.async_ble_device_from_address(self.hass, address, connectable=True),
            get_ble_device=lambda addr: bluetooth.async_ble_device_from_address(self.hass, addr, connectable=True),
        )
        try:
            await device.connect()
            pin = parse_security_pin(self._data.get(CONF_PIN), device.device_type)
            if device.device_type == NukiConst.NukiDeviceType.SMARTLOCK_ULTRA and pin is None:
                return self._pairing_form("pin_required")
            result = await device.pair(pin)
        except NukiErrorException as err:
            error = "pairing" if err.error_code == NukiConst.ErrorCode.P_ERROR_NOT_PAIRING else "pairing_failed"
            LOGGER.debug("Nuki pairing rejected: %s", err.error_code)
            return self._pairing_form(error)
        except (BleakError, TimeoutError, OSError):
            return self._pairing_form("connection")
        except ValueError:
            return self._pairing_form("invalid_pin")
        finally:
            try:
                await device.disconnect()
            except (BleakError, TimeoutError, OSError):
                LOGGER.debug("Could not disconnect the pairing connection", exc_info=True)

        self._data.update({
            CONF_AUTH_ID: result["auth_id"].hex(),
            CONF_DEVICE_PUBLIC_KEY: result["nuki_public_key"].hex(),
            CONF_PUBLIC_KEY: bytes(keypair.public_key).hex(),
            CONF_PRIVATE_KEY: bytes(keypair).hex(),
            CONF_APP_ID: str(app_id),
            CONF_PIN: None if pin is None else str(pin),
        })
        return self.async_create_entry(title=self._data[CONF_NAME], data=self._data)

    def _pairing_form(self, error: str) -> FlowResult:
        return self.async_show_form(
            step_id="pair",
            data_schema=vol.Schema({vol.Optional(CONF_PIN): _SECRET}),
            errors={"base": error},
        )

    async def async_step_manual(self, user_input=None) -> FlowResult:
        """Import an existing pairing without transmitting pairing commands."""
        errors = {}
        if user_input is not None:
            errors = validate_pairing_data(user_input)
            if not errors:
                await self._async_set_device_identity()
                self._data.update(user_input)
                return self.async_create_entry(title=self._data[CONF_NAME], data=self._data)
        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema({
                vol.Required(CONF_AUTH_ID): str,
                vol.Required(CONF_PRIVATE_KEY): _SECRET,
                vol.Required(CONF_PUBLIC_KEY): str,
                vol.Required(CONF_DEVICE_PUBLIC_KEY): str,
                vol.Required(CONF_APP_ID): str,
            }),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input=None) -> FlowResult:
        """Change only the security PIN; retain the existing pairing and IDs."""
        entry = self._get_reconfigure_entry()
        errors = {}
        if user_input is not None:
            coordinator = self.hass.data.get(DOMAIN, {}).get(entry.entry_id)
            device_type = coordinator.device.device_type if coordinator else None
            try:
                pin = parse_security_pin(user_input.get(CONF_PIN), device_type)
            except ValueError:
                errors[CONF_PIN] = "invalid_pin"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_PIN: None if pin is None else str(pin)},
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema({vol.Optional(CONF_PIN): _SECRET}),
            errors=errors,
        )
