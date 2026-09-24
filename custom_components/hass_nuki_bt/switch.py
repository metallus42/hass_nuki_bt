"""Confirmed Continuous Mode state for Nuki Openers."""

from homeassistant.components.switch import SwitchEntity
from pyNukiBT import NukiConst, NukiOpenerConst

from .const import DOMAIN
from .entity import NukiEntity


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Expose mode control only on an Opener."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    if coordinator.device.device_type == NukiConst.NukiDeviceType.OPENER:
        async_add_entities([NukiContinuousModeSwitch(coordinator)])


class NukiContinuousModeSwitch(NukiEntity, SwitchEntity):
    """Control Continuous Mode and report only the device's confirmed state."""

    _attr_translation_key = "continuous_mode"
    _attr_icon = "mdi:door-open"

    def __init__(self, coordinator) -> None:
        """Initialize from the latest confirmed device state."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.base_unique_id}-continuous_mode"
        self._async_update_attrs()

    def _async_update_attrs(self) -> None:
        """Map the device mode without changing any protocol state."""
        state = self.device.keyturner_state.get("nuki_state")
        self._attr_is_on = (
            True if state == NukiConst.State.CONTINUOUS_MODE else
            False if state == NukiConst.State.DOOR_MODE else None
        )

    async def async_turn_on(self, **kwargs) -> None:
        """Enable mode through the same confirmed action path as the buttons."""
        await self.async_lock_action(NukiOpenerConst.LockAction.ACTIVATE_CM)

    async def async_turn_off(self, **kwargs) -> None:
        """Disable mode without predicting the resulting state."""
        await self.async_lock_action(NukiOpenerConst.LockAction.DEACTIVATE_CM)
