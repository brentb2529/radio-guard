"""Guided coordinator replacement / migration wizard and coordinator-down detection.

The integration is set up for real; only the backup source is faked (patched where the
coordinator builds it). The fake implements the whole swap API and records every call, so
the wizard runs through the real repairs / options flow managers, progress tasks included.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.radio_guard.backup.base import (
    BackupPayload,
    BackupSource,
    DeviceReport,
    PortOption,
    RestoreCheck,
    SourceUnavailable,
    SwapPlan,
)
from custom_components.radio_guard.const import (
    CONF_SOURCES,
    DEFAULT_OFFLINE_MINUTES,
    DOMAIN,
    EVENT_RESTORE_COMPLETE,
    ISSUE_BACKUP_FAILED,
    ISSUE_BACKUP_NEVER,
    ISSUE_BACKUP_STALE,
    ISSUE_COORDINATOR_DOWN,
    ISSUE_COORDINATOR_REPLACED,
    PRE_RESTORE_PREFIX,
    SOURCE_Z2M,
    STARTUP_GRACE_MINUTES,
)
from custom_components.radio_guard.coordinator import REPLACED_CONFIRMATIONS, RadioGuardCoordinator
from custom_components.radio_guard.repairs import (
    CoordinatorDownRepairFlow,
    RestoreRepairFlow,
    async_create_fix_flow,
)
from custom_components.radio_guard.wizard import ReplaceWizardMixin

SID = SOURCE_Z2M
OLD_IEEE = "00124b0000000001"
NEW_IEEE = "00124b0000000002"
EPID = "dddddddddddddddd"
OLD_PORT = "/dev/serial/by-id/old-stick"
NEW_PORT = "/dev/serial/by-id/new-stick"
DOWN = f"{ISSUE_COORDINATOR_DOWN}_{SID}"
REPLACED = f"{ISSUE_COORDINATOR_REPLACED}_{SID}"
NEVER = f"{ISSUE_BACKUP_NEVER}_{SID}"
STALE = f"{ISSUE_BACKUP_STALE}_{SID}"
FAILED = f"{ISSUE_BACKUP_FAILED}_{SID}"
NOISE_WARNING = "Unplug the old coordinator for good. Two coordinators with the same IEEE will break it."
STRINGS = Path(__file__).parent.parent / "custom_components" / "radio_guard" / "strings.json"
WIZARD_PY = Path(__file__).parent.parent / "custom_components" / "radio_guard" / "wizard.py"


@pytest.fixture(autouse=True)
def _no_min_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real wizard keeps progress screens up >= 1.5 s for the frontend; tests freeze time."""
    monkeypatch.setattr("custom_components.radio_guard.wizard.MIN_PROGRESS_SECONDS", 0)


# ----------------------------------------------------------------- fake source


class SwapFake(BackupSource):
    """Scriptable source implementing the guided-replacement API."""

    id = SID
    name = "Zigbee2MQTT"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass)
        self.is_available = True
        self.health: bool | None = True
        self.identity: dict[str, Any] = {"ieee": OLD_IEEE, "extended_pan_id": EPID, "device_count": 5}
        self.plan = SwapPlan(supported=True, automatic=True, choose_port=True, current_port=OLD_PORT)
        self.ports = [PortOption(NEW_PORT, "Sonoff ZBDongle-E", new=True), PortOption("/dev/ttyS0", "ttyS0")]
        self.port: str | None = OLD_PORT
        self.check_reasons: list[str] = []
        self.check_warnings: list[str] = []
        self.verify_error: Exception | None = None
        self.backup_error: Exception | None = None
        self.report: DeviceReport | None = DeviceReport(
            mains_total=4,
            mains_back=3,
            battery_total=2,
            battery_back=1,
            missing_mains=["Hall lamp"],
            missing_battery=["Door sensor"],
        )
        self.gates: dict[str, asyncio.Event] = {}
        self.calls: list[str] = []
        self.applied: list[tuple[bytes, dict[str, Any], str | None]] = []
        self.wait_timeouts: list[float | None] = []
        self.backup_calls = 0

    async def _gate(self, name: str) -> None:
        self.calls.append(name)
        # Real sources do I/O; without a suspension point HA's eager tasks finish before the
        # wizard can show its progress step, which no real install would see.
        await asyncio.sleep(0)
        if (gate := self.gates.get(name)) is not None:
            await gate.wait()

    # --- BackupSource basics
    def available(self) -> bool:
        return self.is_available

    async def async_identity(self) -> dict[str, Any]:
        if not self.is_available:
            raise SourceUnavailable(self.name)
        return dict(self.identity)

    async def async_backup(self) -> BackupPayload:
        self.backup_calls += 1
        if (gate := self.gates.get("backup")) is not None:
            await gate.wait()
        if self.backup_error is not None:
            raise self.backup_error
        return BackupPayload(b"fresh-backup", ".zip", {**self.identity})

    async def async_restore_check(self, data: bytes, meta: dict[str, Any]) -> RestoreCheck:
        return RestoreCheck(
            ok=not self.check_reasons,
            reasons=list(self.check_reasons),
            warnings=list(self.check_warnings),
            current=dict(self.identity),
            backup=dict(meta),
        )

    async def async_restore(self, data: bytes, meta: dict[str, Any]) -> None:
        raise AssertionError("the wizard never uses the old restore path")

    # --- swap API
    async def async_health(self) -> bool | None:
        return self.health

    async def async_swap_plan(self) -> SwapPlan:
        return self.plan

    async def async_swap_ports(self) -> list[PortOption]:
        return list(self.ports)

    async def async_swap_stop(self, plan: SwapPlan) -> None:
        await self._gate("stop")
        self.health = False

    async def async_wait_stopped(self, timeout: float) -> None:
        self.calls.append("wait_stopped")

    async def async_swap_apply(self, data: bytes, meta: dict[str, Any], port: str | None) -> None:
        await self._gate("apply")
        self.applied.append((data, meta, port))
        if port:
            self.port = port

    async def async_swap_set_port(self, port: str | None) -> None:
        self.calls.append(f"set_port:{port}")
        if port:
            self.port = port

    async def async_swap_start(self, plan: SwapPlan) -> None:
        await self._gate("start")
        self.health = True

    async def async_wait_started(self, timeout: float | None) -> None:
        self.wait_timeouts.append(timeout)
        await self._gate("wait_started")
        self.health = True

    async def async_swap_verify(self, data: bytes, meta: dict[str, Any]) -> dict[str, Any]:
        await self._gate("verify")
        if self.verify_error is not None:
            raise self.verify_error
        self.identity = {**self.identity, "ieee": meta.get("ieee") or OLD_IEEE, "extended_pan_id": EPID}
        return dict(self.identity)

    async def async_check_devices(self, timeout: float) -> DeviceReport | None:
        await self._gate("check_devices")
        return self.report


def swap_calls(fake: SwapFake) -> list[str]:
    return [c for c in fake.calls if c != "wait_stopped"]


# -------------------------------------------------------------------- fixtures


@pytest.fixture
def entry_data(entry_data: dict[str, Any]) -> dict[str, Any]:
    return {**entry_data, CONF_SOURCES: [SID]}


@pytest.fixture
def fake(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> SwapFake:
    src = SwapFake(hass)
    monkeypatch.setattr(
        "custom_components.radio_guard.coordinator.Zigbee2MQTTSource", lambda hass, topic, *a: src
    )
    return src


@pytest.fixture
def backup_root(config_dir: Path) -> Path:
    return config_dir / "radio_guard" / SID


@pytest.fixture
def shim_migrate_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Work around BUG: the wizard shows menus with step ids that have no handler.

    HA's flow manager refuses (UnknownStep, flow removed) any form/menu whose step_id has no
    ``async_step_<id>``. ``wiz_done_migrate`` and ``wiz_failed_migrate`` have none, so the migrate
    path dies at its last screen. Alias them so the rest of the migrate/rollback logic is testable;
    a no-op once the source grows real handlers.
    """
    if not hasattr(ReplaceWizardMixin, "async_step_wiz_done_migrate"):
        monkeypatch.setattr(
            ReplaceWizardMixin,
            "async_step_wiz_done_migrate",
            ReplaceWizardMixin.async_step_wiz_done,
            raising=False,
        )
    if not hasattr(ReplaceWizardMixin, "async_step_wiz_failed_migrate"):
        monkeypatch.setattr(
            ReplaceWizardMixin,
            "async_step_wiz_failed_migrate",
            ReplaceWizardMixin.async_step_wiz_failed,
            raising=False,
        )


def seed_backup(
    root: Path,
    age: timedelta,
    meta: dict[str, Any] | None = None,
    *,
    label: str | None = None,
) -> str:
    created = dt_util.utcnow() - age
    stem = created.strftime("%Y%m%dT%H%M%SZ")
    if label:
        stem = f"{label}_{stem}"
    root.mkdir(parents=True, exist_ok=True)
    name = f"{stem}.zip"
    (root / name).write_bytes(b"stored-" + stem.encode())
    full = {"label": label, "source": SID, "created": created.isoformat(), "file": name, **(meta or {})}
    (root / f"{stem}.meta.json").write_text(json.dumps(full))
    return name


async def setup_rg(hass: HomeAssistant, entry: MockConfigEntry) -> RadioGuardCoordinator:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data


async def refresh(hass: HomeAssistant, coord: RadioGuardCoordinator) -> None:
    await coord.async_refresh()
    await hass.async_block_till_done()


def issue(hass: HomeAssistant, issue_id: str) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id)


async def go_down(hass: HomeAssistant, freezer, coord: RadioGuardCoordinator, fake: SwapFake) -> None:
    """Coordinator reports offline past the startup grace and the offline window."""
    fake.health = False
    freezer.tick(timedelta(minutes=STARTUP_GRACE_MINUTES))
    await refresh(hass, coord)
    assert coord.states[SID].down_since is not None
    freezer.tick(timedelta(minutes=DEFAULT_OFFLINE_MINUTES))
    await refresh(hass, coord)
    assert issue(hass, DOWN) is not None


async def drive(
    hass: HomeAssistant, mgr, result: dict[str, Any], seen: list[tuple[str, str]]
) -> dict[str, Any]:
    """Let progress steps run to completion; record (step_id, progress_action) of each."""
    for _ in range(20):
        if result["type"] is FlowResultType.SHOW_PROGRESS:
            seen.append((result["step_id"], result["progress_action"]))
        elif result["type"] is not FlowResultType.SHOW_PROGRESS_DONE:
            return result
        await hass.async_block_till_done()
        result = await mgr.async_configure(result["flow_id"])
        await hass.async_block_till_done()
    raise AssertionError("wizard did not settle")


async def step(hass: HomeAssistant, mgr, flow_id: str, user_input: dict | None, seen=None) -> dict[str, Any]:
    result = await mgr.async_configure(flow_id, user_input)
    return await drive(hass, mgr, result, seen if seen is not None else [])


def schema_field(result: dict[str, Any], name: str):
    for key, value in result["data_schema"].schema.items():
        if str(key) == name:
            return key, value
    raise AssertionError(f"no field {name} in {result['step_id']}")


@pytest.fixture
async def down(
    hass: HomeAssistant,
    freezer,
    mock_config_entry: MockConfigEntry,
    fake: SwapFake,
    backup_root: Path,
) -> dict[str, str]:
    """Two nightlies + a newer pre-restore snapshot; the coordinator has gone down."""
    files = {
        "newest": seed_backup(backup_root, timedelta(hours=2), {"ieee": OLD_IEEE, "device_count": 12}),
        "older": seed_backup(backup_root, timedelta(hours=26), {"ieee": OLD_IEEE, "device_count": 12}),
        "snapshot": seed_backup(
            backup_root, timedelta(hours=1), {"ieee": NEW_IEEE, "device_count": 1}, label="pre_restore"
        ),
    }
    coord = await setup_rg(hass, mock_config_entry)
    await go_down(hass, freezer, coord, fake)
    return files


async def open_down_repair(hass: HomeAssistant) -> dict[str, Any]:
    mgr = repairs_flow_manager(hass)
    result = await mgr.async_init(DOMAIN, data={"issue_id": DOWN})
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "init"
    assert result["menu_options"] == ["wiz_pick", "wait"]
    return await mgr.async_configure(result["flow_id"], {"next_step_id": "wiz_pick"})


async def replace_to_swap(hass: HomeAssistant, file: str, seen: list) -> dict[str, Any]:
    mgr = repairs_flow_manager(hass)
    result = await open_down_repair(hass)
    assert result["step_id"] == "wiz_pick"
    result = await step(hass, mgr, result["flow_id"], {"file": file}, seen)
    assert result["type"] is FlowResultType.FORM
    return result


# ===================================================================== dispatch


async def test_fix_flow_dispatch(hass: HomeAssistant) -> None:
    flow = await async_create_fix_flow(hass, DOWN, {"source": SID, "action": "replace"})
    assert isinstance(flow, CoordinatorDownRepairFlow)
    assert flow.sid == SID


async def test_coordinator_down_wait_option(
    hass: HomeAssistant, down: dict[str, str], fake: SwapFake
) -> None:
    mgr = repairs_flow_manager(hass)
    result = await mgr.async_init(DOMAIN, data={"issue_id": DOWN})
    result = await mgr.async_configure(result["flow_id"], {"next_step_id": "wait"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "keep_waiting"
    assert issue(hass, DOWN) is not None  # waiting changes nothing
    assert fake.calls == []


# ============================================================ replace via repair


async def test_replace_via_coordinator_down_repair_end_to_end(
    hass: HomeAssistant, down: dict[str, str], fake: SwapFake, mock_config_entry: MockConfigEntry
) -> None:
    coord: RadioGuardCoordinator = mock_config_entry.runtime_data
    st = coord.states[SID]
    events = async_capture_events(hass, EVENT_RESTORE_COMPLETE)
    fake.check_warnings = [NOISE_WARNING, "Adapter type differs from the backup"]
    backups_before = fake.backup_calls
    mgr = repairs_flow_manager(hass)
    seen: list[tuple[str, str]] = []

    # pick: newest nightly is the default, the pre-restore snapshot is listed last and labelled
    result = await open_down_repair(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "wiz_pick"
    key, sel = schema_field(result, "file")
    assert key.default() == down["newest"]
    options = sel.config["options"]
    assert [o["value"] for o in options] == [down["newest"], down["older"], down["snapshot"]]
    assert options[0]["label"].startswith("Today ")
    assert "12 devices" in options[0]["label"]
    assert "automatic snapshot taken before a restore" in options[2]["label"]
    assert "automatic snapshot" not in options[0]["label"]
    assert down["snapshot"].startswith(PRE_RESTORE_PREFIX)
    assert not st.swapping

    # choose -> stop (progress) -> swap form
    result = await mgr.async_configure(result["flow_id"], {"file": down["newest"]})
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert st.swapping
    assert st.down_since is None
    result = await drive(hass, mgr, result, seen)
    assert seen == [("wiz_stop", "stopping")]
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "wiz_swap"
    assert result["description_placeholders"]["current"] == OLD_PORT

    key, sel = schema_field(result, "port")
    assert key.description == {"suggested_value": NEW_PORT}
    labels = {o["value"]: o["label"] for o in sel.config["options"]}
    assert labels[NEW_PORT] == "Sonoff ZBDongle-E (just plugged in)"
    assert labels["/dev/ttyS0"] == "ttyS0"
    assert labels[OLD_PORT] == f"{OLD_PORT} (current setting)"

    # the unplugged checkbox is required
    result = await mgr.async_configure(result["flow_id"], {"port": NEW_PORT})
    assert result["step_id"] == "wiz_swap"
    assert result["errors"] == {"unplugged": "unplugged_required"}

    result = await mgr.async_configure(result["flow_id"], {"port": NEW_PORT, "unplugged": True})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "wiz_confirm"
    ph = result["description_placeholders"]
    assert ph["port"] == NEW_PORT
    assert ph["network"] == "?"  # seeded meta carries no ext PAN
    assert ph["warnings"] == "- Adapter type differs from the backup"  # pre-flight noise filtered
    assert "12 devices" in ph["file"]

    # restore (automatic) -> devices -> done
    seen.clear()
    result = await step(hass, mgr, result["flow_id"], {}, seen)
    assert seen == [("wiz_restore", "restoring"), ("wiz_devices", "checking_devices")]
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "wiz_done"
    assert result["menu_options"] == ["wiz_finish", "wiz_devices"]
    devices = result["description_placeholders"]["devices"]
    assert "Mains-powered devices back: 3 of 4" in devices
    assert "Hall lamp" in devices
    assert "Battery devices heard from: 1 of 2" in devices
    assert "Door sensor" in devices
    assert result["description_placeholders"]["port"] == NEW_PORT

    assert swap_calls(fake) == ["stop", "apply", "start", "wait_started", "verify", "check_devices"]
    data, _meta, port = fake.applied[0]
    assert data == (Path(st.store.root) / down["newest"]).read_bytes()
    assert port == NEW_PORT
    assert fake.wait_timeouts == [180]

    # swap_end already ran: issue gone, fresh backup taken, event fired
    assert not st.swapping
    assert issue(hass, DOWN) is None
    assert fake.backup_calls == backups_before + 1
    assert [e.data for e in events] == [{"source": SID, "file": str(st.store.root / down["newest"])}]

    # "ask devices again" re-runs the check
    seen.clear()
    result = await step(hass, mgr, result["flow_id"], {"next_step_id": "wiz_devices"}, seen)
    assert seen == [("wiz_devices", "checking_devices")]
    assert result["step_id"] == "wiz_done"
    assert swap_calls(fake).count("check_devices") == 2

    result = await mgr.async_configure(result["flow_id"], {"next_step_id": "wiz_finish"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert issue(hass, DOWN) is None
    assert swap_calls(fake).count("start") == 1  # nothing restarted after success


async def test_replace_without_device_report(
    hass: HomeAssistant, down: dict[str, str], fake: SwapFake
) -> None:
    fake.report = None
    mgr = repairs_flow_manager(hass)
    result = await replace_to_swap(hass, down["newest"], [])
    result = await mgr.async_configure(result["flow_id"], {"port": NEW_PORT, "unplugged": True})
    result = await step(hass, mgr, result["flow_id"], {})
    assert result["step_id"] == "wiz_done"
    assert "cannot ask these devices" in result["description_placeholders"]["devices"]


async def test_replace_manual_plan_uses_wait_actions(
    hass: HomeAssistant, down: dict[str, str], fake: SwapFake, mock_config_entry: MockConfigEntry
) -> None:
    fake.plan = SwapPlan(supported=True, automatic=False, choose_port=True, current_port=OLD_PORT)
    mgr = repairs_flow_manager(hass)
    seen: list[tuple[str, str]] = []
    result = await replace_to_swap(hass, down["newest"], seen)
    assert seen == [("wiz_stop", "wait_stopped")]
    assert result["step_id"] == "wiz_swap"
    result = await mgr.async_configure(result["flow_id"], {"port": NEW_PORT, "unplugged": True})
    assert result["step_id"] == "wiz_confirm"
    seen.clear()
    result = await step(hass, mgr, result["flow_id"], {}, seen)
    assert seen == [
        ("wiz_restore", "writing"),
        ("wiz_start", "wait_started"),
        ("wiz_devices", "checking_devices"),
    ]
    assert result["step_id"] == "wiz_done"
    # the user starts it; Radio Guard never does, and waits without a timeout
    assert "start" not in fake.calls
    assert fake.wait_timeouts == [None]
    assert swap_calls(fake) == ["stop", "apply", "wait_started", "verify", "check_devices"]
    assert not mock_config_entry.runtime_data.states[SID].swapping


async def test_replace_without_port_choice(hass: HomeAssistant, down: dict[str, str], fake: SwapFake) -> None:
    fake.plan = SwapPlan(supported=True, automatic=True, choose_port=False, current_port=None)
    mgr = repairs_flow_manager(hass)
    result = await replace_to_swap(hass, down["newest"], [])
    assert [str(k) for k in result["data_schema"].schema] == ["unplugged"]
    result = await mgr.async_configure(result["flow_id"], {"unplugged": True})
    assert result["description_placeholders"]["port"] == "unchanged"
    await step(hass, mgr, result["flow_id"], {})
    assert fake.applied[0][2] is None


async def test_replace_via_coordinator_replaced_repair_uses_wizard(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, fake: SwapFake, backup_root: Path
) -> None:
    seed_backup(backup_root, timedelta(hours=2), {"ieee": OLD_IEEE, "device_count": 12})
    fake.identity = {"ieee": NEW_IEEE, "device_count": 1}
    coord = await setup_rg(hass, mock_config_entry)
    await refresh(hass, coord)
    assert issue(hass, REPLACED) is not None
    flow = await async_create_fix_flow(hass, REPLACED, {"source": SID, "action": "restore"})
    assert isinstance(flow, RestoreRepairFlow)
    result = await repairs_flow_manager(hass).async_init(DOMAIN, data={"issue_id": REPLACED})
    assert result["step_id"] == "wiz_pick"

    fake.plan = SwapPlan(supported=False, reason="nope")
    result = await repairs_flow_manager(hass).async_init(DOMAIN, data={"issue_id": REPLACED})
    assert result["step_id"] == "pick"  # classic restore flow when guided swap is unavailable


# ============================================================= aborts / guards


async def test_unsupported_plan_aborts_with_reason(
    hass: HomeAssistant, down: dict[str, str], fake: SwapFake
) -> None:
    fake.plan = SwapPlan(supported=False, reason="Cannot see the data folder")
    result = await open_down_repair(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "swap_unsupported"
    assert result["description_placeholders"]["reason"] == "Cannot see the data folder"
    assert fake.calls == []


async def test_no_backups_aborts(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry, fake: SwapFake
) -> None:
    coord = await setup_rg(hass, mock_config_entry)
    await go_down(hass, freezer, coord, fake)
    result = await open_down_repair(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_backups"


async def test_busy_source_aborts_swap_busy(
    hass: HomeAssistant, down: dict[str, str], fake: SwapFake, mock_config_entry: MockConfigEntry
) -> None:
    mgr = repairs_flow_manager(hass)
    result = await open_down_repair(hass)
    mock_config_entry.runtime_data.states[SID].running = True
    result = await mgr.async_configure(result["flow_id"], {"file": down["newest"]})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "swap_busy"
    assert "busy" in result["description_placeholders"]["error"]
    assert fake.calls == []


async def test_blocked_preflight_goes_back_to_pick(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry, fake: SwapFake, backup_root: Path
) -> None:
    old = seed_backup(backup_root, timedelta(days=10), {"ieee": OLD_IEEE, "device_count": 12})
    coord = await setup_rg(hass, mock_config_entry)
    await go_down(hass, freezer, coord, fake)
    mgr = repairs_flow_manager(hass)
    result = await replace_to_swap(hass, old, [])
    result = await mgr.async_configure(result["flow_id"], {"port": NEW_PORT, "unplugged": True})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "wiz_blocked"
    assert "days old" in result["description_placeholders"]["reasons"]
    result = await mgr.async_configure(result["flow_id"], {})
    assert result["step_id"] == "wiz_pick"
    assert fake.applied == []  # nothing was written


async def test_blocked_preflight_hides_noise_reasons(
    hass: HomeAssistant, down: dict[str, str], fake: SwapFake
) -> None:
    """A block that only consists of pre-flight noise does not stop the wizard."""
    fake.check_reasons = ["Live coordinator already has this backup's IEEE and network; ..."]
    mgr = repairs_flow_manager(hass)
    result = await replace_to_swap(hass, down["newest"], [])
    result = await mgr.async_configure(result["flow_id"], {"port": NEW_PORT, "unplugged": True})
    assert result["step_id"] == "wiz_confirm"


async def test_verify_failure_replace_restarts_and_aborts(
    hass: HomeAssistant, down: dict[str, str], fake: SwapFake, mock_config_entry: MockConfigEntry
) -> None:
    fake.verify_error = RuntimeError("Zigbee2MQTT started a different network")
    st = mock_config_entry.runtime_data.states[SID]
    mgr = repairs_flow_manager(hass)
    result = await replace_to_swap(hass, down["newest"], [])
    result = await mgr.async_configure(result["flow_id"], {"port": NEW_PORT, "unplugged": True})
    result = await step(hass, mgr, result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "swap_failed"
    assert "different network" in result["description_placeholders"]["error"]
    assert swap_calls(fake) == ["stop", "apply", "start", "wait_started", "verify", "start"]
    assert not st.swapping  # swap_end ran
    assert fake.health is True  # restarted on the old network files


async def test_stop_failure_aborts_and_restarts(
    hass: HomeAssistant, down: dict[str, str], fake: SwapFake, mock_config_entry: MockConfigEntry
) -> None:
    async def boom(plan: SwapPlan) -> None:
        fake.calls.append("stop")
        raise RuntimeError("supervisor said no")

    fake.async_swap_stop = boom
    mgr = repairs_flow_manager(hass)
    result = await open_down_repair(hass)
    result = await step(hass, mgr, result["flow_id"], {"file": down["newest"]})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "swap_failed"
    assert result["description_placeholders"]["error"] == "supervisor said no"
    assert swap_calls(fake) == ["stop", "start"]
    assert not mock_config_entry.runtime_data.states[SID].swapping


async def test_closing_after_stop_restarts_source(
    hass: HomeAssistant, down: dict[str, str], fake: SwapFake, mock_config_entry: MockConfigEntry
) -> None:
    st = mock_config_entry.runtime_data.states[SID]
    mgr = repairs_flow_manager(hass)
    result = await replace_to_swap(hass, down["newest"], [])
    assert result["step_id"] == "wiz_swap"
    assert st.swapping
    mgr.async_abort(result["flow_id"])
    await hass.async_block_till_done()
    assert swap_calls(fake) == ["stop", "start"]
    assert not st.swapping


async def test_closing_during_stop_cancels_and_restarts(
    hass: HomeAssistant, down: dict[str, str], fake: SwapFake, mock_config_entry: MockConfigEntry
) -> None:
    st = mock_config_entry.runtime_data.states[SID]
    fake.gates["stop"] = asyncio.Event()  # never released
    mgr = repairs_flow_manager(hass)
    result = await open_down_repair(hass)
    result = await mgr.async_configure(result["flow_id"], {"file": down["newest"]})
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await asyncio.sleep(0)
    assert fake.calls == ["stop"]
    mgr.async_abort(result["flow_id"])
    await hass.async_block_till_done()
    assert swap_calls(fake) == ["stop", "start"]
    assert not st.swapping


async def test_closing_manual_plan_only_ends_swap(
    hass: HomeAssistant, down: dict[str, str], fake: SwapFake, mock_config_entry: MockConfigEntry
) -> None:
    fake.plan = SwapPlan(supported=True, automatic=False, choose_port=True, current_port=OLD_PORT)
    st = mock_config_entry.runtime_data.states[SID]
    mgr = repairs_flow_manager(hass)
    result = await replace_to_swap(hass, down["newest"], [])
    mgr.async_abort(result["flow_id"])
    await hass.async_block_till_done()
    assert swap_calls(fake) == ["stop"]  # the user owns start/stop on a manual install
    assert not st.swapping


async def test_closing_before_stop_does_nothing(
    hass: HomeAssistant, down: dict[str, str], fake: SwapFake
) -> None:
    mgr = repairs_flow_manager(hass)
    result = await open_down_repair(hass)
    mgr.async_abort(result["flow_id"])
    await hass.async_block_till_done()
    assert fake.calls == []


# ================================================================ migrate (options)


async def open_options_migrate(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    mgr = hass.config_entries.options
    result = await mgr.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["settings", "replace", "migrate"]
    return await mgr.async_configure(result["flow_id"], {"next_step_id": "migrate"})


@pytest.fixture
async def running(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, fake: SwapFake, backup_root: Path
) -> RadioGuardCoordinator:
    seed_backup(backup_root, timedelta(hours=2), {"ieee": OLD_IEEE, "device_count": 12})
    return await setup_rg(hass, mock_config_entry)


async def migrate_to_confirm(
    hass: HomeAssistant, entry: MockConfigEntry, fake: SwapFake, seen: list
) -> dict[str, Any]:
    mgr = hass.config_entries.options
    result = await open_options_migrate(hass, entry)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "wiz_migrate"
    assert result["last_step"] is False
    assert result["description_placeholders"]["ieee"] == OLD_IEEE
    assert result["description_placeholders"]["port"] == OLD_PORT
    result = await step(hass, mgr, result["flow_id"], {}, seen)
    assert result["step_id"] == "wiz_swap_migrate"
    return await mgr.async_configure(result["flow_id"], {"port": NEW_PORT, "unplugged": True})


@pytest.mark.parametrize(("health", "available"), [(False, True), (True, False)])
async def test_migrate_needs_running_source(
    hass: HomeAssistant,
    running: RadioGuardCoordinator,
    fake: SwapFake,
    mock_config_entry: MockConfigEntry,
    health: bool,
    available: bool,
) -> None:
    fake.health = health
    fake.is_available = available
    await refresh(hass, running)
    result = await open_options_migrate(hass, mock_config_entry)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "migrate_needs_running"
    assert fake.backup_calls == 0


async def test_migrate_unknown_health_is_allowed(
    hass: HomeAssistant, running: RadioGuardCoordinator, fake: SwapFake, mock_config_entry: MockConfigEntry
) -> None:
    fake.health = None
    result = await open_options_migrate(hass, mock_config_entry)
    assert result["step_id"] == "wiz_migrate"


async def test_migrate_takes_fresh_backup_then_swaps(
    hass: HomeAssistant, running: RadioGuardCoordinator, fake: SwapFake, mock_config_entry: MockConfigEntry
) -> None:
    seen: list[tuple[str, str]] = []
    result = await migrate_to_confirm(hass, mock_config_entry, fake, seen)
    assert seen == [("wiz_backup", "backing_up"), ("wiz_stop", "stopping")]
    assert fake.backup_calls == 1
    assert result["step_id"] == "wiz_confirm"
    fresh = running.states[SID].records[0]
    assert fresh.path.read_bytes() == b"fresh-backup"
    assert result["description_placeholders"]["file"].startswith("Today ")


async def test_migrate_fresh_backup_failure_changes_nothing(
    hass: HomeAssistant, running: RadioGuardCoordinator, fake: SwapFake, mock_config_entry: MockConfigEntry
) -> None:
    fake.backup_error = RuntimeError("bridge timeout")
    mgr = hass.config_entries.options
    result = await open_options_migrate(hass, mock_config_entry)
    result = await step(hass, mgr, result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "swap_failed"
    assert "nothing was changed" in result["description_placeholders"]["error"]
    assert fake.calls == []
    assert not running.states[SID].swapping


async def test_migrate_busy_after_fresh_backup_aborts_cleanly(
    hass: HomeAssistant, running: RadioGuardCoordinator, fake: SwapFake, mock_config_entry: MockConfigEntry
) -> None:
    mgr = hass.config_entries.options
    result = await open_options_migrate(hass, mock_config_entry)
    fake.gates["backup"] = asyncio.Event()
    result = await mgr.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    running.states[SID].restoring = True  # e.g. a service-call restore started meanwhile
    fake.gates["backup"].set()
    await hass.async_block_till_done()
    result = await mgr.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "swap_failed"
    assert "busy" in result["description_placeholders"]["error"]
    assert not running.states[SID].swapping  # nothing was stopped, nothing left half-done


async def test_migrate_reaches_done_menu(
    hass: HomeAssistant, running: RadioGuardCoordinator, fake: SwapFake, mock_config_entry: MockConfigEntry
) -> None:
    result = await migrate_to_confirm(hass, mock_config_entry, fake, [])
    result = await step(hass, hass.config_entries.options, result["flow_id"], {})
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "wiz_done_migrate"


async def test_migrate_failure_reaches_failed_menu(
    hass: HomeAssistant, running: RadioGuardCoordinator, fake: SwapFake, mock_config_entry: MockConfigEntry
) -> None:
    fake.verify_error = RuntimeError("different network")
    result = await migrate_to_confirm(hass, mock_config_entry, fake, [])
    result = await step(hass, hass.config_entries.options, result["flow_id"], {})
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "wiz_failed_migrate"


@pytest.mark.usefixtures("shim_migrate_steps")
async def test_migrate_done_then_finish(
    hass: HomeAssistant, running: RadioGuardCoordinator, fake: SwapFake, mock_config_entry: MockConfigEntry
) -> None:
    mgr = hass.config_entries.options
    result = await migrate_to_confirm(hass, mock_config_entry, fake, [])
    seen: list[tuple[str, str]] = []
    result = await step(hass, mgr, result["flow_id"], {}, seen)
    assert seen == [("wiz_restore", "restoring"), ("wiz_devices", "checking_devices")]
    assert result["step_id"] == "wiz_done_migrate"
    assert result["menu_options"] == ["wiz_finish", "wiz_devices", "wiz_rollback"]
    assert fake.backup_calls == 2  # before the move, and of the new coordinator after it
    options_before = dict(mock_config_entry.options)
    result = await mgr.async_configure(result["flow_id"], {"next_step_id": "wiz_finish"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == options_before  # settings untouched


@pytest.mark.usefixtures("shim_migrate_steps")
async def test_migrate_rollback_restores_old_coordinator(
    hass: HomeAssistant, running: RadioGuardCoordinator, fake: SwapFake, mock_config_entry: MockConfigEntry
) -> None:
    mgr = hass.config_entries.options
    result = await migrate_to_confirm(hass, mock_config_entry, fake, [])
    result = await step(hass, mgr, result["flow_id"], {})
    assert result["step_id"] == "wiz_done_migrate"
    assert fake.port == NEW_PORT
    fake.calls.clear()
    fake.wait_timeouts.clear()

    seen: list[tuple[str, str]] = []
    result = await step(hass, mgr, result["flow_id"], {"next_step_id": "wiz_rollback"}, seen)
    assert seen == [("wiz_rollback", "stopping")]
    assert result["step_id"] == "wiz_rollback_swap"
    assert result["description_placeholders"]["old_port"] == OLD_PORT
    result = await mgr.async_configure(result["flow_id"], {})
    assert result["errors"] == {"swapped_back": "unplugged_required"}

    seen.clear()
    result = await step(hass, mgr, result["flow_id"], {"swapped_back": True}, seen)
    assert seen == [("wiz_rollback_start", "starting_old")]
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "rolled_back"
    assert result["description_placeholders"]["port"] == OLD_PORT
    assert swap_calls(fake) == ["stop", f"set_port:{OLD_PORT}", "start", "wait_started", "verify"]
    assert fake.port == OLD_PORT
    assert fake.wait_timeouts == [180]
    assert not running.states[SID].swapping


@pytest.mark.usefixtures("shim_migrate_steps")
async def test_rollback_after_success_marks_source_swapping(
    hass: HomeAssistant, running: RadioGuardCoordinator, fake: SwapFake, mock_config_entry: MockConfigEntry
) -> None:
    mgr = hass.config_entries.options
    result = await migrate_to_confirm(hass, mock_config_entry, fake, [])
    result = await step(hass, mgr, result["flow_id"], {})
    fake.gates["stop"] = asyncio.Event()
    try:
        result = await mgr.async_configure(result["flow_id"], {"next_step_id": "wiz_rollback"})
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        assert running.states[SID].swapping
    finally:
        fake.gates["stop"].set()
        await hass.async_block_till_done()


@pytest.mark.usefixtures("shim_migrate_steps")
async def test_migrate_verify_failure_offers_rollback(
    hass: HomeAssistant, running: RadioGuardCoordinator, fake: SwapFake, mock_config_entry: MockConfigEntry
) -> None:
    fake.verify_error = RuntimeError("different network")
    mgr = hass.config_entries.options
    result = await migrate_to_confirm(hass, mock_config_entry, fake, [])
    result = await step(hass, mgr, result["flow_id"], {})
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "wiz_failed_migrate"
    assert result["menu_options"] == ["wiz_rollback", "wiz_close"]
    assert result["description_placeholders"]["error"] == "different network"
    assert running.states[SID].swapping  # still mid-swap until the user decides

    fake.verify_error = None
    fake.calls.clear()
    result = await step(hass, mgr, result["flow_id"], {"next_step_id": "wiz_rollback"})
    assert result["step_id"] == "wiz_rollback_swap"
    result = await step(hass, mgr, result["flow_id"], {"swapped_back": True})
    assert result["reason"] == "rolled_back"
    assert swap_calls(fake) == ["stop", f"set_port:{OLD_PORT}", "start", "wait_started", "verify"]
    assert fake.port == OLD_PORT
    assert not running.states[SID].swapping


@pytest.mark.usefixtures("shim_migrate_steps")
async def test_migrate_failure_close_puts_old_port_back(
    hass: HomeAssistant, running: RadioGuardCoordinator, fake: SwapFake, mock_config_entry: MockConfigEntry
) -> None:
    fake.verify_error = RuntimeError("different network")
    mgr = hass.config_entries.options
    result = await migrate_to_confirm(hass, mock_config_entry, fake, [])
    result = await step(hass, mgr, result["flow_id"], {})
    fake.calls.clear()
    result = await mgr.async_configure(result["flow_id"], {"next_step_id": "wiz_close"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "swap_failed"
    assert swap_calls(fake) == [f"set_port:{OLD_PORT}", "start"]
    assert not running.states[SID].swapping


async def test_options_replace_goes_to_pick(
    hass: HomeAssistant, running: RadioGuardCoordinator, mock_config_entry: MockConfigEntry
) -> None:
    mgr = hass.config_entries.options
    result = await mgr.async_init(mock_config_entry.entry_id)
    result = await mgr.async_configure(result["flow_id"], {"next_step_id": "replace"})
    assert result["step_id"] == "wiz_pick"


# ======================================================= coordinator-down detection


@pytest.fixture
async def coord(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, fake: SwapFake, backup_root: Path
) -> RadioGuardCoordinator:
    seed_backup(backup_root, timedelta(hours=2), {"ieee": OLD_IEEE, "device_count": 12})
    return await setup_rg(hass, mock_config_entry)


async def test_unknown_health_never_counts(
    hass: HomeAssistant, freezer, coord: RadioGuardCoordinator, fake: SwapFake
) -> None:
    fake.health = None
    for _ in range(4):
        freezer.tick(timedelta(minutes=10))
        await refresh(hass, coord)
    assert coord.states[SID].down_since is None
    assert issue(hass, DOWN) is None


async def test_health_exception_is_unknown(
    hass: HomeAssistant, freezer, coord: RadioGuardCoordinator, fake: SwapFake
) -> None:
    async def boom() -> bool:
        raise RuntimeError("broker gone")

    fake.async_health = boom
    freezer.tick(timedelta(minutes=STARTUP_GRACE_MINUTES + 1))
    await refresh(hass, coord)
    assert coord.states[SID].down_since is None


async def test_down_only_counts_after_startup_grace(
    hass: HomeAssistant, freezer, coord: RadioGuardCoordinator, fake: SwapFake
) -> None:
    fake.health = False
    await refresh(hass, coord)
    assert coord.states[SID].down_since is None
    freezer.tick(timedelta(minutes=STARTUP_GRACE_MINUTES) - timedelta(seconds=1))
    await refresh(hass, coord)
    assert coord.states[SID].down_since is None
    freezer.tick(timedelta(seconds=1))
    await refresh(hass, coord)
    assert coord.states[SID].down_since == dt_util.utcnow()


async def test_down_for_offline_minutes_raises_issue(
    hass: HomeAssistant, freezer, coord: RadioGuardCoordinator, fake: SwapFake
) -> None:
    fake.health = False
    freezer.tick(timedelta(minutes=STARTUP_GRACE_MINUTES))
    await refresh(hass, coord)
    freezer.tick(timedelta(minutes=DEFAULT_OFFLINE_MINUTES) - timedelta(seconds=1))
    await refresh(hass, coord)
    assert issue(hass, DOWN) is None
    assert not coord.coordinator_down(coord.states[SID])
    freezer.tick(timedelta(seconds=1))
    await refresh(hass, coord)
    found = issue(hass, DOWN)
    assert found is not None
    assert found.severity is ir.IssueSeverity.ERROR
    assert found.is_fixable
    assert found.translation_key == ISSUE_COORDINATOR_DOWN
    assert found.data == {"source": SID, "action": "replace"}
    assert found.translation_placeholders["source"] == "Zigbee2MQTT"
    assert found.translation_placeholders["minutes"] == str(DEFAULT_OFFLINE_MINUTES)


async def test_one_healthy_reading_resets(
    hass: HomeAssistant, freezer, coord: RadioGuardCoordinator, fake: SwapFake
) -> None:
    await go_down(hass, freezer, coord, fake)
    fake.health = True
    await refresh(hass, coord)
    assert coord.states[SID].down_since is None
    assert issue(hass, DOWN) is None
    # down again: the clock starts over
    fake.health = False
    await refresh(hass, coord)
    freezer.tick(timedelta(minutes=DEFAULT_OFFLINE_MINUTES - 1))
    await refresh(hass, coord)
    assert issue(hass, DOWN) is None


async def test_unknown_reading_also_resets(
    hass: HomeAssistant, freezer, coord: RadioGuardCoordinator, fake: SwapFake
) -> None:
    await go_down(hass, freezer, coord, fake)
    fake.health = None
    await refresh(hass, coord)
    assert coord.states[SID].down_since is None
    assert issue(hass, DOWN) is None


async def test_backup_nags_suppressed_while_down(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry, fake: SwapFake
) -> None:
    coord = await setup_rg(hass, mock_config_entry)  # no backups at all
    assert issue(hass, NEVER) is not None
    await go_down(hass, freezer, coord, fake)
    assert issue(hass, NEVER) is None
    fake.health = True
    await refresh(hass, coord)
    assert issue(hass, NEVER) is not None


async def test_stale_suppressed_and_failed_deleted_while_down(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry, fake: SwapFake, backup_root: Path
) -> None:
    seed_backup(backup_root, timedelta(hours=72), {"ieee": OLD_IEEE, "device_count": 12})
    coord = await setup_rg(hass, mock_config_entry)
    assert issue(hass, STALE) is not None
    fake.backup_error = RuntimeError("bridge timeout")
    with pytest.raises(RuntimeError):
        await coord.async_backup(SID)
    assert issue(hass, FAILED) is not None
    await go_down(hass, freezer, coord, fake)
    assert issue(hass, STALE) is None
    assert issue(hass, FAILED) is None


async def test_nothing_counts_while_busy(
    hass: HomeAssistant, freezer, coord: RadioGuardCoordinator, fake: SwapFake
) -> None:
    st = coord.states[SID]
    fake.health = False
    freezer.tick(timedelta(minutes=STARTUP_GRACE_MINUTES))
    for flag in ("running", "restoring", "swapping"):
        setattr(st, flag, True)
        freezer.tick(timedelta(minutes=DEFAULT_OFFLINE_MINUTES + 1))
        await refresh(hass, coord)
        assert st.down_since is None, flag
        assert issue(hass, DOWN) is None, flag
        setattr(st, flag, False)


async def test_down_since_not_reported_while_busy(
    hass: HomeAssistant, freezer, coord: RadioGuardCoordinator, fake: SwapFake
) -> None:
    await go_down(hass, freezer, coord, fake)
    st = coord.states[SID]
    st.running = True
    assert not coord.coordinator_down(st)
    st.running = False
    coord.async_swap_begin(SID)
    assert st.down_since is None
    await coord.async_swap_end(SID, None)


async def test_scheduled_backup_skipped_while_down(
    hass: HomeAssistant, freezer, coord: RadioGuardCoordinator, fake: SwapFake
) -> None:
    fake.health = False
    freezer.tick(timedelta(minutes=STARTUP_GRACE_MINUTES))
    await refresh(hass, coord)
    assert coord.states[SID].down_since is not None
    results = await coord.async_backup_all(scheduled=True)
    assert isinstance(results[SID], SourceUnavailable)
    assert fake.backup_calls == 0
    # a manual "back up all" still runs
    await coord.async_backup_all()
    assert fake.backup_calls == 1


async def test_scheduled_backup_skipped_while_swapping(
    hass: HomeAssistant, coord: RadioGuardCoordinator, fake: SwapFake
) -> None:
    coord.async_swap_begin(SID)
    results = await coord.async_backup_all(scheduled=True)
    assert isinstance(results[SID], SourceUnavailable)
    assert fake.backup_calls == 0
    await coord.async_swap_end(SID, None)
    assert not coord.states[SID].swapping
    assert fake.backup_calls == 0  # swap_end without a record takes no backup


async def test_swap_begin_refuses_when_busy(hass: HomeAssistant, coord: RadioGuardCoordinator) -> None:
    coord.states[SID].running = True
    with pytest.raises(RuntimeError, match="busy"):
        coord.async_swap_begin(SID)


# ======================================================= coordinator_replaced debounce


async def test_coordinator_replaced_needs_two_readings(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, fake: SwapFake, backup_root: Path
) -> None:
    assert REPLACED_CONFIRMATIONS == 2
    seed_backup(backup_root, timedelta(hours=2), {"ieee": OLD_IEEE, "device_count": 12})
    fake.identity = {"ieee": NEW_IEEE, "device_count": 1}
    coord = await setup_rg(hass, mock_config_entry)  # first reading
    assert coord.states[SID].replaced_seen == 1
    assert issue(hass, REPLACED) is None
    await refresh(hass, coord)
    assert issue(hass, REPLACED) is not None


async def test_coordinator_replaced_counter_resets_on_match(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, fake: SwapFake, backup_root: Path
) -> None:
    seed_backup(backup_root, timedelta(hours=2), {"ieee": OLD_IEEE, "device_count": 12})
    fake.identity = {"ieee": NEW_IEEE}
    coord = await setup_rg(hass, mock_config_entry)
    fake.identity = {"ieee": OLD_IEEE}  # one blip back to the expected stick
    await refresh(hass, coord)
    assert coord.states[SID].replaced_seen == 0
    fake.identity = {"ieee": NEW_IEEE}
    await refresh(hass, coord)
    assert issue(hass, REPLACED) is None


async def test_coordinator_replaced_not_raised_while_swapping(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, fake: SwapFake, backup_root: Path
) -> None:
    seed_backup(backup_root, timedelta(hours=2), {"ieee": OLD_IEEE, "device_count": 12})
    fake.identity = {"ieee": NEW_IEEE}
    coord = await setup_rg(hass, mock_config_entry)
    coord.async_swap_begin(SID)
    await refresh(hass, coord)
    assert issue(hass, REPLACED) is None


async def test_swap_end_with_record_clears_issues(
    hass: HomeAssistant, freezer, coord: RadioGuardCoordinator, fake: SwapFake
) -> None:
    events = async_capture_events(hass, EVENT_RESTORE_COMPLETE)
    await go_down(hass, freezer, coord, fake)
    st = coord.states[SID]
    record = st.records[0]
    coord.async_swap_begin(SID)
    fake.health = True
    fresh = await coord.async_swap_end(SID, record)
    await hass.async_block_till_done()
    assert fresh is not None and fresh.path.read_bytes() == b"fresh-backup"
    assert issue(hass, DOWN) is None
    assert st.replaced_seen == 0
    assert [e.data["file"] for e in events] == [str(record.path)]


# ==================================================================== strings

WIZ_FORM_MENU_STEPS = {
    "wiz_pick",
    "wiz_migrate",
    "wiz_swap",
    "wiz_swap_migrate",
    "wiz_confirm",
    "wiz_blocked",
    "wiz_done",
    "wiz_done_migrate",
    "wiz_rollback_swap",
    "wiz_failed_migrate",
}
WIZ_PROGRESS = {
    "stopping",
    "wait_stopped",
    "backing_up",
    "restoring",
    "writing",
    "wait_started",
    "checking_devices",
    "starting_old",
}
WIZ_ERRORS = {"unplugged_required"}
WIZ_ABORTS = {
    "swap_unsupported",
    "no_backups",
    "swap_busy",
    "swap_failed",
    "rolled_back",
    "migrate_needs_running",
}
WIZ_MENUS = {
    "wiz_done": ["wiz_finish", "wiz_devices"],
    "wiz_done_migrate": ["wiz_finish", "wiz_devices", "wiz_rollback"],
    "wiz_failed_migrate": ["wiz_rollback", "wiz_close"],
}


def test_explicit_lists_cover_wizard_source() -> None:
    """Guard the explicit lists below against drift in wizard.py."""
    src = WIZARD_PY.read_text()
    shown = set(re.findall(r'(?<!next_)step_id="(wiz_\w+)"', src))
    shown |= {
        a
        for pair in re.findall(r'"(wiz_\w+)" if self\._mode == MODE_\w+ else "(wiz_\w+)"', src)
        for a in pair
    }
    assert shown <= WIZ_FORM_MENU_STEPS, shown - WIZ_FORM_MENU_STEPS
    progress = set(re.findall(r'_progress\(\s*"wiz_\w+",\s*"(\w+)"', src))
    progress |= {
        a for pair in re.findall(r'"(\w+)" if (?:self\._plan\.)?automatic else "(\w+)"', src) for a in pair
    }
    assert progress <= WIZ_PROGRESS, progress - WIZ_PROGRESS
    assert set(re.findall(r'errors\["\w+"\] = "(\w+)"', src)) <= WIZ_ERRORS
    assert set(re.findall(r'reason="(\w+)"', src)) <= WIZ_ABORTS
    assert set(re.findall(r'options\.append\("(wiz_\w+)"\)', src)) <= set(WIZ_MENUS["wiz_done_migrate"])


@pytest.mark.parametrize(
    "path",
    [
        ("options",),
        ("issues", "coordinator_replaced", "fix_flow"),
        ("issues", "coordinator_down", "fix_flow"),
    ],
)
def test_wizard_strings_exist(path: tuple[str, ...]) -> None:
    flow = json.loads(STRINGS.read_text())
    for part in path:
        flow = flow[part]
    assert WIZ_FORM_MENU_STEPS <= set(flow["step"]), WIZ_FORM_MENU_STEPS - set(flow["step"])
    assert WIZ_PROGRESS <= set(flow["progress"]), WIZ_PROGRESS - set(flow["progress"])
    assert WIZ_ERRORS <= set(flow["error"])
    assert WIZ_ABORTS <= set(flow["abort"]), WIZ_ABORTS - set(flow["abort"])
    for menu, options in WIZ_MENUS.items():
        assert set(options) <= set(flow["step"][menu].get("menu_options", {})), (menu, options)
    for sid in ("wiz_swap", "wiz_swap_migrate"):
        assert {"port", "unplugged"} <= set(flow["step"][sid]["data"])
    assert "swapped_back" in flow["step"]["wiz_rollback_swap"]["data"]
    assert "file" in flow["step"]["wiz_pick"]["data"]


def test_flow_specific_strings() -> None:
    strings = json.loads(STRINGS.read_text())
    options = strings["options"]
    assert {"settings", "replace", "migrate"} <= set(options["step"]["init"]["menu_options"])
    assert "pick_source" in options["step"]
    assert "not_loaded" in options["abort"]
    down = strings["issues"]["coordinator_down"]
    assert "title" in down
    assert {"wiz_pick", "wait"} <= set(down["fix_flow"]["step"]["init"]["menu_options"])
    assert "keep_waiting" in down["fix_flow"]["abort"]


async def test_progress_tasks_take_a_minimum_time() -> None:
    """A task that finishes before the frontend subscribes would leave the dialog spinning forever."""
    import time

    from custom_components.radio_guard.wizard import _at_least

    async def fast() -> str:
        return "done"

    t0 = time.monotonic()
    assert await _at_least(fast(), 0.05) == "done"
    assert time.monotonic() - t0 >= 0.05
