"""ZHA backup source.

Backup: zigpy's ``backups.create_backup()`` on the live gateway (safe while the
network runs; ZHA does the same on a timer). Stored in zigpy's native format,
which is lossless and what ZHA's own migration wizard uses.
``NetworkBackup.from_dict`` also reads OCBF, so a Z2M ``coordinator_backup.json``
can be restored through this path too.

Restore: never on the live gateway. ZHA's own flows unload the config entry,
open a throw-away zigpy app, write the network, and set the entry up again;
we reuse ``ZhaRadioManager`` so the semantics stay identical.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from ..const import RESTORE_TIMEOUT, SOURCE_NAMES, SOURCE_ZHA, ZHA_BACKUP_TIMEOUT
from .base import BackupPayload, BackupSource, RestoreCheck, SourceUnavailable
from .ident import norm_hex

_LOGGER = logging.getLogger(__name__)

# Frame counter headroom zigpy adds on restore. If the live network has moved
# further than this since the backup, devices drop the coordinator's frames.
DEFAULT_COUNTER_INCREMENT = 10000


class ZHASource(BackupSource):
    """Backup via ZHA's zigpy application controller."""

    id = SOURCE_ZHA
    name = SOURCE_NAMES[SOURCE_ZHA]

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass)

    def available(self) -> bool:
        return "zha" in self.hass.config.components and self._entry() is not None

    def _entry(self) -> ConfigEntry | None:
        try:
            from homeassistant.components.zha.helpers import get_config_entry  # noqa: PLC0415

            return get_config_entry(self.hass)
        except Exception:  # noqa: BLE001
            return None

    def _app(self):
        try:
            from homeassistant.components.zha.helpers import get_zha_gateway  # noqa: PLC0415
        except ImportError as err:
            raise SourceUnavailable("ZHA is not installed") from err
        try:
            gateway = get_zha_gateway(self.hass)
        except ValueError as err:
            raise SourceUnavailable("ZHA gateway is not running") from err
        app = getattr(gateway, "application_controller", None)
        if app is None:
            raise SourceUnavailable("ZHA application controller not initialised yet")
        return app

    # ---------------------------------------------------------- identity

    async def async_identity(self) -> dict[str, Any]:
        app = self._app()
        entry = self._entry()
        state = app.state
        ni = state.network_info
        node = state.node_info
        return {
            "ieee": norm_hex(node.ieee) if node and node.ieee else None,
            "pan_id": f"0x{int(ni.pan_id):04x}" if ni and ni.pan_id is not None else None,
            "extended_pan_id": norm_hex(ni.extended_pan_id) if ni and ni.extended_pan_id else None,
            "channel": ni.channel if ni else None,
            "nwk_update_id": ni.nwk_update_id if ni else None,
            "frame_counter": ni.network_key.tx_counter if ni and ni.network_key else None,
            "radio_type": entry.data.get("radio_type") if entry else None,
            "device_path": (entry.data.get("device") or {}).get("path") if entry else None,
            "stack": next(iter(ni.stack_specific or {}), None) if ni else None,
            "model": getattr(node, "model", None),
            "manufacturer": getattr(node, "manufacturer", None),
            "version": getattr(node, "version", None),
            "device_count": len(ni.nwk_addresses)
            if ni and ni.nwk_addresses
            else len(ni.children or [])
            if ni
            else None,
        }

    # ------------------------------------------------------------ backup

    async def async_backup(self) -> BackupPayload:
        app = self._app()
        backup = await asyncio.wait_for(app.backups.create_backup(load_devices=True), ZHA_BACKUP_TIMEOUT)
        data = json.dumps(backup.as_dict(), indent=2, sort_keys=True).encode()
        ni, node = backup.network_info, backup.node_info
        meta: dict[str, Any] = {
            "format": "zigpy-network-backup",
            "format_version": backup.version,
            "is_complete": bool(backup.is_complete()),
            "ieee": norm_hex(node.ieee),
            "pan_id": f"0x{int(ni.pan_id):04x}",
            "extended_pan_id": norm_hex(ni.extended_pan_id),
            "channel": ni.channel,
            "nwk_update_id": ni.nwk_update_id,
            "device_count": len(ni.nwk_addresses) or len(ni.children),
            "source": ni.source,
            "stack": next(iter(ni.stack_specific or {}), None),
            "model": node.model,
            "manufacturer": node.manufacturer,
            "version": node.version,
            "frame_counter": ni.network_key.tx_counter if ni.network_key else None,
        }
        try:
            meta.update(
                {k: v for k, v in (await self.async_identity()).items() if k in ("radio_type", "device_path")}
            )
        except Exception:  # noqa: BLE001
            pass
        return BackupPayload(data=data, ext=".json", meta=meta)

    def _parse(self, data: bytes):
        from zigpy.backups import NetworkBackup  # noqa: PLC0415

        return NetworkBackup.from_dict(json.loads(data))

    # ----------------------------------------------------------- restore

    async def async_restore_check(self, data: bytes, meta: dict[str, Any]) -> RestoreCheck:
        check = RestoreCheck(ok=True, backup=meta)
        try:
            backup = await self.hass.async_add_executor_job(self._parse, data)
        except Exception as err:  # noqa: BLE001
            return RestoreCheck(ok=False, reasons=[f"Backup is not a valid zigpy/OCBF file: {err}"])
        if not self.available():
            check.ok = False
            check.reasons.append(
                "ZHA is not loaded. Add or migrate the ZHA integration to the new radio first."
            )
            return check
        try:
            check.current = await self.async_identity()
        except Exception as err:  # noqa: BLE001
            check.warnings.append(f"Could not read the attached radio: {err}")

        bak_ieee = norm_hex(backup.node_info.ieee)
        cur_ieee = check.current.get("ieee")
        if cur_ieee and bak_ieee and cur_ieee == bak_ieee:
            same_net = (
                check.current.get("extended_pan_id") == norm_hex(backup.network_info.extended_pan_id)
                and check.current.get("pan_id") == f"0x{int(backup.network_info.pan_id):04x}"
            )
            if same_net:
                check.ok = False
                check.reasons.append(
                    "Attached radio already carries this backup's network and IEEE. Restoring is almost "
                    "always a mistake here; use force to rewrite it anyway."
                )
        elif cur_ieee and bak_ieee and check.current.get("radio_type") == "ezsp":
            check.warnings.append(
                "Silicon Labs radio with a different IEEE than the backup. The restore writes the backup's "
                "IEEE to the radio; on some firmware that is a one-time operation and needs force=true."
            )
        if not backup.is_complete():
            check.ok = False
            check.reasons.append("Backup is incomplete (zigpy is_complete() is false); zigpy will refuse it.")
        cur_fc = check.current.get("frame_counter")
        bak_fc = backup.network_info.network_key.tx_counter if backup.network_info.network_key else None
        if cur_fc is not None and bak_fc is not None and cur_fc > bak_fc + DEFAULT_COUNTER_INCREMENT:
            check.warnings.append(
                f"Live network frame counter ({cur_fc}) is far ahead of the backup ({bak_fc}); devices may "
                "drop frames until they rejoin. A newer backup is better if you have one."
            )
        check.warnings.append(
            "ZHA will be unloaded during the restore (a minute or two) and reloaded afterwards. Routers "
            "reconnect within minutes; sleepy end devices on their next wake-up."
        )
        check.warnings.append(
            "Never power the old coordinator again on this network; it carries the same IEEE."
        )
        return check

    async def async_restore(self, data: bytes, meta: dict[str, Any]) -> None:
        from homeassistant.components.zha.radio_manager import ZhaRadioManager  # noqa: PLC0415
        from zigpy.exceptions import (  # noqa: PLC0415
            CannotWriteNetworkSettings,
            DestructiveWriteNetworkSettings,
        )

        backup = await self.hass.async_add_executor_job(self._parse, data)
        entry = self._entry()
        if entry is None:
            raise SourceUnavailable("ZHA config entry not found")
        mgr = ZhaRadioManager.from_config_entry(self.hass, entry)
        overwrite_ieee = bool(meta.get("allow_ieee_overwrite"))

        _LOGGER.warning("Unloading ZHA to restore network backup")
        if not await self.hass.config_entries.async_unload(entry.entry_id):
            raise RuntimeError("Could not unload ZHA")
        try:
            await mgr.async_load_network_settings()
            try:
                await asyncio.wait_for(
                    mgr.restore_backup(backup, overwrite_ieee=overwrite_ieee), RESTORE_TIMEOUT
                )
            except DestructiveWriteNetworkSettings as err:
                raise RuntimeError(
                    "This Silicon Labs radio can have its IEEE address written only once with its current "
                    "firmware. Update the firmware, or re-run the restore with force=true to burn it permanently."
                ) from err
            except CannotWriteNetworkSettings as err:
                raise RuntimeError(f"Radio refused the backup: {err}") from err
        finally:
            _LOGGER.info("Reloading ZHA")
            await self.hass.config_entries.async_setup(entry.entry_id)
