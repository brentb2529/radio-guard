"""Radio Guard runtime: schedules backups, tracks state, raises repairs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json
import logging
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_change, async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .backup import (
    BackupSource,
    RestoreCheck,
    SourceUnavailable,
    Zigbee2MQTTSource,
    ZHASource,
    ZWaveJSSource,
)
from .const import (
    CONF_BACKUP_DIR,
    CONF_BACKUP_HOUR,
    CONF_BACKUP_MINUTE,
    CONF_HEARTBEAT_ENABLED,
    CONF_HEARTBEAT_INTERVAL,
    CONF_HEARTBEAT_TOPIC,
    CONF_RETENTION,
    CONF_SOURCES,
    CONF_STALE_HOURS,
    CONF_Z2M_BASE_TOPIC,
    DEFAULT_BACKUP_DIR,
    DEFAULT_BACKUP_HOUR,
    DEFAULT_BACKUP_MINUTE,
    DEFAULT_HEARTBEAT_ENABLED,
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_HEARTBEAT_TOPIC,
    DEFAULT_RETENTION,
    DEFAULT_STALE_HOURS,
    DEFAULT_Z2M_BASE_TOPIC,
    DOMAIN,
    EVENT_BACKUP_COMPLETE,
    EVENT_RESTORE_COMPLETE,
    ISSUE_BACKUP_FAILED,
    ISSUE_BACKUP_STALE,
    SOURCE_Z2M,
    SOURCE_ZHA,
    SOURCE_ZWAVE,
)
from .storage import BackupRecord, BackupStore

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = timedelta(minutes=15)


@dataclass
class SourceState:
    """What we know about one source."""

    source: BackupSource
    store: BackupStore
    available: bool = False
    latest: BackupRecord | None = None
    count: int = 0
    identity: dict[str, Any] = field(default_factory=dict)
    last_error: str | None = None
    last_attempt: datetime | None = None
    running: bool = False

    @property
    def age_hours(self) -> float | None:
        if self.latest is None:
            return None
        return (dt_util.utcnow() - self.latest.created).total_seconds() / 3600


class RadioGuardCoordinator(DataUpdateCoordinator[dict[str, SourceState]]):
    """Owns sources, schedule, and heartbeat for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=POLL_INTERVAL,
            config_entry=entry,
        )
        self.entry = entry
        self._unsubs: list[CALLBACK_TYPE] = []
        self.states: dict[str, SourceState] = {}
        self._build_sources()

    # ------------------------------------------------------------ config

    def _opt(self, key: str, default: Any) -> Any:
        return self.entry.options.get(key, self.entry.data.get(key, default))

    @property
    def root(self) -> Path:
        return Path(self.hass.config.path(self._opt(CONF_BACKUP_DIR, DEFAULT_BACKUP_DIR)))

    @property
    def stale_hours(self) -> float:
        return float(self._opt(CONF_STALE_HOURS, DEFAULT_STALE_HOURS))

    def _build_sources(self) -> None:
        wanted: list[str] = self._opt(CONF_SOURCES, [])
        retention = int(self._opt(CONF_RETENTION, DEFAULT_RETENTION))
        factories = {
            SOURCE_Z2M: lambda: Zigbee2MQTTSource(
                self.hass, self._opt(CONF_Z2M_BASE_TOPIC, DEFAULT_Z2M_BASE_TOPIC)
            ),
            SOURCE_ZHA: lambda: ZHASource(self.hass),
            SOURCE_ZWAVE: lambda: ZWaveJSSource(self.hass),
        }
        for sid in wanted:
            if sid in factories:
                src = factories[sid]()
                self.states[sid] = SourceState(src, BackupStore(self.root, sid, retention))

    # --------------------------------------------------------- lifecycle

    async def async_start(self) -> None:
        hour = int(self._opt(CONF_BACKUP_HOUR, DEFAULT_BACKUP_HOUR))
        minute = int(self._opt(CONF_BACKUP_MINUTE, DEFAULT_BACKUP_MINUTE))
        self._unsubs.append(
            async_track_time_change(self.hass, self._scheduled_backup, hour=hour, minute=minute, second=0)
        )
        if self._opt(CONF_HEARTBEAT_ENABLED, DEFAULT_HEARTBEAT_ENABLED):
            interval = int(self._opt(CONF_HEARTBEAT_INTERVAL, DEFAULT_HEARTBEAT_INTERVAL))
            self._unsubs.append(
                async_track_time_interval(self.hass, self._heartbeat, timedelta(seconds=interval))
            )
            self.hass.async_create_task(self._heartbeat(dt_util.utcnow()))

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    # ------------------------------------------------------------ polling

    async def _async_update_data(self) -> dict[str, SourceState]:
        for sid, st in self.states.items():
            st.available = st.source.available()
            records = await self.hass.async_add_executor_job(st.store.list)
            st.latest = records[0] if records else None
            st.count = len(records)
            if st.available:
                try:
                    st.identity = await st.source.async_identity()
                except SourceUnavailable:
                    st.available = False
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("Identity read failed for %s", sid, exc_info=True)
            self._update_stale_issue(sid, st)
        return self.states

    def _update_stale_issue(self, sid: str, st: SourceState) -> None:
        issue_id = f"{ISSUE_BACKUP_STALE}_{sid}"
        age = st.age_hours
        stale = st.available and (age is None or age > self.stale_hours)
        if stale:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_BACKUP_STALE,
                translation_placeholders={
                    "source": st.source.name,
                    "age": "never" if age is None else f"{age:.0f} h",
                },
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    # ------------------------------------------------------------ backup

    async def _scheduled_backup(self, _now: datetime) -> None:
        await self.async_backup_all()

    async def async_backup_all(self) -> dict[str, BackupRecord | Exception]:
        results: dict[str, BackupRecord | Exception] = {}
        for sid in list(self.states):
            try:
                results[sid] = await self.async_backup(sid)
            except Exception as err:  # noqa: BLE001
                results[sid] = err
        await self.async_request_refresh()
        return results

    async def async_backup(self, sid: str) -> BackupRecord:
        st = self.states[sid]
        if st.running:
            raise RuntimeError(f"{st.source.name} backup already running")
        st.running = True
        st.last_attempt = dt_util.utcnow()
        issue_id = f"{ISSUE_BACKUP_FAILED}_{sid}"
        try:
            if not st.source.available():
                raise SourceUnavailable(f"{st.source.name} is not loaded")
            payload = await st.source.async_backup()
            record = await self.hass.async_add_executor_job(
                st.store.write, payload.data, payload.ext, payload.meta
            )
            st.latest = record
            st.last_error = None
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            self.hass.bus.async_fire(
                EVENT_BACKUP_COMPLETE,
                {"source": sid, "file": str(record.path), "size": record.size},
            )
            _LOGGER.info("%s backup written: %s (%d bytes)", st.source.name, record.path.name, record.size)
            return record
        except Exception as err:
            st.last_error = str(err)
            _LOGGER.error("%s backup failed: %s", st.source.name, err)
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_BACKUP_FAILED,
                translation_placeholders={"source": st.source.name, "error": str(err)},
            )
            raise
        finally:
            st.running = False
            self.async_update_listeners()

    # ----------------------------------------------------------- restore

    async def async_restore_check(self, sid: str, file: str | None) -> tuple[BackupRecord, RestoreCheck]:
        st = self.states[sid]
        record = await self.hass.async_add_executor_job(st.store.resolve, file)
        if record is None:
            raise FileNotFoundError(f"No {st.source.name} backup matching {file or 'latest'}")
        data = await self.hass.async_add_executor_job(st.store.read, record)
        check = await st.source.async_restore_check(data, record.meta)
        return record, check

    async def async_restore(self, sid: str, file: str | None, force: bool) -> BackupRecord:
        st = self.states[sid]
        record, check = await self.async_restore_check(sid, file)
        if not check.ok and not force:
            raise RuntimeError("Restore pre-flight failed: " + "; ".join(check.reasons))
        data = await self.hass.async_add_executor_job(st.store.read, record)
        _LOGGER.warning(
            "Restoring %s from %s. Warnings: %s", st.source.name, record.name, "; ".join(check.warnings) or "none"
        )
        await st.source.async_restore(data, record.meta)
        self.hass.bus.async_fire(EVENT_RESTORE_COMPLETE, {"source": sid, "file": str(record.path)})
        await self.async_request_refresh()
        return record

    # --------------------------------------------------------- heartbeat

    async def _heartbeat(self, now: datetime) -> None:
        """Publish a liveness beacon an external watchdog can key on."""
        if "mqtt" not in self.hass.config.components:
            return
        from homeassistant.components import mqtt  # noqa: PLC0415

        topic = self._opt(CONF_HEARTBEAT_TOPIC, DEFAULT_HEARTBEAT_TOPIC)
        payload = {
            "ts": dt_util.utcnow().isoformat(),
            "uptime_s": int((dt_util.utcnow() - _boot_time(self.hass)).total_seconds()),
            "sources": {
                sid: {
                    "available": st.available,
                    "backup_age_h": None if st.age_hours is None else round(st.age_hours, 1),
                }
                for sid, st in self.states.items()
            },
        }
        try:
            await mqtt.async_publish(self.hass, topic, json.dumps(payload), qos=0, retain=False)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Heartbeat publish failed", exc_info=True)


_BOOT: dict[int, datetime] = {}


def _boot_time(hass: HomeAssistant) -> datetime:
    return _BOOT.setdefault(id(hass), dt_util.utcnow())
