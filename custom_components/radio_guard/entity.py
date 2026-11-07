"""Shared entity base."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import RadioGuardCoordinator, SourceState


class RadioGuardEntity(CoordinatorEntity[RadioGuardCoordinator]):
    """One device per backup source."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: RadioGuardCoordinator, sid: str, key: str) -> None:
        super().__init__(coordinator)
        self.sid = sid
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{sid}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.entry.entry_id}_{sid}")},
            name=f"Radio Guard {self.state_obj.source.name}",
            manufacturer="Radio Guard",
            model=self.state_obj.source.name,
            entry_type=None,
        )

    @property
    def state_obj(self) -> SourceState:
        return self.coordinator.states[self.sid]
