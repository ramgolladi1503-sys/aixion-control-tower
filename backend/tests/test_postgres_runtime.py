from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from psycopg.conninfo import conninfo_to_dict

from app import postgres_database
from app.postgres_database import run_postgres_migrations
from app.postgres_runtime import PostgresRuntime
from scripts.migrate_postgres import MigrationCommandError, inspect_or_apply


def _test_database_name() -> str:
    """Return the configured disposable PostgreSQL test database name."""
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is not configured")
    database = conninfo_to_dict(dsn).get("dbname")
    if not database:
        pytest.skip("AIXION_TEST_POSTGRES_DSN must specify a database name")
    return database


def test_runtime_requires_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIXION_POSTGRES_DSN", raising=False)

    with pytest.raises(RuntimeError, match="AIXION_POSTGRES_DSN is required"):
        PostgresRuntime.from_environment()


@pytest.mark.parametrize("dsn", [
    "postgresql://user:pass@db.example/aixion",
    "postgresql://user:pass@db.example/aixion?sslmode=require",
])
def test_production_runtime_requires_full_tls_verification(
    monkeypatch: pytest.MonkeyPatch,
    dsn: str,
) -> None:
    monkeypatch.setenv("AIXION_PROFILE", "production")
    monkeypatch.setenv("AIXION_POSTGRES_DSN", dsn)

    with pytest.raises(RuntimeError, match="sslmode=verify-full"):
        PostgresRuntime.from_environment()


def test_runtime_connect_uses_bounded_connection_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = "postgresql://user:secret@db.example/aixion?sslmode=verify-full"
    seen: dict[str, Any] = {}

    def connector(value: str, **kwargs: Any) -> object:
        seen["dsn"] = value
        seen.update(kwargs)
        return object()

    monkeypatch.setenv("AIXION_PROFILE", "production")
    monkeypatch.setenv("AIXION_POSTGRES_DSN", dsn)
    monkeypatch.setenv("AIXION_POSTGRES_CONNECT_TIMEOUT_SECONDS", "9")
    monkeypatch.setenv("AIXION_POSTGRES_STATEMENT_TIMEOUT_MS", "22000")
    monkeypatch.setenv("AIXION_POSTGRES_LOCK_TIMEOUT_MS", "4000")
    monkeypatch.setenv("AIXION_POSTGRES_IDLE_TRANSACTION_TIMEOUT_MS", "18000")
    runtime = PostgresRuntime.from_environment(connector=connector)  # type: ignore[arg-type]

    assert "secret" not in repr(runtime)
    assert runtime.connect() is not None
    assert seen == {
        "dsn": dsn,
        "connect_timeout": 9,
        "application_name": "aixion-control-tower",
        "options": "-c statement_timeout=22000 -c lock_timeout=4000 "
        "-c idle_in_transaction_session_timeout=18000",
    }


def test_runtime_repository_bundle_uses_one_shared_connection_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIXION_PROFILE", "test")
    monkeypatch.setenv("AIXION_POSTGRES_DSN", "postgresql://user:pass@db.example/aixion")

    runtime = PostgresRuntime.from_environment(connector=lambda *_args, **_kwargs: None)
    repositories = runtime.repositories

    assert runtime.repositories is repositories

    members = (
        repositories.auth,
        repositories.auth_rate_limits,
        repositories.email_outbox,
        repositories.notification_outbox,
        repositories.approval_decisions,
        repositories.agent_tasks,
        repositories.projects,
        repositories.agent_runs,
        repositories.workspace_membership,
        repositories.external_agent_auth,
        repositories.work_orders,
        repositories.ideas,
        repositories.connectors,
    )
    assert all(repository._connect.__self__ is runtime for repository in members)

    assert runtime.repositories.notification_outbox is repositories.notification_outbox


def test_schema_verification_is_read_only_and_reports_missing_migrations(
    postgres_schema: tuple[str, Any],
) -> None:
    schema, connect = postgres_schema
    run_postgres_migrations(connect)
    def runtime_connect(_dsn: str, **_kwargs: Any):
        return connect()

    runtime = PostgresRuntime(_dsn="hidden", profile="test", _connector=runtime_connect)

    ready = runtime.verify_schema()
    assert ready.ready is True
    probe = runtime.probe()
    assert probe.ready is True
    assert probe.schema.ready is True
    assert probe.pool_size == 0

    with connect() as conn:
        conn.execute(
            "DELETE FROM schema_migrations WHERE migration_id = %s",
            (ready.applied_migration_ids[-1],),
        )
    missing = runtime.verify_schema()

    assert missing.ready is False
    assert missing.missing_migration_ids == (ready.applied_migration_ids[-1],)
    with connect() as conn:
        count = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
    assert count == len(ready.applied_migration_ids) - 1
    assert schema


def test_schema_verification_fails_closed_when_migration_ledger_is_absent(
    postgres_schema: tuple[str, Any],
) -> None:
    schema, connect = postgres_schema

    def runtime_connect(_dsn: str, **_kwargs: Any):
        return connect()

    runtime = PostgresRuntime(_dsn="hidden", profile="test", _connector=runtime_connect)

    status = runtime.verify_schema()

    assert status.ready is False
    assert status.migration_ledger_present is False
    assert status.applied_migration_ids == ()
    assert status.missing_migration_ids


def test_migration_command_check_is_read_only_on_an_empty_schema(
    postgres_schema: tuple[str, Any],
) -> None:
    schema, connect = postgres_schema
    runtime = PostgresRuntime(
        _dsn="host=localhost dbname=postgres",
        profile="test",
        _connector=lambda *_args, **_kwargs: connect(),
    )

    report = inspect_or_apply(
        runtime,
        dsn="host=localhost dbname=postgres",
        apply=False,
        expected_database=None,
        expected_schema=None,
        allow_remote=False,
    )

    assert report["status"] == "NOT_READY"
    assert report["schema"] == schema
    assert report["migration_ledger_present"] is False
    with connect() as connection:
        assert connection.execute("SELECT to_regclass('schema_migrations')").fetchone() == (None,)


def test_migration_command_refuses_wrong_database_before_ddl(
    postgres_schema: tuple[str, Any],
) -> None:
    schema, connect = postgres_schema
    runtime = PostgresRuntime(
        _dsn="host=localhost dbname=postgres",
        profile="test",
        _connector=lambda *_args, **_kwargs: connect(),
    )

    with pytest.raises(MigrationCommandError, match="does not match"):
        inspect_or_apply(
            runtime,
            dsn="host=localhost dbname=postgres",
            apply=True,
            expected_database="wrong_database",
            expected_schema=schema,
            allow_remote=False,
        )

    with connect() as connection:
        assert connection.execute("SELECT to_regclass('schema_migrations')").fetchone() == (None,)


def test_migration_command_requires_explicit_permission_for_remote_target(
    postgres_schema: tuple[str, Any],
) -> None:
    schema, connect = postgres_schema
    runtime = PostgresRuntime(
        _dsn="host=db.example dbname=postgres sslmode=verify-full",
        profile="test",
        _connector=lambda *_args, **_kwargs: connect(),
    )

    with pytest.raises(MigrationCommandError, match="--allow-remote"):
        inspect_or_apply(
            runtime,
            dsn="host=db.example dbname=postgres sslmode=verify-full",
            apply=True,
            expected_database="postgres",
            expected_schema=schema,
            allow_remote=False,
        )

    with connect() as connection:
        assert connection.execute("SELECT to_regclass('schema_migrations')").fetchone() == (None,)


def test_migration_command_requires_full_tls_verification_for_remote_check(
    postgres_schema: tuple[str, Any],
) -> None:
    _schema, connect = postgres_schema
    runtime = PostgresRuntime(
        _dsn="host=db.example dbname=postgres sslmode=require",
        profile="test",
        _connector=lambda *_args, **_kwargs: connect(),
    )

    with pytest.raises(MigrationCommandError, match="sslmode=verify-full"):
        inspect_or_apply(
            runtime,
            dsn="host=db.example dbname=postgres sslmode=require",
            apply=False,
            expected_database=None,
            expected_schema=None,
            allow_remote=False,
        )


def test_migration_command_does_not_treat_cloud_sql_socket_as_local(
    postgres_schema: tuple[str, Any],
) -> None:
    schema, connect = postgres_schema
    runtime = PostgresRuntime(
        _dsn="host=/cloudsql/project:region:instance dbname=postgres sslmode=disable",
        profile="test",
        _connector=lambda *_args, **_kwargs: connect(),
    )

    with pytest.raises(MigrationCommandError, match="sslmode=verify-full"):
        inspect_or_apply(
            runtime,
            dsn="host=/cloudsql/project:region:instance dbname=postgres sslmode=disable",
            apply=True,
            expected_database="postgres",
            expected_schema=schema,
            allow_remote=True,
        )


def test_migration_command_applies_and_rechecks_idempotently(
    postgres_schema: tuple[str, Any],
) -> None:
    schema, connect = postgres_schema
    database = _test_database_name()
    runtime = PostgresRuntime(
        _dsn=f"host=localhost dbname={database}",
        profile="test",
        _connector=lambda *_args, **_kwargs: connect(),
    )

    first = inspect_or_apply(
        runtime,
        dsn=f"host=localhost dbname={database}",
        apply=True,
        expected_database=database,
        expected_schema=schema,
        allow_remote=False,
    )
    with connect() as connection:
        first_ledger = connection.execute(
            "SELECT migration_id,name,migration_checksum,applied_at "
            "FROM schema_migrations ORDER BY migration_id"
        ).fetchall()

    second = inspect_or_apply(
        runtime,
        dsn=f"host=localhost dbname={database}",
        apply=True,
        expected_database=database,
        expected_schema=schema,
        allow_remote=False,
    )
    with connect() as connection:
        second_ledger = connection.execute(
            "SELECT migration_id,name,migration_checksum,applied_at "
            "FROM schema_migrations ORDER BY migration_id"
        ).fetchall()

    assert first["status"] == second["status"] == "READY"
    assert first["apply_requested"] is True
    assert second["applied_migration_ids"] == first["applied_migration_ids"]
    assert first_ledger == second_ledger


def test_migration_command_upgrades_from_a_prior_known_revision(
    postgres_schema: tuple[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _schema, connect = postgres_schema
    known_migrations = postgres_database.POSTGRES_MIGRATIONS
    prior_revision = known_migrations[:1]
    assert prior_revision

    monkeypatch.setattr(postgres_database, "POSTGRES_MIGRATIONS", prior_revision)
    prior_ledger = run_postgres_migrations(connect)
    assert [row["migration_id"] for row in prior_ledger] == [
        migration.migration_id for migration in prior_revision
    ]

    monkeypatch.setattr(postgres_database, "POSTGRES_MIGRATIONS", known_migrations)
    upgraded_ledger = run_postgres_migrations(connect)
    repeated_ledger = run_postgres_migrations(connect)

    expected_ids = [migration.migration_id for migration in known_migrations]
    assert [row["migration_id"] for row in upgraded_ledger] == expected_ids
    assert repeated_ledger == upgraded_ledger


def test_migration_command_refuses_wrong_schema_before_ddl(
    postgres_schema: tuple[str, Any],
) -> None:
    _schema, connect = postgres_schema
    database = _test_database_name()
    runtime = PostgresRuntime(
        _dsn=f"host=localhost dbname={database}",
        profile="test",
        _connector=lambda *_args, **_kwargs: connect(),
    )

    with pytest.raises(MigrationCommandError, match="does not match --expected-schema"):
        inspect_or_apply(
            runtime,
            dsn=f"host=localhost dbname={database}",
            apply=True,
            expected_database=database,
            expected_schema="public",
            allow_remote=False,
        )

    with connect() as connection:
        assert connection.execute("SELECT to_regclass('schema_migrations')").fetchone() == (None,)


def test_runtime_pool_leases_and_closes_postgres_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is not configured")
    monkeypatch.setenv("AIXION_PROFILE", "test")
    monkeypatch.setenv("AIXION_POSTGRES_DSN", dsn)
    monkeypatch.setenv("AIXION_POSTGRES_POOL_MIN_SIZE", "0")
    monkeypatch.setenv("AIXION_POSTGRES_POOL_MAX_SIZE", "1")
    monkeypatch.setenv("AIXION_POSTGRES_POOL_MAX_WAITING", "1")
    runtime = PostgresRuntime.from_environment()

    try:
        runtime.open(wait=True)
        with runtime.connect() as conn:
            assert conn.execute("SELECT 1").fetchone() == (1,)
        stats = runtime.pool_stats()
        assert stats["pool_max"] == 1
    finally:
        runtime.close()


@pytest.fixture
def postgres_schema() -> Any:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is not configured")
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql
    schema = "runtime_test_" + uuid.uuid4().hex

    def connect():
        conn = psycopg.connect(dsn)
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return conn

    with psycopg.connect(dsn) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        yield schema, connect
    finally:
        with psycopg.connect(dsn) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
