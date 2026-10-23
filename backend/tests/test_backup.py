"""OPS-01 tests use temporary SQLite data only, without network or credentials."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
from threading import Event
import time

import pytest

from deployment.cloud import backup


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def rig(tmp_path):
    source = tmp_path / "watchlist.sqlite3"
    with closing(sqlite3.connect(source)) as db, db:
        db.executescript("""
            PRAGMA foreign_keys=ON;
            PRAGMA user_version=10;
            CREATE TABLE parents (id INTEGER PRIMARY KEY, amount INTEGER NOT NULL);
            CREATE TABLE children (id INTEGER PRIMARY KEY REFERENCES parents(id), amount INTEGER NOT NULL);
            INSERT INTO parents VALUES (1, 100);
            INSERT INTO children VALUES (1, 100);
        """)
    release = tmp_path / "release.json"
    release.write_text(json.dumps({"release_id": "lab-20261022T010000Z", "app_version": "0.10.0",
                                   "source_sha256": "a" * 64, "built_at": "2026-10-22T01:00:00+00:00",
                                   "do_not_copy": "fixture-private-value"}), encoding="utf-8")
    return source, tmp_path / "managed-backups", release


def create(rig, **kwargs):
    source, root, release = rig
    return backup.create_backup(source, root, release, **kwargs)


def completed(root):
    return sorted(item.name for item in root.iterdir() if backup.ID_PATTERN.fullmatch(item.name))


def test_complete_snapshot_round_trip_and_source_preserved(rig):
    source, root, release = rig
    source_before = digest(source)
    receipt = create(rig)
    assert receipt["ok"] and receipt["verified"]
    assert digest(source) == source_before
    verified = backup.verify_backup(root, receipt["backup_id"])
    manifest = verified["manifest"]
    assert manifest["source"]["open_mode"] == "ro"
    assert manifest["source"]["release"]["app_version"] == "0.10.0"
    assert "fixture-private-value" not in json.dumps(manifest)
    assert manifest["validation"] == {"integrity_check": "ok", "foreign_key_violations": 0, "user_version": 10}
    for field in ("created_at", "completed_at"):
        utc = datetime.fromisoformat(manifest[field + "_utc"])
        china = datetime.fromisoformat(manifest[field + "_china"])
        assert utc.utcoffset() == timedelta(0)
        assert china.utcoffset() == timedelta(hours=8) and utc == china
    directory = Path(receipt["directory"])
    assert {item.name for item in directory.iterdir()} == {"watchlist.sqlite3", "manifest.json"}
    with closing(sqlite3.connect((directory / "watchlist.sqlite3").as_uri() + "?mode=ro", uri=True)) as db:
        assert db.execute("SELECT * FROM children").fetchall() == [(1, 100)]
    assert [item["backup_id"] for item in backup.list_backups(root)["backups"]] == [receipt["backup_id"]]
    if os.name == "posix":
        for folder in (root, directory):
            assert stat.S_IMODE(folder.stat().st_mode) == 0o700
        for item in (root / backup.OWNER_FILE, root / backup.LOCK_FILE, *directory.iterdir()):
            assert stat.S_IMODE(item.stat().st_mode) == 0o600


def test_missing_source_fails_without_creating_a_database_or_root(rig):
    source, root, _ = rig
    source.unlink()
    with pytest.raises(FileNotFoundError):
        create(rig)
    assert not source.exists() and not root.exists()


@pytest.mark.parametrize("invalid", ["not-json", '{"release_id":"lab-test"}',
    '{"release_id":"lab-test","app_version":"0.10.0","built_at":{"secret":"not-a-date"}}'])
def test_bad_release_metadata_never_becomes_a_completed_backup(rig, invalid):
    _, root, release = rig
    release.write_text(invalid, encoding="utf-8")
    with pytest.raises(backup.BackupError):
        create(rig)
    assert not root.exists()


def test_cannot_adopt_unmarked_nonempty_directory(rig):
    _, root, _ = rig
    root.mkdir(mode=0o700)
    preserved = root / "existing-deployment-backup.sqlite3"
    preserved.write_bytes(b"must remain unchanged")
    with pytest.raises(backup.BackupError, match="unmarked or nonempty"):
        create(rig)
    assert preserved.read_bytes() == b"must remain unchanged"
    assert sorted(item.name for item in root.iterdir()) == [preserved.name]


def test_partial_copy_failure_returns_failure_and_never_publishes(rig, monkeypatch, capsys):
    source, root, release = rig
    original_copy = backup._online_copy
    source_before = digest(source)

    def fail_after_copy(*args):
        original_copy(*args)
        raise OSError("controlled destination failure")

    monkeypatch.setattr(backup, "_online_copy", fail_after_copy)
    assert backup.main(["create", "--source", str(source), "--root", str(root),
                        "--release-file", str(release)]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert completed(root) == []
    assert not any(item.name.startswith(".pending-") for item in root.iterdir())
    assert digest(source) == source_before


def test_release_change_during_copy_is_not_misattributed(rig, monkeypatch):
    _, root, release = rig
    original_copy = backup._online_copy

    def change_release(*args):
        original_copy(*args)
        value = json.loads(release.read_text(encoding="utf-8"))
        value["release_id"] = "lab-new-release"
        release.write_text(json.dumps(value), encoding="utf-8")

    monkeypatch.setattr(backup, "_online_copy", change_release)
    with pytest.raises(backup.BackupError, match="release changed"):
        create(rig)
    assert completed(root) == []


@pytest.mark.parametrize("corruption", ["file", "foreign_key"])
def test_invalid_source_is_never_published_or_repaired(rig, corruption):
    source, root, _ = rig
    if corruption == "file":
        source.write_bytes(b"not a SQLite database")
    else:
        with closing(sqlite3.connect(source)) as db, db:
            db.execute("INSERT INTO children VALUES (999, 100)")
    source_before = digest(source)
    with pytest.raises((backup.BackupError, sqlite3.Error)):
        create(rig)
    assert completed(root) == []
    assert digest(source) == source_before


def test_retention_keeps_seven_valid_owned_backups_and_preserves_other_files(rig):
    source, root, _ = rig
    deployment = source.parent / "backups"
    deployment.mkdir()
    old_backup = deployment / "existing.sqlite3"
    old_backup.write_bytes(b"existing deployment backup")
    receipts = [create(rig)]
    unknown = root / "operator-notes.txt"
    unknown.write_text("leave this alone", encoding="utf-8")
    pending = root / ".pending-old-interrupted-run"
    pending.mkdir(mode=0o700)
    (pending / "operator-file").write_bytes(b"unknown pending data")
    receipts += [create(rig) for _ in range(8)]
    assert completed(root) == sorted(item["backup_id"] for item in receipts[-7:])
    assert receipts[-1]["retention"]["deleted"] == [receipts[1]["backup_id"]]
    assert unknown.read_text(encoding="utf-8") == "leave this alone"
    assert (pending / "operator-file").read_bytes() == b"unknown pending data"
    assert old_backup.read_bytes() == b"existing deployment backup"
    assert {item["name"] for item in receipts[-1]["retention"]["ignored"]} == {unknown.name, pending.name}


@pytest.mark.parametrize("mutation", ["corrupt_db", "extra_file", "foreign_owner", "hardlink"])
def test_unverifiable_old_backups_are_never_deleted(rig, mutation):
    source, root, _ = rig
    first = create(rig, keep=1)
    directory = Path(first["directory"])
    artifact = directory / "watchlist.sqlite3"
    if mutation == "corrupt_db":
        with artifact.open("ab") as handle:
            handle.write(b"tampered")
    elif mutation == "extra_file":
        (directory / "operator-file.txt").write_bytes(b"preserve")
    elif mutation == "foreign_owner":
        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["owner_id"] = "f" * 32
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    else:
        outside = source.parent / "external.sqlite3"
        shutil.copyfile(artifact, outside)
        artifact.unlink()
        try:
            os.link(outside, artifact)
        except OSError:
            if os.name != "nt":
                raise
            pytest.skip("This Windows token cannot create hardlinks; run this case on Linux")
    with pytest.raises(backup.BackupError):
        backup.verify_backup(root, first["backup_id"])
    last = create(rig, keep=1)
    assert directory.exists() and artifact.exists()
    assert last["retention"]["deleted"] == []
    assert first["backup_id"] in {item["name"] for item in last["retention"]["ignored"]}


def test_backup_lock_blocks_a_second_writer_without_mutation(rig):
    _, root, _ = rig
    first = create(rig)
    with backup._exclusive(root):
        with pytest.raises(backup.BackupError, match="Another backup command"):
            create(rig)
    assert completed(root) == [first["backup_id"]]


def test_locked_source_times_out_without_false_success(rig):
    source, root, _ = rig
    with closing(sqlite3.connect(source)) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        with pytest.raises(backup.BackupError, match="time limit"):
            create(rig, timeout=1)
        assert time.monotonic() - started < 4
        writer.rollback()
    assert completed(root) == []


def test_wal_writer_can_commit_during_incremental_online_backup(rig, monkeypatch):
    source, root, _ = rig
    real_connect = sqlite3.connect
    request_commit, committed = Event(), Event()
    with closing(real_connect(source)) as db, db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        db.execute("CREATE TABLE padding (value BLOB)")
        db.executemany("INSERT INTO padding VALUES (?)", [(b"x" * 4096,)] * 300)

    class ObservedSource(sqlite3.Connection):
        def backup(self, target, *, progress, **kwargs):
            checked = False

            def observe(status, remaining, total):
                nonlocal checked
                progress(status, remaining, total)
                if not checked and remaining > 0:
                    checked = True
                    request_commit.set()
                    assert committed.wait(3), "writer could not commit between backup steps"

            return super().backup(target, progress=observe, **kwargs)

    def connect(path, *args, **kwargs):
        if str(path).endswith("?mode=ro"):
            kwargs["factory"] = ObservedSource
        return real_connect(path, *args, **kwargs)

    def writer():
        with closing(real_connect(source, timeout=2)) as db:
            assert request_commit.wait(3)
            with db:
                db.execute("INSERT INTO parents VALUES (2, 200)")
                db.execute("INSERT INTO children VALUES (2, 200)")
            committed.set()

    monkeypatch.setattr(backup.sqlite3, "connect", connect)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(writer)
        receipt = create(rig)
        future.result(timeout=4)
    assert committed.is_set()
    destination = Path(receipt["directory"]) / "watchlist.sqlite3"
    with closing(real_connect(destination.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        assert db.execute("SELECT * FROM parents ORDER BY id").fetchall() == db.execute("SELECT * FROM children ORDER BY id").fetchall()
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert {item.name for item in destination.parent.iterdir()} == {"watchlist.sqlite3", "manifest.json"}


def test_rejects_traversal_and_backup_symlink_without_following_it(rig):
    source, root, _ = rig
    first = create(rig)
    with pytest.raises(backup.BackupError):
        backup.verify_backup(root, "../watchlist.sqlite3")
    outside = source.parent / "outside"
    outside.mkdir(mode=0o700)
    (outside / "preserve.txt").write_bytes(b"must survive")
    linked = root / ("backup-20260101T010101000000Z-" + "a" * 32)
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("This Windows token cannot create symlinks; run this case on Linux")
    next_receipt = create(rig, keep=1)
    assert linked.is_symlink()
    assert (outside / "preserve.txt").read_bytes() == b"must survive"
    assert first["backup_id"] in next_receipt["retention"]["deleted"]
    assert linked.name in {item["name"] for item in next_receipt["retention"]["ignored"]}


@pytest.mark.skipif(os.name != "posix", reason="Linux mode and ownership checks require POSIX")
@pytest.mark.parametrize("target", ["root", "owner", "manifest", "database"])
def test_nonprivate_owned_files_fail_verification(rig, target):
    _, root, _ = rig
    first = create(rig)
    directory = Path(first["directory"])
    targets = {"root": root, "owner": root / backup.OWNER_FILE,
               "manifest": directory / "manifest.json", "database": directory / "watchlist.sqlite3"}
    chosen = targets[target]
    chosen.chmod(0o755 if target == "root" else 0o644)
    with pytest.raises(backup.BackupError, match="not private"):
        backup.verify_backup(root, first["backup_id"])
    assert chosen.exists()
