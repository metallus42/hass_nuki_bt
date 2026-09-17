"""Support for Nuki Opener doorbell events."""
from __future__ import annotations

from homeassistant.components.event import EventEntity, EventEntityDescription
from homeassistant.core import callback

from pyNukiBT import NukiConst

from .const import DOMAIN
from .coordinator import NukiDataUpdateCoordinator
from .entity import NukiEntity


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Set up the Nuki Opener doorbell event entity."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    if coordinator.device.device_type == NukiConst.NukiDeviceType.OPENER:
        async_add_entities([NukiOpenerDoorbellEvent(coordinator)])


class NukiOpenerDoorbellEvent(NukiEntity, EventEntity):
    """Represent a physical press of the Opener's doorbell."""

    _attr_event_types = ["doorbell"]
    entity_description = EventEntityDescription(
        key="doorbell",
        name="Doorbell",
        icon="mdi:doorbell",
    )

    def __init__(self, coordinator: NukiDataUpdateCoordinator) -> None:
        """Initialize the doorbell event entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.base_unique_id}-doorbell"

    async def async_added_to_hass(self) -> None:
        """Subscribe once the entity is managed by Home Assistant."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_doorbell_listener(self._async_handle_doorbell)
        )

    @callback
    def _async_handle_doorbell(self) -> None:
        """Record a physical doorbell press."""
        self.async_set_event("doorbell")
