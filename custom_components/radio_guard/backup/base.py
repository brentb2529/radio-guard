"""Base classes for backup sources.

A source knows how to pull a coordinator backup from the software that owns
the radio (Zigbee2MQTT, ZHA, Z-Wave JS) and how to push one back. It never
talks to the chip directly, so every coordinator the upstream tool supports
is supported here, over serial or network alike.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant


class SourceUnavailable(Exception):
    """The upstream integration is not loaded or not reachable."""


@dataclass
class BackupPayload:
    """Raw backup bytes plus metadata worth keeping next to them."""

    data: bytes
    ext: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class RestoreCheck:
    """Result of a restore pre-flight."""

    ok: bool
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    current: dict[str, Any] = field(default_factory=dict)
    backup: dict[str, Any] = field(default_factory=dict)


class BackupSource(ABC):
    """One coordinator backup provider."""

    id: str
    name: str

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def async_setup(self) -> None:
        """Acquire long-lived resources (subscriptions). Idempotent. Optional."""
        return None

    async def async_teardown(self) -> None:
        """Release what async_setup acquired. Optional."""
        return None

    @abstractmethod
    def available(self) -> bool:
        """True if the upstream integration is loaded."""

    @abstractmethod
    async def async_identity(self) -> dict[str, Any]:
        """Return identifying facts about the live coordinator (IEEE, home ID...)."""

    @abstractmethod
    async def async_backup(self) -> BackupPayload:
        """Pull a backup."""

    @abstractmethod
    async def async_restore_check(self, data: bytes, meta: dict[str, Any]) -> RestoreCheck:
        """Pre-flight a restore without changing anything."""

    @abstractmethod
    async def async_restore(self, data: bytes, meta: dict[str, Any]) -> None:
        """Push a backup to the (new) coordinator."""
