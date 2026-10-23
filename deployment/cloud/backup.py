"""Private, local SQLite online backups. Never restores or overwrites a source DB.

References: https://www.sqlite.org/backup.html and
https://docs.python.org/3.12/library/sqlite3.html#sqlite3.Connection.backup
"""

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import time
import uuid

TOOL = "alphaproject-lab-managed-backup"
FORMAT = 1
DEFAULT_SOURCE = Path("/var/lib/alphaproject-lab/watchlist.sqlite3")
DEFAULT_ROOT = DEFAULT_SOURCE.parent / "managed-backups"
DEFAULT_RELEASE = Path("/opt/alphaproject-lab/current/release.json")
CHINA_TIME = timezone(timedelta(hours=8))
OWNER_FILE = ".owner.json"
LOCK_FILE = ".backup.lock"
DB_FILE = "watchlist.sqlite3"
MANIFEST_FILE = "manifest.json"
ID_PATTERN = re.compile(r"backup-[0-9]{8}T[0-9]{12}Z-[a-f0-9]{32}")
STAMP = "%Y%m%dT%H%M%S%fZ"


class BackupError(RuntimeError):
    pass


def _utc_now():
    return datetime.now(timezone.utc)


def _absolute(path):
    if not isinstance(path, (str, os.PathLike)):
        raise BackupError("Path must be an absolute filesystem path")
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise BackupError("Use an absolute path without parent traversal")
    return path


def _safe_path(path, *, directory=False, private=False):
    """Reject links in every existing component, including Windows junctions."""
    path = _absolute(path)
    for part in reversed((path, *path.parents)):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise BackupError("Symlinks and reparse points are not accepted in data paths")
    info = path.lstat()
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            raise BackupError("Expected a real directory")
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise BackupError("Expected a regular file with one link")
    if private and os.name == "posix":
        wanted = 0o700 if directory else 0o600
        if stat.S_IMODE(info.st_mode) != wanted or info.st_uid != os.geteuid():
            raise BackupError("Managed backup permissions or ownership are not private")
    return info


def _regular_read(path, *, private=False):
    before = _safe_path(path, private=private)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    opened = os.fstat(fd)
    if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino) or opened.st_nlink != 1:
        os.close(fd)
        raise BackupError("File identity changed while opening")
    return os.fdopen(fd, "rb")


def _read_json(path, *, private=False):
    with _regular_read(path, private=private) as handle:
        raw = handle.read(65537)
    if len(raw) > 65536:
        raise BackupError("Metadata exceeds the allowed size")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise BackupError("Metadata is not valid JSON") from error
    if not isinstance(value, dict):
        raise BackupError("Metadata must be an object")
    return value


def _write_new(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
    _safe_path(path, private=True)


def _sync_directory(path):
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _owner(root, *, source=None, initialize=False):
    root = _absolute(root)
    if root.name != "managed-backups":
        raise BackupError("The dedicated directory must be named managed-backups")
    _safe_path(root.parent, directory=True)
    if not root.exists() and not root.is_symlink():
        if not initialize:
            raise BackupError("Managed backup directory has not been initialized")
        root.mkdir(mode=0o700)
    _safe_path(root, directory=True, private=True)
    marker = root / OWNER_FILE
    if not marker.exists() and not marker.is_symlink():
        if not initialize or any(root.iterdir()):
            raise BackupError("An unmarked or nonempty directory cannot be adopted")
        _write_new(marker, {"tool": TOOL, "format": FORMAT, "owner_id": uuid.uuid4().hex,
                            "source_path": str(source), "created_at_utc": _utc_now().isoformat()})
        _sync_directory(root)
    owner = _read_json(marker, private=True)
    if (owner.get("tool") != TOOL or owner.get("format") != FORMAT
            or not re.fullmatch(r"[a-f0-9]{32}", str(owner.get("owner_id", "")))):
        raise BackupError("Managed-directory ownership marker is invalid")
    recorded_source = _absolute(owner.get("source_path", ""))
    if recorded_source.name != DB_FILE:
        raise BackupError("Managed-directory source identity is invalid")
    if source is not None and recorded_source != source:
        raise BackupError("This managed directory belongs to a different source")
    return owner


@contextmanager
def _exclusive(root):
    """OS-released lock. Never unlink a lock file that another process can open."""
    lock_path = root / LOCK_FILE
    if lock_path.exists() or lock_path.is_symlink():
        _safe_path(lock_path, private=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    handle = os.fdopen(fd, "r+b")
    try:
        info = _safe_path(lock_path, private=True)
        opened = os.fstat(handle.fileno())
        if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
            raise BackupError("Lock file identity changed")
        if opened.st_size == 0:
            handle.write(b"1")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise BackupError("Another backup command holds the managed-directory lock") from error
        yield
    finally:
        handle.close()


def _deadline_check(deadline):
    if time.monotonic() >= deadline:
        raise BackupError("Backup operation exceeded its time limit")


def _hash(path, deadline):
    _deadline_check(deadline)
    digest = hashlib.sha256()
    with _regular_read(path, private=True) as handle:
        while chunk := handle.read(1024 * 1024):
            _deadline_check(deadline)
            digest.update(chunk)
    _deadline_check(deadline)
    return digest.hexdigest()


def _database_checks(path, deadline):
    _deadline_check(deadline)
    # Completed destinations are standalone, sealed files; immutable=1 avoids
    # creating WAL/shared-memory sidecars during a read-only verification.
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=3)) as db:
        db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        integrity = db.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise BackupError("Backup integrity_check did not return ok")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise BackupError("Backup foreign_key_check found a violation")
        result = {"integrity_check": "ok", "foreign_key_violations": 0,
                  "user_version": db.execute("PRAGMA user_version").fetchone()[0]}
        _deadline_check(deadline)
        return result


def _release(path):
    path = _absolute(path)
    if path == DEFAULT_RELEASE:
        # Only this documented deployment pointer may be a symlink. All source
        # database, backup and concrete release paths still reject links.
        concrete = path.resolve(strict=True)
        base = Path("/opt/alphaproject-lab/releases")
        if (concrete.name != "release.json" or concrete.parent.parent != base
                or not re.fullmatch(r"lab-[A-Za-z0-9._-]{1,80}", concrete.parent.name)):
            raise BackupError("Current release pointer leaves the Lab release directory")
    else:
        concrete = path
    value = _read_json(concrete)
    if (not re.fullmatch(r"lab-[A-Za-z0-9._-]{1,80}", str(value.get("release_id", "")))
            or not re.fullmatch(r"v?[0-9]+\.[0-9]+\.[0-9]+(?:[-+.][A-Za-z0-9.-]+)?", str(value.get("app_version", "")))):
        raise BackupError("Release metadata has no valid release id or application version")
    # Whitelist version fields: arbitrary config/credential fields are never copied.
    selected = {key: value[key] for key in ("release_id", "app_version", "source_sha256", "built_at") if key in value}
    if "source_sha256" in selected and not re.fullmatch(r"[a-f0-9]{64}", str(selected["source_sha256"])):
        raise BackupError("Release source hash is invalid")
    if "built_at" in selected:
        try:
            built_at = datetime.fromisoformat(selected["built_at"])
            if built_at.utcoffset() is None:
                raise ValueError("Missing time zone")
        except (TypeError, ValueError) as error:
            raise BackupError("Release build timestamp is invalid") from error
    return str(concrete), selected


def _online_copy(source, target, deadline):
    _deadline_check(deadline)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.close(fd)
    # mode=ro is essential: missing/hot/locked sources fail; the tool never fixes
    # or creates a source database. Do not use immutable=1 for an active source.
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=1)) as src, \
            closing(sqlite3.connect(target.as_uri() + "?mode=rw", uri=True, timeout=3)) as dst:
        src.execute("PRAGMA query_only=ON")
        src.backup(dst, pages=128, progress=lambda *_: _deadline_check(deadline), sleep=0.1)
        if dst.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower() != "delete":
            raise BackupError("Backup did not become a standalone database")
        dst.commit()
    before = _safe_path(target, private=True)
    # Windows FlushFileBuffers requires write access. This is exclusively the
    # private destination; the source connection remains mode=ro throughout.
    fd = os.open(target, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino) or opened.st_nlink != 1:
            raise BackupError("Backup destination identity changed before fsync")
        os.fsync(fd)
    finally:
        os.close(fd)


def _verified(root, backup_id, owner, deadline):
    _deadline_check(deadline)
    if not isinstance(backup_id, str) or not ID_PATTERN.fullmatch(backup_id):
        raise BackupError("Invalid backup identifier")
    directory = root / backup_id
    _safe_path(directory, directory=True, private=True)
    if {item.name for item in directory.iterdir()} != {DB_FILE, MANIFEST_FILE}:
        raise BackupError("Backup has missing or unexpected files; it is not cleanup eligible")
    manifest = _read_json(directory / MANIFEST_FILE, private=True)
    if (manifest.get("tool") != TOOL or manifest.get("format") != FORMAT
            or manifest.get("owner_id") != owner["owner_id"] or manifest.get("backup_id") != backup_id
            or manifest.get("status") != "complete"):
        raise BackupError("Backup ownership or completion receipt does not match")
    try:
        if manifest["source"]["database_path"] != owner["source_path"] or manifest["source"]["open_mode"] != "ro":
            raise BackupError("Backup source identity does not match its managed directory")
        created = datetime.fromisoformat(manifest["created_at_utc"])
        completed = datetime.fromisoformat(manifest["completed_at_utc"])
        if (created.utcoffset() != timedelta(0) or completed.utcoffset() != timedelta(0) or completed < created
                or manifest["created_at_china"] != created.astimezone(CHINA_TIME).isoformat()
                or manifest["completed_at_china"] != completed.astimezone(CHINA_TIME).isoformat()
                or not backup_id.startswith("backup-" + created.strftime(STAMP) + "-")):
            raise BackupError("Backup timestamps do not match the receipt")
        artifact = manifest["artifact"]
        database = directory / DB_FILE
        info = _safe_path(database, private=True)
        if (artifact["file"] != DB_FILE or artifact["size"] != info.st_size
                or artifact["sha256"] != _hash(database, deadline)):
            raise BackupError("Backup database hash or size does not match")
        checks = _database_checks(database, deadline)
        if manifest["validation"] != checks:
            raise BackupError("Backup validation receipt no longer matches")
    except (KeyError, TypeError, ValueError) as error:
        raise BackupError("Backup manifest is incomplete or invalid") from error
    return {"backup_id": backup_id, "directory": str(directory), "verified": True, "manifest": manifest}


def _scan(root, owner, deadline):
    verified, ignored = [], []
    for entry in sorted(root.iterdir(), key=lambda path: path.name):
        _deadline_check(deadline)
        if entry.name in (OWNER_FILE, LOCK_FILE):
            continue
        try:
            verified.append(_verified(root, entry.name, owner, deadline))
        except (BackupError, OSError, sqlite3.Error) as error:
            _deadline_check(deadline)
            ignored.append({"name": entry.name, "reason": str(error), "action": "preserved"})
    _deadline_check(deadline)
    verified.sort(key=lambda item: (item["manifest"]["completed_at_utc"], item["backup_id"]), reverse=True)
    return verified, ignored


def _prune(root, owner, keep, deadline):
    verified, ignored = _scan(root, owner, deadline)
    deleted = []
    for item in verified[keep:]:
        _deadline_check(deadline)
        # Recheck the exact artifact just before deletion. Never recurse, follow a
        # link, glob-remove files, or touch deployment/backups and unknown entries.
        _owner(root)
        receipt = _verified(root, item["backup_id"], owner, deadline)
        directory = Path(receipt["directory"])
        if directory.parent != root:
            raise BackupError("Cleanup target left the dedicated managed directory")
        (directory / DB_FILE).unlink()
        (directory / MANIFEST_FILE).unlink()
        directory.rmdir()
        _sync_directory(root)
        deleted.append(item["backup_id"])
    return {"keep": keep, "retained_verified": min(len(verified), keep), "deleted": deleted, "ignored": ignored}


def _cleanup_staging(root, stage):
    if stage.parent != root or not stage.name.startswith(".pending-"):
        return
    try:
        _safe_path(stage, directory=True, private=True)
        files = list(stage.iterdir())
        allowed = {DB_FILE, MANIFEST_FILE, DB_FILE + "-journal", DB_FILE + "-wal", DB_FILE + "-shm"}
        if any(item.name not in allowed for item in files):
            return
        for item in files:
            _safe_path(item, private=True)
        for item in files:
            item.unlink()
        stage.rmdir()
    except (BackupError, OSError):
        pass  # An uncertain staging path stays private and is never a completed backup.


def create_backup(source=DEFAULT_SOURCE, root=None, release_file=DEFAULT_RELEASE, *, keep=7, timeout=60):
    source = _absolute(source)
    root = _absolute(root if root is not None else source.parent / "managed-backups")
    if source.name != DB_FILE or root != source.parent / "managed-backups":
        raise BackupError("Create only uses watchlist.sqlite3 and its sibling managed-backups directory")
    if not isinstance(keep, int) or isinstance(keep, bool) or not 1 <= keep <= 30:
        raise BackupError("Retention must be between 1 and 30 verified backups")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 1 <= timeout <= 300:
        raise BackupError("Time limit must be between 1 and 300 seconds")
    before = _safe_path(source)
    release_before = _release(release_file)
    owner = _owner(root, source=source, initialize=True)
    deadline = time.monotonic() + timeout
    with _exclusive(root):
        # Initialization may have raced another creator; re-read the authoritative owner.
        owner = _owner(root, source=source)
        started = _utc_now()
        backup_id = "backup-" + started.strftime(STAMP) + "-" + uuid.uuid4().hex
        stage = root / (".pending-" + uuid.uuid4().hex)
        stage.mkdir(mode=0o700)
        published = False
        try:
            _safe_path(stage, directory=True, private=True)
            target = stage / DB_FILE
            _online_copy(source, target, deadline)
            checks = _database_checks(target, deadline)
            digest = _hash(target, deadline)
            after = _safe_path(source)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise BackupError("Source database was replaced during backup")
            if _release(release_file) != release_before:
                raise BackupError("Source release changed during backup; no completion was published")
            _deadline_check(deadline)
            completed = _utc_now()
            manifest = {"tool": TOOL, "format": FORMAT, "owner_id": owner["owner_id"],
                        "backup_id": backup_id, "status": "complete",
                        "created_at_utc": started.isoformat(), "created_at_china": started.astimezone(CHINA_TIME).isoformat(),
                        "completed_at_utc": completed.isoformat(), "completed_at_china": completed.astimezone(CHINA_TIME).isoformat(),
                        "source": {"database_path": str(source), "open_mode": "ro", "release": release_before[1],
                                   "release_evidence": "release.json unchanged before and after backup; not process attestation"},
                        "artifact": {"file": DB_FILE, "size": target.stat().st_size, "sha256": digest},
                        "validation": checks,
                        "permissions": {"directory": "0700", "files": "0600", "posix_enforced": os.name == "posix"}}
            _write_new(stage / MANIFEST_FILE, manifest)
            _sync_directory(stage)
            destination = root / backup_id
            if destination.exists() or destination.is_symlink():
                raise BackupError("Backup identifier already exists; refusing to replace it")
            _owner(root, source=source)
            os.rename(stage, destination)
            published = True
            _sync_directory(root)
            verified = _verified(root, backup_id, owner, deadline)
            retention = _prune(root, owner, keep, deadline)
            return {"ok": True, "operation": "create", **verified, "retention": retention}
        except Exception as error:
            if published:
                raise BackupError(f"Backup {backup_id} was published, but final verification/retention did not complete: {type(error).__name__}") from error
            raise
        finally:
            if not published:
                _cleanup_staging(root, stage)


def verify_backup(root, backup_id, *, timeout=60):
    root = _absolute(root)
    owner = _owner(root)
    with _exclusive(root):
        return {"ok": True, "operation": "verify", **_verified(root, backup_id, owner, time.monotonic() + timeout)}


def list_backups(root=DEFAULT_ROOT, *, timeout=60):
    root = _absolute(root)
    owner = _owner(root)
    with _exclusive(root):
        backups, ignored = _scan(root, owner, time.monotonic() + timeout)
    return {"ok": True, "operation": "list", "root": str(root), "backups": backups, "ignored": ignored}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="Create and verify a private online backup")
    create.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    create.add_argument("--root", type=Path)
    create.add_argument("--release-file", type=Path, default=DEFAULT_RELEASE)
    create.add_argument("--keep", type=int, default=7)
    create.add_argument("--timeout", type=int, default=60)
    verify = commands.add_parser("verify", help="Recheck one owned, completed backup")
    verify.add_argument("backup_id")
    verify.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    listing = commands.add_parser("list", help="List verified backups and preserved unknown entries")
    listing.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            result = create_backup(args.source, args.root, args.release_file, keep=args.keep, timeout=args.timeout)
        elif args.command == "verify":
            result = verify_backup(args.root, args.backup_id)
        else:
            result = list_backups(args.root)
    except (BackupError, OSError, sqlite3.Error, ValueError) as error:
        print(json.dumps({"ok": False, "operation": args.command, "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
