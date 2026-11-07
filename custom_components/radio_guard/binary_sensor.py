"""Binary sensors: backup stale (problem), source available (connectivity)."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import RadioGuardConfigEntry
from .entity import RadioGuardEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: RadioGuardConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord = entry.runtime_data
    entities: list[BinarySensorEntity] = []
    for sid in coord.states:
        entities += [StaleSensor(coord, sid), AvailableSensor(coord, sid)]
    async_add_entities(entities)


class StaleSensor(RadioGuardEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coord, sid) -> None:
        super().__init__(coord, sid, "backup_stale")

    @property
    def is_on(self) -> bool:
        age = self.state_obj.age_hours
        return age is None or age > self.coordinator.stale_hours


class AvailableSensor(RadioGuardEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord, sid) -> None:
        super().__init__(coord, sid, "source_available")

    @property
    def is_on(self) -> bool:
        return self.state_obj.available
