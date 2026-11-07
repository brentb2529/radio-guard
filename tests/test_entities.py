"""Sensor, binary_sensor and button entities."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.radio_guard.const import CONF_SOURCES, SOURCE_ZHA, SOURCE_ZWAVE
from custom_components.radio_guard.coordinator import RadioGuardCoordinator
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON, STATE_UNKNOWN, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .conftest import DEFAULT_ENTRY_DATA, FakeSource

ZHA_ONLY = {**DEFAULT_ENTRY_DATA, CONF_SOURCES: [SOURCE_ZHA], "heartbeat_enabled": False}
BOTH = {**DEFAULT_ENTRY_DATA, CONF_SOURCES: [SOURCE_ZHA, SOURCE_ZWAVE], "heartbeat_enabled": False}

LAST = "sensor.radio_guard_zha_last_backup"
AGE = "sensor.radio_guard_zha_backup_age"
COUNT = "sensor.radio_guard_zha_stored_backups"
IDENT = "sensor.radio_guard_zha_coordinator_identity"
ERR = "sensor.radio_guard_zha_last_error"
STALE = "binary_sensor.radio_guard_zha_backup_stale"
AVAIL = "binary_sensor.radio_guard_zha_source_available"
BUTTON = "button.radio_guard_zha_back_up_now"


@pytest.mark.parametrize("entry_data", [ZHA_ONLY])
async def test_initial_sensor_states(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    assert hass.states.get(LAST).state == STATE_UNKNOWN
    assert hass.states.get(AGE).state == STATE_UNKNOWN
    assert hass.states.get(COUNT).state == "0"
    ident = hass.states.get(IDENT)
    assert ident.state == "00:11:22:33:44:55:66:03"
    assert ident.attributes["ieee"] == "00:11:22:33:44:55:66:03"
    assert hass.states.get(ERR).state == "none"
    assert hass.states.get(STALE).state == STATE_ON
    assert hass.states.get(AVAIL).state == STATE_ON
    assert hass.states.get(AGE).attributes["unit_of_measurement"] == "h"
    assert hass.states.get(LAST).attributes["device_class"] == "timestamp"
    assert hass.states.get(STALE).attributes["device_class"] == "problem"
    assert hass.states.get(AVAIL).attributes["device_class"] == "connectivity"


@pytest.mark.parametrize("entry_data", [ZHA_ONLY])
async def test_entity_categories(
    hass: HomeAssistant, setup_integration: MockConfigEntry, entity_registry: er.EntityRegistry
) -> None:
    assert entity_registry.async_get(LAST).entity_category is None
    assert entity_registry.async_get(STALE).entity_category is None
    assert entity_registry.async_get(BUTTON).entity_category is None
    for eid in (AGE, COUNT, IDENT, ERR, AVAIL):
        assert entity_registry.async_get(eid).entity_category is EntityCategory.DIAGNOSTIC, eid


@pytest.mark.parametrize("entry_data", [ZHA_ONLY])
async def test_sensors_reflect_backup(
    hass: HomeAssistant, setup_integration: MockConfigEntry, freezer
) -> None:
    coord: RadioGuardCoordinator = setup_integration.runtime_data
    when = datetime(2026, 11, 7, 3, 17, tzinfo=timezone.utc)
    freezer.move_to(when)
    rec = await coord.async_backup(SOURCE_ZHA)
    await coord.async_refresh()
    await hass.async_block_till_done()

    last = hass.states.get(LAST)
    assert dt_util.parse_datetime(last.state) == when
    assert dt_util.parse_datetime(last.state).tzinfo is not None
    assert last.attributes["file"] in (rec.name, str(rec.path))  # see test_last_backup_file_attribute_is_full_path
    assert last.attributes["size"] == rec.size
    assert last.attributes["format"] == "fake"
    assert last.attributes["device_count"] == 3
    assert last.attributes["source"] == SOURCE_ZHA
    assert "members" not in last.attributes

    assert float(hass.states.get(AGE).state) == pytest.approx(0.0, abs=0.01)
    assert hass.states.get(COUNT).state == "1"
    assert hass.states.get(STALE).state == STATE_OFF

    # Age advances with time on the next poll; stale flips past 36h
    freezer.tick(timedelta(hours=40))
    await coord.async_refresh()
    await hass.async_block_till_done()
    assert float(hass.states.get(AGE).state) == pytest.approx(40.0, abs=0.01)
    assert hass.states.get(STALE).state == STATE_ON


@pytest.mark.xfail(
    strict=True,
    reason="sensor.py LastBackupSensor.extra_state_attributes: meta['file'] (bare name) overwrites "
    "the explicit 'file': str(rec.path); see review/test-findings.md #1",
)
@pytest.mark.parametrize("entry_data", [ZHA_ONLY])
async def test_last_backup_file_attribute_is_full_path(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    coord: RadioGuardCoordinator = setup_integration.runtime_data
    rec = await coord.async_backup(SOURCE_ZHA)
    await hass.async_block_till_done()
    assert hass.states.get(LAST).attributes["file"] == str(rec.path)


@pytest.mark.parametrize("entry_data", [ZHA_ONLY])
async def test_last_backup_attributes_drop_members(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_sources: dict[str, FakeSource]
) -> None:
    coord: RadioGuardCoordinator = setup_integration.runtime_data
    fake_sources[SOURCE_ZHA].payload.meta = {"members": ["a.json", "b.db"], "ieee": "x"}
    await coord.async_backup(SOURCE_ZHA)
    await hass.async_block_till_done()
    attrs = hass.states.get(LAST).attributes
    assert "members" not in attrs
    assert attrs["ieee"] == "x"


@pytest.mark.parametrize("entry_data", [ZHA_ONLY])
async def test_last_error_sensor(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_sources: dict[str, FakeSource]
) -> None:
    coord: RadioGuardCoordinator = setup_integration.runtime_data
    fake_sources[SOURCE_ZHA].backup_error = RuntimeError("x" * 300)
    with pytest.raises(RuntimeError):
        await coord.async_backup(SOURCE_ZHA)
    await hass.async_block_till_done()
    state = hass.states.get(ERR).state
    assert state == "x" * 250  # truncated to fit a state
    fake_sources[SOURCE_ZHA].backup_error = None
    await coord.async_backup(SOURCE_ZHA)
    await hass.async_block_till_done()
    assert hass.states.get(ERR).state == "none"


@pytest.mark.parametrize("entry_data", [ZHA_ONLY])
async def test_available_sensor_and_identity_follow_source(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_sources: dict[str, FakeSource]
) -> None:
    coord: RadioGuardCoordinator = setup_integration.runtime_data
    fake_sources[SOURCE_ZHA].is_available = False
    await coord.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(AVAIL).state == STATE_OFF

    fake_sources[SOURCE_ZHA].is_available = True
    fake_sources[SOURCE_ZHA].identity = {"home_id": 3735928559, "sdk_version": "7.19"}
    await coord.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(AVAIL).state == STATE_ON
    ident = hass.states.get(IDENT)
    assert ident.state == "3735928559"
    assert ident.attributes["sdk_version"] == "7.19"

    fake_sources[SOURCE_ZHA].identity = {}
    await coord.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(IDENT).state == STATE_UNKNOWN


@pytest.mark.parametrize("entry_data", [BOTH])
async def test_button_press_triggers_backup(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_sources: dict[str, FakeSource]
) -> None:
    coord: RadioGuardCoordinator = setup_integration.runtime_data
    await hass.services.async_call(BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: BUTTON}, blocking=True)
    await hass.async_block_till_done()
    assert fake_sources[SOURCE_ZHA].backup_calls == 1
    assert fake_sources[SOURCE_ZWAVE].backup_calls == 0
    assert coord.states[SOURCE_ZHA].latest is not None
    assert hass.states.get(COUNT).state == "1"
    assert hass.states.get(STALE).state == STATE_OFF
    assert hass.states.get(LAST).state != STATE_UNKNOWN


@pytest.mark.parametrize("entry_data", [ZHA_ONLY])
async def test_button_press_failure_raises(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_sources: dict[str, FakeSource]
) -> None:
    fake_sources[SOURCE_ZHA].backup_error = RuntimeError("no radio")
    with pytest.raises(HomeAssistantError, match="no radio"):
        await hass.services.async_call(BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: BUTTON}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(ERR).state == "no radio"
