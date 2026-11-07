"""backup_now and restore services."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events
import voluptuous as vol

from custom_components.radio_guard.backup.base import RestoreCheck
from custom_components.radio_guard.const import (
    ATTR_CONFIRM,
    ATTR_FILE,
    ATTR_FORCE,
    ATTR_SOURCE,
    CONF_SOURCES,
    DOMAIN,
    EVENT_RESTORE_COMPLETE,
    SERVICE_BACKUP_NOW,
    SERVICE_RESTORE,
    SOURCE_Z2M,
    SOURCE_ZHA,
    SOURCE_ZWAVE,
)
from custom_components.radio_guard.coordinator import RadioGuardCoordinator
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .conftest import DEFAULT_ENTRY_DATA, FakeSource

TWO_SOURCES = {**DEFAULT_ENTRY_DATA, CONF_SOURCES: [SOURCE_ZHA, SOURCE_ZWAVE], "heartbeat_enabled": False}


async def _call(hass: HomeAssistant, service: str, data: dict, response: bool = True):
    return await hass.services.async_call(DOMAIN, service, data, blocking=True, return_response=response)


# --------------------------------------------------------------- backup_now


@pytest.mark.parametrize("entry_data", [TWO_SOURCES])
async def test_backup_now_single_source(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_sources: dict[str, FakeSource]
) -> None:
    coord: RadioGuardCoordinator = setup_integration.runtime_data
    resp = await _call(hass, SERVICE_BACKUP_NOW, {ATTR_SOURCE: SOURCE_ZHA})
    assert set(resp["results"]) == {SOURCE_ZHA}
    rec = resp["results"][SOURCE_ZHA]
    assert rec["source"] == SOURCE_ZHA
    assert rec["path"].endswith(".bin")
    assert rec["size"] == len(b"payload-zha")
    assert rec["meta"]["format"] == "fake"
    assert fake_sources[SOURCE_ZHA].backup_calls == 1
    assert fake_sources[SOURCE_ZWAVE].backup_calls == 0
    assert coord.states[SOURCE_ZHA].latest is not None
    assert coord.states[SOURCE_ZHA].count == 1


@pytest.mark.parametrize("entry_data", [TWO_SOURCES])
async def test_backup_now_all_sources(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_sources: dict[str, FakeSource]
) -> None:
    fake_sources[SOURCE_ZWAVE].backup_error = RuntimeError("usb unplugged")
    resp = await _call(hass, SERVICE_BACKUP_NOW, {})
    assert set(resp["results"]) == {SOURCE_ZHA, SOURCE_ZWAVE}
    assert resp["results"][SOURCE_ZHA]["source"] == SOURCE_ZHA
    assert resp["results"][SOURCE_ZWAVE] == {"error": "usb unplugged"}


@pytest.mark.parametrize("entry_data", [TWO_SOURCES])
async def test_backup_now_without_response(hass: HomeAssistant, setup_integration: MockConfigEntry, fake_sources) -> None:
    await _call(hass, SERVICE_BACKUP_NOW, {ATTR_SOURCE: SOURCE_ZHA}, response=False)
    assert fake_sources[SOURCE_ZHA].backup_calls == 1


@pytest.mark.parametrize("entry_data", [TWO_SOURCES])
async def test_backup_now_source_failure_raises(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_sources: dict[str, FakeSource]
) -> None:
    fake_sources[SOURCE_ZHA].backup_error = RuntimeError("timeout")
    with pytest.raises(HomeAssistantError, match="Backup failed: timeout"):
        await _call(hass, SERVICE_BACKUP_NOW, {ATTR_SOURCE: SOURCE_ZHA})


@pytest.mark.parametrize("entry_data", [TWO_SOURCES])
async def test_backup_now_not_enabled_source(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    with pytest.raises(HomeAssistantError, match="zigbee2mqtt is not enabled in Radio Guard"):
        await _call(hass, SERVICE_BACKUP_NOW, {ATTR_SOURCE: SOURCE_Z2M})


@pytest.mark.parametrize("entry_data", [TWO_SOURCES])
async def test_backup_now_unknown_source_rejected_by_schema(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    with pytest.raises(vol.Invalid):
        await _call(hass, SERVICE_BACKUP_NOW, {ATTR_SOURCE: "thread"})


# ------------------------------------------------------------------ restore


@pytest.mark.parametrize("entry_data", [TWO_SOURCES])
async def test_restore_dry_run(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_sources: dict[str, FakeSource]
) -> None:
    coord: RadioGuardCoordinator = setup_integration.runtime_data
    rec = await coord.async_backup(SOURCE_ZHA)
    src = fake_sources[SOURCE_ZHA]
    src.check = RestoreCheck(
        ok=True, warnings=["w1", "w2"], current={"ieee": "live"}, backup={"ieee": "bak"}
    )

    resp = await _call(hass, SERVICE_RESTORE, {ATTR_SOURCE: SOURCE_ZHA})
    assert resp["dry_run"] is True
    assert resp["ok"] is True
    assert resp["reasons"] == []
    assert resp["warnings"] == ["w1", "w2"]
    assert resp["current"] == {"ieee": "live"}
    assert resp["backup"] == {"ieee": "bak"}
    assert resp["file"]["path"] == str(rec.path)
    assert "confirm: true" in resp["hint"]
    # pre-flight got the file bytes and metadata; nothing restored
    assert src.check_calls == [(b"payload-zha", rec.meta)]
    assert src.restore_calls == []


@pytest.mark.parametrize("entry_data", [TWO_SOURCES])
async def test_restore_dry_run_reports_not_ok(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_sources: dict[str, FakeSource]
) -> None:
    coord: RadioGuardCoordinator = setup_integration.runtime_data
    await coord.async_backup(SOURCE_ZHA)
    fake_sources[SOURCE_ZHA].check = RestoreCheck(ok=False, reasons=["bad file"])
    resp = await _call(hass, SERVICE_RESTORE, {ATTR_SOURCE: SOURCE_ZHA, ATTR_CONFIRM: False})
    assert resp["dry_run"] is True
    assert resp["ok"] is False
    assert resp["reasons"] == ["bad file"]


@pytest.mark.parametrize("entry_data", [TWO_SOURCES])
async def test_restore_dry_run_no_backup(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    with pytest.raises(HomeAssistantError, match="No ZHA backup matching latest"):
        await _call(hass, SERVICE_RESTORE, {ATTR_SOURCE: SOURCE_ZHA})


@pytest.mark.parametrize("entry_data", [TWO_SOURCES])
async def test_restore_dry_run_specific_file(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_sources: dict[str, FakeSource], freezer
) -> None:
    coord: RadioGuardCoordinator = setup_integration.runtime_data
    first = await coord.async_backup(SOURCE_ZHA)
    freezer.tick(60)
    second = await coord.async_backup(SOURCE_ZHA)
    assert first.path != second.path

    resp = await _call(hass, SERVICE_RESTORE, {ATTR_SOURCE: SOURCE_ZHA, ATTR_FILE: first.name})
    assert resp["file"]["path"] == str(first.path)
    resp = await _call(hass, SERVICE_RESTORE, {ATTR_SOURCE: SOURCE_ZHA, ATTR_FILE: str(second.path)})
    assert resp["file"]["path"] == str(second.path)
    with pytest.raises(HomeAssistantError, match="No ZHA backup matching missing.json"):
        await _call(hass, SERVICE_RESTORE, {ATTR_SOURCE: SOURCE_ZHA, ATTR_FILE: "missing.json"})


@pytest.mark.parametrize("entry_data", [TWO_SOURCES])
async def test_restore_confirm_calls_source(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_sources: dict[str, FakeSource]
) -> None:
    coord: RadioGuardCoordinator = setup_integration.runtime_data
    rec = await coord.async_backup(SOURCE_ZWAVE)
    src = fake_sources[SOURCE_ZWAVE]
    events = async_capture_events(hass, EVENT_RESTORE_COMPLETE)

    resp = await _call(hass, SERVICE_RESTORE, {ATTR_SOURCE: SOURCE_ZWAVE, ATTR_CONFIRM: True})
    await hass.async_block_till_done()
    assert resp == {"dry_run": False, "restored": rec.as_dict()}
    assert src.restore_calls == [(b"payload-zwave_js", rec.meta)]
    assert len(events) == 1
    assert events[0].data == {"source": SOURCE_ZWAVE, "file": str(rec.path)}


@pytest.mark.parametrize("entry_data", [TWO_SOURCES])
async def test_restore_confirm_blocked_by_preflight_unless_forced(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_sources: dict[str, FakeSource]
) -> None:
    coord: RadioGuardCoordinator = setup_integration.runtime_data
    await coord.async_backup(SOURCE_ZHA)
    src = fake_sources[SOURCE_ZHA]
    src.check = RestoreCheck(ok=False, reasons=["ieee mismatch", "incomplete"])

    with pytest.raises(HomeAssistantError, match="Restore pre-flight failed: ieee mismatch; incomplete"):
        await _call(hass, SERVICE_RESTORE, {ATTR_SOURCE: SOURCE_ZHA, ATTR_CONFIRM: True})
    assert src.restore_calls == []

    resp = await _call(hass, SERVICE_RESTORE, {ATTR_SOURCE: SOURCE_ZHA, ATTR_CONFIRM: True, ATTR_FORCE: True})
    assert resp["dry_run"] is False
    assert len(src.restore_calls) == 1


@pytest.mark.parametrize("entry_data", [TWO_SOURCES])
async def test_restore_source_error_wrapped(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_sources: dict[str, FakeSource]
) -> None:
    coord: RadioGuardCoordinator = setup_integration.runtime_data
    await coord.async_backup(SOURCE_ZHA)
    fake_sources[SOURCE_ZHA].restore_error = RuntimeError("radio rejected")
    with pytest.raises(HomeAssistantError, match="Restore failed: radio rejected"):
        await _call(hass, SERVICE_RESTORE, {ATTR_SOURCE: SOURCE_ZHA, ATTR_CONFIRM: True})


@pytest.mark.parametrize("entry_data", [TWO_SOURCES])
async def test_restore_not_enabled_source(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    with pytest.raises(HomeAssistantError, match="zigbee2mqtt is not enabled"):
        await _call(hass, SERVICE_RESTORE, {ATTR_SOURCE: SOURCE_Z2M})


@pytest.mark.parametrize("entry_data", [TWO_SOURCES])
async def test_restore_requires_source(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    with pytest.raises(vol.Invalid):
        await _call(hass, SERVICE_RESTORE, {})
    with pytest.raises(vol.Invalid):
        await _call(hass, SERVICE_RESTORE, {ATTR_SOURCE: "nope"})


async def test_services_raise_when_not_set_up(hass: HomeAssistant, mock_config_entry: MockConfigEntry, fake_sources) -> None:
    """If the entry was unloaded but a service handler somehow runs, it errors cleanly."""
    from custom_components.radio_guard import _coordinator

    with pytest.raises(HomeAssistantError, match="not set up"):
        _coordinator(hass)
