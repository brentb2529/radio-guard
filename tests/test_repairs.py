"""Repairs: backup-now fix flows and the guided restore flow.

The integration is set up for real; only the backup source is faked, so issue
creation, the repairs flow manager and the coordinator's restore orchestration
all run as in production without MQTT or radios.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.radio_guard.backup.base import (
    BackupPayload,
    BackupSource,
    RestoreCheck,
    RestoreStaged,
)
from custom_components.radio_guard.const import (
    CONF_SOURCES,
    DOMAIN,
    ISSUE_BACKUP_FAILED,
    ISSUE_BACKUP_NEVER,
    ISSUE_BACKUP_STALE,
    ISSUE_COORDINATOR_REPLACED,
    SOURCE_Z2M,
)
from custom_components.radio_guard.repairs import (
    BackupNowRepairFlow,
    RestoreRepairFlow,
    async_create_fix_flow,
)

SID = SOURCE_Z2M
OLD_IEEE = "0x00124b0000000001"  # network in the backups
NEW_IEEE = "0x00124b0000000002"  # replacement stick
NEVER = f"{ISSUE_BACKUP_NEVER}_{SID}"
STALE = f"{ISSUE_BACKUP_STALE}_{SID}"
FAILED = f"{ISSUE_BACKUP_FAILED}_{SID}"
REPLACED = f"{ISSUE_COORDINATOR_REPLACED}_{SID}"


class FakeSource(BackupSource):
    """Scriptable stand-in for a Zigbee2MQTT/ZHA/Z-Wave source."""

    id = SID
    name = "Zigbee2MQTT"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass)
        self.is_available = True
        self.identity: dict[str, Any] = {"ieee": OLD_IEEE}
        self.backup_error: Exception | None = None
        self.check = RestoreCheck(ok=True)
        self.check_error: Exception | None = None
        self.restore_error: Exception | None = None
        self.backup_calls = 0
        self.restores: list[tuple[bytes, dict[str, Any]]] = []

    def available(self) -> bool:
        return self.is_available

    async def async_identity(self) -> dict[str, Any]:
        return dict(self.identity)

    async def async_backup(self) -> BackupPayload:
        self.backup_calls += 1
        if self.backup_error:
            raise self.backup_error
        return BackupPayload(b"backup-bytes", ".zip", {"ieee": self.identity.get("ieee"), "device_count": 5})

    async def async_restore_check(self, data: bytes, meta: dict[str, Any]) -> RestoreCheck:
        if self.check_error:
            raise self.check_error
        self.check.current = dict(self.identity)
        return self.check

    async def async_restore(self, data: bytes, meta: dict[str, Any]) -> None:
        if self.restore_error:
            raise self.restore_error
        self.restores.append((data, meta))
        # the attached stick now carries the restored network
        self.identity = {"ieee": meta.get("ieee")}


# --------------------------------------------------------------- fixtures


@pytest.fixture
def entry_data(entry_data: dict[str, Any]) -> dict[str, Any]:
    """Only one source keeps the issue set small and deterministic."""
    return {**entry_data, CONF_SOURCES: [SID]}


@pytest.fixture
def fake(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> FakeSource:
    src = FakeSource(hass)
    monkeypatch.setattr(
        "custom_components.radio_guard.coordinator.Zigbee2MQTTSource", lambda hass, base_topic, *a: src
    )
    return src


@pytest.fixture
def backup_root(config_dir: Path) -> Path:
    return config_dir / "radio_guard" / SID


def seed_backup(
    root: Path, age: timedelta, meta: dict[str, Any] | None = None, stem: str | None = None
) -> str:
    """Write a stored backup + sidecar with a chosen age. Returns the file name."""
    created = dt_util.utcnow() - age
    stem = stem or created.strftime("%Y%m%dT%H%M%SZ")
    root.mkdir(parents=True, exist_ok=True)
    name = f"{stem}.zip"
    (root / name).write_bytes(b"stored-" + stem.encode())
    full = {"label": None, "source": SID, "created": created.isoformat(), "file": name, **(meta or {})}
    (root / f"{stem}.meta.json").write_text(json.dumps(full))
    return name


async def setup_rg(hass: HomeAssistant, entry: MockConfigEntry) -> MockConfigEntry:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await entry.runtime_data.async_refresh()  # coordinator_replaced needs two readings
    await hass.async_block_till_done()
    return entry


def issue(hass: HomeAssistant, issue_id: str) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id)


async def start_fix(hass: HomeAssistant, issue_id: str) -> dict[str, Any]:
    fm = repairs_flow_manager(hass)
    assert fm is not None
    return await fm.async_init(DOMAIN, data={"issue_id": issue_id})


async def configure(hass: HomeAssistant, flow_id: str, user_input: dict[str, Any]) -> dict[str, Any]:
    result = await repairs_flow_manager(hass).async_configure(flow_id, user_input)
    await hass.async_block_till_done()
    return result


@pytest.fixture
async def replaced(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, fake: FakeSource, backup_root: Path
) -> str:
    """A stored backup of OLD_IEEE while NEW_IEEE is attached -> coordinator_replaced issue."""
    name = seed_backup(backup_root, timedelta(hours=2), {"ieee": OLD_IEEE, "device_count": 12})
    fake.identity = {"ieee": NEW_IEEE, "device_count": 1}
    await setup_rg(hass, mock_config_entry)
    await mock_config_entry.runtime_data.async_refresh()  # replaced needs two readings
    assert issue(hass, REPLACED) is not None
    return name


async def to_confirm(hass: HomeAssistant, file: str) -> dict[str, Any]:
    result = await start_fix(hass, REPLACED)
    result = await configure(hass, result["flow_id"], {"file": file})
    assert result["step_id"] == "preflight"
    result = await configure(hass, result["flow_id"], {"acknowledge": True})
    assert result["step_id"] == "confirm"
    return result


# ------------------------------------------------------ fix flow factory


async def test_create_fix_flow_dispatch(hass: HomeAssistant) -> None:
    restore = await async_create_fix_flow(hass, REPLACED, {"source": SID, "action": "restore"})
    backup = await async_create_fix_flow(hass, STALE, {"source": SID, "action": "backup"})
    assert isinstance(restore, RestoreRepairFlow)
    assert isinstance(backup, BackupNowRepairFlow)
    assert restore.sid == backup.sid == SID


# ------------------------------------------------------ backup-now flows


async def _make_issue(
    kind: str, hass: HomeAssistant, entry: MockConfigEntry, fake: FakeSource, root: Path
) -> str:
    if kind == ISSUE_BACKUP_NEVER:
        await setup_rg(hass, entry)
        return NEVER
    if kind == ISSUE_BACKUP_STALE:
        seed_backup(root, timedelta(hours=48), {"ieee": OLD_IEEE})
        await setup_rg(hass, entry)
        return STALE
    # backup_failed: a backup attempt that errors
    seed_backup(root, timedelta(hours=1), {"ieee": OLD_IEEE})
    await setup_rg(hass, entry)
    fake.backup_error = RuntimeError("bridge timeout")
    with pytest.raises(RuntimeError):
        await entry.runtime_data.async_backup(SID)
    fake.backup_error = None
    fake.backup_calls = 0
    return FAILED


@pytest.mark.parametrize("kind", [ISSUE_BACKUP_NEVER, ISSUE_BACKUP_STALE, ISSUE_BACKUP_FAILED])
async def test_backup_now_fix_flow(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    fake: FakeSource,
    backup_root: Path,
    kind: str,
) -> None:
    issue_id = await _make_issue(kind, hass, mock_config_entry, fake, backup_root)
    found = issue(hass, issue_id)
    assert found is not None
    assert found.is_fixable
    assert found.translation_key == kind
    assert found.data == {"source": SID, "action": "backup"}

    result = await start_fix(hass, issue_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"] == {"source": "Zigbee2MQTT"}
    assert fake.backup_calls == 0  # showing the form does nothing

    result = await configure(hass, result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert fake.backup_calls == 1
    assert issue(hass, issue_id) is None
    coord = mock_config_entry.runtime_data
    assert coord.states[SID].latest is not None
    assert coord.states[SID].age_hours < 1
    # no other problem issues left behind
    for other in (NEVER, STALE, FAILED):
        assert issue(hass, other) is None


async def test_backup_now_fix_flow_failure_aborts_and_keeps_issue(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, fake: FakeSource
) -> None:
    await setup_rg(hass, mock_config_entry)
    assert issue(hass, NEVER) is not None
    fake.backup_error = RuntimeError("no response from bridge")

    result = await start_fix(hass, NEVER)
    result = await configure(hass, result["flow_id"], {})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "backup_failed"
    assert result["description_placeholders"] == {"error": "no response from bridge"}
    # aborted fix flows leave the issue in place, and the failure raised its own issue
    assert issue(hass, NEVER) is not None
    failed = issue(hass, FAILED)
    assert failed is not None
    assert failed.translation_placeholders["error"] == "no response from bridge"


async def test_backup_now_source_unloaded_aborts(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, fake: FakeSource
) -> None:
    await setup_rg(hass, mock_config_entry)
    result = await start_fix(hass, NEVER)
    fake.is_available = False
    result = await configure(hass, result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "backup_failed"
    assert "not loaded" in result["description_placeholders"]["error"]
    assert issue(hass, FAILED) is None  # unavailable is not a failure


# ---------------------------------------------------------- restore flow


async def test_replaced_issue_shape(hass: HomeAssistant, replaced: str) -> None:
    found = issue(hass, REPLACED)
    assert found.is_fixable
    assert found.data == {"source": SID, "action": "restore"}
    assert found.translation_placeholders["live"] == NEW_IEEE
    assert found.translation_placeholders["backup"] == OLD_IEEE


async def test_restore_pick_step_lists_backups(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, fake: FakeSource, backup_root: Path
) -> None:
    older = seed_backup(backup_root, timedelta(days=1), {"ieee": OLD_IEEE, "device_count": 11})
    newest = seed_backup(backup_root, timedelta(hours=1), {"ieee": OLD_IEEE, "device_count": 12})
    fake.identity = {"ieee": NEW_IEEE}
    await setup_rg(hass, mock_config_entry)

    result = await start_fix(hass, REPLACED)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pick"
    assert result["description_placeholders"] == {"source": "Zigbee2MQTT", "count": "2"}
    marker = next(iter(result["data_schema"].schema))
    assert str(marker) == "file"
    assert marker.default() == newest
    options = result["data_schema"].schema[marker].config["options"]
    assert [o["value"] for o in options] == [newest, older]
    assert "12 devices" in options[0]["label"]
    assert OLD_IEEE in options[0]["label"]
    assert "11 devices" in options[1]["label"]


async def test_restore_pick_rejects_unknown_file(hass: HomeAssistant, replaced: str) -> None:
    result = await start_fix(hass, REPLACED)
    with pytest.raises(InvalidData):
        await configure(hass, result["flow_id"], {"file": "../../secrets.yaml"})


async def test_restore_happy_path(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, fake: FakeSource, replaced: str
) -> None:
    fake.check = RestoreCheck(ok=True, warnings=["Unplug the old stick"])
    result = await start_fix(hass, REPLACED)
    assert result["step_id"] == "pick"

    result = await configure(hass, result["flow_id"], {"file": replaced})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "preflight"
    assert result["errors"] == {}
    ph = result["description_placeholders"]
    assert ph["source"] == "Zigbee2MQTT"
    assert ph["file"] == replaced
    assert ph["backup_identity"] == OLD_IEEE
    assert ph["backup_devices"] == "12"
    assert ph["backup_age"] == "2"
    assert ph["current_identity"] == NEW_IEEE
    assert ph["status"] == "OK"
    assert ph["warnings"] == "- Unplug the old stick"
    assert fake.restores == []

    # must tick the acknowledgement; unticked re-shows the pre-flight with an error
    result = await configure(hass, result["flow_id"], {"acknowledge": False})
    assert result["step_id"] == "preflight"
    assert result["errors"] == {"acknowledge": "acknowledge_required"}
    result = await configure(hass, result["flow_id"], {"acknowledge": True})
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"] == {"source": "Zigbee2MQTT"}
    assert result["errors"] is None
    assert fake.restores == []

    result = await configure(hass, result["flow_id"], {"typed": "RESTORE"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(fake.restores) == 1
    data, meta = fake.restores[0]
    assert data == (Path(mock_config_entry.runtime_data.states[SID].store.root) / replaced).read_bytes()
    assert meta["file"] == replaced
    assert meta["allow_ieee_overwrite"] is False  # check was OK -> no force
    assert issue(hass, REPLACED) is None
    # the replacement stick reported 1 device (empty network): no pre-restore snapshot
    st = mock_config_entry.runtime_data.states[SID]
    assert not any(r.name.startswith("pre_restore_") for r in st.records)
    assert st.latest.name == replaced


async def test_restore_snapshots_target_with_unknown_device_count(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, fake: FakeSource, replaced: str
) -> None:
    fake.identity = {"ieee": NEW_IEEE}  # device count unknown -> snapshot it first
    await mock_config_entry.runtime_data.async_refresh()
    assert issue(hass, REPLACED) is not None
    result = await to_confirm(hass, replaced)
    result = await configure(hass, result["flow_id"], {"typed": "RESTORE"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert fake.backup_calls == 1
    st = mock_config_entry.runtime_data.states[SID]
    assert any(r.name.startswith("pre_restore_") for r in st.records)
    assert st.latest.name == replaced  # labelled snapshot never becomes "latest"


@pytest.mark.parametrize("typed", ["restore", "RESTOR", "", "Restore", "RESTORE!"])
async def test_restore_requires_exact_word(
    hass: HomeAssistant, fake: FakeSource, replaced: str, typed: str
) -> None:
    result = await to_confirm(hass, replaced)
    result = await configure(hass, result["flow_id"], {"typed": typed})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert result["errors"] == {"typed": "type_restore"}
    assert fake.restores == []
    assert issue(hass, REPLACED) is not None

    # the same flow can still be completed
    result = await configure(hass, result["flow_id"], {"typed": "  RESTORE  "})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(fake.restores) == 1


async def test_restore_confirm_requires_field(hass: HomeAssistant, replaced: str) -> None:
    result = await to_confirm(hass, replaced)
    with pytest.raises(InvalidData):
        await configure(hass, result["flow_id"], {})


async def test_restore_picks_older_file(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, fake: FakeSource, backup_root: Path
) -> None:
    older = seed_backup(backup_root, timedelta(days=2), {"ieee": OLD_IEEE, "device_count": 30})
    seed_backup(backup_root, timedelta(hours=1), {"ieee": OLD_IEEE, "device_count": 3})
    fake.identity = {"ieee": NEW_IEEE}
    await setup_rg(hass, mock_config_entry)

    result = await to_confirm(hass, older)
    result = await configure(hass, result["flow_id"], {"typed": "RESTORE"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert fake.restores[0][1]["file"] == older


async def test_restore_blocked_preflight_stops_the_flow(
    hass: HomeAssistant, fake: FakeSource, replaced: str
) -> None:
    """A BLOCKED pre-flight shows the reasons and offers no way to proceed with that file."""
    fake.check = RestoreCheck(ok=False, reasons=["Network key mismatch"], warnings=["w1", "w2"])
    result = await start_fix(hass, REPLACED)
    result = await configure(hass, result["flow_id"], {"file": replaced})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "blocked"
    assert list(result["data_schema"].schema) == []  # no acknowledge box, nothing to tick through
    ph = result["description_placeholders"]
    assert ph["reasons"] == "- Network key mismatch"
    assert ph["warnings"] == "- w1\n- w2"
    assert ph["file"] == replaced
    assert ph["source_id"] == "zigbee2mqtt"

    # Submitting goes back to the backup list, never to confirm.
    result = await configure(hass, result["flow_id"], {})
    assert result["step_id"] == "pick"
    assert fake.restores == []
    assert issue(hass, REPLACED) is not None


async def test_restore_blocked_then_pick_ok_backup_restores_without_force(
    hass: HomeAssistant, fake: FakeSource, replaced: str
) -> None:
    fake.check = RestoreCheck(ok=False, reasons=["Network key mismatch"])
    result = await start_fix(hass, REPLACED)
    result = await configure(hass, result["flow_id"], {"file": replaced})
    assert result["step_id"] == "blocked"
    result = await configure(hass, result["flow_id"], {})
    assert result["step_id"] == "pick"

    fake.check = RestoreCheck(ok=True)  # e.g. the user picked a backup that passes
    result = await configure(hass, result["flow_id"], {"file": replaced})
    assert result["step_id"] == "preflight"
    result = await configure(hass, result["flow_id"], {"acknowledge": True})
    result = await configure(hass, result["flow_id"], {"typed": "RESTORE"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert fake.restores[0][1]["allow_ieee_overwrite"] is False


async def test_restore_blocked_by_max_age(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, fake: FakeSource, backup_root: Path
) -> None:
    name = seed_backup(backup_root, timedelta(days=10), {"ieee": OLD_IEEE, "device_count": 4})
    fake.identity = {"ieee": NEW_IEEE}
    await setup_rg(hass, mock_config_entry)

    result = await start_fix(hass, REPLACED)
    result = await configure(hass, result["flow_id"], {"file": name})
    assert result["step_id"] == "blocked"
    assert "10 days old (limit 7)" in result["description_placeholders"]["reasons"]
    assert fake.restores == []


async def test_restore_preflight_error_aborts(hass: HomeAssistant, fake: FakeSource, replaced: str) -> None:
    fake.check_error = ValueError("corrupt zip")
    result = await start_fix(hass, REPLACED)
    result = await configure(hass, result["flow_id"], {"file": replaced})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "preflight_error"
    assert result["description_placeholders"] == {"error": "corrupt zip"}
    assert issue(hass, REPLACED) is not None


async def test_restore_staged_shows_manual_steps(
    hass: HomeAssistant, fake: FakeSource, replaced: str
) -> None:
    fake.restore_error = RestoreStaged(
        "/config/radio_guard/staged", "Stop Zigbee2MQTT, copy the files, start it."
    )
    result = await to_confirm(hass, replaced)
    result = await configure(hass, result["flow_id"], {"typed": "RESTORE"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "restore_staged"
    assert result["description_placeholders"] == {
        "path": "/config/radio_guard/staged",
        "steps": "Stop Zigbee2MQTT, copy the files, start it.",
    }
    # not finished yet, so the issue stays until the user completes it
    assert issue(hass, REPLACED) is not None


async def test_restore_failure_aborts(hass: HomeAssistant, fake: FakeSource, replaced: str) -> None:
    fake.restore_error = RuntimeError("NVM write failed")
    result = await to_confirm(hass, replaced)
    result = await configure(hass, result["flow_id"], {"typed": "RESTORE"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "restore_failed"
    assert result["description_placeholders"] == {"error": "NVM write failed"}
    assert issue(hass, REPLACED) is not None


async def test_restore_no_backups_aborts(hass: HomeAssistant, backup_root: Path, replaced: str) -> None:
    for path in backup_root.iterdir():
        path.unlink()
    result = await start_fix(hass, REPLACED)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_backups"
    assert result["description_placeholders"] == {"source": "Zigbee2MQTT"}
