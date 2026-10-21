from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app.database_migrations import MIGRATIONS, MIGRATION_TABLE, get_applied_migrations, run_migrations
from app.models import User, UserRole
from app.store import SQLiteBackedStore


def test_fresh_database_gets_baseline_email_and_rate_limit_migrations(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "fresh.sqlite3"
    monkeypatch.setenv("AIXION_DB_PATH", str(db_path))

    SQLiteBackedStore()

    with sqlite3.connect(db_path) as conn:
        kv_table = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'kv_store'").fetchone()
        email_table = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'auth_email_outbox'").fetchone()
        failure_table = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'approval_delivery_failures'").fetchone()
        failure_identity_trigger = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' "
            "AND name = 'approval_delivery_failure_claim_identity_insert'"
        ).fetchone()
        rate_table = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'auth_rate_limit_buckets'").fetchone()
        expiry_index = conn.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_auth_rate_limit_window_start'").fetchone()
        migration_rows = conn.execute(f"SELECT migration_id, name, applied_at FROM {MIGRATION_TABLE}").fetchall()

    assert kv_table is not None
    assert email_table is not None
    assert rate_table is not None
    assert expiry_index is not None
    assert failure_table is not None
    assert failure_identity_trigger is not None
    assert len(migration_rows) == 8
    assert migration_rows[0][0] == "0001_baseline_kv_store"
    assert migration_rows[0][1] == "Create baseline key-value persistence table"
    assert migration_rows[0][2]
    assert migration_rows[1][0] == "0002_auth_email_outbox"
    assert migration_rows[1][2]
    assert migration_rows[2][0] == "0003_auth_rate_limit_buckets"
    assert migration_rows[2][2]
    assert migration_rows[3][0] == "0004_auth_rate_limit_expiry_index"
    assert migration_rows[3][2]
    assert migration_rows[4][0] == "0005_approval_invocation_identity"
    assert migration_rows[4][2]
    assert migration_rows[5][0] == "0006_approval_delivery_claims"
    assert migration_rows[5][2]
    assert migration_rows[6][0] == "0007_approval_delivery_failures"
    assert migration_rows[6][2]
    assert migration_rows[7][0] == "0008_delivery_failure_claim_identity"
    assert migration_rows[7][2]


def test_migration_runner_is_idempotent(tmp_path) -> None:
    db_path = tmp_path / "idempotent.sqlite3"

    with sqlite3.connect(db_path) as conn:
        run_migrations(conn)
        run_migrations(conn)
        migration_count = conn.execute(f"SELECT COUNT(*) FROM {MIGRATION_TABLE}").fetchone()[0]
        kv_table_count = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'kv_store'"
        ).fetchone()[0]

        assert migration_count == 8
    assert kv_table_count == 1


def test_concurrent_process_migrations_apply_each_migration_once(tmp_path) -> None:
    db_path = tmp_path / "concurrent-startup.sqlite3"
    start_file = tmp_path / "start-migrations"
    backend_root = Path(__file__).resolve().parents[1]
    script = """
import pathlib
import sqlite3
import sys
import time

sys.path.insert(0, sys.argv[3])
from app.database_migrations import run_migrations

database = pathlib.Path(sys.argv[1])
start = pathlib.Path(sys.argv[2])
while not start.exists():
    time.sleep(0.005)
with sqlite3.connect(database, timeout=30.0) as connection:
    run_migrations(connection)
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(db_path), str(start_file), str(backend_root)],
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    try:
        start_file.touch()
        outputs = [process.communicate(timeout=30) for process in processes]
        assert [process.returncode for process in processes] == [0, 0], outputs
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    with sqlite3.connect(db_path) as conn:
        applied = conn.execute(
            f"SELECT migration_id FROM {MIGRATION_TABLE} ORDER BY migration_id"
        ).fetchall()
    assert len(applied) == len(MIGRATIONS)
    assert len({migration_id for (migration_id,) in applied}) == len(MIGRATIONS)


def test_approval_invocation_index_rejects_duplicate_scoped_identity(tmp_path) -> None:
    db_path = tmp_path / "approval-invocation-identity.sqlite3"
    payloads = [
        ("approval-one", '{"id":"approval-one","workspace_id":"workspace-1",'
         '"source_agent_id":"connector-1","source_invocation_id":"invocation-1"}'),
        ("approval-two", '{"id":"approval-two","workspace_id":"workspace-1",'
         '"source_agent_id":"connector-1","source_invocation_id":"invocation-1"}'),
    ]

    with sqlite3.connect(db_path) as conn:
        run_migrations(conn)
        conn.execute(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES('approval_request',?,?)",
            payloads[0],
        )
        with pytest.raises(sqlite3.IntegrityError, match="idx_approval_invocation_identity"):
            conn.execute(
                "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES('approval_request',?,?)",
                payloads[1],
            )


def test_approval_delivery_claim_rejects_empty_invocation_identity(tmp_path) -> None:
    db_path = tmp_path / "approval-delivery-claims.sqlite3"
    with sqlite3.connect(db_path) as conn:
        run_migrations(conn)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            conn.execute(
                "INSERT INTO approval_delivery_claims "
                "(approval_id,workspace_id,source_agent_id,source_invocation_id,claimed_at) "
                "VALUES('approval-1','','connector-1','','2026-10-21T00:00:00+00:00')"
            )


def test_approval_delivery_failure_rejects_claim_identity_mismatch(tmp_path) -> None:
    db_path = tmp_path / "approval-delivery-failure-identity.sqlite3"
    with sqlite3.connect(db_path) as conn:
        run_migrations(conn)
        conn.execute(
            "INSERT INTO approval_delivery_claims "
            "(approval_id,workspace_id,source_agent_id,source_invocation_id,claimed_at) "
            "VALUES('approval-1','workspace-1','agent-1','invocation-1','2026-10-21T00:00:00+00:00')"
        )
        with pytest.raises(sqlite3.IntegrityError, match="identity must match its claim"):
            conn.execute(
                "INSERT INTO approval_delivery_failures "
                "(approval_id,workspace_id,source_agent_id,source_invocation_id,reason_code,reported_at) "
                "VALUES('approval-1','workspace-2','agent-1','invocation-1',"
                "'CLAIM_RESPONSE_AMBIGUOUS','2026-10-21T00:00:00+00:00')"
            )


def test_delivery_failure_identity_migration_fails_on_inconsistent_existing_rows(tmp_path) -> None:
    db_path = tmp_path / "legacy-delivery-failure-identity.sqlite3"
    with sqlite3.connect(db_path) as conn:
        run_migrations(conn)
        conn.execute("DROP TRIGGER approval_delivery_failure_claim_identity_insert")
        conn.execute(
            "INSERT INTO approval_delivery_claims "
            "(approval_id,workspace_id,source_agent_id,source_invocation_id,claimed_at) "
            "VALUES('approval-1','workspace-1','agent-1','invocation-1','2026-10-21T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO approval_delivery_failures "
            "(approval_id,workspace_id,source_agent_id,source_invocation_id,reason_code,reported_at) "
            "VALUES('approval-1','workspace-2','agent-1','invocation-1',"
            "'CLAIM_RESPONSE_AMBIGUOUS','2026-10-21T00:00:00+00:00')"
        )
        conn.execute("DROP TRIGGER approval_delivery_failure_claim_identity_update")
        conn.execute(
            f"DELETE FROM {MIGRATION_TABLE} "
            "WHERE migration_id='0008_delivery_failure_claim_identity'"
        )
        conn.commit()

    with sqlite3.connect(db_path) as conn:
        with pytest.raises(RuntimeError, match="inconsistent rows exist"):
            run_migrations(conn)


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
    assert [migration["migration_id"] for migration in second_store.applied_migrations()] == [
        "0001_baseline_kv_store",
        "0002_auth_email_outbox",
        "0003_auth_rate_limit_buckets",
        "0004_auth_rate_limit_expiry_index",
        "0005_approval_invocation_identity",
        "0006_approval_delivery_claims",
        "0007_approval_delivery_failures",
        "0008_delivery_failure_claim_identity",
    ]


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

    assert len(applied) == 8
    assert applied[0]["migration_id"] == "0001_baseline_kv_store"
    assert applied[0]["name"] == "Create baseline key-value persistence table"
    assert applied[0]["applied_at"]
    assert applied[1]["migration_id"] == "0002_auth_email_outbox"
    assert applied[2]["migration_id"] == "0003_auth_rate_limit_buckets"
    assert applied[3]["migration_id"] == "0004_auth_rate_limit_expiry_index"
    assert applied[4]["migration_id"] == "0005_approval_invocation_identity"
    assert applied[5]["migration_id"] == "0006_approval_delivery_claims"
    assert applied[6]["migration_id"] == "0007_approval_delivery_failures"
    assert applied[7]["migration_id"] == "0008_delivery_failure_claim_identity"
