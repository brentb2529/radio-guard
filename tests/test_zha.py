"""ZHA backup source: identity, zigpy-native backups, pre-flight, restore orchestration.

``homeassistant.components.zha`` cannot be imported in the test venv (its
hardware/usb dependencies are not installed), and the source only imports it
lazily. Fake ``zha.helpers`` / ``zha.radio_manager`` modules are injected into
``sys.modules`` so the lazy imports resolve to test doubles.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import zigpy.types as t
import zigpy.zdo.types as zdo_t
from homeassistant.core import HomeAssistant
from zigpy.backups import NetworkBackup
from zigpy.exceptions import CannotWriteNetworkSettings, DestructiveWriteNetworkSettings
from zigpy.state import Key, NetworkInfo, NodeInfo

from custom_components.radio_guard.backup.base import SourceUnavailable
from custom_components.radio_guard.backup.zha import DEFAULT_COUNTER_INCREMENT, ZHASource

from .conftest import OCBF_SAMPLE

ZHA_MOD = "custom_components.radio_guard.backup.zha"

IEEE = "00:12:4b:00:24:c2:f0:a1"
IEEE_NORM = "00124b0024c2f0a1"
OTHER_IEEE = "00:12:4b:00:99:99:99:99"
EPID = "dd:dd:dd:dd:dd:dd:dd:dd"
EPID_NORM = "dddddddddddddddd"
KEY = "01:03:05:07:09:0b:0d:0f:00:02:04:06:08:0a:0c:0d"
CHILD = "00:15:8d:00:01:aa:bb:cc"


def make_backup(
    *,
    ieee: str = IEEE,
    epid: str = EPID,
    pan_id: int = 0x1A62,
    channel: int = 15,
    tx_counter: int = 1234,
    nwk_addresses: dict | None = None,
    children: list | None = None,
    stack: str = "ezsp",
) -> NetworkBackup:
    ni = NetworkInfo(
        extended_pan_id=t.ExtendedPanId.convert(epid),
        pan_id=t.PanId(pan_id),
        channel=channel,
        nwk_update_id=2,
        network_key=Key(key=t.KeyData.convert(KEY), tx_counter=tx_counter),
        nwk_addresses={t.EUI64.convert(CHILD): t.NWK(0x1234)} if nwk_addresses is None else nwk_addresses,
        children=[t.EUI64.convert(CHILD)] if children is None else children,
        stack_specific={stack: {"hashed_tclk": "ab" * 16}},
        source="bellows@0.40.0",
    )
    node = NodeInfo(
        nwk=t.NWK(0),
        ieee=t.EUI64.convert(ieee),
        logical_type=zdo_t.LogicalType.Coordinator,
        model="SkyConnect",
        manufacturer="Nabu Casa",
        version="7.4.4",
    )
    return NetworkBackup(network_info=ni, node_info=node)


def backup_bytes(backup: NetworkBackup) -> bytes:
    return json.dumps(backup.as_dict()).encode()


class FakeZHA:
    """Controls what the fake ``homeassistant.components.zha.*`` modules return."""

    def __init__(self) -> None:
        self.entry: Any = SimpleNamespace(
            entry_id="zha_entry",
            data={"radio_type": "ezsp", "device": {"path": "/dev/ttyUSB0"}},
        )
        self.entry_error: Exception | None = None
        self.gateway_error: Exception | None = None
        live = make_backup()
        self.app = MagicMock()
        self.app.state = SimpleNamespace(network_info=live.network_info, node_info=live.node_info)
        self.app.backups.create_backup = AsyncMock(return_value=live)
        self.gateway: Any = SimpleNamespace(application_controller=self.app)
        self.mgr = MagicMock()
        self.mgr.async_load_network_settings = AsyncMock(return_value=None)
        self.mgr.restore_backup = AsyncMock(return_value=None)
        self.radio_manager_cls = MagicMock()
        self.radio_manager_cls.from_config_entry = MagicMock(return_value=self.mgr)

    def get_config_entry(self, hass):
        if self.entry_error is not None:
            raise self.entry_error
        return self.entry

    def get_zha_gateway(self, hass):
        if self.gateway_error is not None:
            raise self.gateway_error
        return self.gateway

    def set_live(self, backup: NetworkBackup) -> None:
        self.app.state = SimpleNamespace(network_info=backup.network_info, node_info=backup.node_info)


@pytest.fixture
def fake_zha(hass: HomeAssistant):
    fake = FakeZHA()
    pkg = types.ModuleType("homeassistant.components.zha")
    pkg.__path__ = []  # mark as package
    helpers = types.ModuleType("homeassistant.components.zha.helpers")
    helpers.get_config_entry = fake.get_config_entry
    helpers.get_zha_gateway = fake.get_zha_gateway
    radio_manager = types.ModuleType("homeassistant.components.zha.radio_manager")
    radio_manager.ZhaRadioManager = fake.radio_manager_cls
    with patch.dict(
        sys.modules,
        {
            "homeassistant.components.zha": pkg,
            "homeassistant.components.zha.helpers": helpers,
            "homeassistant.components.zha.radio_manager": radio_manager,
        },
    ):
        hass.config.components.add("zha")
        yield fake


@pytest.fixture
def source(hass: HomeAssistant, fake_zha: FakeZHA) -> ZHASource:
    return ZHASource(hass)


@pytest.fixture
def entries(hass: HomeAssistant):
    """Patch config-entry lifecycle calls; record call order on one parent mock."""
    order = MagicMock()
    unload = AsyncMock(return_value=True)
    setup = AsyncMock(return_value=True)
    order.attach_mock(unload, "unload")
    order.attach_mock(setup, "setup")
    with (
        patch.object(hass.config_entries, "async_unload", unload),
        patch.object(hass.config_entries, "async_setup", setup),
    ):
        yield SimpleNamespace(unload=unload, setup=setup, order=order)


# --------------------------------------------------------------- availability


async def test_basic_attributes(source: ZHASource) -> None:
    assert source.id == "zha"
    assert source.name == "ZHA"


async def test_available_when_loaded_with_entry(source: ZHASource) -> None:
    assert source.available() is True


async def test_unavailable_when_component_not_loaded(hass: HomeAssistant, source: ZHASource) -> None:
    hass.config.components.remove("zha")
    assert source.available() is False


async def test_unavailable_when_no_gateway(source: ZHASource, fake_zha: FakeZHA) -> None:
    fake_zha.entry_error = ValueError("No gateway object exists to retrieve the config entry from.")
    assert source.available() is False


async def test_unavailable_when_zha_not_importable(hass: HomeAssistant) -> None:
    hass.config.components.add("zha")
    with patch.dict(sys.modules, {"homeassistant.components.zha.helpers": None}):
        src = ZHASource(hass)
        assert src._entry() is None
        assert src.available() is False
        with pytest.raises(SourceUnavailable, match="not installed"):
            src._app()


async def test_app_gateway_not_running(source: ZHASource, fake_zha: FakeZHA) -> None:
    fake_zha.gateway_error = ValueError("No gateway object exists")
    with pytest.raises(SourceUnavailable, match="not running"):
        source._app()


async def test_app_controller_not_initialised(source: ZHASource, fake_zha: FakeZHA) -> None:
    fake_zha.gateway = SimpleNamespace(application_controller=None)
    with pytest.raises(SourceUnavailable, match="not initialised"):
        await source.async_identity()


# ------------------------------------------------------------------- identity


async def test_identity_normalised(source: ZHASource) -> None:
    ident = await source.async_identity()
    assert ident == {
        "ieee": IEEE_NORM,
        "pan_id": "0x1a62",
        "extended_pan_id": EPID_NORM,
        "channel": 15,
        "nwk_update_id": 2,
        "frame_counter": 1234,
        "radio_type": "ezsp",
        "device_path": "/dev/ttyUSB0",
        "stack": "ezsp",
        "model": "SkyConnect",
        "manufacturer": "Nabu Casa",
        "version": "7.4.4",
        "device_count": 1,
    }


async def test_identity_device_count_falls_back_to_children(source: ZHASource, fake_zha: FakeZHA) -> None:
    kids = [t.EUI64.convert(CHILD), t.EUI64.convert("00:15:8d:00:01:dd:ee:ff")]
    fake_zha.set_live(make_backup(nwk_addresses={}, children=kids))
    assert (await source.async_identity())["device_count"] == 2


async def test_device_count_comes_from_the_device_table(source: ZHASource, fake_zha: FakeZHA) -> None:
    """A router is not a child: with only the backup's tables a real network of one plug counted as 0."""
    live = make_backup(nwk_addresses={}, children=[])
    fake_zha.set_live(live)
    fake_zha.app.backups.create_backup.return_value = live
    me = live.node_info.ieee
    fake_zha.app.devices = {
        me: object(),
        t.EUI64.convert(CHILD): object(),
        t.EUI64.convert("00:15:8d:00:01:dd:ee:ff"): object(),
    }
    assert (await source.async_identity())["device_count"] == 2
    assert (await source.async_backup()).meta["device_count"] == 2


async def test_identity_without_entry_or_network(source: ZHASource, fake_zha: FakeZHA) -> None:
    fake_zha.entry_error = ValueError("gone")
    fake_zha.app.state = SimpleNamespace(network_info=None, node_info=None)
    ident = await source.async_identity()
    assert ident["ieee"] is None
    assert ident["pan_id"] is None
    assert ident["extended_pan_id"] is None
    assert ident["channel"] is None
    assert ident["frame_counter"] is None
    assert ident["radio_type"] is None
    assert ident["device_path"] is None
    assert ident["stack"] is None
    assert ident["device_count"] is None
    assert ident["model"] is None


async def test_identity_entry_without_device(source: ZHASource, fake_zha: FakeZHA) -> None:
    fake_zha.entry = SimpleNamespace(entry_id="zha_entry", data={"radio_type": "znp"})
    ident = await source.async_identity()
    assert ident["radio_type"] == "znp"
    assert ident["device_path"] is None


# --------------------------------------------------------------------- backup


async def test_backup_payload_is_zigpy_native_json(source: ZHASource, fake_zha: FakeZHA) -> None:
    payload = await source.async_backup()

    fake_zha.app.backups.create_backup.assert_awaited_once_with(load_devices=True)
    assert payload.ext == ".json"
    raw = payload.data.decode()
    parsed = json.loads(raw)
    assert list(parsed) == sorted(parsed)  # sort_keys=True
    assert "\n  " in raw  # indent=2
    original = fake_zha.app.backups.create_backup.return_value
    assert parsed == json.loads(json.dumps(original.as_dict()))
    assert NetworkBackup.from_dict(parsed) == original


async def test_backup_metadata(source: ZHASource) -> None:
    meta = (await source.async_backup()).meta
    assert meta == {
        "format": "zigpy-network-backup",
        "format_version": 1,
        "is_complete": True,
        "ieee": IEEE_NORM,
        "pan_id": "0x1a62",
        "extended_pan_id": EPID_NORM,
        "channel": 15,
        "nwk_update_id": 2,
        "device_count": 1,
        "source": "bellows@0.40.0",
        "stack": "ezsp",
        "model": "SkyConnect",
        "manufacturer": "Nabu Casa",
        "version": "7.4.4",
        "frame_counter": 1234,
        "radio_type": "ezsp",
        "device_path": "/dev/ttyUSB0",
    }


async def test_backup_meta_comes_from_backup_not_live_state(source: ZHASource, fake_zha: FakeZHA) -> None:
    fake_zha.app.backups.create_backup.return_value = make_backup(ieee=OTHER_IEEE, tx_counter=99)
    meta = (await source.async_backup()).meta
    assert meta["ieee"] == "00124b0099999999"
    assert meta["frame_counter"] == 99


async def test_backup_incomplete_flagged(source: ZHASource, fake_zha: FakeZHA) -> None:
    fake_zha.app.backups.create_backup.return_value = make_backup(channel=0)
    meta = (await source.async_backup()).meta
    assert meta["is_complete"] is False


async def test_backup_device_count_children_fallback(source: ZHASource, fake_zha: FakeZHA) -> None:
    fake_zha.app.backups.create_backup.return_value = make_backup(
        nwk_addresses={}, children=[t.EUI64.convert(CHILD)] * 3
    )
    assert (await source.async_backup()).meta["device_count"] == 3


async def test_backup_survives_identity_failure(source: ZHASource) -> None:
    with patch.object(source, "async_identity", AsyncMock(side_effect=RuntimeError("boom"))):
        meta = (await source.async_backup()).meta
    assert "radio_type" not in meta
    assert "device_path" not in meta
    assert meta["ieee"] == IEEE_NORM


async def test_backup_unavailable_gateway(source: ZHASource, fake_zha: FakeZHA) -> None:
    fake_zha.gateway_error = ValueError("No gateway object exists")
    with pytest.raises(SourceUnavailable):
        await source.async_backup()


async def test_backup_timeout(source: ZHASource, fake_zha: FakeZHA) -> None:
    async def slow(**_: Any):
        await asyncio.sleep(10)

    fake_zha.app.backups.create_backup = slow
    with patch(f"{ZHA_MOD}.ZHA_BACKUP_TIMEOUT", 0.01), pytest.raises(TimeoutError):
        await source.async_backup()


# ------------------------------------------------------------ restore check


def _reasons(check) -> str:
    return " | ".join(check.reasons)


def _warnings(check) -> str:
    return " | ".join(check.warnings)


async def test_check_invalid_file(source: ZHASource) -> None:
    check = await source.async_restore_check(b"not json", {})
    assert check.ok is False
    assert "not a valid zigpy/OCBF file" in _reasons(check)


async def test_check_zha_not_loaded(hass: HomeAssistant, source: ZHASource) -> None:
    hass.config.components.remove("zha")
    meta = {"ieee": IEEE_NORM}
    check = await source.async_restore_check(backup_bytes(make_backup()), meta)
    assert check.ok is False
    assert "ZHA is not loaded" in _reasons(check)
    assert check.backup is meta
    assert check.current == {}


async def test_check_blocks_same_ieee_and_network(source: ZHASource) -> None:
    meta = {"ieee": IEEE_NORM}
    check = await source.async_restore_check(backup_bytes(make_backup()), meta)
    assert check.ok is False
    assert "already carries this backup's network and IEEE" in _reasons(check)
    assert check.current["ieee"] == IEEE_NORM
    assert check.backup is meta


async def test_check_same_ieee_other_network_allowed(source: ZHASource, fake_zha: FakeZHA) -> None:
    fake_zha.set_live(make_backup(epid="aa:aa:aa:aa:aa:aa:aa:aa", pan_id=0x0001))
    check = await source.async_restore_check(backup_bytes(make_backup()), {})
    assert check.ok is True, check.reasons
    assert "Silicon Labs" not in _warnings(check)


async def test_check_same_ieee_same_epid_other_pan_allowed(source: ZHASource, fake_zha: FakeZHA) -> None:
    fake_zha.set_live(make_backup(pan_id=0x2222))
    check = await source.async_restore_check(backup_bytes(make_backup()), {})
    assert check.ok is True, check.reasons


async def test_check_identity_compares_ocbf_against_zigpy(source: ZHASource, fake_zha: FakeZHA) -> None:
    """A Z2M OCBF file and a zigpy live state describing the same radio must match."""
    fake_zha.set_live(make_backup(ieee="00:12:4b:00:24:c2:f0:a1", channel=11))
    check = await source.async_restore_check(json.dumps(OCBF_SAMPLE).encode(), {})
    assert check.ok is False
    assert "already carries" in _reasons(check)


async def test_check_ezsp_different_ieee_warns(source: ZHASource, fake_zha: FakeZHA) -> None:
    fake_zha.set_live(make_backup(ieee=OTHER_IEEE))
    check = await source.async_restore_check(backup_bytes(make_backup()), {})
    assert check.ok is True, check.reasons
    assert "Silicon Labs radio with a different IEEE" in _warnings(check)


async def test_check_non_ezsp_different_ieee_no_warning(source: ZHASource, fake_zha: FakeZHA) -> None:
    fake_zha.set_live(make_backup(ieee=OTHER_IEEE))
    fake_zha.entry = SimpleNamespace(entry_id="zha_entry", data={"radio_type": "znp", "device": {}})
    check = await source.async_restore_check(backup_bytes(make_backup()), {})
    assert check.ok is True
    assert "Silicon Labs" not in _warnings(check)


async def test_check_incomplete_backup_blocked(source: ZHASource, fake_zha: FakeZHA) -> None:
    fake_zha.set_live(make_backup(ieee=OTHER_IEEE))
    check = await source.async_restore_check(backup_bytes(make_backup(channel=0)), {})
    assert check.ok is False
    assert "Backup is incomplete" in _reasons(check)


async def test_check_frame_counter_far_ahead_warns(source: ZHASource, fake_zha: FakeZHA) -> None:
    fake_zha.set_live(make_backup(ieee=OTHER_IEEE, tx_counter=1000 + DEFAULT_COUNTER_INCREMENT + 1))
    check = await source.async_restore_check(backup_bytes(make_backup(tx_counter=1000)), {})
    assert check.ok is True
    assert "frame counter (11001) is far ahead of the backup (1000)" in _warnings(check)


async def test_check_frame_counter_within_headroom(source: ZHASource, fake_zha: FakeZHA) -> None:
    fake_zha.set_live(make_backup(ieee=OTHER_IEEE, tx_counter=1000 + DEFAULT_COUNTER_INCREMENT))
    check = await source.async_restore_check(backup_bytes(make_backup(tx_counter=1000)), {})
    assert "frame counter" not in _warnings(check)


async def test_check_identity_failure_is_warning(source: ZHASource, fake_zha: FakeZHA) -> None:
    fake_zha.gateway_error = ValueError("No gateway object exists")
    check = await source.async_restore_check(backup_bytes(make_backup()), {})
    assert check.ok is True
    assert "Could not read the attached radio" in _warnings(check)
    assert check.current == {}


async def test_check_always_warns_about_unload_and_old_radio(source: ZHASource, fake_zha: FakeZHA) -> None:
    fake_zha.set_live(make_backup(ieee=OTHER_IEEE))
    check = await source.async_restore_check(backup_bytes(make_backup()), {})
    assert "ZHA will be unloaded during the restore" in _warnings(check)
    assert "Never power the old coordinator again" in _warnings(check)


# ------------------------------------------------------------------- restore


async def test_restore_happy_path(source: ZHASource, fake_zha: FakeZHA, entries) -> None:
    backup = make_backup()
    fake_zha.mgr.attach_mock(fake_zha.mgr.async_load_network_settings, "load")
    fake_zha.mgr.attach_mock(fake_zha.mgr.restore_backup, "restore")
    entries.order.attach_mock(fake_zha.mgr, "mgr")

    await source.async_restore(backup_bytes(backup), {})

    fake_zha.radio_manager_cls.from_config_entry.assert_called_once()
    args = fake_zha.radio_manager_cls.from_config_entry.call_args.args
    assert args[1] is fake_zha.entry
    entries.unload.assert_awaited_once_with("zha_entry")
    fake_zha.mgr.restore_backup.assert_awaited_once()
    passed = fake_zha.mgr.restore_backup.await_args
    assert passed.args[0] == backup
    assert isinstance(passed.args[0], NetworkBackup)
    assert passed.kwargs == {"overwrite_ieee": False}
    entries.setup.assert_awaited_once_with("zha_entry")
    names = [c[0] for c in entries.order.mock_calls]
    assert names == ["unload", "mgr.load", "mgr.restore", "setup"]


async def test_restore_force_overwrites_ieee(source: ZHASource, fake_zha: FakeZHA, entries) -> None:
    await source.async_restore(backup_bytes(make_backup()), {"allow_ieee_overwrite": True})
    assert fake_zha.mgr.restore_backup.await_args.kwargs == {"overwrite_ieee": True}


async def test_restore_without_entry(source: ZHASource, fake_zha: FakeZHA, entries) -> None:
    fake_zha.entry_error = ValueError("No gateway")
    with pytest.raises(SourceUnavailable, match="config entry not found"):
        await source.async_restore(backup_bytes(make_backup()), {})
    entries.unload.assert_not_awaited()
    entries.setup.assert_not_awaited()


async def test_restore_invalid_file_touches_nothing(source: ZHASource, fake_zha: FakeZHA, entries) -> None:
    with pytest.raises(json.JSONDecodeError):
        await source.async_restore(b"garbage", {})
    entries.unload.assert_not_awaited()
    fake_zha.mgr.restore_backup.assert_not_awaited()


async def test_restore_unload_failure(source: ZHASource, fake_zha: FakeZHA, entries) -> None:
    entries.unload.return_value = False
    with pytest.raises(RuntimeError, match="Could not unload ZHA"):
        await source.async_restore(backup_bytes(make_backup()), {})
    fake_zha.mgr.restore_backup.assert_not_awaited()
    entries.setup.assert_not_awaited()


async def test_restore_destructive_write(source: ZHASource, fake_zha: FakeZHA, entries) -> None:
    fake_zha.mgr.restore_backup.side_effect = DestructiveWriteNetworkSettings("one-time write")
    with pytest.raises(RuntimeError, match="force=true") as exc:
        await source.async_restore(backup_bytes(make_backup()), {})
    assert isinstance(exc.value.__cause__, DestructiveWriteNetworkSettings)
    entries.setup.assert_awaited_once_with("zha_entry")


async def test_restore_cannot_write(source: ZHASource, fake_zha: FakeZHA, entries) -> None:
    fake_zha.mgr.restore_backup.side_effect = CannotWriteNetworkSettings("nope")
    with pytest.raises(RuntimeError, match="Radio refused the backup: nope") as exc:
        await source.async_restore(backup_bytes(make_backup()), {})
    assert isinstance(exc.value.__cause__, CannotWriteNetworkSettings)
    entries.setup.assert_awaited_once_with("zha_entry")


async def test_restore_reloads_after_load_failure(source: ZHASource, fake_zha: FakeZHA, entries) -> None:
    fake_zha.mgr.async_load_network_settings.side_effect = OSError("serial port busy")
    with pytest.raises(OSError, match="serial port busy"):
        await source.async_restore(backup_bytes(make_backup()), {})
    fake_zha.mgr.restore_backup.assert_not_awaited()
    entries.setup.assert_awaited_once_with("zha_entry")


async def test_restore_timeout_reloads(source: ZHASource, fake_zha: FakeZHA, entries) -> None:
    async def slow(*_: Any, **__: Any):
        await asyncio.sleep(10)

    fake_zha.mgr.restore_backup = slow
    with patch(f"{ZHA_MOD}.RESTORE_TIMEOUT", 0.01), pytest.raises(TimeoutError):
        await source.async_restore(backup_bytes(make_backup()), {})
    entries.setup.assert_awaited_once_with("zha_entry")


async def test_restore_accepts_ocbf(source: ZHASource, fake_zha: FakeZHA, entries) -> None:
    await source.async_restore(json.dumps(OCBF_SAMPLE).encode(), {})
    passed = fake_zha.mgr.restore_backup.await_args.args[0]
    assert isinstance(passed, NetworkBackup)
    assert str(passed.node_info.ieee) == IEEE
    assert passed.network_info.pan_id == 0x1A62
