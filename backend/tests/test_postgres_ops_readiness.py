from __future__ import annotations

import os
import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import psycopg
import pytest
from psycopg import Connection, sql

from app import ops_routes
from app.application_repositories import ApplicationRepositories
from app.postgres_database import run_postgres_migrations
from app.settings import get_settings
from app.models import AuthUser, UserRole
from app.trust_models import CapabilityLeaseStatus, TrustEventType


@pytest.fixture
def postgres_readiness_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Callable[[], Connection], object, object]]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL readiness tests")

    schema = f"ct_ops_readiness_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    try:
        run_postgres_migrations(connect)
        repositories = ApplicationRepositories.from_connector(connect)
        expected_migrations = tuple(
            migration.migration_id for migration in ops_routes.POSTGRES_MIGRATIONS
        )
        schema_status = SimpleNamespace(
            ready=True,
            applied_migration_ids=expected_migrations,
            missing_migration_ids=(),
            unknown_migration_ids=(),
            mismatched_migration_ids=(),
        )
        runtime = SimpleNamespace(
            repositories=repositories,
            connect=connect,
            probe=lambda: SimpleNamespace(
                ready=True,
                schema=schema_status,
                pool_size=1,
                pool_available=1,
                pool_waiting=0,
            ),
        )
        application_state = SimpleNamespace(
            postgres_runtime=runtime,
            postgres_repositories=repositories,
            trust_event_repository=repositories.trust_events,
        )
        settings = replace(
            get_settings(),
            postgres_auth_enabled=True,
            postgres_runtime_probe_enabled=True,
        )
        monkeypatch.setattr(ops_routes, "get_settings", lambda: settings)
        yield connect, runtime, application_state
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_postgres_readiness_verifies_the_bound_durable_trust_ledger(
    postgres_readiness_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect, runtime, application_state = postgres_readiness_runtime
    repository = runtime.repositories.trust_events
    repository.append(
        TrustEventType.ACTION_RECEIVED,
        entity_type="proposed_action",
        entity_id="postgres-readiness-action",
        actor="agent:readiness-test",
    )

    def local_ledger_must_not_be_read():
        pytest.fail("PostgreSQL readiness must not read the process-local trust ledger")

    monkeypatch.setattr(ops_routes, "verify_flight_recorder", local_ledger_must_not_be_read)

    readiness = ops_routes.build_runtime_readiness(
        postgres_runtime=runtime,
        application_state=application_state,
    )

    assert readiness.flight_recorder_valid is True
    assert readiness.flight_recorder_event_count == 1
    assert readiness.database_backend == "mixed"
    assert readiness.status == "not_ready"


def test_postgres_readiness_requires_a_workspace_scoped_lease_count(
    postgres_readiness_runtime,
) -> None:
    _connect, runtime, application_state = postgres_readiness_runtime

    missing = ops_routes.build_runtime_readiness(
        postgres_runtime=runtime,
        application_state=application_state,
    )
    counted = ops_routes.build_runtime_readiness(
        postgres_runtime=runtime,
        application_state=application_state,
        postgres_active_capability_leases=3,
    )

    assert missing.active_capability_leases == 0
    assert missing.active_capability_leases_available is False
    assert any("lease readiness count is unavailable" in e for e in missing.errors)
    assert counted.active_capability_leases == 3
    assert counted.active_capability_leases_available is True


def test_postgres_readiness_logs_only_exception_type_for_unavailable_lease_count(
    postgres_readiness_runtime,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _connect, runtime, application_state = postgres_readiness_runtime

    def fail_with_sensitive_detail():
        raise RuntimeError("private connection detail")

    runtime.connect = fail_with_sensitive_detail
    request = SimpleNamespace(app=SimpleNamespace(state=application_state))
    user = AuthUser(
        id="readiness-owner",
        email="owner@example.test",
        display_name="Owner",
        role=UserRole.OWNER,
        workspace_id="workspace-readiness",
    )
    monkeypatch.setattr(
        ops_routes, "workspace_id_for_request", lambda _user, _request: "workspace-readiness"
    )

    with caplog.at_level(logging.WARNING, logger=ops_routes.__name__):
        readiness = ops_routes.get_runtime_readiness(request, user)

    assert readiness.active_capability_leases_available is False
    assert "RuntimeError" in caplog.text
    assert "private connection detail" not in caplog.text


def test_production_without_postgres_auth_counts_leases_from_bound_postgres_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    production_settings = replace(
        get_settings(),
        profile="production",
        postgres_auth_enabled=False,
        postgres_runtime_probe_enabled=True,
        trust_enforcement=True,
        lease_signing_key_configured=True,
    )
    monkeypatch.setattr(ops_routes, "get_settings", lambda: production_settings)

    workspace_id = "workspace-production-readiness"
    user = AuthUser(
        id="readiness-owner",
        email="owner@example.test",
        display_name="Owner",
        role=UserRole.OWNER,
        workspace_id=workspace_id,
    )
    connection = SimpleNamespace(transaction=lambda: nullcontext())
    repositories = ApplicationRepositories.from_connector(lambda: None)
    count_calls: list[tuple[object, str, datetime]] = []

    def count_bound_postgres_leases(
        conn: object, *, workspace_id: str, now: datetime
    ) -> int:
        count_calls.append((conn, workspace_id, now))
        return 4

    monkeypatch.setattr(
        repositories.trust,
        "count_active_capability_leases",
        count_bound_postgres_leases,
    )
    runtime = SimpleNamespace(
        repositories=repositories,
        connect=lambda: nullcontext(connection),
    )
    application_state = SimpleNamespace(
        postgres_runtime=runtime,
        postgres_repositories=repositories,
        trust_event_repository=repositories.trust_events,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=application_state))
    monkeypatch.setattr(
        ops_routes,
        "workspace_id_for_request",
        lambda _user, _request: workspace_id,
    )

    # A conflicting process-local projection must not be used in production.
    monkeypatch.setattr(
        ops_routes.store,
        "capability_leases",
        {
            "sqlite-only-lease": SimpleNamespace(
                status=CapabilityLeaseStatus.ACTIVE,
                expires_at=datetime.max.replace(tzinfo=timezone.utc),
            )
        },
    )
    result: dict[str, object] = {}

    def capture_readiness(**kwargs: object) -> SimpleNamespace:
        result.update(kwargs)
        return SimpleNamespace(
            active_capability_leases=kwargs["postgres_active_capability_leases"],
            active_capability_leases_available=(
                kwargs["postgres_active_capability_leases"] is not None
            ),
            errors=[],
        )

    monkeypatch.setattr(ops_routes, "build_runtime_readiness", capture_readiness)
    readiness = ops_routes.get_runtime_readiness(request, user)

    assert len(count_calls) == 1
    assert count_calls[0][0] is connection
    assert count_calls[0][1] == workspace_id
    assert count_calls[0][2].tzinfo is not None
    assert result["postgres_runtime"] is runtime
    assert result["application_state"] is application_state
    assert readiness.active_capability_leases == 4
    assert readiness.active_capability_leases_available is True
    assert not any(
        "capability lease readiness count is unavailable" in error
        for error in readiness.errors
    )


def test_postgres_readiness_fails_closed_on_mixed_trust_repository_binding(
    postgres_readiness_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect, runtime, application_state = postgres_readiness_runtime
    application_state.trust_event_repository = object()

    def local_ledger_must_not_be_read():
        pytest.fail("A mixed PostgreSQL binding must not fall back to process-local trust state")

    monkeypatch.setattr(ops_routes, "verify_flight_recorder", local_ledger_must_not_be_read)

    readiness = ops_routes.build_runtime_readiness(
        postgres_runtime=runtime,
        application_state=application_state,
    )

    assert readiness.flight_recorder_valid is False
    assert readiness.flight_recorder_event_count == 0
    assert any("flight recorder failed" in error.lower() for error in readiness.errors)


def test_postgres_readiness_fails_closed_when_durable_ledger_is_unavailable(
    postgres_readiness_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect, runtime, application_state = postgres_readiness_runtime
    runtime.connect = lambda: (_ for _ in ()).throw(RuntimeError("database unavailable"))

    def local_ledger_must_not_be_read():
        pytest.fail("An unavailable PostgreSQL ledger must not fall back to process-local state")

    monkeypatch.setattr(ops_routes, "verify_flight_recorder", local_ledger_must_not_be_read)

    readiness = ops_routes.build_runtime_readiness(
        postgres_runtime=runtime,
        application_state=application_state,
    )

    assert readiness.flight_recorder_valid is False
    assert readiness.flight_recorder_event_count == 0
    assert any("flight recorder failed" in error.lower() for error in readiness.errors)
