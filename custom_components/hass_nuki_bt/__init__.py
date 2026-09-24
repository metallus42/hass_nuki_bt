"""Custom integration to integrate hass_nuki_bt with Home Assistant.

For more details about this integration, please refer to
https://github.com/metallus42/hass_nuki_bt
"""

from __future__ import annotations
import logging
from asyncio import TimeoutError
from bleak import BleakError

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform, CONF_NAME, CONF_PIN, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.components import bluetooth
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady


from pyNukiBT import NukiConst
from pyNukiBT.const import NukiErrorException

from .const import (
    CONF_APP_ID,
    CONF_AUTH_ID,
    CONF_DEVICE_ADDRESS,
    CONF_DEVICE_PUBLIC_KEY,
    CONF_PRIVATE_KEY,
    CONF_PUBLIC_KEY,
    CONF_CLIENT_TYPE,
    DOMAIN,
)
from .coordinator import NukiDataUpdateCoordinator
from .migration import async_migrate_entry  # noqa: F401
from .protocol import SafeNukiDevice
from .validation import parse_security_pin

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.EVENT,
    Platform.LOCK,
    Platform.SENSOR,
    Platform.BUTTON,
    Platform.SWITCH,
]

_LOGGER = logging.getLogger(__name__)


# https://developers.home-assistant.io/docs/config_entries_index/#setting-up-an-entry
async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up this integration using UI."""
    hass.data.setdefault(DOMAIN, {})
    address: str = entry.data[CONF_DEVICE_ADDRESS]

    if not bluetooth.async_address_present(hass, address, connectable=True):
        raise ConfigEntryNotReady(f"Could not find Nuki with address {address}")

    ble_device = bluetooth.async_ble_device_from_address(
        hass, address, connectable=True
    )
    if not ble_device:
        raise ConfigEntryNotReady(f"Could not find Nuki with address {address}")

    if entry.data.get(CONF_CLIENT_TYPE) == "App":
        client_type = NukiConst.NukiClientType.APP
    else:
        client_type = NukiConst.NukiClientType.BRIDGE

    device = SafeNukiDevice(
        address=entry.data[CONF_DEVICE_ADDRESS],
        auth_id=bytes.fromhex(entry.data[CONF_AUTH_ID]),
        nuki_public_key=bytes.fromhex(entry.data[CONF_DEVICE_PUBLIC_KEY]),
        bridge_public_key=bytes.fromhex(entry.data[CONF_PUBLIC_KEY]),
        bridge_private_key=bytes.fromhex(entry.data[CONF_PRIVATE_KEY]),
        app_id=int(entry.data[CONF_APP_ID]),
        client_type=client_type,
        name="HomeAssistant",
        ble_device=ble_device,
        get_ble_device=lambda addr: bluetooth.async_ble_device_from_address(
            hass, addr, connectable=True
        ),
    )
    coordinator = None
    setup_complete = False
    try:
        await device.connect()
        try:
            security_pin = parse_security_pin(entry.data.get(CONF_PIN), device.device_type)
        except ValueError as err:
            raise ConfigEntryError(
                "Invalid security PIN for this Nuki device. Reconfigure the integration to correct the PIN."
            ) from err
        coordinator = NukiDataUpdateCoordinator(
            hass=hass,
            logger=_LOGGER,
            ble_device=ble_device,
            device=device,
            base_unique_id=entry.unique_id,
            device_name=entry.data.get(CONF_NAME),
            connectable=True,
            security_pin=security_pin,
        )
        if not await coordinator.async_wait_ready():
            raise ConfigEntryNotReady(f"{address} is not advertising state")
        hass.data[DOMAIN][entry.entry_id] = coordinator
        coordinator.async_start()
        entry.async_on_unload(coordinator.async_shutdown)
        entry.async_on_unload(
            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, coordinator.async_shutdown)
        )
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        setup_complete = True
    except (BleakError, TimeoutError, NukiErrorException) as ex:
        raise ConfigEntryNotReady(f"Could not initialize Nuki at {address}: {ex}") from ex
    finally:
        if not setup_complete:
            if coordinator is not None:
                await coordinator.async_shutdown()
            else:
                await device.disconnect()
            hass.data[DOMAIN].pop(entry.entry_id, None)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await hass.config_entries.async_reload(entry.entry_id)
