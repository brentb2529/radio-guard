"""Config flow for Radio Guard.

Setup is one step: pick the coordinators (pre-selected from what is loaded)
and whether to take a first backup right away. Everything else lives in
Options, grouped into collapsible sections with sensible defaults.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector

from .const import (
    ALL_SOURCES,
    CONF_BACKUP_DIR,
    CONF_BACKUP_HOUR,
    CONF_BACKUP_MINUTE,
    CONF_BACKUP_TIME,
    CONF_FIRST_BACKUP,
    CONF_HEARTBEAT_ENABLED,
    CONF_HEARTBEAT_INTERVAL,
    CONF_HEARTBEAT_TOPIC,
    CONF_MAX_RESTORE_AGE_DAYS,
    CONF_OFFLINE_MINUTES,
    CONF_RETENTION,
    CONF_SOURCES,
    CONF_STALE_HOURS,
    CONF_Z2M_BASE_TOPIC,
    CONF_Z2M_DATA_DIR,
    DEFAULT_BACKUP_DIR,
    DEFAULT_BACKUP_HOUR,
    DEFAULT_BACKUP_MINUTE,
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_HEARTBEAT_TOPIC,
    DEFAULT_MAX_RESTORE_AGE_DAYS,
    DEFAULT_OFFLINE_MINUTES,
    DEFAULT_RETENTION,
    DEFAULT_STALE_HOURS,
    DEFAULT_Z2M_BASE_TOPIC,
    DOMAIN,
    SOURCE_NAMES,
    SOURCE_Z2M,
    SOURCE_ZHA,
    SOURCE_ZWAVE,
)
from .wizard import ReplaceWizardMixin

SECTION_SCHEDULE = "schedule"
SECTION_STORAGE = "storage"
SECTION_HEARTBEAT = "heartbeat"
SECTION_Z2M = "zigbee2mqtt"


def detect_sources(hass: HomeAssistant) -> list[str]:
    """Which backup sources look usable right now."""
    found: list[str] = []
    if "mqtt" in hass.config.components:
        found.append(SOURCE_Z2M)
    if "zha" in hass.config.components:
        found.append(SOURCE_ZHA)
    if hass.config_entries.async_entries("zwave_js"):
        found.append(SOURCE_ZWAVE)
    return found


def _sources_selector() -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[selector.SelectOptionDict(value=s, label=SOURCE_NAMES[s]) for s in ALL_SOURCES],
            multiple=True,
            mode=selector.SelectSelectorMode.LIST,
        )
    )


def _time_str(hour: int, minute: int) -> str:
    return f"{int(hour):02d}:{int(minute):02d}:00"


def _parse_time(value: str) -> tuple[int, int]:
    parts = str(value).split(":")
    return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0


def _options_schema(current: dict[str, Any], mqtt_loaded: bool) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_SOURCES, default=current.get(CONF_SOURCES, [])): _sources_selector(),
            vol.Required(SECTION_SCHEDULE): section(
                vol.Schema(
                    {
                        vol.Optional(
                            CONF_BACKUP_TIME,
                            default=_time_str(
                                current.get(CONF_BACKUP_HOUR, DEFAULT_BACKUP_HOUR),
                                current.get(CONF_BACKUP_MINUTE, DEFAULT_BACKUP_MINUTE),
                            ),
                        ): selector.TimeSelector(),
                        vol.Optional(
                            CONF_RETENTION, default=current.get(CONF_RETENTION, DEFAULT_RETENTION)
                        ): selector.NumberSelector(
                            selector.NumberSelectorConfig(
                                min=1, max=365, step=1, mode=selector.NumberSelectorMode.BOX
                            )
                        ),
                        vol.Optional(
                            CONF_STALE_HOURS, default=current.get(CONF_STALE_HOURS, DEFAULT_STALE_HOURS)
                        ): selector.NumberSelector(
                            selector.NumberSelectorConfig(
                                min=12,
                                max=168,
                                step=12,
                                unit_of_measurement="h",
                                mode=selector.NumberSelectorMode.SLIDER,
                            )
                        ),
                        vol.Optional(
                            CONF_MAX_RESTORE_AGE_DAYS,
                            default=current.get(CONF_MAX_RESTORE_AGE_DAYS, DEFAULT_MAX_RESTORE_AGE_DAYS),
                        ): selector.NumberSelector(
                            selector.NumberSelectorConfig(
                                min=1,
                                max=365,
                                step=1,
                                unit_of_measurement="d",
                                mode=selector.NumberSelectorMode.BOX,
                            )
                        ),
                        vol.Optional(
                            CONF_OFFLINE_MINUTES,
                            default=current.get(CONF_OFFLINE_MINUTES, DEFAULT_OFFLINE_MINUTES),
                        ): selector.NumberSelector(
                            selector.NumberSelectorConfig(
                                min=1,
                                max=1440,
                                step=1,
                                unit_of_measurement="min",
                                mode=selector.NumberSelectorMode.BOX,
                            )
                        ),
                    }
                ),
                {"collapsed": True},
            ),
            vol.Required(SECTION_Z2M): section(
                vol.Schema(
                    {
                        vol.Optional(
                            CONF_Z2M_BASE_TOPIC,
                            default=current.get(CONF_Z2M_BASE_TOPIC, DEFAULT_Z2M_BASE_TOPIC),
                        ): str,
                        vol.Optional(
                            CONF_Z2M_DATA_DIR,
                            description={"suggested_value": current.get(CONF_Z2M_DATA_DIR)},
                        ): str,
                    }
                ),
                {"collapsed": True},
            ),
            vol.Required(SECTION_STORAGE): section(
                vol.Schema(
                    {
                        vol.Optional(
                            CONF_BACKUP_DIR, default=current.get(CONF_BACKUP_DIR, DEFAULT_BACKUP_DIR)
                        ): str,
                    }
                ),
                {"collapsed": True},
            ),
            vol.Required(SECTION_HEARTBEAT): section(
                vol.Schema(
                    {
                        vol.Optional(
                            CONF_HEARTBEAT_ENABLED,
                            default=current.get(CONF_HEARTBEAT_ENABLED, mqtt_loaded),
                        ): bool,
                        vol.Optional(
                            CONF_HEARTBEAT_TOPIC,
                            default=current.get(CONF_HEARTBEAT_TOPIC, DEFAULT_HEARTBEAT_TOPIC),
                        ): str,
                        vol.Optional(
                            CONF_HEARTBEAT_INTERVAL,
                            default=current.get(CONF_HEARTBEAT_INTERVAL, DEFAULT_HEARTBEAT_INTERVAL),
                        ): selector.NumberSelector(
                            selector.NumberSelectorConfig(
                                min=5,
                                max=3600,
                                step=5,
                                unit_of_measurement="s",
                                mode=selector.NumberSelectorMode.BOX,
                            )
                        ),
                    }
                ),
                {"collapsed": True},
            ),
        }
    )


def flatten_options(user_input: dict[str, Any]) -> dict[str, Any]:
    """Flatten section()-nested input into the flat storage layout."""
    out: dict[str, Any] = {}
    for key, value in user_input.items():
        if isinstance(value, dict) and key in (
            SECTION_SCHEDULE,
            SECTION_STORAGE,
            SECTION_HEARTBEAT,
            SECTION_Z2M,
        ):
            out.update(value)
        else:
            out[key] = value
    if CONF_BACKUP_TIME in out:
        h, m = _parse_time(out.pop(CONF_BACKUP_TIME))
        out[CONF_BACKUP_HOUR], out[CONF_BACKUP_MINUTE] = h, m
    for key in (
        CONF_BACKUP_HOUR,
        CONF_BACKUP_MINUTE,
        CONF_RETENTION,
        CONF_STALE_HOURS,
        CONF_HEARTBEAT_INTERVAL,
        CONF_MAX_RESTORE_AGE_DAYS,
        CONF_OFFLINE_MINUTES,
    ):
        if key in out:
            out[key] = int(out[key])
    if CONF_BACKUP_DIR in out:
        out[CONF_BACKUP_DIR] = _safe_rel_dir(out[CONF_BACKUP_DIR])
    if CONF_Z2M_DATA_DIR in out:
        out[CONF_Z2M_DATA_DIR] = str(out[CONF_Z2M_DATA_DIR] or "").strip().rstrip("/")
    if CONF_Z2M_BASE_TOPIC in out:
        out[CONF_Z2M_BASE_TOPIC] = str(out[CONF_Z2M_BASE_TOPIC]).strip().strip("/") or DEFAULT_Z2M_BASE_TOPIC
    return out


def _safe_rel_dir(value: Any) -> str:
    """A relative folder under /config: drop empty, '.' and '..' parts and any leading '/'.

    Backups hold network keys, so the folder must never resolve outside the config dir.
    """
    parts = [p for p in str(value).strip().replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return "/".join(parts) or DEFAULT_BACKUP_DIR


class RadioGuardConfigFlow(ConfigFlow, domain=DOMAIN):
    """Single-instance config flow."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        detected = detect_sources(self.hass)
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get(CONF_SOURCES):
                errors[CONF_SOURCES] = "no_sources"
            else:
                data = {
                    CONF_SOURCES: user_input[CONF_SOURCES],
                    CONF_FIRST_BACKUP: bool(user_input.get(CONF_FIRST_BACKUP, True)),
                    CONF_HEARTBEAT_ENABLED: "mqtt" in self.hass.config.components,
                }
                return self.async_create_entry(title="Radio Guard", data=data)

        schema = vol.Schema(
            {
                vol.Required(CONF_SOURCES, default=detected or [SOURCE_Z2M]): _sources_selector(),
                vol.Optional(CONF_FIRST_BACKUP, default=True): bool,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "detected": ", ".join(SOURCE_NAMES[s] for s in detected) or "none",
                "backup_dir": DEFAULT_BACKUP_DIR,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return RadioGuardOptionsFlow()


class RadioGuardOptionsFlow(ReplaceWizardMixin, OptionsFlow):
    """Settings, plus the guided coordinator replacement / migration."""

    sid: str = ""
    _next: str = ""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["settings", "replace", "migrate"])

    async def async_step_replace(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._next = "replace"
        return await self.async_step_pick_source()

    async def async_step_migrate(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._next = "migrate"
        return await self.async_step_pick_source()

    async def async_step_pick_source(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self.config_entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="not_loaded")
        sources = list(self.config_entry.runtime_data.states)
        if user_input is not None or len(sources) == 1:
            self.sid = user_input["source"] if user_input else sources[0]
            if self._next == "migrate":
                return await self.async_step_wiz_migrate()
            return await self.async_step_wiz_pick()
        return self.async_show_form(
            step_id="pick_source",
            data_schema=vol.Schema(
                {
                    vol.Required("source", default=sources[0]): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(value=s, label=SOURCE_NAMES[s]) for s in sources
                            ],
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )

    async def _wizard_finish(self) -> ConfigFlowResult:
        return self.async_create_entry(title="", data=dict(self.config_entry.options))

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            flat = flatten_options(user_input)
            if not flat.get(CONF_SOURCES):
                errors[CONF_SOURCES] = "no_sources"
            else:
                internal = {k: v for k, v in self.config_entry.options.items() if k.startswith("_")}
                return self.async_create_entry(title="", data={**internal, **flat})
        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="settings",
            data_schema=_options_schema(current, "mqtt" in self.hass.config.components),
            errors=errors,
        )
