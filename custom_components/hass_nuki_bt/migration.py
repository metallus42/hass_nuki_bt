"""Migrate legacy manual entries while retaining their entity IDs."""

from homeassistant.helpers import entity_registry as er

from .const import CONF_DEVICE_ADDRESS, DOMAIN, LOGGER


async def async_migrate_entry(hass, entry) -> bool:
    """Give old manual entries a stable identity without recreating entities."""
    if entry.version > 2:
        return False
    if entry.version == 2:
        return True
    address = entry.data[CONF_DEVICE_ADDRESS].strip().upper()
    unique_id = entry.unique_id or address.replace(":", "").lower()
    for other in hass.config_entries.async_entries(DOMAIN):
        if other.entry_id != entry.entry_id and other.unique_id == unique_id:
            LOGGER.error("Cannot migrate duplicate Nuki entry %s; remove the duplicate configuration first", entry.title)
            return False

    registry = er.async_get(hass)
    changes = []
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entry.unique_id is None and entity.unique_id.startswith("None-"):
            new_unique_id = unique_id + entity.unique_id[4:]
            conflict = registry.async_get_entity_id(entity.domain, entity.platform, new_unique_id)
            if conflict is not None and conflict != entity.entity_id:
                LOGGER.error("Cannot migrate Nuki entity %s because its unique ID already exists", entity.entity_id)
                return False
            changes.append((entity.entity_id, new_unique_id))
    # Preflight all collisions before changing anything. Updating unique_id does
    # not rename entity_id, so dashboards and automations keep their targets.
    for entity_id, new_unique_id in changes:
        registry.async_update_entity(entity_id, new_unique_id=new_unique_id)
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, CONF_DEVICE_ADDRESS: address},
        unique_id=unique_id,
        version=2,
    )
    return True
