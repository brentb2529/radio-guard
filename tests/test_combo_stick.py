"""Two-in-one sticks: one USB device carrying a Zigbee (ZHA) and a Z-Wave (Z-Wave JS) radio.

Production runs an Aeotec Z-Stick 10 Pro like this. When it dies both networks go down together;
the user should see one Repair, swap the stick once, and get both networks back with the right
port of the new stick picked for each radio.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.radio_guard import hardware
from custom_components.radio_guard.backup.base import BackupSource, PortOption, SwapPlan
from custom_components.radio_guard.const import (
    CONF_SOURCES,
    DEFAULT_OFFLINE_MINUTES,
    DOMAIN,
    ISSUE_COORDINATOR_DOWN,
    SOURCE_ZHA,
    SOURCE_ZWAVE,
    STARTUP_GRACE_MINUTES,
)
from custom_components.radio_guard.coordinator import RadioGuardCoordinator

from .test_wizard import SwapFake, drive, issue, refresh, schema_field, seed_backup, setup_rg, step

STICK = "/dev/serial/by-id/usb-Silicon_Labs_CP2105_Dual_USB_to_UART_Bridge_Controller"
OLD_ZB, OLD_ZW = f"{STICK}_00D9B4EA-if00-port0", f"{STICK}_00D9B4EA-if01-port0"
NEW_ZB, NEW_ZW = f"{STICK}_11AA22BB-if00-port0", f"{STICK}_11AA22BB-if01-port0"
DOWN_ZHA = f"{ISSUE_COORDINATOR_DOWN}_{SOURCE_ZHA}"
DOWN_ZW = f"{ISSUE_COORDINATOR_DOWN}_{SOURCE_ZWAVE}"
ZW_HOME = "c0ffee01"


@pytest.fixture(autouse=True)
def _no_min_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("custom_components.radio_guard.wizard.MIN_PROGRESS_SECONDS", 0)


@pytest.fixture(autouse=True)
def _by_id_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hardware, "by_id", lambda path: path)


# ------------------------------------------------------------------ hardware helpers


def test_usb_parts_and_same_device() -> None:
    assert hardware.usb_parts(OLD_ZW) == (f"{STICK}_00D9B4EA", STICK, "01")
    assert hardware.usb_parts("/dev/ttyUSB0") is None
    assert hardware.usb_parts("tcp://10.0.0.5:6638") is None
    assert hardware.usb_parts(None) is None
    assert hardware.same_device(OLD_ZB, OLD_ZW)
    assert not hardware.same_device(OLD_ZB, OLD_ZB)  # one radio is not two
    assert not hardware.same_device(OLD_ZB, NEW_ZW)  # different stick
    assert not hardware.same_device("/dev/ttyUSB0", "/dev/ttyUSB1")  # unknowable: never merged


def test_match_score() -> None:
    assert hardware.match_score(OLD_ZW, NEW_ZW) == 2  # same model, same interface
    assert hardware.match_score(OLD_ZW, NEW_ZB) == 1  # same model, other radio
    assert hardware.match_score(OLD_ZW, "/dev/serial/by-id/usb-Nabu_Casa_ZBT-1_9e2a-if00-port0") == 0
    assert hardware.match_score(None, NEW_ZW) == 0
    single = "/dev/serial/by-id/usb-ITead_Sonoff_Zigbee_3.0_USB_Dongle_Plus_V2_20230601-if00-port0"
    assert hardware.usb_parts(single)[1].endswith("Dongle_Plus_V2")


class _PortsSource(BackupSource):
    """Only the base-class port logic; hardware discovery patched."""

    id = "x"
    name = "X"
    tech = "zwave"

    def available(self) -> bool:  # pragma: no cover - unused
        return True

    async def async_identity(self) -> dict[str, Any]:  # pragma: no cover
        return {}

    async def async_backup(self):  # pragma: no cover
        raise NotImplementedError

    async def async_restore_check(self, data, meta):  # pragma: no cover
        raise NotImplementedError

    async def async_restore(self, data, meta):  # pragma: no cover
        raise NotImplementedError


@pytest.mark.parametrize(("old", "expected"), [(OLD_ZW, NEW_ZW), (OLD_ZB, NEW_ZB)])
async def test_new_stick_ports_follow_the_old_interface(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, old: str, expected: str
) -> None:
    """HA doesn't know the stick's USB ID, so both new ports are 'unrecognised': the interface decides."""
    found = [
        hardware.Candidate("/dev/ttyAMA0", "ttyAMA0", None),
        hardware.Candidate(NEW_ZB, "CP2105 Dual USB to UART Bridge Controller", None),
        hardware.Candidate(NEW_ZW, "CP2105 Dual USB to UART Bridge Controller", None),
    ]

    async def find(hass, tech, network=True):
        return found

    monkeypatch.setattr(hardware, "async_find", find)
    src = _PortsSource(hass)
    src.swap_reset()
    src._ports_before = {"/dev/ttyAMA0"}
    ports = await src.async_swap_ports(old)
    assert [p.new for p in ports] == [True, True, False]
    assert ports[0].value == expected


# ------------------------------------------------------------------ fakes and setup


class ZhaFake(SwapFake):
    id = SOURCE_ZHA
    name = "ZHA"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass)
        self.plan = SwapPlan(supported=True, automatic=True, choose_port=True, current_port=OLD_ZB)
        self.port = OLD_ZB
        # The flow lists what the base class would: matching interface first.
        self.ports = [
            PortOption(NEW_ZB, "CP2105 (just plugged in)", new=True),
            PortOption(NEW_ZW, "CP2105", new=True),
        ]


class ZwaveFake(SwapFake):
    id = SOURCE_ZWAVE
    name = "Z-Wave JS"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass)
        self.identity = {"home_id": 3237998081, "home_id_hex": ZW_HOME, "device_count": 7}
        self.plan = SwapPlan(supported=True, automatic=True, choose_port=True, current_port=OLD_ZW)
        self.port = OLD_ZW
        self.ports = [PortOption(NEW_ZW, "CP2105", new=True), PortOption(NEW_ZB, "CP2105", new=True)]

    async def async_swap_verify(self, data: bytes, meta: dict[str, Any]) -> dict[str, Any]:
        await self._gate("verify")
        return dict(self.identity)


@pytest.fixture
def entry_data(entry_data: dict[str, Any]) -> dict[str, Any]:
    return {**entry_data, CONF_SOURCES: [SOURCE_ZHA, SOURCE_ZWAVE]}


@pytest.fixture
def fakes(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> tuple[ZhaFake, ZwaveFake]:
    zha, zw = ZhaFake(hass), ZwaveFake(hass)
    monkeypatch.setattr("custom_components.radio_guard.coordinator.ZHASource", lambda hass: zha)
    monkeypatch.setattr("custom_components.radio_guard.coordinator.ZWaveJSSource", lambda hass: zw)
    return zha, zw


@pytest.fixture
def seeded(config_dir: Path) -> dict[str, str]:
    root = config_dir / "radio_guard"
    return {
        SOURCE_ZHA: seed_backup(
            root / SOURCE_ZHA, timedelta(hours=3), {"source": SOURCE_ZHA, "ieee": "00124b0000000001"}
        ),
        SOURCE_ZWAVE: seed_backup(
            root / SOURCE_ZWAVE, timedelta(hours=3), {"source": SOURCE_ZWAVE, "home_id_hex": ZW_HOME}
        ),
    }


async def stick_dies(
    hass: HomeAssistant, freezer, coord: RadioGuardCoordinator, fakes, *, which=(True, True)
) -> None:
    for fake, dead in zip(fakes, which, strict=True):
        fake.health = False if dead else True
    freezer.tick(timedelta(minutes=STARTUP_GRACE_MINUTES))
    await refresh(hass, coord)
    freezer.tick(timedelta(minutes=DEFAULT_OFFLINE_MINUTES))
    await refresh(hass, coord)


# ------------------------------------------------------------------ one Repair


async def test_dead_two_in_one_stick_raises_one_repair(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry, fakes, seeded
) -> None:
    coord = await setup_rg(hass, mock_config_entry)
    await stick_dies(hass, freezer, coord, fakes)
    assert coord.coordinator_down(coord.states[SOURCE_ZWAVE])  # both really are down...
    zha_issue = issue(hass, DOWN_ZHA)
    assert zha_issue is not None  # ...but the user sees one Repair, naming both
    assert zha_issue.translation_placeholders["source"] == "ZHA and Z-Wave JS"
    assert issue(hass, DOWN_ZW) is None


async def test_one_radio_down_keeps_its_own_repair(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry, fakes, seeded
) -> None:
    coord = await setup_rg(hass, mock_config_entry)
    await stick_dies(hass, freezer, coord, fakes, which=(False, True))
    assert issue(hass, DOWN_ZHA) is None
    assert issue(hass, DOWN_ZW).translation_placeholders["source"] == "Z-Wave JS"


async def test_separate_sticks_are_not_merged(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry, fakes, seeded
) -> None:
    zha, zw = fakes
    zw.plan = SwapPlan(
        supported=True, automatic=True, choose_port=True, current_port="/dev/serial/by-id/usb-zooz-if00"
    )
    coord = await setup_rg(hass, mock_config_entry)
    await stick_dies(hass, freezer, coord, fakes)
    assert issue(hass, DOWN_ZHA).translation_placeholders["source"] == "ZHA"
    assert issue(hass, DOWN_ZW) is not None


# ------------------------------------------------------------------ the wizard


async def open_replace(hass: HomeAssistant) -> tuple[Any, dict[str, Any]]:
    mgr = repairs_flow_manager(hass)
    result = await mgr.async_init(DOMAIN, data={"issue_id": DOWN_ZHA})
    assert result["type"] is FlowResultType.MENU
    result = await mgr.async_configure(result["flow_id"], {"next_step_id": "wiz_pick"})
    assert result["step_id"] == "wiz_pick"
    return mgr, result


async def test_replace_two_in_one_stick_end_to_end(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry, fakes, seeded
) -> None:
    zha, zw = fakes
    coord = await setup_rg(hass, mock_config_entry)
    await stick_dies(hass, freezer, coord, fakes)
    mgr, result = await open_replace(hass)
    seen: list = []

    # One stop step stops both radios' software; the dead Z-Wave side uses its stored backup.
    result = await mgr.async_configure(result["flow_id"], {"file": seeded[SOURCE_ZHA]})
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["description_placeholders"]["source"] == "ZHA and Z-Wave JS"
    result = await drive(hass, mgr, result, seen)
    assert seen == [("wiz_stop", "stopping")]
    assert "stop" in zha.calls and "stop" in zw.calls
    assert zw.backup_calls == 0  # its radio is dead: no fresh backup attempted
    assert coord.states[SOURCE_ZWAVE].swapping

    # One swap form, both new ports pre-selected.
    assert result["step_id"] == "wiz_swap"
    assert "two radios" in result["description_placeholders"]["intro"]
    port_key, _ = schema_field(result, "port")
    comp_key, _ = schema_field(result, "companion_port")
    assert port_key.description["suggested_value"] == NEW_ZB
    assert comp_key.description["suggested_value"] == NEW_ZW

    # Same port for both radios is refused.
    bad = await step(
        hass, mgr, result["flow_id"], {"port": NEW_ZB, "companion_port": NEW_ZB, "unplugged": True}
    )
    assert bad["errors"] == {"companion_port": "same_port"}

    result = await step(
        hass, mgr, result["flow_id"], {"port": NEW_ZB, "companion_port": NEW_ZW, "unplugged": True}
    )
    assert result["step_id"] == "wiz_confirm"
    result = await step(hass, mgr, result["flow_id"], {}, seen)

    # ZHA is back; the menu leads on to the second radio (no Finish yet, nothing to unplug).
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "wiz_done_next"
    assert result["menu_options"] == ["wiz_next", "wiz_devices"]
    assert result["description_placeholders"]["next"] == "Z-Wave JS"
    assert zha.applied[-1][2] == NEW_ZB
    assert zw.applied == []
    assert not coord.states[SOURCE_ZHA].swapping
    assert coord.states[SOURCE_ZWAVE].swapping  # still guarded while it waits its turn

    # Second radio: straight to its summary, then restore on the matching port.
    result = await mgr.async_configure(result["flow_id"], {"next_step_id": "wiz_next"})
    assert result["step_id"] == "wiz_confirm"
    assert result["description_placeholders"]["source"] == "Z-Wave JS"
    assert result["description_placeholders"]["port"] == NEW_ZW
    result = await step(hass, mgr, result["flow_id"], {}, seen)
    assert result["step_id"] == "wiz_done"
    assert result["menu_options"] == ["wiz_finish", "wiz_devices"]
    assert "**ZHA**" in result["description_placeholders"]["devices"]
    assert zw.applied[-1][2] == NEW_ZW
    assert zw.applied[-1][1]["home_id_hex"] == ZW_HOME

    result = await mgr.async_configure(result["flow_id"], {"next_step_id": "wiz_finish"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert not coord.states[SOURCE_ZHA].swapping and not coord.states[SOURCE_ZWAVE].swapping
    assert issue(hass, DOWN_ZHA) is None and issue(hass, DOWN_ZW) is None


async def test_closing_after_first_radio_restarts_the_second(
    hass: HomeAssistant, freezer, mock_config_entry: MockConfigEntry, fakes, seeded
) -> None:
    zha, zw = fakes
    coord = await setup_rg(hass, mock_config_entry)
    await stick_dies(hass, freezer, coord, fakes)
    mgr, result = await open_replace(hass)
    result = await step(hass, mgr, result["flow_id"], {"file": seeded[SOURCE_ZHA]})
    result = await step(
        hass, mgr, result["flow_id"], {"port": NEW_ZB, "companion_port": NEW_ZW, "unplugged": True}
    )
    result = await step(hass, mgr, result["flow_id"], {})
    assert result["step_id"] == "wiz_done_next"

    mgr.async_abort(result["flow_id"])  # user closes the dialog
    await hass.async_block_till_done()
    assert "start" in zw.calls  # Z-Wave JS is not left stopped
    assert not coord.states[SOURCE_ZWAVE].swapping
    assert zw.applied == []


async def test_migrate_two_in_one_stick_backs_up_both_and_rolls_back_both(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, fakes, seeded
) -> None:
    """Moving to a new stick while the old one works: fresh backups of both radios first."""
    zha, zw = fakes
    coord = await setup_rg(hass, mock_config_entry)
    await refresh(hass, coord)
    entry = mock_config_entry
    mgr = hass.config_entries.options
    result = await mgr.async_init(entry.entry_id)
    result = await mgr.async_configure(result["flow_id"], {"next_step_id": "migrate"})
    if result["step_id"] == "pick_source":
        result = await mgr.async_configure(result["flow_id"], {"source": SOURCE_ZHA})
    assert result["step_id"] == "wiz_migrate"
    result = await step(hass, mgr, result["flow_id"], {})
    assert result["step_id"] == "wiz_swap_migrate"
    assert zha.backup_calls == 1 and zw.backup_calls == 1  # Z-Wave backed up while it still answered

    # ZHA fails to verify on the new stick -> roll back: both radios return to the old stick.
    zha.verify_error = RuntimeError("network did not come up")
    result = await step(
        hass, mgr, result["flow_id"], {"port": NEW_ZB, "companion_port": NEW_ZW, "unplugged": True}
    )
    result = await step(hass, mgr, result["flow_id"], {})
    assert result["step_id"] == "wiz_failed_migrate"
    result = await step(hass, mgr, result["flow_id"], {"next_step_id": "wiz_rollback"})
    assert result["step_id"] == "wiz_rollback_swap"
    zha.verify_error = None
    result = await step(hass, mgr, result["flow_id"], {"swapped_back": True})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "rolled_back"
    assert zha.port == OLD_ZB
    assert zw.port == OLD_ZW and zw.applied == []  # never moved
    assert "start" in zw.calls
    assert not coord.states[SOURCE_ZHA].swapping and not coord.states[SOURCE_ZWAVE].swapping
