"""Config flow for Radio Guard."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from .const import (
    ALL_SOURCES,
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
    DEFAULT_BACKUP_MINUTE,
    DEFAULT_HEARTBEAT_ENABLED,
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_HEARTBEAT_TOPIC,
    DEFAULT_RETENTION,
    DEFAULT_STALE_HOURS,
    DEFAULT_Z2M_BASE_TOPIC,
    DOMAIN,
    SOURCE_NAMES,
    SOURCE_Z2M,
    SOURCE_ZHA,
    SOURCE_ZWAVE,
)


def _detect_sources(hass: HomeAssistant) -> list[str]:
    found: list[str] = []
    if "mqtt" in hass.config.components:
        found.append(SOURCE_Z2M)
    if "zha" in hass.config.components:
        found.append(SOURCE_ZHA)
    if hass.config_entries.async_entries("zwave_js"):
        found.append(SOURCE_ZWAVE)
    return found


def _schema(hass: HomeAssistant, current: dict[str, Any]) -> vol.Schema:
    detected = _detect_sources(hass)
    return vol.Schema(
        {
            vol.Required(CONF_SOURCES, default=current.get(CONF_SOURCES, detected)): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[selector.SelectOptionDict(value=s, label=SOURCE_NAMES[s]) for s in ALL_SOURCES],
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
            vol.Optional(
                CONF_Z2M_BASE_TOPIC,
                default=current.get(CONF_Z2M_BASE_TOPIC, DEFAULT_Z2M_BASE_TOPIC),
            ): str,
            vol.Optional(
                CONF_BACKUP_HOUR, default=current.get(CONF_BACKUP_HOUR, DEFAULT_BACKUP_HOUR)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0, max=23, mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Optional(
                CONF_BACKUP_MINUTE, default=current.get(CONF_BACKUP_MINUTE, DEFAULT_BACKUP_MINUTE)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0, max=59, mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Optional(
                CONF_RETENTION, default=current.get(CONF_RETENTION, DEFAULT_RETENTION)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=1, max=365, mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Optional(
                CONF_STALE_HOURS, default=current.get(CONF_STALE_HOURS, DEFAULT_STALE_HOURS)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=1, max=720, mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Optional(CONF_BACKUP_DIR, default=current.get(CONF_BACKUP_DIR, DEFAULT_BACKUP_DIR)): str,
            vol.Optional(
                CONF_HEARTBEAT_ENABLED,
                default=current.get(CONF_HEARTBEAT_ENABLED, DEFAULT_HEARTBEAT_ENABLED),
            ): bool,
            vol.Optional(
                CONF_HEARTBEAT_TOPIC,
                default=current.get(CONF_HEARTBEAT_TOPIC, DEFAULT_HEARTBEAT_TOPIC),
            ): str,
            vol.Optional(
                CONF_HEARTBEAT_INTERVAL,
                default=current.get(CONF_HEARTBEAT_INTERVAL, DEFAULT_HEARTBEAT_INTERVAL),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=5, max=3600, mode=selector.NumberSelectorMode.BOX)
            ),
        }
    )


def _coerce(data: dict[str, Any]) -> dict[str, Any]:
    out = dict(data)
    for key in (
        CONF_BACKUP_HOUR,
        CONF_BACKUP_MINUTE,
        CONF_RETENTION,
        CONF_STALE_HOURS,
        CONF_HEARTBEAT_INTERVAL,
    ):
        if key in out:
            out[key] = int(out[key])
    out[CONF_BACKUP_DIR] = str(out.get(CONF_BACKUP_DIR, DEFAULT_BACKUP_DIR)).strip("/ ") or DEFAULT_BACKUP_DIR
    return out


class RadioGuardConfigFlow(ConfigFlow, domain=DOMAIN):
    """Single-instance config flow."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get(CONF_SOURCES):
                errors[CONF_SOURCES] = "no_sources"
            else:
                return self.async_create_entry(title="Radio Guard", data=_coerce(user_input))

        return self.async_show_form(
            step_id="user", data_schema=_schema(self.hass, user_input or {}), errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return RadioGuardOptionsFlow()


class RadioGuardOptionsFlow(OptionsFlow):
    """Same form, editable later."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get(CONF_SOURCES):
                errors[CONF_SOURCES] = "no_sources"
            else:
                return self.async_create_entry(title="", data=_coerce(user_input))
        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_schema(self.hass, current), errors=errors)
