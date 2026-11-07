"""Zigbee2MQTT guided-replacement API: health, swap plan, stop/apply/verify and the device check."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from custom_components.radio_guard.backup.base import SwapPlan
from custom_components.radio_guard.backup.z2m import Zigbee2MQTTSource

from .conftest import BRIDGE_INFO_SAMPLE, OCBF_SAMPLE, make_z2m_zip

BASE = "zigbee2mqtt"
INFO = f"{BASE}/bridge/info"
STATE = f"{BASE}/bridge/state"
DEVICES = f"{BASE}/bridge/devices"
EPID = "dddddddddddddddd"
SLUG = "45df7312_zigbee2mqtt"
AUTO = SwapPlan(supported=True, automatic=True, choose_port=True)
MANUAL = SwapPlan(supported=True, automatic=False, choose_port=True)


# ------------------------------------------------------------------ fixtures


@pytest.fixture
async def source(hass: HomeAssistant, mqtt_mock) -> Zigbee2MQTTSource:
    src = Zigbee2MQTTSource(hass, BASE, None)
    await src.async_setup()
    yield src
    await src.async_teardown()


@pytest.fixture
def fast_timeouts():
    """Every asyncio.wait_for in z2m.py times out almost immediately."""
    real = asyncio.wait_for

    async def fast(fut, timeout):
        return await real(fut, 0.02)

    with patch("custom_components.radio_guard.backup.z2m.asyncio.wait_for", fast):
        yield


def _state(hass: HomeAssistant, state: str) -> None:
    async_fire_mqtt_message(hass, STATE, json.dumps({"state": state}))


def _info(hass: HomeAssistant, info: dict = BRIDGE_INFO_SAMPLE) -> None:
    async_fire_mqtt_message(hass, INFO, json.dumps(info))


def _data_dir(tmp_path: Path, cfg: dict | None = None) -> Path:
    d = tmp_path / "z2m"
    d.mkdir()
    (d / "configuration.yaml").write_text(
        yaml.safe_dump(
            cfg
            or {
                "mqtt": {"server": "mqtt://core-mosquitto"},
                "serial": {"port": "/dev/ttyOLD", "adapter": "zstack"},
                "advanced": {"pan_id": "GENERATE"},
            }
        )
    )
    return d


def _mgr(options: dict | None = None) -> MagicMock:
    mgr = MagicMock(addon_slug=SLUG)
    mgr.async_stop_addon = AsyncMock()
    mgr.async_start_addon = AsyncMock()
    mgr.async_set_addon_options = AsyncMock()
    mgr.async_get_addon_info = AsyncMock(return_value=MagicMock(options=options or {}))
    return mgr


def _published(mqtt_mock, topic: str) -> list:
    return [c for c in mqtt_mock.async_publish.call_args_list if c.args[0] == topic]


# -------------------------------------------------------------------- health


async def test_health_unknown_until_bridge_state(hass: HomeAssistant, source: Zigbee2MQTTSource) -> None:
    assert await source.async_health() is None


async def test_health_online_offline(hass: HomeAssistant, source: Zigbee2MQTTSource) -> None:
    _state(hass, "online")
    await hass.async_block_till_done()
    assert await source.async_health() is True
    _state(hass, "offline")
    await hass.async_block_till_done()
    assert await source.async_health() is False
    async_fire_mqtt_message(hass, STATE, "online")  # legacy plain payload
    await hass.async_block_till_done()
    assert await source.async_health() is True


async def test_health_unknown_when_broker_disconnected(
    hass: HomeAssistant, source: Zigbee2MQTTSource
) -> None:
    _state(hass, "offline")
    await hass.async_block_till_done()
    with patch("homeassistant.components.mqtt.is_connected", return_value=False):
        assert await source.async_health() is None


async def test_health_unknown_without_subscriptions_or_mqtt(hass: HomeAssistant) -> None:
    src = Zigbee2MQTTSource(hass, BASE, None)
    src._state = "offline"
    assert await src.async_health() is None  # mqtt not loaded / not set up


async def test_health_unknown_before_setup(hass: HomeAssistant, mqtt_mock) -> None:
    src = Zigbee2MQTTSource(hass, BASE, None)
    src._state = "offline"
    assert await src.async_health() is None


# ----------------------------------------------------------------- swap plan


async def test_swap_plan_addon(hass: HomeAssistant, source: Zigbee2MQTTSource, tmp_path: Path) -> None:
    data_dir = _data_dir(tmp_path)
    mgr = _mgr({"serial": {"port": "/dev/ttyADDON"}})
    with patch.object(source, "_addon", AsyncMock(return_value=(mgr, data_dir))):
        plan = await source.async_swap_plan()
    assert plan.supported and plan.automatic and plan.choose_port
    assert plan.current_port == "/dev/ttyADDON"  # add-on option beats configuration.yaml
    assert plan.details == {"data_dir": str(data_dir), "addon": SLUG}


async def test_swap_plan_addon_port_falls_back_to_config(
    hass: HomeAssistant, source: Zigbee2MQTTSource, tmp_path: Path
) -> None:
    data_dir = _data_dir(tmp_path)
    with patch.object(source, "_addon", AsyncMock(return_value=(_mgr({}), data_dir))):
        plan = await source.async_swap_plan()
    assert plan.automatic
    assert plan.current_port == "/dev/ttyOLD"


async def test_swap_plan_addon_data_invisible(hass: HomeAssistant, source: Zigbee2MQTTSource) -> None:
    with patch.object(source, "_addon", AsyncMock(return_value=(_mgr(), None))):
        plan = await source.async_swap_plan()
    assert not plan.supported
    assert "data_path" in plan.reason and "/config/zigbee2mqtt" in plan.reason


async def test_swap_plan_data_dir_without_addon_is_manual(
    hass: HomeAssistant, source: Zigbee2MQTTSource, tmp_path: Path
) -> None:
    data_dir = _data_dir(tmp_path)
    source.data_dir_option = str(data_dir)
    with patch.object(source, "_addon", AsyncMock(return_value=(None, None))):
        plan = await source.async_swap_plan()
    assert plan.supported and not plan.automatic and plan.choose_port
    assert plan.current_port == "/dev/ttyOLD"
    assert plan.details == {"data_dir": str(data_dir)}


async def test_swap_plan_nothing_found(hass: HomeAssistant, source: Zigbee2MQTTSource) -> None:
    with patch.object(source, "_addon", AsyncMock(return_value=(None, None))):
        plan = await source.async_swap_plan()
    assert not plan.supported
    assert "cannot see Zigbee2MQTT's data folder" in plan.reason


# ---------------------------------------------------------------------- ports


async def test_swap_ports_flags_newly_plugged(hass: HomeAssistant, source: Zigbee2MQTTSource) -> None:
    ports = [("/dev/serial/by-id/old", "Old stick")]
    target = "custom_components.radio_guard.backup.z2m._serial_ports"
    with patch(target, lambda: list(ports)):
        await source.async_swap_stop(MANUAL)  # not online -> returns at once, records ports
        ports.append(("/dev/serial/by-id/new", "New stick"))
        opts = await source.async_swap_ports()
    assert [(o.value, o.new) for o in opts] == [
        ("/dev/serial/by-id/new", True),
        ("/dev/serial/by-id/old", False),
    ]


# ----------------------------------------------------------------------- stop


async def test_swap_stop_automatic_stops_addon_and_waits_offline(
    hass: HomeAssistant, source: Zigbee2MQTTSource
) -> None:
    _state(hass, "online")
    await hass.async_block_till_done()
    mgr = _mgr()

    async def stop() -> None:
        _state(hass, "offline")

    mgr.async_stop_addon = AsyncMock(side_effect=stop)
    with (
        patch.object(source, "_addon", AsyncMock(return_value=(mgr, None))),
        patch("custom_components.radio_guard.backup.z2m._serial_ports", list),
    ):
        await source.async_swap_stop(AUTO)
    mgr.async_stop_addon.assert_awaited_once()
    assert source._state == "offline"


async def test_swap_stop_automatic_times_out_if_still_online(
    hass: HomeAssistant, source: Zigbee2MQTTSource, fast_timeouts
) -> None:
    _state(hass, "online")
    await hass.async_block_till_done()
    mgr = _mgr()
    with (
        patch.object(source, "_addon", AsyncMock(return_value=(mgr, None))),
        patch("custom_components.radio_guard.backup.z2m._serial_ports", list),
        pytest.raises(TimeoutError),
    ):
        await source.async_swap_stop(AUTO)
    mgr.async_stop_addon.assert_awaited_once()


async def test_swap_stop_manual_waits_for_offline(hass: HomeAssistant, source: Zigbee2MQTTSource) -> None:
    _state(hass, "online")
    await hass.async_block_till_done()
    with (
        patch.object(source, "_addon", AsyncMock(side_effect=AssertionError("manual never touches add-on"))),
        patch("custom_components.radio_guard.backup.z2m._serial_ports", list),
    ):
        task = hass.async_create_task(source.async_swap_stop(MANUAL))
        for _ in range(5):
            await asyncio.sleep(0)
        assert not task.done()
        _state(hass, "offline")
        await asyncio.wait_for(task, 1)


async def test_wait_stopped_returns_when_not_online(hass: HomeAssistant, source: Zigbee2MQTTSource) -> None:
    await source.async_wait_stopped(0.01)  # unknown state
    _state(hass, "offline")
    await hass.async_block_till_done()
    await source.async_wait_stopped(0.01)


# ---------------------------------------------------------------------- apply


async def test_swap_apply_refuses_while_online(
    hass: HomeAssistant, source: Zigbee2MQTTSource, tmp_path: Path
) -> None:
    _state(hass, "online")
    await hass.async_block_till_done()
    source.data_dir_option = str(_data_dir(tmp_path))
    with pytest.raises(RuntimeError, match="running again"):
        await source.async_swap_apply(make_z2m_zip(), {}, "/dev/ttyNEW")
    assert not (tmp_path / "z2m" / "coordinator_backup.json").exists()


async def test_swap_apply_needs_ocbf(hass: HomeAssistant, source: Zigbee2MQTTSource, tmp_path: Path) -> None:
    source.data_dir_option = str(_data_dir(tmp_path))
    with pytest.raises(RuntimeError, match="no coordinator_backup.json"):
        await source.async_swap_apply(make_z2m_zip(ocbf=None), {}, None)


async def test_swap_apply_needs_data_dir(hass: HomeAssistant, source: Zigbee2MQTTSource) -> None:
    with (
        patch.object(source, "_addon", AsyncMock(return_value=(None, None))),
        pytest.raises(RuntimeError, match="no longer reachable"),
    ):
        await source.async_swap_apply(make_z2m_zip(), {}, None)


async def test_swap_apply_writes_files_and_patches_config(
    hass: HomeAssistant, source: Zigbee2MQTTSource, tmp_path: Path
) -> None:
    _state(hass, "offline")
    _info(hass)
    await hass.async_block_till_done()
    data_dir = _data_dir(tmp_path)
    source.data_dir_option = str(data_dir)
    with patch.object(source, "_addon", AsyncMock(return_value=(None, None))):
        await source.async_swap_apply(make_z2m_zip(), {}, "/dev/ttyNEW")

    assert json.loads((data_dir / "coordinator_backup.json").read_text()) == OCBF_SAMPLE
    assert (data_dir / "database.db").exists()
    cfg = yaml.safe_load((data_dir / "configuration.yaml").read_text())
    assert cfg["serial"] == {"port": "/dev/ttyNEW", "adapter": "zstack"}
    assert cfg["mqtt"] == {"server": "mqtt://core-mosquitto"}  # user's own settings kept
    assert cfg["advanced"]["pan_id"] == 0x1A62
    assert cfg["advanced"]["ext_pan_id"] == [0xDD] * 8
    assert cfg["advanced"]["channel"] == 11
    assert cfg["advanced"]["network_key"] == list(bytes.fromhex(OCBF_SAMPLE["network_key"]["key"]))
    assert (data_dir / "configuration.yaml.radio_guard.bak").exists()
    # stale online/info from before the swap is forgotten
    assert not source._online_event.is_set()
    assert not source._info_event.is_set()
    assert source._info == {}


async def test_swap_apply_without_port_keeps_serial_port(
    hass: HomeAssistant, source: Zigbee2MQTTSource, tmp_path: Path
) -> None:
    data_dir = _data_dir(tmp_path)
    source.data_dir_option = str(data_dir)
    with patch.object(source, "_addon", AsyncMock(return_value=(None, None))):
        await source.async_swap_apply(make_z2m_zip(), {}, None)
    cfg = yaml.safe_load((data_dir / "configuration.yaml").read_text())
    assert cfg["serial"]["port"] == "/dev/ttyOLD"
    assert cfg["advanced"]["pan_id"] == 0x1A62


async def test_swap_apply_sets_addon_serial_port(
    hass: HomeAssistant, source: Zigbee2MQTTSource, tmp_path: Path
) -> None:
    data_dir = _data_dir(tmp_path)
    mgr = _mgr({"data_path": "/config/z2m", "serial": {"port": "/dev/ttyOLD", "adapter": "zstack"}})
    with patch.object(source, "_addon", AsyncMock(return_value=(mgr, data_dir))):
        await source.async_swap_apply(make_z2m_zip(), {}, "/dev/ttyNEW")
    mgr.async_set_addon_options.assert_awaited_once_with(
        {"data_path": "/config/z2m", "serial": {"port": "/dev/ttyNEW", "adapter": "zstack"}}
    )
    mgr.async_stop_addon.assert_not_awaited()
    mgr.async_start_addon.assert_not_awaited()
    assert yaml.safe_load((data_dir / "configuration.yaml").read_text())["serial"]["port"] == "/dev/ttyNEW"


async def test_swap_set_port_leaves_addon_options_without_serial(
    hass: HomeAssistant, source: Zigbee2MQTTSource, tmp_path: Path
) -> None:
    data_dir = _data_dir(tmp_path)
    mgr = _mgr({"data_path": "/config/z2m"})
    with patch.object(source, "_addon", AsyncMock(return_value=(mgr, data_dir))):
        await source.async_swap_set_port("/dev/ttyNEW")
        await source.async_swap_set_port(None)  # no-op
    mgr.async_set_addon_options.assert_not_awaited()
    assert yaml.safe_load((data_dir / "configuration.yaml").read_text())["serial"]["port"] == "/dev/ttyNEW"


# ---------------------------------------------------------------- start/verify


async def test_swap_start_only_for_automatic(hass: HomeAssistant, source: Zigbee2MQTTSource) -> None:
    mgr = _mgr()
    with patch.object(source, "_addon", AsyncMock(return_value=(mgr, None))):
        await source.async_swap_start(MANUAL)
        mgr.async_start_addon.assert_not_awaited()
        await source.async_swap_start(AUTO)
    mgr.async_start_addon.assert_awaited_once()


async def test_wait_started(hass: HomeAssistant, source: Zigbee2MQTTSource) -> None:
    task = hass.async_create_task(source.async_wait_started(None))
    await asyncio.sleep(0)
    assert not task.done()
    _state(hass, "online")
    await asyncio.wait_for(task, 1)


async def test_swap_verify_returns_identity_on_match(hass: HomeAssistant, source: Zigbee2MQTTSource) -> None:
    _state(hass, "online")
    _info(hass)
    await hass.async_block_till_done()
    ident = await source.async_swap_verify(make_z2m_zip(), {})
    assert ident["extended_pan_id"] == EPID
    assert ident["ieee"] == "00124b0024c2f0a1"


async def test_swap_verify_raises_on_ext_pan_mismatch(hass: HomeAssistant, source: Zigbee2MQTTSource) -> None:
    _state(hass, "online")
    _info(hass, {**BRIDGE_INFO_SAMPLE, "network": {"pan_id": 1, "extended_pan_id": "0x0102030405060708"}})
    await hass.async_block_till_done()
    with pytest.raises(
        RuntimeError, match="different network \\(0102030405060708\\).*\\(dddddddddddddddd\\)"
    ):
        await source.async_swap_verify(make_z2m_zip(), {})


async def test_swap_verify_times_out_without_info(
    hass: HomeAssistant, source: Zigbee2MQTTSource, fast_timeouts
) -> None:
    with pytest.raises(RuntimeError, match="did not report its network"):
        await source.async_swap_verify(make_z2m_zip(), {})


# ------------------------------------------------------------- device check

DEVICE_LIST = [
    {"friendly_name": "Coordinator", "type": "Coordinator"},
    {"friendly_name": "Lamp", "type": "Router"},
    {"friendly_name": "Plug", "type": "Router"},
    {"friendly_name": "Sensor", "type": "EndDevice"},
    {"friendly_name": "Remote", "type": "EndDevice"},
]


async def _with_devices(hass: HomeAssistant, devices: list = DEVICE_LIST) -> None:
    async_fire_mqtt_message(hass, DEVICES, json.dumps(devices), retain=True)
    await hass.async_block_till_done()


async def _wait_for(cond, what: str) -> None:
    for _ in range(400):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


async def test_check_devices_none_without_device_list(hass: HomeAssistant, source: Zigbee2MQTTSource) -> None:
    assert await source.async_check_devices(0.1) is None


async def test_check_devices_counts_only_live_messages(
    hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource
) -> None:
    await _with_devices(hass)
    task = hass.async_create_task(source.async_check_devices(0.5))
    await _wait_for(lambda: _published(mqtt_mock, f"{BASE}/Plug/get"), "the /get requests")

    # none of these prove a device is on the new coordinator
    async_fire_mqtt_message(hass, f"{BASE}/Plug", '{"state":"ON"}', retain=True)  # retained
    async_fire_mqtt_message(hass, f"{BASE}/Plug/set", '{"state":"ON"}')  # our own command echo
    async_fire_mqtt_message(hass, f"{BASE}/Plug/get", '{"state":""}')  # our own request echo
    async_fire_mqtt_message(hass, f"{BASE}/Plug/availability", '{"state":"offline"}')
    async_fire_mqtt_message(hass, f"{BASE}/bridge/state", '{"state":"online"}')
    # these do
    async_fire_mqtt_message(hass, f"{BASE}/Lamp", '{"state":"ON"}')
    async_fire_mqtt_message(hass, f"{BASE}/Sensor/availability", '{"state":"online"}')

    report = await asyncio.wait_for(task, 5)
    assert report.mains_total == 2
    assert report.mains_back == 1
    assert report.missing_mains == ["Plug"]
    assert report.battery_total == 2
    assert report.battery_back == 1
    assert report.missing_battery == ["Remote"]


async def test_check_devices_publishes_get_to_routers_only(
    hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource
) -> None:
    await _with_devices(hass)
    await source.async_check_devices(0.05)
    gets = [c.args[0] for c in mqtt_mock.async_publish.call_args_list if c.args[0].endswith("/get")]
    assert gets == [f"{BASE}/Lamp/get", f"{BASE}/Plug/get"]
    assert json.loads(_published(mqtt_mock, f"{BASE}/Lamp/get")[0].args[1]) == {"state": ""}


async def test_check_devices_all_back(hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource) -> None:
    await _with_devices(hass)
    task = hass.async_create_task(source.async_check_devices(0.3))
    await _wait_for(lambda: _published(mqtt_mock, f"{BASE}/Plug/get"), "the /get requests")
    for name in ("Lamp", "Plug", "Sensor", "Remote"):
        async_fire_mqtt_message(hass, f"{BASE}/{name}", "{}")
    report = await asyncio.wait_for(task, 5)
    assert (report.mains_back, report.mains_total, report.battery_back, report.battery_total) == (2, 2, 2, 2)
    assert report.missing_mains == report.missing_battery == []
