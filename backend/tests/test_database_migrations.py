from __future__ import annotations

import sqlite3

import pytest

from app.database_migrations import MIGRATION_TABLE, get_applied_migrations, run_migrations
from app.models import User, UserRole
from app.store import SQLiteBackedStore


def test_fresh_database_gets_baseline_migration(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "fresh.sqlite3"
    monkeypatch.setenv("AIXION_DB_PATH", str(db_path))

    SQLiteBackedStore()

    with sqlite3.connect(db_path) as conn:
        kv_table = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'kv_store'").fetchone()
        migration_rows = conn.execute(f"SELECT migration_id, name, applied_at FROM {MIGRATION_TABLE}").fetchall()

    assert kv_table is not None
    assert len(migration_rows) == 1
    assert migration_rows[0][0] == "0001_baseline_kv_store"
    assert migration_rows[0][1] == "Create baseline key-value persistence table"
    assert migration_rows[0][2]


def test_migration_runner_is_idempotent(tmp_path) -> None:
    db_path = tmp_path / "idempotent.sqlite3"

    with sqlite3.connect(db_path) as conn:
        run_migrations(conn)
        run_migrations(conn)
        migration_count = conn.execute(f"SELECT COUNT(*) FROM {MIGRATION_TABLE}").fetchone()[0]
        kv_table_count = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'kv_store'"
        ).fetchone()[0]

    assert migration_count == 1
    assert kv_table_count == 1


def test_existing_database_state_survives_baseline_migration(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "existing.sqlite3"
    monkeypatch.setenv("AIXION_DB_PATH", str(db_path))

    first_store = SQLiteBackedStore()
    user = User(
        email="owner@example.com",
        display_name="Owner",
        role=UserRole.OWNER,
        password_hash="hash",
        password_salt="salt",
    )
    first_store.users[user.id] = user
    first_store.persist()

    second_store = SQLiteBackedStore()

    assert user.id in second_store.users
    assert second_store.users[user.id].email == "owner@example.com"
    assert [migration["migration_id"] for migration in second_store.applied_migrations()] == ["0001_baseline_kv_store"]


def test_unknown_newer_migration_fails_safely(tmp_path) -> None:
    db_path = tmp_path / "newer.sqlite3"

    with sqlite3.connect(db_path) as conn:
        run_migrations(conn)
        conn.execute(
            f"INSERT INTO {MIGRATION_TABLE}(migration_id, name, applied_at) VALUES (?, ?, ?)",
            ("9999_newer_schema", "Schema not supported by this code", "2099-01-01T00:00:00+00:00"),
        )
        conn.commit()

    with sqlite3.connect(db_path) as conn:
        with pytest.raises(RuntimeError, match="newer than this backend understands"):
            run_migrations(conn)


def test_migration_metadata_persists(tmp_path) -> None:
    db_path = tmp_path / "metadata.sqlite3"

    with sqlite3.connect(db_path) as conn:
        run_migrations(conn)

    with sqlite3.connect(db_path) as conn:
        applied = get_applied_migrations(conn)

    assert len(applied) == 1
    assert applied[0]["migration_id"] == "0001_baseline_kv_store"
    assert applied[0]["name"] == "Create baseline key-value persistence table"
    assert applied[0]["applied_at"]
