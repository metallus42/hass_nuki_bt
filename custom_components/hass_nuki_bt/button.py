"""Button platform for hass_nuki_bt."""
from dataclasses import dataclass
from collections.abc import Callable
import logging
from bleak import BleakError
from homeassistant.components.button import ButtonEntity, ButtonEntityDescription, ButtonDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.exceptions import HomeAssistantError
from pyNukiBT import NukiConst, NukiDevice, NukiLockConst, NukiOpenerConst
from pyNukiBT.const import NukiErrorException

from .entity import NukiEntity

from .coordinator import NukiDataUpdateCoordinator
from .const import DOMAIN


logger = logging.getLogger(__name__)


@dataclass
class NukiButtonEntityDescription(ButtonEntityDescription):
    """A class that describes nuki button entities."""

    action: NukiLockConst.LockAction = None
    action_function: Callable = lambda slf: slf.async_lock_action(slf._action)


BUTTON_TYPES_COMMON: list[NukiButtonEntityDescription] = [
    NukiButtonEntityDescription(
        key="query_state",
        name="Query lock state",
        icon="mdi:lock-question",
        device_class=ButtonDeviceClass.UPDATE,
        action_function=lambda slf: slf.coordinator._async_update(),
    ),
]
BUTTON_TYPES_OPENER: list[NukiButtonEntityDescription] = BUTTON_TYPES_COMMON + [
    NukiButtonEntityDescription(
        key="enable_bluetooth_pairing",
        name="Enable Bluetooth pairing",
        icon="mdi:bluetooth-connect",
        entity_category=EntityCategory.CONFIG,
        action_function=lambda slf: slf.async_handle_set_bluetooth_pairing(True),
    ),
    NukiButtonEntityDescription(
        name="Activate Continuous Mode",
        key="activate_cm",
        icon="mdi:home-lock-open",
        action=NukiOpenerConst.LockAction.ACTIVATE_CM,
    ),
    NukiButtonEntityDescription(
        name="Deactivate Continuous Mode",
        key="deactivate_cm",
        icon="mdi:home-lock",
        action=NukiOpenerConst.LockAction.DEACTIVATE_CM,
    ),
]
BUTTON_TYPES_LOCK: list[NukiButtonEntityDescription] = BUTTON_TYPES_COMMON + [
    NukiButtonEntityDescription(
        name="Unlatch",
        key="unlatch",
        icon="mdi:door-open",
        action=NukiLockConst.LockAction.UNLATCH,
    ),
    NukiButtonEntityDescription(
        name="Lock 'n' Go",
        key="lockngo",
        icon="mdi:door-closed-lock",
        action=NukiLockConst.LockAction.LOCK_N_GO,
    ),
    NukiButtonEntityDescription(
        name="Lock 'n' Go with unlatch",
        key="lockngounlatch",
        icon="mdi:door-closed-lock",
        action=NukiLockConst.LockAction.LOCK_N_GO_UNLATCH,
    ),
]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Nuki lock based on a config entry."""
    coordinator: NukiDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    if coordinator.device.device_type == NukiConst.NukiDeviceType.OPENER:
        async_add_entities([NukiButton(coordinator, btn) for btn in BUTTON_TYPES_OPENER])
    else:
        async_add_entities([NukiButton(coordinator, btn) for btn in BUTTON_TYPES_LOCK])


class NukiButton(ButtonEntity, NukiEntity):
    """Buttons for the Nuki lock."""

    _device: NukiDevice

    def __init__(
        self, coordinator: NukiDataUpdateCoordinator, btn: NukiButtonEntityDescription
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._attr_translation_key = btn.key
        self._attr_unique_id = f"{coordinator.base_unique_id}-{btn.key}"
        self._action = btn.action
        self._attr_icon = btn.icon
        self.entity_description = btn

    async def async_press(self) -> None:
        """Handle the button press."""
        try:
            await self.entity_description.action_function(self)
        except (BleakError, TimeoutError, NukiErrorException) as err:
            raise HomeAssistantError(
                f"Nuki button failed: {str(err) or type(err).__name__}"
            ) from err
