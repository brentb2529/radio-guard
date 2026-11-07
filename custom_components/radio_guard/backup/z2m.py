"""Zigbee2MQTT backup source.

Backup: publish ``{"transaction": ...}`` to ``<base>/bridge/request/backup``;
Z2M answers on ``<base>/bridge/response/backup`` with a base64 zip of its whole
data directory (minus log/ and ota/). For zstack and ember adapters that
includes ``coordinator_backup.json`` (Open Coordinator Backup Format). The zip
contains the network key: treat it as a secret.

Restore: Z2M has no restore API. On startup zigbee-herdsman reads
``coordinator_backup.json`` from the data dir and writes it to the adapter,
but ONLY when ``configuration.yaml``'s ``advanced.pan_id / ext_pan_id /
network_key / channel`` equal the backup's. Otherwise it silently forms a new
network and wipes ``database.db``. So the restore is:

    1. stop Z2M
    2. patch advanced.* in the live configuration.yaml to match the backup
       (keeping the user's serial/mqtt settings for the NEW stick)
    3. drop coordinator_backup.json, database.db and the other data files
    4. start Z2M, wait for bridge/state online, verify bridge/info matches

When Z2M is the Supervisor add-on we do all of that; otherwise the files are
staged and the user gets precise manual steps.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import zipfile
from pathlib import Path
from typing import Any

import yaml
from homeassistant.core import HomeAssistant, callback

from ..const import SOURCE_NAMES, SOURCE_Z2M, Z2M_BACKUP_TIMEOUT
from .base import BackupPayload, BackupSource, RestoreCheck, RestoreStaged, SourceUnavailable
from .ident import norm_hex, norm_pan_id

_LOGGER = logging.getLogger(__name__)

Z2M_ADDON_SLUG = "45df7312_zigbee2mqtt"
Z2M_ADDON_SLUG_EDGE = "45df7312_zigbee2mqtt_edge"
DEFAULT_ADDON_DATA_PATH = "/config/zigbee2mqtt"
RESTORE_SKIP = {"configuration.yaml", "secret.yaml", "configuration.yaml.bk"}
RESTORE_SKIP_DIRS = ("log/", "ota/")
ONLINE_TIMEOUT = 120
SUPPORTS_BACKUP = {"zstack", "ember"}


class _SafeLoader(yaml.SafeLoader):
    """Tolerate !secret and other tags we cannot resolve."""


_SafeLoader.add_multi_constructor("!", lambda loader, suffix, node: f"!{suffix} {getattr(node, 'value', '')}")


class Zigbee2MQTTSource(BackupSource):
    """Backup via the Z2M MQTT bridge API."""

    id = SOURCE_Z2M
    name = SOURCE_NAMES[SOURCE_Z2M]

    def __init__(self, hass: HomeAssistant, base_topic: str) -> None:
        super().__init__(hass)
        self.base_topic = base_topic.rstrip("/")
        self._info: dict[str, Any] = {}
        self._state: str | None = None
        self._info_event = asyncio.Event()
        self._online_event = asyncio.Event()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._unsubs: list = []
        self._seq = 0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ helpers

    def available(self) -> bool:
        return "mqtt" in self.hass.config.components

    async def async_setup(self) -> None:
        """Hold subscriptions for the source's lifetime.

        HA debounces MQTT subscriptions, so subscribing and publishing a request
        in the same breath can miss the reply. Subscribing once up front also
        gives us the retained bridge/info and bridge/state for free.
        """
        if self._unsubs or not self.available():
            return
        from homeassistant.components import mqtt  # noqa: PLC0415

        await mqtt.async_wait_for_mqtt_client(self.hass)

        @callback
        def _on_info(msg: mqtt.ReceiveMessage) -> None:
            try:
                self._info = json.loads(msg.payload) or {}
            except ValueError:
                return
            self._info_event.set()

        @callback
        def _on_state(msg: mqtt.ReceiveMessage) -> None:
            payload = msg.payload
            try:
                state = json.loads(payload).get("state")
            except (ValueError, AttributeError):
                state = str(payload)
            self._state = state
            if state == "online":
                self._online_event.set()
            else:
                self._online_event.clear()
                self._info_event.clear()

        @callback
        def _on_backup_resp(msg: mqtt.ReceiveMessage) -> None:
            try:
                resp = json.loads(msg.payload)
            except ValueError:
                _LOGGER.warning("Bad JSON on %s", msg.topic)
                return
            txn = str(resp.get("transaction", ""))
            fut = self._pending.pop(txn, None)
            if fut is None and len(self._pending) == 1:
                fut = self._pending.pop(next(iter(self._pending)))
            if fut is not None and not fut.done():
                fut.set_result(resp)

        base = self.base_topic
        self._unsubs.append(await mqtt.async_subscribe(self.hass, f"{base}/bridge/info", _on_info, qos=1))
        self._unsubs.append(await mqtt.async_subscribe(self.hass, f"{base}/bridge/state", _on_state, qos=1))
        self._unsubs.append(
            await mqtt.async_subscribe(self.hass, f"{base}/bridge/response/backup", _on_backup_resp, qos=1)
        )

    async def async_teardown(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

    async def _request(self, name: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        """Send a bridge request and wait for its correlated response."""
        from homeassistant.components import mqtt  # noqa: PLC0415

        await self.async_setup()
        self._seq += 1
        txn = f"rg-{self._seq}"
        fut: asyncio.Future[dict[str, Any]] = self.hass.loop.create_future()
        self._pending[txn] = fut
        try:
            await mqtt.async_publish(
                self.hass,
                f"{self.base_topic}/bridge/request/{name}",
                json.dumps({**payload, "transaction": txn}),
                qos=1,
            )
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError as err:
            raise TimeoutError(
                f"No response from Zigbee2MQTT on {self.base_topic}/bridge/response/{name} within "
                f"{timeout:.0f}s (is Z2M running and using base topic '{self.base_topic}'?)"
            ) from err
        finally:
            self._pending.pop(txn, None)

    async def _bridge_info(self) -> dict[str, Any]:
        await self.async_setup()
        if not self._info:
            try:
                await asyncio.wait_for(self._info_event.wait(), 5)
            except TimeoutError:
                return {}
        return self._info

    # ---------------------------------------------------------- identity

    async def async_identity(self) -> dict[str, Any]:
        if not self.available():
            raise SourceUnavailable("MQTT integration is not loaded")
        info = await self._bridge_info()
        if not info:
            raise SourceUnavailable(
                f"No retained {self.base_topic}/bridge/info on the broker; Zigbee2MQTT may be offline"
            )
        coord = info.get("coordinator") or {}
        net = info.get("network") or {}
        herdsman = info.get("zigbee_herdsman")
        return {
            "ieee": norm_hex(coord.get("ieee_address")),
            "coordinator_type": coord.get("type"),
            "coordinator_version": (coord.get("meta") or {}).get("revision"),
            "pan_id": norm_pan_id(net.get("pan_id")),
            "extended_pan_id": norm_hex(net.get("extended_pan_id")),
            "channel": net.get("channel"),
            "z2m_version": info.get("version"),
            "zigbee_herdsman": herdsman.get("version") if isinstance(herdsman, dict) else herdsman,
            "bridge_state": self._state,
            "device_count": len(info.get("devices") or []) if info.get("devices") else None,
        }

    # ------------------------------------------------------------ backup

    async def async_backup(self) -> BackupPayload:
        if not self.available():
            raise SourceUnavailable("MQTT integration is not loaded")
        async with self._lock:
            resp = await self._request("backup", {}, Z2M_BACKUP_TIMEOUT)
        if resp.get("status") != "ok":
            raise RuntimeError(f"Z2M backup failed: {resp.get('error', resp)}")
        zip_b64 = (resp.get("data") or {}).get("zip")
        if not zip_b64:
            raise RuntimeError("Z2M backup response had no zip payload")
        data = base64.b64decode(zip_b64)

        meta = await self.hass.async_add_executor_job(self._inspect_zip, data)
        try:
            ident = await self.async_identity()
            for key in ("coordinator_type", "coordinator_version", "z2m_version", "zigbee_herdsman"):
                meta[key] = ident.get(key)
            # Trust the OCBF for network identity; fall back to bridge/info.
            for key in ("ieee", "pan_id", "extended_pan_id", "channel"):
                meta.setdefault(key, ident.get(key))
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not read Z2M bridge info for metadata", exc_info=True)
        if "coordinator_backup.json" not in meta.get("members", []):
            meta["warning"] = (
                "No coordinator_backup.json in this zip: the adapter type does not support coordinator "
                "backups in Zigbee2MQTT (only zstack and ember do). Devices will need re-pairing after a "
                "coordinator failure."
            )
        return BackupPayload(data=data, ext=".zip", meta=meta)

    @staticmethod
    def _inspect_zip(data: bytes) -> dict[str, Any]:
        meta: dict[str, Any] = {"format": "zigbee2mqtt-zip"}
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            meta["members"] = [n for n in zf.namelist() if not n.endswith("/")]
            if "coordinator_backup.json" in zf.namelist():
                meta.update(summarize_ocbf(json.loads(zf.read("coordinator_backup.json"))))
            if "configuration.yaml" in zf.namelist():
                cfg = yaml.load(zf.read("configuration.yaml"), Loader=_SafeLoader) or {}
                serial = cfg.get("serial") or {}
                meta["serial_adapter"] = serial.get("adapter")
                meta["serial_port"] = serial.get("port")
        return meta

    # ----------------------------------------------------------- restore

    async def _addon(self) -> tuple[Any, Path | None]:
        """Return (AddonManager, data dir as seen from HA core) for the installed Z2M add-on."""
        try:
            from homeassistant.components.hassio import AddonManager, AddonState  # noqa: PLC0415
            from homeassistant.helpers.hassio import is_hassio  # noqa: PLC0415
        except ImportError:
            return None, None
        if not is_hassio(self.hass):
            return None, None
        for slug in (Z2M_ADDON_SLUG, Z2M_ADDON_SLUG_EDGE):
            mgr = AddonManager(self.hass, _LOGGER, "Zigbee2MQTT", slug)
            try:
                info = await mgr.async_get_addon_info()
            except Exception:  # noqa: BLE001
                continue
            if info.state is AddonState.NOT_INSTALLED:
                continue
            data_path = (info.options or {}).get("data_path") or DEFAULT_ADDON_DATA_PATH
            if data_path.startswith("/config/"):
                return mgr, Path(self.hass.config.path(data_path[len("/config/") :]))
            if data_path.startswith("/share/"):
                return mgr, Path(data_path)
            return mgr, None  # /addon_config/... is not visible from Core
        return None, None

    def _fallback_data_dir(self) -> Path | None:
        for p in (Path(self.hass.config.path("zigbee2mqtt")), Path("/share/zigbee2mqtt")):
            if (p / "configuration.yaml").exists():
                return p
        return None

    @staticmethod
    def _read_yaml(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        return yaml.load(path.read_text(), Loader=_SafeLoader) or {}

    @staticmethod
    def _ocbf_from_zip(data: bytes) -> dict[str, Any] | None:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            if "coordinator_backup.json" not in zf.namelist():
                return None
            return json.loads(zf.read("coordinator_backup.json"))

    async def async_restore_check(self, data: bytes, meta: dict[str, Any]) -> RestoreCheck:
        check = RestoreCheck(ok=True, backup=meta)
        try:
            check.current = await self.async_identity()
        except Exception as err:  # noqa: BLE001
            check.warnings.append(f"Could not read live coordinator identity: {err}")

        ocbf = await self.hass.async_add_executor_job(self._ocbf_from_zip, data)
        if ocbf is None:
            check.ok = False
            check.reasons.append(
                "Backup zip has no coordinator_backup.json; this adapter type cannot be restored"
            )
            return check
        bak = summarize_ocbf(ocbf)

        mgr, data_dir = await self._addon()
        if mgr is None:
            data_dir = await self.hass.async_add_executor_job(self._fallback_data_dir)
        if mgr and data_dir:
            check.warnings.append(
                f"Will stop add-on {mgr.addon_slug}, patch {data_dir}/configuration.yaml, then start it"
            )
        elif data_dir:
            check.warnings.append(
                f"Z2M data dir found at {data_dir} but Z2M is not a Supervisor add-on; you must stop/start Z2M yourself"
            )
        else:
            check.warnings.append(
                "Could not locate the Z2M data directory; files will be staged for manual restore"
            )

        if data_dir:
            cfg = await self.hass.async_add_executor_job(self._read_yaml, data_dir / "configuration.yaml")
            problems = config_mismatches(cfg, ocbf)
            if problems:
                check.warnings.append(
                    "configuration.yaml advanced.* does not match the backup and will be patched: "
                    + "; ".join(problems)
                )
            live_adapter = (cfg.get("serial") or {}).get("adapter")
            bak_stack = bak.get("stack")
            if live_adapter and bak_stack and live_adapter != bak_stack:
                check.warnings.append(
                    f"Live serial.adapter is '{live_adapter}' but the backup came from a '{bak_stack}' adapter. "
                    "Cross-stack restores are not officially supported by Zigbee2MQTT and may need the backup "
                    "converted (zstack -> ember especially). Expect re-pairing if it fails."
                )
            if (
                live_adapter == "ember"
                and check.current.get("ieee")
                and bak.get("ieee") != check.current.get("ieee")
            ):
                check.warnings.append(
                    "ember adapters do not get their IEEE address written by the restore. Copy the IEEE "
                    f"{bak.get('ieee')} onto the new stick with the vendor tool first, or many devices will not rejoin."
                )

        if check.current.get("ieee") and bak.get("ieee") == check.current.get("ieee"):
            same_net = check.current.get("extended_pan_id") == bak.get("extended_pan_id")
            if same_net:
                check.ok = False
                check.reasons.append(
                    "Live coordinator already has this backup's IEEE and network; restoring is almost always a "
                    "mistake. Use force to rewrite it anyway."
                )
        check.warnings.append(
            "Zigbee2MQTT's device database is replaced by the one in the backup; devices paired after the backup are lost."
        )
        check.warnings.append(
            "Unplug the old coordinator for good. Two coordinators with the same IEEE on one mesh will break it."
        )
        return check

    async def async_restore(self, data: bytes, meta: dict[str, Any]) -> None:
        async with self._lock:
            ocbf = await self.hass.async_add_executor_job(self._ocbf_from_zip, data)
            if ocbf is None:
                raise RuntimeError("Backup zip has no coordinator_backup.json")
            mgr, data_dir = await self._addon()
            if mgr is None:
                data_dir = await self.hass.async_add_executor_job(self._fallback_data_dir)

            if mgr and data_dir:
                _LOGGER.warning("Stopping Z2M add-on %s for restore", mgr.addon_slug)
                self._online_event.clear()
                await mgr.async_stop_addon()
                try:
                    await self.hass.async_add_executor_job(
                        self._write_restore_files, data, data_dir, ocbf, True
                    )
                finally:
                    _LOGGER.info("Starting Z2M add-on %s", mgr.addon_slug)
                    await mgr.async_start_addon()
                await self._verify_after_start(ocbf)
                return

            staging = Path(self.hass.config.path("radio_guard", "zigbee2mqtt", "restore"))
            await self.hass.async_add_executor_job(self._write_restore_files, data, staging, ocbf, False)
            where = f" (its data directory looks like {data_dir})" if data_dir else ""
            raise RestoreStaged(
                str(staging),
                "Zigbee2MQTT is not a Supervisor add-on here, so finish by hand: 1) stop Zigbee2MQTT; "
                f"2) copy every file from the staging folder into its data directory{where}, replacing what is "
                "there (the staged configuration.yaml already has advanced.* patched to the backup; keep your own "
                'serial.port and mqtt settings); 3) start Zigbee2MQTT; 4) its log must say "Started with result '
                "'restored'\". If it says 'reset', stop it, delete the new coordinator_backup.json, re-copy and retry.",
            )

    async def _verify_after_start(self, ocbf: dict[str, Any]) -> None:
        """Wait for Z2M to come back and confirm it did not form a new network."""
        try:
            await asyncio.wait_for(self._online_event.wait(), ONLINE_TIMEOUT)
        except TimeoutError as err:
            raise RuntimeError(
                "Zigbee2MQTT did not report online within 2 minutes after restart. Check the add-on log; "
                "if it says \"Started with result 'reset'\" the restore did not apply (see README)."
            ) from err
        self._info_event.clear()
        self._info = {}
        with_timeout = 30
        try:
            await asyncio.wait_for(self._info_event.wait(), with_timeout)
        except TimeoutError:
            _LOGGER.warning("Z2M online but bridge/info not seen yet; skipping post-restore verification")
            return
        bak = summarize_ocbf(ocbf)
        ident = await self.async_identity()
        if ident.get("extended_pan_id") and ident["extended_pan_id"] != bak.get("extended_pan_id"):
            raise RuntimeError(
                f"Zigbee2MQTT started with a different network (ext PAN {ident['extended_pan_id']} vs backup "
                f"{bak.get('extended_pan_id')}). The restore did not apply and Z2M formed a new network; its "
                "previous database was moved to database.db.backup. Stop Z2M, check advanced.* in "
                "configuration.yaml against the backup, and retry."
            )
        _LOGGER.info(
            "Zigbee2MQTT restored network %s on coordinator %s",
            ident.get("extended_pan_id"),
            ident.get("ieee"),
        )

    @staticmethod
    def _write_restore_files(data: bytes, dest: Path, ocbf: dict[str, Any], patch_live_config: bool) -> None:
        """Write backup data files into dest, patching configuration.yaml advanced.* to match the OCBF."""
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for member in zf.namelist():
                if member.endswith("/") or member.startswith(RESTORE_SKIP_DIRS) or member in RESTORE_SKIP:
                    continue
                target = dest / member
                target.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write(target, zf.read(member))
            bak_cfg = (
                yaml.load(zf.read("configuration.yaml"), Loader=_SafeLoader)
                if "configuration.yaml" in zf.namelist()
                else {}
            )
            if "secret.yaml" in zf.namelist() and not (dest / "secret.yaml").exists():
                _atomic_write(dest / "secret.yaml", zf.read("secret.yaml"))

        live_path = dest / "configuration.yaml"
        if patch_live_config and live_path.exists():
            cfg = yaml.load(live_path.read_text(), Loader=_SafeLoader) or {}
        else:
            cfg = bak_cfg or {}
        adv = cfg.setdefault("advanced", {}) or {}
        adv.update(advanced_from_ocbf(ocbf))
        cfg["advanced"] = adv
        if live_path.exists():
            _atomic_write(live_path.with_suffix(".yaml.radio_guard.bak"), live_path.read_bytes())
        _atomic_write(live_path, yaml.safe_dump(cfg, sort_keys=False).encode())


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def summarize_ocbf(ocbf: dict[str, Any]) -> dict[str, Any]:
    """Pull the useful identity fields out of an Open Coordinator Backup."""
    md = ocbf.get("metadata") or {}
    stack = next(iter(ocbf.get("stack_specific") or {}), None)
    return {
        "ocbf_version": md.get("version"),
        "ocbf_format": md.get("format"),
        "ocbf_source": md.get("source"),
        "stack": stack,
        "pan_id": norm_pan_id(ocbf.get("pan_id")),
        "extended_pan_id": norm_hex(ocbf.get("extended_pan_id")),
        "channel": ocbf.get("channel"),
        "ieee": norm_hex(ocbf.get("coordinator_ieee")),
        "nwk_update_id": ocbf.get("nwk_update_id"),
        "frame_counter": (ocbf.get("network_key") or {}).get("frame_counter"),
        "device_count": len(ocbf.get("devices") or []),
    }


def advanced_from_ocbf(ocbf: dict[str, Any]) -> dict[str, Any]:
    """The configuration.yaml advanced.* values Z2M needs to accept this backup."""
    out: dict[str, Any] = {}
    if ocbf.get("pan_id") is not None:
        out["pan_id"] = int(str(ocbf["pan_id"]), 16)
    if ocbf.get("extended_pan_id"):
        out["ext_pan_id"] = list(bytes.fromhex(norm_hex(ocbf["extended_pan_id"]) or ""))
    key = (ocbf.get("network_key") or {}).get("key")
    if key:
        out["network_key"] = list(bytes.fromhex(norm_hex(key) or ""))
    if ocbf.get("channel") is not None:
        out["channel"] = int(ocbf["channel"])
    return out


def config_mismatches(cfg: dict[str, Any], ocbf: dict[str, Any]) -> list[str]:
    """Human-readable list of advanced.* values that differ from the backup."""
    adv = cfg.get("advanced") or {}
    want = advanced_from_ocbf(ocbf)
    problems: list[str] = []
    for key, expected in want.items():
        have = adv.get(key)
        if isinstance(have, str) and have.upper() == "GENERATE":
            problems.append(f"advanced.{key} is GENERATE")
            continue
        if isinstance(expected, list):
            have_list = list(have) if isinstance(have, (list, tuple)) else None
            if have_list != expected:
                problems.append(f"advanced.{key} differs")
        elif have != expected:
            problems.append(f"advanced.{key} is {have!r}, backup needs {expected!r}")
    return problems
