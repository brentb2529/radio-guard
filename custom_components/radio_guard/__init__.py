"""Radio Guard: scheduled coordinator backups for Zigbee and Z-Wave, with restore."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util

from .backup import RestoreCheck, RestoreStaged
from .const import (
    ALL_SOURCES,
    ATTR_FILE,
    ATTR_FORCE,
    ATTR_MODE,
    ATTR_SOURCE,
    DOMAIN,
    MODE_DRY_RUN,
    MODE_RESTORE,
    NOTIFICATION_RESTORE,
    SERVICE_BACKUP_NOW,
    SERVICE_LIST_BACKUPS,
    SERVICE_RESTORE,
)
from .coordinator import RadioGuardCoordinator, RestoreBlocked, device_count_of
from .storage import BackupRecord

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON]

type RadioGuardConfigEntry = ConfigEntry[RadioGuardCoordinator]

SOURCE_FIELD = vol.In(ALL_SOURCES)
BACKUP_SCHEMA = vol.Schema({vol.Optional(ATTR_SOURCE): SOURCE_FIELD})
LIST_SCHEMA = vol.Schema({vol.Optional(ATTR_SOURCE): SOURCE_FIELD})
RESTORE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_SOURCE): SOURCE_FIELD,
        vol.Optional(ATTR_FILE): cv.string,
        vol.Optional(ATTR_MODE, default=MODE_DRY_RUN): vol.In([MODE_DRY_RUN, MODE_RESTORE]),
        vol.Optional(ATTR_FORCE, default=False): cv.boolean,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: RadioGuardConfigEntry) -> bool:
    coordinator = RadioGuardCoordinator(hass, entry)
    await coordinator.async_setup_sources()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    _remove_orphan_devices(hass, entry, coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await coordinator.async_start()
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    _register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RadioGuardConfigEntry) -> bool:
    await entry.runtime_data.async_stop()
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok and len(hass.config_entries.async_loaded_entries(DOMAIN)) <= 1:
        for name in (SERVICE_BACKUP_NOW, SERVICE_RESTORE, SERVICE_LIST_BACKUPS):
            hass.services.async_remove(DOMAIN, name)
    return ok


def _remove_orphan_devices(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: RadioGuardCoordinator
) -> None:
    """Drop devices for sources the user removed in Options."""
    registry = dr.async_get(hass)
    keep = {f"{entry.entry_id}_{sid}" for sid in coordinator.states}
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        ours = {ident for domain, ident in device.identifiers if domain == DOMAIN}
        if ours and not ours & keep:
            registry.async_remove_device(device.id)


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _coordinator(hass: HomeAssistant) -> RadioGuardCoordinator:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_set_up")
    return entries[0].runtime_data


def _source_or_raise(coord: RadioGuardCoordinator, sid: str) -> str:
    if sid not in coord.states:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="source_not_enabled",
            translation_placeholders={"source": sid},
        )
    return sid


def _record_summary(rec: BackupRecord) -> dict:
    meta = rec.meta
    return {
        "file": rec.name,
        "path": str(rec.path),
        "created": rec.created.isoformat(),
        "age_hours": round((dt_util.utcnow() - rec.created).total_seconds() / 3600, 1),
        "size": rec.size,
        "identity": meta.get("ieee") or meta.get("home_id_hex") or meta.get("home_id"),
        "devices": device_count_of(meta),
        "format": meta.get("format"),
    }


def dry_run_response(source_name: str, rec: BackupRecord, check: RestoreCheck) -> dict:
    """Shape the pre-flight so it reads top to bottom in Developer Tools."""
    bak_id = rec.meta.get("ieee") or rec.meta.get("home_id_hex") or rec.meta.get("home_id") or "?"
    cur = check.current
    cur_id = cur.get("ieee") or cur.get("home_id_hex") or cur.get("home_id") or "not readable"
    devices = device_count_of(rec.meta)
    summary = (
        f"Restore {source_name} network {bak_id}"
        f"{f' ({devices} devices)' if devices is not None else ''} from {rec.name} onto coordinator {cur_id}"
        f"{f' ({device_count_of(cur)} devices)' if device_count_of(cur) is not None else ''}"
    )
    return {
        "dry_run": True,
        "verdict": "OK" if check.ok else "BLOCKED",
        "summary": summary,
        "backup": _record_summary(rec),
        "target": {k: v for k, v in cur.items() if v is not None},
        "blocking": check.reasons,
        "warnings": check.warnings,
        "next": (
            "Call again with mode: restore to perform it, or open Settings > Repairs for a guided restore."
            if check.ok
            else "Fix the blocking items, pick another file, or call again with mode: restore and force: true."
        ),
    }


def _register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_BACKUP_NOW):
        return

    async def _backup_now(call: ServiceCall) -> ServiceResponse:
        coord = _coordinator(hass)
        source = call.data.get(ATTR_SOURCE)
        if source:
            _source_or_raise(coord, source)
            try:
                rec = await coord.async_backup(source)
            except Exception as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="backup_failed",
                    translation_placeholders={"source": coord.states[source].source.name, "error": str(err)},
                ) from err
            await coord.async_request_refresh()
            return {"results": {source: _record_summary(rec)}}
        results = await coord.async_backup_all()
        return {
            "results": {
                sid: (_record_summary(r) if not isinstance(r, Exception) else {"error": str(r)})
                for sid, r in results.items()
            }
        }

    async def _list_backups(call: ServiceCall) -> ServiceResponse:
        coord = _coordinator(hass)
        sids = (
            [_source_or_raise(coord, call.data[ATTR_SOURCE])]
            if call.data.get(ATTR_SOURCE)
            else list(coord.states)
        )
        out = {}
        for sid in sids:
            st = coord.states[sid]
            records = await hass.async_add_executor_job(st.store.list)
            out[sid] = [_record_summary(r) for r in records]
        return {"backups": out}

    async def _restore(call: ServiceCall) -> ServiceResponse:
        coord = _coordinator(hass)
        source = _source_or_raise(coord, call.data[ATTR_SOURCE])
        name = coord.states[source].source.name
        file = call.data.get(ATTR_FILE)
        if call.data[ATTR_MODE] == MODE_DRY_RUN:
            try:
                record, check = await coord.async_restore_check(source, file)
            except FileNotFoundError as err:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="no_backup",
                    translation_placeholders={"source": name, "file": file or "latest"},
                ) from err
            resp = dry_run_response(name, record, check)
            persistent_notification.async_create(
                hass,
                f"**{resp['summary']}**\n\nVerdict: {resp['verdict']}\n\n"
                + ("\n".join(f"- BLOCKING: {r}" for r in check.reasons) + "\n" if check.reasons else "")
                + "\n".join(f"- {w}" for w in check.warnings)
                + f"\n\n{resp['next']}",
                title=f"Radio Guard dry run: {name}",
                notification_id=f"{NOTIFICATION_RESTORE}_{source}",
            )
            return resp
        try:
            record = await coord.async_restore(source, file, call.data[ATTR_FORCE])
        except FileNotFoundError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_backup",
                translation_placeholders={"source": name, "file": file or "latest"},
            ) from err
        except RestoreBlocked as blocked:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="restore_blocked",
                translation_placeholders={"source": name, "reasons": str(blocked)},
            ) from blocked
        except RestoreStaged as staged:
            persistent_notification.async_create(
                hass,
                f"{name} restore needs a few manual steps.\n\nFiles are staged in `{staged.path}`.\n\n{staged.steps}",
                title=f"Radio Guard: finish the {name} restore by hand",
                notification_id=f"{NOTIFICATION_RESTORE}_{source}",
            )
            return {"dry_run": False, "staged": True, "path": staged.path, "steps": staged.steps}
        except Exception as err:
            persistent_notification.async_create(
                hass,
                f"{name} restore from {file or 'latest'} failed:\n\n{err}",
                title=f"Radio Guard restore failed: {name}",
                notification_id=f"{NOTIFICATION_RESTORE}_{source}",
            )
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="restore_failed",
                translation_placeholders={"source": name, "error": str(err)},
            ) from err
        persistent_notification.async_create(
            hass,
            f"{name} network restored from `{record.name}`. Devices re-announce over the next minutes; "
            "battery devices on their next wake-up. Keep the old coordinator unplugged.",
            title=f"Radio Guard restore complete: {name}",
            notification_id=f"{NOTIFICATION_RESTORE}_{source}",
        )
        return {"dry_run": False, "restored": _record_summary(record)}

    hass.services.async_register(
        DOMAIN,
        SERVICE_BACKUP_NOW,
        _backup_now,
        schema=BACKUP_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LIST_BACKUPS,
        _list_backups,
        schema=LIST_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RESTORE, _restore, schema=RESTORE_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
