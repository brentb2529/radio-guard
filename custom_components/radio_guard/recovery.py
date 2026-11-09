"""A printable recovery sheet built from this install: what you have, where backups are, what to do.

Failure day is exactly when Home Assistant may not be there to explain itself, so the steps
have to exist on paper (or a phone) beforehand. Admins open it from Radio Guard > Configure >
Print the recovery sheet: a hidden Home Assistant page (``/radio-guard-recovery``) that fetches
the sheet from an authenticated API view with the user's own login and shows it ready to print.
"""

from __future__ import annotations

import html
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web
from homeassistant.components.http import KEY_HASS, KEY_HASS_USER, HomeAssistantView, StaticPathConfig
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SOURCE_ZHA, SOURCE_ZWAVE, VERSION
from .coordinator import device_count_of
from .ha_backup import backup_summary, coverage_problems

if TYPE_CHECKING:
    from .coordinator import RadioGuardCoordinator

_LOGGER = logging.getLogger(__name__)

URL = "/api/radio_guard/recovery_sheet"
PANEL = "radio-guard-recovery"
STATIC = "/radio_guard_static"
_BOARDS = {
    "rpi5-64": "Raspberry Pi 5",
    "rpi4-64": "Raspberry Pi 4",
    "rpi4": "Raspberry Pi 4",
    "rpi3-64": "Raspberry Pi 3",
    "green": "Home Assistant Green",
    "yellow": "Home Assistant Yellow",
    "generic-x86-64": "PC (x86-64)",
    "generic-aarch64": "ARM computer",
    "ova": "virtual machine",
    "odroid-n2": "ODROID-N2",
}


class RecoverySheetView(HomeAssistantView):
    """GET the sheet: signed-in admins only (it describes the install's hardware and backups)."""

    url = URL
    name = "api:radio_guard:recovery_sheet"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app[KEY_HASS]
        user = request.get(KEY_HASS_USER)
        if user is None or not user.is_admin:
            return web.Response(status=403, text="Only administrators can open the recovery sheet")
        entries = hass.config_entries.async_loaded_entries(DOMAIN)
        if not entries:
            return web.Response(status=404, text="Radio Guard is not loaded")
        page = await async_render(hass, entries[0].runtime_data)
        return web.Response(text=page, content_type="text/html", headers={"Cache-Control": "no-store"})


async def async_register(hass: HomeAssistant) -> None:
    """API view + static script once per HA run; the hidden panel while Radio Guard is loaded."""
    key = f"{DOMAIN}_recovery_http"
    if not hass.data.get(key):
        hass.data[key] = True
        hass.http.register_view(RecoverySheetView())
        await hass.http.async_register_static_paths(
            [StaticPathConfig(STATIC, str(Path(__file__).parent / "frontend"), False)]
        )
    # The page is a convenience: it must never keep Radio Guard itself from starting.
    if "frontend" not in hass.config.components or PANEL in hass.data.get("frontend_panels", {}):
        return
    try:
        from homeassistant.components import panel_custom  # noqa: PLC0415

        await panel_custom.async_register_panel(
            hass,
            frontend_url_path=PANEL,
            webcomponent_name=PANEL,
            module_url=f"{STATIC}/recovery-panel.js?v={VERSION}",
            require_admin=True,
        )
    except Exception:  # noqa: BLE001
        _LOGGER.warning("Could not add the recovery sheet page", exc_info=True)


def async_unregister(hass: HomeAssistant) -> None:
    from homeassistant.components import frontend  # noqa: PLC0415

    frontend.async_remove_panel(hass, PANEL, warn_if_unknown=False)


# --------------------------------------------------------------------------- facts


async def _host(hass: HomeAssistant) -> tuple[str, bool]:
    """('Raspberry Pi 5, Home Assistant OS 18.4', is HA OS)."""
    from homeassistant.helpers.system_info import async_get_system_info  # noqa: PLC0415

    try:
        info = await async_get_system_info(hass)
    except Exception:  # noqa: BLE001
        info = {}
    install = info.get("installation_type") or "Home Assistant"
    haos = install == "Home Assistant OS"
    if haos:
        try:
            from homeassistant.components.hassio import get_os_info  # noqa: PLC0415

            os_info = get_os_info(hass) or {}
        except Exception:  # noqa: BLE001
            os_info = {}
        board = _BOARDS.get(os_info.get("board") or "", os_info.get("board") or "unknown hardware")
        version = os_info.get("version")
        return f"{board}, Home Assistant OS {version}" if version else f"{board}, Home Assistant OS", True
    return install, False


def _hardware(hass: HomeAssistant, sid: str, ident: dict[str, Any]) -> str:
    """The coordinator as the user would recognise it (from HA's device registry where possible)."""
    reg = dr.async_get(hass)
    device = None
    if sid == SOURCE_ZWAVE and ident.get("home_id") is not None and ident.get("own_node_id") is not None:
        device = reg.async_get_device(
            identifiers={("zwave_js", f"{ident['home_id']}-{ident['own_node_id']}")}
        )
    elif sid == SOURCE_ZHA and ident.get("ieee"):
        raw = str(ident["ieee"]).replace(":", "").lower()
        colon = ":".join(raw[i : i + 2] for i in range(0, len(raw), 2))
        device = reg.async_get_device(identifiers={("zha", colon)})
    kind = "Z-Wave stick" if sid == SOURCE_ZWAVE else "Zigbee coordinator"
    if device is not None:
        name = device.name or ""
        if re.fullmatch(r"Node \d+", name) and not device.manufacturer:
            name = ""  # Z-Wave JS's placeholder for a controller it can't name
        parts = [p for p in (device.manufacturer, name) if p]
        if parts:
            label = " ".join(parts)
            if device.model and device.model not in label:
                label += f" ({device.model})"
            return label
    # Firmware/stack names (zStack3x0, ezsp) mean little on a shopping list: say what it is.
    detail = ident.get("model") or ident.get("coordinator_type") or ident.get("radio_type")
    maker = ident.get("manufacturer")
    if maker:
        return " ".join(str(p) for p in (maker, detail) if p)
    return f"{kind} ({detail})" if detail else kind


def _network(ident: dict[str, Any]) -> str:
    if ident.get("home_id_hex"):
        return f"Home ID {ident['home_id_hex']}"
    if ident.get("extended_pan_id"):
        return f"Extended PAN ID {ident['extended_pan_id']}"
    return "unknown"


def _when(value) -> str:
    if value is None:
        return "never"
    local = dt_util.as_local(value)
    return f"{local:%a %d %b %Y, %H:%M}"


# --------------------------------------------------------------------------- page


async def async_render(hass: HomeAssistant, coord: RadioGuardCoordinator) -> str:
    e = html.escape
    host, haos = await _host(hass)
    place = hass.config.location_name or "Home"
    hour, minute = coord.schedule
    ha = backup_summary(hass)
    problems = coverage_problems(hass, hour, minute) or []

    # Radios
    facts: dict[str, tuple[Any, str | None, str]] = {}
    for sid, st in coord.states.items():
        meta = st.identity or (st.latest.meta if st.latest else {})
        port = None
        try:
            port = (await st.source.async_swap_plan()).current_port
        except Exception:  # noqa: BLE001
            _LOGGER.debug("No swap plan for %s", sid, exc_info=True)
        facts[sid] = (meta, port, _hardware(hass, sid, meta))

    rows, spares, shared_lines = [], [], []
    seen_pairs: set[frozenset[str]] = set()
    for sid, st in coord.states.items():
        meta, port, hw = facts[sid]
        count = device_count_of(meta)
        rows.append(
            "<tr>"
            f"<td>{e(st.source.name)}</td><td>{e(hw)}</td><td><code>{e(port or 'n/a')}</code></td>"
            f"<td>{e(_network(meta))}</td><td>{'' if count is None else count}</td>"
            f"<td>{e(_when(st.latest.created if st.latest else None))}</td>"
            "</tr>"
        )
        companion = await coord.async_companion(sid)
        if companion is None:
            spares.append(f"One spare {e(hw)} for {e(st.source.name)}")
        elif (pair := frozenset((sid, companion))) not in seen_pairs:
            seen_pairs.add(pair)
            other = coord.states[companion].source.name
            # Z-Wave JS knows the product name (e.g. Aeotec Z-Stick 10 Pro); ZHA often says "Generic ...".
            stick = facts[SOURCE_ZWAVE][2] if SOURCE_ZWAVE in pair else hw
            shared_lines.append(
                f"<p class=note><b>One stick, two radios:</b> {e(st.source.name)} and {e(other)} share the same "
                f"USB stick ({e(stick)}). If it dies, both stop together; you replace it once and Radio Guard "
                "restores both.</p>"
            )
            spares.append(
                f"One spare {e(stick)}, the same model, for both {e(st.source.name)} and {e(other)}"
            )

    # HA backups
    if ha is None:
        ha_lines = "<li>Radio Guard can't read Home Assistant's backup settings on this install.</li>"
    else:
        ha_lines = (
            f"<li><b>Automatic backups:</b> {e(ha['when'] or 'not turned on')}</li>"
            f"<li><b>Kept in:</b> {e(', '.join(ha['locations']) or 'nowhere')}</li>"
            f"<li><b>Last automatic backup:</b> {e(_when(ha['last']))}</li>"
            f"<li><b>Encrypted:</b> {'yes' if ha['encrypted'] else 'no'}</li>"
        )
    problem_html = "".join(f"<li>{e(p)}</li>" for p in problems)
    problem_block = (
        f"<div class=warn><b>Fix before you need this sheet:</b><ul>{problem_html}</ul></div>"
        if problems
        else ""
    )
    # Add-ons the radios depend on (Z-Wave JS keeps the network's security keys in its settings).
    apps = (
        "the Z-Wave JS app, which holds your Z-Wave security keys"
        if SOURCE_ZWAVE in coord.states
        else "the apps your radios run on (for example Zigbee2MQTT)"
    )
    restore_from = (
        "<b>Home Assistant Cloud</b> (sign in with your Nabu Casa account)"
        if ha and ha["cloud"]
        else "<b>Upload backup</b> (a backup file you copied off this machine)"
    )
    install_step = (
        "Install Home Assistant OS on its disk. On another computer, use Raspberry Pi Imager "
        "(<i>Choose OS &gt; Other specific-purpose OS &gt; Home assistants and home automation &gt; Home "
        "Assistant</i>) or follow home-assistant.io/installation. <b>Tip:</b> keep a spare disk with Home "
        "Assistant OS already installed, and this step disappears."
        if haos
        else "Install Home Assistant the same way as before (home-assistant.io/installation)."
    )
    sources = list(coord.states.values())
    radio_names = " / ".join(e(st.source.name) for st in sources) or "your radio"
    minutes = round(coord.offline_minutes)
    offline = f"{minutes} minute{'s' if minutes != 1 else ''}"
    machine = f"({host.split(',')[0]} or similar)" if haos else "computer"

    return f"""<!doctype html>
<html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>{e(place)}: recovery sheet</title>
<style>
body{{font:15px/1.45 -apple-system,system-ui,Segoe UI,Roboto,sans-serif;color:#111;background:#fff;max-width:820px;
margin:24px auto;padding:0 16px}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:17px;margin:22px 0 6px;border-bottom:2px solid #111;padding-bottom:2px}}
.sub{{color:#444;margin:0 0 12px}} table{{border-collapse:collapse;width:100%;font-size:13px}}
td,th{{border:1px solid #999;padding:4px 6px;text-align:left;vertical-align:top}} th{{background:#eee}}
code{{font-size:12px;word-break:break-all}} ol li,ul li{{margin:3px 0}} .note{{background:#f3f3f3;padding:6px 8px}}
.warn{{border:2px solid #b00;padding:6px 10px;margin:10px 0}} .blank{{display:inline-block;min-width:260px;
border-bottom:1px solid #111}} @media print{{body{{margin:0}}}}
</style></head><body>
<h1>{e(place)}: what to do when something breaks</h1>
<p class=sub>Printed {e(_when(dt_util.now()))} by Radio Guard. Keep it with your spare hardware. Print it again after
changing hardware or backup settings.</p>
{problem_block}

<h2>Your setup</h2>
<p>Home Assistant {e(HA_VERSION)} on {e(host)}.</p>
<table><tr><th>Radio</th><th>Hardware</th><th>Port</th><th>Network</th><th>Devices</th>
<th>Last coordinator backup</th></tr>{"".join(rows)}</table>
{"".join(shared_lines)}
<p>Radio Guard backs up the coordinators every night at {hour:02d}:{minute:02d} into
<code>/config/{e(coord.root.name)}/</code>, which is inside Home Assistant's own backups.</p>

<h2>Where your backups are</h2>
<ul>{ha_lines}
<li><b>Backup encryption key</b> (Settings &gt; System &gt; Backups &gt; emergency kit) is kept in:
<span class=blank></span></li>
<li><b>Home Assistant Cloud / Nabu Casa login</b> is kept in: <span class=blank></span></li></ul>
<p>Without the encryption key nobody can open these backups, not even you. Check today that you can find it.</p>

<h2>A. A radio stops working ({radio_names})</h2>
<ol>
<li>After about {offline}, Home Assistant shows a Repair under <b>Settings</b>: <i>“… coordinator is not
responding”</i>. If Home Assistant or the radio software is just restarting or updating, wait: the message clears
itself.</li>
<li>Open it and choose <b>My coordinator failed, replace it</b>. Pick the newest backup (it's pre-selected).</li>
<li>When asked: unplug the old stick, put it away for good (never plug it in again on this network), plug in the
spare. Radio Guard finds the new stick and picks the right port.</li>
<li>Radio Guard restores your network onto it, checks it and asks your devices to report in. Nothing needs
re-pairing. Battery devices report when they next wake up.</li>
</ol>

<h2>B. The Home Assistant machine dies</h2>
<ol>
<li>Get a replacement {e(machine)}. {install_step}</li>
<li>Connect it to the network, plug in the radio stick(s), power it on and wait 10 to 20 minutes.</li>
<li>Open <b>http://homeassistant.local:8123</b> in a browser (or the address your router shows for it).</li>
<li>On the Welcome screen, under <b>Or restore</b>, choose {restore_from} and pick the newest backup.</li>
<li>Under <b>Select what to restore</b>, <b>tick everything, including Apps</b>. Apps are not ticked by default,
and they include {apps}: without them your radios don't come back. Enter the <b>backup encryption key</b>
and press <b>Restore backup</b>.</li>
<li>Wait for the restore and restart. Your automations, devices and Radio Guard's coordinator backups come back.
With the same radio stick everything simply starts. If the stick was replaced too, continue with A.</li>
</ol>

<h2>C. Both died (for example a power surge)</h2>
<p>Do B first, then A.</p>

<h2>Spares to keep on the shelf</h2>
<ul>{"".join(f"<li>{s}</li>" for s in spares)}
<li>{"A spare disk with Home Assistant OS already installed" if haos else "Whatever you need to reinstall Home Assistant"}
</li></ul>
</body></html>
"""
