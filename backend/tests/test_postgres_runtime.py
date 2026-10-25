from __future__ import annotations

import json
import os
import uuid
from typing import Any

import pytest
from psycopg.conninfo import conninfo_to_dict
from psycopg.errors import UndefinedTable

from app import postgres_database
from app.postgres_database import _migration_checksum, run_postgres_migrations
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


def test_schema_verification_fails_closed_when_migrated_relation_is_missing(
    postgres_schema: tuple[str, Any],
) -> None:
    _schema, connect = postgres_schema
    run_postgres_migrations(connect)

    def runtime_connect(_dsn: str, **_kwargs: Any):
        return connect()

    runtime = PostgresRuntime(_dsn="hidden", profile="test", _connector=runtime_connect)
    before = runtime.verify_schema()
    assert before.ready is True

    with connect() as conn:
        conn.execute("DROP TABLE test_runs")

    status = runtime.verify_schema()
    assert status.ready is False
    assert status.missing_migration_ids == ()
    assert status.missing_relation_names == ("test_runs",)


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


def test_migration_runner_rolls_back_ddl_and_ledger_on_statement_failure(
    postgres_schema: tuple[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _schema, connect = postgres_schema
    monkeypatch.setattr(
        postgres_database,
        "POSTGRES_MIGRATIONS",
        (
            postgres_database.PostgresMigration(
                migration_id="pg_test_transaction_rollback",
                name="Test transactional DDL rollback",
                statements=(
                    "CREATE TABLE migration_rollback_probe (probe_id TEXT PRIMARY KEY)",
                    "SELECT * FROM table_that_does_not_exist_for_migration_rollback",
                ),
            ),
        ),
    )

    with pytest.raises(
        UndefinedTable,
        match="table_that_does_not_exist_for_migration_rollback",
    ):
        run_postgres_migrations(connect)

    with connect() as connection:
        assert connection.execute("SELECT to_regclass('schema_migrations')").fetchone() == (None,)
        assert connection.execute(
            "SELECT to_regclass('migration_rollback_probe')"
        ).fetchone() == (None,)


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

    # Keep representative application records in the prior schema so this
    # exercises an upgrade in place, rather than only checking the ledger.
    workspace_id = "workspace_migration_upgrade_fixture"
    project_id = "project_migration_upgrade_fixture"
    workspace_payload = {
        "id": workspace_id,
        "name": "Migration upgrade fixture",
        "owner_user_id": "user_migration_upgrade_fixture",
        "created_at": "2026-01-02T03:04:05Z",
        "metadata": {"retained": True, "source": "prior_revision"},
    }
    project_payload = {
        "id": project_id,
        "workspace_id": workspace_id,
        "name": "Existing project",
        "description": "Must survive the schema upgrade unchanged",
        "mode": "STRICT",
        "rules": ["preserve audit history", "require approval"],
        "metadata": {"migration_fixture": 1},
    }
    with connect() as connection:
        connection.execute(
            "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s::jsonb)",
            (workspace_id, json.dumps(workspace_payload)),
        )
        connection.execute(
            "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s::jsonb)",
            (project_id, workspace_id, json.dumps(project_payload)),
        )

    monkeypatch.setattr(postgres_database, "POSTGRES_MIGRATIONS", known_migrations)
    upgraded_ledger = run_postgres_migrations(connect)
    repeated_ledger = run_postgres_migrations(connect)

    with connect() as connection:
        retained_workspace = connection.execute(
            "SELECT payload FROM workspaces WHERE workspace_id=%s", (workspace_id,)
        ).fetchone()
        retained_project = connection.execute(
            "SELECT workspace_id,payload FROM projects WHERE project_id=%s", (project_id,)
        ).fetchone()

    expected_ids = [migration.migration_id for migration in known_migrations]
    assert [row["migration_id"] for row in upgraded_ledger] == expected_ids
    assert repeated_ledger == upgraded_ledger
    assert retained_workspace == (workspace_payload,)
    assert retained_project == (workspace_id, project_payload)


def test_migration_command_upgrades_documented_pg0034_state_to_current_schema(
    postgres_schema: tuple[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema, connect = postgres_schema
    migrations = postgres_database.POSTGRES_MIGRATIONS
    next_index = next(
        index for index, migration in enumerate(migrations)
        if migration.migration_id == "pg_0036_device_session_binding"
    )
    assert next_index == len(migrations) - 1
    assert migrations[next_index - 1].migration_id == "pg_0035_legacy_notification_targets"
    prior_migrations = migrations[:next_index]

    # Model the supported pg_0035 schema and verify the guarded operator
    # command binds legacy devices to no session without dropping their records.
    monkeypatch.setattr(postgres_database, "POSTGRES_MIGRATIONS", prior_migrations)
    run_postgres_migrations(connect)
    workspace_id = "workspace_pg0034_operator_upgrade_fixture"
    project_id = "project_pg0034_operator_upgrade_fixture"
    workspace_payload = {
        "id": workspace_id,
        "name": "Prior supported workspace",
        "owner_user_id": "user_pg0034_operator_upgrade_fixture",
        "metadata": {"source": "pg_0034", "must_survive": True},
    }
    project_payload = {
        "id": project_id,
        "workspace_id": workspace_id,
        "name": "Prior supported project",
        "metadata": {"must_survive": True},
    }
    user_id = "user_pg0035_operator_upgrade_fixture"
    user_payload = {
        "id": user_id,
        "email": "pg0035-upgrade@example.test",
        "display_name": "Prior supported owner",
        "role": "OWNER",
        "password_hash": "hash",
        "password_salt": "salt",
        "workspace_id": workspace_id,
        "email_verified": True,
        "disabled": False,
    }
    device_id = "device_pg0035_operator_upgrade_fixture"
    legacy_device_payload = {
        "id": device_id,
        "user_id": user_id,
        "workspace_id": workspace_id,
        "platform": "android",
        "token": "legacy-device-token",
        "app_version": "prior",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    legacy_device_created_at = "2026-01-01T00:00:00Z"
    with connect() as connection:
        connection.execute(
            "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s::jsonb)",
            (workspace_id, json.dumps(workspace_payload)),
        )
        connection.execute(
            "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s::jsonb)",
            (project_id, workspace_id, json.dumps(project_payload)),
        )
        connection.execute(
            "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,email_verified,payload) "
            "VALUES(%s,%s,%s,%s,'OWNER',FALSE,TRUE,%s::jsonb)",
            (
                user_id,
                workspace_id,
                user_payload["email"],
                user_payload["email"],
                json.dumps(user_payload),
            ),
        )
        connection.execute(
            "INSERT INTO device_registrations "
            "(device_id,user_id,workspace_id,platform,token,app_version,created_at,updated_at,payload) "
            "VALUES(%s,%s,%s,'android','legacy-device-token','prior',%s,%s,%s::jsonb)",
            (
                device_id,
                user_id,
                workspace_id,
                legacy_device_created_at,
                legacy_device_created_at,
                json.dumps(legacy_device_payload),
            ),
        )
        prior_ids = connection.execute(
            "SELECT migration_id FROM schema_migrations ORDER BY migration_id"
        ).fetchall()
    assert prior_ids == [(migration.migration_id,) for migration in prior_migrations]

    monkeypatch.setattr(postgres_database, "POSTGRES_MIGRATIONS", migrations)
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    database = _test_database_name()
    runtime = PostgresRuntime(
        _dsn=dsn,
        profile="test",
        _connector=lambda *_args, **_kwargs: connect(),
    )
    report = inspect_or_apply(
        runtime,
        dsn=dsn,
        apply=True,
        expected_database=database,
        expected_schema=schema,
        allow_remote=False,
    )
    replay = inspect_or_apply(
        runtime,
        dsn=dsn,
        apply=True,
        expected_database=database,
        expected_schema=schema,
        allow_remote=False,
    )

    expected_ledger = [
        (migration.migration_id, migration.name, _migration_checksum(migration))
        for migration in migrations
    ]
    with connect() as connection:
        actual_ledger = connection.execute(
            "SELECT migration_id,name,migration_checksum FROM schema_migrations "
            "ORDER BY migration_id"
        ).fetchall()
        retained_workspace = connection.execute(
            "SELECT payload FROM workspaces WHERE workspace_id=%s", (workspace_id,),
        ).fetchone()
        retained_project = connection.execute(
            "SELECT workspace_id,payload FROM projects WHERE project_id=%s", (project_id,),
        ).fetchone()
        retained_legacy_device = connection.execute(
            "SELECT session_id,payload->>'session_id',payload->>'token' "
            "FROM device_registrations WHERE device_id=%s",
            (device_id,),
        ).fetchone()
        schema_objects = connection.execute(
            "SELECT to_regclass(%s),to_regclass(%s),to_regclass(%s)",
            (
                "agent_run_pull_request_intents",
                "idx_agent_run_pull_request_reconcile",
                "idx_agent_runs_id_workspace",
            ),
        ).fetchone()
        intent_columns = {
            row[0] for row in connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name='agent_run_pull_request_intents'",
                (schema,),
            ).fetchall()
        }

    assert report["status"] == replay["status"] == "READY"
    assert report["missing_migration_ids"] == replay["missing_migration_ids"] == []
    assert report["unknown_migration_ids"] == replay["unknown_migration_ids"] == []
    assert report["mismatched_migration_ids"] == replay["mismatched_migration_ids"] == []
    assert report["applied_migration_ids"] == replay["applied_migration_ids"] == [
        migration.migration_id for migration in migrations
    ]
    assert actual_ledger == expected_ledger
    assert retained_workspace == (workspace_payload,)
    assert retained_project == (workspace_id, project_payload)
    assert retained_legacy_device == (None, None, "legacy-device-token")
    assert schema_objects == (
        "agent_run_pull_request_intents",
        "idx_agent_run_pull_request_reconcile",
        "idx_agent_runs_id_workspace",
    )
    assert {
        "intent_id", "workspace_id", "run_id", "task_id", "approval_id",
        "step_id", "action_id", "lease_id", "status", "title_sha256",
        "body_sha256", "last_get_status", "get_match_count",
        "get_response_sha256", "payload",
    } <= intent_columns


def test_invocation_lease_manifest_scope_migration_preserves_v11_and_allows_v12(
    postgres_schema: tuple[str, Any],
) -> None:
    _schema, connect = postgres_schema
    run_postgres_migrations(connect)
    scopes = (
        "approval_auth_agent_mcp_test_runs_connectors_email_task_notifications_delivery_rate_limits_v11",
        "approval_auth_agent_mcp_test_runs_connectors_email_task_notifications_delivery_rate_limits_invocation_leases_v12",
    )
    with connect() as connection:
        for index, scope in enumerate(scopes, start=1):
            connection.execute(
                "INSERT INTO sqlite_import_runs(source_sha256,scope,source_counts,imported_counts) "
                "VALUES(%s,%s,'{}'::jsonb,'{}'::jsonb)",
                (f"{index:064x}", scope),
            )
        stored_scopes = connection.execute(
            "SELECT scope FROM sqlite_import_runs ORDER BY source_sha256"
        ).fetchall()

    assert stored_scopes == [(scope,) for scope in scopes]


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
