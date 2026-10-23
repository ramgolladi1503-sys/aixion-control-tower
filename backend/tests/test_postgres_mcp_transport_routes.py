from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app import main, mcp_transport_routes
from app import mcp_gateway_routes
from app.application_repositories import ApplicationRepositories
from app.models import (
    AuditEvent,
    AuthUser,
    MCPChildServer,
    MCPToolDefinition,
    Project,
    User,
    UserRole,
    Workspace,
)
from app.postgres_database import run_postgres_migrations
from app.store import store


@pytest.fixture
def postgres_mcp_transport_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Callable[[], Connection], Workspace, Project, AuthUser, object]]:
    mcp_gateway_routes.reset_gateway_runtime_for_test()
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL MCP transport tests")

    schema = f"ct_mcp_transport_{uuid.uuid4().hex}"
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
            name="MCP transport test workspace",
            owner_user_id=f"user_{uuid.uuid4().hex}",
        )
        persisted_user = User(
            id=workspace.owner_user_id,
            email="mcp-transport-reviewer@example.test",
            display_name="MCP Transport Reviewer",
            role=UserRole.OWNER,
            password_hash="0" * 64,
            password_salt="0" * 32,
            workspace_id=workspace.id,
        )
        project = Project(
            id=f"project_{uuid.uuid4().hex}",
            name="MCP transport test project",
            workspace_id=workspace.id,
        )
        with connect() as connection:
            connection.execute(
                "INSERT INTO workspaces(workspace_id,payload) VALUES (%s,%s)",
                (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
            )
            connection.execute(
                "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,email_verified,payload) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    persisted_user.id,
                    persisted_user.workspace_id,
                    persisted_user.email,
                    persisted_user.email.lower(),
                    persisted_user.role,
                    persisted_user.disabled,
                    persisted_user.email_verified,
                    Jsonb(persisted_user.model_dump(mode="json")),
                ),
            )
            connection.execute(
                "INSERT INTO projects(project_id,workspace_id,payload) VALUES (%s,%s,%s)",
                (project.id, project.workspace_id, Jsonb(project.model_dump(mode="json"))),
            )

        repositories = ApplicationRepositories.from_connector(connect)
        monkeypatch.setattr(
            "app.application_repository_access.get_settings",
            lambda: SimpleNamespace(postgres_auth_enabled=True, auth_enabled=True),
        )
        monkeypatch.setattr(
            mcp_gateway_routes,
            "get_settings",
            lambda: SimpleNamespace(
                postgres_auth_enabled=True,
                agent_approval_ttl_seconds=600,
            ),
        )
        for name, value in {
            "postgres_repositories": repositories,
            "postgres_runtime": SimpleNamespace(repositories=repositories),
            "project_repository": repositories.projects,
            "mcp_child_server_repository": repositories.mcp_child_servers,
        }.items():
            monkeypatch.setattr(main.app.state, name, value, raising=False)

        user = AuthUser(
            id=workspace.owner_user_id,
            email="mcp-transport-reviewer@example.test",
            display_name="MCP Transport Reviewer",
            role=UserRole.OWNER,
            workspace_id=workspace.id,
        )
        monkeypatch.setattr(
            mcp_transport_routes,
            "workspace_id_for_request",
            lambda _user, _request: workspace.id,
        )
        monkeypatch.setitem(
            main.app.dependency_overrides,
            mcp_transport_routes.MaintainerDependency.dependency,
            lambda: user,
        )
        for dependency in (main.ReviewerDependency, mcp_gateway_routes.ReviewerDependency):
            monkeypatch.setitem(
                main.app.dependency_overrides,
                dependency.dependency,
                lambda: user,
            )
        yield connect, workspace, project, user, repositories
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _register_server(repositories, project: Project, workspace: Workspace) -> MCPChildServer:
    server = MCPChildServer(
        project_id=project.id,
        workspace_id=workspace.id,
        name="pg-only-tools",
        enabled=True,
        tools=[
            MCPToolDefinition(
                name="pg_registry_tool",
                description="Only available from the PostgreSQL registry.",
                input_schema={"type": "object", "properties": {}},
                mutating=True,
            )
        ],
    )
    repositories.mcp_child_servers.create(
        server=server,
        audit_event=AuditEvent(
            event_type="mcp.child_server.created",
            entity_id=server.id,
            workspace_id=workspace.id,
            details={"project_id": project.id, "name": server.name},
        ),
    )
    return server


def test_postgres_mcp_initialize_and_tools_list_use_bound_repositories(
    postgres_mcp_transport_runtime,
) -> None:
    _connect, workspace, project, _user, repositories = postgres_mcp_transport_runtime
    client = TestClient(main.app)
    empty_registry = client.post(
        f"/mcp/{project.id}/jsonrpc",
        json={"jsonrpc": "2.0", "id": "empty", "method": "tools/list"},
    )
    assert empty_registry.status_code == 200, empty_registry.text
    assert empty_registry.json()["result"]["tools"] == []

    server = _register_server(repositories, project, workspace)
    assert project.id not in store.projects
    assert server.id not in store.mcp_child_servers

    initialized = client.post(
        f"/mcp/{project.id}/jsonrpc",
        json={"jsonrpc": "2.0", "id": "initialize", "method": "initialize"},
    )
    listed = client.post(
        f"/mcp/{project.id}/jsonrpc",
        json={"jsonrpc": "2.0", "id": "list", "method": "tools/list"},
    )
    client.close()

    assert initialized.status_code == 200, initialized.text
    assert initialized.json()["result"]["serverInfo"]["name"] == "aixion-control-tower-mcp-gateway"
    assert listed.status_code == 200, listed.text
    tools = listed.json()["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["pg_registry_tool"]
    assert tools[0]["annotations"]["server_id"] == server.id


def test_postgres_mcp_transport_hides_foreign_workspace_projects(
    postgres_mcp_transport_runtime,
) -> None:
    connect, workspace, _project, _user, _repositories = postgres_mcp_transport_runtime
    foreign_workspace = Workspace(
        id=f"workspace_{uuid.uuid4().hex}",
        name="Foreign workspace",
        owner_user_id=f"user_{uuid.uuid4().hex}",
    )
    foreign_project = Project(
        id=f"project_{uuid.uuid4().hex}",
        name="Foreign project",
        workspace_id=foreign_workspace.id,
    )
    with connect() as connection:
        connection.execute(
            "INSERT INTO workspaces(workspace_id,payload) VALUES (%s,%s)",
            (foreign_workspace.id, Jsonb(foreign_workspace.model_dump(mode="json"))),
        )
        connection.execute(
            "INSERT INTO projects(project_id,workspace_id,payload) VALUES (%s,%s,%s)",
            (
                foreign_project.id,
                foreign_project.workspace_id,
                Jsonb(foreign_project.model_dump(mode="json")),
            ),
        )

    client = TestClient(main.app)
    response = client.post(
        f"/mcp/{foreign_project.id}/jsonrpc",
        json={"jsonrpc": "2.0", "id": "foreign", "method": "tools/list"},
    )
    client.close()

    assert response.status_code == 404
    assert workspace.id != foreign_workspace.id


def test_postgres_mcp_tools_call_waits_for_durable_approval_and_forwards_once(
    postgres_mcp_transport_runtime,
) -> None:
    _connect, workspace, project, _user, repositories = postgres_mcp_transport_runtime
    server = _register_server(repositories, project, workspace)

    call_payload = {
        "jsonrpc": "2.0",
        "id": "durable-call-1",
        "method": "tools/call",
        "params": {
            "name": "pg_registry_tool",
            "server_name": server.name,
            "arguments": {},
            "session_id": "pg-transport-session",
        },
    }
    submit_client = TestClient(main.app)
    reviewer_client = TestClient(main.app)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            submit_client.post,
            f"/mcp/{project.id}/jsonrpc",
            json=call_payload,
        )
        pending = None
        for _ in range(100):
            candidates = repositories.mcp_pending_requests.list_for_workspace(
                workspace.id,
                project_id=project.id,
            )
            pending = next(
                (item for item in candidates if item.session_id == "pg-transport-session"),
                None,
            )
            if pending is not None:
                break
            if future.done():
                early_response = future.result(timeout=1)
                pytest.fail(
                    "transport exited before persisting its durable MCP approval: "
                    f"HTTP {early_response.status_code} {early_response.text}"
                )
            time.sleep(0.05)
        assert pending is not None, "transport did not persist its durable MCP approval"
        assert not future.done(), "mutating tools/call returned before reviewer decision"

        decision = reviewer_client.post(
            f"/approvals/{pending.approval_request_id}/decision",
            json={"decision": "approve", "reason": "approve PostgreSQL MCP transport"},
        )
        assert decision.status_code == 200, decision.text
        response = future.result(timeout=15)
    submit_client.close()
    reviewer_client.close()

    assert response.status_code == 200, response.text
    structured = response.json()["result"]["structuredContent"]
    assert structured["forwarded"] is True
    assert structured["approval_required"] is True
    assert structured["approval_request_id"] == pending.approval_request_id
    assert structured["result"]["server_name"] == server.name
    assert repositories.mcp_pending_requests.get(pending.id).status.value == "FORWARDED"
    assert mcp_gateway_routes.child_received_count_for_server_for_test(server.name) == 1


def test_postgres_mcp_tools_call_denial_never_dispatches(
    postgres_mcp_transport_runtime,
) -> None:
    _connect, workspace, project, _user, repositories = postgres_mcp_transport_runtime
    server = _register_server(repositories, project, workspace)

    call_payload = {
        "jsonrpc": "2.0",
        "id": "durable-call-denied",
        "method": "tools/call",
        "params": {
            "name": "pg_registry_tool",
            "server_name": server.name,
            "arguments": {},
            "session_id": "pg-transport-denied-session",
        },
    }
    submit_client = TestClient(main.app)
    reviewer_client = TestClient(main.app)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            submit_client.post,
            f"/mcp/{project.id}/jsonrpc",
            json=call_payload,
        )
        pending = None
        for _ in range(100):
            candidates = repositories.mcp_pending_requests.list_for_workspace(
                workspace.id,
                project_id=project.id,
            )
            pending = next(
                (item for item in candidates if item.session_id == "pg-transport-denied-session"),
                None,
            )
            if pending is not None:
                break
            if future.done():
                early_response = future.result(timeout=1)
                pytest.fail(
                    "transport exited before persisting its durable MCP approval: "
                    f"HTTP {early_response.status_code} {early_response.text}"
                )
            time.sleep(0.05)
        assert pending is not None, "transport did not persist its durable MCP approval"
        decision = reviewer_client.post(
            f"/approvals/{pending.approval_request_id}/decision",
            json={"decision": "deny", "reason": "deny PostgreSQL MCP transport"},
        )
        assert decision.status_code == 200, decision.text
        response = future.result(timeout=15)
    submit_client.close()
    reviewer_client.close()

    assert response.status_code == 200, response.text
    assert response.json()["result"]["isError"] is True
    assert response.json()["result"]["structuredContent"]["forwarded"] is False
    assert repositories.mcp_pending_requests.get(pending.id).status.value == "BLOCKED_BY_DECISION"
    assert mcp_gateway_routes.child_received_count_for_server_for_test(server.name) == 0
