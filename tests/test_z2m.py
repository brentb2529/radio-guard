"""Zigbee2MQTT backup source: persistent subscriptions + transaction correlation."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from custom_components.radio_guard.backup.base import SourceUnavailable
from custom_components.radio_guard.backup.z2m import Zigbee2MQTTSource, _summarize_ocbf
from custom_components.radio_guard.const import Z2M_BACKUP_TIMEOUT
from custom_components.radio_guard.storage import BackupStore
from homeassistant.core import HomeAssistant

from .conftest import BRIDGE_INFO_SAMPLE, OCBF_SAMPLE, make_z2m_zip, z2m_zip_b64

REQ = "zigbee2mqtt/bridge/request/backup"
RESP = "zigbee2mqtt/bridge/response/backup"
INFO = "zigbee2mqtt/bridge/info"


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


def _published(mqtt_mock, topic: str) -> list:
    return [c for c in mqtt_mock.async_publish.call_args_list if c.args[0] == topic]


def _subscribed_topics(mqtt_mock) -> list[str]:
    return [c.args[0] for c in mqtt_mock.async_subscribe.call_args_list]


def _publish_info(hass: HomeAssistant, info: dict = BRIDGE_INFO_SAMPLE) -> None:
    async_fire_mqtt_message(hass, INFO, json.dumps(info), retain=True)


async def _wait_for_request(mqtt_mock) -> dict:
    """Wait until the backup request is published and return its parsed payload."""
    for _ in range(100):
        await asyncio.sleep(0)
        if calls := _published(mqtt_mock, REQ):
            return json.loads(calls[-1].args[1])
    raise AssertionError("no backup request was published")


# ---------------------------------------------------------------- lifecycle


async def test_basic_attributes(hass: HomeAssistant, source: Zigbee2MQTTSource) -> None:
    assert source.base_topic == "zigbee2mqtt"
    assert source.id == "zigbee2mqtt"
    assert source.name == "Zigbee2MQTT"
    assert source.available() is True


async def test_setup_subscribes_once_and_teardown_releases(hass: HomeAssistant, mqtt_mock) -> None:
    src = Zigbee2MQTTSource(hass, "z2m")
    await src.async_setup()
    assert _subscribed_topics(mqtt_mock) == ["z2m/bridge/info", "z2m/bridge/response/backup"]
    assert len(src._unsubs) == 2
    # idempotent
    await src.async_setup()
    assert len(_subscribed_topics(mqtt_mock)) == 2

    await src.async_teardown()
    assert src._unsubs == []
    # a message arriving after teardown is ignored
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


async def test_setup_noop_without_mqtt(hass: HomeAssistant) -> None:
    src = Zigbee2MQTTSource(hass, "zigbee2mqtt")
    assert src.available() is False
    await src.async_setup()  # does not raise, does not subscribe
    assert src._unsubs == []
    with pytest.raises(SourceUnavailable, match="MQTT integration is not loaded"):
        await src.async_identity()
    with pytest.raises(SourceUnavailable, match="MQTT integration is not loaded"):
        await src.async_backup()
    await src.async_teardown()


async def test_teardown_cancels_pending_requests(hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource) -> None:
    task = hass.async_create_task(source.async_backup())
    await _wait_for_request(mqtt_mock)
    assert len(source._pending) == 1
    await source.async_teardown()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert source._pending == {}


# ----------------------------------------------------------------- identity


async def test_identity_from_retained_bridge_info(hass: HomeAssistant, source: Zigbee2MQTTSource) -> None:
    _publish_info(hass)
    await hass.async_block_till_done()
    ident = await source.async_identity()
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


async def test_identity_waits_for_late_bridge_info(hass: HomeAssistant, source: Zigbee2MQTTSource) -> None:
    task = hass.async_create_task(source.async_identity())
    await asyncio.sleep(0)
    assert not task.done()
    _publish_info(hass)
    ident = await task
    assert ident["ieee"] == "0x00124b0024c2f0a1"


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


async def test_identity_ignores_bad_json_info(hass: HomeAssistant, source: Zigbee2MQTTSource, fast_timeouts) -> None:
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


async def test_backup_success_with_transaction(hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource) -> None:
    _publish_info(hass)
    zip_bytes = make_z2m_zip()
    task = hass.async_create_task(source.async_backup())
    req = await _wait_for_request(mqtt_mock)

    assert req == {"transaction": "rg-1"}
    [call] = _published(mqtt_mock, REQ)
    assert call.kwargs.get("qos", call.args[2] if len(call.args) > 2 else None) == 1

    async_fire_mqtt_message(
        hass, RESP, json.dumps({"status": "ok", "transaction": "rg-1", "data": {"zip": z2m_zip_b64(zip_bytes)}})
    )
    payload = await task

    assert payload.ext == ".zip"
    assert payload.data == zip_bytes
    meta = payload.meta
    assert meta["format"] == "zigbee2mqtt-zip"
    assert set(meta["members"]) == {"coordinator_backup.json", "database.db", "configuration.yaml"}
    assert meta["device_count"] == 2
    assert meta["ocbf_version"] == 1
    assert meta["ocbf_source"] == "zigbee2mqtt@1.42.0"
    # bridge/info identity overrides the OCBF string forms
    assert meta["ieee"] == "0x00124b0024c2f0a1"
    assert meta["pan_id"] == 6754
    assert meta["channel"] == 11
    assert meta["z2m_version"] == "1.42.0"
    assert meta["coordinator_type"] == "zStack3x0"
    assert source._pending == {}


async def test_backup_writes_zip_and_sidecar_through_store(
    hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource, tmp_path: Path
) -> None:
    _publish_info(hass)
    store = BackupStore(tmp_path / "rg", "zigbee2mqtt", 5)
    task = hass.async_create_task(source.async_backup())
    req = await _wait_for_request(mqtt_mock)
    async_fire_mqtt_message(
        hass, RESP, json.dumps({"status": "ok", "transaction": req["transaction"], "data": {"zip": z2m_zip_b64(make_z2m_zip())}})
    )
    payload = await task
    rec = await hass.async_add_executor_job(store.write, payload.data, payload.ext, payload.meta)
    assert rec.path.suffix == ".zip"
    assert rec.path.exists()
    sidecar = json.loads((rec.path.parent / f"{rec.path.stem}.meta.json").read_text())
    assert sidecar["ieee"] == "0x00124b0024c2f0a1"
    assert sidecar["pan_id"] == 6754
    assert sidecar["device_count"] == 2
    assert sidecar["source"] == "zigbee2mqtt"
    assert sidecar["file"] == rec.name


async def test_backup_ignores_response_for_other_transaction(
    hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource
) -> None:
    _publish_info(hass)
    t1 = hass.async_create_task(source.async_backup())
    await _wait_for_request(mqtt_mock)
    t2 = hass.async_create_task(source.async_backup())
    for _ in range(50):
        await asyncio.sleep(0)
        if len(_published(mqtt_mock, REQ)) == 2:
            break
    assert set(source._pending) == {"rg-1", "rg-2"}

    # Two waiters: a response with an unknown transaction is dropped, not misdelivered
    async_fire_mqtt_message(hass, RESP, json.dumps({"status": "error", "transaction": "someone-else", "error": "x"}))
    await asyncio.sleep(0)
    assert not t1.done() and not t2.done()

    # Each response goes to its own waiter, out of order
    async_fire_mqtt_message(hass, RESP, json.dumps({"status": "ok", "transaction": "rg-2", "data": {"zip": z2m_zip_b64(make_z2m_zip())}}))
    async_fire_mqtt_message(hass, RESP, json.dumps({"status": "error", "transaction": "rg-1", "error": "busy"}))
    p2 = await t2
    assert p2.ext == ".zip"
    with pytest.raises(RuntimeError, match="busy"):
        await t1


async def test_backup_legacy_response_without_transaction(
    hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource
) -> None:
    """Older Z2M that does not echo the transaction still satisfies a lone waiter."""
    _publish_info(hass)
    task = hass.async_create_task(source.async_backup())
    await _wait_for_request(mqtt_mock)
    async_fire_mqtt_message(hass, RESP, json.dumps({"status": "ok", "data": {"zip": z2m_zip_b64(make_z2m_zip())}}))
    payload = await task
    assert payload.ext == ".zip"


async def test_backup_transaction_ids_increment(hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource) -> None:
    _publish_info(hass)
    for n in (1, 2, 3):
        task = hass.async_create_task(source.async_backup())
        req = await _wait_for_request(mqtt_mock)
        assert req["transaction"] == f"rg-{n}"
        async_fire_mqtt_message(hass, RESP, json.dumps({"status": "ok", "transaction": f"rg-{n}", "data": {"zip": z2m_zip_b64(make_z2m_zip())}}))
        await task


async def test_backup_identity_failure_is_best_effort(
    hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource, fast_timeouts
) -> None:
    """No bridge/info: meta still carries the OCBF identity."""
    task = hass.async_create_task(source.async_backup())
    await _wait_for_request(mqtt_mock)
    async_fire_mqtt_message(hass, RESP, json.dumps({"status": "ok", "transaction": "rg-1", "data": {"zip": z2m_zip_b64(make_z2m_zip())}}))
    payload = await task
    assert payload.meta["ieee"] == "00124b0024c2f0a1"
    assert payload.meta["pan_id"] == "1a62"
    assert payload.meta["device_count"] == 2
    assert "z2m_version" not in payload.meta


async def test_backup_error_status(hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource) -> None:
    task = hass.async_create_task(source.async_backup())
    await _wait_for_request(mqtt_mock)
    async_fire_mqtt_message(hass, RESP, json.dumps({"status": "error", "transaction": "rg-1", "error": "Coordinator does not support backup"}))
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
    with pytest.raises(TimeoutError, match="No response from Zigbee2MQTT on zigbee2mqtt/bridge/response/backup") as ei:
        await source.async_backup()
    assert "base topic 'zigbee2mqtt'" in str(ei.value)
    assert source._pending == {}
    # a late reply for the expired transaction is harmless
    async_fire_mqtt_message(hass, RESP, json.dumps({"status": "ok", "transaction": "rg-1"}))
    await hass.async_block_till_done()


async def test_backup_uses_configured_timeout(hass: HomeAssistant, mqtt_mock, source: Zigbee2MQTTSource) -> None:
    seen: list[float] = []

    async def fake_wait_for(fut, timeout):
        seen.append(timeout)
        raise TimeoutError

    with patch("custom_components.radio_guard.backup.z2m.asyncio.wait_for", fake_wait_for), pytest.raises(TimeoutError):
        await source.async_backup()
    assert seen == [Z2M_BACKUP_TIMEOUT]


async def test_request_lazily_sets_up(hass: HomeAssistant, mqtt_mock) -> None:
    """A backup on a source that was never set up subscribes first."""
    src = Zigbee2MQTTSource(hass, "zigbee2mqtt")
    assert src._unsubs == []
    task = hass.async_create_task(src.async_backup())
    await _wait_for_request(mqtt_mock)
    assert len(src._unsubs) == 2
    async_fire_mqtt_message(hass, RESP, json.dumps({"status": "ok", "transaction": "rg-1", "data": {"zip": z2m_zip_b64(make_z2m_zip())}}))
    await task
    await src.async_teardown()


# ---------------------------------------------------------- pure helpers


def test_summarize_ocbf() -> None:
    assert _summarize_ocbf(OCBF_SAMPLE) == {
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
    assert _summarize_ocbf({"devices": None})["device_count"] == 0


def test_inspect_zip() -> None:
    meta = Zigbee2MQTTSource._inspect_zip(make_z2m_zip())
    assert meta["format"] == "zigbee2mqtt-zip"
    assert meta["ieee"] == "00124b0024c2f0a1"
    assert Zigbee2MQTTSource._inspect_zip(make_z2m_zip(ocbf=None)) == {
        "format": "zigbee2mqtt-zip",
        "members": ["database.db", "configuration.yaml"],
    }


def test_inspect_zip_rejects_garbage() -> None:
    import zipfile

    with pytest.raises(zipfile.BadZipFile):
        Zigbee2MQTTSource._inspect_zip(b"not a zip")


def test_data_dirs_candidates(hass: HomeAssistant) -> None:
    dirs = [str(p) for p in Zigbee2MQTTSource(hass, "z")._data_dirs()]
    assert dirs == [
        "/addon_configs/45df7312_zigbee2mqtt",
        "/addon_configs/45df7312_zigbee2mqtt_edge",
        "/config/zigbee2mqtt",
        "/share/zigbee2mqtt",
    ]


def test_find_data_dir(hass: HomeAssistant, tmp_path: Path) -> None:
    src = Zigbee2MQTTSource(hass, "z")
    good = tmp_path / "good"
    good.mkdir()
    (good / "configuration.yaml").write_text("x")
    with patch.object(src, "_data_dirs", return_value=[tmp_path / "missing", good]):
        assert src._find_data_dir() == good
    with patch.object(src, "_data_dirs", return_value=[tmp_path / "missing"]):
        assert src._find_data_dir() is None


def test_write_restore_files(tmp_path: Path) -> None:
    dest = tmp_path / "out" / "nested"
    Zigbee2MQTTSource._write_restore_files(make_z2m_zip(), dest)
    assert json.loads((dest / "coordinator_backup.json").read_text()) == OCBF_SAMPLE
    assert (dest / "database.db").exists()
    assert not (dest / "configuration.yaml").exists()


# ------------------------------------------------------------ add-on mgmt


async def test_addon_manager_none_without_supervisor(hass: HomeAssistant, source: Zigbee2MQTTSource) -> None:
    assert await source._addon_manager() is None


async def test_addon_manager_picks_installed_slug(hass: HomeAssistant, source: Zigbee2MQTTSource) -> None:
    from homeassistant.components.hassio.addon_manager import AddonState

    infos = {
        "45df7312_zigbee2mqtt": AddonState.NOT_INSTALLED,
        "45df7312_zigbee2mqtt_edge": AddonState.RUNNING,
    }

    class FakeMgr:
        def __init__(self, hass_, logger, name, slug):
            self.addon_slug = slug

        async def async_get_addon_info(self):
            return MagicMock(state=infos[self.addon_slug])

    with (
        patch("homeassistant.helpers.hassio.is_hassio", return_value=True),
        patch("homeassistant.components.hassio.addon_manager.AddonManager", FakeMgr),
    ):
        mgr = await source._addon_manager()
    assert mgr is not None
    assert mgr.addon_slug == "45df7312_zigbee2mqtt_edge"


async def test_addon_manager_none_when_lookup_fails(hass: HomeAssistant, source: Zigbee2MQTTSource) -> None:
    class FailingMgr:
        def __init__(self, hass_, logger, name, slug):
            self.addon_slug = slug

        async def async_get_addon_info(self):
            raise RuntimeError("supervisor down")

    with (
        patch("homeassistant.helpers.hassio.is_hassio", return_value=True),
        patch("homeassistant.components.hassio.addon_manager.AddonManager", FailingMgr),
    ):
        assert await source._addon_manager() is None


# ----------------------------------------------------------------- restore


async def test_restore_check_ok_with_warnings(hass: HomeAssistant, source: Zigbee2MQTTSource, tmp_path: Path) -> None:
    _publish_info(hass)
    with (
        patch.object(source, "_data_dirs", return_value=[tmp_path / "none"]),
        patch.object(source, "_addon_manager", AsyncMock(return_value=None)),
    ):
        check = await source.async_restore_check(make_z2m_zip(), {"stored": True})
    assert check.ok is True
    assert check.reasons == []
    assert check.backup == {"stored": True}
    assert check.current["ieee"] == "0x00124b0024c2f0a1"
    assert check.warnings == ["Could not locate the Z2M data directory; files will be staged for manual restore"]


async def test_restore_check_no_ocbf_member_fails(
    hass: HomeAssistant, source: Zigbee2MQTTSource, fast_timeouts, tmp_path: Path
) -> None:
    with (
        patch.object(source, "_data_dirs", return_value=[tmp_path / "none"]),
        patch.object(source, "_addon_manager", AsyncMock(return_value=None)),
    ):
        check = await source.async_restore_check(make_z2m_zip(ocbf=None), {})
    assert check.ok is False
    assert check.reasons == ["Backup zip does not contain coordinator_backup.json"]
    assert any(w.startswith("Could not read live coordinator identity:") for w in check.warnings)
    assert check.current == {}


async def test_restore_check_same_ieee_warns(hass: HomeAssistant, source: Zigbee2MQTTSource, tmp_path: Path) -> None:
    # Backup OCBF has 00124b0024c2f0a1; make the live one match case-insensitively (no 0x prefix)
    info = {**BRIDGE_INFO_SAMPLE, "coordinator": {**BRIDGE_INFO_SAMPLE["coordinator"], "ieee_address": "00124B0024C2F0A1"}}
    _publish_info(hass, info)
    with (
        patch.object(source, "_data_dirs", return_value=[tmp_path / "none"]),
        patch.object(source, "_addon_manager", AsyncMock(return_value=None)),
    ):
        check = await source.async_restore_check(make_z2m_zip(), {})
    assert check.ok is True
    assert any("already has the backup's IEEE address" in w for w in check.warnings)


async def test_restore_check_different_ieee_no_warning(hass: HomeAssistant, source: Zigbee2MQTTSource, tmp_path: Path) -> None:
    _publish_info(hass)  # live is "0x00124b..." which does not string-match "00124b..."
    with (
        patch.object(source, "_data_dirs", return_value=[tmp_path / "none"]),
        patch.object(source, "_addon_manager", AsyncMock(return_value=None)),
    ):
        check = await source.async_restore_check(make_z2m_zip(), {})
    assert not any("already has the backup's IEEE" in w for w in check.warnings)


async def test_restore_check_addon_and_data_dir_warnings(
    hass: HomeAssistant, source: Zigbee2MQTTSource, fast_timeouts, tmp_path: Path
) -> None:
    data_dir = tmp_path / "addon_configs" / "45df7312_zigbee2mqtt"
    data_dir.mkdir(parents=True)
    (data_dir / "configuration.yaml").write_text("x: 1")
    mgr = MagicMock(addon_slug="45df7312_zigbee2mqtt")

    with patch.object(source, "_data_dirs", return_value=[data_dir]):
        with patch.object(source, "_addon_manager", AsyncMock(return_value=mgr)):
            check = await source.async_restore_check(make_z2m_zip(), {})
        assert f"Will stop add-on 45df7312_zigbee2mqtt, write {data_dir}, then start it" in check.warnings

        with patch.object(source, "_addon_manager", AsyncMock(return_value=None)):
            check = await source.async_restore_check(make_z2m_zip(), {})
        assert any("no Supervisor add-on; you must stop/start Z2M yourself" in w for w in check.warnings)
    assert check.ok is True


async def test_restore_without_addon_stages_files_and_raises(hass: HomeAssistant, source: Zigbee2MQTTSource) -> None:
    with (
        patch.object(source, "_addon_manager", AsyncMock(return_value=None)),
        patch.object(source, "_find_data_dir", return_value=None),
        pytest.raises(RuntimeError, match="not running as a Supervisor add-on") as ei,
    ):
        await source.async_restore(make_z2m_zip(), {})
    staging = Path(hass.config.path("radio_guard", "zigbee2mqtt", "restore"))
    assert str(staging) in str(ei.value)
    assert json.loads((staging / "coordinator_backup.json").read_text()) == OCBF_SAMPLE
    assert (staging / "database.db").exists()
    assert not (staging / "configuration.yaml").exists()


async def test_restore_with_data_dir_but_no_addon_also_stages(
    hass: HomeAssistant, source: Zigbee2MQTTSource, tmp_path: Path
) -> None:
    data_dir = tmp_path / "z2m"
    data_dir.mkdir()
    (data_dir / "configuration.yaml").write_text("x")
    with (
        patch.object(source, "_addon_manager", AsyncMock(return_value=None)),
        patch.object(source, "_data_dirs", return_value=[data_dir]),
        pytest.raises(RuntimeError),
    ):
        await source.async_restore(make_z2m_zip(), {})
    # Nothing was written into the live data dir without being able to stop Z2M
    assert not (data_dir / "coordinator_backup.json").exists()


async def test_restore_with_addon_stops_writes_starts(hass: HomeAssistant, source: Zigbee2MQTTSource, tmp_path: Path) -> None:
    data_dir = tmp_path / "z2m"
    data_dir.mkdir()
    (data_dir / "configuration.yaml").write_text("x")
    order: list[str] = []
    mgr = MagicMock(addon_slug="45df7312_zigbee2mqtt")

    async def stop():
        order.append("stop")
        assert not (data_dir / "coordinator_backup.json").exists()

    async def start():
        order.append("start")
        assert (data_dir / "coordinator_backup.json").exists()

    mgr.async_stop_addon = stop
    mgr.async_start_addon = start

    with (
        patch.object(source, "_addon_manager", AsyncMock(return_value=mgr)),
        patch.object(source, "_data_dirs", return_value=[data_dir]),
    ):
        await source.async_restore(make_z2m_zip(), {})

    assert order == ["stop", "start"]
    assert json.loads((data_dir / "coordinator_backup.json").read_text()) == OCBF_SAMPLE
    assert (data_dir / "database.db").exists()


async def test_restore_with_addon_restarts_even_if_write_fails(
    hass: HomeAssistant, source: Zigbee2MQTTSource, tmp_path: Path
) -> None:
    data_dir = tmp_path / "z2m"
    data_dir.mkdir()
    (data_dir / "configuration.yaml").write_text("x")
    mgr = MagicMock(addon_slug="45df7312_zigbee2mqtt")
    mgr.async_stop_addon = AsyncMock()
    mgr.async_start_addon = AsyncMock()
    with (
        patch.object(source, "_addon_manager", AsyncMock(return_value=mgr)),
        patch.object(source, "_data_dirs", return_value=[data_dir]),
        patch.object(source, "_write_restore_files", side_effect=OSError("ro fs")),
        pytest.raises(OSError),
    ):
        await source.async_restore(make_z2m_zip(), {})
    mgr.async_stop_addon.assert_awaited_once()
    mgr.async_start_addon.assert_awaited_once()
