"""Coordinator hardware discovery: classification via HA's own manifest matchers."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.components.usb.models import USBDevice
from homeassistant.core import HomeAssistant

from custom_components.radio_guard import hardware
from custom_components.radio_guard.backup.base import BackupSource
from custom_components.radio_guard.hardware import ZIGBEE, ZWAVE, Candidate, _classify_usb

MATCHERS = {
    ZIGBEE: [
        {
            "vid": "10C4",
            "pid": "EA60",
            "description": "*sonoff*plus*",
            "known_devices": ["sonoff zbdongle-p"],
        },
        {"vid": "1CF1", "pid": "0030", "known_devices": ["dresden elektronik conbee ii"]},
    ],
    ZWAVE: [
        {"vid": "0658", "pid": "0200", "known_devices": ["Aeotec Z-Stick Gen5+", "Z-WaveMe UZB"]},
        {"vid": "10C4", "pid": "8A2A", "description": "*z-wave*", "known_devices": ["Nortek HUSBZB-1"]},
    ],
}


def _dev(vid: str, pid: str, description: str | None = None) -> USBDevice:
    return USBDevice(
        device="/dev/ttyUSB0",
        vid=vid,
        pid=pid,
        serial_number=None,
        manufacturer=None,
        description=description,
    )


def test_classify_known_zigbee_and_zwave() -> None:
    assert _classify_usb(_dev("1CF1", "0030"), MATCHERS) == (ZIGBEE, "Dresden Elektronik Conbee II")
    assert _classify_usb(_dev("0658", "0200"), MATCHERS) == (ZWAVE, "Aeotec Z-Stick Gen5+")


def test_classify_uses_description_for_shared_usb_chips() -> None:
    sonoff = _dev("10C4", "EA60", "Sonoff Zigbee 3.0 USB Dongle Plus")
    assert _classify_usb(sonoff, MATCHERS)[0] == ZIGBEE
    husbzb = _dev("10C4", "8A2A", "HubZ Z-Wave Com Port")
    assert _classify_usb(husbzb, MATCHERS)[0] == ZWAVE
    generic = _dev("10C4", "EA60", "CP2102 USB to UART Bridge Controller")
    assert _classify_usb(generic, MATCHERS) == (None, None)


def test_classify_plain_serial_port_without_usb_ids() -> None:
    """HA 2026.10+ also returns SerialDevice entries (no vid/pid) for built-in UARTs and VM ports."""

    class SerialDevice:  # shape of homeassistant.components.usb.models.SerialDevice
        device = "/dev/ttyAMA0"
        serial_number = None
        manufacturer = None
        description = "ttyAMA0"

    assert _classify_usb(SerialDevice(), MATCHERS) == (None, None)


async def test_find_filters_by_technology(hass: HomeAssistant) -> None:
    rows = [
        ("/dev/serial/by-id/zigbee", "Sonoff ZBDongle-P", ZIGBEE),
        ("/dev/serial/by-id/zwave", "Aeotec Z-Stick", ZWAVE),
        ("/dev/serial/by-id/unknown", "CP2102", None),
    ]
    with (
        patch.object(hardware, "serial_ports", return_value=rows),
        patch.object(hardware, "_browse_network", AsyncMock(return_value=[])),
    ):
        zigbee = await hardware.async_find(hass, ZIGBEE)
        zwave = await hardware.async_find(hass, ZWAVE)
    assert [c.value for c in zigbee] == ["/dev/serial/by-id/zigbee", "/dev/serial/by-id/unknown"]
    assert [c.value for c in zwave] == ["/dev/serial/by-id/zwave", "/dev/serial/by-id/unknown"]


async def test_find_adds_network_adapters_for_zigbee_only(hass: HomeAssistant) -> None:
    net = [Candidate("tcp://192.168.1.50:6638", "slzb-06 at 192.168.1.50", ZIGBEE, network=True)]
    with (
        patch.object(hardware, "serial_ports", return_value=[]),
        patch.object(hardware, "_browse_network", AsyncMock(return_value=net)) as browse,
    ):
        assert [c.value for c in await hardware.async_find(hass, ZIGBEE)] == ["tcp://192.168.1.50:6638"]
        assert await hardware.async_find(hass, ZWAVE) == []
    browse.assert_awaited_once()


class _Src(BackupSource):
    id = "x"
    name = "X"
    tech = ZIGBEE

    def available(self):  # pragma: no cover - unused
        return True

    async def async_identity(self):  # pragma: no cover
        return {}

    async def async_backup(self):  # pragma: no cover
        raise NotImplementedError

    async def async_restore_check(self, data, meta):  # pragma: no cover
        raise NotImplementedError

    async def async_restore(self, data, meta):  # pragma: no cover
        raise NotImplementedError


async def test_swap_ports_marks_new_network_and_unrecognised(hass: HomeAssistant) -> None:
    src = _Src(hass)
    before = [Candidate("/dev/old", "Old stick", ZIGBEE)]
    after = [
        Candidate("/dev/old", "Old stick", ZIGBEE),
        Candidate("/dev/new", "Sonoff ZBDongle-E", ZIGBEE),
        Candidate("/dev/odd", "CP2102", None),
        Candidate("tcp://192.168.1.50:6638", "slzb-06 at 192.168.1.50", ZIGBEE, network=True),
    ]
    with patch.object(hardware, "async_find", AsyncMock(side_effect=[before, after])):
        await src.async_snapshot_ports()
        ports = await src.async_swap_ports(current="/dev/old")
    by_value = {p.value: p for p in ports}
    assert by_value["/dev/new"].new and not by_value["/dev/old"].new
    assert by_value["/dev/odd"].label == "CP2102 (unrecognised device)"
    assert by_value["tcp://192.168.1.50:6638"].label.endswith("(found on your network)")
    assert all(p.new for p in ports[:3]) and ports[-1].value == "/dev/old"
