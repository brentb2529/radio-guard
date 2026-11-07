"""Zigbee2MQTT backup source: persistent subscriptions + transaction correlation."""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from custom_components.radio_guard.backup.base import RestoreStaged, SourceUnavailable
from custom_components.radio_guard.backup.ident import norm_hex, norm_pan_id
from custom_components.radio_guard.backup.z2m import (
    Zigbee2MQTTSource,
    advanced_from_ocbf,
    config_mismatches,
    summarize_ocbf,
)
from custom_components.radio_guard.const import Z2M_BACKUP_TIMEOUT
from custom_components.radio_guard.storage import BackupStore

from .conftest import BRIDGE_INFO_SAMPLE, OCBF_SAMPLE, Z2M_CONFIG_SAMPLE, make_z2m_zip, z2m_zip_b64

REQ = "zigbee2mqtt/bridge/request/backup"
RESP = "zigbee2mqtt/bridge/response/backup"
INFO = "zigbee2mqtt/bridge/info"
STATE = "zigbee2mqtt/bridge/state"

IEEE = "00124b0024c2f0a1"
EPID = "dddddddddddddddd"


@pytest.fixture
async def source(hass: HomeAssistant, mqtt_mock) -> Zigbee2MQTTSource:
    """A source with live subscriptions, torn down after the test."""
    src = Zigbee2MQTTSource(hass, "zigbee2mqtt/")
    await src.async_setup()
    yield src
    await src.async_teardown()


@pytest.fixture
def fast_timeouts():
    """Make every asyncio.wait_for in z2m.py time out almost immediately."""
    real = asyncio.wait_for

    async def fast(fut, timeout):
        return await real(fut, 0.02)

    with patch("custom_components.radio_guard.backup.z2m.asyncio.wait_for", fast):
        yield


@pytest.fixture
def no_addon(source: Zigbee2MQTTSource):
    with patch.object(source, "_addon", AsyncMock(return_value=(None, None))):
        yield


def _published(mqtt_mock, topic: str) -> list:
    return [c for c in mqtt_mock.async_publish.call_args_list if c.args[0] == topic]


def _subscribed_topics(mqtt_mock, prefix: str = "z2m/") -> list[str]:
    """Topics we subscribed to, ignoring HA's own discovery subscriptions."""
    return [c.args[0] for c in mqtt_mock.async_subscribe.call_args_list if c.args[0].startswith(prefix)]


# HA's MQTT client hands a retained message to each subscription only once (it is the
# "initial state" replay). A real broker forwards later publishes to existing subscribers
# with retain=False, so model a live Z2M that way; tests that need the retained replay
# pass retain=True explicitly.
def _publish_info(hass: HomeAssistant, info: dict = BRIDGE_INFO_SAMPLE, retain: bool = False) -> None:
    async_fire_mqtt_message(hass, INFO, json.dumps(info), retain=retain)


def _publish_state(hass: HomeAssistant, state: str, as_json: bool = True, retain: bool = False) -> None:
    async_fire_mqtt_message(hass, STATE, json.dumps({"state": state}) if as_json else state, retain=retain)


async def _wait_for_request(mqtt_mock) -> dict:
    """Wait until the backup request is published and return its parsed payload."""
    for _ in range(100):
        await asyncio.sleep(0)
        if calls := _published(mqtt_mock, REQ):
            return json.loads(calls[-1].args[1])
    raise AssertionError("no backup request was published")


def _respond_ok(hass: HomeAssistant, txn: str, zip_bytes: bytes | None = None) -> None:
    async_fire_mqtt_message(
        hass,
        RESP,
        json.dumps(
            {"status": "ok", "transaction": txn, "data": {"zip": z2m_zip_b64(zip_bytes or make_z2m_zip())}}
        ),
    )


# ---------------------------------------------------------------- lifecycle


async def test_basic_attributes(hass: HomeAssistant, source: Zigbee2MQTTSource) -> None:
    assert source.base_topic == "zigbee2mqtt"
    assert source.id == "zigbee2mqtt"
    assert source.name == "Zigbee2MQTT"
    assert source.available() is True


async def test_setup_subscribes_once_and_teardown_releases(hass: HomeAssistant, mqtt_mock) -> None:
    src = Zigbee2MQTTSource(hass, "z2m")
    await src.async_setup()
    assert _subscribed_topics(mqtt_mock) == [
        "z2m/bridge/info",
        "z2m/bridge/state",
        "z2m/bridge/response/backup",
    ]
    assert len(src._unsubs) == 3
    await src.async_setup()  # idempotent
    assert len(_subscribed_topics(mqtt_mock)) == 3

    await src.async_teardown()
    assert src._unsubs == []
    async_fire_mqtt_message(hass, "z2m/bridge/info", json.dumps(BRIDGE_INFO_SAMPLE))
    await hass.async_block_till_done()
    assert src._info == {}


async def test_setup_waits_for_mqtt_client(hass: HomeAssistant, mqtt_mock) -> None:
    src = Zigbee2MQTTSource(hass, "z2m")
    with patch(
        "homeassistant.components.mqtt.async_wait_for_mqtt_client", AsyncMock(return_value=True)
    ) as wait:
        await src.async_setup()
    wait.assert_awaited_once_with(hass)
    await src.async_teardown()


async def test_setup_noop_without_mqtt(hass: HomeAssistant) -> None:
    src = Zigbee2MQTTSource(hass, "zigbee2mqtt")
    assert src.available() is False
    await src.async_setup()
    assert src._unsubs == []
    with pytest.raises(SourceUnavailable, match="MQTT integration is not loaded"):
        await src.async_identity()
    with pytest.raises(SourceUnavailable, match="MQTT integration is not loaded"):
        await src.async_backup()
    await src.async_teardown()


async def test_teardown_cancels_pending_requests(
    hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource
) -> None:
    task = hass.async_create_task(source.async_backup())
    await _wait_for_request(mqtt_mock)
    assert len(source._pending) == 1
    await source.async_teardown()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert source._pending == {}


async def test_bridge_state_tracking(hass: HomeAssistant, source: Zigbee2MQTTSource) -> None:
    assert source._state is None
    _publish_state(hass, "online")
    await hass.async_block_till_done()
    assert source._state == "online"
    assert source._online_event.is_set()

    _publish_info(hass)
    await hass.async_block_till_done()
    assert source._info_event.is_set()

    _publish_state(hass, "offline", as_json=False)  # legacy plain-string payload
    await hass.async_block_till_done()
    assert source._state == "offline"
    assert not source._online_event.is_set()
    assert not source._info_event.is_set()


# ----------------------------------------------------------------- identity


async def test_identity_from_retained_bridge_info(hass: HomeAssistant, source: Zigbee2MQTTSource) -> None:
    _publish_state(hass, "online", retain=True)
    _publish_info(hass, retain=True)
    await hass.async_block_till_done()
    ident = await source.async_identity()
    assert ident == {
        "ieee": IEEE,  # 0x prefix stripped, lower-cased
        "coordinator_type": "zStack3x0",
        "coordinator_version": 20240710,
        "pan_id": "0x1a62",  # int 6754 -> canonical hex
        "extended_pan_id": EPID,
        "channel": 11,
        "z2m_version": "1.42.0",
        "zigbee_herdsman": "2.1.3",
        "bridge_state": "online",
        "device_count": None,
    }


async def test_identity_herdsman_dict_and_device_list(hass: HomeAssistant, source: Zigbee2MQTTSource) -> None:
    info = {**BRIDGE_INFO_SAMPLE, "zigbee_herdsman": {"version": "3.0.0"}, "devices": [{"a": 1}, {"b": 2}]}
    _publish_info(hass, info)
    await hass.async_block_till_done()
    ident = await source.async_identity()
    assert ident["zigbee_herdsman"] == "3.0.0"
    assert ident["device_count"] == 2
    assert ident["bridge_state"] is None


async def test_identity_waits_for_late_bridge_info(hass: HomeAssistant, source: Zigbee2MQTTSource) -> None:
    task = hass.async_create_task(source.async_identity())
    await asyncio.sleep(0)
    assert not task.done()
    _publish_info(hass)
    ident = await task
    assert ident["ieee"] == IEEE


async def test_identity_tracks_latest_bridge_info(hass: HomeAssistant, source: Zigbee2MQTTSource) -> None:
    _publish_info(hass)
    _publish_info(hass, {**BRIDGE_INFO_SAMPLE, "version": "2.0.0"})
    await hass.async_block_till_done()
    assert (await source.async_identity())["z2m_version"] == "2.0.0"


async def test_identity_without_bridge_info_raises_unavailable(
    hass: HomeAssistant, source: Zigbee2MQTTSource, fast_timeouts
) -> None:
    with pytest.raises(SourceUnavailable, match="No retained zigbee2mqtt/bridge/info"):
        await source.async_identity()


async def test_identity_ignores_bad_json_info(
    hass: HomeAssistant, source: Zigbee2MQTTSource, fast_timeouts
) -> None:
    async_fire_mqtt_message(hass, INFO, "not json", retain=True)
    await hass.async_block_till_done()
    assert source._info == {}
    with pytest.raises(SourceUnavailable):
        await source.async_identity()


async def test_identity_partial_info(hass: HomeAssistant, source: Zigbee2MQTTSource) -> None:
    _publish_info(hass, {"version": "1.0"})
    await hass.async_block_till_done()
    ident = await source.async_identity()
    assert ident["z2m_version"] == "1.0"
    assert ident["ieee"] is None
    assert ident["pan_id"] is None


# ------------------------------------------------------------------- backup


async def test_backup_success_with_transaction(
    hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource
) -> None:
    _publish_info(hass)
    zip_bytes = make_z2m_zip()
    task = hass.async_create_task(source.async_backup())
    req = await _wait_for_request(mqtt_mock)

    assert req == {"transaction": "rg-1"}
    [call] = _published(mqtt_mock, REQ)
    assert call.kwargs.get("qos", call.args[2] if len(call.args) > 2 else None) == 1

    _respond_ok(hass, "rg-1", zip_bytes)
    payload = await task

    assert payload.ext == ".zip"
    assert payload.data == zip_bytes
    meta = payload.meta
    assert meta["format"] == "zigbee2mqtt-zip"
    assert set(meta["members"]) == {
        "coordinator_backup.json",
        "database.db",
        "configuration.yaml",
        "state.json",
    }
    assert meta["device_count"] == 2
    assert meta["ocbf_version"] == 1
    assert meta["ocbf_source"] == "zigbee2mqtt@1.42.0"
    assert meta["stack"] == "zstack"
    assert meta["frame_counter"] == 1234
    # OCBF is trusted for network identity (normalised); bridge/info supplies software versions
    assert meta["ieee"] == IEEE
    assert meta["pan_id"] == "0x1a62"
    assert meta["extended_pan_id"] == EPID
    assert meta["channel"] == 11
    assert meta["z2m_version"] == "1.42.0"
    assert meta["zigbee_herdsman"] == "2.1.3"
    assert meta["coordinator_type"] == "zStack3x0"
    assert meta["coordinator_version"] == 20240710
    # configuration.yaml in the zip is inspected for the serial settings
    assert meta["serial_adapter"] == "zstack"
    assert meta["serial_port"] == "/dev/ttyUSB0"
    assert "warning" not in meta
    assert source._pending == {}


async def test_backup_writes_zip_and_sidecar_through_store(
    hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource, tmp_path: Path
) -> None:
    _publish_info(hass)
    store = BackupStore(tmp_path / "rg", "zigbee2mqtt", 5)
    task = hass.async_create_task(source.async_backup())
    req = await _wait_for_request(mqtt_mock)
    _respond_ok(hass, req["transaction"])
    payload = await task
    rec = await hass.async_add_executor_job(store.write, payload.data, payload.ext, payload.meta)
    assert rec.path.suffix == ".zip"
    assert rec.path.exists()
    assert zipfile.is_zipfile(rec.path)
    sidecar = json.loads((rec.path.parent / f"{rec.path.stem}.meta.json").read_text())
    assert sidecar["ieee"] == IEEE
    assert sidecar["pan_id"] == "0x1a62"
    assert sidecar["device_count"] == 2
    assert sidecar["source"] == "zigbee2mqtt"
    assert sidecar["file"] == rec.name


async def test_backup_without_ocbf_adds_warning_and_falls_back_to_bridge_identity(
    hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource
) -> None:
    _publish_info(hass)
    task = hass.async_create_task(source.async_backup())
    await _wait_for_request(mqtt_mock)
    _respond_ok(hass, "rg-1", make_z2m_zip(ocbf=None))
    payload = await task
    assert "coordinator_backup.json" not in payload.meta["members"]
    assert "only zstack and ember do" in payload.meta["warning"]
    # identity falls back to bridge/info
    assert payload.meta["ieee"] == IEEE
    assert payload.meta["pan_id"] == "0x1a62"
    assert payload.meta["channel"] == 11


async def test_backup_identity_failure_is_best_effort(
    hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource, fast_timeouts
) -> None:
    """No bridge/info: meta still carries the OCBF identity, no version fields."""
    task = hass.async_create_task(source.async_backup())
    await _wait_for_request(mqtt_mock)
    _respond_ok(hass, "rg-1")
    payload = await task
    assert payload.meta["ieee"] == IEEE
    assert payload.meta["pan_id"] == "0x1a62"
    assert payload.meta["device_count"] == 2
    assert "z2m_version" not in payload.meta


async def test_backup_ignores_response_for_other_transaction(
    hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource
) -> None:
    """Two in-flight requests (bypassing the lock) are matched by transaction, out of order."""
    _publish_info(hass)
    t1 = hass.async_create_task(source._request("backup", {}, 60))
    await _wait_for_request(mqtt_mock)
    t2 = hass.async_create_task(source._request("backup", {}, 60))
    for _ in range(50):
        await asyncio.sleep(0)
        if len(_published(mqtt_mock, REQ)) == 2:
            break
    assert set(source._pending) == {"rg-1", "rg-2"}

    async_fire_mqtt_message(
        hass, RESP, json.dumps({"status": "error", "transaction": "someone-else", "error": "x"})
    )
    await asyncio.sleep(0)
    assert not t1.done() and not t2.done()

    async_fire_mqtt_message(hass, RESP, json.dumps({"status": "ok", "transaction": "rg-2"}))
    async_fire_mqtt_message(
        hass, RESP, json.dumps({"status": "error", "transaction": "rg-1", "error": "busy"})
    )
    assert (await t2)["status"] == "ok"
    assert (await t1)["error"] == "busy"
    assert source._pending == {}


async def test_backup_serialized_by_lock(hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource) -> None:
    """Concurrent async_backup calls are serialized: second request is only sent after the first completes."""
    _publish_info(hass)
    t1 = hass.async_create_task(source.async_backup())
    t2 = hass.async_create_task(source.async_backup())
    await _wait_for_request(mqtt_mock)
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(_published(mqtt_mock, REQ)) == 1
    _respond_ok(hass, "rg-1")
    await t1
    req2 = await _wait_for_request(mqtt_mock)
    assert req2["transaction"] == "rg-2"
    _respond_ok(hass, "rg-2")
    await t2


async def test_backup_legacy_response_without_transaction(
    hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource
) -> None:
    """Older Z2M that does not echo the transaction still satisfies a lone waiter."""
    _publish_info(hass)
    task = hass.async_create_task(source.async_backup())
    await _wait_for_request(mqtt_mock)
    async_fire_mqtt_message(
        hass, RESP, json.dumps({"status": "ok", "data": {"zip": z2m_zip_b64(make_z2m_zip())}})
    )
    payload = await task
    assert payload.ext == ".zip"


async def test_backup_transaction_ids_increment(
    hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource
) -> None:
    _publish_info(hass)
    for n in (1, 2, 3):
        task = hass.async_create_task(source.async_backup())
        req = await _wait_for_request(mqtt_mock)
        assert req["transaction"] == f"rg-{n}"
        _respond_ok(hass, f"rg-{n}")
        await task


async def test_backup_error_status(hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource) -> None:
    task = hass.async_create_task(source.async_backup())
    await _wait_for_request(mqtt_mock)
    async_fire_mqtt_message(
        hass,
        RESP,
        json.dumps(
            {"status": "error", "transaction": "rg-1", "error": "Coordinator does not support backup"}
        ),
    )
    with pytest.raises(RuntimeError, match="Z2M backup failed: Coordinator does not support backup"):
        await task
    assert source._pending == {}


async def test_backup_ok_without_zip(hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource) -> None:
    task = hass.async_create_task(source.async_backup())
    await _wait_for_request(mqtt_mock)
    async_fire_mqtt_message(hass, RESP, json.dumps({"status": "ok", "transaction": "rg-1", "data": {}}))
    with pytest.raises(RuntimeError, match="no zip payload"):
        await task


async def test_backup_bad_json_response_is_ignored_then_times_out(
    hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource, fast_timeouts, caplog
) -> None:
    task = hass.async_create_task(source.async_backup())
    await _wait_for_request(mqtt_mock)
    async_fire_mqtt_message(hass, RESP, "{not json")
    with pytest.raises(TimeoutError):
        await task
    assert "Bad JSON on zigbee2mqtt/bridge/response/backup" in caplog.text


async def test_backup_timeout_message_and_cleanup(
    hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource, fast_timeouts
) -> None:
    with pytest.raises(
        TimeoutError, match="No response from Zigbee2MQTT on zigbee2mqtt/bridge/response/backup"
    ) as ei:
        await source.async_backup()
    assert "base topic 'zigbee2mqtt'" in str(ei.value)
    assert source._pending == {}
    async_fire_mqtt_message(hass, RESP, json.dumps({"status": "ok", "transaction": "rg-1"}))
    await hass.async_block_till_done()  # late reply is harmless


async def test_backup_uses_configured_timeout(
    hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource
) -> None:
    seen: list[float] = []

    async def fake_wait_for(fut, timeout):
        seen.append(timeout)
        raise TimeoutError

    with (
        patch("custom_components.radio_guard.backup.z2m.asyncio.wait_for", fake_wait_for),
        pytest.raises(TimeoutError),
    ):
        await source.async_backup()
    assert seen == [Z2M_BACKUP_TIMEOUT]


async def test_request_lazily_sets_up(hass: HomeAssistant, mqtt_mock) -> None:
    src = Zigbee2MQTTSource(hass, "zigbee2mqtt")
    assert src._unsubs == []
    task = hass.async_create_task(src.async_backup())
    await _wait_for_request(mqtt_mock)
    assert len(src._unsubs) == 3
    _respond_ok(hass, "rg-1")
    await task
    await src.async_teardown()


# ---------------------------------------------------------- pure helpers


def test_norm_helpers() -> None:
    assert norm_hex("0x00124B0024C2F0A1") == IEEE
    assert norm_hex("00:12:4b:00:24:c2:f0:a1") == IEEE
    assert norm_hex(None) is None
    assert norm_hex("0x") is None
    assert norm_pan_id(6754) == "0x1a62"
    assert norm_pan_id("1a62") == "0x1a62"
    assert norm_pan_id("0x1A62") == "0x1a62"
    assert norm_pan_id("41234") == "0xa112"  # >4 digits is decimal (0xa112 == 41234)
    assert norm_pan_id("1234") == "0x1234"  # 4 chars is treated as hex
    assert norm_pan_id("zz") is None
    assert norm_pan_id(None) is None


def test_summarize_ocbf() -> None:
    assert summarize_ocbf(OCBF_SAMPLE) == {
        "ocbf_version": 1,
        "ocbf_format": "zigpy/open-coordinator-backup",
        "ocbf_source": "zigbee2mqtt@1.42.0",
        "stack": "zstack",
        "pan_id": "0x1a62",
        "extended_pan_id": EPID,
        "channel": 11,
        "ieee": IEEE,
        "nwk_update_id": 0,
        "frame_counter": 1234,
        "device_count": 2,
    }
    empty = summarize_ocbf({})
    assert empty["device_count"] == 0
    assert empty["stack"] is None
    assert empty["ieee"] is None


def test_advanced_from_ocbf() -> None:
    adv = advanced_from_ocbf(OCBF_SAMPLE)
    assert adv == {
        "pan_id": 0x1A62,
        "ext_pan_id": [0xDD] * 8,
        "network_key": [1, 3, 5, 7, 9, 11, 13, 15, 0, 2, 4, 6, 8, 10, 12, 13],
        "channel": 11,
    }
    assert advanced_from_ocbf({}) == {}


def test_config_mismatches() -> None:
    assert config_mismatches(Z2M_CONFIG_SAMPLE, OCBF_SAMPLE) == []
    cfg = yaml.safe_load(yaml.safe_dump(Z2M_CONFIG_SAMPLE))
    cfg["advanced"]["pan_id"] = "GENERATE"
    cfg["advanced"]["network_key"] = "GENERATE"
    cfg["advanced"]["channel"] = 15
    cfg["advanced"]["ext_pan_id"] = [0] * 8
    problems = config_mismatches(cfg, OCBF_SAMPLE)
    assert "advanced.pan_id is GENERATE" in problems
    assert "advanced.network_key is GENERATE" in problems
    assert "advanced.ext_pan_id differs" in problems
    assert "advanced.channel is 15, backup needs 11" in problems
    # tuple form of lists is accepted
    cfg2 = {"advanced": {**advanced_from_ocbf(OCBF_SAMPLE)}}
    cfg2["advanced"]["ext_pan_id"] = tuple(cfg2["advanced"]["ext_pan_id"])
    assert config_mismatches(cfg2, OCBF_SAMPLE) == []
    # no advanced section at all
    assert len(config_mismatches({}, OCBF_SAMPLE)) == 4


def test_inspect_zip() -> None:
    meta = Zigbee2MQTTSource._inspect_zip(make_z2m_zip())
    assert meta["format"] == "zigbee2mqtt-zip"
    assert meta["ieee"] == IEEE
    assert meta["serial_adapter"] == "zstack"
    # directories are not listed as members
    data = make_z2m_zip(extra={"log/x.txt": b"log", "ota/": b""})
    meta = Zigbee2MQTTSource._inspect_zip(data)
    assert "ota/" not in meta["members"]
    assert "log/x.txt" in meta["members"]
    no_ocbf = Zigbee2MQTTSource._inspect_zip(make_z2m_zip(ocbf=None))
    assert "ieee" not in no_ocbf
    assert no_ocbf["serial_port"] == "/dev/ttyUSB0"


def test_inspect_zip_tolerates_secret_tags() -> None:
    cfg = "mqtt:\n  password: !secret mqtt_pw\nserial:\n  port: /dev/ttyACM0\n  adapter: ember\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("configuration.yaml", cfg)
    meta = Zigbee2MQTTSource._inspect_zip(buf.getvalue())
    assert meta["serial_adapter"] == "ember"
    assert meta["serial_port"] == "/dev/ttyACM0"


def test_inspect_zip_rejects_garbage() -> None:
    with pytest.raises(zipfile.BadZipFile):
        Zigbee2MQTTSource._inspect_zip(b"not a zip")


def test_ocbf_from_zip() -> None:
    assert Zigbee2MQTTSource._ocbf_from_zip(make_z2m_zip()) == OCBF_SAMPLE
    assert Zigbee2MQTTSource._ocbf_from_zip(make_z2m_zip(ocbf=None)) is None


def test_fallback_data_dir(hass: HomeAssistant, config_dir: Path) -> None:
    src = Zigbee2MQTTSource(hass, "z")
    assert src._fallback_data_dir() is None
    d = config_dir / "zigbee2mqtt"
    d.mkdir()
    (d / "configuration.yaml").write_text("x: 1")
    assert src._fallback_data_dir() == d


def test_write_restore_files_stages_with_patched_backup_config(tmp_path: Path) -> None:
    """Staging (no live config): every data file is written, advanced.* comes from the OCBF."""
    dest = tmp_path / "staging" / "nested"
    bak_cfg = yaml.safe_load(yaml.safe_dump(Z2M_CONFIG_SAMPLE))
    bak_cfg["advanced"]["pan_id"] = "GENERATE"
    data = make_z2m_zip(
        config=bak_cfg, extra={"log/old.txt": b"x", "ota/fw.bin": b"y", "secret.yaml": b"mqtt_pw: hunter2\n"}
    )

    Zigbee2MQTTSource._write_restore_files(data, dest, OCBF_SAMPLE, False)

    assert json.loads((dest / "coordinator_backup.json").read_text()) == OCBF_SAMPLE
    assert (dest / "database.db").exists()
    assert (dest / "state.json").exists()
    assert not (dest / "log").exists()
    assert not (dest / "ota").exists()
    # secret.yaml only when absent at destination
    assert (dest / "secret.yaml").read_text() == "mqtt_pw: hunter2\n"
    cfg = yaml.safe_load((dest / "configuration.yaml").read_text())
    assert cfg["advanced"]["pan_id"] == 0x1A62
    assert cfg["advanced"]["ext_pan_id"] == [0xDD] * 8
    assert cfg["advanced"]["channel"] == 11
    assert cfg["serial"]["port"] == "/dev/ttyUSB0"  # rest of the backup config kept
    assert not list(dest.glob("*.tmp"))
    assert oct((dest / "configuration.yaml").stat().st_mode & 0o777) == "0o600"


def test_write_restore_files_patches_live_config(tmp_path: Path) -> None:
    """Add-on path: the live configuration.yaml is kept (serial/mqtt) with advanced.* patched and backed up."""
    dest = tmp_path / "live"
    dest.mkdir()
    live_cfg = {
        "mqtt": {"server": "mqtt://core-mosquitto", "password": "!secret mqtt_pw"},
        "serial": {"port": "/dev/ttyACM9", "adapter": "ember"},
        "advanced": {"pan_id": "GENERATE", "channel": 25},
        "devices": {},
    }
    (dest / "configuration.yaml").write_text(yaml.safe_dump(live_cfg))
    (dest / "secret.yaml").write_text("mqtt_pw: keepme\n")
    (dest / "database.db").write_bytes(b"old db")
    data = make_z2m_zip(extra={"secret.yaml": b"mqtt_pw: fromzip\n"})

    Zigbee2MQTTSource._write_restore_files(data, dest, OCBF_SAMPLE, True)

    cfg = yaml.safe_load((dest / "configuration.yaml").read_text())
    assert cfg["serial"] == {"port": "/dev/ttyACM9", "adapter": "ember"}
    assert cfg["mqtt"]["server"] == "mqtt://core-mosquitto"
    assert cfg["advanced"] == {
        "pan_id": 0x1A62,
        "channel": 11,
        "ext_pan_id": [0xDD] * 8,
        "network_key": advanced_from_ocbf(OCBF_SAMPLE)["network_key"],
    }
    # live config is backed up first; secret.yaml is not overwritten; database is replaced
    bak = yaml.safe_load((dest / "configuration.yaml.radio_guard.bak").read_text())
    assert bak["advanced"]["pan_id"] == "GENERATE"
    assert (dest / "secret.yaml").read_text() == "mqtt_pw: keepme\n"
    assert (dest / "database.db").read_bytes() != b"old db"


# ------------------------------------------------------------ add-on mgmt


async def test_addon_none_without_supervisor(hass: HomeAssistant, source: Zigbee2MQTTSource) -> None:
    assert await source._addon() == (None, None)


def _fake_addon_manager(states: dict[str, object], options: dict[str, dict] | None = None):
    options = options or {}

    class FakeMgr:
        def __init__(self, hass_, logger, name, slug):
            self.addon_slug = slug

        async def async_get_addon_info(self):
            st = states[self.addon_slug]
            if isinstance(st, Exception):
                raise st
            return MagicMock(state=st, options=options.get(self.addon_slug, {}))

    return FakeMgr


async def test_addon_picks_installed_slug_and_maps_config_path(
    hass: HomeAssistant, source: Zigbee2MQTTSource
) -> None:
    from homeassistant.components.hassio import AddonState

    FakeMgr = _fake_addon_manager(
        {"45df7312_zigbee2mqtt": AddonState.NOT_INSTALLED, "45df7312_zigbee2mqtt_edge": AddonState.RUNNING},
        {"45df7312_zigbee2mqtt_edge": {"data_path": "/config/z2m-edge"}},
    )
    with (
        patch("homeassistant.helpers.hassio.is_hassio", return_value=True),
        patch("homeassistant.components.hassio.AddonManager", FakeMgr),
    ):
        mgr, data_dir = await source._addon()
    assert mgr.addon_slug == "45df7312_zigbee2mqtt_edge"
    assert data_dir == Path(hass.config.path("z2m-edge"))


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({}, lambda hass: Path(hass.config.path("zigbee2mqtt"))),  # default /config/zigbee2mqtt
        ({"data_path": "/share/zigbee2mqtt"}, lambda hass: Path("/share/zigbee2mqtt")),
        ({"data_path": "/addon_config"}, lambda hass: None),  # invisible from core
    ],
)
async def test_addon_data_path_mapping(
    hass: HomeAssistant, source: Zigbee2MQTTSource, options, expected
) -> None:
    from homeassistant.components.hassio import AddonState

    FakeMgr = _fake_addon_manager(
        {"45df7312_zigbee2mqtt": AddonState.NOT_RUNNING}, {"45df7312_zigbee2mqtt": options}
    )
    with (
        patch("homeassistant.helpers.hassio.is_hassio", return_value=True),
        patch("homeassistant.components.hassio.AddonManager", FakeMgr),
    ):
        mgr, data_dir = await source._addon()
    assert mgr.addon_slug == "45df7312_zigbee2mqtt"
    assert data_dir == expected(hass)


async def test_addon_none_when_lookup_fails(hass: HomeAssistant, source: Zigbee2MQTTSource) -> None:
    FakeMgr = _fake_addon_manager(
        {
            "45df7312_zigbee2mqtt": RuntimeError("supervisor down"),
            "45df7312_zigbee2mqtt_edge": RuntimeError("x"),
        }
    )
    with (
        patch("homeassistant.helpers.hassio.is_hassio", return_value=True),
        patch("homeassistant.components.hassio.AddonManager", FakeMgr),
    ):
        assert await source._addon() == (None, None)


# ----------------------------------------------------------- restore check


async def test_restore_check_ok_with_warnings(
    hass: HomeAssistant, source: Zigbee2MQTTSource, no_addon
) -> None:
    # live coordinator is a different stick (different IEEE), same software
    live = {
        **BRIDGE_INFO_SAMPLE,
        "coordinator": {**BRIDGE_INFO_SAMPLE["coordinator"], "ieee_address": "0x00124b00deadbeef"},
    }
    _publish_info(hass, live)
    check = await source.async_restore_check(make_z2m_zip(), {"stored": True})
    assert check.ok is True
    assert check.reasons == []
    assert check.backup == {"stored": True}
    assert check.current["ieee"] == "00124b00deadbeef"
    assert check.warnings == [
        "Could not locate the Z2M data directory; files will be staged for manual restore",
        "Zigbee2MQTT's device database is replaced by the one in the backup; devices paired after the backup are lost.",
        "Unplug the old coordinator for good. Two coordinators with the same IEEE on one mesh will break it.",
    ]


async def test_restore_check_no_ocbf_member_fails(
    hass: HomeAssistant, source: Zigbee2MQTTSource, fast_timeouts, no_addon
) -> None:
    check = await source.async_restore_check(make_z2m_zip(ocbf=None), {})
    assert check.ok is False
    assert check.reasons == [
        "Backup zip has no coordinator_backup.json; this adapter type cannot be restored"
    ]
    assert any(w.startswith("Could not read live coordinator identity:") for w in check.warnings)
    assert check.current == {}


async def test_restore_check_same_coordinator_and_network_refuses(
    hass: HomeAssistant, source: Zigbee2MQTTSource, no_addon
) -> None:
    _publish_info(hass)  # live == backup (0x00124b0024c2f0a1 / dddd...)
    check = await source.async_restore_check(make_z2m_zip(), {})
    assert check.ok is False
    assert any("already has this backup's IEEE and network" in r for r in check.reasons)


async def test_restore_check_same_ieee_different_network_is_ok(
    hass: HomeAssistant, source: Zigbee2MQTTSource, no_addon
) -> None:
    live = {
        **BRIDGE_INFO_SAMPLE,
        "network": {**BRIDGE_INFO_SAMPLE["network"], "extended_pan_id": "0x1111111111111111"},
    }
    _publish_info(hass, live)
    check = await source.async_restore_check(make_z2m_zip(), {})
    assert check.ok is True


async def test_restore_check_addon_and_config_warnings(
    hass: HomeAssistant, source: Zigbee2MQTTSource, fast_timeouts, tmp_path: Path
) -> None:
    data_dir = tmp_path / "zigbee2mqtt"
    data_dir.mkdir()
    live_cfg = {
        "serial": {"port": "/dev/ttyACM0", "adapter": "ember"},
        "advanced": {"pan_id": "GENERATE", "channel": 11},
    }
    (data_dir / "configuration.yaml").write_text(yaml.safe_dump(live_cfg))
    mgr = MagicMock(addon_slug="45df7312_zigbee2mqtt")

    with patch.object(source, "_addon", AsyncMock(return_value=(mgr, data_dir))):
        check = await source.async_restore_check(make_z2m_zip(), {})
    assert check.ok is True
    assert (
        f"Will stop add-on 45df7312_zigbee2mqtt, patch {data_dir}/configuration.yaml, then start it"
        in check.warnings
    )
    [cfg_warn] = [w for w in check.warnings if w.startswith("configuration.yaml advanced.*")]
    assert "advanced.pan_id is GENERATE" in cfg_warn
    assert "advanced.ext_pan_id differs" in cfg_warn
    assert "advanced.network_key differs" in cfg_warn
    assert "channel" not in cfg_warn
    assert any(
        "Live serial.adapter is 'ember' but the backup came from a 'zstack' adapter" in w
        for w in check.warnings
    )
    # No live identity -> no ember IEEE warning
    assert not any(w.startswith("ember adapters do not get their IEEE") for w in check.warnings)


async def test_restore_check_ember_ieee_warning(
    hass: HomeAssistant, source: Zigbee2MQTTSource, tmp_path: Path
) -> None:
    data_dir = tmp_path / "zigbee2mqtt"
    data_dir.mkdir()
    (data_dir / "configuration.yaml").write_text(
        yaml.safe_dump({"serial": {"adapter": "ember"}, "advanced": advanced_from_ocbf(OCBF_SAMPLE)})
    )
    live = {
        **BRIDGE_INFO_SAMPLE,
        "coordinator": {**BRIDGE_INFO_SAMPLE["coordinator"], "ieee_address": "0x00124b00deadbeef"},
    }
    _publish_info(hass, live)
    with (
        patch.object(source, "_addon", AsyncMock(return_value=(None, None))),
        patch.object(source, "_fallback_data_dir", return_value=data_dir),
    ):
        check = await source.async_restore_check(make_z2m_zip(), {})
    assert check.ok is True
    assert any(f"Copy the IEEE {IEEE} onto the new stick" in w for w in check.warnings)
    assert any("not a Supervisor add-on; you must stop/start Z2M yourself" in w for w in check.warnings)
    assert not any(w.startswith("configuration.yaml advanced.*") for w in check.warnings)


# ----------------------------------------------------------------- restore


async def test_restore_without_ocbf_raises(hass: HomeAssistant, source: Zigbee2MQTTSource, no_addon) -> None:
    with pytest.raises(RuntimeError, match="no coordinator_backup.json"):
        await source.async_restore(make_z2m_zip(ocbf=None), {})


async def test_restore_without_addon_stages_files_and_raises(
    hass: HomeAssistant, source: Zigbee2MQTTSource, no_addon
) -> None:
    with pytest.raises(RestoreStaged, match="not a Supervisor add-on here") as ei:
        await source.async_restore(make_z2m_zip(), {})
    staging = Path(hass.config.path("radio_guard", "zigbee2mqtt", "restore"))
    assert str(staging) in str(ei.value)
    assert "its data directory looks like" not in str(ei.value)
    assert json.loads((staging / "coordinator_backup.json").read_text()) == OCBF_SAMPLE
    assert (staging / "database.db").exists()
    cfg = yaml.safe_load((staging / "configuration.yaml").read_text())
    assert cfg["advanced"]["pan_id"] == 0x1A62


async def test_restore_with_data_dir_but_no_addon_stages_and_mentions_dir(
    hass: HomeAssistant, source: Zigbee2MQTTSource, tmp_path: Path
) -> None:
    data_dir = tmp_path / "z2m"
    data_dir.mkdir()
    (data_dir / "configuration.yaml").write_text("serial: {}\n")
    with (
        patch.object(source, "_addon", AsyncMock(return_value=(None, None))),
        patch.object(source, "_fallback_data_dir", return_value=data_dir),
        pytest.raises(RestoreStaged, match=f"its data directory looks like {data_dir}"),
    ):
        await source.async_restore(make_z2m_zip(), {})
    # The live dir was NOT touched since Z2M could not be stopped
    assert not (data_dir / "coordinator_backup.json").exists()
    assert yaml.safe_load((data_dir / "configuration.yaml").read_text()) == {"serial": {}}


async def _mgr_with_data_dir(tmp_path: Path, live_adv: dict | None = None):
    data_dir = tmp_path / "z2m"
    data_dir.mkdir()
    (data_dir / "configuration.yaml").write_text(
        yaml.safe_dump({"serial": {"port": "/dev/ttyACM1"}, "advanced": live_adv or {"pan_id": "GENERATE"}})
    )
    mgr = MagicMock(addon_slug="45df7312_zigbee2mqtt")
    mgr.async_stop_addon = AsyncMock()
    mgr.async_start_addon = AsyncMock()
    return mgr, data_dir


async def test_restore_with_addon_full_flow(
    hass: HomeAssistant, source: Zigbee2MQTTSource, tmp_path: Path
) -> None:
    mgr, data_dir = await _mgr_with_data_dir(tmp_path)
    order: list[str] = []

    async def stop():
        order.append("stop")
        assert not (data_dir / "coordinator_backup.json").exists()

    async def start():
        order.append("start")
        assert (data_dir / "coordinator_backup.json").exists()
        # Z2M comes back online with the restored network
        _publish_state(hass, "online")
        _publish_info(hass)

    mgr.async_stop_addon = stop
    mgr.async_start_addon = start
    _publish_state(hass, "online")
    _publish_info(hass)
    await hass.async_block_till_done()

    with patch.object(source, "_addon", AsyncMock(return_value=(mgr, data_dir))):
        await source.async_restore(make_z2m_zip(), {})

    assert order == ["stop", "start"]
    assert json.loads((data_dir / "coordinator_backup.json").read_text()) == OCBF_SAMPLE
    cfg = yaml.safe_load((data_dir / "configuration.yaml").read_text())
    assert cfg["serial"]["port"] == "/dev/ttyACM1"
    assert cfg["advanced"]["pan_id"] == 0x1A62
    assert (data_dir / "configuration.yaml.radio_guard.bak").exists()


async def test_restore_with_addon_restarts_even_if_write_fails(
    hass: HomeAssistant, source: Zigbee2MQTTSource, tmp_path: Path
) -> None:
    mgr, data_dir = await _mgr_with_data_dir(tmp_path)
    with (
        patch.object(source, "_addon", AsyncMock(return_value=(mgr, data_dir))),
        patch.object(source, "_write_restore_files", side_effect=OSError("ro fs")),
        pytest.raises(OSError),
    ):
        await source.async_restore(make_z2m_zip(), {})
    mgr.async_stop_addon.assert_awaited_once()
    mgr.async_start_addon.assert_awaited_once()


async def test_restore_verify_fails_when_z2m_stays_offline(
    hass: HomeAssistant, source: Zigbee2MQTTSource, tmp_path: Path, fast_timeouts
) -> None:
    mgr, data_dir = await _mgr_with_data_dir(tmp_path)
    with (
        patch.object(source, "_addon", AsyncMock(return_value=(mgr, data_dir))),
        pytest.raises(RuntimeError, match="did not report online within 2 minutes"),
    ):
        await source.async_restore(make_z2m_zip(), {})
    mgr.async_start_addon.assert_awaited_once()


async def test_restore_verify_detects_new_network(
    hass: HomeAssistant, source: Zigbee2MQTTSource, tmp_path: Path
) -> None:
    mgr, data_dir = await _mgr_with_data_dir(tmp_path)
    reset_info = {
        **BRIDGE_INFO_SAMPLE,
        "network": {**BRIDGE_INFO_SAMPLE["network"], "extended_pan_id": "0x0102030405060708"},
    }

    async def start():
        _publish_state(hass, "online")
        _publish_info(hass, reset_info)

    mgr.async_start_addon = start
    with (
        patch.object(source, "_addon", AsyncMock(return_value=(mgr, data_dir))),
        pytest.raises(
            RuntimeError,
            match="started with a different network \\(ext PAN 0102030405060708 vs backup dddddddddddddddd\\)",
        ),
    ):
        await source.async_restore(make_z2m_zip(), {})


async def test_restore_verify_skips_when_no_info_after_online(
    hass: HomeAssistant, source: Zigbee2MQTTSource, tmp_path: Path, caplog
) -> None:
    mgr, data_dir = await _mgr_with_data_dir(tmp_path)

    async def start():
        _publish_state(hass, "online")

    mgr.async_start_addon = start
    real = asyncio.wait_for

    async def fast(fut, timeout):
        return await real(fut, 0.02 if timeout == 30 else timeout)

    with (
        patch.object(source, "_addon", AsyncMock(return_value=(mgr, data_dir))),
        patch("custom_components.radio_guard.backup.z2m.asyncio.wait_for", fast),
    ):
        await source.async_restore(make_z2m_zip(), {})
    assert "skipping post-restore verification" in caplog.text


async def test_restore_clears_stale_online_state_before_restart(
    hass: HomeAssistant, source: Zigbee2MQTTSource, tmp_path: Path, fast_timeouts
) -> None:
    """A retained 'online' from before the stop must not satisfy the post-restart wait."""
    _publish_state(hass, "online")
    _publish_info(hass)
    await hass.async_block_till_done()
    mgr, data_dir = await _mgr_with_data_dir(tmp_path)
    with (
        patch.object(source, "_addon", AsyncMock(return_value=(mgr, data_dir))),
        pytest.raises(RuntimeError, match="did not report online"),
    ):
        await source.async_restore(make_z2m_zip(), {})
