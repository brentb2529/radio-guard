# Radio Guard

Scheduled, restorable backups of your Zigbee and Z-Wave coordinators, from inside Home Assistant.

Home Assistant backs up its own config nightly. It does not back up the one thing you can't recreate from config: the network identity living inside your Zigbee or Z-Wave coordinator. Lose that stick and every device has to be re-paired. Radio Guard fixes that.

- **Nightly backups** of Zigbee2MQTT, ZHA and Z-Wave JS coordinators, stored under `/config/radio_guard/` so they are included in HA's normal backups.
- **Restore to new hardware**: plug in a replacement coordinator, call one service, no re-pairing. Cross-chip (TI to Silabs) and cross-generation (Z-Wave 500 to 800) restores are handled by Zigbee2MQTT / zigpy / zwave-js themselves; Radio Guard just drives them and pre-flights the result.
- **Any coordinator the upstream tool supports**, serial or network. Radio Guard never talks to the chip; it talks to the software that owns it.
- **Sensors and Repairs**: last backup, age, count, coordinator identity, and a "backup stale" problem sensor. A failed or stale backup raises an HA Repair.
- **Heartbeat** on MQTT so a dumb external watchdog (a Shelly plug or an ESP32) can power-cycle a hung HA box. Reference ESPHome config included.

## Install

1. HACS → Integrations → ⋮ → Custom repositories → add `https://github.com/bbensten/radio-guard` as *Integration*.
2. Install **Radio Guard**, restart Home Assistant.
3. Settings → Devices & services → Add integration → **Radio Guard**. It detects which of MQTT (for Zigbee2MQTT), ZHA and Z-Wave JS are loaded and pre-selects them.

## What gets backed up

| Source | How | Format | File |
|---|---|---|---|
| Zigbee2MQTT | MQTT `bridge/request/backup` | Zip containing `coordinator_backup.json` (Open Coordinator Backup Format), `database.db`, `configuration.yaml` | `.zip` |
| ZHA | zigpy `backups.create_backup()` | Open Coordinator Backup Format JSON | `.json` |
| Z-Wave JS | `controller.backup_nvm_raw` via the Z-Wave JS integration's server client | Raw NVM image | `.nvm` |

Each backup gets a `.meta.json` sidecar with the coordinator's identity (IEEE / home ID, PAN ID, channel, SDK version, device count), so you can tell at a glance which network a file belongs to.

## Restore

Pre-flight first. The `restore` service without `confirm` is a dry run: it tells you what it found on the live coordinator, what's in the backup, and any warnings.

```yaml
service: radio_guard.restore
data:
  source: zwave_js
# returns: current controller info, backup info, warnings, hint
```

Then do it:

```yaml
service: radio_guard.restore
data:
  source: zwave_js
  confirm: true
```

Per source:

- **Z-Wave JS**: restores the NVM through zwave-js, then reloads the integration so it picks up the new home ID. Works for the add-on, Z-Wave JS UI, or a standalone `zwave-js-server`.
- **ZHA**: restores through zigpy, which writes the network to whatever radio is attached and handles stack conversion. The EUI64 overwrite flag for Silabs radios is set automatically (that write is one-time per chip).
- **Zigbee2MQTT**: Z2M has no restore API; it restores from `coordinator_backup.json` in its data directory on startup. If Z2M runs as the Supervisor add-on, Radio Guard stops the add-on, writes the file, and starts it. Otherwise it stages the files under `/config/radio_guard/zigbee2mqtt/restore/` and tells you where to copy them.

Never run two coordinators with the same identity on one mesh. Unplug the old one before restoring to the new one.

## Watchdog

Radio Guard publishes a JSON heartbeat to `radio_guard/heartbeat` every 30 s (configurable). `esphome/watchdog.yaml` is a reference ESPHome config for an ESP32 with a relay in series with the HA box's power: if the heartbeat stops for 5 minutes it cycles power once, then backs off for an hour. Pair it with the plan in the next section and the most common real-world failure (a hung Pi) fixes itself while you sleep.

## The recovery plan this is built for

Radio Guard is one piece of a deliberately simple home-automation resilience plan:

1. One HA box on decent storage. Config in git, HA backups to a NAS.
2. Coordinator backups (this integration) landing in the same place.
3. A spare boot drive and a spare coordinator in a drawer.
4. A dumb power watchdog.
5. Local control where it's free: Zigbee bindings, Z-Wave associations, Shelly scripts, so lights work with HA down.

No standby nodes, no clusters. Worst case is ten minutes with a screwdriver, and nothing important went dark in the meantime.

## Services

| Service | Description |
|---|---|
| `radio_guard.backup_now` | Back up one source (`source:`) or all. Returns file info. |
| `radio_guard.restore` | Dry run by default. `confirm: true` to restore. `force: true` to override a failed pre-flight. |

## Entities (per source)

- `sensor.*_last_backup` (timestamp, attributes carry the metadata)
- `sensor.*_backup_age` (hours)
- `sensor.*_stored_backups`
- `sensor.*_coordinator_identity` (IEEE or home ID)
- `sensor.*_last_error`
- `binary_sensor.*_backup_stale` (problem)
- `binary_sensor.*_source_available` (connectivity)
- `button.*_back_up_now`

## Events

- `radio_guard_backup_complete` with `source`, `file`, `size`
- `radio_guard_restore_complete` with `source`, `file`

## Requirements

- Home Assistant 2024.10 or newer
- For Zigbee2MQTT: the MQTT integration configured against the same broker
- For ZHA: the ZHA integration
- For Z-Wave JS: the Z-Wave JS integration

## License

MIT
