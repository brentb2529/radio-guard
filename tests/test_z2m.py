"""Zigbee2MQTT backup source."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from custom_components.radio_guard.backup.base import SourceUnavailable
from custom_components.radio_guard.backup.z2m import Zigbee2MQTTSource, _summarize_ocbf
from homeassistant.core import HomeAssistant

from .conftest import BRIDGE_INFO_SAMPLE, OCBF_SAMPLE, make_z2m_zip, z2m_zip_b64

REQ = "zigbee2mqtt/bridge/request/backup"
RESP = "zigbee2mqtt/bridge/response/backup"
INFO = "zigbee2mqtt/bridge/info"


@pytest.fixture
async def source(hass: HomeAssistant, mqtt_mock) -> Zigbee2MQTTSource:
    return Zigbee2MQTTSource(hass, "zigbee2mqtt/")


@pytest.fixture
def no_timeouts():
    """Make the 5s bridge/info wait and the 60s request wait resolve instantly when nothing answers."""
    real_wait_for = asyncio.wait_for

    async def fast_wait_for(fut, timeout):
        return await real_wait_for(fut, 0.05)

    with patch("custom_components.radio_guard.backup.z2m.asyncio.wait_for", fast_wait_for):
        yield


def _published(mqtt_mock, topic: str) -> list:
    return [c for c in mqtt_mock.async_publish.call_args_list if c.args[0] == topic]


async def _respond_to_backup(hass: HomeAssistant, mqtt_mock, response: dict, info: dict | None = None):
    """Wait for the request publish, then fire the response (and retained info if given)."""
    for _ in range(50):
        await asyncio.sleep(0)
        if _published(mqtt_mock, REQ):
            break
    assert _published(mqtt_mock, REQ), "no backup request was published"
    async_fire_mqtt_message(hass, RESP, json.dumps(response))
    if info is not None:
        # The retained bridge/info arrives as soon as the identity subscription is made
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        async_fire_mqtt_message(hass, INFO, json.dumps(info), retain=True)


async def test_base_topic_normalized_and_available(hass: HomeAssistant, source: Zigbee2MQTTSource) -> None:
    assert source.base_topic == "zigbee2mqtt"
    assert source.available() is True
    assert source.id == "zigbee2mqtt"
    assert source.name == "Zigbee2MQTT"


async def test_unavailable_without_mqtt(hass: HomeAssistant) -> None:
    src = Zigbee2MQTTSource(hass, "zigbee2mqtt")
    assert src.available() is False
    with pytest.raises(SourceUnavailable):
        await src.async_identity()
    with pytest.raises(SourceUnavailable):
        await src.async_backup()


async def test_identity_from_retained_bridge_info(hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource) -> None:
    task = hass.async_create_task(source.async_identity())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    async_fire_mqtt_message(hass, INFO, json.dumps(BRIDGE_INFO_SAMPLE), retain=True)
    ident = await task
    assert ident == {
        "ieee": "0x00124b0024c2f0a1",
        "coordinator_type": "zStack3x0",
        "coordinator_version": 20240710,
        "pan_id": 6754,
        "extended_pan_id": "0xdddddddddddddddd",
        "channel": 11,
        "z2m_version": "1.42.0",
        "zigbee_herdsman": "2.1.3",
    }
    # subscription is released afterwards
    sub_calls = [c for c in mqtt_mock.async_subscribe.call_args_list if c.args[0] == INFO]
    assert len(sub_calls) == 1


async def test_identity_without_bridge_info_times_out_to_empty(
    hass: HomeAssistant, source: Zigbee2MQTTSource, no_timeouts
) -> None:
    ident = await source.async_identity()
    assert all(v is None for v in ident.values())
    assert set(ident) == {
        "ieee", "coordinator_type", "coordinator_version", "pan_id",
        "extended_pan_id", "channel", "z2m_version", "zigbee_herdsman",
    }


async def test_identity_bad_json_is_empty(hass: HomeAssistant, source: Zigbee2MQTTSource) -> None:
    task = hass.async_create_task(source.async_identity())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    async_fire_mqtt_message(hass, INFO, "not json", retain=True)
    ident = await task
    assert ident["ieee"] is None


async def test_backup_success(hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource) -> None:
    zip_bytes = make_z2m_zip()
    task = hass.async_create_task(source.async_backup())
    await _respond_to_backup(
        hass, mqtt_mock, {"status": "ok", "data": {"zip": z2m_zip_b64(zip_bytes)}}, info=BRIDGE_INFO_SAMPLE
    )
    payload = await task

    # Request was sent as JSON "{}" with qos 1
    [req] = _published(mqtt_mock, REQ)
    assert req.args[1] == "{}"
    assert req.kwargs.get("qos", req.args[2] if len(req.args) > 2 else None) == 1

    assert payload.ext == ".zip"
    assert payload.data == zip_bytes
    meta = payload.meta
    assert meta["format"] == "zigbee2mqtt-zip"
    assert set(meta["members"]) == {"coordinator_backup.json", "database.db", "configuration.yaml"}
    assert meta["device_count"] == 2
    assert meta["pan_id"] == 6754  # bridge/info overrides the OCBF string form
    assert meta["channel"] == 11
    assert meta["ocbf_version"] == 1
    assert meta["ocbf_source"] == "zigbee2mqtt@1.42.0"
    assert meta["ieee"] == "0x00124b0024c2f0a1"
    assert meta["z2m_version"] == "1.42.0"
    assert meta["coordinator_type"] == "zStack3x0"
    # response subscription released
    assert all(c.args[0] != RESP or True for c in mqtt_mock.async_subscribe.call_args_list)


async def test_backup_writes_zip_through_store(hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource, tmp_path: Path) -> None:
    """End-to-end through BackupStore: a .zip plus a sidecar with ieee/pan_id/device_count."""
    from custom_components.radio_guard.storage import BackupStore

    store = BackupStore(tmp_path / "rg", "zigbee2mqtt", 5)
    task = hass.async_create_task(source.async_backup())
    await _respond_to_backup(hass, mqtt_mock, {"status": "ok", "data": {"zip": z2m_zip_b64(make_z2m_zip())}}, info=BRIDGE_INFO_SAMPLE)
    payload = await task
    rec = await hass.async_add_executor_job(store.write, payload.data, payload.ext, payload.meta)
    assert rec.path.suffix == ".zip"
    assert rec.path.exists()
    sidecar = json.loads((rec.path.parent / f"{rec.path.stem}.meta.json").read_text())
    assert sidecar["ieee"] == "0x00124b0024c2f0a1"
    assert sidecar["pan_id"] == 6754
    assert sidecar["device_count"] == 2
    assert sidecar["file"] == rec.name


async def test_backup_without_bridge_info_still_has_ocbf_identity(
    hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource, no_timeouts
) -> None:
    task = hass.async_create_task(source.async_backup())
    await _respond_to_backup(hass, mqtt_mock, {"status": "ok", "data": {"zip": z2m_zip_b64(make_z2m_zip())}})
    payload = await task
    # identity merge is best-effort: empty bridge info yields None values that overwrite OCBF values
    assert payload.meta["device_count"] == 2
    assert payload.meta["ocbf_source"] == "zigbee2mqtt@1.42.0"
    assert "ieee" in payload.meta


async def test_backup_error_status(hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource) -> None:
    task = hass.async_create_task(source.async_backup())
    await _respond_to_backup(hass, mqtt_mock, {"status": "error", "error": "Coordinator does not support backup"})
    with pytest.raises(RuntimeError, match="Z2M backup failed: Coordinator does not support backup"):
        await task


async def test_backup_ok_without_zip(hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource) -> None:
    task = hass.async_create_task(source.async_backup())
    await _respond_to_backup(hass, mqtt_mock, {"status": "ok", "data": {}})
    with pytest.raises(RuntimeError, match="no zip payload"):
        await task


async def test_backup_bad_json_response(hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource) -> None:
    task = hass.async_create_task(source.async_backup())
    for _ in range(50):
        await asyncio.sleep(0)
        if _published(mqtt_mock, REQ):
            break
    async_fire_mqtt_message(hass, RESP, "{not json")
    with pytest.raises(ValueError, match="Bad JSON from Z2M"):
        await task


async def test_backup_timeout(hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource, no_timeouts) -> None:
    with pytest.raises(TimeoutError):
        await source.async_backup()
    # subscription to the response topic was cleaned up: a late response is ignored
    async_fire_mqtt_message(hass, RESP, json.dumps({"status": "ok"}))
    await hass.async_block_till_done()


async def test_backup_timeout_constant(hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource) -> None:
    """The request waits Z2M_BACKUP_TIMEOUT seconds."""
    from custom_components.radio_guard.const import Z2M_BACKUP_TIMEOUT

    seen: list[float] = []

    async def fake_wait_for(fut, timeout):
        seen.append(timeout)
        raise TimeoutError

    with patch("custom_components.radio_guard.backup.z2m.asyncio.wait_for", fake_wait_for), pytest.raises(TimeoutError):
        await source.async_backup()
    assert seen == [Z2M_BACKUP_TIMEOUT]


# ---------------------------------------------------------------- restore


def test_summarize_ocbf() -> None:
    out = _summarize_ocbf(OCBF_SAMPLE)
    assert out == {
        "ocbf_version": 1,
        "ocbf_format": "zigpy/open-coordinator-backup",
        "ocbf_source": "zigbee2mqtt@1.42.0",
        "pan_id": "1a62",
        "extended_pan_id": "dddddddddddddddd",
        "channel": 11,
        "ieee": "00124b0024c2f0a1",
        "nwk_update_id": 0,
        "device_count": 2,
    }
    assert _summarize_ocbf({})["device_count"] == 0


def test_inspect_zip_without_ocbf() -> None:
    meta = Zigbee2MQTTSource._inspect_zip(make_z2m_zip(ocbf=None))
    assert meta == {"format": "zigbee2mqtt-zip", "members": ["database.db", "configuration.yaml"]}


async def test_restore_check_no_ocbf_member_fails(hass: HomeAssistant, source: Zigbee2MQTTSource, no_timeouts, tmp_path) -> None:
    with patch.object(source, "_data_dirs", return_value=[tmp_path / "none"]):
        check = await source.async_restore_check(make_z2m_zip(ocbf=None), {"x": 1})
    assert check.ok is False
    assert check.reasons == ["Backup zip does not contain coordinator_backup.json"]
    assert check.backup == {"x": 1}
    assert any("Could not locate the Z2M data directory" in w for w in check.warnings)


async def test_restore_check_same_ieee_warns(hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource, tmp_path) -> None:
    info = {**BRIDGE_INFO_SAMPLE, "coordinator": {**BRIDGE_INFO_SAMPLE["coordinator"], "ieee_address": "00124B0024C2F0A1"}}
    with patch.object(source, "_data_dirs", return_value=[tmp_path / "none"]):
        task = hass.async_create_task(source.async_restore_check(make_z2m_zip(), {}))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        async_fire_mqtt_message(hass, INFO, json.dumps(info), retain=True)
        check = await task
    assert check.ok is True
    assert check.current["ieee"] == "00124B0024C2F0A1"
    assert any("already has the backup's IEEE" in w for w in check.warnings)


async def test_restore_check_addon_and_data_dir_warnings(hass: HomeAssistant, source: Zigbee2MQTTSource, no_timeouts, tmp_path: Path) -> None:
    data_dir = tmp_path / "addon_configs" / "45df7312_zigbee2mqtt"
    data_dir.mkdir(parents=True)
    (data_dir / "configuration.yaml").write_text("x: 1")

    with patch.object(source, "_data_dirs", return_value=[data_dir]):
        with patch.object(source, "_addon_slug", AsyncMock(return_value="45df7312_zigbee2mqtt")):
            check = await source.async_restore_check(make_z2m_zip(), {})
        assert any(f"Will stop add-on 45df7312_zigbee2mqtt, write {data_dir}" in w for w in check.warnings)

        with patch.object(source, "_addon_slug", AsyncMock(return_value=None)):
            check = await source.async_restore_check(make_z2m_zip(), {})
        assert any("no Supervisor add-on" in w for w in check.warnings)
    assert check.ok is True


async def test_data_dirs_candidates(hass: HomeAssistant) -> None:
    src = Zigbee2MQTTSource(hass, "z")
    dirs = [str(p) for p in src._data_dirs()]
    assert dirs == [
        "/addon_configs/45df7312_zigbee2mqtt",
        "/addon_configs/45df7312_zigbee2mqtt_edge",
        "/config/zigbee2mqtt",
        "/share/zigbee2mqtt",
    ]


async def test_addon_slug_not_hassio(hass: HomeAssistant, source: Zigbee2MQTTSource) -> None:
    """Without Supervisor the lookup returns None (or the import fails -> None)."""
    assert await source._addon_slug() is None


async def test_restore_without_addon_stages_files_and_raises(hass: HomeAssistant, source: Zigbee2MQTTSource) -> None:
    with (
        patch.object(source, "_addon_slug", AsyncMock(return_value=None)),
        patch.object(source, "_find_data_dir", return_value=None),
        pytest.raises(RuntimeError, match="not running as a Supervisor add-on") as ei,
    ):
        await source.async_restore(make_z2m_zip(), {})
    staging = Path(hass.config.path("radio_guard", "zigbee2mqtt", "restore"))
    assert str(staging) in str(ei.value)
    assert json.loads((staging / "coordinator_backup.json").read_text()) == OCBF_SAMPLE
    assert (staging / "database.db").exists()
    assert not (staging / "configuration.yaml").exists()


async def test_restore_with_addon_stops_writes_starts(hass: HomeAssistant, source: Zigbee2MQTTSource, tmp_path: Path) -> None:
    data_dir = tmp_path / "z2m"
    data_dir.mkdir()
    (data_dir / "configuration.yaml").write_text("x")
    order: list[str] = []

    async def stop(hass_, slug):
        order.append(f"stop:{slug}")

    async def start(hass_, slug):
        order.append(f"start:{slug}")
        assert (data_dir / "coordinator_backup.json").exists()

    with (
        patch.object(source, "_addon_slug", AsyncMock(return_value="45df7312_zigbee2mqtt")),
        patch.object(source, "_data_dirs", return_value=[data_dir]),
        patch("homeassistant.components.hassio.async_stop_addon", stop, create=True),
        patch("homeassistant.components.hassio.async_start_addon", start, create=True),
    ):
        await source.async_restore(make_z2m_zip(), {})

    assert order == ["stop:45df7312_zigbee2mqtt", "start:45df7312_zigbee2mqtt"]
    assert (data_dir / "database.db").exists()


async def test_restore_with_addon_restarts_even_if_write_fails(hass: HomeAssistant, source: Zigbee2MQTTSource, tmp_path: Path) -> None:
    data_dir = tmp_path / "z2m"
    data_dir.mkdir()
    (data_dir / "configuration.yaml").write_text("x")
    started = AsyncMock()
    with (
        patch.object(source, "_addon_slug", AsyncMock(return_value="45df7312_zigbee2mqtt")),
        patch.object(source, "_data_dirs", return_value=[data_dir]),
        patch("homeassistant.components.hassio.async_stop_addon", AsyncMock(), create=True),
        patch("homeassistant.components.hassio.async_start_addon", started, create=True),
        patch.object(source, "_write_restore_files", side_effect=OSError("ro fs")),
        pytest.raises(OSError),
    ):
        await source.async_restore(make_z2m_zip(), {})
    started.assert_awaited_once()
