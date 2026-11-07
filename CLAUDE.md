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

## Status (as of 2026-11-07 ~17:15 ET; see TODO 2/3 for dev-instance and test results)

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
2. **Dev instance (192.168.1.196, Mac mini) — done for Z2M (2026-11-07).** The dev HA is the Docker Core
   container `bensten-dev-ha` (HA 2026.10.2, no Supervisor, colima, no USB) with config in
   `../dev-ha/config`; it has **no real radios** (prod devices are `prod_mirror` fakes). Radio Guard is
   bind-mounted from this repo (`../dev-ha/docker-compose.yml` + symlink in `config/custom_components/`),
   MQTT entry -> `host.docker.internal:1883` = `smoke/broker.yaml` amqtt on the Mac's 127.0.0.1, fed by
   `smoke/fake_z2m.py`. Neither process survives a reboot; restart both from `smoke/`. Verified there:
   first backup + notification, entities, Repairs (replaced stick via FAKE_IEEE/FAKE_EPID -> guided flow to
   the RESTORE step, aborted), `restore` dry run BLOCKED (same stick) / OK (replaced). Still open: ZHA and
   Z-Wave JS on real network adapters, Z2M add-on path (needs a Supervisor install), a real restore.
3. **Tests — done (332 passing, no xfails).** Open items they surfaced: real `homeassistant.components.zha`
   can't import in the venv (`aiousbwatcher` missing) so ZHA tests use fake modules; ZHA `restore_backup`
   silently no-ops if the radio's settings `supersedes(backup)`, so `force` may "succeed" without writing
   (check on hardware); a blocked pre-flight can be forced from the Repairs UI (intended? decide);
   a failed `pre_restore_*` snapshot leaves a `backup_failed` issue; the first backup's options update
   reloads the entry once.
4. README rewrite per `review/hacs-ux.md` §6: 5-line quick start, "When your coordinator dies"
   checklists (drafts in `review/zigbee.md` and `review/zwave.md` runbook sections), "what this
   does NOT do", FAQ, screenshots under `docs/img/`.
5. Brands PR (`home-assistant/brands`, `custom_integrations/radio_guard/`), then HACS default repo PR.
6. Nice-to-have: `heartbeat` retained + LWT topic; capture Z-Wave JS UI keys (not add-on) via
   user-supplied option; Z2M `bridge/devices` count for `device_count` in identity.
