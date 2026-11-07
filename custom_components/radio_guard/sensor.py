"""Sensors: last backup, backup age, backup count, coordinator identity."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import RadioGuardConfigEntry
from .entity import RadioGuardEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: RadioGuardConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord = entry.runtime_data
    entities: list[SensorEntity] = []
    for sid in coord.states:
        entities += [
            LastBackupSensor(coord, sid),
            BackupAgeSensor(coord, sid),
            BackupCountSensor(coord, sid),
            IdentitySensor(coord, sid),
            LastErrorSensor(coord, sid),
        ]
    async_add_entities(entities)


class LastBackupSensor(RadioGuardEntity, SensorEntity):
    entity_description = SensorEntityDescription(key="last_backup", device_class=SensorDeviceClass.TIMESTAMP)

    def __init__(self, coord, sid) -> None:
        super().__init__(coord, sid, "last_backup")

    @property
    def native_value(self) -> datetime | None:
        rec = self.state_obj.latest
        return rec.created if rec else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        rec = self.state_obj.latest
        if not rec:
            return {}
        return {
            "file": str(rec.path),
            "size": rec.size,
            **{k: v for k, v in rec.meta.items() if k != "members"},
        }


class BackupAgeSensor(RadioGuardEntity, SensorEntity):
    entity_description = SensorEntityDescription(
        key="backup_age",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
    )

    def __init__(self, coord, sid) -> None:
        super().__init__(coord, sid, "backup_age")

    @property
    def native_value(self) -> float | None:
        return self.state_obj.age_hours


class BackupCountSensor(RadioGuardEntity, SensorEntity):
    entity_description = SensorEntityDescription(
        key="backup_count",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    )

    def __init__(self, coord, sid) -> None:
        super().__init__(coord, sid, "backup_count")

    @property
    def native_value(self) -> int:
        return self.state_obj.count


class IdentitySensor(RadioGuardEntity, SensorEntity):
    """IEEE (Zigbee) or home ID (Z-Wave) of the live coordinator."""

    entity_description = SensorEntityDescription(key="identity", entity_category=EntityCategory.DIAGNOSTIC)

    def __init__(self, coord, sid) -> None:
        super().__init__(coord, sid, "identity")

    @property
    def native_value(self) -> str | None:
        ident = self.state_obj.identity
        val = ident.get("ieee") or ident.get("home_id")
        return str(val) if val is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return dict(self.state_obj.identity)


class LastErrorSensor(RadioGuardEntity, SensorEntity):
    entity_description = SensorEntityDescription(key="last_error", entity_category=EntityCategory.DIAGNOSTIC)

    def __init__(self, coord, sid) -> None:
        super().__init__(coord, sid, "last_error")

    @property
    def native_value(self) -> str:
        err = self.state_obj.last_error
        return err[:250] if err else "none"
