"""Sensor platform for hass_nuki_bt."""
from __future__ import annotations
from dataclasses import dataclass
from collections.abc import Callable
import datetime


from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from pyNukiBT import NukiConst, NukiLockConst, NukiOpenerConst

from .const import DOMAIN
from .coordinator import NukiDataUpdateCoordinator
from .entity import NukiEntity

PARALLEL_UPDATES = 0


@dataclass
class NukiSensorEntityDescription(SensorEntityDescription):
    """A class that describes nuki sensor entities."""

    info_function: Callable | None = lambda slf: slf.device.keyturner_state.get(slf.sensor)
    icon_function: Callable | None = None

SENSOR_TYPES: dict[str, NukiSensorEntityDescription] = {
    "name": NukiSensorEntityDescription(
        key="name",
        name="Nuki Device Name",
        icon="mdi:lock",
        entity_category=EntityCategory.DIAGNOSTIC,
        info_function=lambda slf: slf.device.config.get(slf.sensor),
    ),
    "rssi": NukiSensorEntityDescription(
        key="rssi",
        name="Bluetooth signal strength",
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        info_function=lambda slf: slf.device.rssi,
    ),
    "battery": NukiSensorEntityDescription(
        key="battery",
        name="Battery",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        info_function=lambda slf: slf.device.battery_percentage,
    ),
    "lock_state": NukiSensorEntityDescription(
        key="lock_state",
        name="Lock state",
        icon_function=lambda slf: "mdi:lock" if str(slf.device.keyturner_state.get("lock_state")) == "LOCKED" else "mdi:lock-open",
        device_class=SensorDeviceClass.ENUM,
    ),
    "door_sensor_state": NukiSensorEntityDescription(
        key="door_state",
        name="Door state",
        icon="mdi:door",
        device_class=SensorDeviceClass.ENUM,
    ),
    "last_lock_action": NukiSensorEntityDescription(
        key="last_lock_action",
        name="Last lock action",
        icon="mdi:door",
        device_class=SensorDeviceClass.ENUM,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    "last_lock_action_trigger": NukiSensorEntityDescription(
        key="last_lock_action_trigger",
        name="Last Action Trigger",
        icon="mdi:door",
        device_class=SensorDeviceClass.ENUM,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    "last_lock_action_completion_status": NukiSensorEntityDescription(
        key="last_lock_action_completion_status",
        name="Last action completion status",
        icon_function=lambda slf: "mdi:lock-check" if slf.device.keyturner_state.get('last_lock_action_completion_status') == NukiConst.LockActionCompletionStatus.SUCCESS \
            else "mdi:lock-alert",
        device_class=SensorDeviceClass.ENUM,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    "last_nuki_command_status": NukiSensorEntityDescription(
        key="last_nuki_command_status",
        name="Last Nuki command status",
        info_function=lambda slf: slf.device.last_action_status.__name__ if isinstance(slf.device.last_action_status, type) else slf.device.last_action_status,
        icon_function=lambda slf: "mdi:lock-check" if slf.device.last_action_status == NukiConst.StatusCode.COMPLETED \
            or slf.device.last_action_status == NukiConst.StatusCode.ACCEPTED \
                else "mdi:lock-alert",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    "nuki_state": NukiSensorEntityDescription(
        key="nuki_state",
        name="Nuki state",
        icon="mdi:lock",
        device_class=SensorDeviceClass.ENUM,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    "last_action_user": NukiSensorEntityDescription(
        key="last_action_user",
        name="Last action user name",
        icon="mdi:account-lock",
        entity_category=EntityCategory.DIAGNOSTIC,
        # There is no user name if last action was triggered by button/manual etc.
        info_function=lambda slf: name if (name := slf.coordinator.last_nuki_log_entry.get("name")) else \
            trigger if ((data:=slf.coordinator.last_nuki_log_entry.get("data")) and (trigger := data.get("trigger"))) \
            else "Unknown",
    ),
    "last_log_timestamp": NukiSensorEntityDescription(
        key="last_log_timestamp",
        name="Last log timestamp",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        info_function=lambda slf: device_timestamp(slf.coordinator.last_nuki_log_entry.get("timestamp"), slf.device.keyturner_state),
        entity_registry_enabled_default=False,
    ),
    "last_state_timestamp": NukiSensorEntityDescription(
        key="last_state_timestamp",
        name="Last state timestamp",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        info_function=lambda slf: device_timestamp(slf.device.keyturner_state.get("current_time"), slf.device.keyturner_state),
        entity_registry_enabled_default=False,
    ),
}


def device_timestamp(value, state):
    """Return an aware timestamp, or unknown when optional data is missing."""
    offset = state.get("timezone_offset")
    if value is None or offset is None or not -1440 < offset < 1440:
        return None
    return value.replace(tzinfo=datetime.timezone(datetime.timedelta(minutes=offset)))


def enum_options(device_type, sensor: str) -> list[str] | None:
    """Declare supported states while retaining existing raw state strings."""
    device_const = NukiOpenerConst if device_type == NukiConst.NukiDeviceType.OPENER else NukiLockConst
    enum = {
        "lock_state": device_const.LockState,
        "door_sensor_state": NukiConst.DoorsensorState,
        "last_lock_action": device_const.LockAction,
        "last_lock_action_trigger": NukiConst.ActionTrigger,
        "last_lock_action_completion_status": NukiConst.LockActionCompletionStatus,
        "nuki_state": NukiConst.State,
    }.get(sensor)
    return list(map(str, enum.encmapping)) if enum is not None else None

async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Nuki sensor based on a config entry."""
    coordinator: NukiDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [NukiSensor(coordinator, sensor) for sensor in SENSOR_TYPES]
    async_add_entities(entities)


class NukiSensor(NukiEntity, SensorEntity):
    """Representation of a Nuki sensor."""

    def __init__(self, coordinator: NukiDataUpdateCoordinator, sensor: str) -> None:
        """Initialize the Niki sensor."""
        super().__init__(coordinator)
        self.sensor = sensor
        self._attr_unique_id = f"{coordinator.base_unique_id}-{sensor}"
        self._attr_translation_key = sensor
        self._attr_options = enum_options(self.device.device_type, sensor)
        self.entity_description = SENSOR_TYPES[sensor]
        self._info_function = self.entity_description.info_function
        self._async_update_attrs()

    def _async_update_attrs(self) -> None:
        """Update the entity attributes."""
        value = self.entity_description.info_function(self)
        if self.entity_description.device_class == SensorDeviceClass.ENUM:
            value = str(value) if value is not None and str(value) in self._attr_options else None
        self._attr_native_value = value
        if self.entity_description.icon_function:
            self._attr_icon = self.entity_description.icon_function(self)
