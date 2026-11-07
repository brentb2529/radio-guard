"""Binary sensors: backup problem, coordinator replaced, source loaded, backup running."""

from __future__ import annotations

from typing import Any

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
        entities += [
            BackupProblemSensor(coord, sid),
            CoordinatorReplacedSensor(coord, sid),
            SourceLoadedSensor(coord, sid),
            BackupRunningSensor(coord, sid),
        ]
    async_add_entities(entities)


class BackupProblemSensor(RadioGuardEntity, BinarySensorEntity):
    """On when the newest backup is missing, stale, or the last attempt failed."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coord, sid) -> None:
        super().__init__(coord, sid, "backup_stale")

    @property
    def is_on(self) -> bool:
        st = self.state_obj
        if not st.available:
            return False
        age = st.age_hours
        return age is None or age > self.coordinator.stale_hours or bool(st.last_error)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        st = self.state_obj
        return {
            "stale_after_hours": self.coordinator.stale_hours,
            "backup_age_hours": None if st.age_hours is None else round(st.age_hours, 1),
            "last_backup": st.latest.created.isoformat() if st.latest else None,
            "last_attempt": st.last_attempt.isoformat() if st.last_attempt else None,
            "last_error": st.last_error,
        }


class CoordinatorReplacedSensor(RadioGuardEntity, BinarySensorEntity):
    """On when the live coordinator's identity differs from the newest backup's."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coord, sid) -> None:
        super().__init__(coord, sid, "coordinator_replaced")

    @property
    def is_on(self) -> bool:
        return self.state_obj.coordinator_replaced

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        st = self.state_obj
        return {"live_identity": st.live_identity, "backup_identity": st.backup_identity}


class SourceLoadedSensor(RadioGuardEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coord, sid) -> None:
        super().__init__(coord, sid, "source_loaded")

    @property
    def is_on(self) -> bool:
        return self.state_obj.available


class BackupRunningSensor(RadioGuardEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord, sid) -> None:
        super().__init__(coord, sid, "backup_running")

    @property
    def is_on(self) -> bool:
        return self.state_obj.running or self.state_obj.restoring
