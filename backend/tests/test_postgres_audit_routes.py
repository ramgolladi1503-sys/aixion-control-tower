from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app import main
from app.application_repositories import ApplicationRepositories
from app.auth import require_maintainer, require_reviewer
from app.models import AuditEvent, AuthUser, User, UserRole, Workspace
from app.postgres_database import run_postgres_migrations
from app.store import store


@pytest.fixture
def postgres_audit_route_runtime(monkeypatch: pytest.MonkeyPatch) -> Iterator[
    tuple[Callable[[], Connection], Workspace, AuthUser]
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
            id=f"user_{uuid.uuid4().hex}", email=f"owner-{uuid.uuid4().hex}@example.test",
            display_name="Owner", role=UserRole.OWNER, password_hash="hash",
            password_salt="salt", workspace_id=f"workspace_{uuid.uuid4().hex}",
            email_verified=True,
        )
        workspace = Workspace(id=owner.workspace_id, name="Audit route", owner_user_id=owner.id)
        with connect() as conn:
            conn.execute("INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
                         (workspace.id, Jsonb(workspace.model_dump(mode="json"))))
            conn.execute(
                "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,email_verified,payload) "
                "VALUES(%s,%s,%s,%s,%s,FALSE,TRUE,%s)",
                (owner.id, workspace.id, owner.email, owner.email.lower(), owner.role.value,
                 Jsonb(owner.model_dump(mode="json"))),
            )
        repos = ApplicationRepositories.from_connector(connect)
        monkeypatch.setattr(main.app.state, "audit_event_repository", repos.audit_events, raising=False)
        monkeypatch.setattr(main.app.state, "workspace_membership_repository",
                            repos.workspace_membership, raising=False)
        user = AuthUser(id=owner.id, email=owner.email, display_name=owner.display_name,
                        role=owner.role, workspace_id=workspace.id)
        monkeypatch.setitem(main.app.dependency_overrides, require_reviewer, lambda: user)
        monkeypatch.setitem(main.app.dependency_overrides, require_maintainer, lambda: user)
        yield connect, workspace, user
    finally:
        main.app.dependency_overrides.pop(require_reviewer, None)
        main.app.dependency_overrides.pop(require_maintainer, None)
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_audit_http_reads_use_workspace_bound_postgres_repository(postgres_audit_route_runtime) -> None:
    connect, workspace, user = postgres_audit_route_runtime
    event = AuditEvent(
        workspace_id=workspace.id, event_type="agent.token_rotated", entity_id="agent-one",
        actor=user.email, details={"credential_state": "ACTIVE", "token": "must-redact"},
    )
    other = AuditEvent(
        workspace_id=f"workspace_other_{uuid.uuid4().hex}", event_type="agent.token_rotated",
        entity_id="agent-two", actor=user.email, details={"credential_state": "ACTIVE"},
    )
    other_workspace = Workspace(
        id=other.workspace_id, name="Other", owner_user_id=f"owner_{uuid.uuid4().hex}"
    )
    with connect() as conn:
        conn.execute("INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
                     (other_workspace.id, Jsonb(other_workspace.model_dump(mode="json"))))
        for row in (event, other):
            conn.execute(
                "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,event_type,actor,details,created_at,payload) "
                "VALUES(%s,%s,NULL,'external_agent',%s,%s,%s,%s,%s,%s)",
                (row.id, row.workspace_id, row.entity_id, row.event_type, row.actor,
                 Jsonb(row.details), row.created_at, Jsonb(row.model_dump(mode="json"))),
            )
    initial_sqlite_audit_ids = {row.id for row in store.audit_events}
    client = TestClient(main.app)
    listing = client.get("/audit/events")
    assert listing.status_code == 200, listing.text
    assert [row["id"] for row in listing.json()] == [event.id]
    legacy_listing = client.get("/audit")
    assert legacy_listing.status_code == 200, legacy_listing.text
    assert [row["id"] for row in legacy_listing.json()] == [event.id]
    assert legacy_listing.json()[0]["details"]["token"] == "[REDACTED]"
    assert listing.json()[0]["details"]["token"] == "[REDACTED]"
    assert client.get(f"/audit/events/{event.id}").status_code == 200
    assert client.get(f"/audit/events/{other.id}").status_code == 404
    exported = client.get("/audit/export")
    assert exported.status_code == 200, exported.text
    assert exported.json()["count"] == 1
    assert {row.id for row in store.audit_events} == initial_sqlite_audit_ids
