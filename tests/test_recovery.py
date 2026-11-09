"""The printable recovery sheet: built from the live setup, served only to signed-in users."""

# ruff: noqa: F811 - pytest fixtures imported from test_combo_stick are re-bound as test arguments

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.radio_guard.const import SOURCE_ZHA, SOURCE_ZWAVE
from custom_components.radio_guard.recovery import PANEL, URL, async_render

from .test_combo_stick import OLD_ZB, OLD_ZW, fakes, seeded  # noqa: F401 - fixtures
from .test_wizard import refresh, setup_rg


@pytest.fixture(autouse=True)
def _by_id_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    from custom_components.radio_guard import hardware  # noqa: PLC0415

    monkeypatch.setattr(hardware, "by_id", lambda path: path)


@pytest.fixture
def entry_data(entry_data):
    return {**entry_data, "sources": [SOURCE_ZHA, SOURCE_ZWAVE]}


def ha_backups(monkeypatch: pytest.MonkeyPatch, agents: list[str], time=dt.time(4, 0)) -> None:
    data = SimpleNamespace(
        schedule=SimpleNamespace(recurrence="daily", days=[], time=time),
        create_backup=SimpleNamespace(agent_ids=agents, password="secret-not-shown"),
        last_completed_automatic_backup=dt.datetime(2026, 11, 9, 9, 0, tzinfo=dt.UTC),
    )
    manager = SimpleNamespace(config=SimpleNamespace(data=data), local_backup_agents={})
    monkeypatch.setattr("homeassistant.components.backup.async_get_manager", lambda hass: manager)


async def test_sheet_for_a_two_in_one_stick(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, mock_config_entry: MockConfigEntry, fakes, seeded
) -> None:
    zha, zw = fakes
    zw.identity = {**zw.identity, "home_id": 3237998081, "own_node_id": 1}
    zw_entry = MockConfigEntry(domain="zwave_js", title="Z-Wave JS")
    zw_entry.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=zw_entry.entry_id,
        identifiers={("zwave_js", "3237998081-1")},
        manufacturer="Aeotec Ltd.",
        name="Z-Stick 10 Pro",
        model="ZWA060",
    )
    ha_backups(monkeypatch, ["hassio.local", "cloud.cloud"])
    coord = await setup_rg(hass, mock_config_entry)
    await refresh(hass, coord)

    page = await async_render(hass, coord)
    assert "Aeotec Ltd. Z-Stick 10 Pro (ZWA060)" in page
    assert "One stick, two radios" in page and page.count("One stick, two radios") == 1
    assert page.count("One spare Aeotec Ltd. Z-Stick 10 Pro (ZWA060), the same model, for both") == 1
    assert OLD_ZB in page and OLD_ZW in page
    assert "Home ID c0ffee01" in page
    assert "every day at 04:00" in page
    assert "This machine, Home Assistant Cloud" in page
    assert "Encrypted:</b> yes" in page
    assert "secret-not-shown" not in page  # the encryption key itself never goes on paper
    assert "Home Assistant Cloud</b> (sign in" in page
    assert "Fix before you need this sheet" not in page
    assert "<script" not in page.replace('onclick="window.print()"', "")


async def test_sheet_lists_backup_problems_first(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, mock_config_entry: MockConfigEntry, fakes, seeded
) -> None:
    ha_backups(monkeypatch, ["hassio.local"])
    coord = await setup_rg(hass, mock_config_entry)
    page = await async_render(hass, coord)
    assert "Fix before you need this sheet" in page
    assert "only kept on this machine" in page
    assert "<b>Upload backup</b>" in page  # no cloud: restore from a copied file


async def test_sheet_escapes_names(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, mock_config_entry: MockConfigEntry, fakes, seeded
) -> None:
    hass.config.location_name = "<img src=x onerror=alert(1)>"
    coord = await setup_rg(hass, mock_config_entry)
    page = await async_render(hass, coord)
    assert "<img" not in page
    assert "&lt;img src=x" in page


async def test_view_needs_an_admin(
    hass: HomeAssistant,
    hass_client,
    hass_client_no_auth,
    hass_read_only_access_token,
    mock_config_entry: MockConfigEntry,
    fakes,
    seeded,
) -> None:
    await setup_rg(hass, mock_config_entry)
    assert (await (await hass_client_no_auth()).get(URL)).status == 401
    reader = await hass_client(hass_read_only_access_token)
    assert (await reader.get(URL)).status == 403
    resp = await (await hass_client()).get(URL)
    assert resp.status == 200
    assert "what to do when something breaks" in await resp.text()
    assert resp.headers["Cache-Control"] == "no-store"


async def test_hidden_admin_panel_follows_the_entry(
    hass: HomeAssistant, hass_client, mock_config_entry: MockConfigEntry, fakes, seeded
) -> None:
    hass.config.components.add("frontend")  # the real frontend package isn't installed in tests
    await setup_rg(hass, mock_config_entry)
    panel = hass.data["frontend_panels"][PANEL]
    assert panel.require_admin
    assert panel.sidebar_title is None  # not in the sidebar
    script = panel.config["_panel_custom"]["module_url"].split("?")[0]
    resp = await (await hass_client()).get(script)
    assert resp.status == 200
    assert "fetchWithAuth" in await resp.text()
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    assert PANEL not in hass.data["frontend_panels"]


async def test_options_menu_hands_out_the_link(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, fakes, seeded
) -> None:
    await setup_rg(hass, mock_config_entry)
    mgr = hass.config_entries.options
    result = await mgr.async_init(mock_config_entry.entry_id)
    assert "recovery_sheet" in result["menu_options"]
    result = await mgr.async_configure(result["flow_id"], {"next_step_id": "recovery_sheet"})
    assert result["reason"] == "recovery_sheet"
    assert result["description_placeholders"]["url"] == f"/{PANEL}"
