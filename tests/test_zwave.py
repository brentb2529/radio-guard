"""Z-Wave JS backup source: identity, NVM backups + keys, pre-flight, restore orchestration."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from zwave_js_server.const import ControllerStatus, InclusionState

from custom_components.radio_guard.backup.base import SourceUnavailable
from custom_components.radio_guard.backup.zwave import (
    KEY_FIELDS,
    ZWAVE_DOMAIN,
    ZWaveJSSource,
    _series,
)

ZW_MOD = "custom_components.radio_guard.backup.zwave"
HELPERS = "homeassistant.components.zwave_js.helpers"

HOME_ID = 0xE1A2B3C4
OTHER_HOME_ID = 0xC0FFEE01
NVM = b"\x01\x02\x03NVM" * 64

KEYS = {
    "s0_legacy_key": "A" * 32,
    "s2_access_control_key": "B" * 32,
    "s2_authenticated_key": "C" * 32,
    "s2_unauthenticated_key": "D" * 32,
    "lr_s2_access_control_key": "E" * 32,
    "lr_s2_authenticated_key": "F" * 32,
}


class FakeController:
    """Duck-typed zwave_js_server Controller with only what the source touches."""

    def __init__(self, **overrides: Any) -> None:
        self.home_id: int | None = HOME_ID
        self.own_node_id = 1
        self.sdk_version = "7.23.2"
        self.firmware_version = "7.23.2"
        self.manufacturer_id = 0x0000
        self.product_type = 0x0004
        self.product_id = 0x0005
        self.controller_type = 1
        self.nodes = {1: object()}
        self.status = ControllerStatus.READY
        self.inclusion_state = InclusionState.IDLE
        self.is_rebuilding_routes = False
        self.async_is_any_ota_firmware_update_in_progress = AsyncMock(return_value=False)
        self.async_is_firmware_update_in_progress = AsyncMock(return_value=False)
        self.async_backup_nvm_raw = AsyncMock(return_value=NVM)
        self.async_restore_nvm = AsyncMock(return_value=None)
        for k, v in overrides.items():
            setattr(self, k, v)


def add_zwave_entry(
    hass: HomeAssistant,
    *,
    ctrl: FakeController | None = None,
    unique_id: str = str(HOME_ID),
    data: dict[str, Any] | None = None,
    state: ConfigEntryState = ConfigEntryState.LOADED,
    runtime: Any = "default",
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=ZWAVE_DOMAIN,
        title="Z-Wave JS",
        unique_id=unique_id,
        data={"url": "ws://localhost:3000", "use_addon": True, **KEYS} if data is None else data,
        state=state,
    )
    entry.add_to_hass(hass)
    if runtime == "default":
        driver = SimpleNamespace(controller=ctrl or FakeController())
        runtime = SimpleNamespace(client=SimpleNamespace(driver=driver))
    entry.runtime_data = runtime
    return entry


def ctrl_of(entry: MockConfigEntry) -> FakeController:
    return entry.runtime_data.client.driver.controller


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    return add_zwave_entry(hass)


@pytest.fixture
def ctrl(entry: MockConfigEntry) -> FakeController:
    return ctrl_of(entry)


@pytest.fixture
def source(hass: HomeAssistant) -> ZWaveJSSource:
    return ZWaveJSSource(hass)


def backup_meta(**overrides: Any) -> dict[str, Any]:
    meta = {
        "format": "zwave-nvm-raw",
        "home_id": OTHER_HOME_ID,
        "sdk_version": "7.23.2",
        "security_keys": dict(KEYS),
    }
    meta.update(overrides)
    return meta


def _reasons(check) -> str:
    return " | ".join(check.reasons)


def _warnings(check) -> str:
    return " | ".join(check.warnings)


# ------------------------------------------------------------------ helpers


@pytest.mark.parametrize(
    ("sdk", "series"),
    [(None, None), ("", None), ("6.81.06", 500), ("6.61", 500), ("7.19.0", 700), ("8.0", 700), ("x.y", None)],
)
def test_series(sdk: str | None, series: int | None) -> None:
    assert _series(sdk) == series


async def test_basic_attributes(source: ZWaveJSSource) -> None:
    assert source.id == "zwave_js"
    assert source.name == "Z-Wave JS"
    assert source.home_id is None


# -------------------------------------------------------------- availability


async def test_unavailable_without_entries(source: ZWaveJSSource) -> None:
    assert source.available() is False


async def test_unavailable_when_entry_not_loaded(hass: HomeAssistant, source: ZWaveJSSource) -> None:
    add_zwave_entry(hass, state=ConfigEntryState.SETUP_RETRY)
    assert source.available() is False


async def test_available_when_loaded(source: ZWaveJSSource, entry: MockConfigEntry) -> None:
    assert source.available() is True
    assert source._entry() is entry


async def test_home_id_pin_selects_matching_entry(hass: HomeAssistant) -> None:
    first = add_zwave_entry(hass, unique_id="111")
    second = add_zwave_entry(hass, unique_id="222")
    assert ZWaveJSSource(hass, home_id=222)._entry() is second
    assert ZWaveJSSource(hass, home_id=111)._entry() is first
    assert ZWaveJSSource(hass)._entry() is first


async def test_home_id_pin_falls_back_to_first_loaded(hass: HomeAssistant) -> None:
    """Unknown pinned home ID (e.g. a replacement stick) falls back to the first loaded entry."""
    first = add_zwave_entry(hass, unique_id="111")
    add_zwave_entry(hass, unique_id="222", state=ConfigEntryState.NOT_LOADED)
    assert ZWaveJSSource(hass, home_id=999)._entry() is first


async def test_client_from_runtime_data_object(entry: MockConfigEntry) -> None:
    assert ZWaveJSSource._client(entry) is entry.runtime_data.client


async def test_client_from_legacy_runtime_dict(hass: HomeAssistant) -> None:
    client = SimpleNamespace(driver=SimpleNamespace(controller=FakeController()))
    legacy = add_zwave_entry(hass, runtime={"client": client})
    assert ZWaveJSSource._client(legacy) is client


async def test_client_missing(hass: HomeAssistant) -> None:
    bare = add_zwave_entry(hass, runtime=None)
    with pytest.raises(SourceUnavailable, match="client not found"):
        ZWaveJSSource._client(bare)


async def test_controller_not_loaded(source: ZWaveJSSource) -> None:
    with pytest.raises(SourceUnavailable, match="not loaded"):
        source._controller()


@pytest.mark.parametrize("driver", [None, SimpleNamespace(controller=None)])
async def test_controller_driver_not_ready(hass: HomeAssistant, source: ZWaveJSSource, driver) -> None:
    add_zwave_entry(hass, runtime=SimpleNamespace(client=SimpleNamespace(driver=driver)))
    with pytest.raises(SourceUnavailable, match="driver not ready"):
        source._controller()


# ------------------------------------------------------------------ identity


async def test_identity(source: ZWaveJSSource, ctrl: FakeController) -> None:
    ctrl.nodes = {1: object(), 2: object(), 3: object()}
    assert await source.async_identity() == {
        "home_id": HOME_ID,
        "home_id_hex": "0xE1A2B3C4",
        "own_node_id": 1,
        "sdk_version": "7.23.2",
        "firmware_version": "7.23.2",
        "manufacturer_id": 0,
        "product_type": 4,
        "product_id": 5,
        "controller_type": 1,
        "node_count": 3,
        "keys_captured": True,
        "use_addon": True,
    }


async def test_identity_hex_zero_padded(source: ZWaveJSSource, ctrl: FakeController) -> None:
    ctrl.home_id = 0xAB
    assert (await source.async_identity())["home_id_hex"] == "0x000000AB"


async def test_identity_without_home_id_or_keys(hass: HomeAssistant, source: ZWaveJSSource) -> None:
    e = add_zwave_entry(hass, data={"url": "ws://zwave:3000"})
    ctrl_of(e).home_id = None
    del ctrl_of(e).controller_type
    ident = await source.async_identity()
    assert ident["home_id"] is None
    assert ident["home_id_hex"] is None
    assert ident["controller_type"] is None
    assert ident["keys_captured"] is False
    assert ident["use_addon"] is False


# -------------------------------------------------------------------- backup


async def test_backup_payload_and_keys(source: ZWaveJSSource, ctrl: FakeController) -> None:
    payload = await source.async_backup()
    ctrl.async_backup_nvm_raw.assert_awaited_once_with()
    assert payload.data == NVM
    assert payload.ext == ".nvm"
    meta = payload.meta
    assert meta["format"] == "zwave-nvm-raw"
    assert meta["home_id"] == HOME_ID
    assert meta["home_id_hex"] == "0xE1A2B3C4"
    assert meta["sdk_version"] == "7.23.2"
    assert meta["keys_captured"] is True
    assert meta["security_keys"] == KEYS
    assert "security_keys_note" not in meta


async def test_backup_keys_only_known_nonempty_fields(hass: HomeAssistant, source: ZWaveJSSource) -> None:
    add_zwave_entry(
        hass,
        data={
            "url": "ws://zwave:3000",
            "use_addon": True,
            "s0_legacy_key": "0123456789ABCDEF0123456789ABCDEF",
            "s2_access_control_key": "",
            "s2_authenticated_key": None,
            "network_key": "LEGACY",  # not in KEY_FIELDS
        },
    )
    meta = (await source.async_backup()).meta
    assert meta["security_keys"] == {"s0_legacy_key": "0123456789ABCDEF0123456789ABCDEF"}
    assert set(meta["security_keys"]) <= set(KEY_FIELDS)


async def test_backup_without_keys_adds_note(hass: HomeAssistant, source: ZWaveJSSource) -> None:
    add_zwave_entry(hass, data={"url": "ws://zwave:3000", "use_addon": False})
    meta = (await source.async_backup()).meta
    assert "security_keys" not in meta
    assert "Z-Wave JS UI" in meta["security_keys_note"]
    assert meta["keys_captured"] is False


async def test_backup_empty_nvm(source: ZWaveJSSource, ctrl: FakeController) -> None:
    ctrl.async_backup_nvm_raw.return_value = b""
    with pytest.raises(RuntimeError, match="empty NVM"):
        await source.async_backup()


async def test_backup_refused_while_busy(source: ZWaveJSSource, ctrl: FakeController) -> None:
    ctrl.inclusion_state = InclusionState.INCLUDING
    with pytest.raises(RuntimeError, match="inclusion state"):
        await source.async_backup()
    ctrl.async_backup_nvm_raw.assert_not_awaited()


async def test_backup_unavailable(source: ZWaveJSSource) -> None:
    with pytest.raises(SourceUnavailable):
        await source.async_backup()


async def test_backup_timeout(source: ZWaveJSSource, ctrl: FakeController) -> None:
    async def slow():
        await asyncio.sleep(10)

    ctrl.async_backup_nvm_raw = slow
    with patch(f"{ZW_MOD}.ZWAVE_BACKUP_TIMEOUT", 0.01), pytest.raises(TimeoutError):
        await source.async_backup()


# ------------------------------------------------------------- _assert_idle


async def test_idle_ok(source: ZWaveJSSource, ctrl: FakeController) -> None:
    await source._assert_idle(ctrl)
    ctrl.async_is_any_ota_firmware_update_in_progress.assert_awaited_once()
    ctrl.async_is_firmware_update_in_progress.assert_awaited_once()


async def test_idle_smart_start_ok(source: ZWaveJSSource) -> None:
    await source._assert_idle(FakeController(inclusion_state=InclusionState.SMART_START))


async def test_idle_tolerates_missing_attributes(source: ZWaveJSSource) -> None:
    await source._assert_idle(SimpleNamespace())


@pytest.mark.parametrize("status", [ControllerStatus.UNRESPONSIVE, ControllerStatus.JAMMED])
async def test_idle_controller_not_ready(source: ZWaveJSSource, status: ControllerStatus) -> None:
    with pytest.raises(RuntimeError, match=f"Controller not ready \\(status {status.name}\\)"):
        await source._assert_idle(FakeController(status=status))


@pytest.mark.parametrize("state", [InclusionState.INCLUDING, InclusionState.EXCLUDING, InclusionState.BUSY])
async def test_idle_inclusion_busy(source: ZWaveJSSource, state: InclusionState) -> None:
    with pytest.raises(RuntimeError, match=f"inclusion state {state.name}"):
        await source._assert_idle(FakeController(inclusion_state=state))


async def test_idle_route_rebuild(source: ZWaveJSSource) -> None:
    with pytest.raises(RuntimeError, match="Route rebuild in progress"):
        await source._assert_idle(FakeController(is_rebuilding_routes=True))


async def test_idle_firmware_query_errors_ignored(source: ZWaveJSSource) -> None:
    """Old servers lack the firmware-update commands; a failing query must not block."""
    ctrl = FakeController(
        async_is_any_ota_firmware_update_in_progress=AsyncMock(side_effect=Exception("schema too old")),
        async_is_firmware_update_in_progress=AsyncMock(side_effect=Exception("schema too old")),
    )
    await source._assert_idle(ctrl)


@pytest.mark.parametrize(
    "check", ["async_is_any_ota_firmware_update_in_progress", "async_is_firmware_update_in_progress"]
)
async def test_idle_firmware_update_in_progress(source: ZWaveJSSource, check: str) -> None:
    ctrl = FakeController(**{check: AsyncMock(return_value=True)})
    with pytest.raises(RuntimeError, match="Firmware update in progress"):
        await source._assert_idle(ctrl)


# ------------------------------------------------------------- restore check


async def test_check_empty_file(source: ZWaveJSSource, entry: MockConfigEntry) -> None:
    check = await source.async_restore_check(b"", backup_meta())
    assert check.ok is False
    assert check.reasons == ["Backup file is empty"]


async def test_check_not_loaded(source: ZWaveJSSource) -> None:
    meta = backup_meta()
    check = await source.async_restore_check(NVM, meta)
    assert check.ok is False
    assert "Z-Wave JS is not loaded" in _reasons(check)
    assert check.backup is meta


async def test_check_controller_unreadable(hass: HomeAssistant, source: ZWaveJSSource) -> None:
    add_zwave_entry(hass, runtime=SimpleNamespace(client=SimpleNamespace(driver=None)))
    check = await source.async_restore_check(NVM, backup_meta())
    assert check.ok is False
    assert "Could not read the attached controller" in _reasons(check)


async def test_check_clean_restore_ok(source: ZWaveJSSource, ctrl: FakeController) -> None:
    meta = backup_meta()
    check = await source.async_restore_check(NVM, meta)
    assert check.ok is True, check.reasons
    assert check.backup is meta
    assert check.current["home_id"] == HOME_ID
    assert "radio is offline during the restore" in _warnings(check)
    assert "keys" not in _warnings(check)  # matching keys: no key warning
    assert "recommended" not in _warnings(check)


async def test_check_blocks_same_home_id(source: ZWaveJSSource, ctrl: FakeController) -> None:
    check = await source.async_restore_check(NVM, backup_meta(home_id=HOME_ID))
    assert check.ok is False
    assert "already has home ID 0xE1A2B3C4" in _reasons(check)


async def test_check_missing_backup_home_id_not_blocked(source: ZWaveJSSource, ctrl: FakeController) -> None:
    meta = backup_meta()
    del meta["home_id"]
    check = await source.async_restore_check(NVM, meta)
    assert check.ok is True, check.reasons


@pytest.mark.parametrize(
    ("bak_sdk", "cur_sdk", "ok", "reason", "warning"),
    [
        # 500-series backups convert from 6.61+ only
        ("6.60", "7.23.2", False, "converts 500-series NVMs from 6.61+ only", None),
        ("6.61", "7.23.2", True, None, None),
        ("6.81.06", "7.23.2", True, None, None),
        # 500-series target must be 6.61+
        ("7.23.2", "6.60", False, "must be 6.61+ to receive", "Long Range nodes"),
        # 700/800 target: < 7.19.0 blocks, < 7.23.2 warns
        ("7.23.2", "7.18.4", False, "needs 7.19.0+", None),
        ("7.23.2", "7.19.0", True, None, "7.23.2+ is recommended"),
        ("7.23.2", "7.21.3", True, None, "7.23.2+ is recommended"),
        ("7.23.2", "7.23.2", True, None, None),
        ("6.81.06", "8.0.1", True, None, None),
        # 700 -> 500 downgrade drops LR data
        ("7.23.2", "6.81.06", True, None, "Long Range nodes and 700-only data are dropped"),
        # unparseable versions are skipped, not fatal
        ("garbage", "7.23.2", True, None, None),
    ],
)
async def test_check_sdk_rules(
    source: ZWaveJSSource,
    ctrl: FakeController,
    bak_sdk: str,
    cur_sdk: str,
    ok: bool,
    reason: str | None,
    warning: str | None,
) -> None:
    ctrl.sdk_version = cur_sdk
    check = await source.async_restore_check(NVM, backup_meta(sdk_version=bak_sdk))
    assert check.ok is ok, check.reasons
    if reason:
        assert reason in _reasons(check)
    if warning:
        assert warning in _warnings(check)
    else:
        assert "recommended" not in _warnings(check)
        assert "Long Range" not in _warnings(check)


async def test_check_existing_nodes_warn(source: ZWaveJSSource, ctrl: FakeController) -> None:
    ctrl.nodes = {i: object() for i in range(1, 6)}
    check = await source.async_restore_check(NVM, backup_meta())
    assert check.ok is True
    assert "already has 5 nodes; they will be wiped" in _warnings(check)


async def test_check_single_node_no_wipe_warning(source: ZWaveJSSource, ctrl: FakeController) -> None:
    check = await source.async_restore_check(NVM, backup_meta())
    assert "will be wiped" not in _warnings(check)


async def test_check_key_mismatch_blocks(source: ZWaveJSSource, ctrl: FakeController) -> None:
    keys = dict(KEYS, s2_authenticated_key="0" * 32)
    check = await source.async_restore_check(NVM, backup_meta(security_keys=keys))
    assert check.ok is False
    assert "S0/S2 keys configured in Z-Wave JS differ" in _reasons(check)


async def test_check_key_subset_mismatch_blocks(source: ZWaveJSSource, ctrl: FakeController) -> None:
    """Backup has fewer keys than the entry: dict inequality still blocks."""
    check = await source.async_restore_check(NVM, backup_meta(security_keys={"s0_legacy_key": "A" * 32}))
    assert check.ok is False
    assert "differ" in _reasons(check)


async def test_check_backup_keys_but_none_configured_warns(
    hass: HomeAssistant, source: ZWaveJSSource
) -> None:
    add_zwave_entry(hass, data={"url": "ws://zwave:3000", "use_addon": False})
    check = await source.async_restore_check(NVM, backup_meta())
    assert check.ok is True, check.reasons
    assert "none are visible in the Z-Wave JS entry" in _warnings(check)


async def test_check_backup_without_keys_warns(source: ZWaveJSSource, ctrl: FakeController) -> None:
    meta = backup_meta()
    del meta["security_keys"]
    check = await source.async_restore_check(NVM, meta)
    assert check.ok is True
    assert "This backup has no S0/S2 keys" in _warnings(check)


@pytest.mark.parametrize(
    ("attr", "value", "reason"),
    [
        ("status", ControllerStatus.UNRESPONSIVE, "Controller not ready"),
        ("inclusion_state", InclusionState.EXCLUDING, "inclusion state EXCLUDING"),
        ("is_rebuilding_routes", True, "Route rebuild in progress"),
    ],
)
async def test_check_busy_controller_blocks(
    source: ZWaveJSSource, ctrl: FakeController, attr: str, value: Any, reason: str
) -> None:
    setattr(ctrl, attr, value)
    check = await source.async_restore_check(NVM, backup_meta())
    assert check.ok is False
    assert reason in _reasons(check)


async def test_check_firmware_update_blocks(source: ZWaveJSSource, ctrl: FakeController) -> None:
    ctrl.async_is_firmware_update_in_progress = AsyncMock(return_value=True)
    check = await source.async_restore_check(NVM, backup_meta())
    assert check.ok is False
    assert "Firmware update in progress" in _reasons(check)


# ------------------------------------------------------------------- restore


@pytest.fixture
def zw_helpers(hass: HomeAssistant):
    """Patch the zwave_js helpers the source imports lazily, recording call order."""
    order = MagicMock()
    waiter = AsyncMock(return_value=None)
    subscribe = MagicMock(return_value=waiter)
    version = AsyncMock(return_value=SimpleNamespace(home_id=OTHER_HOME_ID))
    reload = AsyncMock(return_value=True)
    order.attach_mock(subscribe, "subscribe")
    order.attach_mock(waiter, "wait")
    order.attach_mock(version, "version")
    order.attach_mock(reload, "reload")
    with (
        patch(f"{HELPERS}.async_wait_for_driver_ready_event", subscribe),
        patch(f"{HELPERS}.async_get_version_info", version),
        patch.object(hass.config_entries, "async_reload", reload),
    ):
        yield SimpleNamespace(order=order, subscribe=subscribe, wait=waiter, version=version, reload=reload)


async def test_restore_happy_path(
    hass: HomeAssistant, source: ZWaveJSSource, entry: MockConfigEntry, ctrl: FakeController, zw_helpers
) -> None:
    zw_helpers.order.attach_mock(ctrl.async_restore_nvm, "restore_nvm")

    await source.async_restore(NVM, backup_meta())

    driver = entry.runtime_data.client.driver
    zw_helpers.subscribe.assert_called_once_with(entry, driver)
    ctrl.async_restore_nvm.assert_awaited_once_with(NVM, {"preserveRoutes": False})
    zw_helpers.wait.assert_awaited_once()
    zw_helpers.version.assert_awaited_once_with(hass, "ws://localhost:3000")
    assert entry.unique_id == str(OTHER_HOME_ID)
    zw_helpers.reload.assert_awaited_once_with(entry.entry_id)
    names = [c[0] for c in zw_helpers.order.mock_calls]
    assert names == ["subscribe", "restore_nvm", "wait", "version", "reload"]


@pytest.mark.parametrize(
    ("bak_sdk", "cur_sdk"),
    [("6.81.06", "7.23.2"), ("7.23.2", "6.81.06"), (None, "7.23.2")],
)
async def test_restore_cross_series_drops_neighbors(
    source: ZWaveJSSource, ctrl: FakeController, zw_helpers, bak_sdk: str | None, cur_sdk: str
) -> None:
    ctrl.sdk_version = cur_sdk
    await source.async_restore(NVM, backup_meta(sdk_version=bak_sdk))
    ctrl.async_restore_nvm.assert_awaited_once_with(
        NVM, {"preserveRoutes": False, "preserveNeighbors": False}
    )


async def test_restore_700_to_800_same_series(
    source: ZWaveJSSource, ctrl: FakeController, zw_helpers
) -> None:
    ctrl.sdk_version = "8.0.1"
    await source.async_restore(NVM, backup_meta(sdk_version="7.21.3"))
    ctrl.async_restore_nvm.assert_awaited_once_with(NVM, {"preserveRoutes": False})


async def test_restore_driver_ready_timeout_tolerated(
    source: ZWaveJSSource, entry: MockConfigEntry, zw_helpers
) -> None:
    zw_helpers.wait.side_effect = TimeoutError
    await source.async_restore(NVM, backup_meta())
    zw_helpers.version.assert_awaited_once()
    assert entry.unique_id == str(OTHER_HOME_ID)
    zw_helpers.reload.assert_awaited_once()


async def test_restore_version_unreachable_still_reloads(
    source: ZWaveJSSource, entry: MockConfigEntry, zw_helpers, caplog: pytest.LogCaptureFixture
) -> None:
    from homeassistant.components.zwave_js.helpers import CannotConnect

    zw_helpers.version.side_effect = CannotConnect
    await source.async_restore(NVM, backup_meta())
    assert entry.unique_id == str(HOME_ID)  # unchanged
    zw_helpers.reload.assert_awaited_once_with(entry.entry_id)
    assert "Could not read the new home ID" in caplog.text


async def test_restore_refused_while_busy(source: ZWaveJSSource, ctrl: FakeController, zw_helpers) -> None:
    ctrl.status = ControllerStatus.JAMMED
    with pytest.raises(RuntimeError, match="Controller not ready"):
        await source.async_restore(NVM, backup_meta())
    zw_helpers.subscribe.assert_not_called()
    ctrl.async_restore_nvm.assert_not_awaited()
    zw_helpers.reload.assert_not_awaited()


async def test_restore_not_loaded(source: ZWaveJSSource, zw_helpers) -> None:
    with pytest.raises(SourceUnavailable, match="not loaded"):
        await source.async_restore(NVM, backup_meta())


async def test_restore_driver_missing(hass: HomeAssistant, source: ZWaveJSSource, zw_helpers) -> None:
    add_zwave_entry(hass, runtime=SimpleNamespace(client=SimpleNamespace(driver=None)))
    with pytest.raises(SourceUnavailable, match="driver not ready"):
        await source.async_restore(NVM, backup_meta())


async def test_restore_nvm_failure_does_not_rewrite_entry(
    source: ZWaveJSSource, entry: MockConfigEntry, ctrl: FakeController, zw_helpers
) -> None:
    ctrl.async_restore_nvm.side_effect = RuntimeError("NVM write failed")
    with pytest.raises(RuntimeError, match="NVM write failed"):
        await source.async_restore(NVM, backup_meta())
    zw_helpers.version.assert_not_awaited()
    assert entry.unique_id == str(HOME_ID)
    zw_helpers.reload.assert_not_awaited()


async def test_restore_timeout(source: ZWaveJSSource, ctrl: FakeController, zw_helpers) -> None:
    async def slow(*_: Any):
        await asyncio.sleep(10)

    ctrl.async_restore_nvm = slow
    with patch(f"{ZW_MOD}.RESTORE_TIMEOUT", 0.01), pytest.raises(TimeoutError):
        await source.async_restore(NVM, backup_meta())
    zw_helpers.reload.assert_not_awaited()


async def test_restore_pinned_controller(hass: HomeAssistant, zw_helpers) -> None:
    add_zwave_entry(hass, unique_id="111")
    target = add_zwave_entry(hass, unique_id="222")
    await ZWaveJSSource(hass, home_id=222).async_restore(NVM, backup_meta())
    ctrl_of(target).async_restore_nvm.assert_awaited_once()
    assert target.unique_id == str(OTHER_HOME_ID)
    zw_helpers.reload.assert_awaited_once_with(target.entry_id)
