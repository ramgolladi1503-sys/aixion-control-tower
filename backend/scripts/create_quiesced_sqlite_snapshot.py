#!/usr/bin/env python3
"""Create an owner-only SQLite snapshot after an operator-managed write freeze.

This tool never stops services. It requires explicit owner acknowledgements,
uses lsof to fail closed when the source has writable open handles, performs a
read-only SQLite backup, and binds the result to an owner-only manifest.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote


class SnapshotBlocked(RuntimeError):
    """Raised when snapshot preconditions cannot be proven."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stat_record(path: Path) -> dict[str, int]:
    details = path.stat()
    return {
        "device": details.st_dev,
        "inode": details.st_ino,
        "modified_ns": details.st_mtime_ns,
        "size": details.st_size,
    }


def _validate_source(path: Path) -> None:
    if not path.is_file() or path.is_symlink():
        raise SnapshotBlocked("source must be an existing regular non-symlink file")
    details = path.stat()
    if details.st_uid != os.getuid():
        raise SnapshotBlocked("source file must be owned by the current user")
    if stat.S_IMODE(details.st_mode) & 0o077:
        raise SnapshotBlocked("source file permissions must be owner-only (0600 or stricter)")
    if Path(str(path) + "-wal").exists() or Path(str(path) + "-shm").exists():
        raise SnapshotBlocked("SQLite WAL/SHM sidecar exists; coordinate source maintenance first")


def _writable_handle_count(path: Path) -> int:
    try:
        result = subprocess.run(
            ["lsof", "-nP", "-F", "pcf", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SnapshotBlocked("lsof preflight failed; writable source handles are unknown") from exc
    if result.returncode not in {0, 1}:
        raise SnapshotBlocked("lsof preflight returned an error; writable source handles are unknown")

    # lsof -Fpcf emits descriptor records such as f3u (read/write) and f4w
    # (write-only). Read-only descriptors end in r and do not block capture.
    return sum(
        1
        for line in result.stdout.splitlines()
        if line.startswith("f") and line[1:].endswith(("u", "w"))
    )


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(manifest, output, sort_keys=True, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.chmod(path, 0o600)
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise SnapshotBlocked("snapshot manifest permissions could not be restricted to its owner")


def create_snapshot(
    source_path: Path,
    output_dir: Path,
    *,
    source_identity_confirmed: bool,
    writer_freeze_confirmed: bool,
) -> dict[str, Any]:
    if not source_identity_confirmed or not writer_freeze_confirmed:
        raise SnapshotBlocked("explicit source-identity and writer-freeze acknowledgements are required")
    source = source_path.expanduser().absolute()
    destination_dir = output_dir.expanduser().absolute()
    _validate_source(source)
    if destination_dir.exists() or destination_dir.is_symlink():
        raise SnapshotBlocked("output directory already exists; choose a new private destination")
    if not destination_dir.parent.is_dir():
        raise SnapshotBlocked("output parent directory must already exist")
    if _writable_handle_count(source) != 0:
        raise SnapshotBlocked("source has writable open handles; no snapshot was created")

    source_stat_before = _stat_record(source)
    source_sha_before = _sha256(source)
    destination_dir.mkdir(mode=0o700)
    os.chmod(destination_dir, 0o700)
    if stat.S_IMODE(destination_dir.stat().st_mode) & 0o077:
        raise SnapshotBlocked("output directory permissions could not be restricted to its owner")

    snapshot = destination_dir / "backend.sqlite3"
    descriptor = os.open(snapshot, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    source_uri = f"file:{quote(source.resolve().as_posix(), safe='/')}?mode=ro"
    with closing(sqlite3.connect(source_uri, uri=True, timeout=5)) as source_db:
        source_db.execute("PRAGMA query_only = ON")
        with closing(sqlite3.connect(snapshot)) as snapshot_db:
            source_db.backup(snapshot_db, pages=128, sleep=0.05)
            integrity = snapshot_db.execute("PRAGMA integrity_check").fetchone()
    if integrity != ("ok",):
        raise SnapshotBlocked("SQLite integrity check failed on the captured snapshot")

    os.chmod(snapshot, 0o600)
    snapshot_sha = _sha256(snapshot)
    source_stat_after = _stat_record(source)
    source_sha_after = _sha256(source)
    writable_handles_after = _writable_handle_count(source)
    if writable_handles_after != 0:
        raise SnapshotBlocked("source acquired writable handles during capture; snapshot is not certified")
    if source_stat_before != source_stat_after or source_sha_before != source_sha_after:
        raise SnapshotBlocked("source changed during capture; snapshot is not certified")

    manifest = {
        "capture_method": "sqlite3 read-only connection backup after lsof writer checks",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "owner_source_identity_confirmed": source_identity_confirmed,
        "source_path": str(source),
        "source_sha256_before_capture": source_sha_before,
        "source_sha256_after_capture": source_sha_after,
        "source_stat_before_capture": source_stat_before,
        "source_stat_after_capture": source_stat_after,
        "source_writable_database_handles_at_capture_end": writable_handles_after,
        "snapshot_bytes": snapshot.stat().st_size,
        "snapshot_integrity_check": "ok",
        "snapshot_path": str(snapshot),
        "snapshot_sha256": snapshot_sha,
        "writer_quiesced": writer_freeze_confirmed,
    }
    manifest_path = snapshot.with_name(snapshot.name + ".manifest.json")
    _write_manifest(manifest_path, manifest)
    return {
        "status": "SNAPSHOT_CREATED_FOR_OWNER_REVIEW",
        "snapshot_path": str(snapshot),
        "manifest_path": str(manifest_path),
        "snapshot_bytes": manifest["snapshot_bytes"],
        "snapshot_sha256": snapshot_sha,
        "migration_authorized": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="owner-confirmed SQLite source")
    parser.add_argument("--output-dir", required=True, type=Path, help="new private destination directory")
    parser.add_argument(
        "--confirm-source-identity",
        action="store_true",
        help="acknowledge that --source is the owner-selected source for staging rehearsal",
    )
    parser.add_argument(
        "--confirm-writers-stopped",
        action="store_true",
        help="acknowledge that all source writers/workers are stopped through normal shutdown",
    )
    args = parser.parse_args(argv)
    try:
        if not args.confirm_source_identity or not args.confirm_writers_stopped:
            raise SnapshotBlocked("explicit source-identity and writer-freeze acknowledgements are required")
        result = create_snapshot(
            args.source,
            args.output_dir,
            source_identity_confirmed=args.confirm_source_identity,
            writer_freeze_confirmed=args.confirm_writers_stopped,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, sqlite3.Error, SnapshotBlocked, ValueError) as exc:
        print(
            json.dumps({"status": "BLOCKED", "reason": str(exc), "snapshot_created": False}, sort_keys=True),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
