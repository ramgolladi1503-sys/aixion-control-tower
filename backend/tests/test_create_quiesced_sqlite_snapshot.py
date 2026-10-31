from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import create_quiesced_sqlite_snapshot as snapshotter
from scripts.import_approval_slice_to_postgres import _validate_source_snapshot_manifest


def _source(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO sample(value) VALUES ('fixture')")
    os.chmod(path, 0o600)


def _no_open_handles(*_args, **_kwargs):
    return SimpleNamespace(returncode=1, stdout="", stderr="")


@pytest.mark.parametrize("descriptor", ["f3u", "f4w"])
def test_snapshotter_refuses_writable_source_handle_without_creating_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    descriptor: str,
) -> None:
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "snapshot"
    _source(source)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(
        snapshotter.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout=f"p123\ncPython\n{descriptor}\n", stderr=""
        ),
    )

    result = snapshotter.main([
        "--source", str(source), "--output-dir", str(destination),
        "--confirm-source-identity", "--confirm-writers-stopped",
    ])

    assert result == 2
    assert "source has writable open handles" in capsys.readouterr().err
    assert not destination.exists()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_snapshotter_fails_closed_when_lsof_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "snapshot"
    _source(source)

    def missing_lsof(*_args, **_kwargs):
        raise FileNotFoundError("lsof")

    monkeypatch.setattr(snapshotter.subprocess, "run", missing_lsof)
    result = snapshotter.main([
        "--source", str(source), "--output-dir", str(destination),
        "--confirm-source-identity", "--confirm-writers-stopped",
    ])

    assert result == 2
    assert "lsof preflight failed" in capsys.readouterr().err
    assert not destination.exists()


def test_snapshotter_creates_readonly_backup_and_bound_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "snapshot"
    _source(source)
    monkeypatch.setattr(snapshotter.subprocess, "run", _no_open_handles)

    result = snapshotter.main([
        "--source", str(source), "--output-dir", str(destination),
        "--confirm-source-identity", "--confirm-writers-stopped",
    ])

    assert result == 0
    response = json.loads(capsys.readouterr().out)
    snapshot = Path(response["snapshot_path"])
    manifest_path = Path(response["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert response["status"] == "SNAPSHOT_CREATED_FOR_OWNER_REVIEW"
    assert response["migration_authorized"] is False
    assert manifest["writer_quiesced"] is True
    assert manifest["owner_source_identity_confirmed"] is True
    assert manifest["source_writable_database_handles_at_capture_end"] == 0
    assert manifest["snapshot_path"] == str(snapshot)
    assert manifest["snapshot_sha256"] == hashlib.sha256(snapshot.read_bytes()).hexdigest()
    assert manifest["snapshot_bytes"] == snapshot.stat().st_size
    assert manifest["snapshot_integrity_check"] == "ok"
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    _validate_source_snapshot_manifest(snapshot, manifest["snapshot_sha256"])
    with sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_snapshotter_requires_both_operator_acknowledgements(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "snapshot"
    _source(source)

    result = snapshotter.main(["--source", str(source), "--output-dir", str(destination)])

    assert result == 2
    assert "explicit source-identity and writer-freeze acknowledgements are required" in capsys.readouterr().err
    assert not destination.exists()
