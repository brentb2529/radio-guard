"""Config and options flow tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.radio_guard.const import (
    CONF_BACKUP_DIR,
    CONF_BACKUP_HOUR,
    CONF_BACKUP_MINUTE,
    CONF_HEARTBEAT_ENABLED,
    CONF_HEARTBEAT_INTERVAL,
    CONF_HEARTBEAT_TOPIC,
    CONF_RETENTION,
    CONF_SOURCES,
    CONF_STALE_HOURS,
    CONF_Z2M_BASE_TOPIC,
    DEFAULT_BACKUP_DIR,
    DEFAULT_BACKUP_HOUR,
    DEFAULT_Z2M_BASE_TOPIC,
    DOMAIN,
    SOURCE_Z2M,
    SOURCE_ZHA,
    SOURCE_ZWAVE,
)
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType


@pytest.fixture
def no_setup():
    """Don't actually set up the integration when the flow creates an entry."""
    with patch("custom_components.radio_guard.async_setup_entry", return_value=True) as m:
        yield m


async def test_user_step_shows_form(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}
    schema_keys = {str(k) for k in result["data_schema"].schema}
    assert schema_keys == {
        CONF_SOURCES,
        CONF_Z2M_BASE_TOPIC,
        CONF_BACKUP_HOUR,
        CONF_BACKUP_MINUTE,
        CONF_RETENTION,
        CONF_STALE_HOURS,
        CONF_BACKUP_DIR,
        CONF_HEARTBEAT_ENABLED,
        CONF_HEARTBEAT_TOPIC,
        CONF_HEARTBEAT_INTERVAL,
    }


async def test_user_step_defaults_detect_sources(hass: HomeAssistant) -> None:
    hass.config.components.add("mqtt")
    hass.config.components.add("zha")
    MockConfigEntry(domain="zwave_js", data={}).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    defaults = {str(k): k.default() for k in result["data_schema"].schema if k.default is not None}
    assert defaults[CONF_SOURCES] == [SOURCE_Z2M, SOURCE_ZHA, SOURCE_ZWAVE]
    assert defaults[CONF_BACKUP_HOUR] == DEFAULT_BACKUP_HOUR
    assert defaults[CONF_Z2M_BASE_TOPIC] == DEFAULT_Z2M_BASE_TOPIC


async def test_user_step_no_detected_sources_has_empty_default(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    defaults = {str(k): k.default() for k in result["data_schema"].schema if k.default is not None}
    assert defaults[CONF_SOURCES] == []


async def test_user_step_empty_sources_errors(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOURCES: [], CONF_BACKUP_HOUR: 1, CONF_BACKUP_MINUTE: 2}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_SOURCES: "no_sources"}
    # Form is re-shown with the user's values as defaults
    defaults = {str(k): k.default() for k in result["data_schema"].schema if k.default is not None}
    assert defaults[CONF_BACKUP_HOUR] == 1
    assert defaults[CONF_BACKUP_MINUTE] == 2


async def test_user_step_creates_entry_with_coerced_ints(hass: HomeAssistant, no_setup: AsyncMock) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    user_input = {
        CONF_SOURCES: [SOURCE_ZHA],
        CONF_Z2M_BASE_TOPIC: "z2m",
        CONF_BACKUP_HOUR: 4.0,  # NumberSelector yields floats
        CONF_BACKUP_MINUTE: 30.0,
        CONF_RETENTION: 7.0,
        CONF_STALE_HOURS: 48.0,
        CONF_BACKUP_DIR: "/backups/radio/",
        CONF_HEARTBEAT_ENABLED: False,
        CONF_HEARTBEAT_TOPIC: "hb/topic",
        CONF_HEARTBEAT_INTERVAL: 60.0,
    }
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Radio Guard"
    data = result["data"]
    assert data[CONF_SOURCES] == [SOURCE_ZHA]
    for key in (CONF_BACKUP_HOUR, CONF_BACKUP_MINUTE, CONF_RETENTION, CONF_STALE_HOURS, CONF_HEARTBEAT_INTERVAL):
        assert isinstance(data[key], int), key
    assert data[CONF_BACKUP_HOUR] == 4
    assert data[CONF_BACKUP_MINUTE] == 30
    assert data[CONF_RETENTION] == 7
    assert data[CONF_STALE_HOURS] == 48
    assert data[CONF_HEARTBEAT_INTERVAL] == 60
    # backup dir is stripped of leading/trailing slashes (it's relative to /config)
    assert data[CONF_BACKUP_DIR] == "backups/radio"
    assert data[CONF_HEARTBEAT_ENABLED] is False
    assert result["result"].unique_id == DOMAIN
    assert no_setup.call_count == 1


async def test_user_step_blank_backup_dir_falls_back_to_default(hass: HomeAssistant, no_setup: AsyncMock) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOURCES: [SOURCE_Z2M], CONF_BACKUP_DIR: " / "}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_BACKUP_DIR] == DEFAULT_BACKUP_DIR


async def test_single_instance_abort(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow_round_trip(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    mock_config_entry = setup_integration

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    # Defaults come from the current entry data
    defaults = {str(k): k.default() for k in result["data_schema"].schema if k.default is not None}
    assert defaults[CONF_SOURCES] == mock_config_entry.data[CONF_SOURCES]
    assert defaults[CONF_RETENTION] == mock_config_entry.data[CONF_RETENTION]
    assert defaults[CONF_BACKUP_HOUR] == mock_config_entry.data[CONF_BACKUP_HOUR]

    # Empty sources is rejected in options too
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_SOURCES: []})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_SOURCES: "no_sources"}

    new = {
        CONF_SOURCES: [SOURCE_ZWAVE],
        CONF_Z2M_BASE_TOPIC: "zigbee2mqtt",
        CONF_BACKUP_HOUR: 5.0,
        CONF_BACKUP_MINUTE: 0.0,
        CONF_RETENTION: 30.0,
        CONF_STALE_HOURS: 72.0,
        CONF_BACKUP_DIR: "rg",
        CONF_HEARTBEAT_ENABLED: True,
        CONF_HEARTBEAT_TOPIC: "radio_guard/heartbeat",
        CONF_HEARTBEAT_INTERVAL: 15.0,
    }
    with patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)) as reload:
        result = await hass.config_entries.options.async_configure(result["flow_id"], new)
        await hass.async_block_till_done()
    reload.assert_awaited_once_with(mock_config_entry.entry_id)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options == {
        **new,
        CONF_BACKUP_HOUR: 5,
        CONF_BACKUP_MINUTE: 0,
        CONF_RETENTION: 30,
        CONF_STALE_HOURS: 72,
        CONF_HEARTBEAT_INTERVAL: 15,
    }
    assert all(
        isinstance(mock_config_entry.options[k], int)
        for k in (CONF_BACKUP_HOUR, CONF_BACKUP_MINUTE, CONF_RETENTION, CONF_STALE_HOURS, CONF_HEARTBEAT_INTERVAL)
    )

    # Re-opening the options form shows the saved options as defaults
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    defaults = {str(k): k.default() for k in result["data_schema"].schema if k.default is not None}
    assert defaults[CONF_SOURCES] == [SOURCE_ZWAVE]
    assert defaults[CONF_RETENTION] == 30
    assert defaults[CONF_BACKUP_DIR] == "rg"


async def test_options_update_triggers_reload(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    """The update listener registered on setup reloads the entry."""
    mock_config_entry = setup_integration

    with patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)) as reload:
        result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SOURCES: [SOURCE_ZHA], CONF_RETENTION: 2.0}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    reload.assert_awaited_once_with(mock_config_entry.entry_id)
