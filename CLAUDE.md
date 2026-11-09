# Radio Guard — project brief for Claude Code

Home Assistant custom integration (HACS) that takes nightly backups of Zigbee (Zigbee2MQTT,
ZHA) and Z-Wave JS coordinators, stores them under `/config/radio_guard/`, and restores them
onto replacement hardware with a guided Repairs flow. Plus an MQTT heartbeat for an external
power watchdog (ESPHome and Shelly references in `esphome/`, `shelly/`).

Owner: Brent (@brentb2529). Product goals, in order: easy onboarding via HACS, easy to start
backups, easy to use on failure day. Rock-solid over feature-rich. HA minimum **2025.8**.

## Hard rules

- **Never write to production HA.** Dev instance only: `http://192.168.1.196:8123` (Mac mini).
  Confirm an instance is dev before any install, config change or service call that changes state.
- Don't talk to radios directly. Backups go through the software that owns the coordinator
  (Z2M MQTT bridge API, zigpy via ZHA, zwave-js-server via the Z-Wave JS integration).
- Don't skip the pre-flight. Every restore goes through `async_restore_check` first.
- Keep `strings.json` and `translations/en.json` identical (CI diffs them).
- `ruff check custom_components tests` must pass; `ruff format` applied.

## Status (as of 2026-11-07 ~20:05 ET; v0.1.0 released; see TODO 2/3)

Verified on a live HA **2026.2.3** instance in a cloud workspace against a fake Z2M bridge
(`smoke/fake_z2m.py`) and an `amqtt` broker:

- Config flow (2 fields: sources pre-selected from detection + "first backup now"), options flow
  with collapsible sections (`schedule`, `zigbee2mqtt`, `storage`, `heartbeat`), reload on save.
- First backup ~45 s after setup → persistent notification with file paths.
- Z2M backup via `bridge/request/backup` with transaction correlation and persistent subscriptions
  (`async_setup`/`async_teardown` on the source; HA debounces MQTT subscribes, so subscribe-then-
  publish in one breath races and times out — that bug was found and fixed here).
- Entities per source device (`<Source> backups`): last_backup, next_backup, stored_backups
  (+`backups` attribute list), coordinator_identity; binary: backup_problem, coordinator_replaced,
  backup_running, integration_loaded (disabled by default); button back_up_now.
- Repairs: `backup_never` / `backup_stale` / `backup_failed` are fixable (confirm → backup).
  `coordinator_replaced` is fixable → guided restore: pick backup → pre-flight → type `RESTORE`.
  A BLOCKED pre-flight stops the Repairs flow (`blocked` step → back to pick); forcing is only
  possible via the `restore` service with `force: true` (decided by Brent 2026-11-07).
  Walked end to end against a simulated replaced coordinator.
- Services: `backup_now`, `list_backups` (response only), `restore` with `mode: dry_run|restore`,
  `force` (advanced). Dry run also raises a persistent notification. Blocked pre-flight →
  `ServiceValidationError` (translation key `restore_blocked`); staged Z2M restore (non add-on) →
  `RestoreStaged` result with manual steps, not a failure.
- Safeguards: identity-match block, max restore age (default 7 d), device-count-drop warning,
  pre-restore snapshot of the target (`pre_restore_*`, excluded from "latest"), per-source
  running/restoring flags, scheduled backup **skipped** while `coordinator_replaced` is true
  (so a fresh replacement stick's backup never hides the real one).

NOT yet verified against real hardware: ZHA backup/restore, Z-Wave JS backup/restore, Z2M
add-on stop/write/start path, post-restart verification. These are implemented per the reviews
below but need the dev instance.

## Key design facts (from specialist reviews in `review/`; read before touching `backup/*.py`)

- **Z2M restore only applies when `configuration.yaml` `advanced.{pan_id,ext_pan_id,network_key,
  channel}` equal the backup's.** Otherwise herdsman forms a NEW network and wipes `database.db`.
  `z2m.py` patches `advanced.*` from the OCBF (keeping the user's `serial`/`mqtt`), writes every data
  file except configuration/secret, and after start verifies `bridge/info` ext PAN matches.
  Add-on data dir: read `AddonInfo.options["data_path"]` (default `/config/zigbee2mqtt`), map
  `/config/…`→`hass.config.path`, `/share/…` as-is; `/addon_config/…` is not visible from Core.
  `/addon_configs/<slug>` is NOT mounted in Core. Use `homeassistant.components.hassio.AddonManager`
  and `homeassistant.helpers.hassio.is_hassio` (the old function helpers are gone since 2024.11).
  ember adapters don't get their IEEE written by the restore (zstack does) → warn.
- **ZHA restore must not run on the live gateway.** Unload the ZHA entry, use
  `ZhaRadioManager.from_config_entry(...).restore_backup(backup, overwrite_ieee=force)`, then
  `async_setup` the entry. Backup is zigpy-native JSON (`as_dict()`), metadata read from the
  `NetworkBackup` object. `DestructiveWriteNetworkSettings` → tell user force=true burns the IEEE once.
- **Z-Wave JS restore mirrors core:** subscribe `async_wait_for_driver_ready_event` before
  `async_restore_nvm(bytes, {"preserveRoutes": False})`, wait, re-read home ID via
  `async_get_version_info`, `async_update_entry(unique_id=...)`, reload. S0/S2 keys are NOT in the
  NVM; captured into meta `security_keys` from `entry.data` for add-on users, warned otherwise;
  key mismatch blocks pre-flight. Conversion: 500↔700/800 all supported by zwave-js (700/800 share
  NVM3); 500 needs SDK ≥6.61, 700/800 target needs ≥7.19 (7.23.2 recommended). Pre-checks:
  controller READY, inclusion idle, no route rebuild, no firmware update.
- Identity comparison always via `backup/ident.py` (`norm_hex`, `norm_pan_id`): Z2M says `0x…`,
  OCBF bare hex, zigpy `aa:bb:…`.

## Layout

```
custom_components/radio_guard/
  __init__.py        setup/unload, services, orphan device cleanup
  coordinator.py     RadioGuardCoordinator: schedule, state, issues, restore orchestration, heartbeat
  config_flow.py     user step + sectioned options; flatten_options()
  repairs.py         BackupNowRepairFlow, RestoreRepairFlow
  storage.py         BackupStore: atomic writes (0600), .meta.json sidecar, rotation, labels
  backup/base.py     BackupSource ABC, BackupPayload, RestoreCheck, RestoreStaged, SourceUnavailable
  backup/z2m.py  backup/zha.py  backup/zwave.py  backup/ident.py
  sensor.py binary_sensor.py button.py entity.py icons.json strings.json translations/en.json services.yaml
esphome/watchdog.yaml  shelly/watchdog.js  blueprints/automation/radio_guard/backup_problem_notify.yaml
smoke/                 live-HA harness (see below)
tests/                 pytest (pytest-homeassistant-custom-component) — partial, see TODO
review/                hacs-ux.md zigbee.md zwave.md (specialist reviews; many items already applied)
.github/workflows/     validate.yml (hassfest, hacs, ruff, pytest), release.yml (zip asset)
```

## Dev setup

```bash
python3.13 -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt amqtt requests websocket-client paho-mqtt
ruff check custom_components tests && pytest -q
```

Smoke harness (fake Z2M + real HA, no hardware):
```bash
cd smoke
../.venv/bin/amqtt -c broker.yaml &                 # MQTT on 127.0.0.1:1883
../.venv/bin/python fake_z2m.py &                   # FAKE_IEEE=0x… FAKE_EPID=0x… to simulate a replaced stick
./start_hass.sh                                     # boots HA from smoke/config (symlinks the integration)
../.venv/bin/python onboard.py                      # first run only; writes token.txt
../.venv/bin/python -c "from ha import *; print(service('radio_guard','backup_now'))"
```
`smoke/ha.py` has `flow()`, `service()`, `state()`, `ws_call()` helpers. `homeassistant.restart`
kills hass here (no supervisor) — use `./start_hass.sh`.

## TODO, in order

1. **Push to GitHub** `brentb2529/radio-guard` (public, no README init), fix `codeowners`/URLs if the
   name differs. Tag `v0.1.0` after first dev-instance validation; manifest `version` must match.
2. **Dev environments (Mac mini 192.168.1.196).**
   - `bensten-dev-ha` (Docker Core HA 2026.10.2, no Supervisor, `../dev-ha/config`, prod clone with fake
     `prod_mirror` entities). **Production** (read-only inventory 2026-11-09): Raspberry Pi 5, HA OS 18.4,
     HA 2026.11; ONE Aeotec Z-Stick 10 Pro (USB `10c4:ea70`, CP2105 dual UART, unknown to HA's manifests)
     carrying both radios: `-if01` = Z-Wave JS add-on (7 devices), `-if00` = ZHA (EZSP, 16 devices). Also
     Matter Server add-on and a SmartThings hub (170 devices, outside Radio Guard). HA backups: daily 04:00,
     encrypted, local x3 + HA Cloud (newest only). On the dev HA, Radio Guard is
     bind-mounted from this repo: **restart the container after code changes** (stale code bit us once).
   - Simulated Z2M: `smoke/z2m.sh start ../dev-ha/config/zigbee2mqtt | stop | status` (ONE instance;
     `pkill -f "python fake_z2m.py"` does NOT match on macOS). Stick = `serial.port tcp://<stick>:6638`.
     MQTT: amqtt on 127.0.0.1:1883 (no LWT on crash, resends all retained on subscribe).
   - Simulated Z-Wave: scratchpad `zwjs/` (session scratch): `node stick.cjs <port> sticks/<A|B>/stick.cjs`
     (zwave-js mock controller, mDNS disabled) + `zws.sh start <port>` (zwave-js-server on 127.0.0.1:3001,
     `--disable-dns-sd`); HA Z-Wave JS entry -> `ws://host.docker.internal:3001`. The mock has NO NVM and
     reports SDK 7.17.99, so backup/restore can't complete there (pre-flight correctly refuses).
   - HA OS VM (`../haos-vm`, not a repo): `vm.sh start|stop|status`, HAOS 18.4 aarch64 / HA 2026.10 on
     QEMU+HVF. **Isolated** (`restrict=on`): no LAN, no internet (it saw the LAN via mDNS/SSDP before;
     never run it unrestricted). HA at http://localhost:8180 (HAOS 18 serves HA on port 80; 8123
     redirects there). Inside, only 10.0.2.100:5555/5556 (simulated Z-Wave sticks A/B) and :1883 (test
     MQTT) reach the Mac. Test owner `radioguard-test` + API token in `../haos-vm/.owner_login` (0600).
     `push.sh` copies `custom_components/radio_guard` in over SSH (Terminal & SSH add-on, key `vm_key`,
     port 2222) and restarts Core; `sup.py <method> <endpoint> [json]` calls the Supervisor API.
     Z-Wave JS **add-on** runs there on `socket: tcp://10.0.2.100:<stick>`.
   - CI tests HA 2026.2 only (newest pytest-homeassistant-custom-component pins it); the dev HA and the VM
     (2026.10) are where newer-HA breakage shows up (two bugs found that way). Re-check both before releases.
   **UI-verified (clean runs):** Z2M: failure detection -> Repair -> replace wizard (Docker path) -> devices
   2/2+1/1; migrate B->C -> devices -> rollback to B. Z-Wave JS (external server): down Repair at the
   predicted time, wizard swap -> found new stick -> pre-flight refused SDK 7.17.99 with a clear message.
   Z-Wave JS **add-on** on HA OS (VM): stick probe detected a dead network stick that HA still showed as
   loaded; wizard unloaded Z-Wave JS, stopped the add-on, switched `socket` to stick B, started it, set the
   integration up on B, pre-flight refused SDK 7.17.99. Only the NVM write itself is untested.
   **Machine-dies drill (VM, 2026-11-09):** encrypted HA backup downloaded off the VM -> HA config and local
   backups wiped (fresh onboarding) -> Welcome > Or restore > Upload backup -> tick everything incl. Apps ->
   key -> restored. Radio Guard, its coordinator backup, the Z-Wave JS add-on (socket + all 7 keys) and API
   tokens came back; Radio Guard matched the live home ID to its backup. Findings: HA's restore screen leaves
   Apps unticked (sheet now says so); a brand-new HA OS disk can't be used in the isolated VM (first boot
   downloads Core; restores need `ha jobs options --ignore-conditions internet_system internet_host`, then
   `ha jobs reset`). Simulated Z-Wave sticks EXIT when their client disconnects: restart `stick.cjs` and the
   VM (guestfwd connects at QEMU start) after any add-on restart/restore.
   **Not verified:** real hardware; Z2M add-on (needs a real adapter, e.g. SLZB-06 in the VM); ZHA live
   (unit tests only); Z-Wave NVM write.
3. **Tests: 581 passing, CI green.** Gaps: ZHA/Z-Wave migrate+rollback paths only partly covered.
4. README rewrite per `review/hacs-ux.md` §6: 5-line quick start, "When your coordinator dies"
   checklists (drafts in `review/zigbee.md` and `review/zwave.md` runbook sections), "what this
   does NOT do", FAQ, screenshots under `docs/img/`.
5. Brands PR (`home-assistant/brands`, `custom_integrations/radio_guard/`), then HACS default repo PR.
6. Nice-to-have: `heartbeat` retained + LWT topic; capture Z-Wave JS UI keys (not add-on) via
   user-supplied option; Z2M `bridge/devices` count for `device_count` in identity.
