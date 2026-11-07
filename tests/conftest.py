"""Shared fixtures for Radio Guard tests."""

from __future__ import annotations

import base64
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
import yaml
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.radio_guard.const import (
    CONF_BACKUP_DIR,
    CONF_BACKUP_HOUR,
    CONF_BACKUP_MINUTE,
    CONF_FIRST_BACKUP,
    CONF_HEARTBEAT_ENABLED,
    CONF_HEARTBEAT_INTERVAL,
    CONF_HEARTBEAT_TOPIC,
    CONF_RETENTION,
    CONF_SOURCES,
    CONF_STALE_HOURS,
    CONF_Z2M_BASE_TOPIC,
    DOMAIN,
    SOURCE_Z2M,
    SOURCE_ZHA,
    SOURCE_ZWAVE,
)
from homeassistant.core import HomeAssistant

pytest_plugins = "pytest_homeassistant_custom_component"

# --------------------------------------------------------------------- basics


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let the loader find custom_components/radio_guard."""
    return


@pytest.fixture(autouse=True)
def config_dir(hass: HomeAssistant, tmp_path: Path) -> Path:
    """Point the HA config dir at a per-test temp dir.

    The plugin's ``hass`` fixture uses the package's shared ``testing_config``
    directory, so anything written under ``hass.config.path()`` would leak
    between tests. Redirect it to ``tmp_path``.
    """
    cfg = tmp_path / "config"
    cfg.mkdir()
    hass.config.config_dir = str(cfg)
    return cfg


DEFAULT_ENTRY_DATA: dict[str, Any] = {
    CONF_SOURCES: [SOURCE_Z2M, SOURCE_ZHA, SOURCE_ZWAVE],
    CONF_Z2M_BASE_TOPIC: "zigbee2mqtt",
    CONF_BACKUP_HOUR: 3,
    CONF_BACKUP_MINUTE: 17,
    CONF_RETENTION: 3,
    CONF_STALE_HOURS: 36,
    CONF_BACKUP_DIR: "radio_guard",
    CONF_HEARTBEAT_ENABLED: False,
    CONF_HEARTBEAT_TOPIC: "radio_guard/heartbeat",
    CONF_HEARTBEAT_INTERVAL: 30,
    CONF_FIRST_BACKUP: False,
}


@pytest.fixture
def entry_data() -> dict[str, Any]:
    return dict(DEFAULT_ENTRY_DATA)


@pytest.fixture
def mock_config_entry(entry_data: dict[str, Any]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Radio Guard",
        unique_id=DOMAIN,
        data=entry_data,
        options={},
        entry_id="rg_test_entry",
    )


# ----------------------------------------------------------- Z2M test data

OCBF_SAMPLE: dict[str, Any] = {
    "metadata": {
        "version": 1,
        "format": "zigpy/open-coordinator-backup",
        "source": "zigbee2mqtt@1.42.0",
        "internal": {},
    },
    "coordinator_ieee": "00124b0024c2f0a1",
    "pan_id": "1a62",
    "extended_pan_id": "dddddddddddddddd",
    "nwk_update_id": 0,
    "security_level": 5,
    "channel": 11,
    "channel_mask": [11],
    "network_key": {"key": "01030507090b0d0f00020406080a0c0d", "sequence_number": 0, "frame_counter": 1234},
    "devices": [
        {"nwk_address": "1234", "ieee_address": "00158d0001aabbcc", "is_child": True},
        {"nwk_address": "5678", "ieee_address": "00158d0001ddeeff", "is_child": False},
    ],
    "stack_specific": {"zstack": {"tclk_seed": "aabbccdd"}},
}

# A Z2M configuration.yaml whose advanced.* already matches OCBF_SAMPLE.
Z2M_CONFIG_SAMPLE: dict[str, Any] = {
    "homeassistant": True,
    "mqtt": {"server": "mqtt://core-mosquitto", "base_topic": "zigbee2mqtt"},
    "serial": {"port": "/dev/ttyUSB0", "adapter": "zstack"},
    "advanced": {
        "pan_id": 0x1A62,
        "ext_pan_id": [0xDD] * 8,
        "network_key": [1, 3, 5, 7, 9, 11, 13, 15, 0, 2, 4, 6, 8, 10, 12, 13],
        "channel": 11,
    },
    "devices": {},
}


def make_z2m_zip(
    ocbf: dict[str, Any] | None = OCBF_SAMPLE,
    config: dict[str, Any] | None = Z2M_CONFIG_SAMPLE,
    extra: dict[str, bytes] | None = None,
) -> bytes:
    """Build a Zigbee2MQTT-style backup zip (data dir snapshot)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if ocbf is not None:
            zf.writestr("coordinator_backup.json", json.dumps(ocbf))
        zf.writestr("database.db", b'{"id":1,"type":"Coordinator"}\n')
        if config is not None:
            zf.writestr("configuration.yaml", yaml.safe_dump(config, sort_keys=False))
        zf.writestr("state.json", b"{}")
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return buf.getvalue()


def z2m_zip_b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


BRIDGE_INFO_SAMPLE: dict[str, Any] = {
    "version": "1.42.0",
    "zigbee_herdsman_version": "2.1.3",
    "zigbee_herdsman": "2.1.3",
    "coordinator": {
        "ieee_address": "0x00124b0024c2f0a1",
        "type": "zStack3x0",
        "meta": {"revision": 20240710, "maintrel": 1},
    },
    "network": {"pan_id": 6754, "extended_pan_id": "0xdddddddddddddddd", "channel": 11},
}
