"""Repairs: one-click backup for stale/failed issues, guided restore when a coordinator changes."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import data_entry_flow
from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util

from .backup import RestoreStaged
from .const import DOMAIN
from .coordinator import RadioGuardCoordinator, device_count_of


def _coordinator(hass: HomeAssistant) -> RadioGuardCoordinator:
    return hass.config_entries.async_loaded_entries(DOMAIN)[0].runtime_data


class BackupNowRepairFlow(ConfirmRepairFlow):
    """Stale / failed / never: confirm runs a backup."""

    def __init__(self, hass: HomeAssistant, sid: str) -> None:
        super().__init__()
        self._hass = hass
        self.sid = sid

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        if user_input is not None:
            coord = _coordinator(self._hass)
            try:
                await coord.async_backup(self.sid)
            except Exception as err:  # noqa: BLE001
                return self.async_abort(reason="backup_failed", description_placeholders={"error": str(err)})
            await coord.async_request_refresh()
            return self.async_create_entry(title="", data={})
        name = _coordinator(self._hass).states[self.sid].source.name
        return self.async_show_form(
            step_id="confirm", data_schema=vol.Schema({}), description_placeholders={"source": name}
        )


class RestoreRepairFlow(RepairsFlow):
    """Pick a backup -> pre-flight -> type RESTORE -> done."""

    def __init__(self, hass: HomeAssistant, sid: str) -> None:
        self._hass = hass
        self.sid = sid
        self.file: str | None = None
        self.record = None
        self.check = None

    @property
    def coord(self) -> RadioGuardCoordinator:
        return _coordinator(self._hass)

    async def async_step_init(self, user_input: dict[str, str] | None = None) -> data_entry_flow.FlowResult:
        return await self.async_step_pick()

    async def async_step_pick(self, user_input: dict[str, Any] | None = None) -> data_entry_flow.FlowResult:
        st = self.coord.states[self.sid]
        records = await self._hass.async_add_executor_job(st.store.list)
        if not records:
            return self.async_abort(reason="no_backups", description_placeholders={"source": st.source.name})
        if user_input is not None:
            self.file = user_input["file"]
            self.check = None
            return await self.async_step_preflight()
        options = []
        for r in records:
            devices = device_count_of(r.meta)
            ident = r.meta.get("ieee") or r.meta.get("home_id_hex") or r.meta.get("home_id") or ""
            label = f"{r.created.astimezone():%Y-%m-%d %H:%M}"
            if devices is not None:
                label += f" - {devices} devices"
            if ident:
                label += f" - {ident}"
            options.append(selector.SelectOptionDict(value=r.name, label=label))
        return self.async_show_form(
            step_id="pick",
            data_schema=vol.Schema(
                {
                    vol.Required("file", default=records[0].name): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options, mode=selector.SelectSelectorMode.DROPDOWN
                        )
                    )
                }
            ),
            description_placeholders={"source": st.source.name, "count": str(len(records))},
        )

    async def async_step_preflight(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        st = self.coord.states[self.sid]
        if self.check is None:
            try:
                self.record, self.check = await self.coord.async_restore_check(self.sid, self.file)
            except Exception as err:  # noqa: BLE001
                return self.async_abort(
                    reason="preflight_error", description_placeholders={"error": str(err)}
                )
        if user_input is not None:
            if not user_input.get("acknowledge"):
                return await self.async_step_preflight()
            return await self.async_step_confirm()
        c, b = self.check.current, self.record.meta
        age_h = (dt_util.utcnow() - self.record.created).total_seconds() / 3600
        return self.async_show_form(
            step_id="preflight",
            data_schema=vol.Schema({vol.Required("acknowledge", default=False): bool}),
            description_placeholders={
                "source": st.source.name,
                "file": self.record.name,
                "backup_identity": str(b.get("ieee") or b.get("home_id_hex") or b.get("home_id") or "?"),
                "backup_devices": str(device_count_of(b) if device_count_of(b) is not None else "?"),
                "backup_age": f"{age_h:.0f}",
                "current_identity": str(
                    c.get("ieee") or c.get("home_id_hex") or c.get("home_id") or "not readable"
                ),
                "status": "OK" if self.check.ok else "BLOCKED: " + "; ".join(self.check.reasons),
                "warnings": "\n".join(f"- {w}" for w in self.check.warnings) or "- none",
            },
            errors={} if self.check.ok else {"base": "preflight_failed"},
        )

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        schema = vol.Schema({vol.Required("typed"): str})
        st = self.coord.states[self.sid]
        if user_input is not None:
            if user_input.get("typed", "").strip() != "RESTORE":
                return self.async_show_form(
                    step_id="confirm",
                    data_schema=schema,
                    errors={"typed": "type_restore"},
                    description_placeholders={"source": st.source.name},
                )
            try:
                await self.coord.async_restore(self.sid, self.file, force=not self.check.ok)
            except RestoreStaged as staged:
                return self.async_abort(
                    reason="restore_staged",
                    description_placeholders={"path": staged.path, "steps": staged.steps},
                )
            except Exception as err:  # noqa: BLE001
                return self.async_abort(reason="restore_failed", description_placeholders={"error": str(err)})
            return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="confirm", data_schema=schema, description_placeholders={"source": st.source.name}
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    data = data or {}
    sid = data["source"]
    if data.get("action") == "restore":
        return RestoreRepairFlow(hass, sid)
    return BackupNowRepairFlow(hass, sid)
