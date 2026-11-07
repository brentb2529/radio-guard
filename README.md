# Radio Guard

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=brentb2529&repository=radio-guard&category=integration)
[![Release](https://img.shields.io/github/v/release/brentb2529/radio-guard)](https://github.com/brentb2529/radio-guard/releases)
[![Validate](https://github.com/brentb2529/radio-guard/actions/workflows/validate.yml/badge.svg)](https://github.com/brentb2529/radio-guard/actions/workflows/validate.yml)

Nightly backups of your Zigbee and Z-Wave coordinators, and a guided restore onto a replacement stick, so a dead coordinator doesn't mean re-pairing every device.

Home Assistant's own backups cover your config. They don't cover the network identity stored *inside* the coordinator (keys, PAN/home ID, the device table). Radio Guard backs that up through the software that owns the stick: Zigbee2MQTT, ZHA or Z-Wave JS.

## Quick start

1. Click **Open in HACS** above (or add `https://github.com/brentb2529/radio-guard` as a custom *Integration* repository), install **Radio Guard**, restart Home Assistant.
2. [![Add integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=radio_guard) It pre-selects the Zigbee2MQTT / ZHA / Z-Wave JS setups it finds. Submit.
3. About a minute later a notification shows your first backup's file path.
4. From then on backups run nightly at 03:17 into `/config/radio_guard/`, so Home Assistant's own backups include them.
5. Make sure those HA backups leave the box (NAS, cloud, Nabu Casa). A backup on the same SD card as the failure doesn't help.

Requires Home Assistant **2025.8** or newer.

## What it backs up

| Source | How | File |
|---|---|---|
| Zigbee2MQTT | MQTT `bridge/request/backup` | `.zip`: Zigbee2MQTT's data folder minus `log/` and `ota/`: `coordinator_backup.json` (Open Coordinator Backup), `database.db`, `configuration.yaml`, … |
| ZHA | zigpy, through the loaded ZHA integration | `.json`: zigpy network backup |
| Z-Wave JS | NVM backup through the Z-Wave JS integration | `.nvm`: raw controller NVM |

Files go to `/config/radio_guard/<source>/`, mode 0600, each with a `.meta.json` sidecar holding the coordinator identity (IEEE or home ID, PAN, channel, SDK, device count). Radio Guard keeps 14 nightly backups per source.

> **These files are secrets.** They contain your network keys, and for Z-Wave JS add-on users the sidecar also holds the S0/S2 keys, because they are not stored in the NVM. Treat them like your HA backups.

## When your coordinator dies

The short version is the same for all three:

1. **Don't delete anything.** Keep the integration and all devices; dead devices are expected for now.
2. **Unplug the old coordinator for good.** Two coordinators with the same identity on one network will break it.
3. Plug in the replacement and point the integration at it (steps below).
4. Within 15 minutes Radio Guard notices the attached coordinator is different from your newest backup. It raises a **Repair**, "coordinator changed, restore available", and it **stops taking nightly backups** for that source, so a backup of the blank new stick never replaces the one you need.
5. **Settings → Repairs → Fix** walks you through it: pick a backup → pre-flight check → tick that the old stick is unplugged → type `RESTORE`.

If the pre-flight finds a blocking problem, such as the wrong network, a key mismatch, a backup older than 7 days, or unsupported firmware, the Repair stops and tells you why. You can pick another backup or fix the cause. A blocked restore can only be forced from **Developer tools → Actions** (see [Services](#services)), never from the Repair.

<details>
<summary><b>Z-Wave JS checklist</b></summary>

1. Stop the Z-Wave JS add-on (or Z-Wave JS UI), remove the dead stick, plug in the new one.
2. Point the server at the new stick: add-on → Configuration → `device`, or Z-Wave JS UI → Settings → Z-Wave → Serial port. Use `/dev/serial/by-id/...`. **Leave the six security-key fields as they are.**
3. Start it. The Z-Wave JS integration loads against an empty controller with a new home ID. If HA itself raises "An unknown adapter was detected", don't confirm it yet.
4. Run the Radio Guard Repair (or the `restore` service). The pre-flight checks:
   - the backup's home ID differs from the stick's (expected);
   - the S0/S2 keys in the backup match the configured ones (a mismatch blocks);
   - the controller is idle (no inclusion, route rebuild or firmware update);
   - SDK compatibility: 500-series needs SDK 6.61+, a 700/800 target needs 7.19+ (7.23.2+ recommended). 500 → 700/800 and 700 ↔ 800 are converted by zwave-js; 800 → 500 loses Long Range nodes.
5. Radio Guard restores the NVM, waits for the driver, updates the integration to the restored home ID and reloads it.
6. Check the controller device shows the **old** home ID. Mains devices come back within a minute; battery devices on their next wake-up (press the button on locks and sensors to hurry them).
7. Optional: **Rebuild network routes**. The restore drops stored routes on purpose.
8. Factory-reset or destroy the dead stick so it can never rejoin with the same home ID.

Both sticks still alive? HA's own **Z-Wave → Migrate adapter** flow does the same job and is the easier path. Radio Guard is for when the old stick is gone.

**Z-Wave JS UI users:** Radio Guard reads the S0/S2 keys from the Z-Wave JS integration's config entry, which only has them with the add-on. With Z-Wave JS UI, keep a copy of your keys yourself (Settings → Z-Wave → Security Keys); the backup will note they are missing.

</details>

<details>
<summary><b>ZHA checklist</b></summary>

1. Unplug the old radio, plug in the new one. Changing vendor (TI ↔ Silicon Labs) is fine; zigpy converts.
2. If you still have ZHA's own backups, **ZHA → Configure → Migrate radio** is the easiest path. Choose *Upload a backup* and use the newest `.json` from `/config/radio_guard/zha/`. Radio Guard's main job here is making sure that file exists off the box.
3. To use Radio Guard's restore instead: point ZHA at the new radio's port first (ZHA → Configure → re-configure the current radio), then run the Repair or the `restore` service. Radio Guard unloads ZHA, writes the backup to the new radio, and loads ZHA again.
4. **Silicon Labs radios** (SkyConnect/ZBT-1, Sonoff -E and similar) on older firmware can have their IEEE address written only once. If the restore stops with that message, either update the radio to firmware that allows rewriting, or run the `restore` service with `force: true` to use the one-time write. Without the old IEEE, many devices won't come back.
5. Afterwards check **ZHA → Configure → Network settings**: PAN ID, extended PAN ID, channel and IEEE should match the backup. Routers come back within minutes, sleepy end devices within the hour; power-cycle stragglers.

</details>

<details>
<summary><b>Zigbee2MQTT checklist</b></summary>

1. Stop Zigbee2MQTT. Unplug the old adapter, plug in the new one.
2. Update `serial.port` (and `serial.adapter` if the chip family changed) in the add-on options or `configuration.yaml`.
3. **TI (zstack)** adapters get the old IEEE written by the restore. **Silicon Labs (ember)** adapters don't: copy the old IEEE onto the stick first with your vendor's tool (it's in the backup's `coordinator_backup.json` as `coordinator_ieee`), or expect some devices to need re-pairing.
4. Start Zigbee2MQTT and run the Radio Guard Repair (or the `restore` service).
   - **Zigbee2MQTT add-on (HA OS / Supervised):** Radio Guard stops the add-on, sets `advanced.pan_id`, `ext_pan_id`, `network_key` and `channel` in `configuration.yaml` to the backup's values (your `serial` and `mqtt` settings stay), writes the backup's data files, starts the add-on and checks Zigbee2MQTT came back on the backup's network.
   - **Docker / standalone Zigbee2MQTT:** Radio Guard can't stop it, so it stages the files in `/config/radio_guard/zigbee2mqtt/restore/` and shows the manual steps: stop Z2M, copy everything from that folder into its data directory, start Z2M.
5. Zigbee2MQTT's log must say `Started with result 'restored'`. If it says `'reset'`, it formed a new network: stop it, put back the staged `coordinator_backup.json` and `database.db`, check `advanced.*` matches the backup, and start again.
6. Mains routers rejoin within minutes, sleepy devices after their next check-in. Power-cycle routers that stay offline.

Moving between TI and Silicon Labs is "might work, not supported" upstream; expect to re-pair some devices.

</details>

## What this does NOT do

- **Back up your HA devices, entities, automations or dashboards.** That's Home Assistant's backup.
- **Copy backups off the machine.** Its files ride along in HA's backups; getting those off-box is up to you.
- **Talk to the radio directly.** Everything goes through Zigbee2MQTT, ZHA or Z-Wave JS. If that software can't back up or restore your stick, neither can Radio Guard.
- **Thread / Matter / OTBR** datasets (not yet).
- **Restore across Z-Wave regions** (US stick → EU stick). Flash the region first.
- **Notice a dying stick.** A backup taken after the stick started corrupting data is still a bad backup. Pick one from before the trouble started; the Repair lists them newest first with device counts.

## Entities

One device per source, e.g. **Zigbee2MQTT backups**:

| Entity | What |
|---|---|
| `sensor.zigbee2mqtt_backups_last_backup` | When the newest backup was taken; attributes: file path, size, identity, device count |
| `sensor.zigbee2mqtt_backups_next_backup` | Next scheduled run |
| `sensor.zigbee2mqtt_backups_stored_backups` | How many backups are kept |
| `sensor.zigbee2mqtt_backups_coordinator_identity` | Live IEEE / home ID |
| `binary_sensor.zigbee2mqtt_backups_backup_problem` | On when the newest backup is stale (default 36 h) or the last one failed |
| `binary_sensor.zigbee2mqtt_backups_coordinator_replaced` | On when the live coordinator differs from the newest backup |
| `binary_sensor.zigbee2mqtt_backups_backup_running` | On while a backup or restore runs |
| `binary_sensor.zigbee2mqtt_backups_integration_loaded` | Whether the source integration is loaded (disabled by default) |
| `button.zigbee2mqtt_backups_back_up_now` | Take a backup now |

ZHA and Z-Wave JS get the same set (`zha_backups_…`, `z_wave_js_backups_…`).

## Services

| Service | What |
|---|---|
| `radio_guard.backup_now` | Back up one `source`, or all. Returns the files. |
| `radio_guard.list_backups` | Lists stored backups (response only). |
| `radio_guard.restore` | `mode: dry_run` (default) runs the pre-flight and reports, changing nothing. `mode: restore` restores. `file:` picks a backup (default newest). `force: true` overrides a blocked pre-flight. This is the **only** way to force a restore. |

Dry run first, always:

```yaml
action: radio_guard.restore
data:
  source: zwave_js
  mode: dry_run
```

The response (and a notification) shows the backup, the attached coordinator, the verdict (`OK` / `BLOCKED`) and warnings. Then run it again with `mode: restore`.

## Options

Settings → Devices & services → Radio Guard → Configure.

| Section | Option | Default |
|---|---|---|
| Schedule | Daily backup time | 03:17 |
| | Backups to keep per source | 14 |
| | Warn when newest backup is older than | 36 h |
| | Refuse to restore backups older than | 7 days |
| Zigbee2MQTT | Base topic | `zigbee2mqtt` |
| Storage | Folder under `/config` | `radio_guard` |
| Watchdog heartbeat | Enabled / topic / interval | on if MQTT is set up / `radio_guard/heartbeat` / 30 s |

## Automations

- **Blueprint:** notify when a backup is stale or failed.
  [![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fbrentb2529%2Fradio-guard%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fradio_guard%2Fbackup_problem_notify.yaml)
- **Events:** `radio_guard_backup_complete` (`source`, `file`, `size`) and `radio_guard_restore_complete` (`source`, `file`).

## Watchdog (optional)

With MQTT set up, Radio Guard publishes a heartbeat to `radio_guard/heartbeat` every 30 s:

```json
{"ts": "2026-11-07T22:00:00+00:00", "uptime_s": 86400,
 "sources": {"zigbee2mqtt": {"available": true, "backup_age_h": 18.7}}}
```

A smart plug or relay in front of the HA box can watch it and power-cycle a hung machine. Two references are included:

- [`esphome/watchdog.yaml`](esphome/watchdog.yaml) for an ESP32 with a relay.
- [`shelly/watchdog.js`](shelly/watchdog.js), a script for a Shelly Gen2/Gen3 relay or plug (Plus Plug, Plus 1, Pro 1, Plug S Gen3).

Both cycle power once after 5 minutes of silence, as long as the MQTT broker is still reachable, then leave HA alone for an hour. The ESPHome one exposes a switch to disarm it before maintenance. Don't use either if your broker runs on the same box as HA *and* you have no way to tell "HA hung" from "broker down".

## FAQ

**Where are my files? Can I use them without Radio Guard?**
`/config/radio_guard/<source>/`. Yes: the Z2M zip is Zigbee2MQTT's own backup format, the ZHA file is zigpy's, and the `.nvm` restores with Z-Wave JS UI.

**Can I restore to a different brand of stick?**
Zigbee: yes, zigpy and Zigbee2MQTT convert between TI and Silicon Labs (with the IEEE caveats above). Z-Wave: same series or newer; zwave-js converts 500 → 700/800 and 700 ↔ 800.

**ZHA already keeps backups. Why this?**
ZHA's backups live in `.storage`, rotate, and cover only ZHA. Radio Guard also covers Zigbee2MQTT and Z-Wave JS, keeps two weeks, and puts plain files where you can see them.

**I run Zigbee2MQTT in Docker.**
Backups work the same. Restores are staged for you to copy; see the Zigbee2MQTT checklist.

**"Backup problem" is on right after install.**
The first backup runs about 45 seconds after setup. If it's still on, press **Back up now** and check the Repair for the error.

**I replaced the stick on purpose and don't want to restore.**
Press **Back up now** on that source. That accepts the new coordinator and clears the Repair.

## Why this exists

Radio Guard is one piece of a deliberately simple resilience plan:

1. One HA box on decent storage, HA backups to a NAS or cloud.
2. Coordinator backups (this) riding along in those backups.
3. A spare boot drive and a spare coordinator in a drawer.
4. A dumb power watchdog.
5. Local control where it's free (Zigbee bindings, Z-Wave associations), so lights work with HA down.

No standby nodes, no clusters. Worst case is ten minutes with a screwdriver.

## Contributing

Issues and PRs welcome at [brentb2529/radio-guard](https://github.com/brentb2529/radio-guard/issues). Dev setup, tests and the smoke harness are described in [`CLAUDE.md`](CLAUDE.md).

## License

MIT
