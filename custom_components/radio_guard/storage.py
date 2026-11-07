"""On-disk backup storage for Radio Guard.

Layout (relative to the HA config directory):

    radio_guard/
      zigbee2mqtt/
        20261107T031700Z.zip
        20261107T031700Z.meta.json   <- sidecar metadata
      zha/
        20261107T031700Z.json        <- zigpy network backup
        20261107T031700Z.meta.json
      zwave_js/
        20261107T031700Z.nvm         <- raw NVM bytes
        20261107T031700Z.meta.json
        pre_restore_20261108T120000Z.nvm   <- snapshot of a target taken before a restore

Everything lives under /config so it is included in Home Assistant's own
backups automatically. All filesystem work runs in the executor.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

META_SUFFIX = ".meta.json"


@dataclass
class BackupRecord:
    """A stored backup plus its metadata."""

    source: str
    path: Path
    created: datetime
    size: int
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.path.name

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["path"] = str(self.path)
        d["created"] = self.created.isoformat()
        return d


class BackupStore:
    """Manages backup files for one source. Sync methods; call via executor."""

    def __init__(self, root: Path, source: str, retention: int) -> None:
        self.root = root / source
        self.source = source
        self.retention = retention

    # ---------------------------------------------------------------- write

    def write(self, data: bytes, ext: str, meta: dict[str, Any], label: str | None = None) -> BackupRecord:
        self.root.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        stem = now.strftime("%Y%m%dT%H%M%SZ")
        if label:
            stem = f"{label}_{stem}"
        path = self.root / f"{stem}{ext}"
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)  # contains network keys
        os.replace(tmp, path)

        meta = {
            **meta,
            "label": label,
            "source": self.source,
            "created": now.isoformat(),
            "size": len(data),
            "file": path.name,
        }
        meta_path = self.root / f"{stem}{META_SUFFIX}"
        meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))

        self._rotate()
        return BackupRecord(self.source, path, now, len(data), meta)

    def _rotate(self) -> None:
        records = self.list()
        nightly = [r for r in records if not r.meta.get("label")]
        labelled = [r for r in records if r.meta.get("label")]
        for old in nightly[self.retention :] + labelled[max(3, self.retention // 2) :]:
            try:
                old.path.unlink(missing_ok=True)
                self._meta_path_for(old.path).unlink(missing_ok=True)
                _LOGGER.debug("Rotated out %s", old.path)
            except OSError as err:
                _LOGGER.warning("Could not remove old backup %s: %s", old.path, err)

    # ----------------------------------------------------------------- read

    def _meta_path_for(self, path: Path) -> Path:
        stem = path.name.split(".", 1)[0]
        return self.root / f"{stem}{META_SUFFIX}"

    def list(self) -> list[BackupRecord]:
        """Newest first."""
        if not self.root.exists():
            return []
        records: list[BackupRecord] = []
        for path in self.root.iterdir():
            if not path.is_file() or path.name.endswith(META_SUFFIX) or path.suffix == ".tmp":
                continue
            meta: dict[str, Any] = {}
            mp = self._meta_path_for(path)
            if mp.exists():
                try:
                    meta = json.loads(mp.read_text())
                except (OSError, ValueError):
                    meta = {}
            created = None
            if meta.get("created"):
                try:
                    created = datetime.fromisoformat(meta["created"])
                except ValueError:
                    created = None
            if created is None:
                created = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            records.append(BackupRecord(self.source, path, created, path.stat().st_size, meta))
        records.sort(key=lambda r: r.created, reverse=True)
        return records

    def latest(self, *, include_labelled: bool = False) -> BackupRecord | None:
        """Newest regular backup; labelled snapshots (pre_restore_*) only if asked."""
        for rec in self.list():
            if include_labelled or not rec.meta.get("label"):
                return rec
        return None

    def resolve(self, name_or_path: str | None) -> BackupRecord | None:
        """Find a backup by bare filename, absolute path, or None for latest."""
        if not name_or_path:
            return self.latest()
        candidate = Path(name_or_path)
        for rec in self.list():
            if rec.path == candidate or rec.path.name == candidate.name:
                return rec
        return None

    def read(self, record: BackupRecord) -> bytes:
        return record.path.read_bytes()
