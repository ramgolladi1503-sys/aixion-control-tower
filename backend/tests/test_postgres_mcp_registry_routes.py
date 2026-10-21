from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app import main, mcp_registry_routes
from app.application_repositories import ApplicationRepositories
from app.models import AuthUser, Project, UserRole, Workspace
from app.postgres_database import run_postgres_migrations
from app.store import store


@pytest.fixture
def postgres_mcp_registry_route_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Callable[[], Connection], Workspace, Project, AuthUser]]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL MCP registry route tests")

    schema = f"ct_mcp_registry_routes_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    try:
        run_postgres_migrations(connect)
        workspace = Workspace(
            id=f"workspace_{uuid.uuid4().hex}",
            name="MCP registry route workspace",
            owner_user_id=f"user_{uuid.uuid4().hex}",
        )
        project = Project(
            id=f"project_{uuid.uuid4().hex}",
            name="MCP registry route project",
            workspace_id=workspace.id,
        )
        with connect() as connection:
            connection.execute(
                "INSERT INTO workspaces(workspace_id,payload) VALUES (%s,%s)",
                (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
            )
            connection.execute(
                "INSERT INTO projects(project_id,workspace_id,payload) VALUES (%s,%s,%s)",
                (project.id, project.workspace_id, Jsonb(project.model_dump(mode="json"))),
            )

        repositories = ApplicationRepositories.from_connector(connect)
        monkeypatch.setattr(
            main.app.state,
            "mcp_child_server_repository",
            repositories.mcp_child_servers,
            raising=False,
        )
        monkeypatch.setattr(
            main.app.state,
            "project_repository",
            repositories.projects,
            raising=False,
        )
        user = AuthUser(
            id=workspace.owner_user_id,
            email="mcp-reviewer@example.test",
            display_name="MCP Reviewer",
            role=UserRole.OWNER,
            workspace_id=workspace.id,
        )
        monkeypatch.setattr(
            mcp_registry_routes,
            "workspace_id_for_request",
            lambda _user, _request: workspace.id,
        )
        monkeypatch.setitem(
            main.app.dependency_overrides,
            mcp_registry_routes.MaintainerDependency.dependency,
            lambda: user,
        )
        monkeypatch.setitem(
            main.app.dependency_overrides,
            mcp_registry_routes.ReviewerDependency.dependency,
            lambda: user,
        )
        yield connect, workspace, project, user
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_mcp_registry_http_routes_use_bound_postgres_repository(
    postgres_mcp_registry_route_runtime,
) -> None:
    connect, workspace, project, _user = postgres_mcp_registry_route_runtime
    client = TestClient(main.app)
    initial_server_ids = set(store.mcp_child_servers)
    initial_audit_ids = {event.id for event in store.audit_events}

    created = client.post(
        "/mcp-registry/child-servers",
        json={
            "project_id": project.id,
            "name": "approval-gated-tools",
            "description": "Isolated PostgreSQL route test",
            "transport": "stdio",
            "endpoint": "local://approval-gated-tools",
            "enabled": True,
            "tools": [{"name": "inspect", "description": "Read-only test tool"}],
        },
    )
    assert created.status_code == 200, created.text
    server = created.json()
    assert server["workspace_id"] == workspace.id

    listed = client.get("/mcp-registry/child-servers", params={"project_id": project.id})
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()] == [server["id"]]

    disabled = client.post(f"/mcp-registry/child-servers/{server['id']}/disable")
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["enabled"] is False

    enabled = client.post(f"/mcp-registry/child-servers/{server['id']}/enable")
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["enabled"] is True

    detail = client.get(f"/mcp-registry/child-servers/{server['id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["updated_at"] == enabled.json()["updated_at"]
    with connect() as connection:
        events = connection.execute(
            "SELECT event_type FROM audit_events WHERE entity_id=%s ORDER BY created_at,event_id",
            (server["id"],),
        ).fetchall()
    assert [event[0] for event in events] == [
        "mcp.child_server.registered",
        "mcp.child_server.disabled",
        "mcp.child_server.enabled",
    ]
    assert set(store.mcp_child_servers) == initial_server_ids
    assert {event.id for event in store.audit_events} == initial_audit_ids
