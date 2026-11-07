"""Shared fixtures for Radio Guard tests."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Callable, Iterator
import io
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch
import zipfile

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.radio_guard.backup.base import (
    BackupPayload,
    BackupSource,
    RestoreCheck,
)
from custom_components.radio_guard.const import (
    CONF_BACKUP_DIR,
    CONF_BACKUP_HOUR,
    CONF_BACKUP_MINUTE,
    CONF_HEARTBEAT_ENABLED,
    CONF_HEARTBEAT_INTERVAL,
    CONF_HEARTBEAT_TOPIC,
    CONF_RETENTION,
    CONF_SOURCES,
    CONF_STALE_HOURS,
    CONF_Z2M_BASE_TOPIC,
    DOMAIN,
    SOURCE_NAMES,
    SOURCE_Z2M,
    SOURCE_ZHA,
    SOURCE_ZWAVE,
)
from homeassistant.config_entries import ConfigEntryState
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
    directory by default, so anything written under ``hass.config.path()`` would
    leak between tests. Redirect it to ``tmp_path``.
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
    CONF_HEARTBEAT_ENABLED: True,
    CONF_HEARTBEAT_TOPIC: "radio_guard/heartbeat",
    CONF_HEARTBEAT_INTERVAL: 30,
}


@pytest.fixture
def entry_data() -> dict[str, Any]:
    """Config entry data; override per test with indirect parametrization or a wrapper."""
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


# --------------------------------------------------------------- fake sources


class FakeSource(BackupSource):
    """In-memory BackupSource that records calls."""

    def __init__(self, hass: HomeAssistant, *args: Any, sid: str) -> None:
        super().__init__(hass)
        self.id = sid
        self.name = SOURCE_NAMES[sid]
        self.init_args = args
        self.is_available = True
        self.identity: dict[str, Any] = {"ieee": f"00:11:22:33:44:55:66:{len(sid):02d}"}
        self.identity_error: Exception | None = None
        self.payload = BackupPayload(
            data=f"payload-{sid}".encode(), ext=".bin", meta={"format": "fake", "device_count": 3}
        )
        self.backup_error: Exception | None = None
        self.backup_calls = 0
        self.check = RestoreCheck(ok=True, warnings=["be careful"], current={"ieee": "live"})
        self.check_calls: list[tuple[bytes, dict[str, Any]]] = []
        self.restore_calls: list[tuple[bytes, dict[str, Any]]] = []
        self.restore_error: Exception | None = None

    def available(self) -> bool:
        return self.is_available

    async def async_identity(self) -> dict[str, Any]:
        if self.identity_error:
            raise self.identity_error
        return dict(self.identity)

    async def async_backup(self) -> BackupPayload:
        self.backup_calls += 1
        if self.backup_error:
            raise self.backup_error
        return self.payload

    async def async_restore_check(self, data: bytes, meta: dict[str, Any]) -> RestoreCheck:
        self.check_calls.append((data, meta))
        return self.check

    async def async_restore(self, data: bytes, meta: dict[str, Any]) -> None:
        self.restore_calls.append((data, meta))
        if self.restore_error:
            raise self.restore_error


@pytest.fixture
def fake_sources() -> Iterator[dict[str, FakeSource]]:
    """Replace the three real sources with FakeSource instances.

    Yields a dict that is filled as the coordinator instantiates sources, so a
    test can reach the live instance via ``fake_sources["zha"]``.
    """
    created: dict[str, FakeSource] = {}

    def factory(sid: str) -> Callable[..., FakeSource]:
        def make(hass: HomeAssistant, *args: Any) -> FakeSource:
            src = FakeSource(hass, *args, sid=sid)
            created[sid] = src
            return src

        return make

    with (
        patch("custom_components.radio_guard.coordinator.Zigbee2MQTTSource", factory(SOURCE_Z2M)),
        patch("custom_components.radio_guard.coordinator.ZHASource", factory(SOURCE_ZHA)),
        patch("custom_components.radio_guard.coordinator.ZWaveJSSource", factory(SOURCE_ZWAVE)),
    ):
        yield created


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    fake_sources: dict[str, FakeSource],
) -> AsyncIterator[MockConfigEntry]:
    """Set up Radio Guard with fake sources and tear it down afterwards."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.LOADED
    yield mock_config_entry
    if mock_config_entry.state is ConfigEntryState.LOADED:
        await hass.config_entries.async_unload(mock_config_entry.entry_id)
        await hass.async_block_till_done()


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
}


def make_z2m_zip(ocbf: dict[str, Any] | None = OCBF_SAMPLE, extra: dict[str, bytes] | None = None) -> bytes:
    """Build a Zigbee2MQTT-style backup zip."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if ocbf is not None:
            zf.writestr("coordinator_backup.json", json.dumps(ocbf))
        zf.writestr("database.db", b'{"id":1,"type":"Coordinator"}\n')
        zf.writestr("configuration.yaml", b"homeassistant: true\n")
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return buf.getvalue()


def z2m_zip_b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


BRIDGE_INFO_SAMPLE: dict[str, Any] = {
    "version": "1.42.0",
    "zigbee_herdsman_version": "2.1.3",
    "coordinator": {
        "ieee_address": "0x00124b0024c2f0a1",
        "type": "zStack3x0",
        "meta": {"revision": 20240710, "maintrel": 1},
    },
    "network": {"pan_id": 6754, "extended_pan_id": "0xdddddddddddddddd", "channel": 11},
}
