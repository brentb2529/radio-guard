"""Z-Wave JS backup source.

Uses the zwave-js-server client that the Z-Wave JS integration already holds
(HA 2025.8+: ``entry.runtime_data.client``), so this works for the Supervisor
add-on, Z-Wave JS UI, or a bare ``zwave-js-server`` on the network.

NVM backup/restore and format conversion between 500/700/800-series are done
by zwave-js itself. Radio Guard adds the parts nobody else does: a schedule,
metadata, the S0/S2 keys (which are NOT in the NVM), and a pre-flight.

Restore mirrors core's ``websocket_restore_nvm``: subscribe to driver-ready
first, restore with conversion, wait for the driver, re-read the home ID via
/version (the client's controller state is stale), update the entry's
unique_id so HA doesn't raise an "unknown adapter" repair, then reload.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any

from awesomeversion import AwesomeVersion
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant

from ..const import RESTORE_TIMEOUT, SOURCE_NAMES, SOURCE_ZWAVE, ZWAVE_BACKUP_TIMEOUT
from .base import (
    BackupPayload,
    BackupSource,
    DeviceReport,
    RestoreCheck,
    SourceUnavailable,
    SwapPlan,
)

_LOGGER = logging.getLogger(__name__)
ZWAVE_DOMAIN = "zwave_js"
ZWAVE_ADDON_SLUG = "core_zwave_js"
DRIVER_TIMEOUT = 300
PING_TIMEOUT = 20

# Keys as the Z-Wave JS integration stores them in entry.data for add-on users.
KEY_FIELDS = (
    "s0_legacy_key",
    "s2_access_control_key",
    "s2_authenticated_key",
    "s2_unauthenticated_key",
    "lr_s2_access_control_key",
    "lr_s2_authenticated_key",
)
MIN_500_SDK = AwesomeVersion("6.61")
MIN_700_SDK = AwesomeVersion("7.19.0")
RECOMMENDED_700_SDK = AwesomeVersion("7.23.2")


def _series(sdk: str | None) -> int | None:
    """500 for SDK 6.x sticks, 700 for 7.x (700 and 800 share the NVM3 format)."""
    if not sdk:
        return None
    try:
        major = int(str(sdk).split(".")[0])
    except ValueError:
        return None
    return 500 if major < 7 else 700


class ZWaveJSSource(BackupSource):
    """Backup via the Z-Wave JS integration's server client."""

    id = SOURCE_ZWAVE
    name = SOURCE_NAMES[SOURCE_ZWAVE]
    tech = "zwave"

    def __init__(self, hass: HomeAssistant, home_id: int | None = None) -> None:
        super().__init__(hass)
        self.home_id = home_id  # pin to one controller when several entries exist

    # ------------------------------------------------------------ helpers

    def available(self) -> bool:
        return self._entry() is not None

    def _entries(self) -> list[ConfigEntry]:
        return [
            e
            for e in self.hass.config_entries.async_entries(ZWAVE_DOMAIN)
            if e.state is ConfigEntryState.LOADED
        ]

    def _entry(self) -> ConfigEntry | None:
        entries = self._entries()
        if not entries:
            return None
        if self.home_id is not None:
            for e in entries:
                if str(e.unique_id) == str(self.home_id):
                    return e
        return entries[0]

    @staticmethod
    def _client(entry: ConfigEntry):
        runtime = getattr(entry, "runtime_data", None)
        client = getattr(runtime, "client", None)
        if client is None and isinstance(runtime, dict):  # HA 2024.6-2025.7
            client = runtime.get("client")
        if client is None:
            raise SourceUnavailable("Z-Wave JS client not found (Home Assistant 2025.8+ required)")
        return client

    def _controller(self):
        entry = self._entry()
        if entry is None:
            raise SourceUnavailable("Z-Wave JS integration is not loaded")
        client = self._client(entry)
        driver = getattr(client, "driver", None)
        if driver is None or driver.controller is None:
            raise SourceUnavailable("Z-Wave JS driver not ready")
        return driver.controller

    async def _assert_idle(self, ctrl) -> None:
        """Refuse to take the radio offline while the controller is busy."""
        from zwave_js_server.const import ControllerStatus, InclusionState  # noqa: PLC0415

        status = getattr(ctrl, "status", None)
        if status is not None and status != ControllerStatus.READY:
            raise RuntimeError(f"Controller not ready (status {getattr(status, 'name', status)})")
        incl = getattr(ctrl, "inclusion_state", None)
        if incl not in (None, InclusionState.IDLE, InclusionState.SMART_START):
            raise RuntimeError(f"Controller busy: inclusion state {getattr(incl, 'name', incl)}")
        if getattr(ctrl, "is_rebuilding_routes", False):
            raise RuntimeError("Route rebuild in progress")
        for check in ("async_is_any_ota_firmware_update_in_progress", "async_is_firmware_update_in_progress"):
            fn = getattr(ctrl, check, None)
            if fn is None:
                continue
            busy = False
            with suppress(Exception):  # a failing query must not block; a True answer must
                busy = bool(await fn())
            if busy:
                raise RuntimeError("Firmware update in progress")

    def _keys(self, entry: ConfigEntry | None) -> dict[str, str]:
        if entry is None:
            return {}
        return {k: str(entry.data[k]) for k in KEY_FIELDS if entry.data.get(k)}

    # ---------------------------------------------------------- identity

    async def async_identity(self) -> dict[str, Any]:
        ctrl = self._controller()
        entry = self._entry()
        return {
            "home_id": ctrl.home_id,
            "home_id_hex": f"0x{ctrl.home_id:08X}" if ctrl.home_id is not None else None,
            "own_node_id": ctrl.own_node_id,
            "sdk_version": ctrl.sdk_version,
            "firmware_version": ctrl.firmware_version,
            "manufacturer_id": ctrl.manufacturer_id,
            "product_type": ctrl.product_type,
            "product_id": ctrl.product_id,
            "controller_type": getattr(ctrl, "controller_type", None),
            "node_count": len(ctrl.nodes),
            "keys_captured": bool(self._keys(entry)),
            "use_addon": bool(entry.data.get("use_addon")) if entry else None,
        }

    # ------------------------------------------------------------ backup

    async def async_backup(self) -> BackupPayload:
        ctrl = self._controller()
        entry = self._entry()
        await self._assert_idle(ctrl)
        data = await asyncio.wait_for(ctrl.async_backup_nvm_raw(), ZWAVE_BACKUP_TIMEOUT)
        if not data:
            raise RuntimeError("Controller returned an empty NVM; firmware may be too old for NVM backup")
        meta = {"format": "zwave-nvm-raw", **await self.async_identity()}
        keys = self._keys(entry)
        if keys:
            meta["security_keys"] = keys
        else:
            meta["security_keys_note"] = (
                "S0/S2 keys are not stored in the NVM and could not be read (not the Supervisor "
                "add-on). Export them from Z-Wave JS UI > Settings > Z-Wave > Security keys."
            )
        return BackupPayload(data=data, ext=".nvm", meta=meta)

    # ----------------------------------------------------------- restore

    async def async_restore_check(self, data: bytes, meta: dict[str, Any]) -> RestoreCheck:
        check = RestoreCheck(ok=True, backup=meta)
        if not data:
            return RestoreCheck(ok=False, reasons=["Backup file is empty"])
        if not self.available():
            check.ok = False
            check.reasons.append("Z-Wave JS is not loaded. Point it at the new stick first, then retry.")
            return check
        try:
            check.current = await self.async_identity()
        except Exception as err:  # noqa: BLE001
            check.ok = False
            check.reasons.append(f"Could not read the attached controller: {err}")
            return check

        cur_home, bak_home = check.current.get("home_id"), meta.get("home_id")
        if cur_home and bak_home and cur_home == bak_home:
            check.ok = False
            check.reasons.append(
                f"Attached controller already has home ID 0x{cur_home:08X}; restoring onto it is almost "
                "always a mistake. Use force to rewrite it anyway."
            )

        bak_sdk, cur_sdk = meta.get("sdk_version"), check.current.get("sdk_version")
        with suppress(Exception):
            if bak_sdk and _series(bak_sdk) == 500 and AwesomeVersion(bak_sdk) < MIN_500_SDK:
                check.ok = False
                check.reasons.append(
                    f"Backup is from SDK {bak_sdk}; zwave-js converts 500-series NVMs from 6.61+ only"
                )
            if cur_sdk and _series(cur_sdk) == 500 and AwesomeVersion(cur_sdk) < MIN_500_SDK:
                check.ok = False
                check.reasons.append(f"Target SDK {cur_sdk}; must be 6.61+ to receive a converted NVM")
            if cur_sdk and _series(cur_sdk) == 700 and AwesomeVersion(cur_sdk) < MIN_700_SDK:
                check.ok = False
                check.reasons.append(
                    f"Target SDK {cur_sdk}: NVM restore on 700/800-series needs 7.19.0+ "
                    f"({RECOMMENDED_700_SDK}+ recommended). Update the stick's firmware first."
                )
            elif cur_sdk and _series(cur_sdk) == 700 and AwesomeVersion(cur_sdk) < RECOMMENDED_700_SDK:
                check.warnings.append(
                    f"Target SDK {cur_sdk}; {RECOMMENDED_700_SDK}+ is recommended for reliable restores"
                )
            if _series(bak_sdk) == 700 and _series(cur_sdk) == 500:
                check.warnings.append(
                    "Restoring a 700/800 backup to a 500-series stick: Long Range nodes and 700-only data are dropped"
                )

        node_count = check.current.get("node_count") or 0
        if node_count > 1:
            check.warnings.append(f"Attached controller already has {node_count} nodes; they will be wiped")

        bak_keys = meta.get("security_keys") or {}
        cur_keys = self._keys(self._entry())
        if bak_keys and cur_keys and bak_keys != cur_keys:
            check.ok = False
            check.reasons.append(
                "S0/S2 keys configured in Z-Wave JS differ from the ones saved with this backup. "
                "Set the saved keys first or every secure device will be dead. Use force to ignore."
            )
        elif bak_keys and not cur_keys:
            check.warnings.append(
                "Backup includes S0/S2 keys but none are visible in the Z-Wave JS entry; make sure "
                "the same keys are configured in Z-Wave JS UI / the add-on before restoring."
            )
        elif not bak_keys:
            check.warnings.append(
                "This backup has no S0/S2 keys. Secure devices only work if Z-Wave JS still uses the original keys."
            )

        check.warnings.append(
            "The radio is offline during the restore (up to a few minutes). Afterwards every node is "
            "re-interviewed; battery devices come back on their next wake-up."
        )
        try:
            await self._assert_idle(self._controller())
        except RuntimeError as err:
            check.ok = False
            check.reasons.append(str(err))
        return check

    async def async_restore(self, data: bytes, meta: dict[str, Any]) -> None:
        entry = self._entry()
        if entry is None:
            raise SourceUnavailable("Z-Wave JS integration is not loaded")
        client = self._client(entry)
        driver = client.driver
        if driver is None:
            raise SourceUnavailable("Z-Wave JS driver not ready")
        ctrl = driver.controller
        await self._assert_idle(ctrl)

        from homeassistant.components.zwave_js.helpers import (  # noqa: PLC0415  (HA 2025.8+)
            CannotConnect,
            async_get_version_info,
            async_wait_for_driver_ready_event,
        )

        wait_for_driver_ready = async_wait_for_driver_ready_event(entry, driver)
        cross_series = _series(meta.get("sdk_version")) != _series(ctrl.sdk_version)
        options = {"preserveRoutes": False}
        if cross_series:
            options["preserveNeighbors"] = False

        _LOGGER.warning("Restoring Z-Wave NVM (%d bytes) to controller; radio will be offline", len(data))
        await asyncio.wait_for(ctrl.async_restore_nvm(data, options), RESTORE_TIMEOUT)
        with suppress(TimeoutError):
            await wait_for_driver_ready()

        try:
            version_info = await async_get_version_info(self.hass, entry.data["url"])
        except CannotConnect:
            _LOGGER.error(
                "Could not read the new home ID after NVM restore; Home Assistant may show an "
                "'unknown adapter' repair for Z-Wave JS that you need to confirm"
            )
        else:
            self.hass.config_entries.async_update_entry(entry, unique_id=str(version_info.home_id))
        await self.hass.config_entries.async_reload(entry.entry_id)

    # ------------------------------------------------- guided replacement

    def _any_entry(self) -> ConfigEntry | None:
        """The Z-Wave JS entry in whatever state (a dead stick leaves it retrying, not loaded)."""
        entries = [e for e in self.hass.config_entries.async_entries(ZWAVE_DOMAIN) if not e.disabled_by]
        if self.home_id is not None:
            entries = [e for e in entries if str(e.unique_id) == str(self.home_id)] or entries
        return entries[0] if entries else None

    def _addon_manager(self):
        from homeassistant.components.hassio import AddonManager  # noqa: PLC0415

        return AddonManager(self.hass, _LOGGER, "Z-Wave JS", ZWAVE_ADDON_SLUG)

    def _uses_addon(self) -> bool:
        entry = self._any_entry()
        if entry is None or not entry.data.get("use_addon"):
            return False
        try:
            from homeassistant.helpers.hassio import is_hassio  # noqa: PLC0415
        except ImportError:
            return False
        return is_hassio(self.hass)

    async def async_health(self) -> bool | None:
        entry = self._any_entry()
        if entry is None:
            return None
        if entry.state in (ConfigEntryState.SETUP_RETRY, ConfigEntryState.SETUP_ERROR):
            return False
        if entry.state is not ConfigEntryState.LOADED:
            return None  # starting up / unloading: unknown, never "down"
        try:
            client = self._client(entry)
        except SourceUnavailable:
            return None
        if not getattr(client, "connected", True):
            return False
        return getattr(client, "driver", None) is not None

    async def async_swap_plan(self) -> SwapPlan:
        entry = self._any_entry()
        if entry is None:
            return SwapPlan(supported=False, reason="The Z-Wave JS integration is not set up.")
        if self._uses_addon():
            port = entry.data.get("usb_path")
            try:
                info = await self._addon_manager().async_get_addon_info()
                port = (info.options or {}).get("device") or port
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Could not read Z-Wave JS add-on options", exc_info=True)
            return SwapPlan(
                supported=True,
                automatic=True,
                choose_port=True,
                current_port=port,
                plug_in=(
                    "A USB stick shows up under *New coordinator* a few seconds after you plug it in "
                    "(marked *just plugged in*)."
                ),
                details={"addon": ZWAVE_ADDON_SLUG},
            )
        return SwapPlan(
            supported=True,
            automatic=True,
            choose_port=False,
            stop_first=False,
            current_port=None,
            details={
                "external_server": True,
                "target": "the stick your Z-Wave JS server now uses",
                "after_failure": "Nothing was written to the new stick.",
                "note": "If Z-Wave JS shows *An unknown adapter was detected*, leave it until the replacement "
                "has finished: Radio Guard updates it when the restore succeeds.",
            },
            plug_in=(
                "Then point your Z-Wave JS server at it. In Z-Wave JS UI: *Settings > Z-Wave > Serial port*, "
                "pick the new stick and save. Radio Guard waits for the new stick to come online and does "
                "the rest."
            ),
        )

    async def async_swap_stop(self, plan: SwapPlan) -> None:
        await self.async_snapshot_ports()
        if plan.automatic and plan.details.get("addon"):
            _LOGGER.warning("Stopping the Z-Wave JS add-on to replace the controller")
            await self._addon_manager().async_stop_addon()

    async def async_wait_stopped(self, timeout: float | None) -> None:
        return None

    async def async_swap_set_port(self, port: str | None) -> None:
        if not port or not self._uses_addon():
            return
        mgr = self._addon_manager()
        info = await mgr.async_get_addon_info()
        await mgr.async_set_addon_options({**(info.options or {}), "device": port})

    async def _wait_driver(self, timeout: float | None, not_home_id: int | None = None) -> Any:
        """Wait until the integration is loaded with a ready driver (optionally on a different controller)."""

        async def _poll() -> Any:
            while True:
                entry = self._any_entry()
                if entry is not None and entry.state is ConfigEntryState.SETUP_RETRY:
                    # Don't sit out HA's retry backoff: try again now that the hardware changed.
                    self.hass.config_entries.async_schedule_reload(entry.entry_id)
                if entry is not None and entry.state is ConfigEntryState.LOADED:
                    with suppress(SourceUnavailable):
                        client = self._client(entry)
                        driver = getattr(client, "driver", None)
                        connected = getattr(client, "connected", True)
                        if connected and driver is not None and driver.controller is not None:
                            if not_home_id is None or driver.controller.home_id != not_home_id:
                                return driver.controller
                await asyncio.sleep(3)

        return await (asyncio.wait_for(_poll(), timeout) if timeout else _poll())

    async def async_swap_apply(self, data: bytes, meta: dict[str, Any], port: str | None) -> None:
        if self._uses_addon():
            await self.async_swap_set_port(port)
            await self._addon_manager().async_start_addon()
        # Wait for a controller that is NOT the backed-up network: the new stick. The old (or the
        # previous, still-cached) controller carries the backup's home ID and must never pass as done.
        try:
            await self._wait_driver(DRIVER_TIMEOUT, not_home_id=meta.get("home_id"))
        except TimeoutError as err:
            raise RuntimeError(
                "Z-Wave JS did not come up on a new controller within 5 minutes. Check the new stick is "
                "plugged in and that Z-Wave JS points at it, then run the replacement again."
            ) from err
        # Now that the new stick is live, run the real pre-flight against it (SDK, keys, idle).
        check = await self.async_restore_check(data, meta)
        if not check.ok:
            raise RuntimeError("Pre-flight on the new controller failed: " + " ".join(check.reasons))
        await self.async_restore(data, meta)

    async def async_swap_start(self, plan: SwapPlan) -> None:
        if self._uses_addon():
            mgr = self._addon_manager()
            with suppress(Exception):
                await mgr.async_start_addon()

    async def async_wait_started(self, timeout: float | None) -> None:
        await self._wait_driver(timeout)

    async def async_swap_verify(self, data: bytes, meta: dict[str, Any]) -> dict[str, Any]:
        await self._wait_driver(DRIVER_TIMEOUT)
        ident = await self.async_identity()
        want = meta.get("home_id")
        if want is not None and ident.get("home_id") != want:
            raise RuntimeError(
                f"The controller came back with home ID {ident.get('home_id_hex')} instead of the backup's "
                f"{meta.get('home_id_hex') or want}. Nothing else was changed; run the replacement again."
            )
        return ident

    async def async_check_devices(self, timeout: float) -> DeviceReport | None:
        from homeassistant.util import dt as dt_util  # noqa: PLC0415

        ctrl = self._controller()
        started = dt_util.utcnow()
        nodes = [n for n in ctrl.nodes.values() if n.node_id != ctrl.own_node_id]
        if not nodes:
            return None

        def _name(n) -> str:
            return getattr(n, "name", None) or getattr(n, "label", None) or f"Node {n.node_id}"

        mains = [n for n in nodes if n.is_listening or getattr(n, "is_frequent_listening", False)]
        battery = [n for n in nodes if n not in mains]

        async def _ping(n) -> bool:
            try:
                return bool(await asyncio.wait_for(n.async_ping(), PING_TIMEOUT))
            except Exception:  # noqa: BLE001
                return False

        results = await asyncio.gather(*(_ping(n) for n in mains))
        back_mains = [n for n, ok in zip(mains, results, strict=True) if ok]

        def _heard(n) -> bool:
            seen = getattr(n, "last_seen", None)
            return seen is not None and seen >= started

        deadline = asyncio.get_running_loop().time() + min(30, timeout)
        while (
            battery and not all(_heard(n) for n in battery) and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(2)
        back_battery = [n for n in battery if _heard(n)]
        return DeviceReport(
            mains_total=len(mains),
            mains_back=len(back_mains),
            battery_total=len(battery),
            battery_back=len(back_battery),
            missing_mains=sorted(_name(n) for n in mains if n not in back_mains),
            missing_battery=sorted(_name(n) for n in battery if n not in back_battery),
        )
