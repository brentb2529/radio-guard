"""Z-Wave JS backup source.

Uses the zwave-js-server client that the Z-Wave JS integration already holds,
so this works for the Supervisor add-on, Z-Wave JS UI, or a bare
``zwave-js-server`` on the network. NVM backup/restore is handled by
zwave-js itself, including conversion between 500/700/800-series formats.
"""

from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant

from ..const import RESTORE_TIMEOUT, SOURCE_NAMES, SOURCE_ZWAVE, ZWAVE_BACKUP_TIMEOUT
from .base import BackupPayload, BackupSource, RestoreCheck, SourceUnavailable

_LOGGER = logging.getLogger(__name__)
ZWAVE_DOMAIN = "zwave_js"


class ZWaveJSSource(BackupSource):
    """Backup via the Z-Wave JS integration's server client."""

    id = SOURCE_ZWAVE
    name = SOURCE_NAMES[SOURCE_ZWAVE]

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass)

    def available(self) -> bool:
        return self._entry() is not None

    def _entry(self) -> ConfigEntry | None:
        for entry in self.hass.config_entries.async_entries(ZWAVE_DOMAIN):
            if entry.state is ConfigEntryState.LOADED:
                return entry
        return None

    def _controller(self):
        entry = self._entry()
        if entry is None:
            raise SourceUnavailable("Z-Wave JS integration is not loaded")
        runtime = getattr(entry, "runtime_data", None)
        client = getattr(runtime, "client", None)
        if client is None:
            # Older cores kept it in hass.data
            try:
                client = self.hass.data[ZWAVE_DOMAIN][entry.entry_id]["client"]
            except (KeyError, TypeError) as err:
                raise SourceUnavailable("Z-Wave JS client not found") from err
        driver = getattr(client, "driver", None)
        if driver is None or driver.controller is None:
            raise SourceUnavailable("Z-Wave JS driver not ready")
        return driver.controller

    async def async_identity(self) -> dict[str, Any]:
        ctrl = self._controller()
        return {
            "home_id": ctrl.home_id,
            "own_node_id": ctrl.own_node_id,
            "sdk_version": ctrl.sdk_version,
            "firmware_version": ctrl.firmware_version,
            "manufacturer_id": ctrl.manufacturer_id,
            "product_type": ctrl.product_type,
            "product_id": ctrl.product_id,
            "controller_type": getattr(ctrl, "controller_type", None),
            "node_count": len(ctrl.nodes),
        }

    async def async_backup(self) -> BackupPayload:
        ctrl = self._controller()
        b64 = await asyncio.wait_for(ctrl.async_backup_nvm_raw_base64(), ZWAVE_BACKUP_TIMEOUT)
        data = base64.b64decode(b64)
        meta = {"format": "zwave-nvm-raw", **await self.async_identity()}
        return BackupPayload(data=data, ext=".nvm", meta=meta)

    async def async_restore_check(self, data: bytes, meta: dict[str, Any]) -> RestoreCheck:
        check = RestoreCheck(ok=True, backup=meta)
        if not data:
            return RestoreCheck(ok=False, reasons=["Backup file is empty"])
        try:
            check.current = await self.async_identity()
        except Exception as err:  # noqa: BLE001
            check.warnings.append(f"Could not read live controller: {err}")
            return check

        cur_home = check.current.get("home_id")
        bak_home = meta.get("home_id")
        if cur_home and bak_home and cur_home == bak_home:
            check.warnings.append(
                f"Live controller already has home ID {cur_home}; restore may be unnecessary"
            )
        bak_sdk = meta.get("sdk_version")
        cur_sdk = check.current.get("sdk_version")
        if bak_sdk and cur_sdk:
            check.warnings.append(
                f"Backup SDK {bak_sdk} -> target SDK {cur_sdk}; zwave-js converts "
                "forward (500->700->800) but cannot restore to an older series"
            )
        check.warnings.append(
            "Restore wipes the target controller. Routes are rebuilt; battery "
            "devices re-report on their next wake."
        )
        return check

    async def async_restore(self, data: bytes, meta: dict[str, Any]) -> None:
        entry = self._entry()
        ctrl = self._controller()
        _LOGGER.info("Restoring Z-Wave NVM to controller")
        await asyncio.wait_for(
            ctrl.async_restore_nvm_base64(base64.b64encode(data).decode(), {"preserveRoutes": False}),
            RESTORE_TIMEOUT,
        )
        # Home ID changed; let the integration pick it up the same way core does.
        if entry is not None:
            with suppress(Exception):
                await self.hass.config_entries.async_reload(entry.entry_id)
