# Radio Guard: HACS packaging and UX review

Reviewer stance: someone who maintains HACS integrations and has watched a lot of people install them, ignore them for a year, and then try to use them for the first time at 2am with a dead stick. Everything below is judged against the three stated goals: easy onboarding via HACS, easy to start backups, easy to use when failure happens.

Files reviewed: `README.md`, `hacs.json`, `manifest.json`, `strings.json`, `translations/en.json`, `services.yaml`, `config_flow.py`, `__init__.py`, `coordinator.py`, `sensor.py`, `binary_sensor.py`, `button.py`, `entity.py`, `storage.py`, `backup/*.py`, `esphome/watchdog.yaml`, `.github/workflows/validate.yml`.

## Top ten, in priority order

| # | Item | Why it matters | Section |
|---|---|---|---|
| 1 | Run a first backup immediately after setup and show a persistent notification saying where it landed | The product is worthless until the first backup exists; today the user waits up to 24 h and gets no confirmation | 2 |
| 2 | Repair-driven guided restore (fixable issue + RepairsFlow) | The restore path today is "open Developer Tools and type YAML from memory" at the worst possible moment | 3 |
| 3 | `restore` without `confirm` returns a response nobody sees unless they tick "return response" in Developer Tools; make the pre-flight visible (notification + repair) | Dry run is the safety feature and it is effectively hidden | 3 |
| 4 | Collapse the 10-field config flow to one step: sources (pre-selected) + optional "advanced" section | First-run friction, and most fields have good defaults | 2 |
| 5 | Add `zip_release`/`filename` to `hacs.json`, tag releases, and publish to the HACS default repo once brands PR is merged | Without releases HACS installs from default branch; without default repo, every user needs a custom repo URL | 1 |
| 6 | Make `backup_stale` a fixable issue that offers "Back up now" from the Repairs panel | Turns a nag into a one-click fix | 3 |
| 7 | Dynamic backup picker in the restore service (`select` populated from stored files) or at least a `date`-sortable filename in the field description | Nobody knows the filename `20261107T031700Z.nvm` exists | 3 |
| 8 | Add `next_backup` timestamp sensor, make `last_error` an attribute, add icons and `entity_category` consistently | Dashboard clarity and less entity sprawl | 4 |
| 9 | README restructure: 5-line quick start at top, per-source restore checklist, "what this does NOT do", FAQ | First-time visitor currently reads 800 words before seeing "Install" | 6 |
| 10 | Fix the ESPHome watchdog's `millis()` overflow and the "no heartbeat since boot" hole; add a Shelly script alternative | Safety of a thing that cuts power | 5 |

Note on a code-level bug I spotted while tracing the flows (not strictly UX, but it will become a UX problem): `storage.py` writes the sidecar as `<stem>.meta.json` but `storage.py` module docstring and `README.md` say `<stem>.json` for Z2M/Z-Wave. Also `_meta_path_for` splits on the first `.` so `20261107T031700Z.meta.json` resolves to `20261107T031700Z` correctly, but `list()` filters only `META_SUFFIX` and `.tmp`; any stray `.json` from the documented-but-not-implemented layout would be listed as a backup. Align the docstring/README with the code.

---

## 1. HACS packaging

### 1.1 `hacs.json`

Current:

```json
{ "name": "Radio Guard", "render_readme": true, "homeassistant": "2024.10.0" }
```

Recommended:

```json
{
  "name": "Radio Guard",
  "render_readme": true,
  "homeassistant": "2024.10.0",
  "zip_release": true,
  "filename": "radio_guard.zip",
  "hide_default_branch": true
}
```

Field by field:

- `name`: fine. HACS shows this in the store; keep it identical to `manifest.json` `name` or the HACS validation action will warn.
- `render_readme`: keep `true`. HACS removed `info.md` support for new repositories; README is the only thing rendered. Do not add `info.md`.
- `homeassistant`: `2024.10.0` is right given the code uses `type RadioGuardConfigEntry = ConfigEntry[...]` (PEP 695, needs Python 3.12 → HA 2024.4+) and `entry.runtime_data` (2024.4+). Keep it. Consider bumping to `2025.1.0` if you adopt the `SubentryFlow` or newer repairs APIs recommended below; otherwise leave it.
- `zip_release` + `filename`: adopt. It makes HACS download a release asset rather than a tarball of the tag, which (a) lets you strip `tests/`, `esphome/`, `.github/` from what lands on users' disks, (b) makes "which version is the user on" unambiguous in bug reports. The filename must match exactly what your release workflow uploads. The zip must contain the contents of `custom_components/radio_guard/` at its root (i.e. `manifest.json` at top level of the zip), not the folder itself.
- `hide_default_branch`: set `true` once you have at least one release so users cannot pick `master` from the version dropdown and end up on an unreleased commit.
- `country`: omit. It is a filter for region-specific integrations (weather, energy providers). Radio Guard is global.
- `content_in_root`: do not set; your layout is already the standard `custom_components/<domain>` one.
- `persistent_directory`: deprecated; do not add. Your data lives in `/config/radio_guard/`, outside the integration folder, which is exactly right so reinstall/upgrade does not touch backups.

### 1.2 Repository structure

Required and present: `custom_components/radio_guard/` at repo root with `manifest.json`, `hacs.json` at root, `README.md`, `LICENSE`. Good.

Things to fix or add:

- `manifest.json` key ordering: hassfest requires `domain`, `name` first, then the rest alphabetical. Yours is `domain, name, codeowners, config_flow, dependencies, after_dependencies, documentation, integration_type, iot_class, issue_tracker, requirements, version` which is not alphabetical (`after_dependencies` must precede `codeowners`, `dependencies` precede `documentation`, etc.). hassfest will fail on this. Correct order:

  ```json
  {
    "domain": "radio_guard",
    "name": "Radio Guard",
    "after_dependencies": ["mqtt", "zha", "zwave_js", "hassio"],
    "codeowners": ["@bbensten"],
    "config_flow": true,
    "dependencies": [],
    "documentation": "https://github.com/bbensten/radio-guard",
    "integration_type": "service",
    "iot_class": "local_polling",
    "issue_tracker": "https://github.com/bbensten/radio-guard/issues",
    "requirements": [],
    "version": "0.1.0"
  }
  ```

  Add `hassio` to `after_dependencies` since `z2m.py` imports from `homeassistant.components.hassio`.
- `iot_class`: `local_polling` is defensible (15-minute poll). `calculated` would also be accepted and arguably more honest since you never touch hardware. Either passes.
- `integration_type: "service"` is right; it suppresses the "device" semantics in the UI. Note that `service` integrations are expected to create one device per config entry, so if you keep one device per source, consider `"hub"` instead (see section 4).
- Remove `.venv/` from the repo. It is gitignored but the directory exists in the tree; make sure it is not committed. Also remove `entry_type=None` in `entity.py`; it is the default and reads as if something was meant.
- `tests/` currently contains only `__init__.py`. The validate workflow runs `pytest -q` which will exit 5 ("no tests collected") and fail CI. Either add one smoke test or add `--no-header -p no:cacheprovider` and accept exit code 5 (`pytest -q || [ $? -eq 5 ]`).
- `requirements_test.txt` is not reviewed here but must include `pytest-homeassistant-custom-component` pinned to a version matching `homeassistant` min version if you want tests to load HA.

### 1.3 Brands

HACS and HA core only show a logo/icon for a custom integration if it exists in `home-assistant/brands` under `custom_integrations/radio_guard/`. Until then the integration tile shows a generic placeholder in Settings → Integrations and the HACS store, which reads as "unfinished" to users. Submit a PR with:

```
custom_integrations/radio_guard/icon.png       256x256, transparent
custom_integrations/radio_guard/icon@2x.png    512x512
custom_integrations/radio_guard/logo.png       optional, wide
custom_integrations/radio_guard/logo@2x.png
```

Pick something visually distinct from the ZHA, Z-Wave JS and Zigbee2MQTT marks so users do not confuse the device cards. A shield with a radio-wave glyph fits the name. The PR is routine but takes 1-3 weeks to merge; open it early.

### 1.4 Release tagging

- Use SemVer tags `v0.1.0` etc. HACS reads GitHub releases (not bare tags) when `zip_release` is set, so always "Publish release", not just push a tag.
- Keep `manifest.json` `version` in sync with the tag. HACS compares them and shows a confusing "update available" loop if they diverge. Automate: a release workflow that fails if `manifest.version != tag`.
- Pre-releases: mark them as GitHub pre-releases; HACS hides them unless the user opts in. Use this for the restore-flow work.
- Suggested release workflow (`.github/workflows/release.yml`):

  ```yaml
  name: Release
  on:
    release:
      types: [published]
  jobs:
    build:
      runs-on: ubuntu-latest
      permissions:
        contents: write
      steps:
        - uses: actions/checkout@v4
        - name: Check version matches tag
          run: |
            v=$(jq -r .version custom_components/radio_guard/manifest.json)
            [ "v$v" = "${GITHUB_REF_NAME}" ] || { echo "manifest $v != tag $GITHUB_REF_NAME"; exit 1; }
        - name: Zip
          run: cd custom_components/radio_guard && zip -r ../../radio_guard.zip .
        - uses: softprops/action-gh-release@v2
          with:
            files: radio_guard.zip
  ```

- Changelog: a `CHANGELOG.md` or release notes with a "Breaking" section. For a backup tool, explicitly note any change to on-disk layout or filenames, since users may have automations pointing at them.

### 1.5 What the HACS validation action checks (`hacs/action` with `category: integration`)

- Repository has a description and topics on GitHub (set topics: `home-assistant`, `hacs`, `zigbee`, `z-wave`, `backup`).
- Repository is not archived, has issues enabled.
- `hacs.json` exists, is valid JSON, has `name`.
- `README.md` or `info.md` exists.
- `custom_components/<domain>/manifest.json` exists with `domain`, `documentation`, `issue_tracker`, `codeowners`, `version`.
- Brands: warns (and for default-repo inclusion, fails) if `custom_integrations/<domain>` is missing from home-assistant/brands.
- If `zip_release` is set, the latest release must have an asset named `filename`.
- Images referenced in README must resolve.

Your workflow already runs it on push, PR and weekly. Good. Also pin `hacs/action@main` → it is what HACS recommends, leave it.

### 1.6 Publish to the HACS default repository?

Yes. The product's entire thesis is "set it up once, forget it until you need it." Requiring a custom repository URL is the single biggest drop-off in that funnel; people copy the URL wrong, pick the wrong category, or never find the ⋮ menu. The default-repo PR to `hacs/default` requires:

1. Brands PR merged.
2. At least one GitHub release (with the zip asset if `zip_release`).
3. HACS action and hassfest passing on the default branch.
4. Repository description and topics set.
5. The integration has been "in use" for a bit; HACS maintainers like to see a working README and a few closed issues, but it is not a hard rule.
6. Your PR to `hacs/default` adds `bbensten/radio-guard` to the `integration` list, alphabetically.

Timeline: do 1.2 fixes → first release → brands PR → wait for merge → default PR. Realistically four to six weeks. In the meantime the README's custom-repo instructions are fine; add a My Home Assistant badge so the user does not have to type the URL:

```markdown
[![Open your Home Assistant instance and open a repository inside HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=bbensten&repository=radio-guard&category=integration)
```

Once in the default repo, replace that with the plain `hacs_repository` badge without owner/repository, plus a config-flow badge:

```markdown
[![Add integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=radio_guard)
```

---

## 2. Onboarding flow

### 2.1 What happens today

User installs via HACS, restarts, adds the integration, and sees one form with ten fields: sources (multi-select list), Z2M base topic, hour, minute, retention, stale hours, backup folder, heartbeat enabled, heartbeat topic, heartbeat interval. Sources are pre-selected from detection, which is good. Then they click Submit and... nothing visible happens. No backup runs. The "Backup stale" binary sensor turns on immediately (age is `None` → stale), and within 15 minutes a Repair appears saying "Zigbee2MQTT backup is stale: never". The first thing the user sees after setup is a warning generated by the thing they just set up. That is the opposite of reassuring.

### 2.2 Proposed flow

One required step, one optional step, everything else in Options.

**Step `user`** (required):

- Title: "Which coordinators should Radio Guard protect?"
- Fields:
  - `sources`: multi-select, pre-checked from detection. Show detected-but-unloaded ones as options too, but add a description line: "Detected: Zigbee2MQTT (via MQTT), Z-Wave JS. Not detected: ZHA."
  - `z2m_base_topic`: only shown if Z2M was detected or selected (use a second mini-step `z2m` that only appears when `zigbee2mqtt in sources` and the topic cannot be inferred). Better: infer it by subscribing to `+/bridge/info` for 3 seconds during the flow and pre-fill. Most users never change `zigbee2mqtt`, and the ones who did will recognize the pre-filled value.
  - `run_first_backup_now`: boolean, default `true`, label "Take a first backup right away".
- Submit → create entry → `async_setup_entry` kicks off the first backup if the flag is set.

**Step `advanced`** (optional, reachable via a checkbox "Show advanced settings" on step 1 or just leave it to Options). Honestly: skip it. Put everything else in Options and say so in the step description: "Schedule, retention and the watchdog heartbeat can be changed later under Configure."

**Options flow**: keep all fields but use `section()` groups (HA 2024.6+) so they collapse:

```python
from homeassistant.data_entry_flow import section

vol.Schema({
    vol.Required(CONF_SOURCES): ...,
    vol.Required("schedule"): section(
        vol.Schema({
            vol.Optional(CONF_BACKUP_HOUR, default=3): ...,
            vol.Optional(CONF_BACKUP_MINUTE, default=17): ...,
            vol.Optional(CONF_RETENTION, default=14): ...,
            vol.Optional(CONF_STALE_HOURS, default=36): ...,
        }),
        {"collapsed": True},
    ),
    vol.Required("storage"): section(
        vol.Schema({vol.Optional(CONF_BACKUP_DIR, default="radio_guard"): str}),
        {"collapsed": True},
    ),
    vol.Required("heartbeat"): section(
        vol.Schema({
            vol.Optional(CONF_HEARTBEAT_ENABLED, default=True): bool,
            vol.Optional(CONF_HEARTBEAT_TOPIC, default="radio_guard/heartbeat"): str,
            vol.Optional(CONF_HEARTBEAT_INTERVAL, default=30): ...,
        }),
        {"collapsed": True},
    ),
})
```

Note that `section()` nests the values one level deep in `user_input`, so `_coerce` and `_opt` need to flatten or read nested. Add a `VERSION = 2` migration that moves existing flat keys into the nested shape (or just keep storage flat and only nest in the schema/un-nest on submit, which is simpler).

Use a `TimeSelector` for the backup time instead of two number boxes:

```python
vol.Optional(CONF_BACKUP_TIME, default="03:17:00"): selector.TimeSelector()
```

One field instead of two, and the picker is localized.

For `stale_hours`, use a `NumberSelector` with `unit_of_measurement="h"` and `mode=SLIDER`, min 12, max 168, step 12. Nobody needs 720.

### 2.3 Defaults (recommendations)

| Setting | Current | Recommended | Reason |
|---|---|---|---|
| Backup time | 03:17 | 03:17 local | Fine. Avoids the 03:00 HA backup rush. Document that it is local time. |
| Retention | 14 | 14 | Fine. Files are KB to low MB. |
| Stale threshold | 36 h | 36 h | Fine; one missed night plus slack. |
| Backup dir | `radio_guard` | `radio_guard` | Fine. Do not let users pick an absolute path outside `/config`; strip and validate in the flow, not only in `_coerce`. |
| Heartbeat enabled | `true` | `false` unless MQTT is loaded; `true` if it is | Today it is `true` even for a ZHA-only install with no MQTT, which silently does nothing. Tie the default to detection. |
| Heartbeat interval | 30 s | 30 s | Fine. |
| First backup now | n/a | `true` | See 2.4. |

### 2.4 Automatic first backup: yes

Run it in `async_setup_entry` after `async_start()`, as a background task so setup does not block:

```python
if entry.data.get(CONF_FIRST_BACKUP, True) and not entry.options.get("_first_backup_done"):
    entry.async_create_background_task(hass, _first_backup(hass, entry, coordinator), "radio_guard_first_backup")
```

`_first_backup` waits 30 s (so ZHA/Z-Wave have finished loading; `after_dependencies` only orders setup start, not readiness), calls `async_backup_all()`, then:

- On success for all sources: create a persistent notification:

  > **Radio Guard is protecting your network.**
  > First backups written:
  > - Zigbee2MQTT: `/config/radio_guard/zigbee2mqtt/20261107T153012Z.zip` (14 devices, IEEE 00:12:4b:...)
  > - Z-Wave JS: `/config/radio_guard/zwave_js/20261107T153014Z.nvm` (home ID 0xD1A2B3C4, 31 nodes)
  >
  > Backups run nightly at 03:17 and are included in Home Assistant's own backups. Make sure those go somewhere off this box.

- On any failure: a Repair (the existing `backup_failed`) plus the notification listing what succeeded.

Persist a marker (`entry.options["_first_backup_done"] = True` via `async_update_entry`) so it does not re-run on every reload.

The notification does three jobs at once: confirms success, teaches the user where the files are, and plants the "off-box backups" idea that the whole plan depends on.

### 2.5 Stale notification blueprint and dashboard card

Ship `blueprints/automation/radio_guard/backup_stale_notify.yaml` in the repo and link it with a My HA blueprint-import badge. Keep it tiny:

```yaml
blueprint:
  name: Radio Guard - notify when a coordinator backup is stale or failed
  domain: automation
  input:
    stale_sensor:
      name: Backup stale sensor
      selector:
        entity:
          filter:
            integration: radio_guard
            domain: binary_sensor
            device_class: problem
    notify_service:
      name: Notify service
      selector:
        text:
      default: notify.notify
trigger:
  - platform: state
    entity_id: !input stale_sensor
    to: "on"
    for: "01:00:00"
action:
  - service: !input notify_service
    data:
      title: "Coordinator backup is stale"
      message: >
        {{ state_attr(trigger.entity_id, 'friendly_name') }} has been on for an hour.
        Last backup: {{ states('sensor.' ~ trigger.entity_id.split('.')[1].replace('_backup_stale', '_last_backup')) }}.
```

(Better: have the blueprint take the device instead of the entity and derive sensors via `device_entities`; the entity-id string surgery above is fragile. Shown for brevity.)

Dashboard card example for the README (one per source, or use an `auto-entities` card filtered by integration):

```yaml
type: entities
title: Radio Guard
entities:
  - entity: binary_sensor.radio_guard_zigbee2mqtt_backup_stale
    name: Zigbee2MQTT
  - entity: sensor.radio_guard_zigbee2mqtt_last_backup
  - entity: sensor.radio_guard_zigbee2mqtt_next_backup
  - entity: button.radio_guard_zigbee2mqtt_back_up_now
```

Since HA already surfaces `problem` binary sensors and Repairs in the Settings badge, most users need nothing beyond the blueprint; put the card in the README as a "nice to have".

---

## 3. Failure-day UX

### 3.1 Walkthrough as it stands

Scenario: Z-Wave stick dies on a Tuesday night. User has had Radio Guard for eight months and has forgotten the details.

1. Lights stop responding. User eventually notices Z-Wave JS integration is red ("Failed to set up"). Radio Guard's `Source available` goes off; `Backup stale` will go on after 36 h. No message from Radio Guard says "your coordinator is gone and you have a backup from last night". **Friction: nothing tells the user that Radio Guard is relevant right now.**
2. User orders/finds a spare stick. Plugs it in. Z-Wave JS add-on may auto-detect the new path or may not; if the device path changed (`/dev/serial/by-id/...` differs), the Z-Wave JS integration must be re-configured first. README does not mention this. **Friction: upstream re-pointing is a prerequisite and is undocumented.**
3. Z-Wave JS starts with the new stick as an empty controller with a new home ID. HA now shows all nodes as dead. User panics. **Friction: nothing says "this is expected, do not delete anything".**
4. User remembers Radio Guard. Opens the README. Finds the `restore` service YAML. Goes to Developer Tools → Actions. Types `radio_guard.restore`, source `zwave_js`. Clicks "Perform action". Gets a green check and... nothing. The response is only shown if they switched to YAML mode and the UI "return response" is visible, and even then it is a wall of JSON. **Friction: the dry run is invisible by default; the user does not know whether anything happened.**
5. User reads the JSON, sees `ok: true`, `warnings: [...]`. The warning about "Restore wipes the target controller" is scary and ambiguous (does it wipe the one that already died?). **Friction: warnings are written for the author, not the user.**
6. User adds `confirm: true`. Restore runs; `async_restore` reloads the entry. If the restore takes more than the UI timeout (10 s) they see a spinner then an error even though it succeeded. **Friction: long-running service with no progress.**
7. Nodes come back as "unknown" until interviewed; battery devices stay dead until woken. README says this in one sentence inside a warning string. **Friction: post-restore expectations are not set.**
8. Which backup was used? The user did not specify `file`, so "latest". If the latest was taken after the stick had already started failing (corrupted NVM), they just restored garbage and have no idea. **Missing safeguard: no way to see the list and pick, and no sanity check comparing the latest to the previous one.**

Zigbee2MQTT variant adds: the add-on stop/write/start path needs Supervisor and the correct data dir; if the user runs Z2M in Docker they get a `RuntimeError` after the files are staged, which surfaces as a red toast with a long message that is cut off. ZHA variant adds: the user must first re-point ZHA at the new radio via its own config flow (ZHA's "Migrate radio" wizard will offer to restore from ZHA's own backups, which may be older or absent; the user needs to know to pick "Keep radio network settings" / skip, then run Radio Guard).

### 3.2 Repair-driven guided restore

Make the failure visible and actionable from the Repairs panel. Two triggers:

- **Identity change detected**: on each poll, if `st.identity` has an `ieee`/`home_id` that differs from `st.latest.meta` identity, raise a fixable issue `coordinator_replaced_<sid>` with severity `warning`: "The Z-Wave JS controller now reports home ID 0xAAAA, but your newest backup is for 0xD1A2. If you replaced the stick, restore the backup now." This catches the exact moment the user plugs in the spare.
- **Manual entry**: a `button.<source>_restore` entity (or a service `radio_guard.start_restore`) that raises the same fixable issue so it can be reached from a dashboard.

RepairsFlow skeleton (`repairs.py`):

```python
"""Repairs: guided coordinator restore."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector

from .const import DOMAIN


class RestoreRepairFlow(RepairsFlow):
    """Pick a backup -> pre-flight -> confirm -> done."""

    def __init__(self, hass: HomeAssistant, sid: str) -> None:
        self.sid = sid
        self.coord = hass.config_entries.async_loaded_entries(DOMAIN)[0].runtime_data
        self.file: str | None = None
        self.check = None
        self.record = None

    async def async_step_init(self, user_input=None) -> data_entry_flow.FlowResult:
        return await self.async_step_pick()

    async def async_step_pick(self, user_input=None) -> data_entry_flow.FlowResult:
        st = self.coord.states[self.sid]
        records = await self.hass.async_add_executor_job(st.store.list)
        if not records:
            return self.async_abort(reason="no_backups")
        if user_input is not None:
            self.file = user_input["file"]
            return await self.async_step_preflight()
        options = [
            selector.SelectOptionDict(
                value=r.name,
                label=f"{r.created.astimezone():%Y-%m-%d %H:%M}  -  "
                      f"{r.meta.get('device_count') or r.meta.get('node_count', '?')} devices  -  "
                      f"{r.meta.get('ieee') or r.meta.get('home_id', '')}",
            )
            for r in records
        ]
        return self.async_show_form(
            step_id="pick",
            data_schema=vol.Schema({
                vol.Required("file", default=records[0].name): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=options, mode=selector.SelectSelectorMode.DROPDOWN)
                )
            }),
            description_placeholders={"source": st.source.name, "count": str(len(records))},
        )

    async def async_step_preflight(self, user_input=None) -> data_entry_flow.FlowResult:
        if self.check is None:
            try:
                self.record, self.check = await self.coord.async_restore_check(self.sid, self.file)
            except Exception as err:  # noqa: BLE001
                return self.async_abort(reason="preflight_error", description_placeholders={"error": str(err)})
        if user_input is not None:
            if not user_input.get("acknowledge"):
                return await self.async_step_preflight()
            return await self.async_step_confirm()
        c, b = self.check.current, self.check.backup
        return self.async_show_form(
            step_id="preflight",
            data_schema=vol.Schema({vol.Required("acknowledge", default=False): bool}),
            description_placeholders={
                "file": self.record.name,
                "backup_identity": str(b.get("ieee") or b.get("home_id") or "?"),
                "backup_devices": str(b.get("device_count") or b.get("node_count") or "?"),
                "backup_age": f"{(self.coord.states[self.sid].age_hours or 0):.0f}",
                "current_identity": str(c.get("ieee") or c.get("home_id") or "not readable"),
                "status": "OK" if self.check.ok else "BLOCKED: " + "; ".join(self.check.reasons),
                "warnings": "\n".join(f"- {w}" for w in self.check.warnings) or "- none",
            },
            errors={} if self.check.ok else {"base": "preflight_failed"},
        )

    async def async_step_confirm(self, user_input=None) -> data_entry_flow.FlowResult:
        if user_input is not None:
            if user_input.get("typed") != "RESTORE":
                return self.async_show_form(step_id="confirm", data_schema=_CONFIRM_SCHEMA, errors={"typed": "type_restore"})
            try:
                await self.coord.async_restore(self.sid, self.file, force=not self.check.ok)
            except Exception as err:  # noqa: BLE001
                return self.async_abort(reason="restore_failed", description_placeholders={"error": str(err)})
            return self.async_create_entry(title="", data={})
        return self.async_show_form(step_id="confirm", data_schema=_CONFIRM_SCHEMA)


_CONFIRM_SCHEMA = vol.Schema({vol.Required("typed"): str})


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None) -> RepairsFlow:
    return RestoreRepairFlow(hass, (data or {})["source"])
```

Register the issue with `is_fixable=True` and `data={"source": sid}`. The `repairs` platform needs `"repairs"` added to `dependencies` in `manifest.json` (or keep it in `after_dependencies`; core integrations do `dependencies`).

Typing `RESTORE` is deliberate. It is a one-way operation that wipes a controller; a checkbox is not enough friction at 2am.

Corresponding `strings.json` additions:

```json
"issues": {
  "coordinator_replaced": {
    "title": "{source}: coordinator changed, restore available",
    "fix_flow": {
      "step": {
        "pick": {
          "title": "Restore {source} network",
          "description": "{count} stored backups. Newest is first. Pick the one taken before the old coordinator failed.",
          "data": { "file": "Backup" }
        },
        "preflight": {
          "title": "Pre-flight check",
          "description": "**Backup** `{file}`\nNetwork {backup_identity}, {backup_devices} devices, {backup_age} h old\n\n**Attached coordinator** {current_identity}\n\n**Status:** {status}\n\n**Before you continue**\n{warnings}",
          "data": { "acknowledge": "The old coordinator is unplugged and I have read the above" }
        },
        "confirm": {
          "title": "Type RESTORE to continue",
          "description": "This overwrites the attached coordinator. It cannot be undone from here.",
          "data": { "typed": "Confirmation" }
        }
      },
      "error": {
        "preflight_failed": "Pre-flight found a blocking problem. Continuing will force the restore.",
        "type_restore": "Type RESTORE in capitals."
      },
      "abort": {
        "no_backups": "There are no stored {source} backups to restore.",
        "preflight_error": "Could not read the backup or the coordinator: {error}",
        "restore_failed": "Restore failed: {error}. Nothing further was changed; check the logs."
      }
    }
  }
}
```

Also make the existing `backup_stale` and `backup_failed` issues fixable with a trivial one-step `ConfirmRepairFlow` subclass whose confirm runs `async_backup(sid)`. That turns "press the Back up now button" (which the user has to go find) into a button in the Repair itself.

### 3.3 Service field selectors

`services.yaml` improvements, in order of value:

1. **`file` field**: the text box with example `20261107T031700Z.nvm` is unusable. Options:
   - Short term: keep text, but change the description to "Leave empty for the newest. To see the list, check `sensor.<source>_last_backup` attributes or the `backups` attribute on the Stored backups sensor." and add a `backups` attribute (list of `{file, created, devices}`) to `BackupCountSensor`.
   - Medium term: a `radio_guard.list_backups` service with `supports_response=ONLY` that returns the list; document the two-step dance.
   - Long term: the Repair flow above makes the service a power-user path only.
   HA service selectors cannot be populated dynamically from the integration today, so a dropdown of real filenames is not possible in `services.yaml`; that is the main argument for the Repair flow.
2. **`source` field**: use `translation_key: source` on the select and move labels into `strings.json` under `selector.source.options` so they translate. Also the service accepts sources that are not enabled and raises a plain `HomeAssistantError`; use `ServiceValidationError` with a `translation_key` so the UI shows a localized, clear message.
3. **`confirm` vs `force`**: two booleans with similar names is a footgun. Rename: `mode: dry_run | restore` (select, default `dry_run`) and keep `force` as a separate advanced boolean with `advanced: true` so it is hidden unless the user has advanced mode on.
4. Add `target: {}`? No. There is one instance; no target needed.
5. Mark the restore service as destructive in the description's first sentence: "Overwrites the attached coordinator's network. Dry run by default."

### 3.4 Dry-run output in the UI

Service responses in Developer Tools render as YAML. Today the response is a flat dict with `current`, `backup` (each a dict of ~10 keys) and strings. Reshape it for reading top to bottom:

```yaml
dry_run: true
verdict: OK            # or BLOCKED
summary: "Restore Z-Wave JS network 0xD1A2B3C4 (31 nodes, backed up 9 h ago) onto controller 0x0000 (empty, SDK 7.19.3)"
backup:
  file: 20261107T031700Z.nvm
  created: 2026-11-07T03:17:00+00:00
  age_hours: 9.2
  home_id: 0xD1A2B3C4
  node_count: 31
  sdk_version: 7.17.2
target:
  home_id: 0x00000000
  node_count: 0
  sdk_version: 7.19.3
  firmware_version: 7.19
blocking: []
warnings:
  - "Restore wipes the attached controller. Routes are rebuilt; battery devices re-report on next wake."
  - "SDK 7.17 -> 7.19: zwave-js converts forward only."
next: "Call again with mode: restore to perform it, or open Settings > Repairs for a guided restore."
```

Changes: rename `current` → `target` (the user is thinking "the new stick"), add a one-line `summary` and `verdict`, format IDs as hex, drop keys that are `null`, and reword warnings in second person with the consequence first.

Also raise the dry run result as a **persistent notification** (dismissable) so the user who did not tick "return response" still sees it. Title: "Radio Guard dry run: OK to restore Z-Wave JS". Body: the summary plus warnings. Same for the restore result.

### 3.5 Restore checklist in the README (per source)

Add a section "When your coordinator dies" with a checklist per source. Draft:

**Z-Wave JS**

1. Do not delete any devices or the Z-Wave JS integration. Dead nodes are expected.
2. Unplug the old stick. Never have both plugged in.
3. Plug in the replacement. Same series or newer (500 → 700 → 800 is fine; 800 → 700 is not).
4. Point Z-Wave JS at the new stick: Z-Wave JS UI add-on → Settings → Z-Wave → Serial port (use `/dev/serial/by-id/...`). Restart the add-on. Wait until the Z-Wave JS integration loads (it will show one node: the controller).
5. Settings → Repairs → "Z-Wave JS: coordinator changed" → Fix → follow the steps. Or run `radio_guard.restore` with `source: zwave_js`.
6. After restore the add-on restarts; nodes appear as "unknown" and interview over the next minutes. Wake battery devices manually (press the button on each) or wait for their wake-up interval.
7. If your sticks have different region firmware (US/EU), the restore will fail; flash the region first.

**ZHA**

1. Do not remove the ZHA integration.
2. Unplug old radio, plug in new. Cross-vendor (TI → Silabs) is supported by zigpy.
3. ZHA will show "Failed to set up" or offer "Migrate radio". Go to Settings → Devices & services → ZHA → Configure → Migrate radio, pick the new port. When asked about restoring a backup, choose **Upload a backup** and pick the newest file from `/config/radio_guard/zha/`, **or** choose "Erase/Start new" and then run Radio Guard's restore. The first path is simpler because ZHA's wizard already does what Radio Guard would do; Radio Guard's value here is that the file exists at all.
4. Silabs (SkyConnect, Sonoff -E) radios can have their EUI64 written only once. Radio Guard sets the flag; if that radio was ever used in another network, the restore will keep the radio's own IEEE and end devices will need re-join.
5. Expect routers to reconnect within minutes and end devices within hours; some sleepy devices need a button press.

**Zigbee2MQTT**

1. Stop the Z2M add-on (or container). Unplug old, plug in new.
2. Update `serial.port` in Z2M's `configuration.yaml` (or add-on config) to the new adapter; set `serial.adapter` if the chip family changed.
3. Add-on: run Radio Guard restore; it stops Z2M, writes `coordinator_backup.json` and `database.db`, starts it.
   Docker/standalone: run the restore anyway; it stages the files under `/config/radio_guard/zigbee2mqtt/restore/`. Copy both files into Z2M's data directory (next to `configuration.yaml`), then start Z2M.
4. Z2M log should say "Restoring coordinator from backup". If it says "Coordinator backup is for a different network", the file is wrong or the adapter was not reset; stop Z2M, delete `coordinator_backup.json`, and retry.
5. Do not run Z2M with the new adapter before the restore; a fresh start creates a new network and the first boot will refuse to overwrite it unless `advanced.transmit_power`... (no; rather, delete the fresh `coordinator_backup.json` it wrote and restore again).

Keep these as collapsible `<details>` blocks so the README stays scannable.

### 3.6 Safeguards

Implement in `async_restore` / `async_restore_check`, surfaced as `reasons` (blocking) or `warnings`:

| Safeguard | Blocking? | Today |
|---|---|---|
| Upstream integration must be loaded (`available()`) | Yes | Not checked in restore path; `_controller()` raises `SourceUnavailable` late with a generic message. Check first and give a clear reason: "Z-Wave JS is not loaded. Point it at the new stick first." |
| Live identity matches backup identity | Warning → make it blocking unless `force` | Warning only. Restoring onto a stick that already has the network is almost always a mistake (e.g. the user picked the wrong source). Block with "Attached coordinator already has this network. Use force if you really want to rewrite it." |
| Backup older than N days | Blocking unless `force`, N = 7 by default (option `max_restore_age_days`) | Not checked. A six-month-old backup restored onto a mesh that gained 20 devices is a half-broken network and the user will blame Radio Guard. |
| Backup taken while source was already failing | Warning | Not checked. Compare `device_count`/`node_count` with the previous record; if the newest has dramatically fewer (say <50%) warn "Newest backup has 3 nodes; the one before it has 31. The stick may have been failing when it was taken. Consider restoring the earlier one." This is the single most valuable heuristic you can add. |
| Z-Wave: target SDK older than backup | Blocking | Only a generic warning today; the code says "cannot restore to an older series" but does not block. Parse major versions and block. |
| Z-Wave: target not empty (node_count > 1) | Warning | Not checked. "Attached controller already has 12 nodes; they will be wiped." |
| ZHA: backup `is_complete() == False` | Warning | Present. Keep. |
| Z2M: no `coordinator_backup.json` in zip | Blocking | Present. Keep. |
| Two restores in flight | Blocking | `running` flag exists for backup only; add for restore. |
| Z2M restore writes `database.db` over a newer one | Warning | Not mentioned. Say "Z2M device database will be replaced by the one from the backup (devices paired since are lost)." |

Also: before any restore, take a backup of the *target* coordinator if it has a network (`node_count > 1` or ZHA/Z2M has devices) and store it as `pre_restore_<ts>`. It costs seconds and makes "oops" recoverable.

---

## 4. Entities, devices, naming

### 4.1 Devices: one per source or one overall?

Keep **one device per source**, but rename them. Today the device is "Radio Guard Zigbee2MQTT" with manufacturer "Radio Guard" and model "Zigbee2MQTT". With `has_entity_name=True` the entities read "Radio Guard Zigbee2MQTT Last backup", which is long but unambiguous. Proposed:

- Device name: `Zigbee2MQTT backups`, `ZHA backups`, `Z-Wave JS backups`.
- Manufacturer: `Radio Guard`; model: `Coordinator backup`; `sw_version`: integration version; `configuration_url`: leave unset (there is no UI).
- Link each device to the upstream integration's device with `via_device` only if you can find a stable identifier (Z-Wave JS controller device exists; ZHA coordinator device exists; Z2M bridge device exists if HA discovery is on). Nice to have, not essential.

Entity IDs then become `sensor.zigbee2mqtt_backups_last_backup`, which is what people will type in templates. Add `suggested_object_id` only if HA's default is worse.

Reason to keep per-source: the problem sensor, the identity and the restore button are inherently per source, and a user with both Z2M and Z-Wave wants two cards. Switch `integration_type` to `"hub"` to match (a `service` type is expected to have one device).

### 4.2 Entity table with recommendations

| Entity | Keep? | Category | Icon | Notes |
|---|---|---|---|---|
| `sensor.last_backup` (timestamp) | Yes, primary | none | `mdi:content-save-check` | Attributes: `file`, `size`, identity fields. Drop `members`. Good. |
| `sensor.backup_age` (h) | Drop, or diagnostic + `enabled_default=False` | diagnostic | `mdi:clock-alert-outline` | Redundant with the timestamp (frontend shows relative time) and with the stale sensor. If kept, use `device_class=DURATION`, unit hours, and no `state_class=MEASUREMENT` (it would fill the recorder with a sawtooth). |
| `sensor.backup_count` | Yes | diagnostic | `mdi:archive-outline` | Rename key to `stored_backups`. Add `backups` attribute (list of `{file, created, devices}`) for the service workflow. `state_class=MEASUREMENT` is fine but unnecessary; drop it. |
| `sensor.identity` | Yes | diagnostic | `mdi:fingerprint` | Name "Coordinator identity" is good. Consider `entity_registry_enabled_default=True`; it is the thing people compare on failure day. |
| `sensor.last_error` | Make it an attribute | - | - | A text sensor whose normal state is "none" is noise in the recorder and on dashboards. Put `last_error` and `last_attempt` as attributes on `binary_sensor.backup_stale` (or on a new `binary_sensor.backup_failed`, device_class `problem`). The Repair already carries the message. |
| `binary_sensor.backup_stale` | Yes, primary | none | default problem icon | Keep. Attributes: `stale_after_hours`, `last_error`, `last_attempt`. |
| `binary_sensor.source_available` | Yes | diagnostic | default connectivity icon | Fine. Consider `entity_registry_enabled_default=False`; the upstream integration already tells you it is down. |
| `button.backup_now` | Yes | config | `mdi:content-save-plus` | Set `entity_category=CONFIG` so it is grouped with controls, not state. |
| **new** `sensor.next_backup` (timestamp) | Add | diagnostic | `mdi:calendar-clock` | Compute from `async_track_time_change` schedule: next occurrence of HH:MM local. Answers "is it actually scheduled?" and is what the dashboard card wants. |
| **new** `button.restore_latest` | Maybe | config | `mdi:backup-restore` | Only if it opens the Repair flow (raise the fixable issue and point the user to Repairs via notification). Do not make a button that restores directly. |
| **new** `binary_sensor.backup_running` | Optional | diagnostic | `mdi:progress-upload` | `running` flag already exists. Useful for Z-Wave where a backup can take a minute. |
| **new** `sensor.backup_schedule` (text "Daily at 03:17") | No | - | - | `next_backup` plus the Options page covers it. |

Mark `last_backup` as `_attr_entity_registry_visible_default=True` and the rest as diagnostic so the device page shows the one number that matters at the top.

### 4.3 Naming nits

- `strings.json` `entity.sensor.backup_count.name` is "Stored backups" but the key is `backup_count`; align the key with the name (`stored_backups`) before release because changing a translation key later changes nothing visible but changing `unique_id` suffixes orphans entities. (Unique id uses the key; pick final keys now.)
- "Source available" → "Integration loaded" is what it actually measures (`"zha" in hass.config.components`). It does not know whether the radio is responding. Either rename or make it mean more (ZHA: `app.state` populated; Z-Wave: `driver.controller` exists; Z2M: `bridge/state` retained `online`).
- "Back up now" (verb) is correct for the button; "Backup now" is what most integrations wrongly use. Keep yours.

---

## 5. Heartbeat and watchdog

### 5.1 Heartbeat payload

Good: JSON, includes `ts`, `uptime_s`, per-source availability. Two suggestions:

- Publish with `retain=True` and an MQTT **Last Will** on a sibling topic (`radio_guard/status` = `online`/`offline`). HA's MQTT integration already sets a birth/will on `homeassistant/status`; a watchdog that listens for `offline` from the broker gets a faster, cleaner signal than "no heartbeat for 5 minutes". Document that `homeassistant/status` is an acceptable alternative heartbeat source for people who do not want Radio Guard's topic.
- The heartbeat says nothing about whether HA is *healthy*; a hung recorder with a live event loop still publishes. Consider gating the heartbeat on a cheap health check (e.g. `hass.states.get("sun.sun")` updated within the last hour, or recorder queue below a threshold). Not required for v1; document the limitation.

### 5.2 ESPHome reference: correctness and safety

Clear and well commented. Issues:

1. **`millis()` overflow.** `uint32_t` millis wraps every 49.7 days. `now - id(last_heartbeat)` is fine under unsigned subtraction, but `id(last_cycle) != 0 && since_cycle < cooldown` and the `heartbeats_seen == 0` guard are not the problem; the problem is `last_heartbeat` set to `millis()` at boot (`on_boot`) when `millis()` may be 0-ish, combined with `heartbeats_seen == 0` guard, which is fine. Actually the real overflow risk: after a wrap, `(now - last_heartbeat)` is correct because both are `uint32_t`. OK. But `if (id(last_cycle) != 0 ...)`: after a wrap, `last_cycle` could legitimately be 0. Harmless. Verdict: safe, but add a comment that the arithmetic relies on unsigned wraparound so nobody "fixes" it with `int`.
2. **Boot hole.** After the ESP reboots (power blip, OTA), `heartbeats_seen == 0` disables the watchdog until the first heartbeat arrives. If HA is already hung at that moment, the watchdog never fires. Add a boot grace: if no heartbeat within `2 * timeout` after boot *and* MQTT is connected, treat as silent. The current behavior is the conservative choice and the comment "never seen one; don't guess" is honest; just document the trade-off in the README.
3. **Relay wiring.** The comment says NC so a dead ESP leaves HA powered, and the GPIO is `inverted: true` with `restore_mode: ALWAYS_ON`. During ESP boot the GPIO floats for ~100 ms; with an NC relay the coil is off and HA stays powered. Good. Add: "Use GPIO26/27/32/33; avoid strapping pins (GPIO0, 2, 12, 15) for the relay."
4. **`led_pin: GPIO2`** is a strapping pin; fine for an LED, note it.
5. **The `armed` switch defaults to ON** and is exposed to HA via `api:`. That means HA can disarm its own watchdog, and an automation bug (or the user clicking in a device page) can silently turn it off. Either drop `api:` (use MQTT-only control) or note that the switch is intentional and should be on a dashboard with a visible state.
6. **Cooldown of 3600 s with a single cycle**: good. Add a max-cycles-per-day counter (e.g. 3) after which it stops and leaves power on; a box that genuinely cannot boot should not be power-cycled every hour forever (SD card corruption).
7. **Off time 10 s** is short for a NUC PSU to fully discharge. Suggest 15 s and say why.
8. **Never switch mains with a bare module** is good; link to a specific safe option (a Shelly Plus 1 or a pre-built relay enclosure).

### 5.3 Shelly alternative: yes, include it

A large fraction of HA users already own a Shelly Plug S / Plus Plug / Plus 1 and will not build an ESP32 board. Shelly Gen2+ devices run mJS scripts and have an MQTT client. Add `shelly/watchdog.js` with the same semantics:

```javascript
// Radio Guard power watchdog for Shelly Gen2/Gen3 (Plus Plug, Plus 1, Pro 1).
// Settings -> Scripts -> Add, paste, enable "Run on startup".
// Enable MQTT on the Shelly and point it at the same broker as Home Assistant.
let CFG = {
  topic: "radio_guard/heartbeat",
  timeoutS: 300,
  cooldownS: 3600,
  offS: 15,
  maxCyclesPerDay: 3,
};
let lastBeat = Shelly.getUptimeMs ? Shelly.getUptimeMs() : Date.now();
let lastCycle = 0, seen = 0, cycles = 0, cyclesToday = 0, dayStart = Date.now();

function now() { return Date.now(); }

MQTT.subscribe(CFG.topic, function () { lastBeat = now(); seen++; });

Timer.set(10000, true, function () {
  if (!MQTT.isConnected()) return;          // broker down != HA down
  if (seen === 0) return;                    // never seen a heartbeat; don't guess
  if (now() - dayStart > 86400000) { cyclesToday = 0; dayStart = now(); }
  if (cyclesToday >= CFG.maxCyclesPerDay) return;
  let silent = (now() - lastBeat) / 1000;
  let since = (now() - lastCycle) / 1000;
  if (silent < CFG.timeoutS) return;
  if (lastCycle !== 0 && since < CFG.cooldownS) return;
  print("radio-guard: no heartbeat for", silent, "s; cycling power");
  lastCycle = now(); cycles++; cyclesToday++;
  Shelly.call("Switch.Set", { id: 0, on: false, toggle_after: CFG.offS });
  lastBeat = now();                          // give HA a full timeout to boot
});
```

Caveats to document: the Shelly's `Switch.Set` `toggle_after` restores power even if the script dies mid-cycle, which is the safe failure mode; set the Shelly's "default power-on state" to ON; the Shelly must be on Wi-Fi that does not depend on the HA box (people run HA on the same host as their UniFi controller, and that is fine, but if the AP needs HA, nothing works). Also: do not put the broker on the HA box if you can avoid it; if you must, say clearly that a hung HA box takes the broker with it, MQTT disconnects, and the watchdog (correctly) does nothing. That is a real limitation worth a FAQ entry. Offer the Shelly's own "no ping to HA for N minutes" option (Shelly Gen2 has a built-in "Ping watchdog"? No; but the script can `Shelly.call("HTTP.GET", {url: "http://ha:8123/api/"}, ...)` as a fallback) for people whose broker is on the HA host.

---

## 6. README structure for a first-time visitor

### 6.1 What is wrong today

- The first 12 lines are good copy but there is no "what do I do" until line 13, and nothing visual.
- "The recovery plan this is built for" is the author's philosophy; valuable, but it interrupts the task flow between Restore and Services. Move it to the end or a separate `docs/philosophy.md`.
- Restore section is three YAML blocks and three dense paragraphs; no checklist, no "what you will see".
- No mention that the user must re-point the upstream integration to the new hardware first.
- No screenshots. HACS renders the README as the store page; a screenshot of the device page and the Repair is worth more than any paragraph.
- No "what this does NOT do". People will assume it restores devices/entities, scenes, or works for Thread/Matter.
- `sensor.*_last_error` etc. are listed with wildcards; show one real entity id.
- Says sidecar is `.meta.json` in one place and `.json` in `storage.py`'s docstring; pick one.

### 6.2 Proposed outline

```
# Radio Guard
[badges: HACS custom repo / default, release, hassfest, HACS action]
One sentence. One screenshot (device page with green "Backup stale: off").

## Quick start (5 lines)
1. Install via HACS [badge]. Restart.
2. Add integration [badge]. It pre-selects what you have. Submit.
3. Within a minute you get a notification with your first backup's path.
4. Backups run nightly at 03:17 into /config/radio_guard/ and ride along in HA's backups.
5. Make sure HA backups leave the box (NAS, Google Drive, Nabu Casa).

## What it backs up (the table, trimmed to Source / Format / Where)

## When your coordinator dies
   [one screenshot of the Repair]
   <details> Z-Wave JS checklist </details>
   <details> ZHA checklist </details>
   <details> Zigbee2MQTT checklist </details>
   Power users: the restore service (dry run / restore / force) with one example.

## What this does NOT do
   - Does not back up your devices, entities, automations or scenes (HA backup does that)
   - Does not back up Thread/Matter/OTBR datasets (yet)
   - Does not copy backups off the box; that is HA backup's job
   - Does not restore Z2M's configuration.yaml (only coordinator + database)
   - Does not protect against a backup taken after the stick started failing; see "Which backup should I pick?"
   - Cannot restore across region firmware (US stick -> EU stick)

## Entities
   (per device, real ids, one line each)

## Watchdog (optional)
   Heartbeat topic + payload. ESPHome reference. Shelly script. Limitations.

## Options
   Table of settings with defaults.

## Automations
   Blueprint badge. Events. Dashboard card.

## FAQ
   - Where are my files? Can I copy them out manually? (yes, they are plain OCBF JSON / Z2M zip / NVM)
   - Can I restore to a different brand of stick? (Zigbee: yes via zigpy/Z2M; Z-Wave: same or newer series)
   - I run Z2M in Docker. (staged files; copy them)
   - ZHA's migrate wizard already offers backups. Why this? (ZHA's are in .storage, rotate, and are ZHA-only; this also covers Z2M and Z-Wave and lands in /config)
   - The watchdog cycled power while I was updating. (disarm switch / cooldown)
   - Backup says "stale" right after install. (fixed by first-backup; otherwise press Back up now)

## Why this exists / the recovery plan (moved from the middle)

## Contributing, License
```

### 6.3 Screenshot placeholders to add now

- `docs/img/device-page.png`: one source device with Last backup, Stale off, Back up now.
- `docs/img/repair-restore.png`: the pre-flight step of the Repair flow.
- `docs/img/notification-first-backup.png`.
- `docs/img/hacs-store.png` optional.

Reference them with absolute raw URLs (`https://raw.githubusercontent.com/bbensten/radio-guard/main/docs/img/...`) so they render inside HACS, which does not resolve relative paths.

---

## 7. Translations: strings that will show raw keys

`strings.json` and `translations/en.json` are identical (good; keep them in sync with a CI check, `diff -q` in the validate workflow). What is missing:

1. **Service `source` select options** in `services.yaml` use literal `label:` values. That works but does not translate. Move to `translation_key: source` on the selector and add:

   ```json
   "selector": {
     "source": {
       "options": {
         "zigbee2mqtt": "Zigbee2MQTT",
         "zha": "ZHA",
         "zwave_js": "Z-Wave JS"
       }
     }
   }
   ```

2. **Config flow `sources` field options** use `SOURCE_NAMES` labels from Python; fine (they are proper nouns), but if you adopt `translation_key="source"` on the `SelectSelector` the same `selector.source.options` block covers it.

3. **`options.step.init.description`** is missing; the Options dialog will show no description (not a raw key, but a blank). Add one line.

4. **Section titles** once you adopt `section()`: each needs `options.step.init.sections.<name>.name` and optionally `.description` or you get the raw section key (`schedule`, `storage`, `heartbeat`) as the header.

5. **Repairs**: every new `issues.<id>.fix_flow.step.<step>` title/description/data key and every `abort`/`error` reason. The skeleton in section 3.2 lists them. Missing any one shows the raw key in the dialog.

6. **Exceptions**: `HomeAssistantError("Radio Guard is not set up")`, `f"{source} is not enabled in Radio Guard"`, `"Backup failed: ..."`, `"Restore failed: ..."`, `"Restore pre-flight failed: ..."` and the Z2M staging `RuntimeError` are raw English strings. HA will display them as-is (not keys), but to be translatable use `ServiceValidationError(translation_domain=DOMAIN, translation_key="source_not_enabled", translation_placeholders={"source": ...})` and add an `"exceptions"` block:

   ```json
   "exceptions": {
     "not_set_up": { "message": "Radio Guard is not set up." },
     "source_not_enabled": { "message": "{source} is not enabled in Radio Guard. Add it under Configure." },
     "backup_failed": { "message": "{source} backup failed: {error}" },
     "restore_blocked": { "message": "Restore pre-flight found a problem: {reasons}. Use force to override." },
     "restore_failed": { "message": "{source} restore failed: {error}" },
     "no_backup": { "message": "No {source} backup matching {file}." },
     "z2m_staged": { "message": "Zigbee2MQTT is not a Supervisor add-on. Files staged in {path}; stop Z2M, copy coordinator_backup.json and database.db into its data directory, start Z2M." }
   }
   ```

7. **Entity `state` translations**: `LastErrorSensor` returns `"none"` as a state string. If you keep the sensor, make it `None` (unknown) when there is no error; "none" will show literally and cannot be translated. (Recommendation above is to drop the sensor.)

8. **Issue placeholders**: `backup_stale` description uses `{age}` which is produced as `"never"` or `"36 h"` in Python; `"never"` is untranslatable. Use two issue translation keys (`backup_stale` / `backup_never`) or pass a numeric placeholder and let the string say "{age} hours".

9. **Device names** `f"Radio Guard {source.name}"` are not translatable; acceptable since they contain product names, but if you adopt "Zigbee2MQTT backups" the word "backups" should come from `strings.json` (`entity` names handle this; device names do not, so keep device names as proper nouns only: `Zigbee2MQTT`, `ZHA`, `Z-Wave JS` with manufacturer `Radio Guard`).

10. Not raw keys but worth fixing: `config.step.user.title` is "Radio Guard", which duplicates the dialog header. Use the question ("Which coordinators should be protected?") as the title.

---

## Appendix: suggested `strings.json` for the slimmed config flow

```json
{
  "config": {
    "step": {
      "user": {
        "title": "Which coordinators should Radio Guard protect?",
        "description": "Detected: {detected}. Backups run nightly and are stored under /config/{backup_dir}/ so they are included in Home Assistant's own backups. Schedule, retention and the watchdog heartbeat can be changed later under Configure.",
        "data": {
          "sources": "Coordinators",
          "first_backup": "Take a first backup right away"
        },
        "data_description": {
          "sources": "You can add or remove coordinators later."
        }
      },
      "z2m": {
        "title": "Zigbee2MQTT",
        "description": "Radio Guard found Zigbee2MQTT publishing on {detected_topic}.",
        "data": { "z2m_base_topic": "Base topic" }
      }
    },
    "error": {
      "no_sources": "Select at least one coordinator.",
      "z2m_not_found": "Nothing is publishing on {topic}/bridge/info. Check the base topic in Zigbee2MQTT's settings."
    },
    "abort": {
      "already_configured": "Radio Guard is already set up. Use Configure to change it.",
      "single_instance_allowed": "Only one Radio Guard instance is needed."
    }
  },
  "options": {
    "step": {
      "init": {
        "title": "Radio Guard",
        "description": "Changes apply after a reload, which happens automatically.",
        "data": { "sources": "Coordinators" },
        "sections": {
          "schedule": {
            "name": "Schedule and retention",
            "data": {
              "backup_time": "Daily backup time",
              "retention": "Backups to keep per coordinator",
              "stale_hours": "Warn when newest backup is older than",
              "max_restore_age_days": "Refuse to restore backups older than (days)"
            }
          },
          "storage": {
            "name": "Storage",
            "data": { "backup_dir": "Folder under /config" }
          },
          "heartbeat": {
            "name": "Watchdog heartbeat (MQTT)",
            "description": "Publishes a liveness message an external power watchdog can monitor. Requires the MQTT integration.",
            "data": {
              "heartbeat_enabled": "Publish heartbeat",
              "heartbeat_topic": "Topic",
              "heartbeat_interval": "Interval"
            }
          }
        }
      }
    },
    "error": { "no_sources": "Select at least one coordinator." }
  }
}
```

End of review.
