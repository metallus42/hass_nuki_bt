"""NukiEntity class."""
from __future__ import annotations

import logging

from bleak import BleakError
from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo
from pyNukiBT import NukiDevice
from pyNukiBT.const import NukiErrorException

from .const import MANUFACTURER
from .coordinator import NukiDataUpdateCoordinator
from .pairing import async_set_opener_pairing_enabled
from .protocol import async_lock_action as async_execute_lock_action

_LOGGER = logging.getLogger(__name__)


class NukiEntity(PassiveBluetoothCoordinatorEntity[NukiDataUpdateCoordinator]):
    """Generic entity encapsulating common features of Nuki device."""

    device: NukiDevice
    _attr_has_entity_name = True

    def __init__(self, coordinator: NukiDataUpdateCoordinator) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self.device = coordinator.device
        self._address = coordinator.ble_device.address
        self._attr_unique_id = coordinator.base_unique_id
        self._attr_device_info = DeviceInfo(
            connections={(dr.CONNECTION_BLUETOOTH, self._address)},
            manufacturer=MANUFACTURER,
            model=coordinator.device.device_type,
            name=coordinator.device_name,
            hw_version=".".join(
                str(x) for x in coordinator.device.config.get("hardware_revision",[])
            ),
            sw_version=".".join(
                str(x) for x in coordinator.device.config.get("firmware_version",[])
            ),
        )

    @callback
    def _async_update_attrs(self) -> None:
        """Update the entity attributes."""

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle data update."""
        self._async_update_attrs()
        self.async_write_ha_state()

    async def async_lock_action(self, action):
        """Do door action."""
        user = await self.hass.auth.async_get_user(self._context.user_id)
        user_name = user.name if user else None
        try:
            async with self.coordinator.async_action():
                await async_execute_lock_action(self.device, action, name_suffix=user_name)
        except (BleakError, TimeoutError, NukiErrorException, RuntimeError) as err:
            raise HomeAssistantError(
                f"Nuki action failed: {str(err) or type(err).__name__}"
            ) from err
        self.coordinator.async_update_listeners()
        self.coordinator.async_refresh_after_action()

    async def async_handle_update_nuki_time(self, time=None):
        """Update nuki time."""
        if self.coordinator._security_pin is None: #security pin can be 0, so check for None
            raise ServiceValidationError("Security PIN is required to update nuki time.")
        async with self.coordinator.async_action():
            result = await self.device.update_nuki_time(self.coordinator._security_pin, time)
        return result.status

    async def async_handle_set_bluetooth_pairing(self, enabled: bool):
        """Allow or disallow starting Opener pairing with its physical button."""
        try:
            async with self.coordinator.async_action():
                result = await async_set_opener_pairing_enabled(
                    self.device, self.coordinator._security_pin, enabled
                )
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        except RuntimeError as err:
            raise HomeAssistantError(str(err)) from err
        finally:
            self.coordinator.async_update_listeners()
        return {"pairing_enabled": result}
