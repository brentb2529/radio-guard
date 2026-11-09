"""Guided coordinator replacement and migration, shared by the Repairs flows and the options flow.

Two modes, one path:

* replace (the old coordinator failed):  pick a backup
* migrate (moving to new hardware):      take a fresh backup of the coordinator in use

    -> stop the owning software -> "swap the hardware" (pick the new port) -> summary
    -> write backup + new port -> start -> verify the network -> ask devices to report in
    -> summary: finish / check devices again / (migrate) go back to the old coordinator

The user only touches hardware (and, for a Zigbee2MQTT that HA does not manage, stops and
starts its container when asked). Progress steps advance on their own.

Two-in-one sticks (one USB device carrying a Zigbee and a Z-Wave radio, e.g. Aeotec Z-Stick 10 Pro):
the other radio's software is the *companion*. It is backed up and stopped together with the first,
the user swaps the stick once (both new ports are pre-selected), and after the first network is back
the wizard continues with the companion on the same stick.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant import data_entry_flow
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util

from .backup.base import ConfirmationNeeded, DeviceReport, SwapPlan
from .const import DOMAIN, PRE_RESTORE_PREFIX
from .coordinator import RadioGuardCoordinator, device_count_of
from .storage import BackupRecord

_LOGGER = logging.getLogger(__name__)

MODE_REPLACE = "replace"
MODE_MIGRATE = "migrate"
STOP_TIMEOUT = 120
START_TIMEOUT = 180
DEVICE_TIMEOUT = 90
MIN_PROGRESS_SECONDS = 1.5
DEFAULT_PLUG_IN = (
    "A USB stick shows up under *New coordinator* a few seconds after you plug it in (marked *just plugged "
    "in*). For a network (Ethernet or Wi-Fi) coordinator, enter its address under *Network coordinator "
    "address* instead."
)
# Pre-flight lines that only make sense for a live coordinator or the old staged path.
_PREFLIGHT_NOISE = (
    "Could not read live coordinator identity",
    "Could not locate the Z2M data directory",
    "Will stop add-on",
    "Z2M data dir found at",
    "Unplug the old coordinator for good",
    "Live coordinator already has this backup",
    "Z-Wave JS is not loaded",  # expected mid-swap; re-checked on the new stick before restoring
    "Could not read the attached controller",
    "ZHA is not loaded",
    "Could not read the attached radio",
    "ZHA will be unloaded during the restore",
    "Never power the old coordinator again",
    "Attached radio already carries this backup",
)


async def _at_least(coro, seconds: float):
    """Run ``coro`` but take at least ``seconds``.

    The frontend subscribes to "flow progressed" only after it has rendered the progress
    screen. A task that finishes before that (a fast backup, Z2M already offline) fires its
    one notification into the void and the dialog spins forever. Seen on the dev instance.
    """
    result, _ = await asyncio.gather(coro, asyncio.sleep(seconds))
    return result


def _coordinator(hass) -> RadioGuardCoordinator:
    return hass.config_entries.async_loaded_entries(DOMAIN)[0].runtime_data


def _record_label(rec: BackupRecord) -> str:
    when = dt_util.as_local(rec.created)
    day = "Today" if when.date() == dt_util.now().date() else when.strftime("%a %b %-d")
    label = f"{day} {when:%H:%M}"
    n = device_count_of(rec.meta)
    if n is not None:
        label += f" · {n} device{'s' if n != 1 else ''}"
    if rec.name.startswith(PRE_RESTORE_PREFIX):
        label += " · automatic snapshot taken before a restore (usually not what you want)"
    return label


def _names(items: list[str], limit: int = 12) -> str:
    if not items:
        return ""
    shown = ", ".join(items[:limit])
    return shown + (f" and {len(items) - limit} more" if len(items) > limit else "")


class ReplaceWizardMixin:
    """Mix into a FlowHandler that sets ``self.sid`` and implements ``_wizard_finish``."""

    sid: str
    _mode: str = MODE_REPLACE
    _plan: SwapPlan | None = None
    _file: str | None = None
    _record: BackupRecord | None = None
    _port: str | None = None
    _old_port: str | None = None
    _task: asyncio.Task | None = None
    _error: str | None = None
    _stopped: bool = False
    _done: bool = False
    _rolled_back: bool = False
    _result: dict[str, Any] | None = None
    _report: DeviceReport | None = None
    _extra_meta: dict[str, Any] | None = None
    _confirm: ConfirmationNeeded | None = None
    _applied: bool = False  # the new coordinator has been put in place (port switched / files written)
    # Two-in-one stick: the other radio's source. Keys: sid, plan, old_port, port, record, stopped.
    _comp: dict[str, Any] | None = None
    _phase: int = 1  # 2 = working on the companion
    _first: tuple[str, str, str] | None = None  # (name, port, device summary) of phase 1

    # ------------------------------------------------------------ helpers

    @property
    def _coord(self) -> RadioGuardCoordinator:
        return _coordinator(self.hass)

    @property
    def _st(self):
        return self._coord.states[self.sid]

    def _ph(self, **extra: str) -> dict[str, str]:
        return {"source": self._st.source.name, **extra}

    async def _wizard_finish(self) -> data_entry_flow.FlowResult:  # pragma: no cover - overridden
        raise NotImplementedError

    def _comp_name(self) -> str:
        return self._coord.states[self._comp["sid"]].source.name if self._comp else ""

    def _progress(self, step_id: str, action: str, job) -> data_entry_flow.FlowResult | None:
        """Run ``job()`` behind a progress spinner; returns None once it has finished.

        ``job`` is a callable returning a coroutine, so re-entering the step does not create
        (and then drop) a fresh coroutine every time.
        """
        if self._task is None:
            self._task = self.hass.async_create_task(
                _at_least(job(), MIN_PROGRESS_SECONDS), f"{DOMAIN}_{action}"
            )
        if not self._task.done():
            return self.async_show_progress(
                step_id=step_id,
                progress_action=action,
                progress_task=self._task,
                description_placeholders={
                    **self._ph(port=self._port or "", old_port=self._old_port or "", stopper=self._stopper()),
                    **(
                        {"source": f"{self._st.source.name} and {self._comp_name()}"}
                        if action == "stopping" and self._comp and not self._comp["stopped"]
                        else {}
                    ),
                },
            )
        return None

    def _stopper(self) -> str:
        return "Radio Guard is stopping it" if self._plan and self._plan.automatic else "stop it now"

    def _take_result(self) -> BaseException | None:
        task, self._task = self._task, None
        if task.cancelled():
            return RuntimeError("cancelled")
        return task.exception()

    def _fail(self, err: BaseException | str) -> data_entry_flow.FlowResult:
        self._error = str(err) or type(err).__name__
        _LOGGER.error("%s coordinator %s failed: %s", self._st.source.name, self._mode, self._error)
        return self.async_show_progress_done(next_step_id="wiz_failed")

    async def _load_plan(self) -> data_entry_flow.FlowResult | None:
        self._plan = await self._st.source.async_swap_plan()
        if not self._plan.supported:
            return self.async_abort(
                reason="swap_unsupported", description_placeholders=self._ph(reason=self._plan.reason or "")
            )
        self._old_port = self._plan.current_port
        if self._plan.automatic and self._plan.choose_port:
            if companion := await self._coord.async_companion(self.sid):
                plan = await self._coord.states[companion].source.async_swap_plan()
                self._comp = {
                    "sid": companion,
                    "plan": plan,
                    "old_port": plan.current_port,
                    "port": None,
                    "record": None,
                    "stopped": False,
                }
        return None

    def _begin(self) -> data_entry_flow.FlowResult | None:
        if self._stopped:
            return None
        try:
            self._coord.async_swap_begin(self.sid)
        except RuntimeError as err:
            return self.async_abort(reason="swap_busy", description_placeholders=self._ph(error=str(err)))
        self._stopped = True
        return None

    # ------------------------------------------------------ replace: pick

    async def async_step_wiz_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        self._mode = MODE_REPLACE
        if self._plan is None and (aborted := await self._load_plan()):
            return aborted
        records = await self.hass.async_add_executor_job(self._st.store.list)
        if not records:
            return self.async_abort(reason="no_backups", description_placeholders=self._ph())
        nightly = [r for r in records if not r.name.startswith(PRE_RESTORE_PREFIX)]
        ordered = nightly + [r for r in records if r.name.startswith(PRE_RESTORE_PREFIX)]
        if user_input is not None:
            self._file = user_input["file"]
            self._record = next(r for r in ordered if r.name == self._file)
            if self._phase == 2:  # the companion is already stopped and its new port chosen
                return await self.async_step_wiz_confirm()
            if aborted := self._begin():
                return aborted
            return await self.async_step_wiz_stop()
        default = (nightly or ordered)[0]
        return self.async_show_form(
            step_id="wiz_pick",
            data_schema=vol.Schema(
                {
                    vol.Required("file", default=default.name): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(value=r.name, label=_record_label(r))
                                for r in ordered
                            ],
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
            description_placeholders=self._ph(),
        )

    # -------------------------------------------- migrate: fresh backup

    async def async_step_wiz_migrate(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        self._mode = MODE_MIGRATE
        if self._plan is None and (aborted := await self._load_plan()):
            return aborted
        st = self._st
        healthy = await st.source.async_health()
        if healthy is False or not st.available:
            return self.async_abort(reason="migrate_needs_running", description_placeholders=self._ph())
        if user_input is not None:
            return await self.async_step_wiz_backup()
        ident = st.identity or {}
        return self.async_show_form(
            step_id="wiz_migrate",
            data_schema=vol.Schema({}),
            description_placeholders=self._ph(
                ieee=str(ident.get("ieee") or ident.get("home_id_hex") or "unknown"),
                port=self._old_port or "unknown",
                devices=str(device_count_of(ident) if device_count_of(ident) is not None else "?"),
            ),
            last_step=False,
        )

    async def async_step_wiz_backup(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        shown = self._progress("wiz_backup", "backing_up", self._fresh_backup)
        if shown:
            return shown
        if err := self._take_result():
            self._error = f"Could not take a fresh backup, so nothing was changed: {err}"
            return self.async_show_progress_done(next_step_id="wiz_failed")
        try:
            self._coord.async_swap_begin(self.sid)
        except RuntimeError as err:
            self._error = str(err)
            return self.async_show_progress_done(next_step_id="wiz_failed")
        self._stopped = True
        return self.async_show_progress_done(next_step_id="wiz_stop")

    async def _fresh_backup(self) -> None:
        self._record = await self._coord.async_backup(self.sid)
        self._file = self._record.name

    # ---------------------------------------------------------- stop

    async def async_step_wiz_stop(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        automatic = self._plan.automatic
        shown = self._progress(
            "wiz_stop",
            ("stopping" if automatic else "wait_stopped") if self._plan.stop_first else "preparing",
            lambda: self._stop(STOP_TIMEOUT if automatic else None),
        )
        if shown:
            return shown
        if err := self._take_result():
            return self._fail(err)
        return self.async_show_progress_done(next_step_id="wiz_swap")

    async def _stop(self, timeout: float | None) -> None:
        async def _timed(coro):
            await (asyncio.wait_for(coro, timeout) if timeout else coro)

        comp = self._comp if self._comp and not self._comp["stopped"] else None
        if comp:
            # Same stick: guard the companion too, and back it up while its radio may still answer.
            self._coord.async_swap_begin(comp["sid"])
            comp["stopped"] = True
            await self._companion_backup()
        await _timed(self._st.source.async_swap_stop(self._plan))
        if comp and self._comp:
            await _timed(self._coord.states[comp["sid"]].source.async_swap_stop(comp["plan"]))

    async def _companion_backup(self) -> None:
        """Fresh backup of the companion if its radio still works, else its latest nightly one."""
        comp = self._comp
        cst = self._coord.states[comp["sid"]]
        record = None
        try:
            if cst.source.available() and await cst.source.async_health() is not False:
                record = await self._coord.async_backup(comp["sid"])
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Fresh %s backup failed, using the latest stored one: %s", cst.source.name, err)
        if record is None:
            records = await self.hass.async_add_executor_job(cst.store.list)
            record = next((r for r in records if not r.name.startswith(PRE_RESTORE_PREFIX)), None)
        if record is None:
            _LOGGER.warning(
                "No %s backup to restore; only %s is moved", cst.source.name, self._st.source.name
            )
            await self._release_companion()
            self._comp = None
            return
        comp["record"] = record

    async def _release_companion(self) -> None:
        """Start the companion again where it was (the wizard stopped short of moving it)."""
        comp = self._comp
        if not comp or not comp["stopped"]:
            return
        comp["stopped"] = False
        cst = self._coord.states[comp["sid"]]
        try:
            await cst.source.async_swap_start(comp["plan"])
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Could not restart %s", cst.source.name, exc_info=True)
        await self._coord.async_swap_end(comp["sid"], None)

    # ---------------------------------------------------------- swap

    async def _port_field(
        self, suggested_current: str | None, *, key: str = "port", source=None
    ) -> tuple[dict[Any, Any], bool]:
        ports = await (source or self._st.source).async_swap_ports(suggested_current)
        options = [
            selector.SelectOptionDict(
                value=p.value, label=f"{p.label} (just plugged in)" if p.new else p.label
            )
            for p in ports
        ]
        if suggested_current and suggested_current not in {p.value for p in ports}:
            options.append(
                selector.SelectOptionDict(
                    value=suggested_current, label=f"{suggested_current} (current setting)"
                )
            )
        new_ports = [p for p in ports if p.new]
        suggested = new_ports[0].value if new_ports else suggested_current
        field = vol.Optional(key, description={"suggested_value": suggested})
        return {
            field: selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options, custom_value=False, mode=selector.SelectSelectorMode.DROPDOWN
                )
            )
        }, bool(new_ports)

    async def async_step_wiz_swap(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get("unplugged"):
                errors["unplugged"] = "unplugged_required"
            else:
                network = (user_input.get("network_address") or "").strip()
                if network and "://" not in network:
                    network = f"tcp://{network}" if ":" in network else f"tcp://{network}:6638"
                self._port = network or (user_input.get("port") or "").strip() or None
                if self._comp:
                    comp_port = (user_input.get("companion_port") or "").strip() or None
                    if comp_port and comp_port == self._port:
                        errors["companion_port"] = "same_port"
                    else:
                        self._comp["port"] = comp_port
                if not errors:
                    return await self.async_step_wiz_confirm()
        schema: dict[Any, Any] = {}
        if self._plan.choose_port:
            field, _ = await self._port_field(self._old_port)
            schema.update(field)
            if self._comp:
                comp_src = self._coord.states[self._comp["sid"]].source
                field, _ = await self._port_field(
                    self._comp["old_port"], key="companion_port", source=comp_src
                )
                schema.update(field)
            schema[vol.Optional("network_address")] = selector.TextSelector()
        schema[vol.Required("unplugged", default=False)] = bool
        return self.async_show_form(
            step_id="wiz_swap" if self._mode == MODE_REPLACE else "wiz_swap_migrate",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders=self._ph(
                current=self._old_port or "not set",
                current_line=f"Current setting: `{self._old_port}`" if self._old_port else "",
                intro=self._swap_intro(),
                plug_in=self._plan.plug_in or DEFAULT_PLUG_IN,
            ),
        )

    def _swap_intro(self) -> str:
        name = self._st.source.name
        if self._comp:
            other = self._comp_name()
            done = "Your fresh backups are saved and both" if self._mode == MODE_MIGRATE else "Both"
            return (
                f"Your stick carries two radios: **{name}** and **{other}**. {done} are stopped. Swap the "
                f"stick once; Radio Guard moves {name} first, then {other}.\n\n*New coordinator* is the "
                f"{name} side, *Second radio on the same stick* the {other} side. Radio Guard picks both "
                "ports of the new stick for you."
            )
        if not self._plan.stop_first:
            return "Your fresh backup is saved." if self._mode == MODE_MIGRATE else "Radio Guard is ready."
        if self._mode == MODE_MIGRATE:
            return f"{name} is stopped and your fresh backup is saved."
        return f"{name} is stopped."

    async def async_step_wiz_swap_migrate(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        return await self.async_step_wiz_swap(user_input)

    # ------------------------------------------------------- summary

    async def async_step_wiz_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        if user_input is not None:
            return await self.async_step_wiz_restore()
        try:
            record, check = await self._coord.async_restore_check(self.sid, self._file)
        except Exception as err:  # noqa: BLE001
            self._error = str(err)
            return await self.async_step_wiz_failed()
        warnings = [w for w in check.warnings if not w.startswith(_PREFLIGHT_NOISE)]
        reasons = [r for r in check.reasons if not r.startswith(_PREFLIGHT_NOISE)]
        if reasons:
            return self.async_show_form(
                step_id="wiz_blocked",
                data_schema=vol.Schema({}),
                description_placeholders=self._ph(
                    file=_record_label(record), reasons="\n".join(f"- {r}" for r in reasons)
                ),
            )
        self._record = record
        meta = record.meta
        return self.async_show_form(
            step_id="wiz_confirm",
            data_schema=vol.Schema({}),
            description_placeholders=self._ph(
                file=_record_label(record),
                network=str(meta.get("extended_pan_id") or meta.get("home_id_hex") or "?"),
                port=self._port or self._old_port or "unchanged",
                target=self._plan.details.get("target") or f"`{self._port or self._old_port or 'unchanged'}`",
                warnings="\n".join(f"- {w}" for w in warnings) or "- none",
                start_note=(
                    "Radio Guard then waits for it on the new stick, restores your network, checks it and asks "
                    "your devices to report in."
                    if self._plan.details.get("external_server")
                    else "Radio Guard then starts it, checks the network and asks your devices to report in."
                    if self._plan.automatic
                    else f"You will then be asked to start {self._st.source.name} again; Radio Guard checks the "
                    "network and asks your devices to report in."
                ),
            ),
            last_step=False,
        )

    async def async_step_wiz_blocked(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        if self._mode == MODE_MIGRATE:
            self._error = "the fresh backup did not pass the pre-flight check"
            return await self.async_step_wiz_failed()
        return await self.async_step_wiz_pick()

    # ------------------------------------------------------- restore

    async def async_step_wiz_restore(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        action = "restoring" if self._plan.automatic else "writing"
        shown = self._progress("wiz_restore", action, self._apply)
        if shown:
            return shown
        if err := self._take_result():
            if isinstance(err, ConfirmationNeeded):
                self._confirm = err
                return self.async_show_progress_done(next_step_id="wiz_extra")
            return self._fail(err)
        return self.async_show_progress_done(
            next_step_id="wiz_devices" if self._result is not None else "wiz_start"
        )

    async def async_step_wiz_extra(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """The radio asked for something irreversible (e.g. a one-time IEEE write): ask, then retry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get("agree"):
                self._extra_meta = {**(self._extra_meta or {}), self._confirm.key: True}
                return await self.async_step_wiz_restore()
            errors["agree"] = "agree_required"
        return self.async_show_form(
            step_id="wiz_extra",
            data_schema=vol.Schema({vol.Required("agree", default=False): bool}),
            errors=errors,
            description_placeholders=self._ph(message=self._confirm.message if self._confirm else ""),
        )

    async def _apply(self) -> None:
        self._applied = True
        st = self._st
        data = await self.hass.async_add_executor_job(st.store.read, self._record)
        await st.source.async_swap_apply(data, {**self._record.meta, **(self._extra_meta or {})}, self._port)
        if self._plan.automatic:
            await st.source.async_swap_start(self._plan)
            await st.source.async_wait_started(START_TIMEOUT)
            await self._verify(data)

    async def _verify(self, data: bytes) -> None:
        self._result = await self._st.source.async_swap_verify(data, self._record.meta)
        self._done = True
        await self._coord.async_swap_end(self.sid, self._record)

    async def async_step_wiz_start(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        shown = self._progress("wiz_start", "wait_started", self._wait_and_verify)
        if shown:
            return shown
        if err := self._take_result():
            return self._fail(err)
        return self.async_show_progress_done(next_step_id="wiz_devices")

    async def _wait_and_verify(self) -> None:
        st = self._st
        await st.source.async_wait_started(None)
        data = await self.hass.async_add_executor_job(st.store.read, self._record)
        await self._verify(data)

    # ------------------------------------------------------- devices

    async def async_step_wiz_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        shown = self._progress("wiz_devices", "checking_devices", self._check_devices)
        if shown:
            return shown
        if err := self._take_result():
            _LOGGER.warning("Device check failed: %s", err)
            self._report = None
        return self.async_show_progress_done(next_step_id="wiz_done")

    async def _check_devices(self) -> None:
        self._report = await self._st.source.async_check_devices(DEVICE_TIMEOUT)

    def _device_summary(self) -> str:
        r = self._report
        if r is None:
            return "Radio Guard cannot ask these devices to report in; check a few in Home Assistant."
        lines = [f"**Mains-powered devices back: {r.mains_back} of {r.mains_total}**"]
        if r.missing_mains:
            lines.append(f"Not answering yet: {_names(r.missing_mains)}. Power-cycle them if they stay away.")
        if r.battery_total:
            lines.append(f"Battery devices heard from: {r.battery_back} of {r.battery_total}")
            if r.missing_battery:
                lines.append(
                    f"Not heard yet: {_names(r.missing_battery)}. They report when they next wake; "
                    "press a button on one to hurry it."
                )
        return "\n\n".join(lines)

    async def async_step_wiz_done(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        ident = self._result or {}
        placeholders = self._ph(
            network=str(ident.get("extended_pan_id") or ident.get("home_id_hex") or ""),
            ieee=str(ident.get("ieee") or ident.get("home_id_hex") or ""),
            port=self._port or self._old_port or "",
            devices=self._device_summary(),
        )
        if self._comp:  # phase 1 of a two-in-one stick: the second radio is next
            return self.async_show_menu(
                step_id="wiz_done_next",
                menu_options=["wiz_next", "wiz_devices"],
                description_placeholders={**placeholders, "next": self._comp_name()},
            )
        if self._first:
            name, port, summary = self._first
            placeholders["devices"] = (
                f"{placeholders['devices']}\n\n**{name}** (`{port}`), checked earlier:\n\n{summary}"
            )
        options = ["wiz_finish", "wiz_devices"]
        if self._mode == MODE_MIGRATE and not self._first:
            options.append("wiz_rollback")
        return self.async_show_menu(
            step_id="wiz_done" if self._mode == MODE_REPLACE else "wiz_done_migrate",
            menu_options=options,
            description_placeholders=placeholders,
        )

    async def async_step_wiz_done_next(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        return await self.async_step_wiz_done(user_input)

    async def async_step_wiz_next(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Two-in-one stick: the first network is back; now the second radio on the same stick."""
        comp, self._comp = self._comp, None
        self._first = (self._st.source.name, self._port or self._old_port or "", self._device_summary())
        self.sid = comp["sid"]
        self._plan = comp["plan"]
        self._old_port = comp["old_port"]
        self._port = comp["port"]
        self._record = comp["record"]
        self._file = comp["record"].name
        self._phase = 2
        self._stopped = True
        self._done = self._applied = False
        self._result = self._report = self._extra_meta = self._confirm = None
        return await self.async_step_wiz_confirm()

    async def async_step_wiz_done_migrate(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        return await self.async_step_wiz_done(user_input)

    async def async_step_wiz_failed_migrate(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        return await self.async_step_wiz_failed(user_input)

    async def async_step_wiz_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        return await self._wizard_finish()

    # ------------------------------------------------------ rollback

    async def async_step_wiz_rollback(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        if self._task is None and not self._st.swapping:
            # After a successful move the swap had ended: guard the source again for the rollback.
            self._coord.async_swap_begin(self.sid)
        self._stopped = True
        self._done = False
        automatic = self._plan.automatic
        shown = self._progress(
            "wiz_rollback",
            ("stopping" if automatic else "wait_stopped") if self._plan.stop_first else "preparing",
            lambda: self._stop(STOP_TIMEOUT if automatic else None),
        )
        if shown:
            return shown
        if err := self._take_result():
            return self._fail(err)
        return self.async_show_progress_done(next_step_id="wiz_rollback_swap")

    async def async_step_wiz_rollback_swap(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get("swapped_back"):
                return await self.async_step_wiz_rollback_start()
            errors["swapped_back"] = "unplugged_required"
        return self.async_show_form(
            step_id="wiz_rollback_swap",
            data_schema=vol.Schema({vol.Required("swapped_back", default=False): bool}),
            errors=errors,
            description_placeholders=self._ph(old_port=self._old_port or "its previous port"),
        )

    async def async_step_wiz_rollback_start(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        shown = self._progress(
            "wiz_rollback_start", "starting_old" if self._plan.automatic else "wait_started", self._start_old
        )
        if shown:
            return shown
        if err := self._take_result():
            return self._fail(err)
        return self.async_show_progress_done(next_step_id="wiz_rolled_back")

    async def _start_old(self) -> None:
        st = self._st
        await st.source.async_swap_set_port(self._old_port)
        if self._plan.automatic:
            await st.source.async_swap_start(self._plan)
        await st.source.async_wait_started(START_TIMEOUT if self._plan.automatic else None)
        data = await self.hass.async_add_executor_job(st.store.read, self._record)
        self._result = await st.source.async_swap_verify(data, self._record.meta)
        self._done = True
        self._rolled_back = True
        self._port = self._old_port
        await self._coord.async_swap_end(self.sid, None)
        await self._release_companion()  # the old stick is back: its other radio runs as before

    async def async_step_wiz_rolled_back(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        return self.async_abort(
            reason="rolled_back", description_placeholders=self._ph(port=self._old_port or "")
        )

    # ------------------------------------------------------- failure

    async def async_step_wiz_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        if self._mode == MODE_MIGRATE and self._stopped and not self._rolled_back and self._phase == 1:
            return self.async_show_menu(
                step_id="wiz_failed_migrate",
                menu_options=["wiz_rollback", "wiz_close"],
                description_placeholders=self._ph(error=self._error or ""),
            )
        await self._cleanup()
        return self.async_abort(reason="swap_failed", description_placeholders=self._failure_ph())

    def _failure_ph(self) -> dict[str, str]:
        details = self._plan.details if self._plan else {}
        name = self._st.source.name
        if self._first:
            after = (
                f"{self._first[0]} already runs on the new stick. {name} was not moved: open Radio Guard > "
                f"Configure > Replace a failed coordinator and choose {name} to try again."
            )
            return self._ph(error=self._error or "", after=after)
        if details.get("after_failure"):
            after = details["after_failure"]
        elif self._applied and self._mode == MODE_REPLACE:
            after = f"{name} is now set up for the new coordinator, but your network was not written to it."
        else:
            after = f"Radio Guard put {name} back the way it was."
        note = details.get("note", "")
        return self._ph(error=self._error or "", after=f"{after} {note}".strip())

    async def async_step_wiz_close(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        await self._cleanup()
        return self.async_abort(reason="swap_failed", description_placeholders=self._failure_ph())

    async def _cleanup(self) -> None:
        """If the user walks away or it fails: put back what we stopped."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
        if self._stopped and not self._done:
            self._stopped = False
            st = self._st
            if self._plan and self._plan.automatic:
                try:
                    if self._mode == MODE_MIGRATE and self._old_port and self._port:
                        await st.source.async_swap_set_port(self._old_port)
                    await st.source.async_swap_start(self._plan)
                except Exception:  # noqa: BLE001
                    _LOGGER.warning("Could not restart %s after an unfinished replacement", st.source.name)
            await self._coord.async_swap_end(self.sid, None)
        await self._release_companion()

    def async_remove(self) -> None:
        """Flow closed (dialog X) before finishing."""
        if (self._stopped and not self._done) or (self._comp and self._comp["stopped"]):
            self.hass.async_create_task(self._cleanup())
