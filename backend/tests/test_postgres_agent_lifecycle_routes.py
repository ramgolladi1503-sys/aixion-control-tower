from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app import main
from app.application_repositories import ApplicationRepositories
from app.agent_routes import APPROVAL_CONNECTOR_ACTIONS
from app.auth import require_owner
from app.models import AuthUser, Project, User, UserRole, Workspace, now_utc
from app.postgres_database import run_postgres_migrations
from app.settings import get_settings
from app.store import store


@pytest.fixture
def postgres_agent_route_runtime(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[
    Callable[[], Connection], Workspace, Project, AuthUser, ApplicationRepositories
]]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL agent route tests")
    schema = f"ct_agent_routes_{uuid.uuid4().hex}"
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
        )
        workspace = Workspace(id=owner.workspace_id, name="Agent lifecycle", owner_user_id=owner.id)
        owner = owner.model_copy(update={"email_verified": True})
        project = Project(
            id=f"project_{uuid.uuid4().hex}", name="Connector project", workspace_id=workspace.id
        )
        with connect() as conn:
            conn.execute("INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
                         (workspace.id, Jsonb(workspace.model_dump(mode="json"))))
            conn.execute(
                "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,email_verified,payload) "
                "VALUES(%s,%s,%s,%s,%s,FALSE,TRUE,%s)",
                (owner.id, workspace.id, owner.email, owner.email.lower(), owner.role.value,
                 Jsonb(owner.model_dump(mode="json"))),
            )
            conn.execute("INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
                         (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))))

        repositories = ApplicationRepositories.from_connector(connect)
        for name, value in {
            "external_agent_auth_repository": repositories.external_agent_auth,
            "project_repository": repositories.projects,
            "workspace_membership_repository": repositories.workspace_membership,
        }.items():
            monkeypatch.setattr(main.app.state, name, value, raising=False)
        user = AuthUser(
            id=owner.id, email=owner.email, display_name=owner.display_name,
            role=owner.role, workspace_id=workspace.id,
        )
        monkeypatch.setitem(main.app.dependency_overrides, require_owner, lambda: user)
        yield connect, workspace, project, user, repositories
    finally:
        main.app.dependency_overrides.pop(require_owner, None)
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_postgres_agent_owner_lifecycle_is_scoped_audited_and_does_not_project_to_sqlite(
    postgres_agent_route_runtime,
) -> None:
    connect, workspace, project, _user, repositories = postgres_agent_route_runtime
    client = TestClient(main.app)
    initial_sqlite_ids = set(store.external_agents)
    registered_started = now_utc()
    create = client.post("/agents", json={
        "provider": "ANTIGRAVITY", "display_name": "Local Antigravity",
        "auth_type": "API_KEY", "allowed_project_ids": [project.id],
        "allowed_actions": sorted(action.value for action in APPROVAL_CONNECTOR_ACTIONS), "enabled": True,
    })
    assert create.status_code == 200, create.text
    created = create.json()
    agent_id, first_token = created["agent"]["id"], created["agent_token"]
    assert first_token and agent_id not in store.external_agents
    assert [item["id"] for item in client.get("/agents").json()] == [agent_id]
    assert client.get(f"/agents/{agent_id}").status_code == 200
    ttl_seconds = get_settings().external_agent_token_ttl_seconds
    status = client.get(f"/agents/{agent_id}/credentials")
    registered_finished = now_utc()
    assert status.status_code == 200 and status.json()["credential_state"] == "ACTIVE"
    registered_expiry = datetime.fromisoformat(status.json()["token_expires_at"])
    assert registered_expiry >= registered_started + timedelta(seconds=ttl_seconds - 2)
    assert registered_expiry <= registered_finished + timedelta(seconds=ttl_seconds + 2)

    rotated_started = now_utc()
    rotated = client.post(f"/agents/{agent_id}/token/rotate", json={"rate_limit_per_minute": 37})
    assert rotated.status_code == 200, rotated.text
    second_token = rotated.json()["agent_token"]
    assert second_token and second_token != first_token
    rotated_status = client.get(f"/agents/{agent_id}/credentials")
    rotated_finished = now_utc()
    assert rotated_status.status_code == 200
    rotated_expiry = datetime.fromisoformat(rotated_status.json()["token_expires_at"])
    assert rotated_expiry >= rotated_started + timedelta(seconds=ttl_seconds - 2)
    assert rotated_expiry <= rotated_finished + timedelta(seconds=ttl_seconds + 2)
    assert rotated_status.json()["credential_state"] == "ACTIVE"
    assert client.get("/agents/identity", params={"provider": "ANTIGRAVITY", "project_id": project.id},
                      headers={"X-Aixion-Agent-Id": agent_id,
                               "X-Aixion-Agent-Token": first_token}).status_code == 401
    assert client.get("/agents/identity", params={"provider": "ANTIGRAVITY", "project_id": project.id},
                      headers={"X-Aixion-Agent-Id": agent_id,
                               "X-Aixion-Agent-Token": second_token}).status_code == 200

    revoked = client.post(f"/agents/{agent_id}/token/revoke")
    assert revoked.status_code == 200, revoked.text
    assert client.get("/agents/identity", params={"provider": "ANTIGRAVITY", "project_id": project.id},
                      headers={"X-Aixion-Agent-Id": agent_id,
                               "X-Aixion-Agent-Token": second_token}).status_code == 401
    record = repositories.external_agent_auth.get(agent_id)
    assert record is not None and record[1].token_revoked_at is not None
    assert record[1].rate_limit_per_minute == 37
    with connect() as conn:
        events = conn.execute(
            "SELECT event_type FROM audit_events WHERE workspace_id=%s AND entity_id=%s "
            "ORDER BY created_at,event_id", (workspace.id, agent_id),
        ).fetchall()
    assert [row[0] for row in events] == [
        "agent.registered", "agent.token_rotated", "agent.auth_failed",
        "agent.token_revoked", "agent.auth_failed",
    ]
    assert set(store.external_agents) == initial_sqlite_ids
