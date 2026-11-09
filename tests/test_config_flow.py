"""Config flow, options flow and flatten_options()."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData, UnknownFlow, section
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.radio_guard.backup.base import BackupPayload, BackupSource, RestoreCheck
from custom_components.radio_guard.config_flow import (
    SECTION_HEARTBEAT,
    SECTION_SCHEDULE,
    SECTION_STORAGE,
    SECTION_Z2M,
    detect_sources,
    flatten_options,
)
from custom_components.radio_guard.const import (
    CONF_BACKUP_DIR,
    CONF_BACKUP_HOUR,
    CONF_BACKUP_MINUTE,
    CONF_BACKUP_TIME,
    CONF_FIRST_BACKUP,
    CONF_FIRST_BACKUP_DONE,
    CONF_HEARTBEAT_ENABLED,
    CONF_HEARTBEAT_INTERVAL,
    CONF_HEARTBEAT_TOPIC,
    CONF_MAX_RESTORE_AGE_DAYS,
    CONF_RETENTION,
    CONF_SOURCES,
    CONF_STALE_HOURS,
    CONF_Z2M_BASE_TOPIC,
    DEFAULT_BACKUP_DIR,
    DEFAULT_Z2M_BASE_TOPIC,
    DOMAIN,
    SOURCE_Z2M,
    SOURCE_ZHA,
    SOURCE_ZWAVE,
)

COMPONENT_DIR = Path(__file__).parent.parent / "custom_components" / DOMAIN


async def _open_settings(hass, entry_id):
    """Configure opens a menu; go to the Settings page."""
    result = await hass.config_entries.options.async_init(entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["settings", "replace", "migrate", "recovery_sheet"]
    return await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "settings"})


# ------------------------------------------------------------------ helpers


def _defaults(schema: vol.Schema) -> dict[str, Any]:
    """Map key -> default for a flat voluptuous schema."""
    out: dict[str, Any] = {}
    for marker in schema.schema:
        default = marker.default
        out[str(marker)] = default() if callable(default) else default
    return out


def _sections(schema: vol.Schema) -> dict[str, section]:
    return {str(k): v for k, v in schema.schema.items() if isinstance(v, section)}


def _all_sections(**overrides: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Options input with every (required) section present; defaults fill the rest."""
    base: dict[str, dict[str, Any]] = {
        SECTION_SCHEDULE: {},
        SECTION_Z2M: {},
        SECTION_STORAGE: {},
        SECTION_HEARTBEAT: {},
    }
    base.update(overrides)
    return base


@pytest.fixture
def mock_setup_entry():
    """Keep the user-step tests from setting the integration up."""
    with (
        patch("custom_components.radio_guard.async_setup_entry", return_value=True) as m,
        patch("custom_components.radio_guard.async_unload_entry", return_value=True),
    ):
        yield m


class _IdleSource(BackupSource):
    """A source that is never available: no MQTT, no radios, no issues."""

    def __init__(self, hass: HomeAssistant, sid: str) -> None:
        super().__init__(hass)
        self.id = sid
        self.name = sid

    def available(self) -> bool:
        return False

    async def async_identity(self) -> dict[str, Any]:
        return {}

    async def async_backup(self) -> BackupPayload:  # pragma: no cover - never called
        raise AssertionError("no backups in options tests")

    async def async_restore_check(
        self, data: bytes, meta: dict[str, Any]
    ) -> RestoreCheck:  # pragma: no cover
        return RestoreCheck(ok=False)

    async def async_restore(self, data: bytes, meta: dict[str, Any]) -> None:  # pragma: no cover
        raise AssertionError


@pytest.fixture
def fake_sources():
    """Replace the real sources with idle fakes and count constructions (one per setup)."""
    built: list[str] = []

    def factory(sid: str):
        def make(hass: HomeAssistant, *_args: Any) -> _IdleSource:
            built.append(sid)
            return _IdleSource(hass, sid)

        return make

    mod = "custom_components.radio_guard.coordinator"
    with (
        patch(f"{mod}.Zigbee2MQTTSource", factory(SOURCE_Z2M)),
        patch(f"{mod}.ZHASource", factory(SOURCE_ZHA)),
        patch(f"{mod}.ZWaveJSSource", factory(SOURCE_ZWAVE)),
    ):
        yield built


@pytest.fixture
async def loaded_entry(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, fake_sources
) -> MockConfigEntry:
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.LOADED
    return mock_config_entry


# ------------------------------------------------------------ detection


async def test_detect_sources_none(hass: HomeAssistant) -> None:
    assert detect_sources(hass) == []


async def test_detect_sources_all(hass: HomeAssistant) -> None:
    hass.config.components.add("mqtt")
    hass.config.components.add("zha")
    MockConfigEntry(domain="zwave_js", data={}).add_to_hass(hass)
    assert detect_sources(hass) == [SOURCE_Z2M, SOURCE_ZHA, SOURCE_ZWAVE]


async def test_detect_zwave_by_entry_not_component(hass: HomeAssistant) -> None:
    """Z-Wave JS is detected from config entries; ZHA from loaded components."""
    MockConfigEntry(domain="zwave_js", data={}).add_to_hass(hass)
    MockConfigEntry(domain="zha", data={}).add_to_hass(hass)  # entry only, not loaded
    assert detect_sources(hass) == [SOURCE_ZWAVE]


# ------------------------------------------------------------- user step


async def test_user_form_preselects_detected(hass: HomeAssistant) -> None:
    hass.config.components.add("mqtt")
    MockConfigEntry(domain="zwave_js", data={}).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}
    defaults = _defaults(result["data_schema"])
    assert set(defaults) == {CONF_SOURCES, CONF_FIRST_BACKUP}
    assert defaults[CONF_SOURCES] == [SOURCE_Z2M, SOURCE_ZWAVE]
    assert defaults[CONF_FIRST_BACKUP] is True
    assert result["description_placeholders"] == {
        "detected": "Zigbee2MQTT, Z-Wave JS",
        "backup_dir": DEFAULT_BACKUP_DIR,
    }


async def test_user_form_zha_detected(hass: HomeAssistant) -> None:
    hass.config.components.add("zha")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert _defaults(result["data_schema"])[CONF_SOURCES] == [SOURCE_ZHA]
    assert result["description_placeholders"]["detected"] == "ZHA"


async def test_user_form_nothing_detected_falls_back_to_z2m(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert _defaults(result["data_schema"])[CONF_SOURCES] == [SOURCE_Z2M]
    assert result["description_placeholders"]["detected"] == "none"


async def test_user_creates_entry(hass: HomeAssistant, mock_setup_entry) -> None:
    hass.config.components.add("mqtt")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOURCES: [SOURCE_Z2M, SOURCE_ZHA], CONF_FIRST_BACKUP: False}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Radio Guard"
    assert result["data"] == {
        CONF_SOURCES: [SOURCE_Z2M, SOURCE_ZHA],
        CONF_FIRST_BACKUP: False,
        CONF_HEARTBEAT_ENABLED: True,
    }
    entry = result["result"]
    assert entry.unique_id == DOMAIN
    assert entry.options == {}
    assert len(mock_setup_entry.mock_calls) == 1


async def test_user_first_backup_defaults_true_and_no_mqtt_disables_heartbeat(
    hass: HomeAssistant, mock_setup_entry
) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_SOURCES: [SOURCE_ZWAVE]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_FIRST_BACKUP] is True
    assert result["data"][CONF_HEARTBEAT_ENABLED] is False


async def test_user_no_sources_error_then_recovers(hass: HomeAssistant, mock_setup_entry) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOURCES: [], CONF_FIRST_BACKUP: True}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_SOURCES: "no_sources"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_SOURCES: [SOURCE_ZHA]})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_rejects_unknown_source(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_SOURCES: ["deconz"]})


async def test_single_instance_abort(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_second_flow_aborts_after_first_completes(hass: HomeAssistant, mock_setup_entry) -> None:
    first = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    second = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    done = await hass.config_entries.flow.async_configure(first["flow_id"], {CONF_SOURCES: [SOURCE_Z2M]})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    # HA aborts other in-progress flows for the same unique_id once the entry exists.
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    with pytest.raises(UnknownFlow):
        await hass.config_entries.flow.async_configure(second["flow_id"], {CONF_SOURCES: [SOURCE_Z2M]})
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


# ---------------------------------------------------------- flatten_options


def test_flatten_options_full() -> None:
    flat = flatten_options(
        {
            CONF_SOURCES: [SOURCE_ZHA],
            SECTION_SCHEDULE: {
                CONF_BACKUP_TIME: "04:05:00",
                CONF_RETENTION: 10.0,
                CONF_STALE_HOURS: 48.0,
                CONF_MAX_RESTORE_AGE_DAYS: 3.0,
            },
            SECTION_Z2M: {CONF_Z2M_BASE_TOPIC: " /z2m/ "},
            SECTION_STORAGE: {CONF_BACKUP_DIR: "/rg_backups/"},
            SECTION_HEARTBEAT: {
                CONF_HEARTBEAT_ENABLED: True,
                CONF_HEARTBEAT_TOPIC: "hb",
                CONF_HEARTBEAT_INTERVAL: 60.0,
            },
        }
    )
    assert flat == {
        CONF_SOURCES: [SOURCE_ZHA],
        CONF_BACKUP_HOUR: 4,
        CONF_BACKUP_MINUTE: 5,
        CONF_RETENTION: 10,
        CONF_STALE_HOURS: 48,
        CONF_MAX_RESTORE_AGE_DAYS: 3,
        CONF_Z2M_BASE_TOPIC: "z2m",
        CONF_BACKUP_DIR: "rg_backups",
        CONF_HEARTBEAT_ENABLED: True,
        CONF_HEARTBEAT_TOPIC: "hb",
        CONF_HEARTBEAT_INTERVAL: 60,
    }
    for key in (CONF_RETENTION, CONF_STALE_HOURS, CONF_MAX_RESTORE_AGE_DAYS, CONF_HEARTBEAT_INTERVAL):
        assert type(flat[key]) is int


def test_flatten_options_time_without_seconds_and_hour_only() -> None:
    assert flatten_options({SECTION_SCHEDULE: {CONF_BACKUP_TIME: "23:59"}}) == {
        CONF_BACKUP_HOUR: 23,
        CONF_BACKUP_MINUTE: 59,
    }
    assert flatten_options({SECTION_SCHEDULE: {CONF_BACKUP_TIME: "7"}}) == {
        CONF_BACKUP_HOUR: 7,
        CONF_BACKUP_MINUTE: 0,
    }


def test_flatten_options_blank_values_fall_back_to_defaults() -> None:
    flat = flatten_options(
        {SECTION_STORAGE: {CONF_BACKUP_DIR: " / "}, SECTION_Z2M: {CONF_Z2M_BASE_TOPIC: "///"}}
    )
    assert flat == {CONF_BACKUP_DIR: DEFAULT_BACKUP_DIR, CONF_Z2M_BASE_TOPIC: DEFAULT_Z2M_BASE_TOPIC}


def test_flatten_options_passes_through_unknown_and_non_section_dicts() -> None:
    flat = flatten_options({"_internal": 1, "other": {"a": 1}})
    assert flat == {"_internal": 1, "other": {"a": 1}}


def test_flatten_options_strips_parent_refs_in_the_middle() -> None:
    flat = flatten_options({SECTION_STORAGE: {CONF_BACKUP_DIR: "a/../b"}})
    assert ".." not in flat[CONF_BACKUP_DIR]
    assert not os.path.isabs(flat[CONF_BACKUP_DIR])


def test_flatten_options_backup_dir_cannot_escape_config() -> None:
    for raw, expected in (
        ("../etc", "etc"),
        ("/etc/radio", "etc/radio"),
        ("a/../../b", "a/b"),
        ("..\\..\\x", "x"),
        ("./backups/", "backups"),
        ("..", "radio_guard"),
        ("  ", "radio_guard"),
    ):
        flat = flatten_options({SECTION_STORAGE: {CONF_BACKUP_DIR: raw}})
        assert flat[CONF_BACKUP_DIR] == expected, raw
        assert not os.path.isabs(flat[CONF_BACKUP_DIR])
        joined = os.path.normpath(os.path.join("/config", flat[CONF_BACKUP_DIR]))
        assert joined.startswith("/config/"), raw


# ------------------------------------------------------------ options flow


async def test_options_form_sections_collapsed_with_current_defaults(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    result = await _open_settings(hass, loaded_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "settings"
    schema: vol.Schema = result["data_schema"]

    sections = _sections(schema)
    assert set(sections) == {SECTION_SCHEDULE, SECTION_Z2M, SECTION_STORAGE, SECTION_HEARTBEAT}
    assert all(s.options["collapsed"] is True for s in sections.values())

    top = {str(k): k for k in schema.schema}
    assert top[CONF_SOURCES].default() == [SOURCE_Z2M, SOURCE_ZHA, SOURCE_ZWAVE]

    sched = _defaults(sections[SECTION_SCHEDULE].schema)
    assert sched == {
        CONF_BACKUP_TIME: "03:17:00",
        CONF_RETENTION: 3,
        CONF_STALE_HOURS: 36,
        CONF_MAX_RESTORE_AGE_DAYS: 7,
        "offline_minutes": 10,
    }
    assert _defaults(sections[SECTION_Z2M].schema)[CONF_Z2M_BASE_TOPIC] == "zigbee2mqtt"
    assert _defaults(sections[SECTION_STORAGE].schema) == {CONF_BACKUP_DIR: "radio_guard"}
    assert _defaults(sections[SECTION_HEARTBEAT].schema) == {
        CONF_HEARTBEAT_ENABLED: False,
        CONF_HEARTBEAT_TOPIC: "radio_guard/heartbeat",
        CONF_HEARTBEAT_INTERVAL: 30,
    }


async def test_options_defaults_prefer_options_over_data(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, fake_sources
) -> None:
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_BACKUP_HOUR: 22, CONF_BACKUP_MINUTE: 5, CONF_SOURCES: [SOURCE_ZHA]}
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    result = await _open_settings(hass, mock_config_entry.entry_id)
    schema = result["data_schema"]
    assert {str(k): k for k in schema.schema}[CONF_SOURCES].default() == [SOURCE_ZHA]
    assert _defaults(_sections(schema)[SECTION_SCHEDULE].schema)[CONF_BACKUP_TIME] == "22:05:00"


@pytest.mark.parametrize("mqtt_loaded", [True, False])
async def test_options_heartbeat_default_follows_mqtt_when_unset(
    hass: HomeAssistant, entry_data: dict[str, Any], fake_sources, mqtt_loaded: bool
) -> None:
    entry_data.pop(CONF_HEARTBEAT_ENABLED)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data=entry_data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    if mqtt_loaded:
        hass.config.components.add("mqtt")

    result = await _open_settings(hass, entry.entry_id)
    hb = _defaults(_sections(result["data_schema"])[SECTION_HEARTBEAT].schema)
    assert hb[CONF_HEARTBEAT_ENABLED] is mqtt_loaded


async def test_options_save_flattens_preserves_internal_and_reloads(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, fake_sources: list[str]
) -> None:
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={CONF_FIRST_BACKUP_DONE: True, "_private": "keep", CONF_RETENTION: 9},
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    first_coord = mock_config_entry.runtime_data
    built_before = len(fake_sources)

    result = await _open_settings(hass, mock_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SOURCES: [SOURCE_ZHA],
            **_all_sections(
                schedule={CONF_BACKUP_TIME: "01:30:00", CONF_RETENTION: 5},
                storage={CONF_BACKUP_DIR: "rg"},
            ),
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    opts = dict(mock_config_entry.options)
    # internal keys survive, user keys are replaced wholesale by the flat form values
    assert opts[CONF_FIRST_BACKUP_DONE] is True
    assert opts["_private"] == "keep"
    assert opts[CONF_SOURCES] == [SOURCE_ZHA]
    assert opts[CONF_BACKUP_HOUR] == 1
    assert opts[CONF_BACKUP_MINUTE] == 30
    assert opts[CONF_RETENTION] == 5
    assert opts[CONF_BACKUP_DIR] == "rg"
    assert opts[CONF_STALE_HOURS] == 36
    assert opts[CONF_Z2M_BASE_TOPIC] == "zigbee2mqtt"
    assert CONF_BACKUP_TIME not in opts
    assert not any(k in opts for k in (SECTION_SCHEDULE, SECTION_Z2M, SECTION_STORAGE, SECTION_HEARTBEAT))

    # reload happened: a fresh coordinator built only the remaining source
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_config_entry.runtime_data is not first_coord
    assert fake_sources[built_before:] == [SOURCE_ZHA]
    assert list(mock_config_entry.runtime_data.states) == [SOURCE_ZHA]
    assert mock_config_entry.runtime_data.schedule == (1, 30)
    assert str(mock_config_entry.runtime_data.root).endswith(os.sep + "rg")


async def test_options_save_does_not_clobber_data(hass: HomeAssistant, loaded_entry: MockConfigEntry) -> None:
    data_before = dict(loaded_entry.data)
    result = await _open_settings(hass, loaded_entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SOURCES: [SOURCE_Z2M], **_all_sections()}
    )
    await hass.async_block_till_done()
    assert dict(loaded_entry.data) == data_before
    # first_backup lives in data and stays there
    assert CONF_FIRST_BACKUP not in loaded_entry.options


async def test_options_no_sources_error(hass: HomeAssistant, loaded_entry: MockConfigEntry) -> None:
    result = await _open_settings(hass, loaded_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SOURCES: [], **_all_sections()}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_SOURCES: "no_sources"}
    assert loaded_entry.options == {}


@pytest.mark.parametrize(
    ("section_key", "values"),
    [
        (SECTION_SCHEDULE, {CONF_RETENTION: 0}),
        (SECTION_SCHEDULE, {CONF_RETENTION: 366}),
        (SECTION_SCHEDULE, {CONF_STALE_HOURS: 6}),
        (SECTION_SCHEDULE, {CONF_MAX_RESTORE_AGE_DAYS: 0}),
        (SECTION_SCHEDULE, {CONF_BACKUP_TIME: "25:00:00"}),
        (SECTION_HEARTBEAT, {CONF_HEARTBEAT_INTERVAL: 1}),
        (SECTION_HEARTBEAT, {CONF_HEARTBEAT_ENABLED: "maybe"}),
    ],
)
async def test_options_out_of_range_rejected(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, section_key: str, values: dict[str, Any]
) -> None:
    result = await _open_settings(hass, loaded_entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SOURCES: [SOURCE_Z2M], **_all_sections(**{section_key: values})}
        )
    assert loaded_entry.options == {}


async def test_options_missing_section_rejected(hass: HomeAssistant, loaded_entry: MockConfigEntry) -> None:
    result = await _open_settings(hass, loaded_entry.entry_id)
    sections = _all_sections()
    sections.pop(SECTION_HEARTBEAT)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SOURCES: [SOURCE_Z2M], **sections}
        )


# ------------------------------------------------------------ translations


def test_strings_match_en_translation() -> None:
    strings = json.loads((COMPONENT_DIR / "strings.json").read_text())
    en = json.loads((COMPONENT_DIR / "translations" / "en.json").read_text())
    assert strings == en
