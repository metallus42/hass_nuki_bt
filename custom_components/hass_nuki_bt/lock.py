"""Library to handle connection with Nuki Lock."""
import logging
import voluptuous as vol
from typing import Any
from homeassistant.components.lock import LockEntity, LockEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform
from pyNukiBT import NukiConst, NukiDevice, NukiLockConst, NukiOpenerConst

from .entity import NukiEntity

from .coordinator import NukiDataUpdateCoordinator
from .const import DOMAIN

logger = logging.getLogger(__name__)

UPDATE_NUKI_TIME_SERVICE_NAME = "update_nuki_time"
# Has to be a simple dictionary to be extended with "target" parameters.
UPDATE_NUKI_TIME_SCHEMA = {
    vol.Optional("time"): cv.datetime,
}

async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: entity_platform.AddEntitiesCallback
) -> None:
    """Set up Nuki lock based on a config entry."""
    coordinator: NukiDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    if coordinator.device.device_type == NukiConst.NukiDeviceType.OPENER:
        async_add_entities([NukiOpener(coordinator)])
    else:
        async_add_entities([NukiLock(coordinator)])
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        UPDATE_NUKI_TIME_SERVICE_NAME,
        schema=UPDATE_NUKI_TIME_SCHEMA,
        func="async_handle_update_nuki_time",
        supports_response=SupportsResponse.OPTIONAL,
    )
    platform.async_register_entity_service(
        "set_bluetooth_pairing",
        schema={vol.Required("enabled"): cv.boolean},
        func="async_handle_set_bluetooth_pairing",
        supports_response=SupportsResponse.OPTIONAL,
    )


class NukiLock(NukiEntity, LockEntity):
    """Representation of a Nuki lock."""

    _device: NukiDevice

    # Mark this as default entity for the device. This will avoid adding any
    # suffixes to device name when creating entity_id:
    # "Front door" -> entity_id: "lock.front_door" (not "lock.front_door_lock").
    _attr_name = None

    def __init__(self, coordinator: NukiDataUpdateCoordinator) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.base_unique_id}-lock"
        self._attr_supported_features = LockEntityFeature.OPEN
        self._async_update_attrs()

    def _async_update_attrs(self) -> None:
        """Update the entity attributes."""
        status = self.device.keyturner_state.get("lock_state")
        states = NukiLockConst.LockState
        self._attr_is_jammed = status == states.MOTOR_BLOCKED
        self._attr_is_open = status == states.UNLATCHED
        self._attr_is_opening = status == states.UNLATCHING
        self._attr_is_locked = (
            True if status == states.LOCKED else
            False if status in (states.UNLOCKED, states.UNLOCKED_LOCK_N_GO, states.UNLATCHED, states.UNLATCHING) else None
        )
        self._attr_is_locking = status == states.LOCKING
        self._attr_is_unlocking = status == states.UNLOCKING

    async def async_lock(self, **kwargs: Any) -> None:
        """Lock the lock."""
        await self.async_lock_action(NukiLockConst.LockAction.LOCK)

    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock the lock."""
        await self.async_lock_action(NukiLockConst.LockAction.UNLOCK)

    async def async_open(self, **kwargs: Any) -> None:
        """Open the door latch."""
        await self.async_lock_action(NukiLockConst.LockAction.UNLATCH)

class NukiOpener(NukiEntity, LockEntity):
    """Representation of a Nuki opener."""

    _device: NukiDevice

    # See the remark in NukiLock above.
    _attr_name = None

    def __init__(self, coordinator: NukiDataUpdateCoordinator) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.base_unique_id}-lock"
        self._attr_supported_features = LockEntityFeature.OPEN
        self._async_update_attrs()

    def _async_update_attrs(self) -> None:
        """Update the entity attributes."""
        status = self.device.keyturner_state.get("lock_state")
        states = NukiOpenerConst.LockState
        self._attr_is_jammed = False
        self._attr_is_open = status == states.OPEN
        self._attr_is_opening = status == states.OPENING
        self._attr_is_locked = (
            True if status == states.LOCKED else
            False if status in (states.RTO_ACTIVE, states.OPEN, states.OPENING) else None
        )

    async def async_lock(self, **kwargs: Any) -> None:
        """Lock the lock."""
        await self.async_lock_action(NukiOpenerConst.LockAction.DEACTIVATE_RTO)

    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock the lock."""
        await self.async_lock_action(NukiOpenerConst.LockAction.ACTIVATE_RTO)

    async def async_open(self, **kwargs: Any) -> None:
        """Open the door latch."""
        await self.async_lock_action(NukiOpenerConst.LockAction.ELECTRIC_STRIKE_ACTUATION)
