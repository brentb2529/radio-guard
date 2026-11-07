"""Zigbee2MQTT backup source.

Backup: publish to ``<base>/bridge/request/backup``; Z2M answers on
``<base>/bridge/response/backup`` with a base64 zip containing
``coordinator_backup.json`` (Open Coordinator Backup Format), the Z2M
``database.db`` and ``configuration.yaml``.

Restore: Z2M has no restore API. On startup it reads ``coordinator_backup.json``
from its data directory and, if the attached coordinator's network does not
match, writes the backup to the coordinator. So a restore is:

    1. stop Z2M
    2. drop ``coordinator_backup.json`` (and database.db) into its data dir
    3. start Z2M

When Z2M runs as the official Supervisor add-on we can do all three from
here. Otherwise we stage the files and tell the user where they are.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import zipfile
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant, callback

from ..const import SOURCE_NAMES, SOURCE_Z2M, Z2M_BACKUP_TIMEOUT
from .base import BackupPayload, BackupSource, RestoreCheck, SourceUnavailable

_LOGGER = logging.getLogger(__name__)

Z2M_ADDON_SLUG = "45df7312_zigbee2mqtt"
Z2M_ADDON_SLUG_EDGE = "45df7312_zigbee2mqtt_edge"
CANDIDATE_DATA_DIRS = (
    "/addon_configs/{slug}",
    "/config/zigbee2mqtt",
    "/share/zigbee2mqtt",
)


class Zigbee2MQTTSource(BackupSource):
    """Backup via the Z2M MQTT bridge API."""

    id = SOURCE_Z2M
    name = SOURCE_NAMES[SOURCE_Z2M]

    def __init__(self, hass: HomeAssistant, base_topic: str) -> None:
        super().__init__(hass)
        self.base_topic = base_topic.rstrip("/")

    # ------------------------------------------------------------ helpers

    def available(self) -> bool:
        return "mqtt" in self.hass.config.components

    async def _request(self, name: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        """Send a bridge request and wait for its response."""
        from homeassistant.components import mqtt  # noqa: PLC0415

        req = f"{self.base_topic}/bridge/request/{name}"
        resp = f"{self.base_topic}/bridge/response/{name}"
        fut: asyncio.Future[dict[str, Any]] = self.hass.loop.create_future()

        @callback
        def _on_msg(msg: mqtt.ReceiveMessage) -> None:
            if fut.done():
                return
            try:
                fut.set_result(json.loads(msg.payload))
            except ValueError as err:
                fut.set_exception(ValueError(f"Bad JSON from Z2M: {err}"))

        unsub = await mqtt.async_subscribe(self.hass, resp, _on_msg, qos=1)
        try:
            await mqtt.async_publish(self.hass, req, json.dumps(payload), qos=1)
            return await asyncio.wait_for(fut, timeout)
        finally:
            unsub()

    async def _bridge_info(self) -> dict[str, Any]:
        """Read the retained bridge/info message."""
        from homeassistant.components import mqtt  # noqa: PLC0415

        fut: asyncio.Future[dict[str, Any]] = self.hass.loop.create_future()

        @callback
        def _on_msg(msg: mqtt.ReceiveMessage) -> None:
            if not fut.done():
                try:
                    fut.set_result(json.loads(msg.payload))
                except ValueError:
                    fut.set_result({})

        unsub = await mqtt.async_subscribe(self.hass, f"{self.base_topic}/bridge/info", _on_msg, qos=1)
        try:
            return await asyncio.wait_for(fut, 5)
        except TimeoutError:
            return {}
        finally:
            unsub()

    # ---------------------------------------------------------- identity

    async def async_identity(self) -> dict[str, Any]:
        if not self.available():
            raise SourceUnavailable("MQTT integration is not loaded")
        info = await self._bridge_info()
        coord = info.get("coordinator") or {}
        net = info.get("network") or {}
        return {
            "ieee": coord.get("ieee_address"),
            "coordinator_type": coord.get("type"),
            "coordinator_version": (coord.get("meta") or {}).get("revision"),
            "pan_id": net.get("pan_id"),
            "extended_pan_id": net.get("extended_pan_id"),
            "channel": net.get("channel"),
            "z2m_version": info.get("version"),
            "zigbee_herdsman": info.get("zigbee_herdsman_version"),
        }

    # ------------------------------------------------------------ backup

    async def async_backup(self) -> BackupPayload:
        if not self.available():
            raise SourceUnavailable("MQTT integration is not loaded")
        resp = await self._request("backup", {}, Z2M_BACKUP_TIMEOUT)
        if resp.get("status") != "ok":
            raise RuntimeError(f"Z2M backup failed: {resp.get('error', resp)}")
        zip_b64 = (resp.get("data") or {}).get("zip")
        if not zip_b64:
            raise RuntimeError("Z2M backup response had no zip payload")
        data = base64.b64decode(zip_b64)

        meta = await self.hass.async_add_executor_job(self._inspect_zip, data)
        try:
            meta.update(await self.async_identity())
        except Exception:  # noqa: BLE001 - identity is best-effort
            _LOGGER.debug("Could not read Z2M bridge info for metadata", exc_info=True)
        return BackupPayload(data=data, ext=".zip", meta=meta)

    @staticmethod
    def _inspect_zip(data: bytes) -> dict[str, Any]:
        meta: dict[str, Any] = {"format": "zigbee2mqtt-zip"}
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            meta["members"] = zf.namelist()
            if "coordinator_backup.json" in zf.namelist():
                ocbf = json.loads(zf.read("coordinator_backup.json"))
                meta.update(_summarize_ocbf(ocbf))
        return meta

    # ----------------------------------------------------------- restore

    def _data_dirs(self) -> list[Path]:
        dirs: list[Path] = []
        for tmpl in CANDIDATE_DATA_DIRS:
            for slug in (Z2M_ADDON_SLUG, Z2M_ADDON_SLUG_EDGE):
                p = Path(tmpl.format(slug=slug))
                if p not in dirs:
                    dirs.append(p)
        return dirs

    def _find_data_dir(self) -> Path | None:
        for p in self._data_dirs():
            if (p / "configuration.yaml").exists():
                return p
        return None

    async def _addon_manager(self):
        """Return an AddonManager for the installed Z2M add-on, if Supervisor is present."""
        try:
            from homeassistant.components.hassio.addon_manager import (  # noqa: PLC0415
                AddonManager,
                AddonState,
            )
            from homeassistant.helpers.hassio import is_hassio  # noqa: PLC0415
        except ImportError:
            return None
        if not is_hassio(self.hass):
            return None
        for slug in (Z2M_ADDON_SLUG, Z2M_ADDON_SLUG_EDGE):
            mgr = AddonManager(self.hass, _LOGGER, "Zigbee2MQTT", slug)
            try:
                info = await mgr.async_get_addon_info()
            except Exception:  # noqa: BLE001
                continue
            if info.state is not AddonState.NOT_INSTALLED:
                return mgr
        return None

    async def async_restore_check(self, data: bytes, meta: dict[str, Any]) -> RestoreCheck:
        check = RestoreCheck(ok=True, backup=meta)
        try:
            check.current = await self.async_identity()
        except Exception as err:  # noqa: BLE001
            check.warnings.append(f"Could not read live coordinator identity: {err}")

        zip_meta = await self.hass.async_add_executor_job(self._inspect_zip, data)
        if "coordinator_backup.json" not in zip_meta.get("members", []):
            check.ok = False
            check.reasons.append("Backup zip does not contain coordinator_backup.json")

        data_dir = await self.hass.async_add_executor_job(self._find_data_dir)
        mgr = await self._addon_manager()
        if mgr and data_dir:
            check.warnings.append(f"Will stop add-on {mgr.addon_slug}, write {data_dir}, then start it")
        elif data_dir:
            check.warnings.append(
                f"Z2M data dir found at {data_dir} but no Supervisor add-on; you must stop/start Z2M yourself"
            )
        else:
            check.warnings.append(
                "Could not locate the Z2M data directory; files will be staged for manual restore"
            )

        cur_ieee = check.current.get("ieee")
        bak_ieee = zip_meta.get("ieee")
        if cur_ieee and bak_ieee and cur_ieee.lower() == bak_ieee.lower():
            check.warnings.append(
                "Live coordinator already has the backup's IEEE address; restore is probably unnecessary"
            )
        return check

    async def async_restore(self, data: bytes, meta: dict[str, Any]) -> None:
        mgr = await self._addon_manager()
        data_dir = await self.hass.async_add_executor_job(self._find_data_dir)

        if mgr and data_dir:
            _LOGGER.info("Stopping Z2M add-on %s for restore", mgr.addon_slug)
            await mgr.async_stop_addon()
            try:
                await self.hass.async_add_executor_job(self._write_restore_files, data, data_dir)
            finally:
                _LOGGER.info("Starting Z2M add-on %s", mgr.addon_slug)
                await mgr.async_start_addon()
            return

        staging = Path(self.hass.config.path("radio_guard", "zigbee2mqtt", "restore"))
        await self.hass.async_add_executor_job(self._write_restore_files, data, staging)
        raise RuntimeError(
            "Zigbee2MQTT is not running as a Supervisor add-on. Restore files were staged in "
            f"{staging}. Stop Z2M, copy coordinator_backup.json (and database.db) into its data "
            "directory, then start Z2M; it will write the backup to the new coordinator on boot."
        )

    @staticmethod
    def _write_restore_files(data: bytes, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for member in ("coordinator_backup.json", "database.db"):
                if member in zf.namelist():
                    (dest / member).write_bytes(zf.read(member))


def _summarize_ocbf(ocbf: dict[str, Any]) -> dict[str, Any]:
    """Pull the useful identity fields out of an Open Coordinator Backup."""
    out: dict[str, Any] = {"ocbf_version": ocbf.get("metadata", {}).get("version")}
    md = ocbf.get("metadata") or {}
    out["ocbf_format"] = md.get("format")
    out["ocbf_source"] = md.get("source")
    out["pan_id"] = ocbf.get("pan_id")
    out["extended_pan_id"] = ocbf.get("extended_pan_id")
    out["channel"] = ocbf.get("channel")
    out["ieee"] = ocbf.get("coordinator_ieee")
    out["nwk_update_id"] = ocbf.get("nwk_update_id")
    out["device_count"] = len(ocbf.get("devices") or [])
    return out
