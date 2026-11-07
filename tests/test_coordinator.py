"""Coordinator: schedule, stale/failed repairs, heartbeat, events."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.radio_guard.backup.base import SourceUnavailable
from custom_components.radio_guard.const import (
    CONF_HEARTBEAT_ENABLED,
    CONF_HEARTBEAT_INTERVAL,
    CONF_HEARTBEAT_TOPIC,
    CONF_SOURCES,
    DOMAIN,
    EVENT_BACKUP_COMPLETE,
    ISSUE_BACKUP_FAILED,
    ISSUE_BACKUP_STALE,
    SOURCE_Z2M,
    SOURCE_ZHA,
)
from custom_components.radio_guard.coordinator import RadioGuardCoordinator
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .conftest import DEFAULT_ENTRY_DATA, FakeSource

ZHA_ONLY = {**DEFAULT_ENTRY_DATA, CONF_SOURCES: [SOURCE_ZHA]}
NO_HB = {**ZHA_ONLY, CONF_HEARTBEAT_ENABLED: False}


def _stale_issue(hass: HomeAssistant, sid: str) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE_BACKUP_STALE}_{sid}")


def _failed_issue(hass: HomeAssistant, sid: str) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE_BACKUP_FAILED}_{sid}")


# ----------------------------------------------------------------- schedule


@pytest.mark.parametrize("entry_data", [NO_HB])
async def test_scheduled_backup_fires_at_configured_time(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_sources: dict[str, FakeSource], freezer
) -> None:
    coord: RadioGuardCoordinator = setup_integration.runtime_data
    src = fake_sources[SOURCE_ZHA]
    assert src.backup_calls == 0

    # 03:16 local -> nothing
    now = dt_util.now().replace(hour=3, minute=16, second=0, microsecond=0) + timedelta(days=1)
    freezer.move_to(now)
    async_fire_time_changed(hass, now)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert src.backup_calls == 0

    # 03:17:00 local -> scheduled backup
    now = now.replace(minute=17)
    freezer.move_to(now)
    async_fire_time_changed(hass, now)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert src.backup_calls == 1

    st = coord.states[SOURCE_ZHA]
    assert st.latest is not None
    assert st.latest.path.exists()
    assert st.latest.path.suffix == ".bin"
    assert st.latest.path.read_bytes() == b"payload-zha"
    assert str(st.latest.path).startswith(hass.config.config_dir)
    assert st.count == 1

    # Next day fires again
    now = now + timedelta(days=1)
    freezer.move_to(now)
    async_fire_time_changed(hass, now)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert src.backup_calls == 2


@pytest.mark.parametrize("entry_data", [NO_HB])
async def test_schedule_stops_after_unload(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_sources: dict[str, FakeSource], freezer
) -> None:
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()
    now = dt_util.now().replace(hour=3, minute=17, second=0, microsecond=0) + timedelta(days=1)
    freezer.move_to(now)
    async_fire_time_changed(hass, now)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert fake_sources[SOURCE_ZHA].backup_calls == 0


# -------------------------------------------------------------- stale issue


@pytest.mark.parametrize("entry_data", [NO_HB])
async def test_stale_issue_when_never_backed_up_then_cleared(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    coord: RadioGuardCoordinator = setup_integration.runtime_data
    issue = _stale_issue(hass, SOURCE_ZHA)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == ISSUE_BACKUP_STALE
    assert issue.translation_placeholders == {"source": "ZHA", "age": "never"}
    assert issue.is_fixable is False

    await coord.async_backup(SOURCE_ZHA)
    await coord.async_refresh()
    assert _stale_issue(hass, SOURCE_ZHA) is None


@pytest.mark.parametrize("entry_data", [NO_HB])
async def test_stale_issue_by_age(
    hass: HomeAssistant, setup_integration: MockConfigEntry, freezer
) -> None:
    """Age is measured from the sidecar's created timestamp, compared to stale_hours (36)."""
    coord: RadioGuardCoordinator = setup_integration.runtime_data
    st = coord.states[SOURCE_ZHA]
    now = dt_util.utcnow()

    # Backdate a backup 35h (write while frozen in the past so created is old)
    freezer.move_to(now - timedelta(hours=35))
    st.store.write(b"old", ".bin", {})
    freezer.move_to(now)
    await coord.async_refresh()
    assert st.age_hours == pytest.approx(35, abs=0.01)
    assert _stale_issue(hass, SOURCE_ZHA) is None

    # Another at 37h: still not stale because the newest (35h) counts
    freezer.move_to(now - timedelta(hours=37))
    st.store.write(b"older", ".bin", {})
    freezer.move_to(now)
    await coord.async_refresh()
    assert _stale_issue(hass, SOURCE_ZHA) is None

    # Remove the 35h file; the 37h one is now newest -> stale
    newest = st.store.latest()
    newest.path.unlink()
    st.store._meta_path_for(newest.path).unlink()
    await coord.async_refresh()
    issue = _stale_issue(hass, SOURCE_ZHA)
    assert issue is not None
    assert issue.translation_placeholders["age"] == "37 h"

    # A fresh backup clears it
    await coord.async_backup(SOURCE_ZHA)
    await coord.async_refresh()
    assert _stale_issue(hass, SOURCE_ZHA) is None


@pytest.mark.parametrize("entry_data", [NO_HB])
async def test_no_stale_issue_when_source_unavailable(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_sources: dict[str, FakeSource]
) -> None:
    coord: RadioGuardCoordinator = setup_integration.runtime_data
    fake_sources[SOURCE_ZHA].is_available = False
    await coord.async_refresh()
    assert coord.states[SOURCE_ZHA].available is False
    assert _stale_issue(hass, SOURCE_ZHA) is None


@pytest.mark.parametrize("entry_data", [NO_HB])
async def test_polling_picks_up_existing_files(
    hass: HomeAssistant, setup_integration: MockConfigEntry, freezer
) -> None:
    """Files written to the store out of band are discovered on the next poll."""
    coord: RadioGuardCoordinator = setup_integration.runtime_data
    st = coord.states[SOURCE_ZHA]
    st.store.write(b"external", ".json", {})
    assert st.count == 0
    freezer.tick(timedelta(minutes=16))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert st.count == 1
    assert st.latest is not None


# ------------------------------------------------------------- failed issue


@pytest.mark.parametrize("entry_data", [NO_HB])
async def test_failed_issue_created_and_cleared(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_sources: dict[str, FakeSource]
) -> None:
    coord: RadioGuardCoordinator = setup_integration.runtime_data
    src = fake_sources[SOURCE_ZHA]
    src.backup_error = RuntimeError("radio on fire")
    events = async_capture_events(hass, EVENT_BACKUP_COMPLETE)

    with pytest.raises(RuntimeError, match="radio on fire"):
        await coord.async_backup(SOURCE_ZHA)

    st = coord.states[SOURCE_ZHA]
    assert st.last_error == "radio on fire"
    assert st.last_attempt is not None
    assert st.running is False
    issue = _failed_issue(hass, SOURCE_ZHA)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.translation_placeholders == {"source": "ZHA", "error": "radio on fire"}
    assert events == []

    src.backup_error = None
    rec = await coord.async_backup(SOURCE_ZHA)
    assert st.last_error is None
    assert _failed_issue(hass, SOURCE_ZHA) is None
    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0].data == {"source": SOURCE_ZHA, "file": str(rec.path), "size": rec.size}


@pytest.mark.parametrize("entry_data", [NO_HB])
async def test_backup_when_source_unavailable(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_sources: dict[str, FakeSource]
) -> None:
    coord: RadioGuardCoordinator = setup_integration.runtime_data
    fake_sources[SOURCE_ZHA].is_available = False
    with pytest.raises(SourceUnavailable, match="ZHA is not loaded"):
        await coord.async_backup(SOURCE_ZHA)
    assert fake_sources[SOURCE_ZHA].backup_calls == 0
    assert _failed_issue(hass, SOURCE_ZHA) is not None


@pytest.mark.parametrize("entry_data", [NO_HB])
async def test_backup_rejects_concurrent_run(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    coord: RadioGuardCoordinator = setup_integration.runtime_data
    coord.states[SOURCE_ZHA].running = True
    with pytest.raises(RuntimeError, match="already running"):
        await coord.async_backup(SOURCE_ZHA)
    # The rejected call must not clobber the flag owned by the in-flight run.
    assert coord.states[SOURCE_ZHA].running is True
    coord.states[SOURCE_ZHA].running = False
    await coord.async_backup(SOURCE_ZHA)
    assert coord.states[SOURCE_ZHA].running is False


async def test_backup_all_collects_errors(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_sources: dict[str, FakeSource]
) -> None:
    coord: RadioGuardCoordinator = setup_integration.runtime_data
    fake_sources[SOURCE_Z2M].backup_error = ValueError("nope")
    results = await coord.async_backup_all()
    assert set(results) == set(coord.states)
    assert isinstance(results[SOURCE_Z2M], ValueError)
    assert results[SOURCE_ZHA].path.exists()
    assert results["zwave_js"].path.exists()
    assert _failed_issue(hass, SOURCE_Z2M) is not None
    assert _failed_issue(hass, SOURCE_ZHA) is None


@pytest.mark.parametrize("entry_data", [NO_HB])
async def test_backup_write_respects_retention(
    hass: HomeAssistant, setup_integration: MockConfigEntry, freezer
) -> None:
    coord: RadioGuardCoordinator = setup_integration.runtime_data
    st = coord.states[SOURCE_ZHA]
    for _ in range(5):
        freezer.tick(timedelta(hours=1))
        await coord.async_backup(SOURCE_ZHA)
    await coord.async_refresh()
    assert st.count == DEFAULT_ENTRY_DATA["retention"] == 3
    assert len(list(st.store.root.glob("*.bin"))) == 3
    assert len(list(st.store.root.glob("*.meta.json"))) == 3


# ---------------------------------------------------------------- heartbeat


@pytest.mark.parametrize(
    "entry_data",
    [{**ZHA_ONLY, CONF_HEARTBEAT_TOPIC: "rg/hb", CONF_HEARTBEAT_INTERVAL: 30}],
)
async def test_heartbeat_publishes_json(
    hass: HomeAssistant, mqtt_mock, mock_config_entry: MockConfigEntry, fake_sources, freezer
) -> None:
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coord: RadioGuardCoordinator = mock_config_entry.runtime_data

    # Initial heartbeat on start
    hb_calls = [c for c in mqtt_mock.async_publish.call_args_list if c.args[0] == "rg/hb"]
    assert len(hb_calls) == 1
    topic, payload_str, qos, retain = hb_calls[0].args
    assert qos == 0 and retain is False
    payload = json.loads(payload_str)
    assert set(payload) == {"ts", "uptime_s", "sources"}
    datetime.fromisoformat(payload["ts"])
    assert isinstance(payload["uptime_s"], int)
    assert payload["sources"] == {SOURCE_ZHA: {"available": True, "backup_age_h": None}}

    # Fires again on the interval, reflecting backup age
    await coord.async_backup(SOURCE_ZHA)
    mqtt_mock.async_publish.reset_mock()
    freezer.tick(timedelta(seconds=30, hours=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    hb_calls = [c for c in mqtt_mock.async_publish.call_args_list if c.args[0] == "rg/hb"]
    assert len(hb_calls) == 1
    payload = json.loads(hb_calls[0].args[1])
    assert payload["sources"][SOURCE_ZHA]["backup_age_h"] == pytest.approx(2.0, abs=0.1)
    assert payload["uptime_s"] >= 7200


@pytest.mark.parametrize("entry_data", [NO_HB])
async def test_heartbeat_disabled(
    hass: HomeAssistant, mqtt_mock, mock_config_entry: MockConfigEntry, fake_sources, freezer
) -> None:
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=60))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not [c for c in mqtt_mock.async_publish.call_args_list if c.args[0] == "radio_guard/heartbeat"]
    assert len(mock_config_entry.runtime_data._unsubs) == 1  # schedule only


@pytest.mark.parametrize("entry_data", [ZHA_ONLY])
async def test_heartbeat_noop_without_mqtt(
    hass: HomeAssistant, setup_integration: MockConfigEntry, freezer
) -> None:
    """Without the MQTT integration loaded the heartbeat silently does nothing."""
    assert "mqtt" not in hass.config.components
    coord: RadioGuardCoordinator = setup_integration.runtime_data
    freezer.tick(timedelta(seconds=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(coord._unsubs) == 2


@pytest.mark.parametrize("entry_data", [ZHA_ONLY])
async def test_heartbeat_publish_error_swallowed(
    hass: HomeAssistant, mqtt_mock, mock_config_entry: MockConfigEntry, fake_sources, freezer
) -> None:
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    mqtt_mock.async_publish.side_effect = RuntimeError("broker down")
    freezer.tick(timedelta(seconds=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)  # no exception propagates


# ------------------------------------------------------------------- misc


@pytest.mark.parametrize("entry_data", [NO_HB])
async def test_age_hours_uses_tz_aware_created(
    hass: HomeAssistant, setup_integration: MockConfigEntry, freezer
) -> None:
    coord: RadioGuardCoordinator = setup_integration.runtime_data
    st = coord.states[SOURCE_ZHA]
    assert st.age_hours is None
    freezer.move_to(datetime(2026, 11, 7, 3, 17, tzinfo=timezone.utc))
    await coord.async_backup(SOURCE_ZHA)
    assert st.latest.created.tzinfo is not None
    freezer.tick(timedelta(hours=1, minutes=30))
    assert st.age_hours == pytest.approx(1.5)
