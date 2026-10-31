from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from psycopg import Connection, sql

from app import postgres_database
from app.postgres_database import PostgresMigration, _migration_checksum, run_postgres_migrations


def test_postgres_migration_chain_bootstraps_empty_schema_and_replays_idempotently() -> None:
    """The complete checked-in chain must build a pristine schema and replay cleanly."""
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL migration tests")

    schema = f"ct_migration_fresh_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    try:
        first_ledger = run_postgres_migrations(connect)
        expected_ids = sorted(migration.migration_id for migration in postgres_database.POSTGRES_MIGRATIONS)
        assert [row["migration_id"] for row in first_ledger] == expected_ids
        assert len(expected_ids) == len(set(expected_ids))

        replayed_ledger = run_postgres_migrations(connect)
        assert replayed_ledger == first_ledger

        with connect() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema=current_schema() AND table_type='BASE TABLE'"
                ).fetchall()
            }
            persisted_migrations = conn.execute(
                "SELECT count(*) FROM schema_migrations"
            ).fetchone()[0]

        assert {
            "workspaces",
            "users",
            "sessions",
            "projects",
            "approval_requests",
            "agent_tasks",
            "audit_events",
        } <= tables
        assert persisted_migrations == len(expected_ids)
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_postgres_migration_chain_upgrades_every_prior_ledger_prefix() -> None:
    """Every supported applied prefix must upgrade to the current checked-in chain."""
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL migration tests")

    migrations = postgres_database.POSTGRES_MIGRATIONS
    assert migrations, "migration chain must not be empty"
    migration_ids = [migration.migration_id for migration in migrations]
    assert len(migration_ids) == len(set(migration_ids))
    schemas = [
        (prefix_length, f"ct_migration_prefix_{prefix_length}_{uuid.uuid4().hex}")
        for prefix_length in range(len(migrations))
    ]
    with psycopg.connect(dsn, autocommit=True) as admin:
        for _, schema in schemas:
            admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect_to(schema: str) -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    try:
        for prefix_length, schema in schemas:
            # Seed exactly the historical ledger prefix, then restore the full
            # checked-in chain before asserting upgrade behavior.
            previous_chain = postgres_database.POSTGRES_MIGRATIONS
            try:
                postgres_database.POSTGRES_MIGRATIONS = migrations[:prefix_length]
                seeded_ledger = run_postgres_migrations(lambda: connect_to(schema))
            finally:
                postgres_database.POSTGRES_MIGRATIONS = previous_chain

            assert [row["migration_id"] for row in seeded_ledger] == migration_ids[:prefix_length], (
                f"prefix {prefix_length} did not seed the expected ledger"
            )
            upgraded_ledger = run_postgres_migrations(lambda: connect_to(schema))
            assert [row["migration_id"] for row in upgraded_ledger] == migration_ids, (
                f"prefix {prefix_length} did not upgrade to the complete ledger"
            )
            assert [row["migration_checksum"] for row in upgraded_ledger] == [
                _migration_checksum(migration) for migration in migrations
            ], f"prefix {prefix_length} has a missing or changed migration checksum"

            with connect_to(schema) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema=current_schema() AND table_type='BASE TABLE'"
                    ).fetchall()
                }
                persisted_ledger = conn.execute(
                    "SELECT migration_id, name, migration_checksum "
                    "FROM schema_migrations ORDER BY migration_id"
                ).fetchall()
            assert {
                "workspaces",
                "users",
                "sessions",
                "projects",
                "approval_requests",
                "agent_tasks",
                "audit_events",
            } <= tables, f"prefix {prefix_length} is missing required current tables"
            assert persisted_ledger == [
                (
                    migration.migration_id,
                    migration.name,
                    _migration_checksum(migration),
                )
                for migration in migrations
            ], f"prefix {prefix_length} persisted an unexpected final ledger"
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            for _, schema in schemas:
                admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def test_postgres_migration_failure_rolls_back_prior_migrations_and_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed migration batch must leave no partial schema or migration history."""
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL migration tests")

    schema = f"ct_migration_atomicity_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    monkeypatch.setattr(
        postgres_database,
        "POSTGRES_MIGRATIONS",
        (
            PostgresMigration(
                migration_id="test_atomicity_0001",
                name="create an earlier valid schema object",
                statements=("CREATE TABLE migration_atomicity_earlier (id INTEGER PRIMARY KEY)",),
            ),
            PostgresMigration(
                migration_id="test_atomicity_0002",
                name="fail after creating a partial schema object",
                statements=(
                    "CREATE TABLE migration_atomicity_partial (id INTEGER PRIMARY KEY)",
                    "THIS IS INTENTIONALLY INVALID SQL",
                ),
            ),
        ),
    )

    try:
        with pytest.raises(psycopg.errors.SyntaxError):
            run_postgres_migrations(connect)

        with connect() as conn:
            assert conn.execute("SELECT to_regclass('schema_migrations')").fetchone() == (None,)
            assert conn.execute(
                "SELECT to_regclass('migration_atomicity_earlier')"
            ).fetchone() == (None,)
            assert conn.execute(
                "SELECT to_regclass('migration_atomicity_partial')"
            ).fetchone() == (None,)
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_agent_run_fault_migration_upgrades_existing_postgres_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL migration tests")

    schema = f"ct_agent_run_fault_migration_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    migrations = postgres_database.POSTGRES_MIGRATIONS
    fault_migration = next(
        migration
        for migration in migrations
        if migration.migration_id == "pg_0037_agent_run_fault_controls"
    )
    try:
        monkeypatch.setattr(
            postgres_database,
            "POSTGRES_MIGRATIONS",
            tuple(migration for migration in migrations if migration is not fault_migration),
        )
        before = run_postgres_migrations(connect)
        assert any(item["migration_id"] == "pg_0036_device_session_binding" for item in before)
        with connect() as conn:
            assert conn.execute("SELECT to_regclass('agent_run_faults')").fetchone() == (None,)

        monkeypatch.setattr(postgres_database, "POSTGRES_MIGRATIONS", migrations)
        after = run_postgres_migrations(connect)
        assert any(item["migration_id"] == fault_migration.migration_id for item in after)
        with connect() as conn:
            columns = {
                row[0]
                for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=current_schema() AND table_name='agent_run_faults'"
                ).fetchall()
            }
        assert {
            "fault_id",
            "workspace_id",
            "run_id",
            "remaining_uses",
            "payload",
        } <= columns
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
