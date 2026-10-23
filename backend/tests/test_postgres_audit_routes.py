from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from dataclasses import replace

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app import application_repository_access, main
from app.auth import require_maintainer, require_reviewer
from app.models import AuditEvent, AuthUser, User, UserRole, Workspace
from app.postgres_database import run_postgres_migrations
from app.postgres_runtime import PostgresRuntime
from app.settings import get_settings
from app.store import store


@pytest.fixture
def postgres_audit_route_runtime(monkeypatch: pytest.MonkeyPatch) -> Iterator[
    tuple[Callable[[], Connection], Workspace, AuthUser, PostgresRuntime]
]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL audit route tests")
    schema = f"ct_audit_routes_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    try:
        run_postgres_migrations(connect)
        owner = User(
            id=f"user_{uuid.uuid4().hex}",
            email=f"owner-{uuid.uuid4().hex}@example.test",
            display_name="Owner",
            role=UserRole.OWNER,
            password_hash="hash",
            password_salt="salt",
            workspace_id=f"workspace_{uuid.uuid4().hex}",
            email_verified=True,
        )
        workspace = Workspace(id=owner.workspace_id, name="Audit route", owner_user_id=owner.id)
        with connect() as conn:
            conn.execute(
                "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
                (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
            )
            conn.execute(
                "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,email_verified,payload) "
                "VALUES(%s,%s,%s,%s,%s,FALSE,TRUE,%s)",
                (
                    owner.id,
                    workspace.id,
                    owner.email,
                    owner.email.lower(),
                    owner.role.value,
                    Jsonb(owner.model_dump(mode="json")),
                ),
            )

        settings = replace(
            get_settings(),
            profile="test",
            auth_enabled=True,
            postgres_auth_enabled=True,
            postgres_runtime_probe_enabled=False,
            postgres_auth_rate_limits_enabled=False,
        )
        runtime = PostgresRuntime(
            _dsn=dsn,
            profile="test",
            settings=settings,
            _connector=lambda *_args, **_kwargs: connect(),
        )
        repositories = runtime.repositories
        membership = repositories.workspace_membership.snapshot(
            user_id=owner.id, claimed_workspace_id=workspace.id
        )
        assert membership.user is not None
        assert membership.user.workspace_id == workspace.id
        assert membership.resolved_workspace_exists
        monkeypatch.setattr(main, "get_settings", lambda: settings)
        monkeypatch.setattr(
            application_repository_access, "get_settings", lambda: settings
        )
        monkeypatch.setattr(main.app.state, "postgres_runtime", runtime, raising=False)
        monkeypatch.setattr(
            main.app.state, "postgres_repositories", repositories, raising=False
        )
        monkeypatch.setattr(
            main.app.state,
            "audit_event_repository",
            repositories.audit_events,
            raising=False,
        )
        monkeypatch.setattr(
            main.app.state,
            "workspace_membership_repository",
            repositories.workspace_membership,
            raising=False,
        )
        user = AuthUser(
            id=owner.id,
            email=owner.email,
            display_name=owner.display_name,
            role=owner.role,
            workspace_id=workspace.id,
        )
        monkeypatch.setitem(main.app.dependency_overrides, require_reviewer, lambda: user)
        monkeypatch.setitem(main.app.dependency_overrides, require_maintainer, lambda: user)
        yield connect, workspace, user, runtime
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _insert_audit_events(
    connect: Callable[[], Connection], *events: AuditEvent
) -> None:
    with connect() as conn:
        for row in events:
            conn.execute(
                "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,event_type,actor,details,created_at,payload) "
                "VALUES(%s,%s,NULL,'external_agent',%s,%s,%s,%s,%s,%s)",
                (
                    row.id,
                    row.workspace_id,
                    row.entity_id,
                    row.event_type,
                    row.actor,
                    Jsonb(row.details),
                    row.created_at,
                    Jsonb(row.model_dump(mode="json")),
                ),
            )


def test_legacy_audit_http_reads_bound_postgres_workspace_and_redacts_secrets(
    postgres_audit_route_runtime,
) -> None:
    connect, workspace, user, _runtime = postgres_audit_route_runtime
    event = AuditEvent(
        workspace_id=workspace.id,
        event_type="agent.token_rotated",
        entity_id="agent-one",
        actor=user.email,
        details={"credential_state": "ACTIVE", "nested": {"token": "must-redact"}},
    )
    other = AuditEvent(
        workspace_id=f"workspace_other_{uuid.uuid4().hex}",
        event_type="agent.token_rotated",
        entity_id="agent-two",
        actor=user.email,
        details={"credential_state": "ACTIVE"},
    )
    other_workspace = Workspace(
        id=other.workspace_id, name="Other", owner_user_id=f"owner_{uuid.uuid4().hex}"
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
            (other_workspace.id, Jsonb(other_workspace.model_dump(mode="json"))),
        )
    _insert_audit_events(connect, event, other)

    sqlite_fallback = AuditEvent(
        workspace_id=workspace.id,
        event_type="sqlite.must_not_be_read",
        entity_id="sqlite-fallback",
        actor=user.email,
        details={"token": "sqlite-secret"},
    )
    store.audit_events.append(sqlite_fallback)
    try:
        response = TestClient(main.app).get("/audit")
    finally:
        store.audit_events[:] = [
            row for row in store.audit_events if row.id != sqlite_fallback.id
        ]

    assert response.status_code == 200, response.text
    payload = response.json()
    assert [row["id"] for row in payload] == [event.id]
    assert payload[0]["details"]["nested"]["token"] == "[REDACTED]"
    assert all(row["id"] != sqlite_fallback.id for row in payload)


@pytest.mark.parametrize("runtime_state", ["missing", "mismatched"])
def test_legacy_audit_fails_closed_without_matching_postgres_runtime(
    postgres_audit_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
    runtime_state: str,
) -> None:
    _connect, workspace, user, runtime = postgres_audit_route_runtime
    fallback = AuditEvent(
        workspace_id=workspace.id,
        event_type="sqlite.must_not_be_read",
        entity_id="sqlite-fallback",
        actor=user.email,
        details={"secret": "must-not-leak"},
    )
    store.audit_events.append(fallback)
    try:
        if runtime_state == "missing":
            monkeypatch.setattr(main.app.state, "postgres_runtime", None)
        else:
            monkeypatch.setattr(
                main.app.state,
                "postgres_repositories",
                PostgresRuntime(
                    _dsn="unused",
                    profile="test",
                    _connector=lambda *_args, **_kwargs: None,
                ).repositories,
            )
        response = TestClient(main.app).get("/audit")
    finally:
        store.audit_events[:] = [row for row in store.audit_events if row.id != fallback.id]

    assert response.status_code == 503
    assert runtime.repositories is not None
    assert fallback.id not in response.text


def test_legacy_audit_requires_verified_postgres_workspace_membership(
    postgres_audit_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect, _workspace, user, _runtime = postgres_audit_route_runtime
    unpersisted_user = user.model_copy(update={"id": f"unpersisted_{uuid.uuid4().hex}"})
    monkeypatch.setitem(
        main.app.dependency_overrides,
        require_reviewer,
        lambda: unpersisted_user,
    )

    response = TestClient(main.app).get("/audit")

    assert response.status_code == 403
