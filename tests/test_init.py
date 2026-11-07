"""Setup, unload, entity creation and service registration."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.radio_guard import PLATFORMS
from custom_components.radio_guard.const import (
    CONF_BACKUP_DIR,
    CONF_SOURCES,
    DOMAIN,
    SERVICE_BACKUP_NOW,
    SERVICE_RESTORE,
    SOURCE_Z2M,
    SOURCE_ZHA,
    SOURCE_ZWAVE,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .conftest import DEFAULT_ENTRY_DATA, FakeSource


async def test_setup_and_unload(hass: HomeAssistant, setup_integration: MockConfigEntry, fake_sources) -> None:
    entry = setup_integration
    assert entry.state is ConfigEntryState.LOADED
    coord = entry.runtime_data
    assert set(coord.states) == {SOURCE_Z2M, SOURCE_ZHA, SOURCE_ZWAVE}
    assert set(fake_sources) == {SOURCE_Z2M, SOURCE_ZHA, SOURCE_ZWAVE}
    # Z2M source receives the configured base topic
    assert fake_sources[SOURCE_Z2M].init_args == ("zigbee2mqtt",)
    # Stores live under the HA config dir
    assert str(coord.root) == hass.config.path("radio_guard")
    assert str(coord.root).startswith(hass.config.config_dir)
    for sid, st in coord.states.items():
        assert st.store.root == coord.root / sid
        assert st.store.retention == DEFAULT_ENTRY_DATA["retention"]

    assert hass.services.has_service(DOMAIN, SERVICE_BACKUP_NOW)
    assert hass.services.has_service(DOMAIN, SERVICE_RESTORE)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert not hass.services.has_service(DOMAIN, SERVICE_BACKUP_NOW)
    assert not hass.services.has_service(DOMAIN, SERVICE_RESTORE)
    # Timers are gone: coordinator has no subscriptions left
    assert coord._unsubs == []


async def test_entities_created_per_source(
    hass: HomeAssistant, setup_integration: MockConfigEntry, entity_registry: er.EntityRegistry
) -> None:
    entry = setup_integration
    entries = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    by_domain: dict[str, list[er.RegistryEntry]] = {}
    for e in entries:
        by_domain.setdefault(e.domain, []).append(e)

    n = 3  # sources
    assert len(by_domain[Platform.SENSOR]) == 5 * n
    assert len(by_domain[Platform.BINARY_SENSOR]) == 2 * n
    assert len(by_domain[Platform.BUTTON]) == 1 * n
    assert len(entries) == 8 * n
    assert set(PLATFORMS) == set(by_domain)

    unique_ids = {e.unique_id for e in entries}
    for sid in (SOURCE_Z2M, SOURCE_ZHA, SOURCE_ZWAVE):
        for key in ("last_backup", "backup_age", "backup_count", "identity", "last_error",
                    "backup_stale", "source_available", "backup_now"):
            assert f"{entry.entry_id}_{sid}_{key}" in unique_ids
    assert len(unique_ids) == len(entries)

    # entity_ids follow has_entity_name: "Radio Guard <Source>" + translated name
    assert entity_registry.async_get("sensor.radio_guard_zha_last_backup") is not None
    assert entity_registry.async_get("binary_sensor.radio_guard_zigbee2mqtt_backup_stale") is not None
    assert entity_registry.async_get("button.radio_guard_z_wave_js_back_up_now") is not None
    assert hass.states.get("sensor.radio_guard_zha_last_backup") is not None


async def test_one_device_per_source(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device_registry: dr.DeviceRegistry
) -> None:
    entry = setup_integration
    devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    assert len(devices) == 3
    names = {d.name for d in devices}
    assert names == {"Radio Guard Zigbee2MQTT", "Radio Guard ZHA", "Radio Guard Z-Wave JS"}
    assert {d.manufacturer for d in devices} == {"Radio Guard"}
    idents = {next(iter(d.identifiers)) for d in devices}
    assert idents == {(DOMAIN, f"{entry.entry_id}_{sid}") for sid in (SOURCE_Z2M, SOURCE_ZHA, SOURCE_ZWAVE)}


@pytest.mark.parametrize("entry_data", [{**DEFAULT_ENTRY_DATA, CONF_SOURCES: [SOURCE_ZHA]}])
async def test_only_configured_sources(
    hass: HomeAssistant, setup_integration: MockConfigEntry, entity_registry: er.EntityRegistry, fake_sources
) -> None:
    entry = setup_integration
    assert list(entry.runtime_data.states) == [SOURCE_ZHA]
    assert list(fake_sources) == [SOURCE_ZHA]
    entries = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    assert len(entries) == 8


@pytest.mark.parametrize("entry_data", [{**DEFAULT_ENTRY_DATA, CONF_SOURCES: ["bogus", SOURCE_ZWAVE]}])
async def test_unknown_source_ignored(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    assert list(setup_integration.runtime_data.states) == [SOURCE_ZWAVE]


@pytest.mark.parametrize("entry_data", [{**DEFAULT_ENTRY_DATA, CONF_BACKUP_DIR: "custom/dir"}])
async def test_custom_backup_dir(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    coord = setup_integration.runtime_data
    assert str(coord.root) == hass.config.path("custom/dir")


async def test_options_override_data(hass: HomeAssistant, mock_config_entry: MockConfigEntry, fake_sources) -> None:
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(mock_config_entry, options={CONF_SOURCES: [SOURCE_Z2M], "retention": 9})
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coord = mock_config_entry.runtime_data
    assert list(coord.states) == [SOURCE_Z2M]
    assert coord.states[SOURCE_Z2M].store.retention == 9


async def test_services_survive_while_another_entry_loaded(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_sources
) -> None:
    """Services are only removed once the last entry is unloaded."""
    second = MockConfigEntry(domain=DOMAIN, data=dict(DEFAULT_ENTRY_DATA), entry_id="rg_second")
    second.add_to_hass(hass)
    assert await hass.config_entries.async_setup(second.entry_id)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()
    assert hass.services.has_service(DOMAIN, SERVICE_BACKUP_NOW)
    assert hass.services.has_service(DOMAIN, SERVICE_RESTORE)

    assert await hass.config_entries.async_unload(second.entry_id)
    await hass.async_block_till_done()
    assert not hass.services.has_service(DOMAIN, SERVICE_BACKUP_NOW)
    assert not hass.services.has_service(DOMAIN, SERVICE_RESTORE)


async def test_first_refresh_populates_state(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_sources: dict[str, FakeSource]
) -> None:
    coord = setup_integration.runtime_data
    for sid, st in coord.states.items():
        assert st.available is True
        assert st.identity == fake_sources[sid].identity
        assert st.latest is None
        assert st.count == 0


async def test_identity_failure_is_tolerated(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, fake_sources: dict[str, FakeSource]
) -> None:
    """A generic identity error leaves the source available; SourceUnavailable flips it off."""
    from custom_components.radio_guard.backup.base import SourceUnavailable

    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coord = mock_config_entry.runtime_data

    fake_sources[SOURCE_ZHA].identity_error = RuntimeError("boom")
    fake_sources[SOURCE_ZWAVE].identity_error = SourceUnavailable("gone")
    await coord.async_refresh()
    assert coord.states[SOURCE_ZHA].available is True
    assert coord.states[SOURCE_ZWAVE].available is False
    assert coord.states[SOURCE_Z2M].available is True
