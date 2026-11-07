"""Sensors: last backup, next backup, stored backups, coordinator identity."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import RadioGuardConfigEntry
from .coordinator import device_count_of
from .entity import RadioGuardEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: RadioGuardConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord = entry.runtime_data
    entities: list[SensorEntity] = []
    for sid in coord.states:
        entities += [
            LastBackupSensor(coord, sid),
            NextBackupSensor(coord, sid),
            StoredBackupsSensor(coord, sid),
            IdentitySensor(coord, sid),
        ]
    async_add_entities(entities)


class LastBackupSensor(RadioGuardEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

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
        skip = {"members", "security_keys"}
        # Sidecar meta carries "file" as the bare name; the attribute is the full path.
        return {
            **{k: v for k, v in rec.meta.items() if k not in skip},
            "file": str(rec.path),
            "size": rec.size,
        }


class NextBackupSensor(RadioGuardEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord, sid) -> None:
        super().__init__(coord, sid, "next_backup")

    @property
    def native_value(self) -> datetime:
        return self.coordinator.next_backup


class StoredBackupsSensor(RadioGuardEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = None

    def __init__(self, coord, sid) -> None:
        super().__init__(coord, sid, "stored_backups")

    @property
    def native_value(self) -> int:
        return self.state_obj.count

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "backups": [
                {
                    "file": r.name,
                    "created": r.created.isoformat(),
                    "devices": device_count_of(r.meta),
                    "identity": r.meta.get("ieee") or r.meta.get("home_id_hex") or r.meta.get("home_id"),
                    "label": r.meta.get("label"),
                }
                for r in self.state_obj.records
            ]
        }


class IdentitySensor(RadioGuardEntity, SensorEntity):
    """IEEE (Zigbee) or home ID (Z-Wave) of the live coordinator."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord, sid) -> None:
        super().__init__(coord, sid, "identity")

    @property
    def native_value(self) -> str | None:
        ident = self.state_obj.identity
        val = ident.get("ieee") or ident.get("home_id_hex") or ident.get("home_id")
        return str(val) if val is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        ident = {k: v for k, v in self.state_obj.identity.items() if v is not None}
        ident["matches_latest_backup"] = (
            not self.state_obj.coordinator_replaced
            if self.state_obj.latest and self.state_obj.identity
            else None
        )
        return ident
