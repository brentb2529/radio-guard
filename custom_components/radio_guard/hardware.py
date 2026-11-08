"""Find coordinator hardware the user just plugged in, so they never have to type a port.

No private hardware list: Home Assistant's own ZHA and Z-Wave JS manifests already carry the USB
IDs (with friendly ``known_devices`` names) and the mDNS service types of supported sticks and
network adapters. Radio Guard reuses them:

* USB: HA's serial-port scan, each port classified as Zigbee or Z-Wave with HA's own matcher.
* Network: a short zeroconf browse (through HA's shared instance) for the Zigbee adapter service
  types in the ZHA manifest (SLZB-06, ZigStar, UZG-01, XZG, ...).

The wizard shows the right technology only, names devices in plain words and pre-selects the one
that just appeared.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

ZIGBEE = "zigbee"
ZWAVE = "zwave"
TECH_INTEGRATION = {ZIGBEE: "zha", ZWAVE: "zwave_js"}
BROWSE_SECONDS = 3.0


@dataclass
class Candidate:
    """A place the new coordinator can be reached."""

    value: str  # what goes into the owning software's port setting
    label: str
    tech: str | None  # zigbee / zwave / None (unrecognised)
    network: bool = False


async def _manifest(hass: HomeAssistant, domain: str) -> dict[str, Any]:
    from homeassistant.loader import async_get_integration  # noqa: PLC0415

    try:
        integration = await async_get_integration(hass, domain)
    except Exception:  # noqa: BLE001
        return {}
    return integration.manifest


_ROMAN = {"i", "ii", "iii", "iv", "v"}


def _title(text: str) -> str:
    """'dresden elektronik conbee ii' -> 'Dresden Elektronik Conbee II'; leaves 'ZBT-1', 'Gen5+' alone."""

    def word(w: str) -> str:
        if w.lower() in _ROMAN:
            return w.upper()
        if w != w.lower() or any(c.isdigit() for c in w):
            return w
        return w.capitalize()

    return " ".join(word(w) for w in text.split())


def _classify_usb(dev, matchers: dict[str, list[dict[str, Any]]]) -> tuple[str | None, str | None]:
    """(tech, friendly name) from HA's manifest matchers. Description-specific matches win."""
    from homeassistant.components.usb.utils import usb_device_matches_matcher  # noqa: PLC0415

    if getattr(dev, "vid", None) is None:
        return None, None  # a plain serial port (HA 2026.10+ SerialDevice): nothing to match on
    best: tuple[int, str | None, str | None] = (-1, None, None)
    for tech, items in matchers.items():
        for m in items:
            usb_m = {
                k: v
                for k, v in m.items()
                if k in ("vid", "pid", "serial_number", "manufacturer", "description")
            }
            if not usb_device_matches_matcher(dev, usb_m):
                continue
            score = len(usb_m)  # a vid/pid+description match beats a bare vid/pid one
            if score > best[0]:
                names = m.get("known_devices") or []
                best = (score, tech, _title(names[0]) if names else None)
    return best[1], best[2]


_MATCHERS: dict[str, list[dict[str, Any]]] = {}


def serial_ports() -> list[tuple[str, str] | tuple[str, str, str | None]]:
    """(path, label, tech) for every serial port HA can see; tech from HA's manifest matchers."""
    try:
        from homeassistant.components.usb import get_serial_by_id, human_readable_device_name  # noqa: PLC0415
        from homeassistant.components.usb.utils import scan_serial_ports  # noqa: PLC0415
    except ImportError:
        return []
    out: list[tuple[str, str, str | None]] = []
    for dev in scan_serial_ports():
        path = get_serial_by_id(dev.device)
        tech, known = _classify_usb(dev, _MATCHERS)
        name = known or human_readable_device_name(
            path,
            getattr(dev, "serial_number", None),
            getattr(dev, "manufacturer", None),
            getattr(dev, "description", None),
            getattr(dev, "vid", None),
            getattr(dev, "pid", None),
        )
        out.append((path, name, tech))
    return out


async def _browse_network(hass: HomeAssistant, zeroconf_types: list[dict[str, Any] | str]) -> list[Candidate]:
    """Briefly browse the LAN for network Zigbee adapters announced via mDNS."""
    if not zeroconf_types:
        return []
    try:
        from homeassistant.components.zeroconf import async_get_async_instance  # noqa: PLC0415
        from zeroconf import ServiceStateChange  # noqa: PLC0415
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo  # noqa: PLC0415
    except ImportError:
        return []
    patterns: dict[str, str] = {}
    for item in zeroconf_types:
        if isinstance(item, str):
            patterns[item] = "*"
        elif item.get("type") and item["type"] != "_esphomelib._tcp.local.":  # ESPHome: too broad
            patterns[item["type"]] = item.get("name", "*")
    try:
        aiozc = await async_get_async_instance(hass)
    except Exception:  # noqa: BLE001
        return []
    found: dict[str, str] = {}

    def _on_change(zeroconf, service_type, name, state_change) -> None:
        if state_change is ServiceStateChange.Added and fnmatch.fnmatch(
            name.split(".")[0].lower(), patterns.get(service_type, "*").lower()
        ):
            found[name] = service_type

    browser = AsyncServiceBrowser(aiozc.zeroconf, list(patterns), handlers=[_on_change])
    try:
        await asyncio.sleep(BROWSE_SECONDS)
    finally:
        await browser.async_cancel()
    out: list[Candidate] = []
    for name, service_type in found.items():
        info = AsyncServiceInfo(service_type, name)
        if not await info.async_request(aiozc.zeroconf, 2000):
            continue
        addresses = info.parsed_addresses()
        if not addresses or not info.port:
            continue
        host, port = addresses[0], info.port
        if service_type == "_zigbee-coordinator._tcp.local." and info.properties:
            port = int((info.properties.get(b"serial_port") or str(port).encode()).decode() or port)
        label = name.split(".")[0]
        out.append(
            Candidate(value=f"tcp://{host}:{port}", label=f"{label} at {host}", tech=ZIGBEE, network=True)
        )
    return out


async def async_find(hass: HomeAssistant, tech: str, *, network: bool = True) -> list[Candidate]:
    """Every place a coordinator of ``tech`` could be right now, plus unrecognised serial ports."""
    manifests = {t: await _manifest(hass, d) for t, d in TECH_INTEGRATION.items()}
    _MATCHERS.update({t: m.get("usb") or [] for t, m in manifests.items()})
    usb = [
        Candidate(value=row[0], label=row[1], tech=row[2] if len(row) > 2 else None)
        for row in await hass.async_add_executor_job(serial_ports)
    ]
    candidates = [c for c in usb if c.tech in (tech, None)]
    if network and tech == ZIGBEE:
        try:
            candidates += await _browse_network(hass, manifests[ZIGBEE].get("zeroconf") or [])
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Network coordinator discovery failed", exc_info=True)
    return candidates
