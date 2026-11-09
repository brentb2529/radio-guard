"""Radio Guard checks that Home Assistant's own backups carry its files off the machine."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.radio_guard.const import DOMAIN, ISSUE_HA_BACKUPS
from custom_components.radio_guard.ha_backup import coverage_problems


def fake_manager(
    monkeypatch: pytest.MonkeyPatch,
    *,
    recurrence: str = "daily",
    days: list[str] | None = None,
    time: dt.time | None = dt.time(4, 0),
    agents: list[str] | None = None,
) -> None:
    data = SimpleNamespace(
        schedule=SimpleNamespace(recurrence=recurrence, days=days or [], time=time),
        create_backup=SimpleNamespace(
            agent_ids=agents if agents is not None else ["hassio.local", "cloud.cloud"]
        ),
    )
    manager = SimpleNamespace(config=SimpleNamespace(data=data), local_backup_agents={})
    monkeypatch.setattr("homeassistant.components.backup.async_get_manager", lambda hass: manager)


def test_production_like_setup_is_fine(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """Production today: daily 04:00, on the Pi + HA Cloud; Radio Guard at 03:17."""
    fake_manager(monkeypatch)
    assert coverage_problems(hass, 3, 17) == []


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"recurrence": "never"}, "doesn't make automatic backups"),
        ({"recurrence": "custom_days", "days": []}, "doesn't make automatic backups"),
        ({"agents": ["hassio.local"]}, "only kept on this machine"),
        ({"agents": ["backup.local"]}, "only kept on this machine"),
        ({"agents": []}, "only kept on this machine"),
        ({"time": dt.time(3, 20)}, "too soon after or before"),  # 3 min after Radio Guard: too tight
        ({"time": dt.time(2, 0)}, "too soon after or before"),  # before Radio Guard: a day old
    ],
)
def test_problems(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any], expected: str
) -> None:
    fake_manager(monkeypatch, **kwargs)
    problems = coverage_problems(hass, 3, 17)
    assert len(problems) == 1
    assert expected in problems[0]


def test_network_share_counts_as_off_machine(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_manager(monkeypatch, agents=["hassio.local", "hassio.nas_backups"])
    assert coverage_problems(hass, 3, 17) == []


def test_order_wraps_around_midnight(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_manager(monkeypatch, time=dt.time(4, 45))
    assert coverage_problems(hass, 23, 50) == []  # 23:50 then 04:45 next morning: fine
    fake_manager(monkeypatch, time=None)  # HA's default 04:45
    assert coverage_problems(hass, 3, 17) == []


def test_unreadable_means_unknown(hass: HomeAssistant) -> None:
    assert coverage_problems(hass, 3, 17) is None  # backup integration not loaded


async def test_real_backup_integration_default_raises_issue(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Against HA's real backup integration: out of the box it makes no automatic backups."""
    assert await async_setup_component(hass, "backup", {})
    await hass.async_block_till_done()
    assert coverage_problems(hass, 3, 17) == [
        "Home Assistant doesn't make automatic backups, so nothing copies your coordinator backups off "
        "this machine."
    ]
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    await mock_config_entry.runtime_data.async_refresh()
    found = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_HA_BACKUPS)
    assert found is not None
    assert not found.is_fixable
    assert "doesn't make automatic backups" in found.translation_placeholders["problems"]
    assert found.translation_placeholders["dir"] == "radio_guard"
