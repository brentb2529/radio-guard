"""Constants for Radio Guard."""

from __future__ import annotations

DOMAIN = "radio_guard"

# Backup sources
SOURCE_Z2M = "zigbee2mqtt"
SOURCE_ZHA = "zha"
SOURCE_ZWAVE = "zwave_js"
ALL_SOURCES = (SOURCE_Z2M, SOURCE_ZHA, SOURCE_ZWAVE)

SOURCE_NAMES = {
    SOURCE_Z2M: "Zigbee2MQTT",
    SOURCE_ZHA: "ZHA",
    SOURCE_ZWAVE: "Z-Wave JS",
}

# Config / options keys
CONF_SOURCES = "sources"
CONF_Z2M_BASE_TOPIC = "z2m_base_topic"
CONF_BACKUP_HOUR = "backup_hour"
CONF_BACKUP_MINUTE = "backup_minute"
CONF_RETENTION = "retention"
CONF_STALE_HOURS = "stale_hours"
CONF_BACKUP_DIR = "backup_dir"
CONF_HEARTBEAT_ENABLED = "heartbeat_enabled"
CONF_HEARTBEAT_TOPIC = "heartbeat_topic"
CONF_HEARTBEAT_INTERVAL = "heartbeat_interval"

DEFAULT_Z2M_BASE_TOPIC = "zigbee2mqtt"
DEFAULT_BACKUP_HOUR = 3
DEFAULT_BACKUP_MINUTE = 17
DEFAULT_RETENTION = 14
DEFAULT_STALE_HOURS = 36
DEFAULT_BACKUP_DIR = "radio_guard"  # relative to /config
DEFAULT_HEARTBEAT_ENABLED = True
DEFAULT_HEARTBEAT_TOPIC = "radio_guard/heartbeat"
DEFAULT_HEARTBEAT_INTERVAL = 30  # seconds

# Services
SERVICE_BACKUP_NOW = "backup_now"
SERVICE_RESTORE = "restore"
ATTR_SOURCE = "source"
ATTR_FILE = "file"
ATTR_CONFIRM = "confirm"
ATTR_FORCE = "force"

# Repairs
ISSUE_BACKUP_FAILED = "backup_failed"
ISSUE_BACKUP_STALE = "backup_stale"

# Events
EVENT_BACKUP_COMPLETE = f"{DOMAIN}_backup_complete"
EVENT_RESTORE_COMPLETE = f"{DOMAIN}_restore_complete"

# Timeouts (seconds)
Z2M_BACKUP_TIMEOUT = 60
ZHA_BACKUP_TIMEOUT = 90
ZWAVE_BACKUP_TIMEOUT = 300
RESTORE_TIMEOUT = 600
