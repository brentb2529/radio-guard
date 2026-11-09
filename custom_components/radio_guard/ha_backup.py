"""Does Home Assistant's own backup carry Radio Guard's files off this machine?

Radio Guard stores coordinator backups under ``/config``, so they ride inside Home Assistant's
backups. That only helps on the day the machine itself dies if those backups (a) are made
automatically, (b) leave the machine (HA Cloud, Google Drive, a network share...) and (c) run
after Radio Guard's nightly backup, so they carry last night's coordinator state.
"""

from __future__ import annotations

import datetime as dt
import logging

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Agents that write to this machine's own disk: Core installs and the Supervisor's /backup folder.
# Network-storage mounts on HA OS are hassio.<mount name>, which do leave the machine.
LOCAL_AGENTS = {"backup.local", "hassio.local"}
HA_DEFAULT_TIME = dt.time(4, 45)  # homeassistant.components.backup.config.DEFAULT_BACKUP_TIME
MARGIN_MINUTES = 10  # a Z-Wave NVM backup can take a minute or two
MAX_GAP_MINUTES = 12 * 60  # beyond this the HA backup carries a coordinator backup most of a day old


def coverage_problems(hass: HomeAssistant, rg_hour: int, rg_minute: int) -> list[str] | None:
    """Plain-words problems with HA's backup setup; [] if fine, None if it can't be read."""
    try:
        from homeassistant.components.backup import async_get_manager  # noqa: PLC0415

        manager = async_get_manager(hass)
        cfg = manager.config.data
    except Exception:  # noqa: BLE001 - backup integration missing, not loaded yet, or changed shape
        _LOGGER.debug("Home Assistant backup settings not readable", exc_info=True)
        return None

    schedule = cfg.schedule
    recurrence = str(getattr(schedule.recurrence, "value", schedule.recurrence))
    if recurrence == "never" or (recurrence == "custom_days" and not schedule.days):
        return [
            "Home Assistant doesn't make automatic backups, so nothing copies your coordinator backups "
            "off this machine."
        ]

    problems: list[str] = []
    local = LOCAL_AGENTS | set(getattr(manager, "local_backup_agents", {}) or {})
    if not [a for a in cfg.create_backup.agent_ids if a not in local]:
        problems.append(
            "Home Assistant's automatic backups are only kept on this machine. If its disk dies, your "
            "coordinator backups die with it."
        )

    ha_time = schedule.time or HA_DEFAULT_TIME
    # Minutes from Radio Guard's backup to the next HA backup (both daily, wrapping at midnight).
    gap = (ha_time.hour * 60 + ha_time.minute - (rg_hour * 60 + rg_minute)) % (24 * 60)
    if gap < MARGIN_MINUTES or gap > MAX_GAP_MINUTES:
        problems.append(
            f"Home Assistant's backup runs at {ha_time:%H:%M}, too soon after or before Radio Guard's "
            f"coordinator backup at {rg_hour:02d}:{rg_minute:02d}, so the copy that leaves this machine can "
            "miss last night's coordinator backup. Set Radio Guard's time between "
            f"{MARGIN_MINUTES} minutes and {MAX_GAP_MINUTES // 60} hours before Home Assistant's "
            "(Radio Guard > Configure > Settings)."
        )
    return problems


_AGENT_NAMES = {
    "cloud": "Home Assistant Cloud",
    "google_drive": "Google Drive",
    "onedrive": "Microsoft OneDrive",
    "synology_dsm": "Synology NAS",
    "backblaze_b2": "Backblaze B2",
    "webdav": "WebDAV",
    "aws_s3": "Amazon S3",
    "azure_storage": "Azure Storage",
    "idrive_e2": "IDrive e2",
    "sftp_storage": "SFTP server",
}


def agent_label(agent_id: str) -> str:
    """'hassio.local' -> 'This machine', 'hassio.nas' -> 'Network storage “nas”', 'cloud.cloud' -> ..."""
    if agent_id in LOCAL_AGENTS:
        return "This machine"
    domain, _, name = agent_id.partition(".")
    if domain == "hassio":
        return f"Network storage “{name}”"
    return _AGENT_NAMES.get(domain, domain.replace("_", " ").title())


def backup_summary(hass: HomeAssistant) -> dict | None:
    """Plain facts about HA's automatic backups for the recovery sheet; None if unreadable."""
    try:
        from homeassistant.components.backup import async_get_manager  # noqa: PLC0415

        cfg = async_get_manager(hass).config.data
    except Exception:  # noqa: BLE001
        return None
    schedule = cfg.schedule
    recurrence = str(getattr(schedule.recurrence, "value", schedule.recurrence))
    at = f"{(schedule.time or HA_DEFAULT_TIME):%H:%M}"
    if recurrence == "daily":
        when = f"every day at {at}"
    elif recurrence == "custom_days" and schedule.days:
        days = ", ".join(str(getattr(d, "value", d)).capitalize() for d in schedule.days)
        when = f"{days} at {at}"
    else:
        when = None
    agents = list(cfg.create_backup.agent_ids)
    return {
        "when": when,
        "locations": [agent_label(a) for a in agents],
        "cloud": any(a.startswith("cloud.") for a in agents),
        "last": cfg.last_completed_automatic_backup,
        "encrypted": bool(getattr(cfg.create_backup, "password", None)),
    }
