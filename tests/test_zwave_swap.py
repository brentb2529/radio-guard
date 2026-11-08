"""Z-Wave JS guided-replacement API (health, plan, stop/apply/verify, device check) and the wizard."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.radio_guard.backup import zwave as zwave_mod
from custom_components.radio_guard.backup.base import DeviceReport, RestoreCheck, SwapPlan
from custom_components.radio_guard.backup.zwave import (
    DRIVER_TIMEOUT,
    ZWAVE_ADDON_SLUG,
    ZWAVE_DOMAIN,
    ZWaveJSSource,
)
from custom_components.radio_guard.const import (
    CONF_SOURCES,
    DEFAULT_OFFLINE_MINUTES,
    DOMAIN,
    ISSUE_COORDINATOR_DOWN,
    SOURCE_ZWAVE,
    STARTUP_GRACE_MINUTES,
)
from custom_components.radio_guard.coordinator import RadioGuardCoordinator

from .test_zwave import HOME_ID, KEYS, OTHER_HOME_ID, FakeController

ZW_MOD = "custom_components.radio_guard.backup.zwave"
SERIAL_PORTS = "custom_components.radio_guard.hardware.serial_ports"
IS_HASSIO = "homeassistant.helpers.hassio.is_hassio"
ADDON_MANAGER = "homeassistant.components.hassio.AddonManager"
SERVER_URL = "ws://zwave-js-ui:3000"
ADDON_URL = "ws://core-zwave-js:3000"
OLD_PORT = "/dev/serial/by-id/usb-old-stick"
NEW_PORT = "/dev/serial/by-id/usb-new-stick"
DOWN = f"{ISSUE_COORDINATOR_DOWN}_{SOURCE_ZWAVE}"


# ------------------------------------------------------------------ helpers


class _FastAsyncio:
    """Stand-in for the ``asyncio`` module inside zwave.py: real everything, ``sleep`` yields once."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    async def sleep(self, delay: float, result: Any = None) -> Any:
        self.sleeps.append(delay)
        await asyncio.sleep(0)
        return result


@pytest.fixture
def fast_sleep() -> _FastAsyncio:
    proxy = _FastAsyncio()
    with patch(f"{ZW_MOD}.asyncio", proxy):
        yield proxy


def zw_entry(
    hass: HomeAssistant,
    *,
    ctrl: FakeController | None = None,
    state: ConfigEntryState = ConfigEntryState.LOADED,
    data: dict[str, Any] | None = None,
    runtime: Any = "default",
    connected: bool | None = None,
    unique_id: str = str(HOME_ID),
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=ZWAVE_DOMAIN,
        title="Z-Wave JS",
        unique_id=unique_id,
        data={"url": SERVER_URL, "use_addon": False, **KEYS} if data is None else data,
        state=state,
    )
    entry.add_to_hass(hass)
    if runtime == "default":
        client = SimpleNamespace(driver=SimpleNamespace(controller=ctrl or FakeController()))
        if connected is not None:
            client.connected = connected
        runtime = SimpleNamespace(client=client)
    entry.runtime_data = runtime
    return entry


def addon_data(**extra: Any) -> dict[str, Any]:
    return {"url": ADDON_URL, "use_addon": True, "usb_path": OLD_PORT, **KEYS, **extra}


def addon_mgr(options: dict[str, Any] | None = None, *, info_error: Exception | None = None) -> MagicMock:
    mgr = MagicMock()
    mgr.async_stop_addon = AsyncMock()
    mgr.async_start_addon = AsyncMock()
    mgr.async_set_addon_options = AsyncMock()
    if info_error is not None:
        mgr.async_get_addon_info = AsyncMock(side_effect=info_error)
    else:
        mgr.async_get_addon_info = AsyncMock(return_value=SimpleNamespace(options=options))
    return mgr


@pytest.fixture
def source(hass: HomeAssistant) -> ZWaveJSSource:
    return ZWaveJSSource(hass)


class FakeNode:
    def __init__(
        self,
        node_id: int,
        *,
        name: str | None = None,
        label: str | None = None,
        listening: bool | None = True,
        frequent: Any = False,
        last_seen: datetime | None = None,
        ping: Any = True,
    ) -> None:
        self.node_id = node_id
        self.name = name
        self.label = label
        self.is_listening = listening
        self.is_frequent_listening = frequent
        self.last_seen = last_seen
        if isinstance(ping, BaseException) or (isinstance(ping, type) and issubclass(ping, BaseException)):
            self.async_ping = AsyncMock(side_effect=ping)
        elif ping == "hang":

            async def _hang() -> bool:
                await asyncio.Event().wait()
                return True

            self.async_ping = _hang
        else:
            self.async_ping = AsyncMock(return_value=ping)


def ctrl_with_nodes(*nodes: FakeNode) -> FakeController:
    own = FakeNode(1, name="Controller", ping=AssertionError("never ping yourself"))
    return FakeController(nodes={1: own, **{n.node_id: n for n in nodes}})


# ===================================================================== health


async def test_health_without_entry(source: ZWaveJSSource) -> None:
    assert await source.async_health() is None


@pytest.mark.parametrize("state", [ConfigEntryState.SETUP_RETRY, ConfigEntryState.SETUP_ERROR])
async def test_health_down_states(hass: HomeAssistant, source: ZWaveJSSource, state) -> None:
    zw_entry(hass, state=state, runtime=None)
    assert await source.async_health() is False


@pytest.mark.parametrize("state", [ConfigEntryState.SETUP_IN_PROGRESS, ConfigEntryState.NOT_LOADED])
async def test_health_unknown_states(hass: HomeAssistant, source: ZWaveJSSource, state) -> None:
    zw_entry(hass, state=state)
    assert await source.async_health() is None


async def test_health_disconnected_client(hass: HomeAssistant, source: ZWaveJSSource) -> None:
    zw_entry(hass, connected=False)
    assert await source.async_health() is False


async def test_health_driver_missing(hass: HomeAssistant, source: ZWaveJSSource) -> None:
    zw_entry(hass, runtime=SimpleNamespace(client=SimpleNamespace(connected=True, driver=None)))
    assert await source.async_health() is False


@pytest.mark.parametrize("connected", [True, None])
async def test_health_ok(hass: HomeAssistant, source: ZWaveJSSource, connected) -> None:
    zw_entry(hass, connected=connected)  # None: no ``connected`` attribute at all
    assert await source.async_health() is True


async def _health_with(hass: HomeAssistant, source: ZWaveJSSource, **ctrl_attrs: Any) -> bool | None:
    ctrl = FakeController(home_id=HOME_ID)
    for k, v in ctrl_attrs.items():
        setattr(ctrl, k, v)
    zw_entry(hass, ctrl=ctrl)
    return await source.async_health()


async def test_health_probe_unanswered_is_down(hass: HomeAssistant, source: ZWaveJSSource) -> None:
    """Z-Wave JS keeps the integration loaded when a stick stops answering; the probe catches it."""
    assert (
        await _health_with(
            hass, source, async_get_rf_region=AsyncMock(side_effect=RuntimeError("ZW0200 no ACK"))
        )
        is False
    )


async def test_health_probe_timeout_is_down(hass: HomeAssistant, source: ZWaveJSSource) -> None:
    async def hang() -> int:
        await asyncio.sleep(1)
        return 1

    with patch(f"{ZW_MOD}.PROBE_TIMEOUT", 0.01):
        assert await _health_with(hass, source, async_get_rf_region=hang) is False


async def test_health_unresponsive_controller_is_down(hass: HomeAssistant, source: ZWaveJSSource) -> None:
    from zwave_js_server.const import ControllerStatus

    probe = AsyncMock(return_value=1)
    assert (
        await _health_with(hass, source, status=ControllerStatus.UNRESPONSIVE, async_get_rf_region=probe)
        is False
    )
    probe.assert_not_awaited()


async def test_health_probe_not_supported_falls_back_to_status(
    hass: HomeAssistant, source: ZWaveJSSource
) -> None:
    """500-series sticks have no RF-region API: don't call them down, and stop asking."""
    probe = AsyncMock(side_effect=RuntimeError("Command not supported by this controller"))
    assert await _health_with(hass, source, async_get_rf_region=probe) is True
    assert source._probe_supported is False
    assert await source.async_health() is True
    assert probe.await_count == 1


async def test_health_client_missing_is_unknown(hass: HomeAssistant, source: ZWaveJSSource) -> None:
    zw_entry(hass, runtime=None)
    assert await source.async_health() is None


async def test_health_ignores_disabled_entry(hass: HomeAssistant, source: ZWaveJSSource) -> None:
    entry = zw_entry(hass, state=ConfigEntryState.SETUP_RETRY)
    entry.disabled_by = "user"
    assert source._any_entry() is None
    assert await source.async_health() is None


async def test_any_entry_prefers_pinned_home_id(hass: HomeAssistant) -> None:
    first = zw_entry(hass, unique_id="111", state=ConfigEntryState.SETUP_RETRY)
    second = zw_entry(hass, unique_id="222", state=ConfigEntryState.SETUP_RETRY)
    assert ZWaveJSSource(hass, home_id=222)._any_entry() is second
    assert ZWaveJSSource(hass, home_id=999)._any_entry() is first  # unknown pin falls back


# ================================================================ uses add-on


async def test_uses_addon_needs_flag_and_supervisor(hass: HomeAssistant, source: ZWaveJSSource) -> None:
    assert source._uses_addon() is False  # no entry
    entry = zw_entry(hass, data=addon_data())
    with patch(IS_HASSIO, return_value=False):
        assert source._uses_addon() is False
    with patch(IS_HASSIO, return_value=True):
        assert source._uses_addon() is True
    hass.config_entries.async_update_entry(entry, data={**entry.data, "use_addon": False})
    with patch(IS_HASSIO, return_value=True):
        assert source._uses_addon() is False


# ================================================================== swap plan


async def test_swap_plan_without_entry(source: ZWaveJSSource) -> None:
    plan = await source.async_swap_plan()
    assert plan.supported is False
    assert "not set up" in plan.reason


async def test_swap_plan_addon(hass: HomeAssistant, source: ZWaveJSSource) -> None:
    zw_entry(hass, data=addon_data(), state=ConfigEntryState.SETUP_RETRY, runtime=None)
    mgr = addon_mgr({"device": NEW_PORT, "s0_legacy_key": "x"})
    with patch(IS_HASSIO, return_value=True), patch(ADDON_MANAGER, return_value=mgr) as cls:
        plan = await source.async_swap_plan()
    assert plan.supported and plan.automatic and plan.choose_port
    assert plan.stop_first is True
    assert plan.current_port == NEW_PORT  # the add-on's option wins over entry.data usb_path
    assert plan.details == {"addon": ZWAVE_ADDON_SLUG}
    assert plan.plug_in is None  # the wizard's standard text (USB list + network address)
    cls.assert_called_once_with(hass, zwave_mod._LOGGER, "Z-Wave JS", ZWAVE_ADDON_SLUG)


@pytest.mark.parametrize(
    "mgr",
    [
        addon_mgr({}),
        addon_mgr(None),
        addon_mgr({"device": ""}),
        addon_mgr(info_error=RuntimeError("supervisor")),
    ],
    ids=["no-device", "no-options", "empty-device", "info-fails"],
)
async def test_swap_plan_addon_port_falls_back_to_usb_path(
    hass: HomeAssistant, source: ZWaveJSSource, mgr: MagicMock
) -> None:
    zw_entry(hass, data=addon_data(), state=ConfigEntryState.SETUP_RETRY, runtime=None)
    with patch(IS_HASSIO, return_value=True), patch.object(source, "_addon_manager", return_value=mgr):
        plan = await source.async_swap_plan()
    assert plan.supported and plan.choose_port
    assert plan.current_port == OLD_PORT


async def test_swap_plan_external_server(hass: HomeAssistant, source: ZWaveJSSource) -> None:
    zw_entry(hass, state=ConfigEntryState.SETUP_RETRY, runtime=None)
    with patch(IS_HASSIO, return_value=True):  # Supervisor present, but Z-Wave JS UI runs elsewhere
        plan = await source.async_swap_plan()
    assert plan.supported is True
    assert plan.automatic is True
    assert plan.choose_port is False
    assert plan.stop_first is False
    assert plan.current_port is None  # the server URL is not a "port" the user cares about
    assert plan.details["external_server"] is True
    assert plan.details["after_failure"] == "Nothing was written to the new stick."
    assert "unknown adapter" in plan.details["note"]
    assert "Z-Wave JS UI" in plan.plug_in


async def test_swap_plan_addon_flag_without_supervisor_is_external(
    hass: HomeAssistant, source: ZWaveJSSource
) -> None:
    zw_entry(hass, data=addon_data(), state=ConfigEntryState.SETUP_RETRY, runtime=None)
    with patch(IS_HASSIO, return_value=False):
        plan = await source.async_swap_plan()
    assert plan.choose_port is False
    assert plan.stop_first is False


# ========================================================== stop / ports


async def test_swap_stop_addon_plan_stops_addon_and_snapshots_once(
    hass: HomeAssistant, source: ZWaveJSSource
) -> None:
    mgr = addon_mgr()
    plan = SwapPlan(supported=True, automatic=True, choose_port=True, details={"addon": ZWAVE_ADDON_SLUG})
    scans = [[(OLD_PORT, "Old stick")], [(OLD_PORT, "Old stick"), (NEW_PORT, "New stick")]]
    scanner = MagicMock(side_effect=scans)
    with patch(SERIAL_PORTS, scanner), patch.object(source, "_addon_manager", return_value=mgr):
        await source.async_swap_stop(plan)
        assert source._ports_before == {OLD_PORT}
        await source.async_swap_stop(plan)  # user went back a step: first snapshot is kept
    assert scanner.call_count == 1
    assert source._ports_before == {OLD_PORT}
    assert mgr.async_stop_addon.await_count == 2


async def test_swap_stop_addon_unloads_integration_before_stopping(
    hass: HomeAssistant, source: ZWaveJSSource
) -> None:
    """HA's Z-Wave JS integration restarts its add-on while retrying: unload it first (seen on HA OS)."""
    entry = zw_entry(hass, data=addon_data(), state=ConfigEntryState.SETUP_RETRY, runtime=None)
    order: list[str] = []
    mgr = addon_mgr()
    mgr.async_stop_addon.side_effect = lambda: order.append("stop_addon")

    async def unload(entry_id: str) -> bool:
        order.append("unload")
        return True

    plan = SwapPlan(supported=True, automatic=True, choose_port=True, details={"addon": ZWAVE_ADDON_SLUG})
    with (
        patch(SERIAL_PORTS, return_value=[]),
        patch.object(source, "_addon_manager", return_value=mgr),
        patch.object(hass.config_entries, "async_unload", side_effect=unload) as unl,
    ):
        await source.async_swap_stop(plan)
    assert order == ["unload", "stop_addon"]
    unl.assert_awaited_once_with(entry.entry_id)


async def test_swap_stop_addon_refused_unload_stops_nothing(
    hass: HomeAssistant, source: ZWaveJSSource
) -> None:
    zw_entry(hass, data=addon_data())
    mgr = addon_mgr()
    plan = SwapPlan(supported=True, automatic=True, choose_port=True, details={"addon": ZWAVE_ADDON_SLUG})
    with (
        patch(SERIAL_PORTS, return_value=[]),
        patch.object(source, "_addon_manager", return_value=mgr),
        patch.object(hass.config_entries, "async_unload", AsyncMock(return_value=False)),
        pytest.raises(RuntimeError, match="could not stop Z-Wave JS"),
    ):
        await source.async_swap_stop(plan)
    mgr.async_stop_addon.assert_not_awaited()


@pytest.mark.parametrize(
    "plan",
    [
        SwapPlan(supported=True, automatic=True, choose_port=False, stop_first=False),
        SwapPlan(supported=True, automatic=False, choose_port=True, details={"addon": ZWAVE_ADDON_SLUG}),
    ],
    ids=["external-server", "manual-plan"],
)
async def test_swap_stop_never_touches_addon_otherwise(
    hass: HomeAssistant, source: ZWaveJSSource, plan: SwapPlan
) -> None:
    mgr = addon_mgr()
    with (
        patch(SERIAL_PORTS, return_value=[(OLD_PORT, "Old stick")]),
        patch.object(source, "_addon_manager", return_value=mgr) as getter,
    ):
        await source.async_swap_stop(plan)
    getter.assert_not_called()
    mgr.async_stop_addon.assert_not_awaited()
    assert source._ports_before == {OLD_PORT}


async def test_swap_ports_new_first(hass: HomeAssistant, source: ZWaveJSSource) -> None:
    with patch(SERIAL_PORTS, return_value=[("/dev/ttyAMA0", "AAA serial"), (OLD_PORT, "Old stick")]):
        await source.async_swap_stop(SwapPlan(supported=True))
    later = [("/dev/ttyAMA0", "AAA serial"), (OLD_PORT, "Old stick"), (NEW_PORT, "ZZZ new stick")]
    with patch(SERIAL_PORTS, return_value=later):
        ports = await source.async_swap_ports()
    assert [(p.value, p.new) for p in ports] == [
        (NEW_PORT, True),
        ("/dev/ttyAMA0", False),
        (OLD_PORT, False),
    ]
    assert ports[0].label == "ZZZ new stick (unrecognised device)"


async def test_swap_ports_without_snapshot_marks_all_new(hass: HomeAssistant, source: ZWaveJSSource) -> None:
    with patch(SERIAL_PORTS, return_value=[(NEW_PORT, "B"), (OLD_PORT, "A")]):
        ports = await source.async_swap_ports()
    assert [p.value for p in ports] == [OLD_PORT, NEW_PORT]  # all new -> sorted by label
    assert all(p.new for p in ports)


async def test_wait_stopped_is_a_no_op(source: ZWaveJSSource) -> None:
    assert await source.async_wait_stopped(1) is None
    assert await source.async_wait_stopped(None) is None


# ================================================================== set port


async def test_swap_set_port_updates_addon_device_only(hass: HomeAssistant, source: ZWaveJSSource) -> None:
    zw_entry(hass, data=addon_data())
    mgr = addon_mgr({"device": OLD_PORT, "s0_legacy_key": "abc", "log_level": "info"})
    with patch(IS_HASSIO, return_value=True), patch.object(source, "_addon_manager", return_value=mgr):
        await source.async_swap_set_port(NEW_PORT)
    mgr.async_set_addon_options.assert_awaited_once_with(
        {"device": NEW_PORT, "s0_legacy_key": "abc", "log_level": "info"}
    )


@pytest.mark.parametrize(
    ("before", "port", "after"),
    [
        ({"socket": "tcp://10.0.2.2:5555"}, "tcp://10.0.2.2:5556", {"socket": "tcp://10.0.2.2:5556"}),
        ({"device": OLD_PORT}, "tcp://192.168.1.60:6638", {"socket": "tcp://192.168.1.60:6638"}),
        ({"socket": "tcp://10.0.2.2:5555"}, NEW_PORT, {"device": NEW_PORT}),
    ],
    ids=["network-to-network", "usb-to-network", "network-to-usb"],
)
async def test_swap_set_port_network_sticks_use_socket_option(
    hass: HomeAssistant, source: ZWaveJSSource, before: dict, port: str, after: dict
) -> None:
    """The add-on keeps network sticks in 'socket' and serial ones in 'device'; never both."""
    zw_entry(hass, data=addon_data())
    mgr = addon_mgr({**before, "log_level": "info"})
    with patch(IS_HASSIO, return_value=True), patch.object(source, "_addon_manager", return_value=mgr):
        await source.async_swap_set_port(port)
    mgr.async_set_addon_options.assert_awaited_once_with({**after, "log_level": "info"})


async def test_swap_plan_addon_reads_socket_option(hass: HomeAssistant, source: ZWaveJSSource) -> None:
    zw_entry(hass, data=addon_data())
    mgr = addon_mgr({"socket": "tcp://10.0.2.2:5555"})
    with patch(IS_HASSIO, return_value=True), patch.object(source, "_addon_manager", return_value=mgr):
        plan = await source.async_swap_plan()
    assert plan.current_port == "tcp://10.0.2.2:5555"


async def test_swap_set_port_with_empty_addon_options(hass: HomeAssistant, source: ZWaveJSSource) -> None:
    zw_entry(hass, data=addon_data())
    mgr = addon_mgr(None)
    with patch(IS_HASSIO, return_value=True), patch.object(source, "_addon_manager", return_value=mgr):
        await source.async_swap_set_port(NEW_PORT)
    mgr.async_set_addon_options.assert_awaited_once_with({"device": NEW_PORT})


@pytest.mark.parametrize(("use_addon", "port"), [(False, NEW_PORT), (True, None), (True, "")])
async def test_swap_set_port_no_op(hass: HomeAssistant, source: ZWaveJSSource, use_addon: bool, port) -> None:
    zw_entry(hass, data=addon_data(use_addon=use_addon))
    mgr = addon_mgr({"device": OLD_PORT})
    with (
        patch(IS_HASSIO, return_value=True),
        patch.object(source, "_addon_manager", return_value=mgr) as getter,
    ):
        await source.async_swap_set_port(port)
    getter.assert_not_called()
    mgr.async_set_addon_options.assert_not_awaited()


# ================================================================ wait driver


async def test_wait_driver_returns_ready_controller(hass: HomeAssistant, source: ZWaveJSSource) -> None:
    ctrl = FakeController()
    zw_entry(hass, ctrl=ctrl)
    assert await source._wait_driver(None) is ctrl


async def test_wait_driver_waits_for_loaded_entry(
    hass: HomeAssistant, source: ZWaveJSSource, fast_sleep: _FastAsyncio
) -> None:
    entry = zw_entry(hass, state=ConfigEntryState.SETUP_IN_PROGRESS, runtime=None)
    task = asyncio.ensure_future(source._wait_driver(5))
    for _ in range(5):
        await asyncio.sleep(0)
    assert not task.done()
    # loaded but the driver is not ready yet: keep waiting
    entry.runtime_data = SimpleNamespace(client=SimpleNamespace(driver=None))
    entry.mock_state(hass, ConfigEntryState.LOADED)
    for _ in range(5):
        await asyncio.sleep(0)
    assert not task.done()
    ctrl = FakeController()
    entry.runtime_data = SimpleNamespace(client=SimpleNamespace(driver=SimpleNamespace(controller=ctrl)))
    assert await task is ctrl
    assert fast_sleep.sleeps and set(fast_sleep.sleeps) == {3}


async def test_wait_driver_waits_for_controller_object(
    hass: HomeAssistant, source: ZWaveJSSource, fast_sleep: _FastAsyncio
) -> None:
    zw_entry(hass, runtime=SimpleNamespace(client=SimpleNamespace(driver=SimpleNamespace(controller=None))))
    with pytest.raises(TimeoutError):
        await source._wait_driver(0.05)


async def test_wait_driver_not_home_id_waits_for_other_controller(
    hass: HomeAssistant, source: ZWaveJSSource, fast_sleep: _FastAsyncio
) -> None:
    old = FakeController(home_id=HOME_ID)
    entry = zw_entry(hass, ctrl=old)
    task = asyncio.ensure_future(source._wait_driver(5, not_home_id=HOME_ID))
    for _ in range(5):
        await asyncio.sleep(0)
    assert not task.done()
    new = FakeController(home_id=OTHER_HOME_ID)
    entry.runtime_data.client.driver.controller = new
    assert await task is new


async def test_wait_driver_not_home_id_times_out_on_same_controller(
    hass: HomeAssistant, source: ZWaveJSSource, fast_sleep: _FastAsyncio
) -> None:
    zw_entry(hass, ctrl=FakeController(home_id=HOME_ID))
    with pytest.raises(TimeoutError):
        await source._wait_driver(0.05, not_home_id=HOME_ID)
    assert await source._wait_driver(0.05, not_home_id=OTHER_HOME_ID) is not None


async def test_wait_driver_reloads_retrying_entry(
    hass: HomeAssistant, source: ZWaveJSSource, fast_sleep: _FastAsyncio
) -> None:
    entry = zw_entry(hass, state=ConfigEntryState.SETUP_RETRY, runtime=None)
    with (
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
        pytest.raises(TimeoutError),
    ):
        await source._wait_driver(0.05)
    assert reload.call_count >= 1
    assert {c.args for c in reload.call_args_list} == {(entry.entry_id,)}


async def test_wait_driver_does_not_reload_other_states(
    hass: HomeAssistant, source: ZWaveJSSource, fast_sleep: _FastAsyncio
) -> None:
    zw_entry(hass, state=ConfigEntryState.SETUP_IN_PROGRESS, runtime=None)
    with (
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
        pytest.raises(TimeoutError),
    ):
        await source._wait_driver(0.05)
    reload.assert_not_called()


async def test_wait_driver_without_entry_times_out(source: ZWaveJSSource, fast_sleep: _FastAsyncio) -> None:
    with pytest.raises(TimeoutError):
        await source._wait_driver(0.05)


async def test_wait_driver_survives_missing_client(
    hass: HomeAssistant, source: ZWaveJSSource, fast_sleep: _FastAsyncio
) -> None:
    entry = zw_entry(hass, runtime=None)  # loaded, runtime_data not populated yet
    task = asyncio.ensure_future(source._wait_driver(5))
    for _ in range(5):
        await asyncio.sleep(0)
    assert not task.done()
    ctrl = FakeController()
    entry.runtime_data = SimpleNamespace(client=SimpleNamespace(driver=SimpleNamespace(controller=ctrl)))
    assert await task is ctrl


async def test_wait_driver_ignores_disconnected_client(
    hass: HomeAssistant, source: ZWaveJSSource, fast_sleep: _FastAsyncio
) -> None:
    zw_entry(hass, connected=False)
    assert await source.async_health() is False
    with pytest.raises(TimeoutError):
        await source._wait_driver(0.05)


async def test_wait_started_uses_wait_driver(source: ZWaveJSSource) -> None:
    with patch.object(source, "_wait_driver", AsyncMock()) as wait:
        await source.async_wait_started(42)
    wait.assert_awaited_once_with(42)


# ======================================================================= apply


async def test_swap_apply_addon_sets_port_starts_and_restores(
    hass: HomeAssistant, source: ZWaveJSSource
) -> None:
    zw_entry(hass, data=addon_data())
    mgr = addon_mgr({"device": OLD_PORT, "network_key": "k"})
    order: list[str] = []
    mgr.async_set_addon_options.side_effect = lambda *a: order.append("set_port")
    mgr.async_start_addon.side_effect = lambda: order.append("start")
    new = FakeController(home_id=OTHER_HOME_ID)

    async def _wait(timeout, not_home_id=None):
        order.append(f"wait:{timeout}:{not_home_id}")
        return new

    async def _check(*a):
        order.append("preflight")
        return RestoreCheck(ok=True)

    meta = {"home_id": HOME_ID}
    with (
        patch(IS_HASSIO, return_value=True),
        patch.object(source, "_addon_manager", return_value=mgr),
        patch.object(source, "_wait_driver", side_effect=_wait),
        patch.object(source, "async_restore_check", side_effect=_check),
        patch.object(
            source, "async_restore", AsyncMock(side_effect=lambda *a: order.append("restore"))
        ) as rs,
    ):
        await source.async_swap_apply(b"nvm", meta, NEW_PORT)
    assert order == ["set_port", "start", f"wait:{DRIVER_TIMEOUT}:{HOME_ID}", "preflight", "restore"]
    mgr.async_set_addon_options.assert_awaited_once_with({"device": NEW_PORT, "network_key": "k"})
    rs.assert_awaited_once_with(b"nvm", meta)


async def test_swap_apply_never_accepts_the_backed_up_network_as_new_stick(
    hass: HomeAssistant, source: ZWaveJSSource, fast_sleep: _FastAsyncio
) -> None:
    """Only the old controller (backup's home ID) is visible: wait for the new one, never 'succeed'."""
    zw_entry(hass, ctrl=FakeController(home_id=HOME_ID))
    with (
        patch(f"{ZW_MOD}.DRIVER_TIMEOUT", 0.05),
        patch.object(source, "async_restore", AsyncMock()) as rs,
        pytest.raises(RuntimeError, match="did not come up on a new controller"),
    ):
        await source.async_swap_apply(b"nvm", {"home_id": HOME_ID}, None)
    rs.assert_not_awaited()


async def test_swap_apply_blocks_when_preflight_on_new_stick_fails(
    hass: HomeAssistant, source: ZWaveJSSource
) -> None:
    zw_entry(hass, ctrl=FakeController(home_id=OTHER_HOME_ID))
    with (
        patch.object(
            source,
            "async_restore_check",
            AsyncMock(return_value=RestoreCheck(ok=False, reasons=["Keys differ."])),
        ),
        patch.object(source, "async_restore", AsyncMock()) as rs,
        pytest.raises(RuntimeError, match="Pre-flight on the new controller failed: Keys differ."),
    ):
        await source.async_swap_apply(b"nvm", {"home_id": HOME_ID}, None)
    rs.assert_not_awaited()


@pytest.mark.parametrize("meta", [{"home_id": HOME_ID}, {}], ids=["other-home-id", "no-home-id-in-meta"])
async def test_swap_apply_external_server_restores_without_addon(
    hass: HomeAssistant, source: ZWaveJSSource, meta: dict[str, Any]
) -> None:
    zw_entry(hass, ctrl=FakeController(home_id=OTHER_HOME_ID))
    with (
        patch(IS_HASSIO, return_value=True),
        patch.object(source, "_addon_manager") as getter,
        patch.object(source, "async_restore", AsyncMock()) as rs,
    ):
        await source.async_swap_apply(b"nvm", meta, NEW_PORT)
    getter.assert_not_called()
    rs.assert_awaited_once_with(b"nvm", meta)


async def test_swap_apply_times_out_when_driver_never_comes(
    hass: HomeAssistant, source: ZWaveJSSource, fast_sleep: _FastAsyncio
) -> None:
    zw_entry(hass, state=ConfigEntryState.SETUP_RETRY, runtime=None)
    with (
        patch(f"{ZW_MOD}.DRIVER_TIMEOUT", 0.05),
        patch.object(hass.config_entries, "async_schedule_reload"),
        patch.object(source, "async_restore", AsyncMock()) as rs,
        pytest.raises(RuntimeError, match="did not come up on a new controller"),
    ):
        await source.async_swap_apply(b"nvm", {"home_id": HOME_ID}, None)
    rs.assert_not_awaited()


# ======================================================================= start


async def test_swap_start_addon_swallows_errors(hass: HomeAssistant, source: ZWaveJSSource) -> None:
    zw_entry(hass, data=addon_data())
    mgr = addon_mgr()
    mgr.async_start_addon.side_effect = RuntimeError("supervisor said no")
    with patch(IS_HASSIO, return_value=True), patch.object(source, "_addon_manager", return_value=mgr):
        await source.async_swap_start(SwapPlan(supported=True, automatic=True))
    mgr.async_start_addon.assert_awaited_once()


async def test_swap_start_external_server_is_no_op(hass: HomeAssistant, source: ZWaveJSSource) -> None:
    zw_entry(hass)
    with patch.object(source, "_addon_manager") as getter:
        await source.async_swap_start(SwapPlan(supported=True, automatic=True))
    getter.assert_not_called()


# ====================================================================== verify


async def test_swap_verify_returns_identity(hass: HomeAssistant, source: ZWaveJSSource) -> None:
    zw_entry(hass, ctrl=FakeController(home_id=HOME_ID))
    ident = await source.async_swap_verify(b"nvm", {"home_id": HOME_ID, "home_id_hex": f"0x{HOME_ID:08X}"})
    assert ident["home_id"] == HOME_ID
    assert ident["home_id_hex"] == "0xE1A2B3C4"


async def test_swap_verify_without_expected_home_id(hass: HomeAssistant, source: ZWaveJSSource) -> None:
    zw_entry(hass, ctrl=FakeController(home_id=OTHER_HOME_ID))
    assert (await source.async_swap_verify(b"nvm", {}))["home_id"] == OTHER_HOME_ID


@pytest.mark.parametrize(
    ("meta", "want"),
    [
        ({"home_id": HOME_ID, "home_id_hex": "0xE1A2B3C4"}, "0xE1A2B3C4"),
        ({"home_id": HOME_ID}, str(HOME_ID)),
    ],
)
async def test_swap_verify_mismatch(hass: HomeAssistant, source: ZWaveJSSource, meta, want: str) -> None:
    zw_entry(hass, ctrl=FakeController(home_id=OTHER_HOME_ID))
    with pytest.raises(RuntimeError) as err:
        await source.async_swap_verify(b"nvm", meta)
    assert "0xC0FFEE01" in str(err.value)
    assert want in str(err.value)
    assert "run the replacement again" in str(err.value)


# ============================================================== device check


async def test_check_devices_none_without_other_nodes(hass: HomeAssistant, source: ZWaveJSSource) -> None:
    zw_entry(hass, ctrl=ctrl_with_nodes())
    assert await source.async_check_devices(90) is None


async def test_check_devices_mains_pinged(hass: HomeAssistant, source: ZWaveJSSource) -> None:
    nodes = [
        FakeNode(2, name="Hall lamp", ping=True),
        FakeNode(3, name="Porch dimmer", ping=False),
        FakeNode(4, name=None, label="ZEN77", ping=RuntimeError("node dead")),
        FakeNode(5, name=None, label=None, listening=False, frequent="1000ms", ping=True),  # FLiRS lock
        FakeNode(6, name="", label="", listening=False, frequent="250ms", ping=TimeoutError),
    ]
    ctrl = ctrl_with_nodes(*nodes)
    zw_entry(hass, ctrl=ctrl)
    report = await source.async_check_devices(90)
    assert report == DeviceReport(
        mains_total=5,
        mains_back=2,
        battery_total=0,
        battery_back=0,
        missing_mains=sorted(["Porch dimmer", "ZEN77", "Node 6"]),
        missing_battery=[],
    )
    for n in nodes:
        n.async_ping.assert_awaited_once()
    ctrl.nodes[1].async_ping.assert_not_awaited()


async def test_check_devices_ping_timeout(hass: HomeAssistant, source: ZWaveJSSource) -> None:
    zw_entry(hass, ctrl=ctrl_with_nodes(FakeNode(2, name="Stuck", ping="hang"), FakeNode(3, name="Ok")))
    with patch(f"{ZW_MOD}.PING_TIMEOUT", 0.02):
        report = await source.async_check_devices(90)
    assert (report.mains_total, report.mains_back) == (2, 1)
    assert report.missing_mains == ["Stuck"]


async def test_check_devices_battery_heard_only_after_start(
    hass: HomeAssistant, source: ZWaveJSSource
) -> None:
    now = dt_util.utcnow()
    nodes = [
        FakeNode(2, name="Lamp", ping=True),
        FakeNode(3, name="Fresh sensor", listening=False, last_seen=now + timedelta(minutes=5)),
        FakeNode(4, name="Old sensor", listening=False, last_seen=now - timedelta(minutes=5)),
        FakeNode(5, name=None, label="Remote", listening=None, last_seen=None),
    ]
    zw_entry(hass, ctrl=ctrl_with_nodes(*nodes))
    report = await source.async_check_devices(0)  # no time to wait for wake-ups
    assert report == DeviceReport(
        mains_total=1,
        mains_back=1,
        battery_total=3,
        battery_back=1,
        missing_mains=[],
        missing_battery=["Old sensor", "Remote"],
    )
    for n in nodes[1:]:
        n.async_ping.assert_not_awaited()  # battery nodes are never pinged


async def test_check_devices_waits_for_battery_wakeup(
    hass: HomeAssistant, source: ZWaveJSSource, fast_sleep: _FastAsyncio
) -> None:
    sensor = FakeNode(2, name="Door", listening=False, last_seen=None)
    zw_entry(hass, ctrl=ctrl_with_nodes(sensor))
    task = asyncio.ensure_future(source.async_check_devices(90))
    for _ in range(5):
        await asyncio.sleep(0)
    assert not task.done()
    sensor.last_seen = dt_util.utcnow() + timedelta(seconds=1)  # it woke up and reported
    report = await task
    assert (report.battery_total, report.battery_back, report.missing_battery) == (1, 1, [])
    assert set(fast_sleep.sleeps) == {2}


async def test_check_devices_needs_loaded_controller(source: ZWaveJSSource) -> None:
    from custom_components.radio_guard.backup.base import SourceUnavailable

    with pytest.raises(SourceUnavailable):
        await source.async_check_devices(90)


# ================================================== wizard via coordinator_down


class WizardZWave(ZWaveJSSource):
    """Real Z-Wave JS source; only the NVM write is faked (it flips the home ID like a real restore)."""

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass)
        self.restored: list[tuple[bytes, dict[str, Any]]] = []

    async def async_restore(self, data: bytes, meta: dict[str, Any]) -> None:
        self.restored.append((data, meta))
        ctrl = self._controller()
        ctrl.home_id = meta["home_id"]
        ctrl.nodes = {
            **ctrl.nodes,
            2: FakeNode(2, name="Kitchen dimmer", ping=True),
            3: FakeNode(3, name="Porch switch", ping=False),
            4: FakeNode(4, name="Front door lock", listening=False, last_seen=None),
        }


@pytest.fixture(autouse=True)
def _no_min_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("custom_components.radio_guard.wizard.MIN_PROGRESS_SECONDS", 0)


@pytest.fixture
def entry_data(entry_data: dict[str, Any]) -> dict[str, Any]:
    return {**entry_data, CONF_SOURCES: [SOURCE_ZWAVE]}


@pytest.fixture
def wiz_source(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> WizardZWave:
    src = WizardZWave(hass)
    monkeypatch.setattr("custom_components.radio_guard.coordinator.ZWaveJSSource", lambda hass: src)
    monkeypatch.setattr("custom_components.radio_guard.wizard.DEVICE_TIMEOUT", 0)  # don't wait for wake-ups
    return src


def seed_nvm(root: Path, age: timedelta, meta: dict[str, Any]) -> str:
    created = dt_util.utcnow() - age
    stem = created.strftime("%Y%m%dT%H%M%SZ")
    root.mkdir(parents=True, exist_ok=True)
    name = f"{stem}.nvm"
    (root / name).write_bytes(b"NVM-" + stem.encode())
    full = {"source": SOURCE_ZWAVE, "created": created.isoformat(), "file": name, **meta}
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


async def _zwave_down(
    hass: HomeAssistant,
    freezer,
    mock_config_entry: MockConfigEntry,
    config_dir: Path,
    zwave_data: dict[str, Any],
) -> tuple[RadioGuardCoordinator, MockConfigEntry, str]:
    """Z-Wave JS stuck in setup retry (dead stick) long enough to raise coordinator_down."""
    zw = zw_entry(hass, data=zwave_data, state=ConfigEntryState.SETUP_RETRY, runtime=None)
    meta = {
        "format": "zwave-nvm-raw",
        "home_id": HOME_ID,
        "home_id_hex": f"0x{HOME_ID:08X}",
        "sdk_version": "7.23.2",
        "node_count": 4,
        "security_keys": dict(KEYS),
    }
    file = seed_nvm(config_dir / "radio_guard" / SOURCE_ZWAVE, timedelta(hours=2), meta)
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coord: RadioGuardCoordinator = mock_config_entry.runtime_data
    freezer.tick(timedelta(minutes=STARTUP_GRACE_MINUTES))
    await coord.async_refresh()
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=DEFAULT_OFFLINE_MINUTES))
    await coord.async_refresh()
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, DOWN) is not None
    return coord, zw, file


async def _open_pick(hass: HomeAssistant, mgr, file: str, seen: list) -> dict[str, Any]:
    result = await mgr.async_init(DOMAIN, data={"issue_id": DOWN})
    assert result["menu_options"] == ["wiz_pick", "wait"]
    result = await mgr.async_configure(result["flow_id"], {"next_step_id": "wiz_pick"})
    assert result["step_id"] == "wiz_pick"
    result = await mgr.async_configure(result["flow_id"], {"file": file})
    return await _drive(hass, mgr, result, seen)


async def test_wizard_external_server_end_to_end(
    hass: HomeAssistant,
    freezer,
    mock_config_entry: MockConfigEntry,
    config_dir: Path,
    wiz_source: WizardZWave,
) -> None:
    coord, zw, file = await _zwave_down(
        hass, freezer, mock_config_entry, config_dir, {"url": SERVER_URL, **KEYS}
    )
    st = coord.states[SOURCE_ZWAVE]
    mgr = repairs_flow_manager(hass)
    seen: list[tuple[str, str]] = []

    with patch(SERIAL_PORTS, return_value=[(OLD_PORT, "Old stick")]):
        result = await _open_pick(hass, mgr, file, seen)

    # nothing to stop: a "preparing" spinner, then the swap form without any port picker
    assert seen == [("wiz_stop", "preparing")]
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "wiz_swap"
    assert [str(k) for k in result["data_schema"].schema] == ["unplugged"]
    ph = result["description_placeholders"]
    assert ph["intro"] == "Radio Guard is ready."
    assert "Z-Wave JS UI" in ph["plug_in"]
    assert ph["current_line"] == ""
    assert st.swapping

    # the user points Z-Wave JS UI at the new (blank) stick; HA's integration comes back on it
    blank = ctrl_with_nodes()
    blank.home_id = OTHER_HOME_ID
    zw.runtime_data = SimpleNamespace(
        client=SimpleNamespace(connected=True, driver=SimpleNamespace(controller=blank))
    )
    zw.mock_state(hass, ConfigEntryState.LOADED)

    result = await mgr.async_configure(result["flow_id"], {"unplugged": True})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "wiz_confirm"
    ph = result["description_placeholders"]
    assert ph["network"] == "0xE1A2B3C4"
    assert ph["target"] == "the stick your Z-Wave JS server now uses"
    assert "waits for it on the new stick" in ph["start_note"]

    seen.clear()
    result = await mgr.async_configure(result["flow_id"], {})
    result = await _drive(hass, mgr, result, seen)
    assert seen == [("wiz_restore", "restoring"), ("wiz_devices", "checking_devices")]
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "wiz_done"
    assert len(wiz_source.restored) == 1
    data, meta = wiz_source.restored[0]
    assert data == (st.store.root / file).read_bytes()
    assert meta["home_id"] == HOME_ID
    assert blank.home_id == HOME_ID
    ph = result["description_placeholders"]
    assert ph["network"] == "0xE1A2B3C4"
    devices = ph["devices"]
    assert "Mains-powered devices back: 1 of 2" in devices
    assert "Porch switch" in devices
    assert "Battery devices heard from: 0 of 1" in devices
    assert "Front door lock" in devices

    assert not st.swapping
    assert ir.async_get(hass).async_get_issue(DOMAIN, DOWN) is None

    result = await mgr.async_configure(result["flow_id"], {"next_step_id": "wiz_finish"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


async def test_wizard_addon_reaches_confirm_with_addon_stopped(
    hass: HomeAssistant,
    freezer,
    mock_config_entry: MockConfigEntry,
    config_dir: Path,
    wiz_source: WizardZWave,
) -> None:
    mgr_addon = addon_mgr({"device": OLD_PORT})
    with (
        patch(IS_HASSIO, return_value=True),
        patch.object(wiz_source, "_addon_manager", return_value=mgr_addon),
    ):
        _coord, _zw, file = await _zwave_down(hass, freezer, mock_config_entry, config_dir, addon_data())
        mgr = repairs_flow_manager(hass)
        seen: list[tuple[str, str]] = []
        with patch(SERIAL_PORTS, side_effect=[[(OLD_PORT, "Old")], [(NEW_PORT, "New stick")]]):
            result = await _open_pick(hass, mgr, file, seen)
            assert seen == [("wiz_stop", "stopping")]
            mgr_addon.async_stop_addon.assert_awaited_once()
            assert result["step_id"] == "wiz_swap"
            key = next(k for k in result["data_schema"].schema if str(k) == "port")
            assert key.description == {"suggested_value": NEW_PORT}
        result = await mgr.async_configure(result["flow_id"], {"port": NEW_PORT, "unplugged": True})
    assert result["step_id"] == "wiz_confirm", result.get("description_placeholders")
