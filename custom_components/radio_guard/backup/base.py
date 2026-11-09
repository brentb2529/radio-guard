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


class RestoreStaged(Exception):
    """The restore could not be completed automatically; files are staged for manual steps."""

    def __init__(self, path: str, steps: str) -> None:
        super().__init__(f"Restore files staged in {path}. {steps}")
        self.path = path
        self.steps = steps


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


class ConfirmationNeeded(Exception):
    """The restore can go on, but only after the user agrees to something irreversible."""

    def __init__(self, key: str, message: str) -> None:
        super().__init__(message)
        self.key = key  # set meta[key] = True and retry once the user confirms
        self.message = message


@dataclass
class SwapPlan:
    """How a guided coordinator replacement works for a source on this install."""

    supported: bool
    automatic: bool = False  # True: Radio Guard stops/starts the owning software itself
    choose_port: bool = False  # offer a port picker for the new coordinator
    current_port: str | None = None
    stop_first: bool = True  # stop the owning software before the hardware swap
    plug_in: str | None = None  # source-specific "plug in the new one" instructions (markdown)
    reason: str | None = None  # why not supported
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class PortOption:
    """A place the new coordinator can be: serial device or network address."""

    value: str
    label: str
    new: bool = False


@dataclass
class DeviceReport:
    """Which paired devices answered after a restore."""

    mains_total: int = 0
    mains_back: int = 0
    battery_total: int = 0
    battery_back: int = 0
    missing_mains: list[str] = field(default_factory=list)
    missing_battery: list[str] = field(default_factory=list)


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

    # ------------------------------------------------ guided replacement (optional)

    async def async_health(self) -> bool | None:
        """True if the coordinator is up, False if it is known to be down, None if unknown."""
        return None

    def swap_reset(self) -> None:
        """Forget per-swap state (e.g. which serial ports existed before the hardware swap)."""
        self._ports_before: set[str] | None = None

    async def async_swap_plan(self) -> SwapPlan:
        return SwapPlan(supported=False, reason=f"Guided replacement is not available for {self.name} yet.")

    tech: str | None = None  # "zigbee" / "zwave": which hardware the swap step should offer

    async def async_snapshot_ports(self) -> None:
        """Remember which devices exist before the swap, so the new one can be pre-selected."""
        if self.tech is None or getattr(self, "_ports_before", None) is not None:
            return
        from .. import hardware  # noqa: PLC0415

        self._ports_before = {c.value for c in await hardware.async_find(self.hass, self.tech, network=False)}

    async def async_swap_ports(self, current: str | None = None) -> list[PortOption]:
        """Candidates for the new coordinator, newly appeared first, labelled in plain words."""
        if self.tech is None:
            return []
        from .. import hardware  # noqa: PLC0415

        before = getattr(self, "_ports_before", None) or set()
        out = []
        for c in await hardware.async_find(self.hass, self.tech):
            new = c.value != current and (c.network or c.value not in before)
            label = c.label
            if c.network:
                label += " (found on your network)"
            elif c.tech is None:
                label += " (unrecognised device)"
            out.append(PortOption(c.value, label, new=new))
        # Newly plugged in first; among those, the same kind of stick on the same interface as the old
        # one (a two-in-one stick shows two new ports: the Zigbee and the Z-Wave side).
        out.sort(key=lambda o: (not o.new, -hardware.match_score(current, o.value), o.label))
        return out

    async def async_swap_stop(self, plan: SwapPlan) -> None:
        """Stop the owning software (automatic plans) and wait until it is down."""
        raise NotImplementedError

    async def async_wait_stopped(self, timeout: float) -> None:
        raise NotImplementedError

    async def async_swap_apply(self, data: bytes, meta: dict[str, Any], port: str | None) -> None:
        """Put the backup (and the new port) where the owning software will pick it up."""
        raise NotImplementedError

    async def async_swap_set_port(self, port: str | None) -> None:
        """Point the owning software at ``port`` (used to roll back to the old coordinator)."""
        raise NotImplementedError

    async def async_swap_start(self, plan: SwapPlan) -> None:
        raise NotImplementedError

    async def async_wait_started(self, timeout: float) -> None:
        raise NotImplementedError

    async def async_swap_verify(self, data: bytes, meta: dict[str, Any]) -> dict[str, Any]:
        """Confirm the new coordinator runs the backup's network; return its identity."""
        raise NotImplementedError

    async def async_check_devices(self, timeout: float) -> DeviceReport | None:
        """Ask paired devices to report in on the (new) coordinator. None if not supported."""
        return None
