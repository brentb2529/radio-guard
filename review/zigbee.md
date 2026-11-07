# Zigbee backup/restore review (z2m.py, zha.py)

Reviewed: `custom_components/radio_guard/backup/z2m.py`, `backup/zha.py` (plus `base.py`, `const.py` for context).
Verified against upstream source on 2026-11-07: Koenkk/zigbee2mqtt `master`, Koenkk/zigbee-herdsman `master`,
zigpy/zigpy `dev`, zigpy/bellows `dev`, zigpy/zigpy-znp `dev`, zigpy/zigpy-deconz `dev`, zigpy/zha `dev`,
home-assistant/core `dev` (plus tagged releases where noted), home-assistant/supervisor `main`,
zigbee2mqtt/hassio-zigbee2mqtt `master`.

Severity legend: **BLOCKER** (code cannot work / data loss), **HIGH** (wrong on common installs), **MEDIUM** (wrong on some
installs or misleading), **LOW** (cosmetic / robustness).

---

## 1. Z2M backup request / response — CONFIRMED (with LOW notes)

Source: [lib/extension/bridge.ts](https://github.com/Koenkk/zigbee2mqtt/blob/master/lib/extension/bridge.ts) (`backup()` and `onMQTTMessage()`),
[lib/util/utils.ts `getResponse`](https://github.com/Koenkk/zigbee2mqtt/blob/master/lib/util/utils.ts),
[MQTT topics docs](https://github.com/Koenkk/zigbee2mqtt.io/blob/master/docs/guide/usage/mqtt_topics_and_messages.md).

| Claim in code | Verdict |
|---|---|
| Request topic `<base>/bridge/request/backup` | CONFIRMED |
| Response topic `<base>/bridge/response/backup` | CONFIRMED. Bridge publishes `bridge/response/${match[1]}` where `match[1]` is the request suffix. |
| Payload `{}` is valid | CONFIRMED. Docs say "Payload has to be empty"; `backup(message)` ignores the message content except for `transaction`. `{}` (or an empty string) both work. |
| Response shape `{"data":{"zip":"<b64>"},"status":"ok"}` | CONFIRMED |
| Error shape `{"data":{},"status":"error","error":"<msg>"}` | CONFIRMED (`getResponse` always returns `data: {}` on error). |
| `transaction` correlation | CONFIRMED as *available*: if the request object contains `transaction`, Z2M echoes it in the response (both ok and error). The code currently does **not** send one. |
| 60 s timeout | Reasonable. The handler calls `zigbee.backup()` (full NVRAM read on zstack / key table export on ember), then reads and deflate-6 compresses the whole data dir. Typical 1-10 s; 60 s is a safe ceiling. |

**LOW — add a `transaction` and filter on it.** Any other client (e.g. the Z2M frontend, a user clicking "Backup" in the UI at the same time) that triggers a backup produces a response on the same topic; without correlation we would consume someone else's response. Also, `getResponse` copies `transaction` *only when the request is an object*, so `{}` plus a transaction field is the right form. Corrected `_request`:

```python
async def _request(self, name: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    from homeassistant.components import mqtt  # noqa: PLC0415
    import secrets  # noqa: PLC0415

    req = f"{self.base_topic}/bridge/request/{name}"
    resp = f"{self.base_topic}/bridge/response/{name}"
    txn = f"radio_guard-{secrets.token_hex(4)}"
    payload = {**payload, "transaction": txn}
    fut: asyncio.Future[dict[str, Any]] = self.hass.loop.create_future()

    @callback
    def _on_msg(msg: mqtt.ReceiveMessage) -> None:
        if fut.done():
            return
        try:
            data = json.loads(msg.payload)
        except ValueError as err:
            fut.set_exception(ValueError(f"Bad JSON from Z2M: {err}"))
            return
        if data.get("transaction") != txn:
            return  # someone else's response
        fut.set_result(data)

    unsub = await mqtt.async_subscribe(self.hass, resp, _on_msg, qos=1)
    try:
        # HA debounces subscriptions (INITIAL_SUBSCRIBE_COOLDOWN=0.5 s / SUBSCRIBE_COOLDOWN=0.1 s);
        # the SUBSCRIBE may not have hit the broker yet when we publish. Give it a moment.
        await asyncio.sleep(0.5)
        await mqtt.async_publish(self.hass, req, json.dumps(payload), qos=1)
        return await asyncio.wait_for(fut, timeout)
    finally:
        unsub()
```

**LOW — subscribe/publish race.** HA's MQTT client batches subscriptions through a debouncer
([client.py](https://github.com/home-assistant/core/blob/dev/homeassistant/components/mqtt/client.py), `INITIAL_SUBSCRIBE_COOLDOWN = 0.5`, `SUBSCRIBE_COOLDOWN = 0.1`). `await mqtt.async_subscribe(...)` returns before the broker has acknowledged the subscription, so a fast Z2M (error responses are near-instant) can answer before we are subscribed. The 0.5 s sleep above, or subscribing to `<base>/bridge/response/#` once at setup, avoids it.

**LOW — payload size.** The zip contains the whole data dir (see §3). Z2M publishes with `maximumPacketSize` default 1 MiB (`mqtt.maximum_packet_size`, [mqtt.md](https://github.com/Koenkk/zigbee2mqtt.io/blob/master/docs/guide/configuration/mqtt.md)); Mosquitto's default `max_packet_size` is unlimited. Base64 of a large `database.db` + `state.json` can exceed 1 MiB on big networks and the publish silently fails on the Z2M side. Worth mentioning in the error text if the request times out: "check `mqtt.maximum_packet_size` in Z2M and `max_packet_size` on the broker".

**LOW — `available()` check.** `"mqtt" in self.hass.config.components` is fine, but prefer `await mqtt.async_wait_for_mqtt_client(hass)` (exported from `homeassistant.components.mqtt`) before subscribing so the first scheduled backup after HA start does not fail.

---

## 2. `bridge/info` field names — MOSTLY WRONG (HIGH for two fields)

Source: [lib/types/api.ts `"bridge/info"`](https://github.com/Koenkk/zigbee2mqtt/blob/master/lib/types/api.ts), [bridge.ts `publishInfo()`](https://github.com/Koenkk/zigbee2mqtt/blob/master/lib/extension/bridge.ts), herdsman [tstype.ts](https://github.com/Koenkk/zigbee-herdsman/blob/master/src/adapter/tstype.ts) (`CoordinatorVersion`, `NetworkParameters`), [emberAdapter.ts](https://github.com/Koenkk/zigbee-herdsman/blob/master/src/adapter/ember/adapter/emberAdapter.ts), [zStackAdapter.ts](https://github.com/Koenkk/zigbee-herdsman/blob/master/src/adapter/z-stack/adapter/zStackAdapter.ts).

| Field used | Verdict | Actual |
|---|---|---|
| `coordinator.ieee_address` | CONFIRMED | `"0x..."` string |
| `coordinator.type` | CONFIRMED | `"zStack3x0"`, `"zStack30x"`, `"zStack12"`, `"EmberZNet"`, `"ConBee2"`/deconz product name, etc. |
| `coordinator.meta.revision` | CONFIRMED but stack-dependent | `meta` is `{[s: string]: number \| string}`. zstack: `revision` is a number (e.g. `20240710`), plus `transportrev/product/majorrel/minorrel/maintrel`. ember: `revision` is a string like `"8.0.2 [GA]"`, plus `ezsp`, `major`, `minor`, `patch`, `build`, `special`, `type`. deconz: has `revision` too. Fine as a best-effort string; cast with `str()` before storing. |
| `network.pan_id` | CONFIRMED | **number** (decimal), e.g. `6754`, not a hex string. |
| `network.extended_pan_id` | CONFIRMED | now a `"0x..."` 16-hex-digit string (`NetworkParameters.extendedPanID: string`). Older Z2M (<2.x) published a byte array; be tolerant of a list. |
| `network.channel` | CONFIRMED | number |
| `version` | CONFIRMED | Z2M version string |
| `zigbee_herdsman_version` | **WRONG** | The key is `zigbee_herdsman` and it is an object: `{"version": "x.y.z"}`. Likewise `zigbee_herdsman_converters: {"version": ...}`. |

**HIGH — `pan_id` type mismatch between sources.** `bridge/info.network.pan_id` is a decimal integer, the OCBF `pan_id` is a 4-hex-digit string without `0x` (see §3), and ZHA's identity formats it as `"0x%04X"`. Any comparison between "current" and "backup" identity in `async_restore_check` (or in the coordinator UI) will mismatch unless normalised. Corrected `async_identity`:

```python
async def async_identity(self) -> dict[str, Any]:
    if not self.available():
        raise SourceUnavailable("MQTT integration is not loaded")
    info = await self._bridge_info()
    coord = info.get("coordinator") or {}
    net = info.get("network") or {}
    meta = coord.get("meta") or {}
    pan = net.get("pan_id")
    epid = net.get("extended_pan_id")
    if isinstance(epid, list):  # Z2M 1.x published a byte array
        epid = "0x" + "".join(f"{b:02x}" for b in epid)
    return {
        "ieee": _norm_ieee(coord.get("ieee_address")),
        "coordinator_type": coord.get("type"),
        "coordinator_version": str(meta["revision"]) if "revision" in meta else None,
        "pan_id": f"0x{pan:04x}" if isinstance(pan, int) else pan,
        "extended_pan_id": _norm_ieee(epid),
        "channel": net.get("channel"),
        "z2m_version": info.get("version"),
        "zigbee_herdsman": (info.get("zigbee_herdsman") or {}).get("version"),
    }


def _norm_ieee(v: Any) -> str | None:
    """Normalise '0x00124b00...' / '00124b00...' / '00:12:4b:...' to lowercase '0x' + 16 hex."""
    if not v:
        return None
    s = str(v).lower().replace(":", "")
    if s.startswith("0x"):
        s = s[2:]
    return f"0x{s.zfill(16)}" if s else None
```

And in `_summarize_ocbf`, normalise `pan_id` to `"0x" + pan_id.zfill(4)` and IEEE/EPID through `_norm_ieee` so the two sides compare equal.

**LOW — `_bridge_info` retained read.** `bridge/info` is published with `retain: true` (confirmed in `publishInfo()`), so a fresh subscription receives it immediately *unless* the user set `mqtt.force_disable_retain: true`, in which case the 5 s wait times out and identity is `{}`. Acceptable, but the warning text should say so.

---

## 3. Z2M zip contents and OCBF keys — PARTLY WRONG (MEDIUM)

Source: [bridge.ts `backup()`](https://github.com/Koenkk/zigbee2mqtt/blob/master/lib/extension/bridge.ts), [utils.ts `getAllFiles`](https://github.com/Koenkk/zigbee2mqtt/blob/master/lib/util/utils.ts), herdsman [utils/backup.ts `toUnifiedBackup`](https://github.com/Koenkk/zigbee-herdsman/blob/master/src/utils/backup.ts), [OCBF schema.json](https://github.com/zigpy/open-coordinator-backup/blob/main/schema.json), [OCBF README](https://github.com/zigpy/open-coordinator-backup/blob/main/README.md).

**Zip contents — WRONG as described (MEDIUM).** The module docstring says the zip contains `coordinator_backup.json`, `database.db` and `configuration.yaml`. In reality `backup()` walks the **entire data directory recursively** (`getAllFiles(dataPath)`), skipping only the `log/` and `ota/` prefixes and symlinks. So the zip typically contains at least:

- `configuration.yaml` (and `configuration.yaml.bk` on the HA add-on)
- `coordinator_backup.json` — **only if the adapter supports backups** (`zstack`, `ember`); absent for `deconz`, `zigate`, `zboss`, legacy `ezsp`. Docs: "Note that not all adapters support backup (`coordinator_backup.json`)".
- `database.db` (+ `database.db.backup` after a reset)
- `state.json`, `devices.yaml`/`groups.yaml` if split config is used, `secret.yaml` if used (**contains network_key / mqtt password**), `external_converters/*`, `external_extensions/*`, `stack_config.json` (ember), etc.

Paths inside the zip are relative to the data dir and use the host `path.sep` (so forward slashes on Linux). The `members` check `"coordinator_backup.json" in zf.namelist()` is correct for top-level files. Note the backup zip is a **secret** (network key is in `coordinator_backup.json` and `configuration.yaml`); store it accordingly.

**OCBF top-level keys — CONFIRMED, but the value formats differ from what the code assumes (MEDIUM).** Schema required keys: `metadata`, `coordinator_ieee`, `pan_id`, `extended_pan_id`, `nwk_update_id`, `security_level`, `channel`, `channel_mask`, `network_key`, `devices`; optional `stack_specific`. `metadata` has `format` ("zigpy/open-coordinator-backup"), `version` (integer `1`), `source` (`"zigbee-herdsman@x.y.z"` or `"zigpy-znp@…"`/`"bellows@…"`), `internal` (free-form).

Format details that matter:

- `coordinator_ieee`: 16 lowercase hex chars, **no `0x`** (e.g. `00124b0009d69f77`). `bridge/info.coordinator.ieee_address` has `0x`. The IEEE comparison in `async_restore_check` (`cur_ieee.lower() == bak_ieee.lower()`) therefore **never matches** today — HIGH, see `_norm_ieee` above.
- `pan_id`: hex string, no `0x`. herdsman writes `panId.toString(16)` **without zero-padding** (so PAN `0x0abc` is `"abc"`); zigpy pads to 4. Use `.zfill(4)`.
- `extended_pan_id`: 16 hex chars, no `0x`.
- `channel`: int; `nwk_update_id`: int; `devices`: array of `{nwk_address, ieee_address, is_child, link_key?}`.

Corrected `_summarize_ocbf`:

```python
def _summarize_ocbf(ocbf: dict[str, Any]) -> dict[str, Any]:
    md = ocbf.get("metadata") or {}
    pan = ocbf.get("pan_id")
    return {
        "ocbf_format": md.get("format"),
        "ocbf_version": md.get("version"),
        "ocbf_source": md.get("source"),
        # OCBF stores bare hex without 0x; normalise to match bridge/info and ZHA.
        "ieee": _norm_ieee(ocbf.get("coordinator_ieee")),
        "pan_id": f"0x{str(pan).lower().zfill(4)}" if pan is not None else None,
        "extended_pan_id": _norm_ieee(ocbf.get("extended_pan_id")),
        "channel": ocbf.get("channel"),
        "nwk_update_id": ocbf.get("nwk_update_id"),
        "device_count": len(ocbf.get("devices") or []),
        "stack": next(iter(ocbf.get("stack_specific") or {}), None),  # "zstack" | "ezsp" | None
        "frame_counter": (ocbf.get("network_key") or {}).get("frame_counter"),
    }
```

`stack` and `frame_counter` are useful for the restore pre-flight (§4): herdsman's `ember` adapter refuses OCBF files without `stack_specific.ezsp`, and a backup with a lower frame counter than the live network is dangerous to restore.

---

## 4. Z2M restore mechanics — PARTLY WRONG (BLOCKER-level omissions)

Source: herdsman [z-stack/adapter/manager.ts `determineStrategy()`](https://github.com/Koenkk/zigbee-herdsman/blob/master/src/adapter/z-stack/adapter/manager.ts),
[z-stack/adapter/adapter-backup.ts `restoreBackup()`](https://github.com/Koenkk/zigbee-herdsman/blob/master/src/adapter/z-stack/adapter/adapter-backup.ts),
[ember/adapter/emberAdapter.ts `initTrustCenter()` / `getStoredBackup()`](https://github.com/Koenkk/zigbee-herdsman/blob/master/src/adapter/ember/adapter/emberAdapter.ts),
[z-stack/utils/network-options.ts `compareNetworkOptions`](https://github.com/Koenkk/zigbee-herdsman/blob/master/src/adapter/z-stack/utils/network-options.ts),
[controller.ts `start()`](https://github.com/Koenkk/zigbee-herdsman/blob/master/src/controller/controller.ts),
Z2M [lib/zigbee.ts](https://github.com/Koenkk/zigbee2mqtt/blob/master/lib/zigbee.ts) (`backupPath: data.joinPath("coordinator_backup.json")`),
[Z2M FAQ: migrate adapters / re-pairing](https://github.com/Koenkk/zigbee2mqtt.io/blob/master/docs/guide/faq/README.md),
[copy_ieee_addr.md](https://github.com/Koenkk/zigbee2mqtt.io/blob/master/docs/guide/adapters/flashing/copy_ieee_addr.md).

### 4a. Does Z2M restore from `coordinator_backup.json` on startup? — CONFIRMED, but the conditions are stricter than "attached coordinator does not match configuration.yaml"

Z2M has no restore API (CONFIRMED). herdsman reads `<data>/coordinator_backup.json` at start. The decision is made per adapter:

**zstack (`determineStrategy`)** — restore happens only when **`configMatchesBackup`** is true, i.e. configuration.yaml's `advanced.pan_id`, `advanced.ext_pan_id`, `advanced.network_key`, `advanced.channel` all equal the values inside the backup (`compareNetworkOptions(..., lenient=true)`; also `networkKeyDistribute`, which herdsman always sets `false` on the config side and the backup's `fromUnifiedBackup` also sets `false`, so it is a no-op), **and** either
- the adapter is blank/not commissioned (`!hasConfigured || !nib`), or
- the adapter is commissioned with a network that matches neither config nor backup.

Outcomes when things do **not** line up:
- Config != backup → **`startCommissioning`**: herdsman forms a brand-new network from configuration.yaml, `startResult = "reset"`, and `controller.start()` then **clears `database.db`** (after copying it to `database.db.backup`) and overwrites `coordinator_backup.json` with a backup of the *new* network. All devices must be re-paired. This is the data-loss path and is exactly what happens if the user's `configuration.yaml` still has `network_key: GENERATE`/`pan_id: GENERATE` or an on-boarding-generated key that differs from the backup.
- Adapter already has a network that matches the backup but config differs → hard error `startup failed - configuration-adapter mismatch` (unless `adapter.force_start_with_inconsistent_adapter_configuration`).
- Z-Stack 1.2 (CC2531) targets cannot be restored at all; a Z-Stack 3 backup on a 1.2 adapter throws.

**ember (`initTrustCenter`)** — `ezspNetworkInit`; if the NCP has a network whose PAN/EPID/network key differ from config it **leaves** it; then if there is no network and a backup exists **and** config pan_id/ext_pan_id/channel/network_key all equal the backup → `FORM_BACKUP` (imports link keys, forms with backup key + frame counter + `hashed_tclk`); else `FORM_CONFIG` = new network, `"reset"`, database wiped. Additionally `getStoredBackup()` **throws** if the file lacks `stack_specific.ezsp` / `metadata.internal.ezspVersion` ("Current backup file is not for EmberZNet stack") and renames+ignores backups from EZSP < 12.

**Other adapters** (`deconz`, `zigate`, `zboss`, legacy `ezsp`): `supportsBackup()` is false; `coordinator_backup.json` is neither written nor read. A restore via this route is impossible.

### 4b. Does the user need to delete anything? — Yes in some cases; the code never says so (HIGH)

- If the **new** adapter is already commissioned (second-hand stick, or it was previously used to form a different network), zstack takes the "configured, matches neither" branch → restore (OK). ember leaves the stale network first (OK). So usually nothing to erase on the stick.
- The user must **not** delete `coordinator_backup.json` (that is the restore source), but for the "requires re-pairing" migration path Z2M docs tell them to delete both `coordinator_backup.json` and `database.db`.
- If there is a `database.db.backup` or stale `coordinator_backup.json.tmp`, harmless.

### 4c. Must `configuration.yaml` match the backup? — **Yes, this is the central requirement and the code ignores it (BLOCKER)**

Both stacks restore **only when** `advanced.pan_id`, `advanced.ext_pan_id`, `advanced.network_key` and `advanced.channel` in `configuration.yaml` equal the values in `coordinator_backup.json`. Otherwise Z2M silently forms a new network and wipes the database. Today the integration writes `coordinator_backup.json` + `database.db` into the data dir and starts the add-on without checking. On a fresh add-on install (user replaced the SD card / did a clean HA restore / onboarding generated a new key) this **destroys the backup's usefulness** on first start, because `controller.start()` overwrites `coordinator_backup.json` with the new network's backup.

Required fix in `_write_restore_files` / `async_restore_check`:

1. Parse `configuration.yaml` from the zip (it is always there) and from the live data dir.
2. Pre-flight: compare live `advanced.{pan_id,ext_pan_id,network_key,channel}` against the OCBF values (`pan_id` hex→int, `extended_pan_id` hex→byte list, `network_key.key` hex→byte list, `channel`). If they differ, or any is `GENERATE`, refuse unless `force`, and either (a) write the backup's `configuration.yaml` too (simplest, preserves `serial`/`mqtt` only if we merge), or (b) patch `advanced.*` in the live file. Option (b) is safer because the live file has the correct `serial.port` for the *new* stick. Note Z2M 2.x stores `network_key` as a YAML list of 16 ints; `pan_id` as int; `ext_pan_id` as list of 8 ints; `channel` as int.
3. Also write `configuration.yaml` → no. Prefer patching, since the user's serial port, adapter type and MQTT settings in the live file are what must survive.

Sketch of the pre-flight comparison:

```python
def _z2m_cfg_matches_ocbf(cfg: dict, ocbf: dict) -> list[str]:
    adv = (cfg.get("advanced") or {})
    problems = []
    want_pan = int(ocbf["pan_id"], 16)
    want_epid = list(bytes.fromhex(ocbf["extended_pan_id"]))
    want_key = list(bytes.fromhex(ocbf["network_key"]["key"]))
    if adv.get("pan_id") != want_pan:
        problems.append(f"advanced.pan_id is {adv.get('pan_id')!r}, backup needs {want_pan}")
    if list(adv.get("ext_pan_id") or []) != want_epid:
        problems.append("advanced.ext_pan_id does not match backup")
    if list(adv.get("network_key") or []) != want_key:
        problems.append("advanced.network_key does not match backup")
    if adv.get("channel") != ocbf["channel"]:
        problems.append(f"advanced.channel is {adv.get('channel')!r}, backup needs {ocbf['channel']}")
    return problems
```

(`configuration.yaml` may reference `secret.yaml` via `!secret`; the zip also includes `secret.yaml`, so load both or treat unresolved `!secret` as "cannot verify".)

### 4d. Is `database.db` needed/wanted? — Wanted, not strictly needed (MEDIUM)

`database.db` is herdsman's device database (friendly names live in `configuration.yaml`/`devices.yaml`; endpoints, clusters, interview results, bindings live in `database.db`). The coordinator NVRAM restore does **not** need it, but without it every device is "unknown"/must be re-interviewed and the Z2M frontend shows nothing until devices announce. So restoring it is correct. On a successful restore (`startResult === "restored"`) herdsman keeps the database; on `"reset"` it moves it to `database.db.backup` and clears it. Also note `controller.start()` updates the coordinator row's IEEE in the database to whatever the adapter reports (`Coordinator address changed, updating to ...`) — so an IEEE mismatch does not corrupt the DB, but see 4e.

Also worth restoring from the zip: `devices.yaml`/`groups.yaml` if present (friendly names, when `devices: devices.yaml` split config is used). Current code only writes two files; recommend extracting everything **except** `configuration.yaml` and `secret.yaml` (which must be merged, not clobbered) and `log/`.

### 4e. Coordinator IEEE handling — different per stack (HIGH, affects runbook)

- **zstack restore writes the backup's IEEE to the new chip**: `adapter-backup.ts restoreBackup()` → `nv.writeItem(NvItemsIds.EXTADDR, reversedAdapterIeee)`. So TI→TI replacement via `coordinator_backup.json` restores the IEEE automatically (secondary IEEE; survives until a full chip erase).
- **ember restore does NOT write the IEEE.** `formNetwork(fromBackup=true, …)` never touches the EUI64 (no `setMfgToken`/custom-EUI write anywhere in `emberAdapter.ts`). Devices that key on the coordinator's IEEE (TC link-key hashed with TC address, many Tuya/Xiaomi, anything that cached the TC EUI64) will fail to rejoin. Z2M docs therefore require the user to **copy the IEEE address onto the new EFR32 stick with an external tool** (XZG Multi-Tool / Silabs tools; one-time `MFG_CUSTOM_EUI_64` burn if the firmware lacks NV3 token support) **before** starting Z2M. The integration cannot do this; the restore_check must warn when `stack == "ezsp"` and current IEEE != backup IEEE.

### 4f. Cross-chip (TI → Silabs/ember and back) via this route — officially "might work" (MEDIUM)

- herdsman's unified format is shared, so `fromUnifiedBackup` of a zstack OCBF on an `ember` adapter is *structurally* possible, **but** `ember.getStoredBackup()` throws `"Current backup file is not for EmberZNet stack"` unless `stack_specific.ezsp` and `metadata.internal.ezspVersion` are present. A TI backup has `stack_specific.zstack.tclk_seed` instead, so **a Z2M TI backup cannot be dropped in front of an ember adapter as-is** — it needs conversion (zigpy/ZHA can do this: import the OCBF into zigpy and `write_network_info` on the new stick, or hand-edit the JSON to add an `ezsp` block with a `hashed_tclk`). The reverse (ember backup → zstack) loads (`fromUnifiedBackup` tolerates missing `zstack` section; TCLK seed just isn't recovered, so per-device link keys fall back to APS key table entries if present).
- Z2M FAQ: "switching from `zstack` -> `ember` or `ember` -> `zstack` re-pairing **might not** be required, however results might vary as this is not officially supported"; the officially supported no-re-pair cases are `zstack -> zstack` (not from Z-Stack 1.2) and `ember -> ember`.
- The user must also change `serial.adapter` in `configuration.yaml` (and usually `serial.port`/`baudrate`). The integration does not do that; flag it in the restore_check warnings.

### 4g. Timing / add-on stop-start — CONFIRMED approach, LOW notes

- Stopping the add-on before writing is required: herdsman writes `database.db` every 5 min and on shutdown (`backup()` → `databaseSave()`), and writes `coordinator_backup.json` on stop, so writing while running gets overwritten.
- After `async_start_addon`, the restore is asynchronous inside Z2M; success/failure is only visible in the add-on log (`Started with result 'restored'` vs `'reset'`). Recommend subscribing to `<base>/bridge/state` (`{"state":"online"}`) and then re-reading `bridge/info` to compare `coordinator.ieee_address` / `network.pan_id` against the backup, and surfacing "Z2M came up with a *different* network — restore failed, database was reset" if they differ.

---

## 5. Add-on slugs and data directory — WRONG for the data-dir mount (BLOCKER on current installs)

Source: [hassio-zigbee2mqtt zigbee2mqtt/config.json](https://raw.githubusercontent.com/zigbee2mqtt/hassio-zigbee2mqtt/master/zigbee2mqtt/config.json) (`slug: zigbee2mqtt`, `map`, `options.data_path`), [zigbee2mqtt-edge/config.json](https://raw.githubusercontent.com/zigbee2mqtt/hassio-zigbee2mqtt/master/zigbee2mqtt-edge/config.json), [add-on CHANGELOG](https://raw.githubusercontent.com/zigbee2mqtt/hassio-zigbee2mqtt/master/zigbee2mqtt/CHANGELOG.md), Supervisor [docker/homeassistant.py](https://github.com/home-assistant/supervisor/blob/main/supervisor/docker/homeassistant.py) and [docker/const.py](https://github.com/home-assistant/supervisor/blob/main/supervisor/docker/const.py).

| Claim | Verdict |
|---|---|
| Slug `45df7312_zigbee2mqtt` | CONFIRMED (repository hash `45df7312` + `slug: zigbee2mqtt`). |
| Slug `45df7312_zigbee2mqtt_edge` | CONFIRMED (`slug: zigbee2mqtt_edge`). Note: Edge is an unstable nightly; fine to probe. |
| Data dir `/addon_configs/<slug>` visible from HA core | **WRONG.** The HA Core container mounts only `/config`, `/ssl`, `/share`, `/media` (+ audio, machine-id, supervisor sockets). `/addon_configs` is mounted into **add-ons** that declare `addon_config` (and into the SSH/Studio Code Server add-ons), **not** into Core. The Z2M add-on does map `app_config` → `/addon_config` for *its own* container, but its **default `data_path` is `/config/zigbee2mqtt`**, i.e. inside the shared `homeassistant_config` mount, not under `/addon_config`. |
| `/config/zigbee2mqtt` | CONFIRMED as the default for every add-on version since 1.15.x ("Default `data_path` changed from `/share/` to `/config`"). From HA Core's point of view that is `hass.config.path("zigbee2mqtt")`. |
| `/share/zigbee2mqtt` | CONFIRMED as the **legacy** default (pre-1.15) and still valid if the user set `data_path: /share/zigbee2mqtt`; `/share` is mounted in Core at `/share`. |

Important subtlety: the add-on's `data_path` option is user-configurable (schema `data_path: str`). The authoritative way to find it is to read the add-on options via Supervisor: `AddonManager(...).async_get_addon_info()` → `AddonInfo.options["data_path"]`, then map the add-on's path to Core's:

- `/config/...` (add-on's `homeassistant_config` mount) → `hass.config.path(...)`
- `/share/...` → `/share/...` (same path in Core)
- `/addon_config/...` → **not reachable from Core**; fall back to staging + manual instructions (or the user can symlink/copy via the SSH add-on).

Corrected discovery:

```python
async def _addon_data_dir(self) -> tuple[str | None, Path | None]:
    """Return (slug, data dir as seen from HA core) for a running Z2M add-on."""
    from homeassistant.helpers.hassio import is_hassio  # noqa: PLC0415
    if not is_hassio(self.hass):
        return None, None
    from homeassistant.components.hassio import AddonError, AddonManager, AddonState  # noqa: PLC0415
    for slug in (Z2M_ADDON_SLUG, Z2M_ADDON_SLUG_EDGE):
        mgr = AddonManager(self.hass, _LOGGER, "Zigbee2MQTT", slug)
        try:
            info = await mgr.async_get_addon_info()
        except AddonError:
            continue
        if info.state is AddonState.NOT_INSTALLED:
            continue
        data_path = info.options.get("data_path") or "/config/zigbee2mqtt"
        if data_path.startswith("/config/"):
            core_path = Path(self.hass.config.path(data_path[len("/config/"):]))
        elif data_path.startswith("/share/"):
            core_path = Path(data_path)
        else:  # /addon_config/... or anything else: not visible from Core
            core_path = None
        return slug, core_path
    return None, None
```

And `CANDIDATE_DATA_DIRS` (fallback when Supervisor is absent, e.g. Container install with a bind-mount) should be `(hass.config.path("zigbee2mqtt"), "/share/zigbee2mqtt")`; drop the `/addon_configs/{slug}` entries.

**BLOCKER — `homeassistant.components.hassio` no longer exports `async_get_addon_info`, `async_start_addon`, `async_stop_addon`, `is_hassio`.** The functional helpers were removed in HA **2024.11** (present in 2024.10.0, gone in 2024.11.0); `is_hassio` was a deprecated shim with `breaks_in_ha_version="2025.11"` and is gone from `dev`. Today's API:

- `from homeassistant.helpers.hassio import is_hassio`
- `from homeassistant.components.hassio import AddonManager, AddonError, AddonInfo, AddonState` → `AddonManager(hass, logger, addon_name, addon_slug)` with `async_get_addon_info()`, `async_start_addon()`, `async_stop_addon()`, `async_restart_addon()` (all raise `AddonError`).

The current `_addon_slug()` catches `ImportError` and returns `None`, so on any supported HA the Supervisor path silently never triggers and every restore ends in "staged, do it yourself". Corrected restore:

```python
async def async_restore(self, data: bytes, meta: dict[str, Any]) -> None:
    slug, data_dir = await self._addon_data_dir()
    if slug and data_dir and (data_dir / "configuration.yaml").exists():
        from homeassistant.components.hassio import AddonError, AddonManager  # noqa: PLC0415
        mgr = AddonManager(self.hass, _LOGGER, "Zigbee2MQTT", slug)
        _LOGGER.info("Stopping Z2M add-on %s for restore", slug)
        await mgr.async_stop_addon()
        try:
            await self.hass.async_add_executor_job(self._write_restore_files, data, data_dir)
        finally:
            _LOGGER.info("Starting Z2M add-on %s", slug)
            await mgr.async_start_addon()
        return
    ...  # staging path unchanged
```

**MEDIUM — after `async_stop_addon` returns, the container may still be flushing.** Supervisor's stop is synchronous for the container, and herdsman writes `database.db`/`coordinator_backup.json` during its stop handler before the process exits, so by the time Supervisor reports stopped the files are final. No extra wait needed, but write with a temp-file + `os.replace` so a crash mid-write can't leave a truncated `coordinator_backup.json` (herdsman would throw "Unknown backup format" and refuse to start).

---

## 6. ZHA API surface — CONFIRMED (with MEDIUM notes)

Source: [core/homeassistant/components/zha/helpers.py](https://github.com/home-assistant/core/blob/dev/homeassistant/components/zha/helpers.py), [zigpy/zha application/gateway.py](https://github.com/zigpy/zha/blob/dev/zha/application/gateway.py), [zigpy/backups.py](https://github.com/zigpy/zigpy/blob/dev/zigpy/backups.py), [zigpy/state.py](https://github.com/zigpy/zigpy/blob/dev/zigpy/state.py), [core zha/websocket_api.py](https://github.com/home-assistant/core/blob/dev/homeassistant/components/zha/websocket_api.py).

| Claim | Verdict |
|---|---|
| `homeassistant.components.zha.helpers.get_zha_gateway(hass)` | CONFIRMED. Raises `ValueError("No gateway object exists")` when ZHA is not set up. Catching `Exception` is fine; catching `ValueError` is more precise. |
| `gateway.application_controller` | CONFIRMED (`zha.application.gateway.Gateway.application_controller: ControllerApplication`). It is `None` until `async_initialize()` completes, so the `getattr(..., None)` guard is right. |
| `app.backups.create_backup(load_devices=True)` | CONFIRMED; this is exactly what ZHA's own `zha/network/backups/create` websocket command does. Signature `create_backup(self, *, load_devices: bool = True)`. |
| `backup.as_dict()` | CONFIRMED — **but it is zigpy's *native* format** (`{"version", "backup_time", "network_info": {...}, "node_info": {...}}`), **not OCBF**. The module docstring and `meta["format"] = "ocbf"` are WRONG (MEDIUM, see below). |
| `backup.is_complete()` | CONFIRMED. |
| `NetworkBackup.from_dict(obj)` | CONFIRMED. It auto-detects: `"metadata" in obj` → OCBF parser; `"network_info" in obj` → native. So the restore path accepts both a Radio Guard ZHA backup and a Z2M `coordinator_backup.json`. |
| `app.backups.restore_backup(backup)` | CONFIRMED; signature `restore_backup(backup, *, counter_increment=10000, allow_incomplete=False, create_new=True)`. Raises `ValueError` if `not backup.is_complete()`. |
| `app.state.network_info.{pan_id, extended_pan_id, channel, nwk_update_id}` | CONFIRMED. `pan_id` is `t.PanId` (int subclass) so `f"0x{ni.pan_id:04X}"` works; `extended_pan_id` is `t.ExtendedPanId` whose `str()` is `xx:xx:...` colon form. |
| `app.state.node_info.{ieee, model, manufacturer, version}` | CONFIRMED (`model`, `manufacturer`, `version` were added in backup format v1; may be `None`). |

**MEDIUM — the ZHA backup is written in zigpy-native format, not OCBF.** `as_dict()` keys are `network_info`/`node_info`; there is no top-level `coordinator_ieee`, `pan_id`, `channel`, etc. So every `as_dict.get("coordinator_ieee")`, `.get("pan_id")`, `.get("channel")`, `.get("nwk_update_id")`, `.get("devices")`, `.get("metadata")` in `async_backup` returns `None`/`[]` — the stored metadata is empty. Two options:

(a) Keep native format (recommended — it is lossless: route table, key table seq numbers, tx_power, `stack_specific`, TC link key all round-trip, and it is what ZHA's own migration flow stores) and read fields from the object:

```python
async def async_backup(self) -> BackupPayload:
    app = self._app()
    backup = await asyncio.wait_for(
        app.backups.create_backup(load_devices=True), ZHA_BACKUP_TIMEOUT
    )
    data = json.dumps(backup.as_dict(), indent=2, sort_keys=True).encode()
    ni, node = backup.network_info, backup.node_info
    meta = {
        "format": "zigpy-network-backup",
        "format_version": backup.version,
        "is_complete": backup.is_complete(),
        "ieee": _norm_ieee(str(node.ieee)),
        "pan_id": f"0x{int(ni.pan_id):04x}",
        "extended_pan_id": _norm_ieee(str(ni.extended_pan_id)),
        "channel": ni.channel,
        "nwk_update_id": ni.nwk_update_id,
        "device_count": len(ni.nwk_addresses) or len(ni.children),
        "source": ni.source,              # e.g. "bellows@0.45.0"
        "stack": next(iter(ni.stack_specific or {}), None),
        "model": node.model, "manufacturer": node.manufacturer, "version": node.version,
        "frame_counter": ni.network_key.tx_counter,
    }
    return BackupPayload(data=data, ext=".json", meta=meta)
```

(b) If you want the file to be OCBF so it can be dropped into Z2M: `backup.as_open_coordinator_json()` exists, and then the current `as_dict.get(...)` lines are correct. OCBF loses nothing critical for zigpy (`metadata.internal` carries node/TC key/route table), and `NetworkBackup.from_dict` reads it back. Pick one and make the docstring match; consider writing both (`.json` native + `.ocbf.json`) since a ZHA→Z2M migration needs OCBF but a ZHA→ZHA restore benefits from native.

**LOW — `_norm_ieee` for ZHA.** `str(EUI64)` is `"00:12:4b:..."`. Use the same normaliser as Z2M so cross-source comparisons work.

**LOW — `radio_type`.** `type(app).__module__` gives `"bellows.zigbee.application"` etc. Better: `app.config[CONF_DEVICE]` for the path and ZHA's `config_entry.data["radio_type"]` (`"ezsp"`, `"znp"`, `"deconz"`, `"zigate"`, `"xbee"`) via `get_config_entry(hass)` in the same helpers module.

---

## 7. EZSP EUI64 overwrite flag — CONFIRMED name; WRONG policy (HIGH)

Source: [bellows/zigbee/application.py `can_write_network_settings` / `write_network_info`](https://github.com/zigpy/bellows/blob/dev/bellows/zigbee/application.py), [core zha/const.py `EZSP_OVERWRITE_EUI64`](https://github.com/home-assistant/core/blob/dev/homeassistant/components/zha/const.py), [core zha/radio_manager.py `_allow_overwrite_ezsp_ieee`](https://github.com/home-assistant/core/blob/dev/homeassistant/components/zha/radio_manager.py), [core zha/config_flow.py](https://github.com/home-assistant/core/blob/dev/homeassistant/components/zha/config_flow.py) (`async_step_restore_backup` → `DestructiveWriteNetworkSettings` → `pre_confirm_ezsp_ieee_overwrite`), [zigpy-znp api.py](https://github.com/zigpy/zigpy-znp/blob/dev/zigpy_znp/api.py) (`OsalNvIds.EXTADDR`), [zigpy-deconz application.py](https://github.com/zigpy/zigpy-deconz/blob/dev/zigpy_deconz/zigbee/application.py) (`NetworkParameter.mac_address`).

- Key name `stack_specific["ezsp"]["i_understand_i_can_update_eui64_only_once_and_i_still_want_to_do_it"]` — **CONFIRMED** (HA exports it as `homeassistant.components.zha.const.EZSP_OVERWRITE_EUI64`; import that instead of hard-coding).
- Semantics in bellows: `can_write_network_settings()` returns True without needing the flag when (1) `node_info.ieee == EUI64.UNKNOWN`, (2) backup IEEE == current IEEE, or (3) `ezsp.can_rewrite_custom_eui64()` (firmware with NV3 token support, i.e. EmberZNet ≥ 7.x builds that expose the rewritable custom-EUI token — nearly all current SkyConnect/ZBT-1/Yellow/SLZB/SMLIGHT firmware). Only when the NCP can *not* rewrite but *can* burn `MFG_CUSTOM_EUI_64` once does it require the flag; without it, it raises `DestructiveWriteNetworkSettings`; if it can neither rewrite nor burn (already burned once), `CannotWriteNetworkSettings`.
- **Setting the flag unconditionally is WRONG (HIGH).** It converts a deliberate, irreversible, one-shot operation into a silent default. ZHA's own flow does the opposite: attempts restore *without* the flag, catches `DestructiveWriteNetworkSettings`, shows the user a checkbox ("Permanently replace the radio IEEE address"), and only then re-runs with `_allow_overwrite_ezsp_ieee(backup)`. Mirror that: set it only when the live IEEE differs from the backup's **and** the user passed `force`/confirm, and surface `DestructiveWriteNetworkSettings` as a clear "needs confirmation" error otherwise. Also the flag only matters for `ezsp`; harmless but pointless for other stacks.

```python
from zigpy.exceptions import CannotWriteNetworkSettings, DestructiveWriteNetworkSettings

async def async_restore(self, data: bytes, meta: dict[str, Any]) -> None:
    app = self._app()
    backup = await self.hass.async_add_executor_job(self._parse, data)
    allow_ieee_burn = bool(meta.get("allow_ieee_overwrite"))  # from the service's `force`/confirm
    if allow_ieee_burn:
        from homeassistant.components.zha.const import EZSP_OVERWRITE_EUI64  # noqa: PLC0415
        backup = backup.replace(
            network_info=backup.network_info.replace(
                stack_specific={
                    **backup.network_info.stack_specific,
                    "ezsp": {**backup.network_info.stack_specific.get("ezsp", {}), EZSP_OVERWRITE_EUI64: True},
                }
            )
        )
    try:
        await app.can_write_network_settings(network_info=backup.network_info, node_info=backup.node_info)
    except DestructiveWriteNetworkSettings as err:
        raise RuntimeError(
            "The new Silicon Labs radio can only have its IEEE address written once with its current "
            "firmware. Upgrade the firmware, or re-run the restore with force=true to burn it permanently."
        ) from err
    except CannotWriteNetworkSettings as err:
        raise RuntimeError(f"Radio cannot accept this backup: {err}") from err
    ...
```

- **Other stacks:** nothing similar is needed. `zigpy-znp` writes `OsalNvIds.EXTADDR` from `node_info.ieee` (deleting it if UNKNOWN, which resets to the hardware MAC) — freely rewritable. `zigpy-deconz` writes `NetworkParameter.mac_address` when `node_info.ieee != UNKNOWN` (rewritable; no way back to the factory MAC, but not one-shot). ZiGate/XBee do not support `write_network_info` meaningfully.

---

## 8. Concurrency / safety — several issues (one BLOCKER)

Source: [zigpy/application.py `initialize()`](https://github.com/zigpy/zigpy/blob/dev/zigpy/application.py), [zigpy/backups.py `restore_backup`](https://github.com/zigpy/zigpy/blob/dev/zigpy/backups.py), [bellows `write_network_info` / `reset_network_info`](https://github.com/zigpy/bellows/blob/dev/bellows/zigbee/application.py), [core zha/radio_manager.py `restore_backup`, `create_zigpy_app`](https://github.com/home-assistant/core/blob/dev/homeassistant/components/zha/radio_manager.py), [core zha/config_flow.py](https://github.com/home-assistant/core/blob/dev/homeassistant/components/zha/config_flow.py) (`async_unload` before migrate), [core zha/__init__.py](https://github.com/home-assistant/core/blob/dev/homeassistant/components/zha/__init__.py) (`NetworkSettingsInconsistent` handling), [core zha/repairs/network_settings_inconsistent.py](https://github.com/home-assistant/core/blob/dev/homeassistant/components/zha/repairs/network_settings_inconsistent.py).

**ZHA `create_backup` while the network is running — SAFE (CONFIRMED).** ZHA does exactly this from its UI ("Network settings → Create backup") and on its periodic timer (`CONF_NWK_BACKUP_ENABLED`, every `backup_period` minutes). `load_network_info(load_devices=True)` is a read-only NVRAM/key-table dump. It is serialised through the radio's request queue; fine to call concurrently with traffic. Note it also appends to `app.backups.backups` (persisted in `zigbee.db`), so Radio Guard backups will also appear in ZHA's backup list — acceptable, and the list is capped by zigpy.

**ZHA `restore_backup` on the live gateway's `application_controller` — BLOCKER.** Every upstream restore path (`ZhaRadioManager.restore_backup`, the config-flow migration, the inconsistent-settings repair) first **unloads the ZHA config entry** (`hass.config_entries.async_unload`) and then creates a *fresh, throw-away* `ControllerApplication` via `create_zigpy_app()` with `auto_form=False, start_radio=False`, `connect()` only (no `initialize()`/`start_network()`), performs `can_write_network_settings` + `backups.restore_backup`, `shutdown()`s it, sleeps `CONNECT_DELAY_S`, and finally `async_setup`s the entry again. Reasons the current in-place approach is unsafe:

1. `write_network_info` on bellows calls `reset_network_info()` → `factory_reset()`, `clear_gp_tables()`, `reset_custom_eui64()`, `_reset()` (restart EZSP), `leaveNetwork()`, then forms the network, then `_reset()` again. znp does `migrate_nvram()` + `reset()`. The running ZHA gateway has listeners, the watchdog loop, the periodic-backup task and in-flight device requests bound to that connection; they will error/raise during the reset and the gateway is left in an undefined state. Nothing re-runs `start_network()`/`initialize()` afterwards, so devices, groups and the coordinator device object are not reloaded.
2. `restore_backup(create_new=True)` then calls `create_backup()` on the same app; on a half-reset radio this can fail or record an inconsistent backup.
3. ZHA's startup validates the radio against the most recent stored backup (`CONF_NWK_VALIDATE_SETTINGS`) and raises `NetworkSettingsInconsistent` → `ConfigEntryError` + a repair issue if they differ. A restore performed *outside* ZHA's own bookkeeping (or interrupted) can trip this on the next reload.
4. "Does restore_backup handle the radio reconnect?" — **No.** It only writes NVRAM. Reconnection/`initialize()` is the caller's job; in ZHA that is the config-entry reload.

Correct approach for Radio Guard (reuse HA's own helper so the semantics stay in lockstep with ZHA):

```python
async def async_restore(self, data: bytes, meta: dict[str, Any]) -> None:
    from homeassistant.components.zha.helpers import get_config_entry  # noqa: PLC0415
    from homeassistant.components.zha.radio_manager import ZhaRadioManager  # noqa: PLC0415
    from zigpy.exceptions import CannotWriteNetworkSettings, DestructiveWriteNetworkSettings  # noqa: PLC0415

    backup = await self.hass.async_add_executor_job(self._parse, data)
    entry = get_config_entry(self.hass)
    mgr = ZhaRadioManager.from_config_entry(self.hass, entry)

    _LOGGER.info("Unloading ZHA to restore network backup")
    if not await self.hass.config_entries.async_unload(entry.entry_id):
        raise RuntimeError("Could not unload ZHA")
    try:
        # Reads the stick's current settings into mgr.current_settings; restore_backup()
        # early-returns if the stick already *supersedes* the backup (newer frame counter,
        # same network), so a no-op restore is safe.
        await mgr.async_load_network_settings()
        try:
            await asyncio.wait_for(
                mgr.restore_backup(backup, overwrite_ieee=bool(meta.get("allow_ieee_overwrite"))),
                RESTORE_TIMEOUT,
            )
        except DestructiveWriteNetworkSettings as err:
            raise RuntimeError("New radio needs a one-time IEEE overwrite; re-run with force=true") from err
        except CannotWriteNetworkSettings as err:
            raise RuntimeError(f"Radio refused the backup: {err}") from err
    finally:
        _LOGGER.info("Reloading ZHA")
        await self.hass.config_entries.async_setup(entry.entry_id)
```

Notes on this path:
- `ZhaRadioManager` is an internal HA module (no stability guarantee), but it is the same helper the ZHA config flow and repair flow use; pin tests against it. If you prefer to avoid it, replicate `create_zigpy_app()`: `radio_type.controller.new({CONF_DEVICE: entry.data["device"], CONF_DATABASE: <zigbee.db path>, CONF_NWK_BACKUP_ENABLED: False, CONF_USE_THREAD: False}, auto_form=False, start_radio=False)` → `await app.connect()` → `await app.can_write_network_settings(...)` → `await app.backups.restore_backup(backup)` → `await app.shutdown()` → `await asyncio.sleep(1.5)`.
- `restore_backup` bumps the network-key frame counter by `counter_increment=10000` (default) so the restored coordinator is ahead of anything the devices last saw. If the backup is old and the live network has sent > 10000 frames since, devices will drop the coordinator's frames as replays; ZHA's own flow tolerates this because it restores a *fresh* backup. In the pre-flight, compare `backup.network_info.network_key.tx_counter` against the current one and warn when the backup is older/lower; offer a larger `counter_increment` on force.
- Passing `node_info.ieee` to the new stick means the **old** stick, if ever plugged in again, is a duplicate TC. ZHA's flow resets the old radio for this reason; Radio Guard should tell the user.
- After `async_setup`, ZHA re-validates settings vs. its most recent stored backup. Because `restore_backup(create_new=True)` stores the post-restore state as the newest backup, this passes.

**Z2M concurrency — MEDIUM.** The restore stops the add-on, so Z2M itself is quiescent; but Radio Guard's own scheduled `async_backup` or `_bridge_info` may fire while the add-on is down and wait the full timeout. Hold a per-source `asyncio.Lock` around backup/restore and skip the scheduled backup while a restore is in flight.

**Z2M backup during Z2M's own daily backup — LOW.** herdsman writes `coordinator_backup.json` via `.tmp` + rename, and `backup()` first awaits `zigbee.backup()` then reads files, so a torn read is not possible. Fine.

**Both sources — LOW.** `_write_restore_files` writes `database.db` with plain `write_bytes`; use a temp file + `os.replace` and set mode `0600`, since `coordinator_backup.json` contains the network key.

---

## Restore runbook

Assumptions: the backup zip (Z2M) or JSON (ZHA) was created by Radio Guard while the old coordinator was healthy. "Same chip" = same herdsman/zigpy stack (`zstack`→`zstack`, `ember`→`ember`, `znp`→`znp`, `ezsp`→`ezsp`).

### A. Replace a dead Z2M coordinator with the same stack

1. **Do not start Z2M with the new stick yet.** If the add-on's watchdog or HA restarts it, it will see a blank stick and, if `configuration.yaml` still matches the backup, restore automatically — which is fine — but if it does *not* match it forms a new network and wipes `database.db`. Stop the add-on first.
2. Plug in the new stick. If it is a **TI (zstack)** stick, flash current Z-Stack 3.x coordinator firmware (not Z-Stack 1.2; restore onto 1.2 is refused). If **Silicon Labs (ember)**, flash EmberZNet NCP ≥ 7.4 (EZSP v13+) and, **before step 4, copy the old coordinator's IEEE onto it** with XZG Multi-Tool / Silabs tooling (ember restore does not write the IEEE; see §4e). The old IEEE is in `coordinator_backup.json → coordinator_ieee` and `bridge/info → coordinator.ieee_address`.
3. Update `serial.port` (and `baudrate`/`rtscts` if the hardware differs) in the add-on options or `configuration.yaml`. Keep `serial.adapter` unchanged.
4. **Verify `configuration.yaml` `advanced.pan_id`, `advanced.ext_pan_id`, `advanced.network_key`, `advanced.channel` equal the values in the backup's `coordinator_backup.json`** (`pan_id` hex→decimal, `ext_pan_id`/`network_key` hex→byte lists). If any is `GENERATE` or differs, set it to the backup's value. This is the gate for herdsman's `restoreBackup` strategy.
5. Run the Radio Guard restore (or manually: with the add-on stopped, copy `coordinator_backup.json`, `database.db` and — if present — `devices.yaml`/`groups.yaml`/`state.json` from the zip into the Z2M data dir, default `/config/zigbee2mqtt` on the add-on = `<HA config>/zigbee2mqtt`; `/share/zigbee2mqtt` on pre-1.15 installs or if `data_path` was changed).
6. Start the add-on. Check the log for `Started with result 'restored'` (zstack: "(stage-2) configuration matches backup" / "(stage-5) adapter backup matches configuration"; ember: "[INIT TC] Forming from backup."). If you see `'reset'`, "Config does not match backup" or "Forming from config", **stop immediately**: the network was re-formed and `database.db` was moved to `database.db.backup`. Fix step 4, restore `database.db.backup` → `database.db`, put the original `coordinator_backup.json` back (Z2M overwrote it), and repeat.
7. Confirm in the frontend (Settings → About) that the coordinator IEEE, PAN ID and channel match the backup. Devices rejoin on their own; power-cycle routers that stay offline. Mains routers are usually back within minutes, sleepy end devices after their next wake/poll.

### B. Migrate Z2M to a different stack (zstack ↔ ember)

Officially "might work, not supported" per the Z2M FAQ. Expect to re-pair some devices.

1. Stop Z2M. Take a fresh backup first if the old stick still works (Radio Guard `backup_now`).
2. Flash the new stick with coordinator firmware for its stack (Z-Stack 3.x or EmberZNet ≥ 7.4).
3. **Copy the old IEEE onto the new stick** with an external tool (mandatory here: for ember herdsman never writes it; for zstack it does, but copying it up front avoids a mismatch during the provisioning step). For EFR32 without NV3-token firmware this is a one-time burn — be sure.
4. Edit `configuration.yaml`: `serial.adapter: ember` (or `zstack`), new `serial.port`, correct `baudrate` (ember adapters are usually 115200; some 230400), and keep `advanced.pan_id`/`ext_pan_id`/`network_key`/`channel` exactly as in the backup (same gate as A.4).
5. **Convert the backup if going TI → Silabs.** ember's loader rejects a `coordinator_backup.json` that has no `stack_specific.ezsp` block and no `metadata.internal.ezspVersion`. Options: (a) load the zstack OCBF into ZHA/zigpy (`NetworkBackup.from_dict`) and `write_network_info` to the new stick with a throw-away zigpy app (then let Z2M start against the already-formed network — with config matching, herdsman's ember path hits `NetworkInitAction.DONE`, "Adapter network matches config", and will write a fresh ember `coordinator_backup.json`), or (b) edit the JSON: add `"stack_specific": {"ezsp": {"hashed_tclk": "<32 hex>"}}` and `"metadata": {"internal": {"ezspVersion": 13}}`. There is no exact TCLK equivalence between stacks, so devices that rely on the TC link key may need re-pairing either way. Silabs → TI loads without conversion (TCLK seed is just missing; per-device APS keys in `devices[].link_key` are used instead).
6. Place `coordinator_backup.json` (converted) and `database.db` in the data dir, start Z2M, and check for `'restored'` as in A.6.
7. Power-cycle mains routers; re-pair anything still unreachable after ~30 min (the FAQ: "re-pair some routers close to the coordinator while only permitting joining via the coordinator").

### C. Replace a dead ZHA coordinator

Preferred path is ZHA's own UI: **Settings → Devices & services → Zigbee Home Automation → Configure → Migrate radio**, which unloads ZHA, probes the new stick, offers "Migrate automatically" (uses ZHA's last stored backup — which Radio Guard's `create_backup` calls also add to) or "Advanced → upload a backup", handles the Silabs one-time IEEE prompt, resets the old radio, and reloads. Radio Guard's value is holding an off-box copy when `zigbee.db` is gone. If using Radio Guard's restore:

1. Plug in the new stick (any stack ZHA supports for `write_network_info`: `ezsp`, `znp`, `deconz` ≥ fw 0x26700700). If the radio type changed, first fix the ZHA config entry's `radio_type`/`device.path` (ZHA UI → Configure → "Migrate or change adapter settings" → "Re-configure the current radio" or the migrate flow) so `ZhaRadioManager.from_config_entry` opens the right port with the right driver.
2. Run the Radio Guard restore with the saved `.json`. Internally it must: unload ZHA, open a temporary zigpy app on the new stick, `can_write_network_settings`, `backups.restore_backup`, shut down, reload ZHA (§8). Do not run it while ZHA is loaded against the new stick.
3. If the error says the Silicon Labs radio needs a one-time IEEE overwrite (`DestructiveWriteNetworkSettings`), either flash newer EmberZNet firmware (which makes the custom EUI64 rewritable) or re-run with `force: true` to burn it. Without the old IEEE many devices will not resume; with it, never run the old stick on the same site again without re-flashing/erasing its IEEE.
4. After reload, check **ZHA → Configure → Network settings**: PAN ID, extended PAN ID, channel and coordinator IEEE must equal the backup. If ZHA raises "Network settings do not match most recent backup" (`NetworkSettingsInconsistent` repair), choose "Restore old settings" in the repair to re-apply the stored backup, or "Use new settings" if the restore was intended.
5. Devices rejoin on their own (ZHA docs: "normally within one hour"); power-cycle routers to speed it up. If the backup was taken long ago, expect some devices to reject the coordinator (frame-counter replay) until re-paired — restoring from the most recent Radio Guard backup minimises this.

---

## Summary of required code changes (by severity)

| # | Severity | File | Change |
|---|---|---|---|
| 5 | BLOCKER | z2m.py | Replace removed `hassio.async_get_addon_info/async_start_addon/async_stop_addon/is_hassio` with `helpers.hassio.is_hassio` + `hassio.AddonManager`; otherwise the Supervisor path never runs. |
| 5 | BLOCKER | z2m.py | Drop `/addon_configs/{slug}`; data dir is `hass.config.path("zigbee2mqtt")` (add-on `data_path` default `/config/zigbee2mqtt`) or `/share/zigbee2mqtt`; read `data_path` from add-on options. |
| 4c | BLOCKER | z2m.py | Pre-flight and enforce that `configuration.yaml` `advanced.{pan_id,ext_pan_id,network_key,channel}` match the OCBF, else Z2M re-forms the network and wipes the database. |
| 8 | BLOCKER | zha.py | Never call `restore_backup` on the live gateway's app; unload ZHA, use a throw-away zigpy app (`ZhaRadioManager.restore_backup` or equivalent), then `async_setup` the entry. |
| 7 | HIGH | zha.py | Do not set the EZSP one-shot EUI64 flag unconditionally; set only on explicit force after `DestructiveWriteNetworkSettings`. Import `EZSP_OVERWRITE_EUI64` from `zha.const`. |
| 2/3 | HIGH | z2m.py | Normalise IEEE (`0x` vs bare hex) and PAN ID (decimal int vs hex string) so `async_restore_check` comparisons can ever match. |
| 4e | HIGH | z2m.py | Warn that ember restores do not write the IEEE; user must copy it externally. Warn on cross-stack (`stack_specific` vs `serial.adapter`). |
| 2 | MEDIUM | z2m.py | `zigbee_herdsman_version` → `zigbee_herdsman.version`. |
| 3 | MEDIUM | z2m.py | Docstring: zip is the whole data dir minus `log/`/`ota/`; `coordinator_backup.json` absent for deconz/zigate/zboss. Treat zip as secret. |
| 6 | MEDIUM | zha.py | `as_dict()` is zigpy-native, not OCBF; metadata extraction currently yields `None`s. Read from the `NetworkBackup` object or use `as_open_coordinator_json()`. |
| 4d | MEDIUM | z2m.py | Also restore `devices.yaml`/`groups.yaml`/`state.json`; merge, don't clobber, `configuration.yaml`/`secret.yaml`. |
| 8 | MEDIUM | both | Per-source lock; verify post-restart identity via `bridge/state` + `bridge/info` and report `'reset'` outcomes. |
| 1 | LOW | z2m.py | Add `transaction` correlation; wait for subscribe debounce; mention `maximum_packet_size` on timeout. |
