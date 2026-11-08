"""ZHA guided-replacement API (health, plan, stop/set-port/apply/start/verify, device check) and the wizard.

``homeassistant.components.zha`` is importable in the venv, so the real ``ZhaRadioManager`` and
``ProbeResult`` are used; only the radio-touching methods (probe, load settings, restore) are
patched. ZHA config entries are real ``MockConfigEntry`` objects in the state under test.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.components.zha.radio_manager import ProbeResult, ZhaRadioManager
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from zha.application.const import RadioType
from zigpy.exceptions import CannotWriteNetworkSettings, DestructiveWriteNetworkSettings

from custom_components.radio_guard.backup import zha as zha_mod
from custom_components.radio_guard.backup.base import (
    ConfirmationNeeded,
    DeviceReport,
    PortOption,
    SourceUnavailable,
    SwapPlan,
)
from custom_components.radio_guard.backup.zha import IEEE_KEY, ZHASource
from custom_components.radio_guard.const import (
    CONF_SOURCES,
    DEFAULT_OFFLINE_MINUTES,
    DOMAIN,
    ISSUE_COORDINATOR_DOWN,
    SOURCE_ZHA,
    STARTUP_GRACE_MINUTES,
)
from custom_components.radio_guard.coordinator import RadioGuardCoordinator
from custom_components.radio_guard.hardware import Candidate

from .test_wizard import SwapFake
from .test_zha import EPID_NORM, IEEE_NORM, backup_bytes, make_backup

ZHA_MOD = "custom_components.radio_guard.backup.zha"
FIND = "custom_components.radio_guard.hardware.async_find"
GET_GATEWAY = "homeassistant.components.zha.helpers.get_zha_gateway"
GET_ENTRY = "homeassistant.components.zha.helpers.get_config_entry"
OLD_PORT = "/dev/serial/by-id/usb-old-znp"
NEW_PORT = "/dev/serial/by-id/usb-new-ezsp"
DOWN = f"{ISSUE_COORDINATOR_DOWN}_{SOURCE_ZHA}"
OTHER_EPID = "eeeeeeeeeeeeeeee"


# ------------------------------------------------------------------ helpers


class _FastAsyncio:
    """Stand-in for ``asyncio`` inside zha.py: real everything, ``sleep`` yields once (and can hook)."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []
        self.on_sleep: Any = None

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    async def sleep(self, delay: float, result: Any = None) -> Any:
        self.sleeps.append(delay)
        if self.on_sleep is not None:
            self.on_sleep(len(self.sleeps))
        await asyncio.sleep(0)
        return result


@pytest.fixture
def fast_sleep() -> _FastAsyncio:
    proxy = _FastAsyncio()
    with patch(f"{ZHA_MOD}.asyncio", proxy):
        yield proxy


def zha_entry(
    hass: HomeAssistant,
    *,
    state: ConfigEntryState = ConfigEntryState.LOADED,
    path: str | None = OLD_PORT,
    radio_type: str = "znp",
    disabled: bool = False,
    entry_id: str = "zha_entry",
) -> MockConfigEntry:
    data: dict[str, Any] = {"radio_type": radio_type}
    if path is not None:
        data["device"] = {"path": path, "baudrate": 115200, "flow_control": None}
    entry = MockConfigEntry(
        domain="zha",
        title="ZHA",
        data=data,
        state=state,
        entry_id=entry_id,
        disabled_by=ConfigEntryDisabler.USER if disabled else None,
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def source(hass: HomeAssistant) -> ZHASource:
    return ZHASource(hass)


@pytest.fixture
def entries(hass: HomeAssistant):
    """Patch config-entry unload/setup (they would try to load the real ZHA integration)."""
    unload = AsyncMock(return_value=True)
    setup = AsyncMock(return_value=True)
    with (
        patch.object(hass.config_entries, "async_unload", unload),
        patch.object(hass.config_entries, "async_setup", setup),
    ):
        yield SimpleNamespace(unload=unload, setup=setup)


def probe(result: ProbeResult = ProbeResult.RADIO_TYPE_DETECTED, radio: RadioType = RadioType.ezsp):
    """Patch ZhaRadioManager.detect_radio_type the way ZHA's probe mutates the manager."""
    seen: list[str | None] = []

    async def _detect(self: ZhaRadioManager) -> ProbeResult:
        seen.append(self.device_path)
        if result is ProbeResult.RADIO_TYPE_DETECTED:
            self.radio_type = radio
            self.device_settings = {"path": self.device_path, "baudrate": 460800, "flow_control": "hardware"}
        return result

    mock = patch.object(ZhaRadioManager, "detect_radio_type", autospec=True, side_effect=_detect)
    return mock, seen


@pytest.fixture
def radio():
    """Patch the radio-touching manager calls; record them."""
    load = AsyncMock(return_value=None)
    restore = AsyncMock(return_value=None)
    det, seen = probe()
    with (
        det as detect,
        patch.object(ZhaRadioManager, "async_load_network_settings", load),
        patch.object(ZhaRadioManager, "restore_backup", restore),
    ):
        yield SimpleNamespace(detect=detect, probed=seen, load=load, restore=restore)


def gateway_with(*devices: Any, app: Any = None) -> SimpleNamespace:
    return SimpleNamespace(
        devices={i: d for i, d in enumerate(devices)},
        application_controller=app if app is not None else MagicMock(),
    )


def live_app(backup=None) -> MagicMock:
    backup = backup or make_backup()
    app = MagicMock()
    app.state = SimpleNamespace(network_info=backup.network_info, node_info=backup.node_info)
    app.backups.create_backup = AsyncMock(return_value=backup)
    return app


# ===================================================================== health


async def test_health_without_entry(source: ZHASource) -> None:
    assert source._any_entry() is None
    assert await source.async_health() is None


@pytest.mark.parametrize("state", [ConfigEntryState.SETUP_RETRY, ConfigEntryState.SETUP_ERROR])
async def test_health_down_states(hass: HomeAssistant, source: ZHASource, state) -> None:
    zha_entry(hass, state=state)
    assert await source.async_health() is False


async def test_health_loaded(hass: HomeAssistant, source: ZHASource) -> None:
    zha_entry(hass, state=ConfigEntryState.LOADED)
    assert await source.async_health() is True


@pytest.mark.parametrize("state", [ConfigEntryState.SETUP_IN_PROGRESS, ConfigEntryState.NOT_LOADED])
async def test_health_unknown_states(hass: HomeAssistant, source: ZHASource, state) -> None:
    zha_entry(hass, state=state)
    assert await source.async_health() is None


async def test_health_ignores_disabled_entry(hass: HomeAssistant, source: ZHASource) -> None:
    zha_entry(hass, state=ConfigEntryState.SETUP_RETRY, disabled=True)
    assert source._any_entry() is None
    assert await source.async_health() is None
    live = zha_entry(hass, state=ConfigEntryState.LOADED, entry_id="zha_live")
    assert source._any_entry() is live
    assert await source.async_health() is True


# ======================================================================= plan


async def test_swap_plan_without_entry(source: ZHASource) -> None:
    plan = await source.async_swap_plan()
    assert plan.supported is False
    assert "not set up" in plan.reason


async def test_swap_plan_with_entry(hass: HomeAssistant, source: ZHASource) -> None:
    zha_entry(hass, state=ConfigEntryState.SETUP_RETRY)
    plan = await source.async_swap_plan()
    assert plan.supported is True
    assert plan.automatic is True
    assert plan.choose_port is True
    assert plan.current_port == OLD_PORT
    assert plan.stop_first is True


async def test_swap_plan_entry_without_device(hass: HomeAssistant, source: ZHASource) -> None:
    zha_entry(hass, path=None)
    plan = await source.async_swap_plan()
    assert plan.supported is True
    assert plan.current_port is None


async def test_source_tech_is_zigbee(source: ZHASource) -> None:
    assert source.tech == "zigbee"


# ======================================================================= stop


@pytest.mark.parametrize("state", [ConfigEntryState.LOADED, ConfigEntryState.SETUP_RETRY])
async def test_swap_stop_unloads_and_snapshots(
    hass: HomeAssistant, source: ZHASource, entries, state
) -> None:
    entry = zha_entry(hass, state=state)
    find = AsyncMock(return_value=[Candidate(OLD_PORT, "Old stick", "zigbee")])
    with patch(FIND, find):
        source.swap_reset()
        await source.async_swap_stop(SwapPlan(supported=True, automatic=True))
    entries.unload.assert_awaited_once_with(entry.entry_id)
    find.assert_awaited_once_with(hass, "zigbee", network=False)
    assert source._ports_before == {OLD_PORT}


async def test_swap_stop_leaves_not_loaded_entry_alone(
    hass: HomeAssistant, source: ZHASource, entries
) -> None:
    zha_entry(hass, state=ConfigEntryState.NOT_LOADED)
    with patch(FIND, AsyncMock(return_value=[])):
        source.swap_reset()
        await source.async_swap_stop(SwapPlan(supported=True, automatic=True))
    entries.unload.assert_not_awaited()
    assert source._ports_before == set()


async def test_swap_stop_without_entry_only_snapshots(source: ZHASource, entries) -> None:
    with patch(FIND, AsyncMock(return_value=[Candidate("/dev/ttyACM0", "x", None)])):
        source.swap_reset()
        await source.async_swap_stop(SwapPlan(supported=True, automatic=True))
    entries.unload.assert_not_awaited()
    assert source._ports_before == {"/dev/ttyACM0"}


async def test_swap_stop_real_unload_of_retrying_entry(hass: HomeAssistant, source: ZHASource) -> None:
    """Unmocked config-entry manager: a dead radio's retrying entry ends NOT_LOADED."""
    entry = zha_entry(hass, state=ConfigEntryState.SETUP_RETRY)
    with patch(FIND, AsyncMock(return_value=[])):
        await source.async_swap_stop(SwapPlan(supported=True, automatic=True))
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_swap_stop_waits_for_setup_attempt_then_unloads(
    hass: HomeAssistant, source: ZHASource, entries
) -> None:
    """A ZHA retry attempt probing the dead stick must finish before unloading (HA refuses mid-attempt)."""
    entry = zha_entry(hass, state=ConfigEntryState.SETUP_IN_PROGRESS)

    async def settle(_seconds: float) -> None:
        entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)

    with patch(FIND, AsyncMock(return_value=[])), patch(f"{ZHA_MOD}.asyncio.sleep", side_effect=settle):
        await source.async_swap_stop(SwapPlan(supported=True, automatic=True))
    entries.unload.assert_awaited_once_with(entry.entry_id)


async def test_swap_stop_gives_up_if_setup_never_settles(hass: HomeAssistant, source: ZHASource) -> None:
    zha_entry(hass, state=ConfigEntryState.SETUP_IN_PROGRESS)
    with (
        patch(FIND, AsyncMock(return_value=[])),
        patch(f"{ZHA_MOD}.SETTLE_TIMEOUT", 0),
        pytest.raises(RuntimeError, match="still busy starting"),
    ):
        await source.async_swap_stop(SwapPlan(supported=True, automatic=True))


async def test_swap_stop_unload_refused_raises(hass: HomeAssistant, source: ZHASource, entries) -> None:
    zha_entry(hass, state=ConfigEntryState.LOADED)
    entries.unload.return_value = False
    with patch(FIND, AsyncMock(return_value=[])), pytest.raises(RuntimeError, match="could not stop ZHA"):
        await source.async_swap_stop(SwapPlan(supported=True, automatic=True))


async def test_swap_ports_marks_new_stick(hass: HomeAssistant, source: ZHASource, entries) -> None:
    zha_entry(hass)
    old = Candidate(OLD_PORT, "Old stick", "zigbee")
    new = Candidate(NEW_PORT, "Sonoff ZBDongle-E", "zigbee")
    with patch(FIND, AsyncMock(side_effect=[[old], [old, new]])):
        source.swap_reset()
        await source.async_swap_stop(SwapPlan(supported=True, automatic=True))
        ports = await source.async_swap_ports(OLD_PORT)
    assert [(p.value, p.new) for p in ports] == [(NEW_PORT, True), (OLD_PORT, False)]


async def test_wait_stopped_is_a_no_op(source: ZHASource) -> None:
    assert await source.async_wait_stopped(5) is None
    assert await source.async_wait_stopped(None) is None


# =================================================================== set port


async def test_set_port_detects_radio_and_updates_entry(
    hass: HomeAssistant, source: ZHASource, radio
) -> None:
    entry = zha_entry(hass, radio_type="znp")
    await source.async_swap_set_port(NEW_PORT)
    assert radio.probed == [NEW_PORT]
    assert entry.data["radio_type"] == "ezsp"
    assert entry.data["device"] == {"path": NEW_PORT, "baudrate": 460800, "flow_control": "hardware"}


async def test_set_port_keeps_other_entry_data(hass: HomeAssistant, source: ZHASource, radio) -> None:
    entry = zha_entry(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, "extra": 1})
    await source.async_swap_set_port(NEW_PORT)
    assert entry.data["extra"] == 1


async def test_set_port_wrong_firmware(hass: HomeAssistant, source: ZHASource) -> None:
    entry = zha_entry(hass)
    before = dict(entry.data)
    det, _ = probe(ProbeResult.WRONG_FIRMWARE_INSTALLED)
    with det, pytest.raises(RuntimeError, match="isn't for Zigbee coordinators"):
        await source.async_swap_set_port(NEW_PORT)
    assert dict(entry.data) == before


async def test_set_port_probing_failed(hass: HomeAssistant, source: ZHASource) -> None:
    entry = zha_entry(hass)
    before = dict(entry.data)
    det, _ = probe(ProbeResult.PROBING_FAILED)
    with det, pytest.raises(RuntimeError, match=f"No Zigbee coordinator answered on {NEW_PORT}"):
        await source.async_swap_set_port(NEW_PORT)
    assert dict(entry.data) == before


async def test_set_port_noop_without_port_or_entry(hass: HomeAssistant, source: ZHASource, radio) -> None:
    await source.async_swap_set_port(NEW_PORT)  # no entry
    entry = zha_entry(hass)
    before = dict(entry.data)
    await source.async_swap_set_port(None)
    await source.async_swap_set_port("")
    radio.detect.assert_not_called()
    assert dict(entry.data) == before


# ====================================================================== apply


async def test_apply_sets_port_loads_and_restores(hass: HomeAssistant, source: ZHASource, radio) -> None:
    entry = zha_entry(hass, radio_type="znp")
    backup = make_backup()
    await source.async_swap_apply(backup_bytes(backup), {"extended_pan_id": EPID_NORM}, NEW_PORT)
    assert radio.probed == [NEW_PORT]
    assert entry.data["device"]["path"] == NEW_PORT
    assert entry.data["radio_type"] == "ezsp"
    radio.load.assert_awaited_once()
    radio.restore.assert_awaited_once()
    restored, kwargs = radio.restore.await_args.args[0], radio.restore.await_args.kwargs
    assert kwargs == {"overwrite_ieee": False}
    assert restored.network_info.extended_pan_id == backup.network_info.extended_pan_id
    assert restored.node_info.ieee == backup.node_info.ieee


async def test_apply_restores_with_manager_for_new_port(hass: HomeAssistant, source: ZHASource) -> None:
    """The restore must run on a manager built after the entry points at the new stick."""
    zha_entry(hass, radio_type="znp")
    used: list[tuple[str, RadioType]] = []

    async def _restore(self, backup, *, overwrite_ieee=False, **kw):
        used.append((self.device_path, self.radio_type))

    det, _ = probe()
    with (
        det,
        patch.object(ZhaRadioManager, "async_load_network_settings", AsyncMock()),
        patch.object(ZhaRadioManager, "restore_backup", autospec=True, side_effect=_restore),
    ):
        await source.async_swap_apply(backup_bytes(make_backup()), {}, NEW_PORT)
    assert used == [(NEW_PORT, RadioType.ezsp)]


async def test_apply_without_port_reprobes_current(hass: HomeAssistant, source: ZHASource, radio) -> None:
    entry = zha_entry(hass)
    await source.async_swap_apply(backup_bytes(make_backup()), {}, None)
    assert radio.probed == [OLD_PORT]
    assert entry.data["device"]["path"] == OLD_PORT
    radio.restore.assert_awaited_once()


async def test_apply_overwrite_ieee_from_meta(hass: HomeAssistant, source: ZHASource, radio) -> None:
    zha_entry(hass)
    await source.async_swap_apply(backup_bytes(make_backup()), {IEEE_KEY: True}, NEW_PORT)
    assert radio.restore.await_args.kwargs == {"overwrite_ieee": True}


async def test_apply_destructive_write_needs_confirmation(
    hass: HomeAssistant, source: ZHASource, radio
) -> None:
    zha_entry(hass)
    radio.restore.side_effect = DestructiveWriteNetworkSettings("IEEE can be written once")
    with pytest.raises(ConfirmationNeeded) as exc:
        await source.async_swap_apply(backup_bytes(make_backup()), {}, NEW_PORT)
    assert exc.value.key == IEEE_KEY == "allow_ieee_overwrite"
    assert "only once" in exc.value.message
    assert isinstance(exc.value.__cause__, DestructiveWriteNetworkSettings)


async def test_apply_cannot_write(hass: HomeAssistant, source: ZHASource, radio) -> None:
    zha_entry(hass)
    radio.restore.side_effect = CannotWriteNetworkSettings("nope")
    with pytest.raises(RuntimeError, match="The new stick refused the backup: nope"):
        await source.async_swap_apply(backup_bytes(make_backup()), {}, NEW_PORT)


async def test_apply_without_entry(source: ZHASource, radio) -> None:
    with pytest.raises(SourceUnavailable):
        await source.async_swap_apply(backup_bytes(make_backup()), {}, NEW_PORT)
    radio.restore.assert_not_awaited()


async def test_apply_probe_failure_writes_nothing(hass: HomeAssistant, source: ZHASource) -> None:
    zha_entry(hass)
    restore = AsyncMock()
    det, _ = probe(ProbeResult.PROBING_FAILED)
    with det, patch.object(ZhaRadioManager, "restore_backup", restore), pytest.raises(RuntimeError):
        await source.async_swap_apply(backup_bytes(make_backup()), {}, NEW_PORT)
    restore.assert_not_awaited()


async def test_apply_invalid_backup_writes_nothing(hass: HomeAssistant, source: ZHASource, radio) -> None:
    zha_entry(hass)
    with pytest.raises(json.JSONDecodeError):
        await source.async_swap_apply(b"not json", {}, NEW_PORT)
    radio.restore.assert_not_awaited()


# ====================================================================== start


async def test_swap_start_sets_up_unloaded_entry(hass: HomeAssistant, source: ZHASource, entries) -> None:
    entry = zha_entry(hass, state=ConfigEntryState.NOT_LOADED)
    await source.async_swap_start(SwapPlan(supported=True))
    entries.setup.assert_awaited_once_with(entry.entry_id)


async def test_swap_start_leaves_loaded_entry(hass: HomeAssistant, source: ZHASource, entries) -> None:
    zha_entry(hass, state=ConfigEntryState.LOADED)
    await source.async_swap_start(SwapPlan(supported=True))
    entries.setup.assert_not_awaited()


async def test_swap_start_without_entry(source: ZHASource, entries) -> None:
    await source.async_swap_start(SwapPlan(supported=True))
    entries.setup.assert_not_awaited()


async def test_wait_started_needs_loaded_and_gateway(
    hass: HomeAssistant, source: ZHASource, fast_sleep: _FastAsyncio
) -> None:
    entry = zha_entry(hass, state=ConfigEntryState.SETUP_IN_PROGRESS)
    gateway: dict[str, Any] = {"gw": None}

    def _get_gateway(_hass):
        if gateway["gw"] is None:
            raise ValueError("No gateway object exists")
        return gateway["gw"]

    def _tick(n: int) -> None:
        if n == 2:
            entry.mock_state(hass, ConfigEntryState.LOADED)  # loaded, gateway not up yet
        if n == 4:
            gateway["gw"] = gateway_with(app=live_app())

    fast_sleep.on_sleep = _tick
    with patch(GET_GATEWAY, side_effect=_get_gateway):
        await source.async_wait_started(5)
    assert len(fast_sleep.sleeps) == 4
    assert set(fast_sleep.sleeps) == {2}


async def test_wait_started_gateway_without_app_keeps_waiting(
    hass: HomeAssistant, source: ZHASource, fast_sleep: _FastAsyncio
) -> None:
    zha_entry(hass, state=ConfigEntryState.LOADED)
    gw = SimpleNamespace(application_controller=None, devices={})

    def _tick(n: int) -> None:
        if n == 3:
            gw.application_controller = live_app()

    fast_sleep.on_sleep = _tick
    with patch(GET_GATEWAY, return_value=gw):
        await source.async_wait_started(None)
    assert len(fast_sleep.sleeps) == 3


async def test_wait_started_timeout(hass: HomeAssistant, source: ZHASource, fast_sleep: _FastAsyncio) -> None:
    zha_entry(hass, state=ConfigEntryState.SETUP_RETRY)
    with pytest.raises(TimeoutError):
        await source.async_wait_started(0.05)
    assert fast_sleep.sleeps


# ===================================================================== verify


async def test_verify_matching_network(
    hass: HomeAssistant, source: ZHASource, fast_sleep: _FastAsyncio
) -> None:
    entry = zha_entry(hass, state=ConfigEntryState.LOADED)
    with patch(GET_GATEWAY, return_value=gateway_with(app=live_app())), patch(GET_ENTRY, return_value=entry):
        ident = await source.async_swap_verify(b"", {"extended_pan_id": EPID_NORM})
    assert ident["extended_pan_id"] == EPID_NORM
    assert ident["ieee"] == IEEE_NORM
    assert ident["device_path"] == OLD_PORT


async def test_verify_without_expected_network_returns_identity(
    hass: HomeAssistant, source: ZHASource, fast_sleep: _FastAsyncio
) -> None:
    entry = zha_entry(hass, state=ConfigEntryState.LOADED)
    with patch(GET_GATEWAY, return_value=gateway_with(app=live_app())), patch(GET_ENTRY, return_value=entry):
        ident = await source.async_swap_verify(b"", {})
    assert ident["extended_pan_id"] == EPID_NORM


async def test_verify_mismatch_raises(
    hass: HomeAssistant, source: ZHASource, fast_sleep: _FastAsyncio
) -> None:
    entry = zha_entry(hass, state=ConfigEntryState.LOADED)
    with (
        patch(GET_GATEWAY, return_value=gateway_with(app=live_app())),
        patch(GET_ENTRY, return_value=entry),
        pytest.raises(
            RuntimeError, match=f"different network \\({EPID_NORM}\\).*backup's \\({OTHER_EPID}\\)"
        ),
    ):
        await source.async_swap_verify(b"", {"extended_pan_id": OTHER_EPID})


async def test_verify_waits_for_start_with_timeout(hass: HomeAssistant, source: ZHASource) -> None:
    wait = AsyncMock(side_effect=TimeoutError)
    with patch.object(source, "async_wait_started", wait), pytest.raises(TimeoutError):
        await source.async_swap_verify(b"", {"extended_pan_id": EPID_NORM})
    wait.assert_awaited_once_with(zha_mod.START_TIMEOUT)


# ============================================================== device check


def cluster(result: Any = True) -> SimpleNamespace:
    if result == "hang":

        async def _hang(*_a, **_k):
            await asyncio.Event().wait()

        read = AsyncMock(side_effect=_hang)
    elif isinstance(result, BaseException):
        read = AsyncMock(side_effect=result)
    else:
        read = AsyncMock(return_value=([{0: 3}], []))
    return SimpleNamespace(read_attributes=read)


def zdev(
    name: str,
    *,
    mains: bool = True,
    basic: Any = True,
    last_seen: float | None = None,
    coordinator: bool = False,
    endpoints: dict | None = None,
) -> SimpleNamespace:
    if endpoints is None:
        in_clusters = {0x0006: object()}
        if basic is not None:
            in_clusters[0x0000] = cluster(basic)
        endpoints = {0: SimpleNamespace(in_clusters={}), 1: SimpleNamespace(in_clusters=in_clusters)}
    return SimpleNamespace(
        name=name,
        ieee=f"ieee-{name}",
        is_coordinator=coordinator,
        is_mains_powered=mains,
        last_seen=last_seen,
        device=SimpleNamespace(endpoints=endpoints),
    )


def basic_read(d: SimpleNamespace):
    return d.device.endpoints[1].in_clusters[0x0000].read_attributes


async def test_check_devices_report(source: ZHASource) -> None:
    now = time.time()
    coord = zdev("Coordinator", coordinator=True, basic=AssertionError("never ping yourself"))
    ok = zdev("Kitchen plug")
    err = zdev("Hall lamp", basic=RuntimeError("delivery failed"))
    nobasic = zdev("Old relay", basic=None)
    fresh = zdev("Door sensor", mains=False, last_seen=now + 3600)
    stale = zdev("Motion", mains=False, last_seen=now - 3600)
    never = zdev("Leak", mains=False, last_seen=None)
    gw = gateway_with(coord, ok, err, nobasic, fresh, stale, never)
    with patch(GET_GATEWAY, return_value=gw):
        report = await source.async_check_devices(0)
    assert report == DeviceReport(
        mains_total=3,
        mains_back=1,
        battery_total=3,
        battery_back=1,
        missing_mains=["Hall lamp", "Old relay"],
        missing_battery=["Leak", "Motion"],
    )
    basic_read(ok).assert_awaited_once_with([0], allow_cache=False)
    basic_read(coord).assert_not_awaited()


async def test_check_devices_ping_timeout(source: ZHASource) -> None:
    slow = zdev("Slow plug", basic="hang")
    with patch(GET_GATEWAY, return_value=gateway_with(slow)), patch(f"{ZHA_MOD}.PING_TIMEOUT", 0.01):
        report = await source.async_check_devices(0)
    assert report.mains_total == 1
    assert report.mains_back == 0
    assert report.missing_mains == ["Slow plug"]


async def test_check_devices_skips_zdo_endpoint_and_missing_device(source: ZHASource) -> None:
    zdo_only = zdev("ZDO only", endpoints={0: SimpleNamespace(in_clusters={0x0000: cluster()})})
    nodev = zdev("No zigpy device")
    nodev.device = None
    later = zdev(
        "Second endpoint",
        endpoints={
            1: SimpleNamespace(in_clusters={0x0006: object()}),
            2: SimpleNamespace(in_clusters={0x0000: cluster()}),
        },
    )
    with patch(GET_GATEWAY, return_value=gateway_with(zdo_only, nodev, later)):
        report = await source.async_check_devices(0)
    zdo_only.device.endpoints[0].in_clusters[0x0000].read_attributes.assert_not_awaited()
    assert report.mains_back == 1
    assert report.missing_mains == ["No zigpy device", "ZDO only"]


async def test_check_devices_name_falls_back_to_ieee(source: ZHASource) -> None:
    d = zdev("x", basic=RuntimeError("nope"))
    d.name = None
    with patch(GET_GATEWAY, return_value=gateway_with(d)):
        report = await source.async_check_devices(0)
    assert report.missing_mains == ["ieee-x"]


async def test_check_devices_waits_for_battery(source: ZHASource, fast_sleep: _FastAsyncio) -> None:
    sensor = zdev("Door sensor", mains=False, last_seen=None)

    def _tick(n: int) -> None:
        if n == 2:
            sensor.last_seen = time.time() + 1  # wakes up during the wait

    fast_sleep.on_sleep = _tick
    with patch(GET_GATEWAY, return_value=gateway_with(sensor)):
        report = await source.async_check_devices(90)
    assert report.battery_back == 1
    assert report.missing_battery == []
    assert fast_sleep.sleeps == [2, 2]


async def test_check_devices_battery_heard_before_start_not_counted(source: ZHASource) -> None:
    sensor = zdev("Door sensor", mains=False, last_seen=time.time() - 1)
    with patch(GET_GATEWAY, return_value=gateway_with(sensor)):
        report = await source.async_check_devices(0)
    assert (report.battery_total, report.battery_back) == (1, 0)


async def test_check_devices_none_without_devices(source: ZHASource) -> None:
    with patch(GET_GATEWAY, return_value=gateway_with()):
        assert await source.async_check_devices(0) is None
    with patch(GET_GATEWAY, return_value=gateway_with(zdev("C", coordinator=True))):
        assert await source.async_check_devices(0) is None


async def test_check_devices_none_without_gateway(source: ZHASource) -> None:
    with patch(GET_GATEWAY, side_effect=ValueError("No gateway object exists")):
        assert await source.async_check_devices(0) is None


# ======================================= wizard: confirmation via coordinator_down


@pytest.fixture(autouse=True)
def _no_min_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("custom_components.radio_guard.wizard.MIN_PROGRESS_SECONDS", 0)


@pytest.fixture
def entry_data(entry_data: dict[str, Any]) -> dict[str, Any]:
    return {**entry_data, CONF_SOURCES: [SOURCE_ZHA]}


class ConfirmFake(SwapFake):
    """Wizard fake for ZHA whose first apply needs a one-time IEEE write confirmation."""

    id = SOURCE_ZHA
    name = "ZHA"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass)
        self.plan = SwapPlan(supported=True, automatic=True, choose_port=True, current_port=OLD_PORT)
        self.ports = [PortOption(NEW_PORT, "Sonoff ZBDongle-E", new=True)]
        self.confirm_message = "This stick can store your network's address only once."

    async def async_swap_apply(self, data: bytes, meta: dict[str, Any], port: str | None) -> None:
        self.calls.append("apply")
        await asyncio.sleep(0)
        self.applied.append((data, dict(meta), port))
        if not meta.get(IEEE_KEY):
            raise ConfirmationNeeded(IEEE_KEY, self.confirm_message)


def seed(root: Path, meta: dict[str, Any]) -> str:
    created = dt_util.utcnow() - timedelta(hours=2)
    stem = created.strftime("%Y%m%dT%H%M%SZ")
    root.mkdir(parents=True, exist_ok=True)
    name = f"{stem}.json"
    (root / name).write_bytes(backup_bytes(make_backup()))
    full = {"source": SOURCE_ZHA, "created": created.isoformat(), "file": name, **meta}
    (root / f"{stem}.meta.json").write_text(json.dumps(full))
    return name


async def _drive(hass: HomeAssistant, mgr, result: dict[str, Any], seen: list) -> dict[str, Any]:
    for _ in range(20):
        if result["type"] is FlowResultType.SHOW_PROGRESS:
            seen.append((result["step_id"], result["progress_action"]))
        elif result["type"] is not FlowResultType.SHOW_PROGRESS_DONE:
            return result
        await hass.async_block_till_done()
        result = await mgr.async_configure(result["flow_id"])
        await hass.async_block_till_done()
    raise AssertionError("wizard did not settle")


async def _go_down(hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry) -> RadioGuardCoordinator:
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coord: RadioGuardCoordinator = mock_config_entry.runtime_data
    for minutes in (STARTUP_GRACE_MINUTES, DEFAULT_OFFLINE_MINUTES):
        freezer.tick(timedelta(minutes=minutes))
        await coord.async_refresh()
        await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, DOWN) is not None
    return coord


async def _to_confirm(hass: HomeAssistant, mgr, file: str, seen: list) -> dict[str, Any]:
    result = await mgr.async_init(DOMAIN, data={"issue_id": DOWN})
    assert result["menu_options"] == ["wiz_pick", "wait"]
    result = await mgr.async_configure(result["flow_id"], {"next_step_id": "wiz_pick"})
    assert result["step_id"] == "wiz_pick"
    result = await _drive(hass, mgr, await mgr.async_configure(result["flow_id"], {"file": file}), seen)
    assert result["step_id"] == "wiz_swap"
    result = await mgr.async_configure(result["flow_id"], {"port": NEW_PORT, "unplugged": True})
    assert result["step_id"] == "wiz_confirm"
    return result


async def test_wizard_ieee_confirmation_end_to_end(
    hass: HomeAssistant,
    freezer,
    mock_config_entry: MockConfigEntry,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = ConfirmFake(hass)
    fake.health = False
    monkeypatch.setattr("custom_components.radio_guard.coordinator.ZHASource", lambda hass: fake)
    file = seed(config_dir / "radio_guard" / SOURCE_ZHA, {"ieee": IEEE_NORM, "extended_pan_id": EPID_NORM})
    coord = await _go_down(hass, freezer, mock_config_entry)
    st = coord.states[SOURCE_ZHA]
    mgr = repairs_flow_manager(hass)
    seen: list[tuple[str, str]] = []

    result = await _to_confirm(hass, mgr, file, seen)
    assert seen == [("wiz_stop", "stopping")]

    # first write: the radio needs an irreversible IEEE write -> ask
    seen.clear()
    result = await _drive(hass, mgr, await mgr.async_configure(result["flow_id"], {}), seen)
    assert seen == [("wiz_restore", "restoring")]
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "wiz_extra"
    assert result["description_placeholders"]["message"] == fake.confirm_message
    assert result["description_placeholders"]["source"] == "ZHA"
    assert [str(k) for k in result["data_schema"].schema] == ["agree"]
    assert swap_calls(fake) == ["stop", "apply"]  # nothing started yet
    assert IEEE_KEY not in fake.applied[0][1]
    assert st.swapping

    # unticked -> error, nothing retried
    result = await mgr.async_configure(result["flow_id"], {"agree": False})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "wiz_extra"
    assert result["errors"] == {"agree": "agree_required"}
    assert len(fake.applied) == 1

    # ticked -> restore retried with the confirmation in meta, then the usual tail
    seen.clear()
    result = await _drive(hass, mgr, await mgr.async_configure(result["flow_id"], {"agree": True}), seen)
    assert seen == [("wiz_restore", "restoring"), ("wiz_devices", "checking_devices")]
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "wiz_done"
    assert len(fake.applied) == 2
    data, meta, port = fake.applied[1]
    assert meta[IEEE_KEY] is True
    assert meta["extended_pan_id"] == EPID_NORM  # record meta still merged in
    assert port == NEW_PORT
    assert data == (st.store.root / file).read_bytes()
    assert swap_calls(fake) == ["stop", "apply", "apply", "start", "wait_started", "verify", "check_devices"]
    assert not st.swapping
    assert ir.async_get(hass).async_get_issue(DOMAIN, DOWN) is None

    result = await mgr.async_configure(result["flow_id"], {"next_step_id": "wiz_finish"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


def swap_calls(fake: SwapFake) -> list[str]:
    return [c for c in fake.calls if c != "wait_stopped"]


async def test_wizard_real_zha_source_ieee_confirmation(
    hass: HomeAssistant,
    freezer,
    mock_config_entry: MockConfigEntry,
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same path with the real ZHASource: DestructiveWriteNetworkSettings -> wiz_extra -> overwrite_ieee."""
    monkeypatch.setattr("custom_components.radio_guard.wizard.DEVICE_TIMEOUT", 0)
    zha = zha_entry(hass, state=ConfigEntryState.SETUP_RETRY, radio_type="znp")
    file = seed(config_dir / "radio_guard" / SOURCE_ZHA, {"ieee": IEEE_NORM, "extended_pan_id": EPID_NORM})
    coord = await _go_down(hass, freezer, mock_config_entry)
    st = coord.states[SOURCE_ZHA]
    assert isinstance(st.source, ZHASource)

    restore = AsyncMock(side_effect=[DestructiveWriteNetworkSettings("one-time IEEE write"), None])
    plug = zdev("Kitchen plug")
    gw = gateway_with(plug, app=live_app())

    async def _setup(entry_id: str) -> bool:
        if entry_id == zha.entry_id:
            zha.mock_state(hass, ConfigEntryState.LOADED)
        return True

    async def _unload(entry_id: str) -> bool:
        if entry_id == zha.entry_id:
            zha.mock_state(hass, ConfigEntryState.NOT_LOADED)
        return True

    old = Candidate(OLD_PORT, "Old stick", "zigbee")
    new = Candidate(NEW_PORT, "Sonoff ZBDongle-E", "zigbee")
    det, probed = probe()
    mgr = repairs_flow_manager(hass)
    seen: list[tuple[str, str]] = []
    with (
        det,
        patch.object(ZhaRadioManager, "async_load_network_settings", AsyncMock()),
        patch.object(ZhaRadioManager, "restore_backup", restore),
        patch.object(hass.config_entries, "async_setup", AsyncMock(side_effect=_setup)),
        patch.object(hass.config_entries, "async_unload", AsyncMock(side_effect=_unload)),
        patch(FIND, AsyncMock(side_effect=[[old], [old, new]])),
        patch(GET_GATEWAY, return_value=gw),
        patch(GET_ENTRY, return_value=zha),
    ):
        result = await _to_confirm(hass, mgr, file, seen)
        assert zha.state is ConfigEntryState.NOT_LOADED
        assert result["description_placeholders"]["warnings"] == "- none"  # ZHA pre-flight noise filtered

        result = await _drive(hass, mgr, await mgr.async_configure(result["flow_id"], {}), seen)
        assert result["step_id"] == "wiz_extra"
        assert "only once" in result["description_placeholders"]["message"]
        assert restore.await_args.kwargs == {"overwrite_ieee": False}
        assert zha.state is ConfigEntryState.NOT_LOADED  # not started after the refusal

        result = await mgr.async_configure(result["flow_id"], {"agree": False})
        assert result["errors"] == {"agree": "agree_required"}

        result = await _drive(hass, mgr, await mgr.async_configure(result["flow_id"], {"agree": True}), seen)
        assert result["type"] is FlowResultType.MENU
        assert result["step_id"] == "wiz_done"
        assert restore.await_count == 2
        assert restore.await_args.kwargs == {"overwrite_ieee": True}
        assert probed == [NEW_PORT, NEW_PORT]
        assert zha.data["device"]["path"] == NEW_PORT
        assert zha.data["radio_type"] == "ezsp"
        assert zha.state is ConfigEntryState.LOADED
        ph = result["description_placeholders"]
        assert ph["network"] == EPID_NORM
        assert "Mains-powered devices back: 1 of 1" in ph["devices"]
        assert not st.swapping

        result = await mgr.async_configure(result["flow_id"], {"next_step_id": "wiz_finish"})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
