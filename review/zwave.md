# Review: `custom_components/radio_guard/backup/zwave.py`

Reviewed 2026-11-07 against upstream `dev`/`master` plus tagged releases:

- home-assistant/core `dev` (zwave_js requires `zwave-js-server-python==0.74.0`)
- home-assistant-libs/zwave-js-server-python `main` (0.74.0, schema 51)
- zwave-js/zwave-js-server `master` (3.11.0, zwave-js ^15.31.0, maxSchemaVersion 51)
- zwave-js/node-zwave-js `master` (15.32.0, 2026-11-03)
- home-assistant/addons `zwave_js` 1.10.0

Severity tags: **[BLOCKER]** restore will fail or leave HA broken; **[HIGH]** wrong on a
supported HA version or misleading to the user in a way that risks the network;
**[MED]** correctness/robustness; **[LOW]** polish.

Files reviewed: `backup/zwave.py`, `backup/base.py`, `const.py` (the task's `backup/const.py`
does not exist; the constants live in `custom_components/radio_guard/const.py`:
`ZWAVE_BACKUP_TIMEOUT = 300`, `RESTORE_TIMEOUT = 600`).

---

## 1. Where the HA integration keeps its client — CONFIRMED (current), WRONG (fallback) **[HIGH]**

### Current layout (HA ≥ 2025.8)

`entry.runtime_data` is a `ZwaveJSData` dataclass with attribute `client`:

```python
# homeassistant/components/zwave_js/models.py (dev)
@dataclass
class ZwaveJSData:
    client: ZwaveClient
    driver_events: DriverEvents
    old_server_log_level: LogLevel | None = None
    network_neighbors_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

type ZwaveJSConfigEntry = ConfigEntry[ZwaveJSData]
```

Source: https://github.com/home-assistant/core/blob/dev/homeassistant/components/zwave_js/models.py
(`entry.runtime_data = entry_runtime_data` in `__init__.py` `async_setup_entry`). Core itself
reads it as `entry.runtime_data.client` (api.py `_async_get_entry`, helpers.py, config_flow.py).

So `getattr(runtime, "client", None)` in `_controller()` is **correct for HA 2025.8+**.

### Version history (verified by probing tagged files)

| HA release | Layout | `hass.data[DOMAIN][entry_id]` |
|---|---|---|
| ≤ 2024.5 | `hass.data[DOMAIN][entry.entry_id][DATA_CLIENT]`, `DATA_CLIENT = "client"` | yes |
| 2024.6 – 2025.7 | `entry.runtime_data = {DATA_CLIENT: client, DATA_DRIVER_EVENTS: ...}` — a **plain dict**, key `"client"` | **no** (`hass.data[DOMAIN]` is only the YAML config dict) |
| ≥ 2025.8 | `entry.runtime_data = ZwaveJSData(client=..., ...)` dataclass | no |

Sources (tag-pinned):
- 2024.5.0: https://github.com/home-assistant/core/blob/2024.5.0/homeassistant/components/zwave_js/__init__.py#L200 (`entry_hass_data[DATA_CLIENT] = client`), const.py `DATA_CLIENT = "client"`
- 2024.6.0: https://github.com/home-assistant/core/blob/2024.6.0/homeassistant/components/zwave_js/__init__.py#L198 (`entry.runtime_data[DATA_CLIENT] = client`)
- 2025.7.0: https://github.com/home-assistant/core/blob/2025.7.0/homeassistant/components/zwave_js/__init__.py#L265 (`entry.runtime_data = {DATA_CLIENT: client}`)
- 2025.8.0: first release with `ZwaveJSData(` in `__init__.py`.

### What is wrong

`hacs.json` declares `"homeassistant": "2024.10.0"`. On every release from 2024.6 through
2025.7 `runtime_data` is a **dict**, so `getattr(runtime, "client", None)` returns `None`,
and the fallback `self.hass.data[ZWAVE_DOMAIN][entry.entry_id]["client"]` raises `KeyError`
(hass.data[DOMAIN] is the YAML config, not per-entry). Result: `SourceUnavailable("Z-Wave JS
client not found")` on the whole 2024.10–2025.7 range the manifest claims to support. The
comment "Older cores kept it in hass.data" is only true for ≤ 2024.5, which is below the
declared minimum.

### Corrected code

```python
def _client(self, entry: ConfigEntry):
    runtime = getattr(entry, "runtime_data", None)
    client = None
    if runtime is not None:
        # HA >= 2025.8: ZwaveJSData dataclass
        client = getattr(runtime, "client", None)
        # HA 2024.6 .. 2025.7: plain dict keyed by DATA_CLIENT == "client"
        if client is None and isinstance(runtime, dict):
            client = runtime.get("client")
    if client is None:
        # HA <= 2024.5 only
        try:
            client = self.hass.data[ZWAVE_DOMAIN][entry.entry_id]["client"]
        except (KeyError, TypeError) as err:
            raise SourceUnavailable("Z-Wave JS client not found") from err
    return client
```

Given the other findings below (restore API only correct on 0.65+ client / HA 2025.7+), the
simpler fix is to raise `hacs.json` to `"homeassistant": "2025.8.0"` and drop the fallbacks.

---

## 2. `zwave_js_server` Controller API — CONFIRMED (signatures), WRONG on older clients **[HIGH]**

### Exact signatures (0.74.0, `zwave_js_server/model/controller/__init__.py`)

```python
async def async_backup_nvm_raw(self) -> bytes:                      # require_schema=14
async def async_backup_nvm_raw_base64(self) -> str:                 # require_schema=14, returns data["nvmData"] (base64 str)
async def async_restore_nvm(self, file: bytes,
                            options: dict[str, bool] | None = None) -> None:        # require_schema=42
async def async_restore_nvm_base64(self, base64_data: str,
                                   options: dict[str, bool] | None = None) -> None: # require_schema=42
```

Both restore variants send `{"command": "controller.restore_nvm", "nvmData": <b64>,
"migrateOptions": {} if options is None else options}`.
Source: https://github.com/home-assistant-libs/zwave-js-server-python/blob/main/zwave_js_server/model/controller/__init__.py#L760-L798

Server side (`zwave-js-server/src/lib/controller/message_handler.ts`) passes
`message.migrateOptions` straight into `driver.controller.restoreNVM(nvmData, convertProgress,
restoreProgress, migrateOptions)`. Source: https://github.com/zwave-js/zwave-js-server/blob/master/src/lib/controller/message_handler.ts#L269-L292

`MigrateNVMOptions` (zwave-js, `docs/api/controller.md`):

```ts
interface MigrateNVMOptions {
  preserveApplicationData?: boolean;   // default true
  preserveSUCUpdateEntries?: boolean;  // default true
  preserveRoutes?: boolean;            // default true — LWR, NLWR and priority-route flag
  preserveNeighbors?: boolean;         // default true
}
```

Source: https://github.com/zwave-js/node-zwave-js/blob/master/docs/api/controller.md#nvm-backup-and-restore
and `packages/nvmedit/src/convert.ts` `migrateNVM()` (defaults all `true`). `{"preserveRoutes":
False}` is exactly what core passes in `websocket_restore_nvm` — CONFIRMED.

### Version gotcha (WRONG for HA 2025.4–2025.6)

The `options` parameter was added in zwave-js-server-python **0.65.0** (HA 2025.7). In
0.62–0.64 (HA 2025.4–2025.6) the signature is `async_restore_nvm_base64(self, base64_data: str)`
with `require_schema=14`; passing a second positional arg raises `TypeError`. Before 0.62 (HA
≤ 2025.3) `async_backup_nvm_raw_base64` and `async_restore_nvm_base64` **do not exist**
(`AttributeError`). Sources: tag-pinned
https://github.com/home-assistant-libs/zwave-js-server-python/blob/0.62.0/zwave_js_server/model/controller/__init__.py#L763
and 0.61.0 (no `_base64` methods).

`migrateOptions` is honoured server-side from `@zwave-js/server` **3.0.1** (schema 42, zwave-js
^15.3.0, 2025-05-05). On an older server the client raises `InvalidServerVersion` before sending.
Sources: https://github.com/zwave-js/zwave-js-server/blob/3.0.1/src/lib/const.ts,
https://github.com/zwave-js/zwave-js-server/blob/3.0.1/src/lib/controller/message_handler.ts

### Events that fire

Controller events (python client `handle_nvm_*`, server `trySendEvent`):

- backup: `"nvm backup progress"` `{bytesRead, total}`
- restore: `"nvm convert progress"` `{bytesRead, total}` then `"nvm restore progress"` `{bytesWritten, total}`
- after restore the driver soft-resets and re-interviews the controller, emitting **`"driver ready"`** on the Driver (zwave-js ≥ 15.3.0 no longer destroys the driver; before that the restore call rejected with "controller instance is being destroyed" and the server reconnected). The HA integration listens for `"driver ready"` and calls `async_schedule_reload` (init `DriverEvents.setup`).

Sources: https://github.com/zwave-js/node-zwave-js/blob/master/CHANGELOG.md (15.3.0 "no longer destroys the driver instance after NVM restore"), controller.py `handle_nvm_backup_progress/convert/restore`.

### Controller properties — CONFIRMED

All exist as `@property` on `Controller`: `home_id` (int), `own_node_id`, `sdk_version` (str
like `"7.19.3"`), `firmware_version` (str), `manufacturer_id`, `product_type`, `product_id`,
`controller_type` (int, `data["type"]`), `nodes` (dict[int, Node]). `getattr(ctrl,
"controller_type", None)` is unnecessary but harmless. Also useful: `inclusion_state`
(`InclusionState` IntEnum: IDLE/INCLUDING/EXCLUDING/BUSY/SMART_START), `is_rebuilding_routes`,
`rf_region`, `is_primary`, `is_suc`, `supported_function_types`.
Source: https://github.com/home-assistant-libs/zwave-js-server-python/blob/main/zwave_js_server/model/controller/__init__.py#L110-L240

Minor: `async_backup_nvm_raw()` already returns `bytes`; the base64 round-trip in
`async_backup`/`async_restore` is wasted work. Prefer `async_backup_nvm_raw()` /
`async_restore_nvm(data, {...})` (both are what core's migrate flow uses). **[LOW]**

---

## 3. What core does after restore; does our code need to update `unique_id`? — WRONG **[BLOCKER]**

### Core's `websocket_restore_nvm` (dev `api.py` L3233–3308), verbatim logic

```python
wait_for_driver_ready = async_wait_for_driver_ready_event(entry, driver)   # subscribe BEFORE restoring
await controller.async_restore_nvm_base64(msg["data"], {"preserveRoutes": False})
with suppress(TimeoutError):
    await wait_for_driver_ready()        # waits for "driver ready" AND entry back to LOADED (60 s)
try:
    version_info = await async_get_version_info(hass, entry.data[CONF_URL])  # fresh /version handshake
except CannotConnect:
    LOGGER.error("Failed to get server version, cannot update config entry unique id ...")
else:
    hass.config_entries.async_update_entry(entry, unique_id=str(version_info.home_id))
await hass.config_entries.async_reload(entry.entry_id)
```

Source: https://github.com/home-assistant/core/blob/dev/homeassistant/components/zwave_js/api.py#L3272-L3297

Helpers (all in `homeassistant/components/zwave_js/helpers.py`, importable):

```python
from homeassistant.components.zwave_js.helpers import (
    async_get_version_info,            # HA >= 2025.6  (async, raises CannotConnect; 10 s timeout)
    async_wait_for_driver_ready_event, # HA >= 2025.8  (@callback; returns awaitable; DRIVER_READY_EVENT_TIMEOUT = 60)
    CannotConnect,                     # HA >= 2025.6
)
```

Version check (tag-pinned): 2025.4.0 helpers has none of them; 2025.6.0 has
`async_get_version_info` + `CannotConnect`; 2025.8.0 adds `async_wait_for_driver_ready_event`.
`async_get_version_info` is a thin wrapper around
`zwave_js_server.version.get_server_version(ws_url, session)` which returns `VersionInfo` with
`.home_id: int`, so you can call that directly on any version.

### Why `unique_id` matters — the reload does NOT fail, but HA opens an ERROR repair

The zwave_js config entry's `unique_id` is `str(home_id)`. On every setup, `DriverEvents.setup`
compares it with `str(driver.controller.home_id)`; if different it creates a **fixable repair
issue** `migrate_unique_id.<entry_id>` with severity ERROR titled "An unknown adapter was
detected", whose text tells the user it may be because "A backup of the adapter's non-volatile
memory was restored to the adapter using a 3rd party application" and asks them to confirm.
Source: https://github.com/home-assistant/core/blob/dev/homeassistant/components/zwave_js/__init__.py#L421-L461
and strings.json `issues.migrate_unique_id`.

Our `async_restore` only calls `async_reload` (wrapped in `suppress(Exception)`), so after a
restore from Radio Guard the user gets that repair every time. Worse, there's a race: the
integration itself reloads on `"driver ready"` (`async_schedule_reload`), and our immediate
`async_reload` can collide with it. Also: python-client `Controller.home_id` is **stale** after
restore (controller state object isn't refreshed until reload), which is why core goes back to
`/version` — do not read `ctrl.home_id` after restoring.

### Corrected `async_restore`

```python
async def async_restore(self, data: bytes, meta: dict[str, Any]) -> None:
    entry = self._entry()
    if entry is None:
        raise SourceUnavailable("Z-Wave JS integration is not loaded")
    client = self._client(entry)
    driver = client.driver
    if driver is None:
        raise SourceUnavailable("Z-Wave JS driver not ready")
    ctrl = driver.controller

    from homeassistant.components.zwave_js.helpers import (  # HA >= 2025.8
        CannotConnect,
        async_get_version_info,
        async_wait_for_driver_ready_event,
    )

    # Subscribe BEFORE issuing the restore; the driver emits "driver ready" and the
    # integration schedules its own reload as part of the restore.
    wait_for_driver_ready = async_wait_for_driver_ready_event(entry, driver)

    _LOGGER.info("Restoring Z-Wave NVM to controller")
    await asyncio.wait_for(
        ctrl.async_restore_nvm(data, {"preserveRoutes": False}),
        RESTORE_TIMEOUT,
    )
    with suppress(TimeoutError):
        await wait_for_driver_ready()

    # Controller state in the client is stale; read the new home ID via /version.
    try:
        version_info = await async_get_version_info(self.hass, entry.data["url"])
    except CannotConnect:
        _LOGGER.error("Could not read new home ID after NVM restore; HA will raise a "
                      "'migrate_unique_id' repair you must confirm")
    else:
        self.hass.config_entries.async_update_entry(
            entry, unique_id=str(version_info.home_id)
        )
    await self.hass.config_entries.async_reload(entry.entry_id)
```

If you need to support HA < 2025.8, replicate `async_wait_for_driver_ready_event` locally:

```python
def _wait_for_driver_ready(entry, driver, timeout=60):
    ready = asyncio.Event(); loaded = asyncio.Event()
    unsubs = [driver.once("driver ready", lambda _e: ready.set()),
              entry.async_on_state_change(
                  lambda: loaded.set() if entry.state is ConfigEntryState.LOADED else None)]
    async def wait():
        try:
            async with asyncio.timeout(timeout):
                await asyncio.gather(ready.wait(), loaded.wait())
        finally:
            for u in unsubs: u()
    return wait
```

and use `zwave_js_server.version.get_server_version(entry.data["url"],
async_get_clientsession(hass))` with a 10 s timeout in place of `async_get_version_info`.

Note on failure semantics: core's `_async_restore_network_backup` (migrate flow) catches
`FailedCommand` from `async_restore_nvm` and aborts. Ours lets any exception propagate, which is
fine, but do not `suppress(Exception)` around the reload: if the reload fails the user needs to
know.

---

## 4. NVM compatibility rules — PARTIALLY WRONG **[HIGH]**

Source of truth: `packages/nvmedit/src/convert.ts` `migrateNVM()` + `jsonToNVM()` /
`jsonToNVM500()`, and `Controller.getRestoreNVMTask()`.
https://github.com/zwave-js/node-zwave-js/blob/master/packages/nvmedit/src/convert.ts#L2094-L2333

### What zwave-js actually supports

| Source → Target | Supported? | Mechanism |
|---|---|---|
| 500 → 500 | yes | `jsonToNVM500(json, target.protocolVersion)`; target `meta` + `rfConfig` kept |
| 500 → 700/800 | yes | `json500To700(source, truncateApplicationData=true)` → `jsonToNVM(json, target.applicationVersion)`; target RF config kept |
| 700 → 700, 700 → 800, 800 → 800, 800 → 700 | yes | both parse as "700" (NVM3). Same-size/same-FS + preserveAll + listening static controller + target protocol ≥ 7.16 ≥ source → raw copy; otherwise JSON round-trip with target `meta` |
| **700/800 → 500** | **yes** | `json700To500()` exists and is used when `source.type === 700 && target.type === 500`. LR nodes are dropped. |
| Either side on SDK < 6.61 (pre-NVM-JSON 500 formats) | no | `"Did not find a matching implementation for protocol version ..."` / both-unknown passthrough only |
| Source or target protocol file format > 5 (`MAX_PROTOCOL_FILE_FORMAT = 5`) | partial | if **target** is newer and source ≤ target and same size and preserveAll → raw copy (controller firmware upgrades it); otherwise `ZW0280 NVM_NotSupported` "The target/source NVM has an unsupported format" |

So the warning in `async_restore_check` — "zwave-js converts forward (500->700->800) but cannot
restore to an older series" — is **wrong**: downgrade 700/800→500 is implemented (with LR nodes
and 700-only data lost), and there's no distinct 700→800 step; 700 and 800 share the NVM3
format and differ by `sharedFileSystem` and page size (800: shared FS, `ZWAVE_SHARED_NVM_SIZE
= 0xa000`; 700: app 0x3000 + protocol 0x9000). The `sdk_version`-based warning also fires on
every restore even when source and target are identical. Replace with a check on actual
constraints:

```python
def _series(sdk: str | None) -> int | None:
    if not sdk: return None
    major = int(sdk.split(".")[0])
    return 500 if major < 7 else 700   # 700 and 800 both report SDK 7.x; distinguish by product if needed

bak_sdk, cur_sdk = meta.get("sdk_version"), check.current.get("sdk_version")
if bak_sdk and _series(bak_sdk) == 500 and AwesomeVersion(bak_sdk) < "6.61":
    check.ok = False
    check.reasons.append(f"Backup is from SDK {bak_sdk}; zwave-js can only convert 500-series NVMs from SDK 6.61+")
if cur_sdk and _series(cur_sdk) == 500 and AwesomeVersion(cur_sdk) < "6.61":
    check.ok = False
    check.reasons.append(f"Target is SDK {cur_sdk}; must be 6.61+ to receive a converted NVM")
if cur_sdk and _series(cur_sdk) == 700 and AwesomeVersion(cur_sdk) < "7.19.0":
    check.warnings.append(f"Target SDK {cur_sdk}: NVM restore on 700/800 series needs SDK 7.19.0+ (7.23.2+ recommended); upgrade controller firmware first")
if bak_sdk and cur_sdk and _series(bak_sdk) == 700 and _series(cur_sdk) == 500:
    check.warnings.append("Restoring 700/800 backup to a 500-series stick: Long Range nodes and 700-only data are dropped")
```

(`AwesomeVersion` is already a HA dependency: `from awesomeversion import AwesomeVersion`.)

### Different manufacturer, same series

Allowed. zwave-js converts to the **target's** `meta` (page size/FS layout), keeps the target's
`applicationVersion` and `rfConfig`, and `normalizeControllerNodeInfo()` fixes the controller
node entry. That is the documented 700→800 path (Zooz ZST10→ZST39, Zooz→ZWA-2, UZB-7→Zooz).
The practical failure is `ZW0280` when the target firmware writes a protocol file format zwave-js
doesn't parse yet (seen when a brand-new 800 stick is ahead of the installed zwave-js) or when the
source is a 7.23.0/7.23.1 stick missing the protocol-version file (zwave-js has a heuristic
work-around since 14.x, #7846).

### Known gotchas (with sources)

1. **Firmware floor.** NVM backup/restore on 700/800 requires controller firmware based on
   Z-Wave SDK **7.19.0+** (0-byte backups and refused restores on older ZST39 1.1 firmware).
   HA docs recommend **7.23.2+** for 800-series, and specifically that the Aeotec Z-Stick 10 Pro
   be on 7.23.2+ before migrating. 500-series must be SDK 6.61+.
   Sources: https://github.com/zwave-js/node-zwave-js/issues/5257 ;
   https://www.home-assistant.io/integrations/zwave_js/ (Migrating to a new adapter).
2. **Never restore with a tool that skips conversion.** Silicon Labs PC Controller / Z-Wave JS
   UI "skip compatibility check" (`restoreNVMRaw`) write bytes verbatim; multiple reports of
   bricked ZST39s restoring a 7.18.x image onto 7.22.x. Our code uses `restore_nvm`
   (= `restoreNVM`, with conversion), which is correct. Sources: HA community threads 690434,
   572000, 949117; zwave-js docs warning "The given buffer is NOT checked for compatibility".
3. **Radio is off during backup and restore**; the operation is scheduled in the
   `controller-exclusive` task group with `TaskInterruptBehavior.Forbidden` (zwave-js ≥ 15.25.2),
   so nothing else talks to the mesh until it finishes. After a 700+ backup zwave-js performs a
   `trySoftReset()` to work around an SDK bug where the NVM isn't closed properly — expect a brief
   USB re-enumeration even on backup. Source: `Controller.ts` `getBackupNVMRawTask`.
4. **Restore ends in `softResetAndRestart()`**; `_nodes.clear()` then full controller
   re-interview; all nodes are re-interviewed from scratch (battery devices on next wake-up).
   On 500-series sticks where soft reset is disabled/blacklisted (Z-Wave.me UZB/UZB1, Vision
   Gen5 15.1, or the add-on `soft_reset: Disabled` option) the driver cannot restart itself —
   the user must power-cycle the stick (unplug/replug) and restart the add-on/Z-Wave JS UI.
   On 700/800 `maySoftReset()` always returns true. Source: `Driver.ts` `maySoftReset()`,
   Z-Wave JS UI docs *Soft reset* setting.
5. **Never have old and new stick powered at once** after a restore — they share a home ID
   and node ID 1. Core's migrate flow unloads the entry and makes the user unplug the old
   adapter before continuing (`async_step_instruct_unplug`); it no longer hard-resets the old
   stick (that was removed after 2025.6). Factory-reset or physically retire the old stick.
6. **`preserveRoutes: False`** is what core uses; zwave-js docs add that when moving between
   different hardware "omitting routing and neighbor information may even be beneficial".
   Consider `{"preserveRoutes": False, "preserveNeighbors": False}` for cross-series restores.
7. **Z-Wave JS UI alternative.** Z-Wave JS UI has NVM backup/restore in the controller
   "Advanced" dialog, scheduled NVM backups (Settings → General → Backup, NVM section: cron,
   "on nodes changes", max files; files land in `store/backups/nvm_*.bin`), and a *Driver
   function* snippet `const data = await driver.controller.backupNVMRaw(); await fs.writeFile(storeDir + '/NVM.bin', data, 'binary')`.
   Source: https://github.com/zwave-js/zwave-js-ui/blob/master/docs/usage/driver_function.md ,
   docs/usage/setup.md. If the user already runs Z-Wave JS UI scheduled NVM backups, Radio Guard's
   nightly backup is a second radio-off window per night — document that.
8. Backup file format is a raw NVM image (`.bin` in UI/core; we write `.nvm`), identical bytes to
   what Z-Wave JS UI and core's "Download backup" produce, so they are interchangeable.

---

## 5. Security keys are NOT in the NVM — CONFIRMED, and the integration says nothing **[HIGH]**

The NVM holds protocol state (home ID, node list, routes, SUC data, application data). S0/S2
network keys are **driver options** supplied by the host application:

```ts
// zwave-js ZWaveOptions
securityKeys?: { S2_AccessControl?, S2_Authenticated?, S2_Unauthenticated?, S0_Legacy? };
securityKeysLongRange?: { S2_AccessControl?, S2_Authenticated? };
```

Source: https://github.com/zwave-js/node-zwave-js/blob/master/docs/api/driver.md (ZWaveOptions).
`nvmedit/convert.ts` has no notion of security keys at all. HA docs: "Make sure that you keep a
backup of these keys in a safe place... you need the same keys to access securely paired devices."
Z-Wave JS UI FAQ: "It is strongly recommended to keep a backup of your security keys separately."
If the keys are lost, every S0/S2 device must be excluded, factory reset and re-included
(zwave-js troubleshooting/common-issues.md).

(One community post claims the NVM "contains network security keys" — it does not; treat the NVM
as sensitive anyway because it fully identifies the network.)

### Where the keys live

- **Official add-on** (`core_zwave_js`): add-on options `s0_legacy_key`, `s2_access_control_key`,
  `s2_authenticated_key`, `s2_unauthenticated_key`, `lr_s2_access_control_key`,
  `lr_s2_authenticated_key` (legacy `network_key` = S0). Source:
  https://github.com/home-assistant/addons/blob/master/zwave_js/config.yaml
- **HA config entry `entry.data`** mirrors them under the same names (`CONF_S0_LEGACY_KEY`
  etc.) when `use_addon` is true — `async_ensure_addon_running` syncs add-on options into
  `entry.data`. Source: `__init__.py` L1335–1410. For non-add-on setups (Z-Wave JS UI, bare
  server) `entry.data` has no keys.
- **Z-Wave JS UI**: `store/settings.json` → `zwave.securityKeys.{S0_Legacy,S2_Unauthenticated,
  S2_Authenticated,S2_AccessControl}` and `zwave.securityKeysLongRange.{S2_Authenticated,S2_AccessControl}`;
  or `ZWAVE_EXTERNAL_SETTINGS` JSON. Source: zwave-js-ui docs/usage/setup.md.

### Can HA core read add-on options? Yes.

`homeassistant.components.hassio.AddonManager.async_get_addon_info()` returns `AddonInfo` with
`.options: dict[str, Any]` (from Supervisor `/addons/core_zwave_js/info`). The zwave_js
integration exposes `homeassistant.components.zwave_js.addon.get_addon_manager(hass)`.

```python
from homeassistant.components.hassio import is_hassio
from homeassistant.components.zwave_js.addon import get_addon_manager

async def _addon_keys(hass) -> dict[str, str] | None:
    if not is_hassio(hass):
        return None
    info = await get_addon_manager(hass).async_get_addon_info()
    opts = info.options or {}
    keys = {k: opts.get(k, "") for k in (
        "s0_legacy_key", "s2_access_control_key", "s2_authenticated_key",
        "s2_unauthenticated_key", "lr_s2_access_control_key", "lr_s2_authenticated_key")}
    if not keys["s0_legacy_key"]:
        keys["s0_legacy_key"] = opts.get("network_key", "")
    return keys
```

Simpler and version-stable: read `entry.data.get("s0_legacy_key")` etc. — they are already there
for add-on users.

### Recommendation for Radio Guard

1. In `async_backup`, capture the keys (add-on → `entry.data`/add-on options; otherwise none) and
   write them **alongside** the NVM, e.g. `meta["security_keys"]` or a sibling
   `<name>.keys.json`, and put a clear sentence in the README that the backup dir now contains
   secrets (it already lives under `/config`, which is in HA backups and typically already
   contains `.storage/core.config_entries` with the same keys for add-on users).
2. If keys can't be captured (Z-Wave JS UI / standalone server), add a persistent **warning** in
   `RestoreCheck.warnings` and a one-time repair/notification: "Radio Guard cannot read your
   S0/S2 keys from Z-Wave JS UI; export them from Settings → Z-Wave → Security Keys (or back up
   `store/settings.json`)".
3. In `async_restore_check`, if the backup has keys and the live `entry.data` keys differ (or are
   empty), fail the pre-flight (`ok=False`) unless `force`: a restore with the wrong keys yields a
   network where every secure node is "dead"/unencrypted.

---

## 6. Timeouts — CONFIRMED (adequate) **[LOW]**

Observed/documented durations:

- zwave-js docs: backup/restore "may take several seconds up to a few minutes depending on the
  NVM size".
- NVM sizes: 700-series 0xC000 (48 KiB), 800-series shared FS 0xA000 (40 KiB), 500-series
  16–64 KiB depending on chip; read in ≤255-byte chunks over 115200 serial (falls back to 48-byte
  chunks on some sticks — Z-Wave.me UZB and Aeotec Gen5 are notably slow).
- Community reports: 700→800 "2 minutes max to backup and restore"; a full Gen5 (500) →
  ZWA-2 migration including conversion and re-interview start ~10 minutes.
- Restore = read target NVM + convert + write + soft reset + controller re-interview; core waits
  60 s (`DRIVER_READY_EVENT_TIMEOUT`) for driver ready and then reloads.

So `ZWAVE_BACKUP_TIMEOUT = 300` and `RESTORE_TIMEOUT = 600` are generous and fine. Two cautions:

- `asyncio.wait_for` cancelling the awaiting coroutine does **not** cancel the server-side task;
  the radio stays off until zwave-js finishes. Don't retry on timeout; log and leave it.
- Core's migrate-flow connect timeout is 60 s and version timeout 10 s; keep the post-restore
  driver-ready wait at 60 s like core (it's suppressed on timeout).

---

## 7. Concurrency hazards — MISSING in code **[MED]**

zwave-js (≥ 15.25.2) schedules backup/restore as tasks with `group: "controller-exclusive"` and
`interrupt: Forbidden`. Inclusion/exclusion use group `inclusion-exclusion`; rebuild-routes is
`TaskPriority.Lower`; OTA is per-node; OTW uses `controller-exclusive`. Per the scheduler rules,
a Normal-priority backup **interleaves** with same-priority tasks outside its group at yield
points — but since it switches the radio off first, any inclusion/OTA frame sent meanwhile fails.
`softReset()` explicitly refuses when `isAnyOTAFirmwareUpdateInProgress()`; the post-backup
`trySoftReset()` would then throw inside the task.

Pre-flight checks to add before `async_backup` and `async_restore` (all exist in the python client):

```python
if ctrl.inclusion_state is not InclusionState.IDLE and ctrl.inclusion_state is not InclusionState.SMART_START:
    raise RuntimeError(f"Controller busy: inclusion state {ctrl.inclusion_state.name}")
if ctrl.is_rebuilding_routes:
    raise RuntimeError("Route rebuild in progress")
if await ctrl.async_is_any_ota_firmware_update_in_progress():   # schema 21
    raise RuntimeError("Node firmware update in progress")
if await ctrl.async_is_firmware_update_in_progress():           # schema 26, controller OTW
    raise RuntimeError("Controller firmware update in progress")
```

(`from zwave_js_server.const import InclusionState`.) For nightly backups, consider skipping
rather than raising when any node is mid-interview (`node.interview_stage`), since the radio
goes dark for the duration. Also skip when `driver.controller.status` is not `ControllerStatus.READY`
(`ControllerStatus` enum in `zwave_js_server.const`).

Also note the add-on and Z-Wave JS UI's own scheduled NVM backup may run at the same time;
zwave-js serialises them, but two 03:17 jobs double the radio-off window.

---

## Other findings

- **[MED]** `_entry()` returns the first LOADED zwave_js entry. HA supports multiple Z-Wave
  entries (multi-controller). Radio Guard should back up every loaded entry or let the user pick;
  restore must target the entry whose `unique_id`/home ID matches the backup `meta["home_id"]`,
  or (dead-stick case) the one the user chose.
- **[MED]** `async_restore_check` warns "Live controller already has home ID X; restore may be
  unnecessary". After a successful NVM restore to a new stick that *is* the expected state; after a
  factory reset it won't match. Fine as a warning, but note that a mismatch is **expected** when
  replacing hardware and should not reduce confidence.
- **[LOW]** `"Battery devices re-report on their next wake"` — more precisely all nodes are
  re-interviewed; battery nodes complete the interview on wake-up.
- **[LOW]** Docstring says "handled by zwave-js itself, including conversion between 500/700/800";
  OK, but add "requires SDK 6.61+ (500) / 7.19+ (700/800)".
- **[LOW]** `backup/const.py` referenced by the task does not exist; constants are in
  `custom_components/radio_guard/const.py`.

---

## Restore runbook

### A. Replace a dead stick with the same series (e.g. Zooz ZST10 700 → ZST10 700, or ZST39 800 → ZST39 800)

Prereqs: latest Radio Guard `.nvm` for that network; the S0/S2 keys (add-on: Settings → Apps →
Z-Wave JS → Configuration; Z-Wave JS UI: Settings → Z-Wave → Security Keys or `store/settings.json`);
new stick firmware on SDK ≥ 7.19 (ideally 7.23.2+). Do **not** plug the new stick in while the
old one is connected.

1. Shut down HA (or stop the Z-Wave JS add-on / Z-Wave JS UI). Remove the dead stick.
2. Plug in the new stick. Confirm the serial path (prefer `/dev/serial/by-id/...`).
3. Add-on: Settings → Apps → Z-Wave JS → Configuration → set `device` to the new path (and verify
   the six key fields are unchanged). Z-Wave JS UI: Settings → Z-Wave → Serial port. Start it.
4. HA: the Z-Wave JS integration will load against an *empty* controller with a new random home
   ID; HA raises repair "An unknown adapter was detected" — **do not** confirm it yet.
5. Call `radio_guard.restore` with `source: zwave_js`, `confirm: true` (latest file, or `file:`).
   Pre-flight shows backup home ID ≠ live home ID (expected) and SDK versions. Restore converts,
   writes, soft-resets; the stick re-enumerates; driver ready → entry reloaded; Radio Guard
   updates the entry `unique_id` to the restored home ID (after fix in §3).
6. Verify: Settings → Devices → Z-Wave controller device shows the **old** home ID; node count
   matches; mains nodes come alive within a minute; battery nodes on wake-up (press the
   wake button on locks/sensors to speed up). If any repair issue remains, confirm it only
   if the shown new ID equals the backup's home ID.
7. Optional: Rebuild routes (Z-Wave → Controller → Rebuild network routes) since
   `preserveRoutes: false` dropped LWR/NLWR.
8. Physically destroy or factory-reset the dead stick so it can never come back online with the
   same home ID.

### B. Migrate 700 → 800 (e.g. ZST10 700 → ZST39 800 / Nabu Casa ZWA-2)

Both sticks are alive, so prefer HA's built-in flow (Settings → Devices → Z-Wave → ⋮ → Migrate
adapter), which does exactly: backup NVM to `/config/zwavejs_nvm_backup_<ts>.bin` →
unload entry → user unplugs old → pick new port → restart add-on → `restore_nvm(backup,
{preserveRoutes:false})` → wait driver ready → reload. The flow does not support Z-Wave JS UI as
the server. If you use Radio Guard instead:

1. **Update firmware first**: 800 stick to SDK 7.23.2+ (Zooz 1.5x/7.22.1+ at minimum; ZWA-2 ships
   7.23.x). If the 700 is below 7.19 upgrade it before taking the backup (OTW from Z-Wave JS UI or
   HA). A target firmware newer than what zwave-js parses yields `ZW0280` — update the add-on /
   Z-Wave JS UI to current first.
2. Take a fresh backup: `radio_guard.backup_now` (`source: zwave_js`) while the network is quiet
   (no inclusion/OTA/heal). Record the keys from the add-on/UI config.
3. Stop the add-on/UI. Unplug the 700. Plug in the 800. Point the add-on/UI at the new serial
   path. Start. HA loads an empty controller (new home ID; repair appears — ignore for now).
4. `radio_guard.restore` (`source: zwave_js`, `confirm: true`). Pre-flight should show
   7.x → 7.x (both 700-series format); conversion re-lays the NVM3 FS to the 800's shared layout,
   keeps the 800's RF config and application version, drops routes.
5. Driver soft-resets and re-interviews. Radio Guard updates `unique_id`, reloads. Verify home ID
   and node list as in A.6.
6. RF region/LR: the 800's own RF config is preserved, so set Region (USA / USA Long Range) in the
   add-on/UI after restore if it isn't what you want. Long Range nodes from a 700 don't exist
   (700 has no LR), so nothing to migrate there.
7. Rebuild routes (recommended on new RF hardware). Never power the old 700 again without a
   factory reset.

Rollback: stop, swap sticks back, point the server at the 700's path; the 700 still holds the
network. Anything included/excluded on the 800 after the swap is unknown to the 700.

### C. 500 → 700/800

Same as B but: source stick must be SDK 6.61+ (Aeotec Gen5 1.2 is 6.51 and **not** convertible by
zwave-js; a community converter or a fresh network is needed). Expect ZW0280 "source NVM has an
unsupported format" otherwise. Restoring 700/800 backups back **onto** a 500 is supported by
zwave-js (`json700To500`) but drops LR nodes and 700-only data; verify the 500 is on 6.61+.

---

## Summary of required changes (priority order)

1. **[BLOCKER]** `async_restore`: subscribe to driver-ready before restoring, re-read home ID via
   `/version`, `async_update_entry(unique_id=...)`, then reload (code in §3). Remove
   `suppress(Exception)` around reload.
2. **[HIGH]** Fix the `runtime_data` dict fallback or raise `hacs.json` minimum to 2025.8.0 (§1);
   the restore API (`options` arg, schema 42) is only correct with client ≥ 0.65 / server ≥ 3.0.1,
   i.e. HA ≥ 2025.7 anyway (§2).
3. **[HIGH]** Capture/warn about S0/S2 keys and pre-flight key mismatch (§5).
4. **[HIGH]** Replace the "cannot restore to an older series" warning with real constraints (SDK
   6.61+/7.19+, ZW0280 on unsupported formats), note 700/800→500 is supported with loss (§4).
5. **[MED]** Busy checks before backup/restore: inclusion state, route rebuild, OTA/OTW (§7).
6. **[MED]** Multi-entry support / match backup `home_id` to entry (Other findings).
7. **[LOW]** Use `async_backup_nvm_raw()` / `async_restore_nvm()` to skip the base64 detour.
