"""Radio Guard: scheduled coordinator backups for Zigbee and Z-Wave, with restore."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import (
    ALL_SOURCES,
    ATTR_CONFIRM,
    ATTR_FILE,
    ATTR_FORCE,
    ATTR_SOURCE,
    DOMAIN,
    SERVICE_BACKUP_NOW,
    SERVICE_RESTORE,
)
from .coordinator import RadioGuardCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON]

type RadioGuardConfigEntry = ConfigEntry[RadioGuardCoordinator]

BACKUP_SCHEMA = vol.Schema({vol.Optional(ATTR_SOURCE): vol.In(ALL_SOURCES)})
RESTORE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_SOURCE): vol.In(ALL_SOURCES),
        vol.Optional(ATTR_FILE): cv.string,
        vol.Optional(ATTR_CONFIRM, default=False): cv.boolean,
        vol.Optional(ATTR_FORCE, default=False): cv.boolean,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: RadioGuardConfigEntry) -> bool:
    coordinator = RadioGuardCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    await coordinator.async_start()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    _register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RadioGuardConfigEntry) -> bool:
    await entry.runtime_data.async_stop()
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok and not hass.config_entries.async_loaded_entries(DOMAIN):
        hass.services.async_remove(DOMAIN, SERVICE_BACKUP_NOW)
        hass.services.async_remove(DOMAIN, SERVICE_RESTORE)
    return ok


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _coordinator(hass: HomeAssistant) -> RadioGuardCoordinator:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise HomeAssistantError("Radio Guard is not set up")
    return entries[0].runtime_data


def _register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_BACKUP_NOW):
        return

    async def _backup_now(call: ServiceCall) -> ServiceResponse:
        coord = _coordinator(hass)
        source = call.data.get(ATTR_SOURCE)
        if source:
            if source not in coord.states:
                raise HomeAssistantError(f"{source} is not enabled in Radio Guard")
            try:
                rec = await coord.async_backup(source)
            except Exception as err:
                raise HomeAssistantError(f"Backup failed: {err}") from err
            await coord.async_request_refresh()
            return {"results": {source: rec.as_dict()}}
        results = await coord.async_backup_all()
        out = {
            sid: (r.as_dict() if not isinstance(r, Exception) else {"error": str(r)})
            for sid, r in results.items()
        }
        return {"results": out}

    async def _restore(call: ServiceCall) -> ServiceResponse:
        coord = _coordinator(hass)
        source = call.data[ATTR_SOURCE]
        if source not in coord.states:
            raise HomeAssistantError(f"{source} is not enabled in Radio Guard")
        file = call.data.get(ATTR_FILE)
        if not call.data[ATTR_CONFIRM]:
            # Dry run: report what would happen and stop.
            try:
                record, check = await coord.async_restore_check(source, file)
            except Exception as err:
                raise HomeAssistantError(str(err)) from err
            return {
                "dry_run": True,
                "file": record.as_dict(),
                "ok": check.ok,
                "reasons": check.reasons,
                "warnings": check.warnings,
                "current": check.current,
                "backup": check.backup,
                "hint": "Call again with confirm: true to restore.",
            }
        try:
            record = await coord.async_restore(source, file, call.data[ATTR_FORCE])
        except Exception as err:
            raise HomeAssistantError(f"Restore failed: {err}") from err
        return {"dry_run": False, "restored": record.as_dict()}

    hass.services.async_register(
        DOMAIN,
        SERVICE_BACKUP_NOW,
        _backup_now,
        schema=BACKUP_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RESTORE,
        _restore,
        schema=RESTORE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
