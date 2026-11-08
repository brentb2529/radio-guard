"""Button: back up now, per source."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import RadioGuardConfigEntry
from .const import DOMAIN
from .entity import RadioGuardEntity


def _short(err: Exception) -> str:
    """Drop library prefixes like 'zwave_error: Z-Wave error 203 - ' so the toast reads as a sentence."""
    text = str(err) or type(err).__name__
    if " - " in text and text.split(" - ", 1)[0].count(" ") < 6:
        text = text.split(" - ", 1)[1]
    return text.rstrip(".")


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
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="backup_failed",
                translation_placeholders={"source": self.state_obj.source.name, "error": _short(err)},
            ) from err
        await self.coordinator.async_request_refresh()
