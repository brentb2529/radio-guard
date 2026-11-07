"""Button: back up now, per source."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import RadioGuardConfigEntry
from .entity import RadioGuardEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: RadioGuardConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord = entry.runtime_data
    async_add_entities(BackupNowButton(coord, sid) for sid in coord.states)


class BackupNowButton(RadioGuardEntity, ButtonEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coord, sid) -> None:
        super().__init__(coord, sid, "backup_now")

    async def async_press(self) -> None:
        try:
            await self.coordinator.async_backup(self.sid)
        except Exception as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()
