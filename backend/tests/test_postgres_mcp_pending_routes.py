from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta
from types import SimpleNamespace

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app import main, mcp_gateway_routes
from app.application_repositories import ApplicationRepositories
from app.models import AuthUser, MCPPendingRequest, MCPPendingStatus, Project, UserRole, Workspace, now_utc
from app.postgres_database import run_postgres_migrations
from app.store import store


@pytest.fixture
def postgres_mcp_pending_route_runtime(monkeypatch: pytest.MonkeyPatch) -> Iterator[
    tuple[Callable[[], Connection], Workspace, Project, AuthUser, MCPPendingRequest]
]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL MCP pending route tests")
    schema = f"ct_mcp_pending_routes_{uuid.uuid4().hex}"
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
            name="MCP pending route workspace",
            owner_user_id=f"user_{uuid.uuid4().hex}",
        )
        project = Project(
            id=f"project_{uuid.uuid4().hex}",
            name="MCP pending route project",
            workspace_id=workspace.id,
        )
        pending = MCPPendingRequest(
            id=f"mcp_pending_{uuid.uuid4().hex}",
            project_id=project.id,
            workspace_id=workspace.id,
            approval_request_id=f"approval_{uuid.uuid4().hex}",
            server_name="test-child",
            tool_name="write_file",
            arguments={"path": "README.md"},
            created_at=now_utc() - timedelta(minutes=4),
        )
        approval_payload = {
            "id": pending.approval_request_id,
            "project_id": project.id,
            "workspace_id": workspace.id,
            "work_order_id": None,
            "source_agent_id": None,
            "status": "REQUESTED",
        }
        with connect() as connection:
            connection.execute(
                "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
                (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
            )
            connection.execute(
                "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
                (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))),
            )
            connection.execute(
                "INSERT INTO approval_requests(approval_id,project_id,workspace_id,work_order_id,"
                "source_agent_id,status,expires_at,created_at,updated_at,version,payload) "
                "VALUES(%s,%s,%s,NULL,NULL,'REQUESTED',NULL,%s,%s,0,%s)",
                (
                    pending.approval_request_id,
                    project.id,
                    workspace.id,
                    pending.created_at,
                    pending.updated_at,
                    Jsonb(approval_payload),
                ),
            )
            connection.execute(
                "INSERT INTO mcp_pending_requests(pending_id,approval_id,project_id,workspace_id,"
                "status,attempts,lease_expires_at,created_at,updated_at,payload) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    pending.id,
                    pending.approval_request_id,
                    pending.project_id,
                    pending.workspace_id,
                    pending.status.value,
                    pending.attempts,
                    pending.lease_expires_at,
                    pending.created_at,
                    pending.updated_at,
                    Jsonb(pending.model_dump(mode="json")),
                ),
            )

        repositories = ApplicationRepositories.from_connector(connect)
        monkeypatch.setattr(main.app.state, "postgres_repositories", repositories, raising=False)
        monkeypatch.setattr(
            main.app.state,
            "postgres_runtime",
            SimpleNamespace(repositories=repositories),
            raising=False,
        )
        monkeypatch.setattr(
            mcp_gateway_routes,
            "get_settings",
            lambda: SimpleNamespace(postgres_auth_enabled=True),
        )
        monkeypatch.setattr(
            mcp_gateway_routes,
            "workspace_id_for_request",
            lambda user, _request: user.workspace_id,
        )
        user = AuthUser(
            id=workspace.owner_user_id,
            email="mcp-owner@example.test",
            display_name="MCP Owner",
            role=UserRole.OWNER,
            workspace_id=workspace.id,
        )
        for dependency in (mcp_gateway_routes.ReviewerDependency, mcp_gateway_routes.MaintainerDependency):
            monkeypatch.setitem(main.app.dependency_overrides, dependency.dependency, lambda: user)
        yield connect, workspace, project, user, pending
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_postgres_mcp_pending_reads_are_workspace_scoped_and_do_not_project_to_sqlite(
    postgres_mcp_pending_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, workspace, project, user, pending = postgres_mcp_pending_route_runtime
    client = TestClient(main.app)
    before_pending = dict(store.mcp_pending_requests)
    before_approvals = dict(store.approval_requests)
    before_audits = {event.id for event in store.audit_events}

    listed = client.get("/mcp-gateway/pending-requests", params={"project_id": project.id})
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()] == [pending.id]

    filtered = client.get(
        "/mcp-gateway/pending-requests",
        params={"approval_request_id": pending.approval_request_id, "status": MCPPendingStatus.WAITING_FOR_APPROVAL.value},
    )
    assert filtered.status_code == 200, filtered.text
    assert [item["id"] for item in filtered.json()] == [pending.id]

    detail = client.get(f"/mcp-gateway/pending-requests/{pending.id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["workspace_id"] == workspace.id

    health = client.get("/mcp-gateway/pending-requests/health", params={"project_id": project.id})
    assert health.status_code == 200, health.text
    assert health.json()["total"] == 1
    assert health.json()["waiting_for_approval"] == 1

    # A different workspace must receive the same 404 as an absent request.
    monkeypatch.setattr(
        mcp_gateway_routes,
        "workspace_id_for_request",
        lambda _user, _request: "workspace_other",
    )
    denied_detail = client.get(f"/mcp-gateway/pending-requests/{pending.id}")
    assert denied_detail.status_code == 404
    denied_list = client.get("/mcp-gateway/pending-requests")
    assert denied_list.status_code == 200
    assert denied_list.json() == []

    with connect() as connection:
        assert connection.execute("SELECT count(*) FROM mcp_pending_requests").fetchone()[0] == 1
    assert store.mcp_pending_requests == before_pending
    assert store.approval_requests == before_approvals
    assert {event.id for event in store.audit_events} == before_audits


def test_postgres_mcp_mutation_routes_fail_closed_without_sqlite_projection(
    postgres_mcp_pending_route_runtime,
) -> None:
    connect, _workspace, project, _user, pending = postgres_mcp_pending_route_runtime
    client = TestClient(main.app)
    before_pending = dict(store.mcp_pending_requests)
    before_approvals = dict(store.approval_requests)
    before_audits = {event.id for event in store.audit_events}

    submitted = client.post(
        "/mcp-gateway/requests",
        json={
            "project_id": project.id,
            "request": {
                "server_name": "test-child",
                "tool_name": "write_file",
                "arguments": {"path": "README.md"},
                "requested_by": "test",
                "mutating": True,
            },
        },
    )
    assert submitted.status_code == 503

    retried = client.post(f"/mcp-gateway/pending-requests/{pending.id}/retry", json={})
    assert retried.status_code == 503
    resolved = client.post(f"/mcp-gateway/approvals/{pending.approval_request_id}/resolve")
    assert resolved.status_code == 503

    with connect() as connection:
        assert connection.execute("SELECT count(*) FROM mcp_pending_requests").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM approval_requests").fetchone()[0] == 1
    assert store.mcp_pending_requests == before_pending
    assert store.approval_requests == before_approvals
    assert {event.id for event in store.audit_events} == before_audits


def test_partial_postgres_runtime_does_not_fall_back_to_sqlite_pending_reads(
    postgres_mcp_pending_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect, _workspace, _project, _user, _pending = postgres_mcp_pending_route_runtime
    monkeypatch.setattr(main.app.state, "postgres_repositories", None, raising=False)
    before_pending = dict(store.mcp_pending_requests)

    response = TestClient(main.app).get("/mcp-gateway/pending-requests")

    assert response.status_code == 503
    assert store.mcp_pending_requests == before_pending


def test_configured_postgres_auth_without_runtime_does_not_select_sqlite(
    postgres_mcp_pending_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect, _workspace, _project, _user, _pending = postgres_mcp_pending_route_runtime
    monkeypatch.setattr(main.app.state, "postgres_runtime", None, raising=False)
    monkeypatch.setattr(main.app.state, "postgres_repositories", None, raising=False)
    before_pending = dict(store.mcp_pending_requests)

    response = TestClient(main.app).get("/mcp-gateway/pending-requests")

    assert response.status_code == 503
    assert store.mcp_pending_requests == before_pending


def test_postgres_pending_binding_is_rejected_without_postgres_auth(
    postgres_mcp_pending_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect, _workspace, _project, _user, _pending = postgres_mcp_pending_route_runtime
    monkeypatch.setattr(
        mcp_gateway_routes,
        "get_settings",
        lambda: SimpleNamespace(postgres_auth_enabled=False),
    )

    response = TestClient(main.app).get("/mcp-gateway/pending-requests")

    assert response.status_code == 503
