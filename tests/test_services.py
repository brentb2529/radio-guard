"""Services: backup_now, list_backups, restore (dry_run / restore)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.radio_guard.backup.base import RestoreStaged
from custom_components.radio_guard.const import (
    CONF_SOURCES,
    DOMAIN,
    NOTIFICATION_RESTORE,
    PRE_RESTORE_PREFIX,
    SOURCE_Z2M,
    SOURCE_ZHA,
    SOURCE_ZWAVE,
)
from custom_components.radio_guard.coordinator import RadioGuardCoordinator

from .test_coordinator import (
    ENTRY_ID,
    IEEE_A,
    IEEE_B,
    SIDS,
    FakeSource,
    ago,
    files_in,
    issue,
    notifications,
    seed_backup,
    setup_entry,
)


@pytest.fixture
def fk(hass: HomeAssistant) -> dict[str, FakeSource]:
    sources = {sid: FakeSource(hass, sid) for sid in SIDS}
    with (
        patch(
            "custom_components.radio_guard.coordinator.Zigbee2MQTTSource",
            lambda hass, topic: sources[SOURCE_Z2M],
        ),
        patch("custom_components.radio_guard.coordinator.ZHASource", lambda hass: sources[SOURCE_ZHA]),
        patch("custom_components.radio_guard.coordinator.ZWaveJSSource", lambda hass: sources[SOURCE_ZWAVE]),
    ):
        yield sources


@pytest.fixture
async def loaded(
    hass: HomeAssistant, fk: dict[str, FakeSource], mock_config_entry: MockConfigEntry
) -> RadioGuardCoordinator:
    return await setup_entry(hass, mock_config_entry)


async def call(hass: HomeAssistant, service: str, data: dict[str, Any] | None = None, **kw: Any) -> Any:
    return await hass.services.async_call(DOMAIN, service, data or {}, blocking=True, **kw)


def restore_note(hass: HomeAssistant, sid: str) -> dict[str, Any]:
    return notifications(hass)[f"{NOTIFICATION_RESTORE}_{sid}"]


# ================================================================ backup_now


async def test_backup_now_single_source(
    hass: HomeAssistant, loaded: RadioGuardCoordinator, fk: dict[str, FakeSource], config_dir: Path
) -> None:
    fk[SOURCE_Z2M].backup_meta = {"ieee": IEEE_A, "device_count": 7, "format": "z2m-zip"}
    resp = await call(hass, "backup_now", {"source": SOURCE_Z2M}, return_response=True)

    [name] = files_in(config_dir, SOURCE_Z2M)
    summary = resp["results"][SOURCE_Z2M]
    assert list(resp["results"]) == [SOURCE_Z2M]
    assert summary["file"] == name
    assert summary["path"] == str(config_dir / "radio_guard" / SOURCE_Z2M / name)
    assert summary["identity"] == IEEE_A
    assert summary["devices"] == 7
    assert summary["format"] == "z2m-zip"
    assert summary["age_hours"] == 0.0
    assert fk[SOURCE_ZHA].backups_taken == 0
    assert issue(hass, f"backup_never_{SOURCE_Z2M}") is None


async def test_backup_now_without_response(
    hass: HomeAssistant, loaded: RadioGuardCoordinator, fk: dict[str, FakeSource]
) -> None:
    assert await call(hass, "backup_now", {"source": SOURCE_ZWAVE}) is None
    assert fk[SOURCE_ZWAVE].backups_taken == 1


async def test_backup_now_all_sources_reports_errors_inline(
    hass: HomeAssistant, loaded: RadioGuardCoordinator, fk: dict[str, FakeSource]
) -> None:
    fk[SOURCE_ZHA].is_available = False
    fk[SOURCE_ZWAVE].backup_error = RuntimeError("controller jammed")
    resp = await call(hass, "backup_now", return_response=True)

    res = resp["results"]
    assert set(res) == set(SIDS)
    assert "file" in res[SOURCE_Z2M]
    assert res[SOURCE_ZHA] == {"error": "ZHA is not loaded"}
    assert res[SOURCE_ZWAVE] == {"error": "controller jammed"}
    assert issue(hass, f"backup_failed_{SOURCE_ZWAVE}") is not None


async def test_backup_now_single_source_failure_raises(
    hass: HomeAssistant, loaded: RadioGuardCoordinator, fk: dict[str, FakeSource]
) -> None:
    fk[SOURCE_Z2M].backup_error = RuntimeError("bridge offline")
    with pytest.raises(HomeAssistantError) as exc:
        await call(hass, "backup_now", {"source": SOURCE_Z2M})
    assert exc.value.translation_key == "backup_failed"
    assert exc.value.translation_placeholders == {"source": "Zigbee2MQTT", "error": "bridge offline"}


async def test_backup_now_source_not_enabled(
    hass: HomeAssistant, fk: dict[str, FakeSource], entry_data: dict[str, Any]
) -> None:
    entry_data[CONF_SOURCES] = [SOURCE_Z2M]
    await setup_entry(
        hass, MockConfigEntry(domain=DOMAIN, data=entry_data, entry_id=ENTRY_ID, unique_id=DOMAIN)
    )
    with pytest.raises(ServiceValidationError) as exc:
        await call(hass, "backup_now", {"source": SOURCE_ZHA})
    assert exc.value.translation_key == "source_not_enabled"
    assert exc.value.translation_placeholders == {"source": SOURCE_ZHA}
    assert fk[SOURCE_ZHA].backups_taken == 0


async def test_backup_now_rejects_unknown_source(hass: HomeAssistant, loaded: RadioGuardCoordinator) -> None:
    with pytest.raises(vol.Invalid):
        await call(hass, "backup_now", {"source": "deconz"})


# ============================================================== list_backups


async def test_list_backups_response_only(
    hass: HomeAssistant, loaded: RadioGuardCoordinator, fk: dict[str, FakeSource], config_dir: Path
) -> None:
    old = seed_backup(config_dir, SOURCE_Z2M, ago(hours=30), {"ieee": IEEE_A, "device_count": 9})
    snap = seed_backup(config_dir, SOURCE_Z2M, ago(hours=2), {"ieee": IEEE_B}, label="pre_restore")
    seed_backup(config_dir, SOURCE_ZWAVE, ago(hours=1), {"home_id": 3735928559, "home_id_hex": "0xdeadbeef"})

    # SupportsResponse.ONLY: calling without return_response is rejected
    with pytest.raises(ServiceValidationError):
        await call(hass, "list_backups")

    resp = await call(hass, "list_backups", return_response=True)
    backups = resp["backups"]
    assert set(backups) == set(SIDS)
    assert backups[SOURCE_ZHA] == []
    # newest first, labelled snapshots are listed too
    assert [b["file"] for b in backups[SOURCE_Z2M]] == [snap.name, old.name]
    assert backups[SOURCE_Z2M][1]["devices"] == 9
    assert backups[SOURCE_Z2M][1]["age_hours"] == pytest.approx(30, abs=0.2)
    assert backups[SOURCE_ZWAVE][0]["identity"] == "0xdeadbeef"

    one = await call(hass, "list_backups", {"source": SOURCE_ZWAVE}, return_response=True)
    assert list(one["backups"]) == [SOURCE_ZWAVE]


async def test_list_backups_source_not_enabled(
    hass: HomeAssistant, fk: dict[str, FakeSource], entry_data: dict[str, Any]
) -> None:
    entry_data[CONF_SOURCES] = [SOURCE_ZWAVE]
    await setup_entry(
        hass, MockConfigEntry(domain=DOMAIN, data=entry_data, entry_id=ENTRY_ID, unique_id=DOMAIN)
    )
    with pytest.raises(ServiceValidationError) as exc:
        await call(hass, "list_backups", {"source": SOURCE_Z2M}, return_response=True)
    assert exc.value.translation_key == "source_not_enabled"


# =================================================================== restore


@pytest.fixture
async def with_backup(
    hass: HomeAssistant, fk: dict[str, FakeSource], mock_config_entry: MockConfigEntry, config_dir: Path
) -> Path:
    path = seed_backup(config_dir, SOURCE_Z2M, ago(hours=3), {"ieee": IEEE_A, "device_count": 20})
    fk[SOURCE_Z2M].identity = {"ieee": IEEE_B, "device_count": 4}
    fk[SOURCE_Z2M].backup_meta = {"ieee": IEEE_B, "device_count": 4}
    await setup_entry(hass, mock_config_entry)
    return path


async def test_restore_dry_run_is_default_and_notifies(
    hass: HomeAssistant, with_backup: Path, fk: dict[str, FakeSource], config_dir: Path
) -> None:
    fk[SOURCE_Z2M].check_warnings = ["Adapter type differs"]
    resp = await call(hass, "restore", {"source": SOURCE_Z2M}, return_response=True)

    assert resp["dry_run"] is True
    assert resp["verdict"] == "OK"
    assert resp["summary"] == (
        f"Restore Zigbee2MQTT network {IEEE_A} (20 devices) from {with_backup.name} onto coordinator {IEEE_B} (4 devices)"
    )
    assert resp["backup"]["file"] == with_backup.name
    assert resp["target"] == {"ieee": IEEE_B, "device_count": 4}
    assert resp["blocking"] == []
    assert resp["warnings"] == ["Adapter type differs"]
    assert "mode: restore" in resp["next"]

    # dry run never changes anything
    assert fk[SOURCE_Z2M].calls == ["restore_check"]
    assert files_in(config_dir, SOURCE_Z2M) == [with_backup.name]

    note = restore_note(hass, SOURCE_Z2M)
    assert note["title"] == "Radio Guard dry run: Zigbee2MQTT"
    assert "Verdict: OK" in note["message"]
    assert "- Adapter type differs" in note["message"]


async def test_restore_dry_run_blocked_returns_verdict(
    hass: HomeAssistant, with_backup: Path, fk: dict[str, FakeSource]
) -> None:
    fk[SOURCE_Z2M].check_ok = False
    fk[SOURCE_Z2M].check_reasons = ["Target already runs a network"]
    resp = await call(hass, "restore", {"source": SOURCE_Z2M, "mode": "dry_run"}, return_response=True)
    assert resp["verdict"] == "BLOCKED"
    assert resp["blocking"] == ["Target already runs a network"]
    assert "force: true" in resp["next"]
    assert "- BLOCKING: Target already runs a network" in restore_note(hass, SOURCE_Z2M)["message"]
    assert "restore" not in fk[SOURCE_Z2M].calls


async def test_restore_dry_run_age_block_from_coordinator(
    hass: HomeAssistant, fk: dict[str, FakeSource], mock_config_entry: MockConfigEntry, config_dir: Path
) -> None:
    seed_backup(config_dir, SOURCE_ZHA, ago(days=10), {"ieee": IEEE_A})
    await setup_entry(hass, mock_config_entry)
    resp = await call(hass, "restore", {"source": SOURCE_ZHA}, return_response=True)
    assert resp["verdict"] == "BLOCKED"
    assert any("10 days old (limit 7)" in r for r in resp["blocking"])


async def test_restore_mode_restore(
    hass: HomeAssistant, with_backup: Path, fk: dict[str, FakeSource], config_dir: Path
) -> None:
    resp = await call(hass, "restore", {"source": SOURCE_Z2M, "mode": "restore"}, return_response=True)
    assert resp["dry_run"] is False
    assert resp["restored"]["file"] == with_backup.name
    assert fk[SOURCE_Z2M].calls == ["restore_check", "backup", "restore"]
    assert fk[SOURCE_Z2M].restored[0][1]["allow_ieee_overwrite"] is False
    assert any(n.startswith(PRE_RESTORE_PREFIX) for n in files_in(config_dir, SOURCE_Z2M))
    note = restore_note(hass, SOURCE_Z2M)
    assert note["title"] == "Radio Guard restore complete: Zigbee2MQTT"
    assert with_backup.name in note["message"]
    assert issue(hass, f"coordinator_replaced_{SOURCE_Z2M}") is None


async def test_restore_specific_file(
    hass: HomeAssistant, fk: dict[str, FakeSource], mock_config_entry: MockConfigEntry, config_dir: Path
) -> None:
    older = seed_backup(config_dir, SOURCE_ZWAVE, ago(hours=30), {"home_id": 1})
    seed_backup(config_dir, SOURCE_ZWAVE, ago(hours=2), {"home_id": 1})
    await setup_entry(hass, mock_config_entry)
    resp = await call(
        hass,
        "restore",
        {"source": SOURCE_ZWAVE, "file": older.name, "mode": "restore"},
        return_response=True,
    )
    assert resp["restored"]["file"] == older.name
    assert fk[SOURCE_ZWAVE].restored[0][0] == older.read_bytes()


async def test_restore_blocked_preflight_raises_validation_error(
    hass: HomeAssistant, with_backup: Path, fk: dict[str, FakeSource]
) -> None:
    fk[SOURCE_Z2M].check_ok = False
    fk[SOURCE_Z2M].check_reasons = ["IEEE mismatch", "Security keys differ"]
    with pytest.raises(ServiceValidationError) as exc:
        await call(hass, "restore", {"source": SOURCE_Z2M, "mode": "restore"})
    assert exc.value.translation_domain == DOMAIN
    assert exc.value.translation_key == "restore_blocked"
    assert exc.value.translation_placeholders == {
        "source": "Zigbee2MQTT",
        "reasons": "IEEE mismatch; Security keys differ",
    }
    assert fk[SOURCE_Z2M].calls == ["restore_check"]


async def test_restore_force_overrides_block(
    hass: HomeAssistant, with_backup: Path, fk: dict[str, FakeSource]
) -> None:
    fk[SOURCE_Z2M].check_ok = False
    fk[SOURCE_Z2M].check_reasons = ["IEEE mismatch"]
    resp = await call(
        hass,
        "restore",
        {"source": SOURCE_Z2M, "mode": "restore", "force": True},
        return_response=True,
    )
    assert resp["restored"]["file"] == with_backup.name
    assert fk[SOURCE_Z2M].restored[0][1]["allow_ieee_overwrite"] is True


async def test_restore_staged_is_a_result_not_a_failure(
    hass: HomeAssistant, with_backup: Path, fk: dict[str, FakeSource]
) -> None:
    fk[SOURCE_Z2M].restore_error = RestoreStaged("/config/radio_guard/staged", "1. Stop Z2M\n2. Copy files")
    resp = await call(hass, "restore", {"source": SOURCE_Z2M, "mode": "restore"}, return_response=True)
    assert resp == {
        "dry_run": False,
        "staged": True,
        "path": "/config/radio_guard/staged",
        "steps": "1. Stop Z2M\n2. Copy files",
    }
    note = restore_note(hass, SOURCE_Z2M)
    assert note["title"] == "Radio Guard: finish the Zigbee2MQTT restore by hand"
    assert "/config/radio_guard/staged" in note["message"]


async def test_restore_failure_raises_and_notifies(
    hass: HomeAssistant, with_backup: Path, fk: dict[str, FakeSource]
) -> None:
    fk[SOURCE_Z2M].restore_error = RuntimeError("adapter vanished")
    with pytest.raises(HomeAssistantError) as exc:
        await call(hass, "restore", {"source": SOURCE_Z2M, "mode": "restore"})
    assert not isinstance(exc.value, ServiceValidationError)
    assert exc.value.translation_key == "restore_failed"
    assert exc.value.translation_placeholders == {"source": "Zigbee2MQTT", "error": "adapter vanished"}
    note = restore_note(hass, SOURCE_Z2M)
    assert note["title"] == "Radio Guard restore failed: Zigbee2MQTT"
    assert "adapter vanished" in note["message"]


@pytest.mark.parametrize("mode", ["dry_run", "restore"])
async def test_restore_no_backup(
    hass: HomeAssistant, loaded: RadioGuardCoordinator, fk: dict[str, FakeSource], mode: str
) -> None:
    with pytest.raises(ServiceValidationError) as exc:
        await call(hass, "restore", {"source": SOURCE_ZHA, "mode": mode})
    assert exc.value.translation_key == "no_backup"
    assert exc.value.translation_placeholders == {"source": "ZHA", "file": "latest"}

    with pytest.raises(ServiceValidationError) as exc:
        await call(hass, "restore", {"source": SOURCE_ZHA, "mode": mode, "file": "missing.json"})
    assert exc.value.translation_placeholders["file"] == "missing.json"
    assert fk[SOURCE_ZHA].calls == []


async def test_restore_schema(hass: HomeAssistant, loaded: RadioGuardCoordinator) -> None:
    with pytest.raises(vol.Invalid):
        await call(hass, "restore", {})
    with pytest.raises(vol.Invalid):
        await call(hass, "restore", {"source": SOURCE_Z2M, "mode": "yolo"})


async def test_restore_source_not_enabled(
    hass: HomeAssistant, fk: dict[str, FakeSource], entry_data: dict[str, Any]
) -> None:
    entry_data[CONF_SOURCES] = [SOURCE_Z2M]
    await setup_entry(
        hass, MockConfigEntry(domain=DOMAIN, data=entry_data, entry_id=ENTRY_ID, unique_id=DOMAIN)
    )
    with pytest.raises(ServiceValidationError) as exc:
        await call(hass, "restore", {"source": SOURCE_ZWAVE})
    assert exc.value.translation_key == "source_not_enabled"


# ====================================================== service registration


async def test_services_removed_on_unload_and_back_on_setup(
    hass: HomeAssistant, loaded: RadioGuardCoordinator, mock_config_entry: MockConfigEntry
) -> None:
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert not hass.services.has_service(DOMAIN, "restore")

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    for svc in ("backup_now", "list_backups", "restore"):
        assert hass.services.has_service(DOMAIN, svc)
