"""Radio Guard runtime: schedules backups, tracks state, raises repairs."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_change, async_track_time_interval
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .backup import (
    BackupSource,
    RestoreCheck,
    SourceUnavailable,
    ZHASource,
    Zigbee2MQTTSource,
    ZWaveJSSource,
)
from .const import (
    CONF_BACKUP_DIR,
    CONF_BACKUP_HOUR,
    CONF_BACKUP_MINUTE,
    CONF_FIRST_BACKUP,
    CONF_FIRST_BACKUP_DONE,
    CONF_HEARTBEAT_ENABLED,
    CONF_HEARTBEAT_INTERVAL,
    CONF_HEARTBEAT_TOPIC,
    CONF_MAX_RESTORE_AGE_DAYS,
    CONF_OFFLINE_MINUTES,
    CONF_RETENTION,
    CONF_SOURCES,
    CONF_STALE_HOURS,
    CONF_Z2M_BASE_TOPIC,
    CONF_Z2M_DATA_DIR,
    DEFAULT_BACKUP_DIR,
    DEFAULT_BACKUP_HOUR,
    DEFAULT_BACKUP_MINUTE,
    DEFAULT_HEARTBEAT_ENABLED,
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_HEARTBEAT_TOPIC,
    DEFAULT_MAX_RESTORE_AGE_DAYS,
    DEFAULT_OFFLINE_MINUTES,
    DEFAULT_RETENTION,
    DEFAULT_STALE_HOURS,
    DEFAULT_Z2M_BASE_TOPIC,
    DOMAIN,
    EVENT_BACKUP_COMPLETE,
    EVENT_RESTORE_COMPLETE,
    ISSUE_BACKUP_FAILED,
    ISSUE_BACKUP_NEVER,
    ISSUE_BACKUP_STALE,
    ISSUE_COORDINATOR_DOWN,
    ISSUE_COORDINATOR_REPLACED,
    ISSUE_NEW_SOURCE,
    NOTIFICATION_FIRST_BACKUP,
    PRE_RESTORE_PREFIX,
    SOURCE_NAMES,
    SOURCE_Z2M,
    SOURCE_ZHA,
    SOURCE_ZWAVE,
    STARTUP_GRACE_MINUTES,
)
from .storage import BackupRecord, BackupStore

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = timedelta(minutes=1)
REPLACED_CONFIRMATIONS = 2  # consecutive polls before 'coordinator replaced' is believed
FIRST_BACKUP_DELAY = timedelta(seconds=45)
DEVICE_COUNT_DROP_RATIO = 0.5


def normalize_identity(value: Any) -> str | None:
    """Canonical form for IEEE / home ID so '0x00124B..', '00:12:4b..' and 1234 compare equal."""
    if value is None:
        return None
    s = str(value).strip().lower()
    if s.startswith("0x"):
        s = s[2:]
    s = re.sub(r"[^0-9a-f]", "", s)
    if not s:
        return None
    if s.isdigit() and len(s) <= 10 and not re.fullmatch(r"[0-9a-f]{16}", s):
        # decimal home id -> hex
        with_hex = f"{int(s):08x}"
        return with_hex
    return s


def identity_of(meta: dict[str, Any]) -> str | None:
    return normalize_identity(meta.get("ieee") or meta.get("home_id"))


def device_count_of(meta: dict[str, Any]) -> int | None:
    for key in ("device_count", "node_count"):
        if meta.get(key) is not None:
            return int(meta[key])
    return None


class CoordinatorReplaced(Exception):
    """Scheduled backup skipped because the live coordinator differs from the last backup."""


class RestoreBlocked(Exception):
    """Pre-flight found blocking problems and force was not set."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


@dataclass
class SourceState:
    """What we know about one source."""

    source: BackupSource
    store: BackupStore
    available: bool = False
    latest: BackupRecord | None = None
    records: list[BackupRecord] = field(default_factory=list)
    identity: dict[str, Any] = field(default_factory=dict)
    last_error: str | None = None
    last_attempt: datetime | None = None
    running: bool = False
    restoring: bool = False
    swapping: bool = False  # guided replacement in progress
    down_since: datetime | None = None
    replaced_seen: int = 0

    @property
    def busy(self) -> bool:
        return self.running or self.restoring or self.swapping

    @property
    def count(self) -> int:
        return len(self.records)

    @property
    def age_hours(self) -> float | None:
        if self.latest is None:
            return None
        return (dt_util.utcnow() - self.latest.created).total_seconds() / 3600

    @property
    def live_identity(self) -> str | None:
        return identity_of(self.identity)

    @property
    def backup_identity(self) -> str | None:
        return identity_of(self.latest.meta) if self.latest else None

    @property
    def coordinator_replaced(self) -> bool:
        live, bak = self.live_identity, self.backup_identity
        return bool(live and bak and live != bak)


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
        # Down-detection only counts after HA has fully started plus a grace period, so a slow
        # boot (add-ons, Z2M, the broker still coming up) never looks like a dead coordinator.
        self._ready_at: datetime | None = None
        self._shared: dict[str, str] = {}  # sid -> companion on the same two-in-one stick (while down)
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

    @property
    def max_restore_age_days(self) -> float:
        return float(self._opt(CONF_MAX_RESTORE_AGE_DAYS, DEFAULT_MAX_RESTORE_AGE_DAYS))

    @property
    def offline_minutes(self) -> float:
        return float(self._opt(CONF_OFFLINE_MINUTES, DEFAULT_OFFLINE_MINUTES))

    def _health_counting(self) -> bool:
        return self._ready_at is not None and dt_util.utcnow() >= self._ready_at

    @property
    def schedule(self) -> tuple[int, int]:
        return (
            int(self._opt(CONF_BACKUP_HOUR, DEFAULT_BACKUP_HOUR)),
            int(self._opt(CONF_BACKUP_MINUTE, DEFAULT_BACKUP_MINUTE)),
        )

    @property
    def next_backup(self) -> datetime:
        hour, minute = self.schedule
        now = dt_util.now()
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    def _build_sources(self) -> None:
        wanted: list[str] = self._opt(CONF_SOURCES, [])
        retention = int(self._opt(CONF_RETENTION, DEFAULT_RETENTION))
        factories = {
            SOURCE_Z2M: lambda: Zigbee2MQTTSource(
                self.hass,
                self._opt(CONF_Z2M_BASE_TOPIC, DEFAULT_Z2M_BASE_TOPIC),
                self._opt(CONF_Z2M_DATA_DIR, None),
            ),
            SOURCE_ZHA: lambda: ZHASource(self.hass),
            SOURCE_ZWAVE: lambda: ZWaveJSSource(self.hass),
        }
        for sid in wanted:
            if sid in factories:
                src = factories[sid]()
                self.states[sid] = SourceState(src, BackupStore(self.root, sid, retention))

    # --------------------------------------------------------- lifecycle

    async def async_setup_sources(self) -> None:
        """Let sources acquire subscriptions before the first refresh."""
        for st in self.states.values():
            try:
                await st.source.async_setup()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Source %s setup deferred", st.source.id, exc_info=True)

    async def async_start(self) -> None:
        grace = timedelta(minutes=STARTUP_GRACE_MINUTES)

        @callback
        def _ha_started(_hass: HomeAssistant) -> None:
            # Also runs right away when the integration is (re)loaded on a running HA:
            # sources still get the grace period before anything counts as down.
            self._ready_at = dt_util.utcnow() + grace

        self._unsubs.append(async_at_started(self.hass, _ha_started))
        hour, minute = self.schedule
        self._unsubs.append(
            async_track_time_change(self.hass, self._scheduled_backup, hour=hour, minute=minute, second=0)
        )
        if self._opt(CONF_HEARTBEAT_ENABLED, DEFAULT_HEARTBEAT_ENABLED):
            interval = int(self._opt(CONF_HEARTBEAT_INTERVAL, DEFAULT_HEARTBEAT_INTERVAL))
            self._unsubs.append(
                async_track_time_interval(self.hass, self._heartbeat, timedelta(seconds=interval))
            )
            self.entry.async_create_background_task(
                self.hass, self._heartbeat(dt_util.utcnow()), f"{DOMAIN}_heartbeat_initial"
            )
        if self._opt(CONF_FIRST_BACKUP, True) and not self.entry.options.get(CONF_FIRST_BACKUP_DONE):
            self.entry.async_create_background_task(self.hass, self._first_backup(), f"{DOMAIN}_first_backup")

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for st in self.states.values():
            try:
                await st.source.async_teardown()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Source %s teardown failed", st.source.id, exc_info=True)

    async def _first_backup(self) -> None:
        """Take a first backup shortly after setup and tell the user where it landed."""
        import asyncio  # noqa: PLC0415

        await asyncio.sleep(FIRST_BACKUP_DELAY.total_seconds())
        results = await self.async_backup_all()
        lines = []
        for sid, res in results.items():
            name = self.states[sid].source.name
            if isinstance(res, SourceUnavailable):
                lines.append(f"- **{name}**: skipped, integration not loaded yet")
            elif isinstance(res, Exception):
                lines.append(f"- **{name}**: failed ({res})")
            else:
                ident = res.meta.get("ieee") or res.meta.get("home_id_hex") or res.meta.get("home_id") or ""
                count = device_count_of(res.meta)
                extra = f", {count} devices" if count is not None else ""
                lines.append(f"- **{name}**: `{res.path}`{extra}{f', {ident}' if ident else ''}")
        hour, minute = self.schedule
        body = (
            "First coordinator backups:\n\n"
            + "\n".join(lines)
            + f"\n\nBackups run nightly at {hour:02d}:{minute:02d} and live under `/config/{self.root.name}/`, "
            "so they are included in Home Assistant's own backups. Make sure those leave this machine "
            "(NAS, cloud, or Nabu Casa)."
        )
        persistent_notification.async_create(
            self.hass,
            body,
            title="Radio Guard is protecting your network",
            notification_id=NOTIFICATION_FIRST_BACKUP,
        )
        self.hass.config_entries.async_update_entry(
            self.entry, options={**self.entry.options, CONF_FIRST_BACKUP_DONE: True}
        )

    # ------------------------------------------------------------ polling

    async def _async_update_data(self) -> dict[str, SourceState]:
        for sid, st in self.states.items():
            st.available = st.source.available()
            st.records = await self.hass.async_add_executor_job(st.store.list)
            st.latest = next((r for r in st.records if not r.meta.get("label")), None)
            if st.available:
                try:
                    st.identity = await st.source.async_identity()
                except SourceUnavailable:
                    st.available = False
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("Identity read failed for %s", sid, exc_info=True)
            else:
                st.identity = {}
            await self._update_health(st)
            st.replaced_seen = st.replaced_seen + 1 if st.coordinator_replaced else 0
        self._shared = {}
        if any(self.coordinator_down(st) for st in self.states.values()):
            for sid in self.states:
                if companion := await self.async_companion(sid):
                    self._shared[sid] = companion
        for sid, st in self.states.items():
            self._update_issues(sid, st)
        self._suggest_new_sources()
        return self.states

    def _down_issue(self, sid: str, st: SourceState) -> tuple[bool, str]:
        """(raise the down issue for ``sid``?, name to show). One issue for a dead two-in-one stick."""
        down = self.coordinator_down(st)
        companion = self._shared.get(sid)
        if not down or companion is None or not self.coordinator_down(self.states[companion]):
            return down, st.source.name
        # Both radios of one stick are down: one issue, on the Zigbee side (the wizard handles both).
        if sid == SOURCE_ZWAVE:
            return False, st.source.name
        return True, f"{st.source.name} and {self.states[companion].source.name}"

    def _suggest_new_sources(self) -> None:
        """Offer to protect a ZHA / Z-Wave JS set up after Radio Guard (Z2M can't be told from plain MQTT)."""
        present = {
            SOURCE_ZHA: bool(self.hass.config_entries.async_entries("zha")),
            SOURCE_ZWAVE: bool(self.hass.config_entries.async_entries("zwave_js")),
        }
        for sid, found in present.items():
            _set_issue(
                self.hass,
                f"{ISSUE_NEW_SOURCE}_{sid}",
                found and sid not in self.states,
                translation_key=ISSUE_NEW_SOURCE,
                severity=ir.IssueSeverity.WARNING,
                placeholders={"source": SOURCE_NAMES[sid]},
                data={"source": sid, "action": "add_source"},
            )

    async def _update_health(self, st: SourceState) -> None:
        """Track how long the coordinator has been *known* down. Unknown never counts as down."""
        if st.busy:  # never probe a radio mid-backup/restore/swap
            st.down_since = None
            return
        try:
            healthy = await st.source.async_health()
        except Exception:  # noqa: BLE001
            healthy = None
        if healthy is False and self._health_counting() and not st.busy:
            st.down_since = st.down_since or dt_util.utcnow()
        else:
            st.down_since = None

    def coordinator_down(self, st: SourceState) -> bool:
        if st.down_since is None or st.busy:
            return False
        return dt_util.utcnow() - st.down_since >= timedelta(minutes=self.offline_minutes)

    def _update_issues(self, sid: str, st: SourceState) -> None:
        age = st.age_hours
        down = self.coordinator_down(st)
        # One clear message on failure day: while the coordinator is down, backup nags are noise.
        never = st.available and age is None and not down
        stale = st.available and age is not None and age > self.stale_hours and not down
        if down:
            ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_BACKUP_FAILED}_{sid}")
        down_issue, down_name = self._down_issue(sid, st)
        _set_issue(
            self.hass,
            f"{ISSUE_COORDINATOR_DOWN}_{sid}",
            down_issue,
            translation_key=ISSUE_COORDINATOR_DOWN,
            severity=ir.IssueSeverity.ERROR,
            placeholders={
                "source": down_name,
                "minutes": f"{self.offline_minutes:.0f}",
                "since": dt_util.as_local(st.down_since).strftime("%H:%M") if st.down_since else "",
            },
            data={"source": sid, "action": "replace"},
        )

        _set_issue(
            self.hass,
            f"{ISSUE_BACKUP_NEVER}_{sid}",
            never,
            translation_key=ISSUE_BACKUP_NEVER,
            severity=ir.IssueSeverity.WARNING,
            placeholders={"source": st.source.name},
            data={"source": sid, "action": "backup"},
        )
        _set_issue(
            self.hass,
            f"{ISSUE_BACKUP_STALE}_{sid}",
            stale,
            translation_key=ISSUE_BACKUP_STALE,
            severity=ir.IssueSeverity.WARNING,
            placeholders={"source": st.source.name, "age": f"{age:.0f}" if age else "0"},
            data={"source": sid, "action": "backup"},
        )
        _set_issue(
            self.hass,
            f"{ISSUE_COORDINATOR_REPLACED}_{sid}",
            st.replaced_seen >= REPLACED_CONFIRMATIONS and not st.busy,
            translation_key=ISSUE_COORDINATOR_REPLACED,
            severity=ir.IssueSeverity.WARNING,
            placeholders={
                "source": st.source.name,
                "live": str(st.identity.get("ieee") or st.identity.get("home_id_hex") or st.live_identity),
                "backup": str(
                    (st.latest.meta.get("ieee") or st.latest.meta.get("home_id_hex") or st.backup_identity)
                    if st.latest
                    else ""
                ),
            },
            data={"source": sid, "action": "restore"},
        )

    # ------------------------------------------------------------ backup

    async def _scheduled_backup(self, _now: datetime) -> None:
        await self.async_backup_all(scheduled=True)

    async def async_backup_all(self, *, scheduled: bool = False) -> dict[str, BackupRecord | Exception]:
        """Back up every source. Scheduled runs skip a source whose coordinator changed.

        Otherwise the nightly backup of a freshly plugged-in replacement stick would
        become "latest" and silently hide the backup the user actually needs.
        """
        results: dict[str, BackupRecord | Exception] = {}
        for sid, st in list(self.states.items()):
            if scheduled and (st.down_since is not None or st.swapping):
                _LOGGER.warning("%s: coordinator is down; skipping the scheduled backup", st.source.name)
                results[sid] = SourceUnavailable(f"{st.source.name} is down")
                continue
            if scheduled and st.coordinator_replaced:
                _LOGGER.warning(
                    "%s: coordinator identity changed since the last backup; skipping the scheduled backup "
                    "so the previous network backup stays current. Restore it, or press Back up now to accept "
                    "the new coordinator.",
                    st.source.name,
                )
                results[sid] = CoordinatorReplaced(st.source.name)
                continue
            try:
                results[sid] = await self.async_backup(sid)
            except Exception as err:  # noqa: BLE001
                results[sid] = err
        await self.async_request_refresh()
        return results

    async def async_backup(self, sid: str, *, label: str | None = None) -> BackupRecord:
        st = self.states[sid]
        if st.running:
            raise RuntimeError(f"{st.source.name} backup already running")
        st.running = True
        st.last_attempt = dt_util.utcnow()
        issue_id = f"{ISSUE_BACKUP_FAILED}_{sid}"
        self.async_update_listeners()
        try:
            if not st.source.available():
                # Not an error: the user may have enabled a source ahead of installing it.
                st.last_error = None
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
                raise SourceUnavailable(f"{st.source.name} is not loaded")
            payload = await st.source.async_backup()
            record = await self.hass.async_add_executor_job(
                st.store.write, payload.data, payload.ext, payload.meta, label
            )
            st.records = await self.hass.async_add_executor_job(st.store.list)
            if not label:
                st.latest = record
            st.last_error = None
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            self.hass.bus.async_fire(
                EVENT_BACKUP_COMPLETE, {"source": sid, "file": str(record.path), "size": record.size}
            )
            _LOGGER.info("%s backup written: %s (%d bytes)", st.source.name, record.path.name, record.size)
            return record
        except SourceUnavailable:
            _LOGGER.info("%s backup skipped: integration not loaded", st.source.name)
            raise
        except Exception as err:
            st.last_error = str(err) or type(err).__name__
            _LOGGER.error("%s backup failed: %s", st.source.name, st.last_error)
            _set_issue(
                self.hass,
                issue_id,
                True,
                translation_key=ISSUE_BACKUP_FAILED,
                severity=ir.IssueSeverity.ERROR,
                placeholders={"source": st.source.name, "error": st.last_error},
                data={"source": sid, "action": "backup"},
            )
            raise
        finally:
            st.running = False
            self.async_update_listeners()

    # ----------------------------------------------------------- restore

    def _generic_checks(self, st: SourceState, record: BackupRecord, check: RestoreCheck) -> None:
        """Safeguards that apply to every source."""
        age_days = (dt_util.utcnow() - record.created).total_seconds() / 86400
        if age_days > self.max_restore_age_days:
            check.ok = False
            check.reasons.append(
                f"Backup is {age_days:.0f} days old (limit {self.max_restore_age_days:.0f}). Devices added since "
                "then will be missing. Pick a newer file or use force."
            )
        # Backup taken while the stick was already failing?
        idx = next((i for i, r in enumerate(st.records) if r.path == record.path), None)
        if idx is not None and idx + 1 < len(st.records):
            this_n = device_count_of(record.meta)
            prev_n = device_count_of(st.records[idx + 1].meta)
            if this_n is not None and prev_n and this_n < prev_n * DEVICE_COUNT_DROP_RATIO:
                check.warnings.append(
                    f"This backup has {this_n} devices; the one before it has {prev_n}. The coordinator may "
                    "already have been failing when it was taken. Consider restoring the earlier file."
                )
        if record.path.name.startswith(PRE_RESTORE_PREFIX):
            check.warnings.append(
                "This is a pre-restore snapshot of a target controller, not a nightly backup."
            )

    async def async_restore_check(self, sid: str, file: str | None) -> tuple[BackupRecord, RestoreCheck]:
        st = self.states[sid]
        st.records = await self.hass.async_add_executor_job(st.store.list)
        record = await self.hass.async_add_executor_job(st.store.resolve, file)
        if record is None:
            raise FileNotFoundError(f"No {st.source.name} backup matching {file or 'latest'}")
        data = await self.hass.async_add_executor_job(st.store.read, record)
        check = await st.source.async_restore_check(data, record.meta)
        self._generic_checks(st, record, check)
        return record, check

    async def async_restore(self, sid: str, file: str | None, force: bool) -> BackupRecord:
        st = self.states[sid]
        if st.restoring:
            raise RuntimeError(f"{st.source.name} restore already running")
        st.restoring = True
        self.async_update_listeners()
        try:
            record, check = await self.async_restore_check(sid, file)
            if not check.ok and not force:
                raise RestoreBlocked(check.reasons)
            # Snapshot the target first if it has a network of its own.
            target_n = device_count_of(st.identity)
            if st.available and (target_n is None or target_n > 1):
                try:
                    await self.async_backup(sid, label=PRE_RESTORE_PREFIX.rstrip("_"))
                except Exception:  # noqa: BLE001
                    _LOGGER.warning(
                        "Could not snapshot target %s before restore", st.source.name, exc_info=True
                    )
            data = await self.hass.async_add_executor_job(st.store.read, record)
            _LOGGER.warning(
                "Restoring %s from %s. Warnings: %s",
                st.source.name,
                record.name,
                "; ".join(check.warnings) or "none",
            )
            await st.source.async_restore(data, {**record.meta, "allow_ieee_overwrite": force})
            self.hass.bus.async_fire(EVENT_RESTORE_COMPLETE, {"source": sid, "file": str(record.path)})
            ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_COORDINATOR_REPLACED}_{sid}")
            return record
        finally:
            st.restoring = False
            await self.async_request_refresh()

    # ------------------------------------------------ guided replacement

    async def async_companion(self, sid: str) -> str | None:
        """The other source whose radio sits on the same USB stick as ``sid``'s, if any.

        Only for sources Radio Guard can stop, re-point and start itself; a two-in-one stick is
        then replaced in one go.
        """
        from . import hardware  # noqa: PLC0415

        ports: dict[str, str | None] = {}
        for other in self.states:
            try:
                plan = await self.states[other].source.async_swap_plan()
            except Exception:  # noqa: BLE001
                continue
            if plan.supported and plan.automatic and plan.choose_port and plan.current_port:
                ports[other] = await self.hass.async_add_executor_job(hardware.by_id, plan.current_port)
        mine = ports.get(sid)
        for other, port in ports.items():
            if other != sid and hardware.same_device(mine, port):
                return other
        return None

    def async_swap_begin(self, sid: str) -> None:
        st = self.states[sid]
        if st.busy:
            raise RuntimeError(f"{st.source.name} is busy with a backup or restore; try again in a minute")
        st.swapping = True
        st.down_since = None
        st.source.swap_reset()
        self.async_update_listeners()

    async def async_swap_end(self, sid: str, record: BackupRecord | None) -> BackupRecord | None:
        """Finish a replacement. On success: clear issues, take a fresh backup of the new coordinator."""
        st = self.states[sid]
        st.swapping = False
        fresh = None
        if record is not None:
            for issue in (ISSUE_COORDINATOR_DOWN, ISSUE_COORDINATOR_REPLACED, ISSUE_BACKUP_FAILED):
                ir.async_delete_issue(self.hass, DOMAIN, f"{issue}_{sid}")
            st.replaced_seen = 0
            self.hass.bus.async_fire(EVENT_RESTORE_COMPLETE, {"source": sid, "file": str(record.path)})
            try:
                fresh = await self.async_backup(sid)
            except Exception:  # noqa: BLE001
                _LOGGER.warning("Post-restore backup of %s failed", st.source.name, exc_info=True)
        await self.async_request_refresh()
        return fresh

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


def _set_issue(
    hass: HomeAssistant,
    issue_id: str,
    active: bool,
    *,
    translation_key: str,
    severity: ir.IssueSeverity,
    placeholders: dict[str, str],
    data: dict[str, Any],
) -> None:
    if active:
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            severity=severity,
            translation_key=translation_key,
            translation_placeholders=placeholders,
            data=data,
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, issue_id)


_BOOT: dict[int, datetime] = {}


def _boot_time(hass: HomeAssistant) -> datetime:
    return _BOOT.setdefault(id(hass), dt_util.utcnow())
