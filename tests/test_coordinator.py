"""Coordinator, setup/unload, schedule, issues and restore orchestration.

Every source is replaced by ``FakeSource`` (patched where the coordinator builds
its sources), so nothing here touches MQTT, zigpy or zwave-js.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.radio_guard.backup.base import (
    BackupPayload,
    BackupSource,
    RestoreCheck,
    RestoreStaged,
    SourceUnavailable,
)
from custom_components.radio_guard.const import (
    CONF_BACKUP_HOUR,
    CONF_BACKUP_MINUTE,
    CONF_FIRST_BACKUP,
    CONF_FIRST_BACKUP_DONE,
    CONF_MAX_RESTORE_AGE_DAYS,
    CONF_SOURCES,
    DOMAIN,
    EVENT_BACKUP_COMPLETE,
    EVENT_RESTORE_COMPLETE,
    NOTIFICATION_FIRST_BACKUP,
    PRE_RESTORE_PREFIX,
    SOURCE_NAMES,
    SOURCE_Z2M,
    SOURCE_ZHA,
    SOURCE_ZWAVE,
)
from custom_components.radio_guard.coordinator import (
    CoordinatorReplaced,
    RadioGuardCoordinator,
    RestoreBlocked,
    normalize_identity,
)

ENTRY_ID = "rg_test_entry"
IEEE_A = "00124b0024c2f0a1"
IEEE_B = "00124b00ffffffff"
SIDS = (SOURCE_Z2M, SOURCE_ZHA, SOURCE_ZWAVE)
SENSOR_KEYS = ("last_backup", "next_backup", "stored_backups", "identity")
BINARY_KEYS = ("backup_stale", "coordinator_replaced", "source_loaded", "backup_running")

# --------------------------------------------------------------- fake source


class FakeSource(BackupSource):
    """Scriptable stand-in for a Z2M / ZHA / Z-Wave JS source."""

    def __init__(self, hass: HomeAssistant | None, sid: str) -> None:
        super().__init__(hass)  # type: ignore[arg-type]
        self.id = sid
        self.name = SOURCE_NAMES[sid]
        self.is_available = True
        self.identity: dict[str, Any] = {"ieee": IEEE_A, "device_count": 5}
        self.backup_meta: dict[str, Any] = {"ieee": IEEE_A, "device_count": 5}
        self.backup_error: Exception | None = None
        self.backup_gate: asyncio.Event | None = None
        self.check_ok = True
        self.check_reasons: list[str] = []
        self.check_warnings: list[str] = []
        self.restore_error: Exception | None = None
        self.restore_gate: asyncio.Event | None = None
        self.calls: list[str] = []
        self.restored: list[tuple[bytes, dict[str, Any]]] = []
        self.backups_taken = 0
        self.setup_calls = 0
        self.teardown_calls = 0

    async def async_setup(self) -> None:
        self.setup_calls += 1

    async def async_teardown(self) -> None:
        self.teardown_calls += 1

    def available(self) -> bool:
        return self.is_available

    async def async_identity(self) -> dict[str, Any]:
        if not self.is_available:
            raise SourceUnavailable(self.name)
        return dict(self.identity)

    async def async_backup(self) -> BackupPayload:
        self.calls.append("backup")
        if self.backup_gate is not None:
            await self.backup_gate.wait()
        if self.backup_error is not None:
            raise self.backup_error
        self.backups_taken += 1
        return BackupPayload(
            data=f"{self.id}-{self.backups_taken}".encode(), ext=".bin", meta=dict(self.backup_meta)
        )

    async def async_restore_check(self, data: bytes, meta: dict[str, Any]) -> RestoreCheck:
        self.calls.append("restore_check")
        return RestoreCheck(
            ok=self.check_ok,
            reasons=list(self.check_reasons),
            warnings=list(self.check_warnings),
            current=dict(self.identity),
            backup=dict(meta),
        )

    async def async_restore(self, data: bytes, meta: dict[str, Any]) -> None:
        self.calls.append("restore")
        if self.restore_gate is not None:
            await self.restore_gate.wait()
        if self.restore_error is not None:
            raise self.restore_error
        self.restored.append((data, meta))
        # The new stick now carries the restored network.
        if meta.get("ieee"):
            self.identity = {**self.identity, "ieee": meta["ieee"]}


@pytest.fixture
def fakes(hass: HomeAssistant) -> dict[str, FakeSource]:
    """Patch the three source classes; every coordinator build gets the same fakes."""
    fk = {sid: FakeSource(hass, sid) for sid in SIDS}
    with (
        patch(
            "custom_components.radio_guard.coordinator.Zigbee2MQTTSource",
            lambda hass, topic: fk[SOURCE_Z2M],
        ),
        patch("custom_components.radio_guard.coordinator.ZHASource", lambda hass: fk[SOURCE_ZHA]),
        patch("custom_components.radio_guard.coordinator.ZWaveJSSource", lambda hass: fk[SOURCE_ZWAVE]),
    ):
        yield fk


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> RadioGuardCoordinator:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry.runtime_data


@pytest.fixture
async def coord(
    hass: HomeAssistant, fakes: dict[str, FakeSource], mock_config_entry: MockConfigEntry
) -> RadioGuardCoordinator:
    return await setup_entry(hass, mock_config_entry)


def seed_backup(
    config_dir: Path,
    sid: str,
    created: datetime,
    meta: dict[str, Any] | None = None,
    *,
    label: str | None = None,
    ext: str = ".bin",
) -> Path:
    """Drop a backup + sidecar on disk exactly as BackupStore.write would."""
    d = config_dir / "radio_guard" / sid
    d.mkdir(parents=True, exist_ok=True)
    stem = created.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    if label:
        stem = f"{label}_{stem}"
    path = d / f"{stem}{ext}"
    data = f"seed-{sid}-{stem}".encode()
    path.write_bytes(data)
    sidecar = {
        **(meta or {}),
        "label": label,
        "source": sid,
        "created": created.astimezone(UTC).isoformat(),
        "size": len(data),
        "file": path.name,
    }
    (d / f"{stem}.meta.json").write_text(json.dumps(sidecar))
    return path


def ago(**kw: float) -> datetime:
    return dt_util.utcnow() - timedelta(**kw)


def entity_id(hass: HomeAssistant, platform: str, sid: str, key: str) -> str:
    eid = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{ENTRY_ID}_{sid}_{key}")
    assert eid, f"no {platform} entity for {sid}/{key}"
    return eid


def issue(hass: HomeAssistant, issue_id: str) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id)


def notifications(hass: HomeAssistant) -> dict[str, Any]:
    return persistent_notification._async_get_or_create_notifications(hass)


def files_in(config_dir: Path, sid: str) -> list[str]:
    d = config_dir / "radio_guard" / sid
    if not d.exists():
        return []
    return sorted(p.name for p in d.iterdir() if not p.name.endswith(".meta.json"))


async def advance(hass: HomeAssistant, seconds: float) -> None:
    """Fire timers due within ``seconds`` and wait for background tasks (first backup is one)."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done(wait_background_tasks=True)


async def refresh(hass: HomeAssistant, coord: RadioGuardCoordinator) -> None:
    await coord.async_refresh()
    await hass.async_block_till_done()


def capture(hass: HomeAssistant, event_type: str) -> list[Any]:
    events: list[Any] = []
    hass.bus.async_listen(event_type, events.append)
    return events


# ===================================================================== setup


async def test_setup_creates_entities_per_source_device(
    hass: HomeAssistant, coord: RadioGuardCoordinator, fakes: dict[str, FakeSource]
) -> None:
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    devices = dr.async_entries_for_config_entry(dev_reg, ENTRY_ID)
    assert sorted(d.name for d in devices) == sorted(f"{SOURCE_NAMES[s]} backups" for s in SIDS)

    entries = er.async_entries_for_config_entry(ent_reg, ENTRY_ID)
    # 4 sensors + 4 binary sensors + 1 button per source
    assert len(entries) == 9 * len(SIDS)

    for sid in SIDS:
        assert fakes[sid].setup_calls == 1
        device = dev_reg.async_get_device(identifiers={(DOMAIN, f"{ENTRY_ID}_{sid}")})
        assert device is not None
        for key in SENSOR_KEYS:
            eid = entity_id(hass, "sensor", sid, key)
            assert ent_reg.async_get(eid).device_id == device.id
        for key in BINARY_KEYS:
            assert ent_reg.async_get(entity_id(hass, "binary_sensor", sid, key)).device_id == device.id
        assert entity_id(hass, "button", sid, "backup_now")

        # source_loaded is a disabled-by-default diagnostic
        loaded = ent_reg.async_get(entity_id(hass, "binary_sensor", sid, "source_loaded"))
        assert loaded.disabled_by is er.RegistryEntryDisabler.INTEGRATION

        assert hass.states.get(entity_id(hass, "sensor", sid, "last_backup")).state == STATE_UNKNOWN
        assert hass.states.get(entity_id(hass, "sensor", sid, "stored_backups")).state == "0"
        assert hass.states.get(entity_id(hass, "sensor", sid, "identity")).state == IEEE_A
        # never backed up -> problem
        assert hass.states.get(entity_id(hass, "binary_sensor", sid, "backup_stale")).state == STATE_ON

    for svc in ("backup_now", "list_backups", "restore"):
        assert hass.services.has_service(DOMAIN, svc)


async def test_only_configured_sources_are_built(
    hass: HomeAssistant, fakes: dict[str, FakeSource], entry_data: dict[str, Any]
) -> None:
    entry_data[CONF_SOURCES] = [SOURCE_ZWAVE]
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data, entry_id=ENTRY_ID, unique_id=DOMAIN)
    coord = await setup_entry(hass, entry)
    assert list(coord.states) == [SOURCE_ZWAVE]
    assert len(dr.async_entries_for_config_entry(dr.async_get(hass), ENTRY_ID)) == 1
    assert fakes[SOURCE_Z2M].setup_calls == 0


async def test_unload_cleans_up(
    hass: HomeAssistant,
    coord: RadioGuardCoordinator,
    fakes: dict[str, FakeSource],
    mock_config_entry: MockConfigEntry,
    config_dir: Path,
) -> None:
    eid = entity_id(hass, "sensor", SOURCE_Z2M, "last_backup")
    unsubs = list(coord._unsubs)
    assert unsubs  # nightly schedule at least

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
    assert coord._unsubs == []
    for sid in SIDS:
        assert fakes[sid].teardown_calls == 1
    assert hass.states.get(eid).state == STATE_UNAVAILABLE
    for svc in ("backup_now", "list_backups", "restore"):
        assert not hass.services.has_service(DOMAIN, svc)

    # The nightly timer is gone: firing the schedule writes nothing.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(days=1, minutes=1))
    await hass.async_block_till_done()
    assert all(f.backups_taken == 0 for f in fakes.values())
    assert files_in(config_dir, SOURCE_Z2M) == []


async def test_orphan_device_removed_when_source_dropped(
    hass: HomeAssistant, fakes: dict[str, FakeSource], mock_config_entry: MockConfigEntry
) -> None:
    await setup_entry(hass, mock_config_entry)
    dev_reg = dr.async_get(hass)
    assert dev_reg.async_get_device(identifiers={(DOMAIN, f"{ENTRY_ID}_{SOURCE_ZHA}")})

    hass.config_entries.async_update_entry(mock_config_entry, options={CONF_SOURCES: [SOURCE_Z2M]})
    await hass.async_block_till_done()  # update listener reloads

    assert list(mock_config_entry.runtime_data.states) == [SOURCE_Z2M]
    assert dev_reg.async_get_device(identifiers={(DOMAIN, f"{ENTRY_ID}_{SOURCE_ZHA}")}) is None
    assert dev_reg.async_get_device(identifiers={(DOMAIN, f"{ENTRY_ID}_{SOURCE_ZWAVE}")}) is None
    assert dev_reg.async_get_device(identifiers={(DOMAIN, f"{ENTRY_ID}_{SOURCE_Z2M}")})


async def test_source_setup_failure_does_not_block_entry(
    hass: HomeAssistant, fakes: dict[str, FakeSource], mock_config_entry: MockConfigEntry
) -> None:
    async def boom() -> None:
        raise RuntimeError("broker down")

    fakes[SOURCE_Z2M].async_setup = boom  # type: ignore[method-assign]
    coord = await setup_entry(hass, mock_config_entry)
    assert SOURCE_Z2M in coord.states


# ============================================================== first backup


@pytest.fixture
def first_backup_entry(entry_data: dict[str, Any]) -> MockConfigEntry:
    entry_data[CONF_FIRST_BACKUP] = True
    return MockConfigEntry(
        domain=DOMAIN, title="Radio Guard", data=entry_data, entry_id=ENTRY_ID, unique_id=DOMAIN
    )


async def test_first_backup_runs_45s_after_setup_with_notification(
    hass: HomeAssistant,
    fakes: dict[str, FakeSource],
    first_backup_entry: MockConfigEntry,
    config_dir: Path,
) -> None:
    fakes[SOURCE_ZHA].is_available = False
    fakes[SOURCE_ZWAVE].backup_error = RuntimeError("controller busy")
    fakes[SOURCE_Z2M].backup_meta = {"ieee": IEEE_A, "device_count": 12}
    await setup_entry(hass, first_backup_entry)

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=40))
    await hass.async_block_till_done()  # the first-backup task is still sleeping
    assert fakes[SOURCE_Z2M].backups_taken == 0
    assert NOTIFICATION_FIRST_BACKUP not in notifications(hass)

    await advance(hass, 46)

    assert fakes[SOURCE_Z2M].backups_taken == 1
    [z2m_file] = files_in(config_dir, SOURCE_Z2M)
    note = notifications(hass)[NOTIFICATION_FIRST_BACKUP]
    msg = note["message"]
    assert str(config_dir / "radio_guard" / SOURCE_Z2M / z2m_file) in msg
    assert "12 devices" in msg
    assert IEEE_A in msg
    assert "**ZHA**: skipped, integration not loaded yet" in msg
    assert "**Z-Wave JS**: failed (controller busy)" in msg
    assert "nightly at 03:17" in msg
    assert "/config/radio_guard/" in msg
    assert first_backup_entry.options[CONF_FIRST_BACKUP_DONE] is True

    # The options write reloads the entry; the reloaded entry does not repeat it.
    assert first_backup_entry.state is ConfigEntryState.LOADED
    await advance(hass, 120)
    assert fakes[SOURCE_Z2M].backups_taken == 1


async def test_first_backup_disabled(
    hass: HomeAssistant, coord: RadioGuardCoordinator, fakes: dict[str, FakeSource]
) -> None:
    await advance(hass, 120)
    assert all(f.backups_taken == 0 for f in fakes.values())
    assert NOTIFICATION_FIRST_BACKUP not in notifications(hass)


async def test_first_backup_flag_preserved_across_options_save(
    hass: HomeAssistant, fakes: dict[str, FakeSource], first_backup_entry: MockConfigEntry
) -> None:
    await setup_entry(hass, first_backup_entry)
    await advance(hass, 46)
    assert fakes[SOURCE_Z2M].backups_taken == 1
    assert first_backup_entry.options[CONF_FIRST_BACKUP_DONE] is True

    # Save options through the real options flow (sectioned schema).
    result = await hass.config_entries.options.async_init(first_backup_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SOURCES: [SOURCE_Z2M, SOURCE_ZHA],
            "schedule": {"backup_time": "04:30:00", "retention": 5, "stale_hours": 48},
            "zigbee2mqtt": {"z2m_base_topic": "zigbee2mqtt"},
            "storage": {"backup_dir": "radio_guard"},
            "heartbeat": {"heartbeat_enabled": False},
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    opts = first_backup_entry.options
    assert opts[CONF_FIRST_BACKUP_DONE] is True
    assert (opts[CONF_BACKUP_HOUR], opts[CONF_BACKUP_MINUTE]) == (4, 30)
    coord = first_backup_entry.runtime_data
    assert list(coord.states) == [SOURCE_Z2M, SOURCE_ZHA]
    assert coord.schedule == (4, 30)

    # Reloaded with the flag intact: no second "first" backup.
    await advance(hass, 120)
    assert fakes[SOURCE_Z2M].backups_taken == 1


# ================================================================== schedule


def _local(hour: int, minute: int, second: int = 0) -> datetime:
    """A fixed local wall-clock time (independent of a frozen clock)."""
    return datetime(2026, 11, 10, hour, minute, second, tzinfo=dt_util.get_default_time_zone())


async def test_scheduled_backup_runs_at_configured_time(
    hass: HomeAssistant,
    fakes: dict[str, FakeSource],
    mock_config_entry: MockConfigEntry,
    config_dir: Path,
    freezer,
) -> None:
    freezer.move_to(_local(3, 16, 0))
    coord = await setup_entry(hass, mock_config_entry)
    assert coord.schedule == (3, 17)
    assert (coord.next_backup.hour, coord.next_backup.minute) == (3, 17)
    events = capture(hass, EVENT_BACKUP_COMPLETE)

    async_fire_time_changed(hass, _local(3, 16, 30))
    await hass.async_block_till_done()
    assert all(f.backups_taken == 0 for f in fakes.values())

    async_fire_time_changed(hass, _local(3, 17, 0))
    await hass.async_block_till_done(wait_background_tasks=True)
    for sid in SIDS:
        assert fakes[sid].backups_taken == 1, sid
        assert len(files_in(config_dir, sid)) == 1
        assert coord.states[sid].latest is not None
    assert sorted(e.data["source"] for e in events) == sorted(SIDS)


async def test_scheduled_backup_uses_options_time(
    hass: HomeAssistant, fakes: dict[str, FakeSource], entry_data: dict[str, Any], freezer
) -> None:
    freezer.move_to(_local(22, 0, 0))
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=entry_data,
        options={CONF_BACKUP_HOUR: 22, CONF_BACKUP_MINUTE: 45},
        entry_id=ENTRY_ID,
        unique_id=DOMAIN,
    )
    coord = await setup_entry(hass, entry)
    # options win over entry.data (03:17)
    assert coord.schedule == (22, 45)
    async_fire_time_changed(hass, _local(22, 44, 30))
    await hass.async_block_till_done()
    assert fakes[SOURCE_Z2M].backups_taken == 0
    async_fire_time_changed(hass, _local(22, 45, 0))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert fakes[SOURCE_Z2M].backups_taken == 1


async def test_scheduled_backup_skipped_while_coordinator_replaced(
    hass: HomeAssistant,
    fakes: dict[str, FakeSource],
    mock_config_entry: MockConfigEntry,
    config_dir: Path,
) -> None:
    seed_backup(config_dir, SOURCE_ZHA, ago(hours=2), {"ieee": IEEE_A, "device_count": 5})
    fakes[SOURCE_ZHA].identity = {"ieee": IEEE_B, "device_count": 1}
    coord = await setup_entry(hass, mock_config_entry)
    assert coord.states[SOURCE_ZHA].coordinator_replaced

    results = await coord.async_backup_all(scheduled=True)

    assert isinstance(results[SOURCE_ZHA], CoordinatorReplaced)
    assert fakes[SOURCE_ZHA].backups_taken == 0
    assert len(files_in(config_dir, SOURCE_ZHA)) == 1  # old network backup still "latest"
    assert fakes[SOURCE_Z2M].backups_taken == 1
    assert fakes[SOURCE_ZWAVE].backups_taken == 1


async def test_nightly_timer_skips_replaced_but_backs_up_others(
    hass: HomeAssistant,
    fakes: dict[str, FakeSource],
    mock_config_entry: MockConfigEntry,
    config_dir: Path,
    freezer,
) -> None:
    freezer.move_to(_local(3, 16, 0))
    seed_backup(config_dir, SOURCE_ZHA, ago(hours=2), {"ieee": IEEE_A})
    fakes[SOURCE_ZHA].identity = {"ieee": IEEE_B}
    await setup_entry(hass, mock_config_entry)

    async_fire_time_changed(hass, _local(3, 17, 0))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert fakes[SOURCE_ZHA].backups_taken == 0
    assert fakes[SOURCE_ZWAVE].backups_taken == 1


async def test_scheduled_skip_never_writes_backup_of_replacement_stick(
    hass: HomeAssistant,
    fakes: dict[str, FakeSource],
    mock_config_entry: MockConfigEntry,
    config_dir: Path,
) -> None:
    """The replaced source is not backed up, so the old network's backup stays latest."""
    seed_backup(config_dir, SOURCE_Z2M, ago(hours=2), {"ieee": IEEE_A})
    fakes[SOURCE_Z2M].identity = {"ieee": IEEE_B}
    coord = await setup_entry(hass, mock_config_entry)

    results = await coord.async_backup_all(scheduled=True)
    assert isinstance(results[SOURCE_Z2M], CoordinatorReplaced)
    assert fakes[SOURCE_Z2M].backups_taken == 0
    assert coord.states[SOURCE_Z2M].latest.meta["ieee"] == IEEE_A


async def test_manual_backup_all_accepts_replacement(
    hass: HomeAssistant,
    fakes: dict[str, FakeSource],
    mock_config_entry: MockConfigEntry,
    config_dir: Path,
) -> None:
    """Unscheduled (Back up now) backups do run on a replaced coordinator and clear the flag."""
    seed_backup(config_dir, SOURCE_Z2M, ago(hours=2), {"ieee": IEEE_A})
    fakes[SOURCE_Z2M].identity = {"ieee": IEEE_B}
    fakes[SOURCE_Z2M].backup_meta = {"ieee": IEEE_B}
    coord = await setup_entry(hass, mock_config_entry)
    assert coord.states[SOURCE_Z2M].coordinator_replaced

    results = await coord.async_backup_all()
    await hass.async_block_till_done()
    assert not isinstance(results[SOURCE_Z2M], Exception)
    assert not coord.states[SOURCE_Z2M].coordinator_replaced
    assert issue(hass, f"coordinator_replaced_{SOURCE_Z2M}") is None


# ==================================================================== issues


async def test_backup_never_issue_lifecycle(
    hass: HomeAssistant, coord: RadioGuardCoordinator, fakes: dict[str, FakeSource]
) -> None:
    iss = issue(hass, f"backup_never_{SOURCE_Z2M}")
    assert iss is not None
    assert iss.translation_key == "backup_never"
    assert iss.is_fixable
    assert iss.data == {"source": SOURCE_Z2M, "action": "backup"}

    await coord.async_backup(SOURCE_Z2M)
    await refresh(hass, coord)
    assert issue(hass, f"backup_never_{SOURCE_Z2M}") is None
    assert issue(hass, f"backup_never_{SOURCE_ZHA}") is not None


async def test_no_never_issue_for_unavailable_source(
    hass: HomeAssistant, fakes: dict[str, FakeSource], mock_config_entry: MockConfigEntry
) -> None:
    fakes[SOURCE_ZHA].is_available = False
    coord = await setup_entry(hass, mock_config_entry)
    assert issue(hass, f"backup_never_{SOURCE_ZHA}") is None
    assert coord.states[SOURCE_ZHA].identity == {}
    # problem sensor stays off for a source that is not installed
    assert hass.states.get(entity_id(hass, "binary_sensor", SOURCE_ZHA, "backup_stale")).state == STATE_OFF

    # Source appears later -> issue appears on the next poll
    fakes[SOURCE_ZHA].is_available = True
    await refresh(hass, coord)
    assert issue(hass, f"backup_never_{SOURCE_ZHA}") is not None


async def test_backup_stale_issue_lifecycle(
    hass: HomeAssistant,
    fakes: dict[str, FakeSource],
    mock_config_entry: MockConfigEntry,
    config_dir: Path,
) -> None:
    seed_backup(config_dir, SOURCE_Z2M, ago(hours=40), {"ieee": IEEE_A})  # stale_hours = 36
    seed_backup(config_dir, SOURCE_ZHA, ago(hours=10), {"ieee": IEEE_A})
    coord = await setup_entry(hass, mock_config_entry)

    stale = issue(hass, f"backup_stale_{SOURCE_Z2M}")
    assert stale is not None
    assert stale.translation_placeholders == {"source": "Zigbee2MQTT", "age": "40"}
    assert issue(hass, f"backup_never_{SOURCE_Z2M}") is None
    assert issue(hass, f"backup_stale_{SOURCE_ZHA}") is None
    assert hass.states.get(entity_id(hass, "binary_sensor", SOURCE_Z2M, "backup_stale")).state == STATE_ON
    assert hass.states.get(entity_id(hass, "binary_sensor", SOURCE_ZHA, "backup_stale")).state == STATE_OFF

    await coord.async_backup(SOURCE_Z2M)
    await refresh(hass, coord)
    assert issue(hass, f"backup_stale_{SOURCE_Z2M}") is None
    assert hass.states.get(entity_id(hass, "binary_sensor", SOURCE_Z2M, "backup_stale")).state == STATE_OFF


async def test_backup_failed_issue_lifecycle(
    hass: HomeAssistant, coord: RadioGuardCoordinator, fakes: dict[str, FakeSource]
) -> None:
    fakes[SOURCE_Z2M].backup_error = TimeoutError()
    with pytest.raises(TimeoutError):
        await coord.async_backup(SOURCE_Z2M)
    await hass.async_block_till_done()

    failed = issue(hass, f"backup_failed_{SOURCE_Z2M}")
    assert failed is not None
    assert failed.severity is ir.IssueSeverity.ERROR
    # empty str(err) falls back to the exception type name
    assert failed.translation_placeholders == {"source": "Zigbee2MQTT", "error": "TimeoutError"}
    st = coord.states[SOURCE_Z2M]
    assert st.last_error == "TimeoutError"
    assert st.last_attempt is not None
    state = hass.states.get(entity_id(hass, "binary_sensor", SOURCE_Z2M, "backup_stale"))
    assert state.attributes["last_error"] == "TimeoutError"

    fakes[SOURCE_Z2M].backup_error = None
    await coord.async_backup(SOURCE_Z2M)
    await hass.async_block_till_done()
    assert issue(hass, f"backup_failed_{SOURCE_Z2M}") is None
    assert st.last_error is None


async def test_unavailable_source_is_not_a_failure(
    hass: HomeAssistant, coord: RadioGuardCoordinator, fakes: dict[str, FakeSource]
) -> None:
    fakes[SOURCE_Z2M].backup_error = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        await coord.async_backup(SOURCE_Z2M)
    assert issue(hass, f"backup_failed_{SOURCE_Z2M}") is not None

    fakes[SOURCE_Z2M].is_available = False
    with pytest.raises(SourceUnavailable):
        await coord.async_backup(SOURCE_Z2M)
    # unavailable clears a previous failure instead of piling on
    assert issue(hass, f"backup_failed_{SOURCE_Z2M}") is None
    assert coord.states[SOURCE_Z2M].last_error is None
    assert fakes[SOURCE_Z2M].calls == ["backup"]  # second call never reached the source


async def test_backup_complete_event_and_sensors(
    hass: HomeAssistant, coord: RadioGuardCoordinator, fakes: dict[str, FakeSource]
) -> None:
    events = capture(hass, EVENT_BACKUP_COMPLETE)
    rec = await coord.async_backup(SOURCE_ZWAVE)
    await coord.async_request_refresh()
    await hass.async_block_till_done()

    assert events[0].data == {"source": SOURCE_ZWAVE, "file": str(rec.path), "size": rec.size}
    assert rec.path.stat().st_mode & 0o777 == 0o600
    last = hass.states.get(entity_id(hass, "sensor", SOURCE_ZWAVE, "last_backup"))
    assert last.attributes["file"] == str(rec.path)
    assert hass.states.get(entity_id(hass, "sensor", SOURCE_ZWAVE, "stored_backups")).state == "1"


async def test_last_backup_file_attribute_is_full_path(
    hass: HomeAssistant, coord: RadioGuardCoordinator, fakes: dict[str, FakeSource]
) -> None:
    rec = await coord.async_backup(SOURCE_Z2M)
    await refresh(hass, coord)
    last = hass.states.get(entity_id(hass, "sensor", SOURCE_Z2M, "last_backup"))
    assert last.attributes["file"] == str(rec.path)


# ==================================================== coordinator replaced


@pytest.mark.parametrize(
    ("live", "backed_up", "replaced"),
    [
        (IEEE_A, IEEE_A, False),
        ("0x00124B0024C2F0A1", "00:12:4b:00:24:c2:f0:a1", False),
        (IEEE_B, IEEE_A, True),
        (None, IEEE_A, False),  # identity unreadable: never claim replaced
    ],
)
async def test_coordinator_replaced_detection(
    hass: HomeAssistant,
    fakes: dict[str, FakeSource],
    mock_config_entry: MockConfigEntry,
    config_dir: Path,
    live: str | None,
    backed_up: str,
    replaced: bool,
) -> None:
    seed_backup(config_dir, SOURCE_Z2M, ago(hours=1), {"ieee": backed_up})
    fakes[SOURCE_Z2M].identity = {"ieee": live} if live else {}
    coord = await setup_entry(hass, mock_config_entry)

    assert coord.states[SOURCE_Z2M].coordinator_replaced is replaced
    iss = issue(hass, f"coordinator_replaced_{SOURCE_Z2M}")
    assert (iss is not None) is replaced
    state = hass.states.get(entity_id(hass, "binary_sensor", SOURCE_Z2M, "coordinator_replaced"))
    assert state.state == (STATE_ON if replaced else STATE_OFF)
    if replaced:
        assert iss.data == {"source": SOURCE_Z2M, "action": "restore"}
        assert iss.translation_placeholders == {"source": "Zigbee2MQTT", "live": IEEE_B, "backup": IEEE_A}
        ident = hass.states.get(entity_id(hass, "sensor", SOURCE_Z2M, "identity"))
        assert ident.attributes["matches_latest_backup"] is False


async def test_coordinator_replaced_zwave_home_id(
    hass: HomeAssistant,
    fakes: dict[str, FakeSource],
    mock_config_entry: MockConfigEntry,
    config_dir: Path,
) -> None:
    seed_backup(config_dir, SOURCE_ZWAVE, ago(hours=1), {"home_id": 3735928559, "home_id_hex": "0xdeadbeef"})
    fakes[SOURCE_ZWAVE].identity = {"home_id": 3735928559}
    coord = await setup_entry(hass, mock_config_entry)
    assert not coord.states[SOURCE_ZWAVE].coordinator_replaced

    fakes[SOURCE_ZWAVE].identity = {"home_id": 0x0BADF00D}
    await refresh(hass, coord)
    assert coord.states[SOURCE_ZWAVE].coordinator_replaced
    assert issue(hass, f"coordinator_replaced_{SOURCE_ZWAVE}") is not None


def test_normalize_identity() -> None:
    assert normalize_identity("0x00124B0024C2F0A1") == IEEE_A
    assert normalize_identity("00:12:4b:00:24:c2:f0:a1") == IEEE_A
    assert normalize_identity(3735928559) == "deadbeef"
    assert normalize_identity("3735928559") == "deadbeef"
    assert normalize_identity(None) is None
    assert normalize_identity("") is None
    # 16 decimal digits is an IEEE, not a home id
    assert normalize_identity("0012345678901234") == "0012345678901234"


# ============================================================ running flags


async def test_running_flag_and_concurrent_backup_rejected(
    hass: HomeAssistant, coord: RadioGuardCoordinator, fakes: dict[str, FakeSource]
) -> None:
    gate = asyncio.Event()
    fakes[SOURCE_Z2M].backup_gate = gate
    running = entity_id(hass, "binary_sensor", SOURCE_Z2M, "backup_running")

    task = hass.async_create_task(coord.async_backup(SOURCE_Z2M))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert coord.states[SOURCE_Z2M].running
    assert hass.states.get(running).state == STATE_ON

    with pytest.raises(RuntimeError, match="already running"):
        await coord.async_backup(SOURCE_Z2M)
    # a different source is independent
    await coord.async_backup(SOURCE_ZHA)

    gate.set()
    await task
    await hass.async_block_till_done()
    assert not coord.states[SOURCE_Z2M].running
    assert hass.states.get(running).state == STATE_OFF


async def test_running_flag_cleared_on_failure(
    hass: HomeAssistant, coord: RadioGuardCoordinator, fakes: dict[str, FakeSource]
) -> None:
    fakes[SOURCE_Z2M].backup_error = RuntimeError("nope")
    with pytest.raises(RuntimeError):
        await coord.async_backup(SOURCE_Z2M)
    assert not coord.states[SOURCE_Z2M].running


async def test_restoring_flag_and_concurrent_restore_rejected(
    hass: HomeAssistant,
    fakes: dict[str, FakeSource],
    mock_config_entry: MockConfigEntry,
    config_dir: Path,
) -> None:
    seed_backup(config_dir, SOURCE_Z2M, ago(hours=1), {"ieee": IEEE_A})
    fakes[SOURCE_Z2M].identity = {"ieee": IEEE_B, "device_count": 1}
    coord = await setup_entry(hass, mock_config_entry)
    gate = asyncio.Event()
    fakes[SOURCE_Z2M].restore_gate = gate

    task = hass.async_create_task(coord.async_restore(SOURCE_Z2M, None, False))
    for _ in range(10):
        await asyncio.sleep(0)
    st = coord.states[SOURCE_Z2M]
    assert st.restoring
    assert hass.states.get(entity_id(hass, "binary_sensor", SOURCE_Z2M, "backup_running")).state == STATE_ON
    # while restoring, a poll must not (re)raise the replaced repair
    await coord.async_refresh()  # no block_till_done: the restore task is parked on the gate
    assert issue(hass, f"coordinator_replaced_{SOURCE_Z2M}") is None

    with pytest.raises(RuntimeError, match="already running"):
        await coord.async_restore(SOURCE_Z2M, None, False)

    gate.set()
    await task
    await hass.async_block_till_done()
    assert not st.restoring


async def test_restoring_flag_cleared_on_failure(
    hass: HomeAssistant,
    fakes: dict[str, FakeSource],
    mock_config_entry: MockConfigEntry,
    config_dir: Path,
) -> None:
    seed_backup(config_dir, SOURCE_Z2M, ago(hours=1), {"ieee": IEEE_A})
    coord = await setup_entry(hass, mock_config_entry)
    fakes[SOURCE_Z2M].restore_error = RuntimeError("serial gone")
    with pytest.raises(RuntimeError, match="serial gone"):
        await coord.async_restore(SOURCE_Z2M, None, False)
    assert not coord.states[SOURCE_Z2M].restoring


# ================================================================== restore


@pytest.fixture
async def replaced(
    hass: HomeAssistant,
    fakes: dict[str, FakeSource],
    mock_config_entry: MockConfigEntry,
    config_dir: Path,
) -> tuple[RadioGuardCoordinator, Path]:
    """Z2M with one good 1 h old backup of network A, live stick B with its own small network."""
    path = seed_backup(config_dir, SOURCE_Z2M, ago(hours=1), {"ieee": IEEE_A, "device_count": 20})
    fakes[SOURCE_Z2M].identity = {"ieee": IEEE_B, "device_count": 3}
    fakes[SOURCE_Z2M].backup_meta = {"ieee": IEEE_B, "device_count": 3}
    coord = await setup_entry(hass, mock_config_entry)
    assert coord.states[SOURCE_Z2M].coordinator_replaced
    return coord, path


async def test_restore_happy_path_order_snapshot_and_event(
    hass: HomeAssistant,
    replaced: tuple[RadioGuardCoordinator, Path],
    fakes: dict[str, FakeSource],
    config_dir: Path,
) -> None:
    coord, path = replaced
    fake = fakes[SOURCE_Z2M]
    events = capture(hass, EVENT_RESTORE_COMPLETE)
    assert issue(hass, f"coordinator_replaced_{SOURCE_Z2M}") is not None

    record = await coord.async_restore(SOURCE_Z2M, None, False)
    await hass.async_block_till_done()

    assert record.path == path
    # pre-flight first, then target snapshot, then the restore itself
    assert fake.calls == ["restore_check", "backup", "restore"]
    data, meta = fake.restored[0]
    assert data == path.read_bytes()
    assert meta["ieee"] == IEEE_A
    assert meta["allow_ieee_overwrite"] is False

    names = files_in(config_dir, SOURCE_Z2M)
    snaps = [n for n in names if n.startswith(PRE_RESTORE_PREFIX)]
    assert len(snaps) == 1
    st = coord.states[SOURCE_Z2M]
    # the snapshot is listed but never "latest"
    assert any(r.name == snaps[0] for r in st.records)
    assert st.latest.path == path
    assert st.store.latest().path == path
    assert st.store.latest(include_labelled=True).name == snaps[0]
    last = hass.states.get(entity_id(hass, "sensor", SOURCE_Z2M, "last_backup"))
    assert last.attributes["file"] == str(path)

    assert events[0].data == {"source": SOURCE_Z2M, "file": str(path)}
    assert issue(hass, f"coordinator_replaced_{SOURCE_Z2M}") is None
    assert not st.coordinator_replaced


@pytest.mark.parametrize(("target_count", "snapshot"), [(None, True), (0, False), (1, False), (2, True)])
async def test_pre_restore_snapshot_only_when_target_has_network(
    hass: HomeAssistant,
    fakes: dict[str, FakeSource],
    mock_config_entry: MockConfigEntry,
    config_dir: Path,
    target_count: int | None,
    snapshot: bool,
) -> None:
    seed_backup(config_dir, SOURCE_Z2M, ago(hours=1), {"ieee": IEEE_A})
    fakes[SOURCE_Z2M].identity = {"ieee": IEEE_B, "device_count": target_count}
    coord = await setup_entry(hass, mock_config_entry)
    await coord.async_restore(SOURCE_Z2M, None, False)
    assert ("backup" in fakes[SOURCE_Z2M].calls) is snapshot
    assert any(n.startswith(PRE_RESTORE_PREFIX) for n in files_in(config_dir, SOURCE_Z2M)) is snapshot


async def test_no_snapshot_when_target_unavailable(
    hass: HomeAssistant,
    fakes: dict[str, FakeSource],
    mock_config_entry: MockConfigEntry,
    config_dir: Path,
) -> None:
    seed_backup(config_dir, SOURCE_Z2M, ago(hours=1), {"ieee": IEEE_A})
    fakes[SOURCE_Z2M].is_available = False
    coord = await setup_entry(hass, mock_config_entry)
    await coord.async_restore(SOURCE_Z2M, None, False)
    assert fakes[SOURCE_Z2M].calls == ["restore_check", "restore"]


async def test_snapshot_failure_does_not_block_restore(
    hass: HomeAssistant, replaced: tuple[RadioGuardCoordinator, Path], fakes: dict[str, FakeSource]
) -> None:
    coord, path = replaced
    fakes[SOURCE_Z2M].backup_error = RuntimeError("target unreadable")
    record = await coord.async_restore(SOURCE_Z2M, None, False)
    assert record.path == path
    assert fakes[SOURCE_Z2M].calls == ["restore_check", "backup", "restore"]


async def test_restore_blocked_by_preflight_and_force_overrides(
    hass: HomeAssistant, replaced: tuple[RadioGuardCoordinator, Path], fakes: dict[str, FakeSource]
) -> None:
    coord, path = replaced
    fake = fakes[SOURCE_Z2M]
    fake.check_ok = False
    fake.check_reasons = ["Target coordinator already runs network 00124b00ffffffff"]

    with pytest.raises(RestoreBlocked) as exc:
        await coord.async_restore(SOURCE_Z2M, None, False)
    assert exc.value.reasons == fake.check_reasons
    assert fake.calls == ["restore_check"]  # nothing written, no snapshot, no restore
    assert not coord.states[SOURCE_Z2M].restoring

    fake.calls.clear()
    record = await coord.async_restore(SOURCE_Z2M, None, True)
    assert record.path == path
    assert fake.calls == ["restore_check", "backup", "restore"]
    assert fake.restored[0][1]["allow_ieee_overwrite"] is True


async def test_identity_match_block_reported_by_source(
    hass: HomeAssistant,
    fakes: dict[str, FakeSource],
    mock_config_entry: MockConfigEntry,
    config_dir: Path,
) -> None:
    """The identity-match rule lives in each source; the coordinator surfaces it unchanged."""
    seed_backup(config_dir, SOURCE_ZWAVE, ago(hours=1), {"home_id": 1})
    coord = await setup_entry(hass, mock_config_entry)
    fakes[SOURCE_ZWAVE].check_ok = False
    fakes[SOURCE_ZWAVE].check_reasons = ["Security keys differ from the backup"]
    record, check = await coord.async_restore_check(SOURCE_ZWAVE, None)
    assert not check.ok
    assert check.reasons == ["Security keys differ from the backup"]
    assert fakes[SOURCE_ZWAVE].restored == []


@pytest.mark.parametrize(
    ("age_days", "options", "ok"),
    [
        (6.5, {}, True),
        (8, {}, False),  # default limit 7 d
        (8, {CONF_MAX_RESTORE_AGE_DAYS: 30}, True),
        (2, {CONF_MAX_RESTORE_AGE_DAYS: 1}, False),
    ],
)
async def test_max_restore_age(
    hass: HomeAssistant,
    fakes: dict[str, FakeSource],
    entry_data: dict[str, Any],
    config_dir: Path,
    age_days: float,
    options: dict[str, Any],
    ok: bool,
) -> None:
    seed_backup(config_dir, SOURCE_Z2M, ago(days=age_days), {"ieee": IEEE_A})
    entry = MockConfigEntry(
        domain=DOMAIN, data=entry_data, options=options, entry_id=ENTRY_ID, unique_id=DOMAIN
    )
    coord = await setup_entry(hass, entry)
    _record, check = await coord.async_restore_check(SOURCE_Z2M, None)
    assert check.ok is ok
    if not ok:
        limit = options.get(CONF_MAX_RESTORE_AGE_DAYS, 7)
        assert any(f"(limit {limit})" in r for r in check.reasons)
        with pytest.raises(RestoreBlocked):
            await coord.async_restore(SOURCE_Z2M, None, False)
        assert "restore" not in fakes[SOURCE_Z2M].calls
        await coord.async_restore(SOURCE_Z2M, None, True)
        assert "restore" in fakes[SOURCE_Z2M].calls


async def test_device_count_drop_warning(
    hass: HomeAssistant,
    fakes: dict[str, FakeSource],
    mock_config_entry: MockConfigEntry,
    config_dir: Path,
) -> None:
    older = seed_backup(config_dir, SOURCE_Z2M, ago(hours=30), {"ieee": IEEE_A, "device_count": 10})
    newer = seed_backup(config_dir, SOURCE_Z2M, ago(hours=6), {"ieee": IEEE_A, "device_count": 4})
    coord = await setup_entry(hass, mock_config_entry)

    _rec, check = await coord.async_restore_check(SOURCE_Z2M, None)
    assert _rec.path == newer
    assert check.ok  # warning, not a block
    assert any("has 4 devices; the one before it has 10" in w for w in check.warnings)

    _rec, check = await coord.async_restore_check(SOURCE_Z2M, older.name)
    assert _rec.path == older
    assert not any("devices" in w for w in check.warnings)


async def test_device_count_small_drop_no_warning(
    hass: HomeAssistant,
    fakes: dict[str, FakeSource],
    mock_config_entry: MockConfigEntry,
    config_dir: Path,
) -> None:
    seed_backup(config_dir, SOURCE_ZWAVE, ago(hours=30), {"home_id": 1, "node_count": 10})
    seed_backup(config_dir, SOURCE_ZWAVE, ago(hours=6), {"home_id": 1, "node_count": 5})
    coord = await setup_entry(hass, mock_config_entry)
    _rec, check = await coord.async_restore_check(SOURCE_ZWAVE, None)
    assert check.warnings == []


async def test_restoring_pre_restore_snapshot_warns(
    hass: HomeAssistant,
    fakes: dict[str, FakeSource],
    mock_config_entry: MockConfigEntry,
    config_dir: Path,
) -> None:
    good = seed_backup(config_dir, SOURCE_Z2M, ago(hours=5), {"ieee": IEEE_A})
    snap = seed_backup(config_dir, SOURCE_Z2M, ago(hours=1), {"ieee": IEEE_B}, label="pre_restore")
    coord = await setup_entry(hass, mock_config_entry)
    # newest file is the snapshot, but "latest" skips it
    assert coord.states[SOURCE_Z2M].latest.path == good
    rec, check = await coord.async_restore_check(SOURCE_Z2M, None)
    assert rec.path == good

    rec, check = await coord.async_restore_check(SOURCE_Z2M, snap.name)
    assert rec.path == snap
    assert any("pre-restore snapshot" in w for w in check.warnings)


async def test_restore_check_missing_file(
    hass: HomeAssistant, coord: RadioGuardCoordinator, fakes: dict[str, FakeSource]
) -> None:
    with pytest.raises(FileNotFoundError):
        await coord.async_restore_check(SOURCE_Z2M, None)
    with pytest.raises(FileNotFoundError):
        await coord.async_restore(SOURCE_Z2M, "nope.bin", False)
    assert fakes[SOURCE_Z2M].calls == []
    assert not coord.states[SOURCE_Z2M].restoring


async def test_restore_staged_propagates_as_staged(
    hass: HomeAssistant, replaced: tuple[RadioGuardCoordinator, Path], fakes: dict[str, FakeSource]
) -> None:
    coord, _ = replaced
    fakes[SOURCE_Z2M].restore_error = RestoreStaged("/config/zigbee2mqtt", "Stop Z2M, copy, start.")
    events = capture(hass, EVENT_RESTORE_COMPLETE)
    with pytest.raises(RestoreStaged) as exc:
        await coord.async_restore(SOURCE_Z2M, None, False)
    await hass.async_block_till_done()
    assert exc.value.path == "/config/zigbee2mqtt"
    assert not coord.states[SOURCE_Z2M].restoring
    assert events == []
    # not a backup failure
    assert issue(hass, f"backup_failed_{SOURCE_Z2M}") is None
