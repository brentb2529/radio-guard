"""Unit tests for custom_components.radio_guard.storage."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from custom_components.radio_guard.storage import META_SUFFIX, BackupRecord, BackupStore


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "radio_guard"


@pytest.fixture
def store(root: Path) -> BackupStore:
    return BackupStore(root, "zha", retention=3)


def _write_at(store: BackupStore, freezer, when: datetime, data: bytes, ext: str = ".json", **meta) -> BackupRecord:
    freezer.move_to(when)
    return store.write(data, ext, meta)


def test_write_creates_file_and_sidecar(store: BackupStore, root: Path, freezer) -> None:
    freezer.move_to("2026-11-07 03:17:00+00:00")
    rec = store.write(b'{"a": 1}', ".json", {"ieee": "00:11", "device_count": 2})

    assert rec.source == "zha"
    assert rec.path == root / "zha" / "20261107T031700Z.json"
    assert rec.path.read_bytes() == b'{"a": 1}'
    assert rec.size == 8
    assert rec.created == datetime(2026, 11, 7, 3, 17, tzinfo=timezone.utc)
    assert rec.created.tzinfo is not None
    assert rec.name == "20261107T031700Z.json"

    meta_path = root / "zha" / f"20261107T031700Z{META_SUFFIX}"
    assert meta_path.exists()
    meta = json.loads(meta_path.read_text())
    assert meta == {
        "ieee": "00:11",
        "device_count": 2,
        "source": "zha",
        "created": "2026-11-07T03:17:00+00:00",
        "size": 8,
        "file": "20261107T031700Z.json",
    }
    assert rec.meta == meta
    # No temp file left behind
    assert not list((root / "zha").glob("*.tmp"))


def test_write_tmp_file_atomicity(store: BackupStore, root: Path, freezer) -> None:
    """Data goes to <name>.tmp first, is fsynced, then os.replace'd into place."""
    freezer.move_to("2026-11-07 03:17:00+00:00")
    seen: dict[str, object] = {}

    real_replace = os.replace

    def spy_replace(src, dst, *a, **kw):
        seen["src"] = Path(src)
        seen["dst"] = Path(dst)
        seen["src_exists_before"] = Path(src).exists()
        seen["dst_exists_before"] = Path(dst).exists()
        return real_replace(src, dst, *a, **kw)

    with (
        patch("custom_components.radio_guard.storage.os.replace", side_effect=spy_replace),
        patch("custom_components.radio_guard.storage.os.fsync", wraps=os.fsync) as fsync,
    ):
        rec = store.write(b"xyz", ".nvm", {})

    assert seen["src"] == root / "zha" / "20261107T031700Z.nvm.tmp"
    assert seen["dst"] == rec.path
    assert seen["src_exists_before"] is True
    assert seen["dst_exists_before"] is False
    fsync.assert_called_once()
    assert not seen["src"].exists()


def test_write_failure_leaves_no_final_file(store: BackupStore, root: Path, freezer) -> None:
    freezer.move_to("2026-11-07 03:17:00+00:00")
    with (
        patch("custom_components.radio_guard.storage.os.fsync", side_effect=OSError("disk full")),
        pytest.raises(OSError),
    ):
        store.write(b"xyz", ".nvm", {})
    final = root / "zha" / "20261107T031700Z.nvm"
    assert not final.exists()
    # The temp file is skipped by list()
    assert store.list() == []


def test_list_empty_when_no_dir(store: BackupStore) -> None:
    assert store.list() == []
    assert store.latest() is None
    assert store.resolve(None) is None
    assert store.resolve("nope.json") is None


def test_list_newest_first_and_latest(store: BackupStore, freezer) -> None:
    t0 = datetime(2026, 11, 1, 3, 17, tzinfo=timezone.utc)
    r_old = _write_at(store, freezer, t0, b"old")
    r_new = _write_at(store, freezer, t0 + timedelta(days=1), b"new")
    r_mid = _write_at(store, freezer, t0 + timedelta(hours=12), b"mid")

    records = store.list()
    assert [r.path for r in records] == [r_new.path, r_mid.path, r_old.path]
    assert [r.created for r in records] == sorted((r.created for r in records), reverse=True)
    assert store.latest().path == r_new.path
    # meta sidecar files are not listed as backups
    assert all(not r.path.name.endswith(META_SUFFIX) for r in records)
    # metadata round-trips through the sidecar
    assert records[0].meta["created"] == r_new.created.isoformat()
    assert records[0].size == 3


def test_list_falls_back_to_mtime_without_sidecar(store: BackupStore, root: Path) -> None:
    d = root / "zha"
    d.mkdir(parents=True)
    f = d / "20260101T000000Z.json"
    f.write_bytes(b"{}")
    mtime = datetime(2026, 1, 2, tzinfo=timezone.utc).timestamp()
    os.utime(f, (mtime, mtime))

    [rec] = store.list()
    assert rec.meta == {}
    assert rec.created == datetime(2026, 1, 2, tzinfo=timezone.utc)
    assert rec.created.tzinfo is not None


def test_list_tolerates_corrupt_sidecar(store: BackupStore, root: Path) -> None:
    d = root / "zha"
    d.mkdir(parents=True)
    (d / "20260101T000000Z.json").write_bytes(b"{}")
    (d / f"20260101T000000Z{META_SUFFIX}").write_text("not json")
    (d / "20260102T000000Z.json").write_bytes(b"{}")
    (d / f"20260102T000000Z{META_SUFFIX}").write_text(json.dumps({"created": "garbage"}))

    records = store.list()
    assert len(records) == 2
    assert all(r.created.tzinfo is not None for r in records)


def test_list_skips_subdirectories_and_tmp(store: BackupStore, root: Path) -> None:
    d = root / "zha"
    (d / "restore").mkdir(parents=True)
    (d / "20260101T000000Z.json.tmp").write_bytes(b"partial")
    (d / "20260103T000000Z.json").write_bytes(b"{}")
    assert [r.name for r in store.list()] == ["20260103T000000Z.json"]


def test_resolve_by_name_path_and_none(store: BackupStore, freezer) -> None:
    t0 = datetime(2026, 11, 1, 3, 17, tzinfo=timezone.utc)
    a = _write_at(store, freezer, t0, b"a")
    b = _write_at(store, freezer, t0 + timedelta(days=1), b"b")

    assert store.resolve(None).path == b.path
    assert store.resolve("").path == b.path
    assert store.resolve(a.name).path == a.path
    assert store.resolve(str(a.path)).path == a.path
    # a bare filename from a different directory still resolves by name
    assert store.resolve(f"/somewhere/else/{a.name}").path == a.path
    assert store.resolve("20200101T000000Z.json") is None


def test_read_returns_bytes(store: BackupStore, freezer) -> None:
    freezer.move_to("2026-11-07 03:17:00+00:00")
    rec = store.write(b"\x00\x01\x02", ".nvm", {})
    assert store.read(rec) == b"\x00\x01\x02"


def test_rotate_respects_retention(store: BackupStore, root: Path, freezer) -> None:
    t0 = datetime(2026, 11, 1, 3, 17, tzinfo=timezone.utc)
    recs = [_write_at(store, freezer, t0 + timedelta(days=i), f"d{i}".encode()) for i in range(5)]

    remaining = store.list()
    assert len(remaining) == 3
    assert [r.path for r in remaining] == [recs[4].path, recs[3].path, recs[2].path]
    for old in recs[:2]:
        assert not old.path.exists()
        assert not (root / "zha" / f"{old.path.stem}{META_SUFFIX}").exists()
    for kept in recs[2:]:
        assert kept.path.exists()
        assert (root / "zha" / f"{kept.path.stem}{META_SUFFIX}").exists()


def test_rotate_keeps_newest_by_created_not_name(root: Path, freezer) -> None:
    """Rotation uses the sidecar's created timestamp, newest-first."""
    store = BackupStore(root, "zwave_js", retention=1)
    t0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
    older = _write_at(store, freezer, t0, b"older", ext=".nvm")
    newer = _write_at(store, freezer, t0 + timedelta(hours=1), b"newer", ext=".nvm")
    assert not older.path.exists()
    assert newer.path.exists()
    assert store.latest().path == newer.path


def test_rotate_survives_unlink_error(store: BackupStore, freezer, caplog) -> None:
    t0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
    for i in range(3):
        _write_at(store, freezer, t0 + timedelta(days=i), b"x")
    with patch.object(Path, "unlink", side_effect=OSError("busy")):
        rec = _write_at(store, freezer, t0 + timedelta(days=3), b"y")
    assert rec.path.exists()
    assert "Could not remove old backup" in caplog.text


def test_stores_are_isolated_per_source(root: Path, freezer) -> None:
    freezer.move_to("2026-11-07 03:17:00+00:00")
    zha = BackupStore(root, "zha", 5)
    zw = BackupStore(root, "zwave_js", 5)
    zha.write(b"1", ".json", {})
    zw.write(b"2", ".nvm", {})
    assert [r.source for r in zha.list()] == ["zha"]
    assert [r.source for r in zw.list()] == ["zwave_js"]
    assert zha.root == root / "zha"
    assert zw.root == root / "zwave_js"


def test_meta_path_for_multi_dot_names(store: BackupStore, root: Path) -> None:
    assert store._meta_path_for(root / "zha" / "20260101T000000Z.json") == root / "zha" / f"20260101T000000Z{META_SUFFIX}"
    assert store._meta_path_for(root / "zha" / "20260101T000000Z.tar.gz") == root / "zha" / f"20260101T000000Z{META_SUFFIX}"


def test_record_as_dict_serializable(store: BackupStore, freezer) -> None:
    freezer.move_to("2026-11-07 03:17:00+00:00")
    rec = store.write(b"abc", ".zip", {"members": ["a"]})
    d = rec.as_dict()
    assert d["path"] == str(rec.path)
    assert d["created"] == "2026-11-07T03:17:00+00:00"
    assert d["size"] == 3
    assert d["source"] == "zha"
    assert d["meta"]["members"] == ["a"]
    json.dumps(d)  # must be JSON serializable for service responses
