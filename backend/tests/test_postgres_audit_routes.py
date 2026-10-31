from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import timedelta

import psycopg
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app import application_repository_access, audit_routes, main, recovery_routes
from app.auth import require_maintainer, require_owner, require_reviewer
from app.models import AuditEvent, AuthUser, Project, User, UserRole, Workspace, now_utc
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
        monkeypatch.setattr(audit_routes, "get_settings", lambda: settings)
        monkeypatch.setattr(recovery_routes, "get_settings", lambda: settings)
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
        monkeypatch.setitem(main.app.dependency_overrides, require_owner, lambda: user)
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


def test_legacy_audit_http_does_not_fall_back_to_sqlite(
    postgres_audit_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, workspace, user, _runtime = postgres_audit_route_runtime
    event = AuditEvent(
        workspace_id=workspace.id,
        event_type="approval.decision",
        entity_id=f"approval_{uuid.uuid4().hex}",
        actor=user.email,
        details={"decision": "deny"},
    )
    _insert_audit_events(connect, event)

    def reject_sqlite_access() -> None:
        raise AssertionError("PostgreSQL-authoritative audit read accessed SQLite")

    # The legacy repository uses this connection provider. PostgreSQL-backed
    # workspace resolution and audit reads must not consult it.
    monkeypatch.setattr(store, "_connect", reject_sqlite_access)

    response = TestClient(main.app).get("/audit")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert [row["id"] for row in payload] == [event.id]


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


def test_postgres_audit_list_export_and_detail_are_workspace_scoped_filtered_and_redacted(
    postgres_audit_route_runtime,
) -> None:
    connect, workspace, user, _runtime = postgres_audit_route_runtime
    timestamp = now_utc()
    newest = AuditEvent(
        workspace_id=workspace.id,
        event_type="approval.decision",
        entity_id="approval-private",
        actor=user.email,
        details={"decision": "approve", "nested": {"api_secret": "never-return"}},
        created_at=timestamp,
    )
    older = AuditEvent(
        workspace_id=workspace.id,
        event_type="approval.created",
        entity_id="approval-private",
        actor=user.email,
        details={"decision": "pending"},
        created_at=timestamp - timedelta(minutes=1),
    )
    foreign = AuditEvent(
        workspace_id=f"workspace_foreign_{uuid.uuid4().hex}",
        event_type="approval.decision",
        entity_id="approval-private",
        actor=user.email,
        details={"decision": "deny", "api_secret": "foreign-secret"},
        created_at=timestamp + timedelta(minutes=1),
    )
    foreign_workspace = Workspace(
        id=foreign.workspace_id, name="Foreign", owner_user_id=f"owner_{uuid.uuid4().hex}"
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
            (foreign_workspace.id, Jsonb(foreign_workspace.model_dump(mode="json"))),
        )
    _insert_audit_events(connect, newest, older, foreign)

    client = TestClient(main.app)
    listed = client.get(
        "/audit/events",
        params={"event_type": "approval.decision", "entity_id": "approval-private", "limit": 1},
    )
    exported = client.get(
        "/audit/export",
        params={"entity_id": "approval-private", "limit": 1},
    )
    detail = client.get(f"/audit/events/{newest.id}")
    foreign_detail = client.get(f"/audit/events/{foreign.id}")

    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()] == [newest.id]
    assert listed.json()[0]["details"]["nested"]["api_secret"] == "[REDACTED]"
    assert exported.status_code == 200, exported.text
    assert exported.json()["count"] == 1
    assert exported.json()["truncated"] is True
    assert exported.json()["events"][0]["id"] == newest.id
    assert exported.json()["events"][0]["details"]["nested"]["api_secret"] == "[REDACTED]"
    assert detail.status_code == 200, detail.text
    assert detail.json()["details"]["nested"]["api_secret"] == "[REDACTED]"
    assert foreign_detail.status_code == 404


@pytest.mark.parametrize("path", ["/audit/events", "/audit/export", "/audit/events/missing"])
@pytest.mark.parametrize("runtime_state", ["missing", "mismatched"])
def test_postgres_audit_reads_fail_closed_without_matching_lifespan_bundle(
    postgres_audit_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    runtime_state: str,
) -> None:
    _connect, _workspace, _user, runtime = postgres_audit_route_runtime
    if runtime_state == "missing":
        monkeypatch.setattr(main.app.state, "postgres_runtime", None)
    else:
        mismatched = PostgresRuntime(
            _dsn="unused",
            profile="test",
            _connector=lambda *_args, **_kwargs: None,
        )
        monkeypatch.setattr(main.app.state, "postgres_repositories", mismatched.repositories)

    response = TestClient(main.app).get(path)

    assert response.status_code == 503, response.text
    assert runtime.repositories is not None


def test_postgres_audit_recovery_export_uses_workspace_scoped_runtime_snapshot(
    postgres_audit_route_runtime,
) -> None:
    connect, workspace, _user, _runtime = postgres_audit_route_runtime
    own_project = Project(
        id=f"project_{uuid.uuid4().hex}",
        name="Recovery export owner project",
        workspace_id=workspace.id,
    )
    foreign_workspace = Workspace(
        id=f"workspace_{uuid.uuid4().hex}",
        name="Foreign recovery workspace",
        owner_user_id=f"user_{uuid.uuid4().hex}",
    )
    foreign_project = Project(
        id=f"project_{uuid.uuid4().hex}",
        name="Foreign recovery project",
        workspace_id=foreign_workspace.id,
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
            (foreign_workspace.id, Jsonb(foreign_workspace.model_dump(mode="json"))),
        )
        for project in (own_project, foreign_project):
            conn.execute(
                "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
                (
                    project.id,
                    project.workspace_id,
                    Jsonb(project.model_dump(mode="json")),
                ),
            )

    client = TestClient(main.app)
    audit_response = client.get("/audit/recovery/export")
    client.close()

    assert audit_response.status_code == 200, audit_response.text
    audit_snapshot = audit_response.json()
    expected_project_ids = [own_project.id]
    assert [row["id"] for row in audit_snapshot["entities"]["projects"]] == expected_project_ids
    assert audit_snapshot["applied_migrations"]


@pytest.mark.parametrize("runtime_state", ["missing", "mismatched"])
def test_postgres_recovery_export_fails_closed_without_matching_runtime(
    postgres_audit_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
    runtime_state: str,
) -> None:
    _connect, _workspace, _user, runtime = postgres_audit_route_runtime
    if runtime_state == "missing":
        monkeypatch.setattr(main.app.state, "postgres_runtime", None)
    else:
        mismatched = PostgresRuntime(
            _dsn="unused",
            profile="test",
            _connector=lambda *_args, **_kwargs: None,
        )
        monkeypatch.setattr(main.app.state, "postgres_repositories", mismatched.repositories)

    response = TestClient(main.app).get("/audit/recovery/export")

    assert response.status_code == 503, response.text
    assert runtime.repositories is not None


def test_postgres_audit_read_fails_closed_without_workspace(
    postgres_audit_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audit_routes, "workspace_id_for_request", lambda *_args: None)

    response = TestClient(main.app).get("/audit/events")

    assert response.status_code == 503


@pytest.mark.parametrize(
    ("profile", "postgres_auth_enabled"),
    [("production", False), ("test", True)],
)
def test_direct_legacy_audit_read_fails_closed_before_sqlite_fallback(
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    postgres_auth_enabled: bool,
) -> None:
    settings = replace(
        get_settings(),
        profile=profile,
        postgres_auth_enabled=postgres_auth_enabled,
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)

    def unexpected_workspace_lookup(_user: AuthUser) -> str | None:
        raise AssertionError("direct audit read touched SQLite workspace membership")

    class UnexpectedAuditEvents:
        def __iter__(self):
            raise AssertionError("direct audit read touched SQLite audit events")

    monkeypatch.setattr(main, "_workspace_id_for_user", unexpected_workspace_lookup)
    monkeypatch.setattr(main.store, "audit_events", UnexpectedAuditEvents())
    user = AuthUser(
        id="direct-audit-user",
        email="direct-audit@example.test",
        display_name="Direct audit user",
        role=UserRole.OWNER,
        workspace_id="workspace_direct_audit",
    )

    with pytest.raises(HTTPException) as raised:
        main.list_audit_events(user=user, http_request=None)

    assert raised.value.status_code == 503


def test_direct_legacy_audit_read_retains_local_sqlite_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = replace(
        get_settings(),
        profile="test",
        postgres_auth_enabled=False,
        postgres_runtime_probe_enabled=True,
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    event = AuditEvent(
        workspace_id="workspace_direct_audit",
        event_type="local.direct_audit_read",
        entity_id="direct-audit",
        actor="owner@example.test",
        details={"source": "sqlite"},
    )
    monkeypatch.setattr(main, "_workspace_id_for_user", lambda _user: "workspace_direct_audit")
    monkeypatch.setattr(main.store, "audit_events", [event])
    user = AuthUser(
        id="direct-audit-user",
        email="direct-audit@example.test",
        display_name="Direct audit user",
        role=UserRole.OWNER,
        workspace_id="workspace_direct_audit",
    )

    assert main.list_audit_events(user=user, http_request=None) == [event]
