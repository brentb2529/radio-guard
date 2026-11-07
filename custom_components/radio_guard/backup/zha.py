"""ZHA backup source.

ZHA exposes zigpy's backup machinery on the gateway's application
controller. ``create_backup`` returns an Open Coordinator Backup; ``restore_backup``
writes one to whatever radio is attached, converting between stacks (TI,
Silabs, ConBee...) as needed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from homeassistant.core import HomeAssistant

from ..const import RESTORE_TIMEOUT, SOURCE_NAMES, SOURCE_ZHA, ZHA_BACKUP_TIMEOUT
from .base import BackupPayload, BackupSource, RestoreCheck, SourceUnavailable

_LOGGER = logging.getLogger(__name__)


class ZHASource(BackupSource):
    """Backup via ZHA's zigpy application controller."""

    id = SOURCE_ZHA
    name = SOURCE_NAMES[SOURCE_ZHA]

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass)

    def available(self) -> bool:
        return "zha" in self.hass.config.components

    def _app(self):
        try:
            from homeassistant.components.zha.helpers import get_zha_gateway  # noqa: PLC0415
        except ImportError as err:
            raise SourceUnavailable("ZHA is not installed") from err
        try:
            gateway = get_zha_gateway(self.hass)
        except Exception as err:  # noqa: BLE001
            raise SourceUnavailable(f"ZHA gateway not ready: {err}") from err
        app = getattr(gateway, "application_controller", None)
        if app is None:
            raise SourceUnavailable("ZHA application controller not available")
        return app

    async def async_identity(self) -> dict[str, Any]:
        app = self._app()
        state = app.state
        ni = state.network_info
        node = state.node_info
        return {
            "ieee": str(node.ieee) if node and node.ieee else None,
            "pan_id": f"0x{ni.pan_id:04X}" if ni and ni.pan_id is not None else None,
            "extended_pan_id": str(ni.extended_pan_id) if ni else None,
            "channel": ni.channel if ni else None,
            "nwk_update_id": ni.nwk_update_id if ni else None,
            "radio_type": type(app).__module__,
            "model": getattr(node, "model", None),
            "manufacturer": getattr(node, "manufacturer", None),
            "version": getattr(node, "version", None),
        }

    async def async_backup(self) -> BackupPayload:
        app = self._app()
        backup = await asyncio.wait_for(app.backups.create_backup(load_devices=True), ZHA_BACKUP_TIMEOUT)
        as_dict = backup.as_dict()
        data = json.dumps(as_dict, indent=2, sort_keys=True).encode()
        meta = {
            "format": "ocbf",
            "is_complete": bool(backup.is_complete()),
            "ieee": as_dict.get("coordinator_ieee"),
            "pan_id": as_dict.get("pan_id"),
            "extended_pan_id": as_dict.get("extended_pan_id"),
            "channel": as_dict.get("channel"),
            "nwk_update_id": as_dict.get("nwk_update_id"),
            "device_count": len(as_dict.get("devices") or []),
            "ocbf_version": (as_dict.get("metadata") or {}).get("version"),
            "ocbf_source": (as_dict.get("metadata") or {}).get("source"),
        }
        return BackupPayload(data=data, ext=".json", meta=meta)

    def _parse(self, data: bytes):
        from zigpy.backups import NetworkBackup  # noqa: PLC0415

        return NetworkBackup.from_dict(json.loads(data))

    async def async_restore_check(self, data: bytes, meta: dict[str, Any]) -> RestoreCheck:
        check = RestoreCheck(ok=True, backup=meta)
        try:
            backup = await self.hass.async_add_executor_job(self._parse, data)
        except Exception as err:  # noqa: BLE001
            return RestoreCheck(ok=False, reasons=[f"Backup is not a valid OCBF file: {err}"])
        try:
            check.current = await self.async_identity()
        except Exception as err:  # noqa: BLE001
            check.warnings.append(f"Could not read live coordinator: {err}")

        bak_ieee = str(backup.node_info.ieee)
        cur_ieee = check.current.get("ieee")
        if cur_ieee and cur_ieee.lower() == bak_ieee.lower():
            check.warnings.append(
                "Live coordinator already carries this backup's IEEE; restore may be unnecessary"
            )
        if not backup.is_complete():
            check.warnings.append("Backup is marked incomplete by zigpy")
        return check

    async def async_restore(self, data: bytes, meta: dict[str, Any]) -> None:
        app = self._app()
        backup = await self.hass.async_add_executor_job(self._parse, data)
        # Silabs radios refuse to overwrite the EUI64 unless told to; a
        # replacement coordinator needs exactly that.
        try:
            backup.network_info.stack_specific.setdefault("ezsp", {})[
                "i_understand_i_can_update_eui64_only_once_and_i_still_want_to_do_it"
            ] = True
        except Exception:  # noqa: BLE001
            pass
        _LOGGER.info("Restoring ZHA network backup to attached coordinator")
        await asyncio.wait_for(app.backups.restore_backup(backup), RESTORE_TIMEOUT)
