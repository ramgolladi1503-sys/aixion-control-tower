from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from psycopg import Connection, sql

from app import postgres_database
from app.postgres_database import PostgresMigration, run_postgres_migrations


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
