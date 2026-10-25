from __future__ import annotations

import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable, Iterator
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace

import psycopg
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app import main, mcp_gateway_routes
from app.application_repositories import ApplicationRepositories
from app.models import AuthUser, MCPPendingRequest, MCPPendingStatus, Project, User, UserRole, Workspace, now_utc
from app.approval_integrity import compute_approval_payload_hash
from app.models import ApprovalStatus, AuditEvent, MCPChildServer, MCPToolDefinition
from app.approval_repository import PostgresApprovalDecisionRepository
from app.mcp_pending_repository import SQLiteMCPPendingRepository
from app.project_repository import PostgresProjectRepository
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
        registered_test_server = MCPChildServer(
            id=f"mcp_server_{uuid.uuid4().hex}",
            project_id=project.id,
            workspace_id=workspace.id,
            name="test-child",
            transport="test",
            enabled=True,
            tools=[MCPToolDefinition(name="write_file", mutating=True)],
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
            connection.execute(
                "INSERT INTO mcp_child_servers(server_id,project_id,workspace_id,name,enabled,"
                "created_at,updated_at,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    registered_test_server.id,
                    registered_test_server.project_id,
                    registered_test_server.workspace_id,
                    registered_test_server.name,
                    registered_test_server.enabled,
                    registered_test_server.created_at,
                    registered_test_server.updated_at,
                    Jsonb(registered_test_server.model_dump(mode="json")),
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
        persisted_user = User(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            role=user.role,
            password_hash="test-password-hash",
            password_salt="test-password-salt",
            workspace_id=workspace.id,
            email_verified=True,
        )
        with connect() as connection:
            connection.execute(
                "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,email_verified,payload) "
                "VALUES(%s,%s,%s,%s,%s,FALSE,TRUE,%s)",
                (
                    persisted_user.id,
                    workspace.id,
                    persisted_user.email,
                    persisted_user.email.lower(),
                    persisted_user.role.value,
                    Jsonb(persisted_user.model_dump(mode="json")),
                ),
            )
        for dependency in (mcp_gateway_routes.ReviewerDependency, mcp_gateway_routes.MaintainerDependency):
            monkeypatch.delitem(
                main.app.dependency_overrides,
                dependency.dependency,
                raising=False,
            )
            monkeypatch.setitem(main.app.dependency_overrides, dependency.dependency, lambda: user)
        yield connect, workspace, project, user, pending
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _create_approved_mcp_forwarding_case(
    connect,
    project: Project,
    user: AuthUser,
    *,
    transport: str = "test",
    endpoint: str = "",
) -> tuple[str, MCPChildServer]:
    repositories = main.app.state.postgres_repositories
    server = MCPChildServer(
        project_id=project.id,
        workspace_id=project.workspace_id,
        name="route-test-child",
        transport=transport,
        endpoint=endpoint,
        enabled=True,
        tools=[MCPToolDefinition(name="write_file", mutating=True)],
    )
    repositories.mcp_child_servers.create(
        server=server,
        audit_event=AuditEvent(
            event_type="mcp.child_server.created",
            actor=user.email,
            entity_id=server.id,
            workspace_id=server.workspace_id,
            details={"project_id": project.id, "name": server.name},
        ),
    )
    client = TestClient(main.app)
    submitted = client.post(
        "/mcp-gateway/requests",
        headers={"Idempotency-Key": f"resolve-{uuid.uuid4().hex}"},
        json={
            "project_id": project.id,
            "request": {
                "server_name": "route-test-child",
                "tool_name": "write_file",
                "arguments": {"path": "approved.txt", "content": "approved"},
                "session_id": "mcp-session-01",
                "requested_by": "codex-connector",
                "mutating": True,
            },
        },
    )
    assert submitted.status_code == 200, submitted.text
    approval_id = submitted.json()["approval_request_id"]
    approval = repositories.approval_decisions.get(approval_id)
    assert approval is not None
    approved = approval.model_copy(
        update={
            "status": ApprovalStatus.APPROVED,
            "approved_payload_hash_version": 2,
            "approved_at": now_utc(),
            "approved_by_user_id": user.id,
            "updated_at": now_utc(),
        }
    )
    approved = approved.model_copy(
        update={"approved_payload_hash": compute_approval_payload_hash(approved)}
    )
    with connect() as connection:
        connection.execute(
            "UPDATE approval_requests SET status=%s,updated_at=%s,version=version+1,payload=%s "
            "WHERE approval_id=%s",
            (approved.status.value, approved.updated_at, Jsonb(approved.model_dump(mode="json")), approval_id),
        )

    return approval_id, server


def _update_mcp_server_endpoint(server: MCPChildServer, user: AuthUser, endpoint: str) -> MCPChildServer:
    updated = server.model_copy(update={"endpoint": endpoint, "updated_at": now_utc()})
    main.app.state.postgres_repositories.mcp_child_servers.update(
        server=updated,
        audit_event=AuditEvent(
            event_type="mcp.child_server.updated",
            actor=user.email,
            entity_id=server.id,
            workspace_id=server.workspace_id,
            details={"project_id": server.project_id, "name": server.name},
        ),
        expected_updated_at=server.updated_at.isoformat(),
    )
    return updated


def test_postgres_mcp_pending_reads_are_workspace_scoped_and_do_not_project_to_sqlite(
    postgres_mcp_pending_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, workspace, project, user, pending = postgres_mcp_pending_route_runtime
    client = TestClient(main.app)
    before_pending = dict(store.mcp_pending_requests)
    before_approvals = dict(store.approval_requests)
    before_audits = {event.id for event in store.audit_events}
    before_child_calls = mcp_gateway_routes.child_received_count_for_test()

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
    assert mcp_gateway_routes.child_received_count_for_test() == before_child_calls


def test_postgres_mcp_submission_is_transactional_and_does_not_project_to_sqlite(
    postgres_mcp_pending_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, _workspace, project, _user, pending = postgres_mcp_pending_route_runtime
    # Keep this safety regression independent of app-state changes made by
    # other route/lifespan tests. monkeypatch restores every value on teardown.
    repositories = main.app.state.postgres_repositories
    runtime = SimpleNamespace(repositories=repositories)
    monkeypatch.setattr(
        mcp_gateway_routes,
        "get_settings",
        lambda: SimpleNamespace(postgres_auth_enabled=True),
    )
    monkeypatch.setattr(main.app.state, "postgres_repositories", repositories, raising=False)
    monkeypatch.setattr(main.app.state, "postgres_runtime", runtime, raising=False)
    for dependency in (mcp_gateway_routes.ReviewerDependency, mcp_gateway_routes.MaintainerDependency):
        monkeypatch.delitem(
            main.app.dependency_overrides,
            dependency.dependency,
            raising=False,
        )
        monkeypatch.setitem(
            main.app.dependency_overrides,
            dependency.dependency,
            lambda: _user,
        )
    assert mcp_gateway_routes.get_settings().postgres_auth_enabled is True
    assert main.app.state.postgres_runtime.repositories is main.app.state.postgres_repositories

    client = TestClient(main.app)
    before_pending = dict(store.mcp_pending_requests)
    before_approvals = dict(store.approval_requests)
    before_audits = {event.id for event in store.audit_events}
    before_child_calls = mcp_gateway_routes.child_received_count_for_test()

    missing_idempotency_key = client.post(
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
    assert missing_idempotency_key.status_code == 400
    blank_idempotency_key = client.post(
        "/mcp-gateway/requests",
        headers={"Idempotency-Key": "   "},
        json={
            "project_id": project.id,
            "request": {"server_name": "test-child", "tool_name": "write_file", "mutating": True},
        },
    )
    assert blank_idempotency_key.status_code == 400

    submitted = client.post(
        "/mcp-gateway/requests",
        headers={"Idempotency-Key": "mcp-call-001"},
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
    assert submitted.status_code == 200, submitted.text
    submitted_body = submitted.json()
    assert submitted_body["approval_required"] is True
    assert submitted_body["forwarded"] is False
    assert submitted_body["status"] == "REQUESTED"
    assert submitted_body["approval_request_id"]

    repeated = client.post(
        "/mcp-gateway/requests",
        headers={"Idempotency-Key": "mcp-call-001"},
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
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["approval_request_id"] == submitted_body["approval_request_id"]
    assert repeated.json()["reason"] == "MCP submission already exists for this idempotency key."

    with connect() as connection:
        approval_payload = connection.execute(
            "SELECT payload FROM approval_requests WHERE approval_id=%s",
            (submitted_body["approval_request_id"],),
        ).fetchone()[0]
        assert "README.md" in approval_payload["files"][0]["new_content"]
        notification_payload = connection.execute(
            "SELECT payload FROM notifications WHERE approval_id=%s",
            (submitted_body["approval_request_id"],),
        ).fetchone()[0]
        audit_details = connection.execute(
            "SELECT details FROM audit_events WHERE approval_id=%s",
            (submitted_body["approval_request_id"],),
        ).fetchone()[0]
    assert "README.md" not in notification_payload["body"]
    assert "README.md" not in str(audit_details)

    mismatched_replay = client.post(
        "/mcp-gateway/requests",
        headers={"Idempotency-Key": "mcp-call-001"},
        json={
            "project_id": project.id,
            "request": {
                "server_name": "test-child",
                "tool_name": "write_file",
                "arguments": {"path": "different.txt"},
                "requested_by": "test",
                "mutating": True,
            },
        },
    )
    assert mismatched_replay.status_code == 409

    foreign_project = client.post(
        "/mcp-gateway/requests",
        headers={"Idempotency-Key": "mcp-call-foreign-project"},
        json={
            "project_id": "project_from_another_workspace",
            "request": {"server_name": "test-child", "tool_name": "write_file", "mutating": True},
        },
    )
    assert foreign_project.status_code == 404

    # Retrying an already waiting request is an idempotent read-like no-op.
    retried = client.post(f"/mcp-gateway/pending-requests/{pending.id}/retry", json={})
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == MCPPendingStatus.WAITING_FOR_APPROVAL
    resolved = client.post(f"/mcp-gateway/approvals/{pending.approval_request_id}/resolve")
    assert resolved.status_code == 503

    with connect() as connection:
        assert connection.execute("SELECT count(*) FROM mcp_pending_requests").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM approval_requests").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM audit_events").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM notifications").fetchone()[0] == 1
    assert store.mcp_pending_requests == before_pending
    assert store.approval_requests == before_approvals
    assert {event.id for event in store.audit_events} == before_audits
    assert mcp_gateway_routes.child_received_count_for_test() == before_child_calls


def test_postgres_mcp_oversized_arguments_are_rejected_before_repository_access(
    postgres_mcp_pending_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, _workspace, project, _user, _pending = postgres_mcp_pending_route_runtime
    with connect() as connection:
        before_counts = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("mcp_pending_requests", "approval_requests", "audit_events", "notifications")
        }
    before_sqlite_pending = dict(store.mcp_pending_requests)
    before_sqlite_approvals = dict(store.approval_requests)
    monkeypatch.setattr(
        mcp_gateway_routes,
        "_postgres_pending_repository_for_request",
        lambda _request: pytest.fail("oversized arguments reached PostgreSQL repository selection"),
    )
    client = TestClient(main.app)
    response = client.post(
        "/mcp-gateway/requests",
        headers={"Idempotency-Key": "oversized-call"},
        json={
            "project_id": project.id,
            "request": {
                "server_name": "test-child",
                "tool_name": "write_file",
                "arguments": {"large": "x" * mcp_gateway_routes.MAX_MCP_ARGUMENTS_BYTES},
                "mutating": True,
            },
        },
    )
    assert response.status_code == 413, response.text
    with connect() as connection:
        after_counts = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in before_counts
        }
    assert after_counts == before_counts
    assert store.mcp_pending_requests == before_sqlite_pending
    assert store.approval_requests == before_sqlite_approvals


@pytest.mark.parametrize(
    ("input_schema", "arguments", "expected_status"),
    [
        (
            {
                "type": "object",
                "required": ["path", "content"],
                "additionalProperties": False,
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            },
            {"path": "valid.txt"},
            422,
        ),
        (
            {
                "type": "object",
                "required": ["path"],
                "additionalProperties": False,
                "properties": {"path": {"type": "string"}},
            },
            {"path": 17},
            422,
        ),
        (
            {
                "type": "object",
                "required": ["path"],
                "additionalProperties": False,
                "properties": {"path": {"type": "string"}},
            },
            {"path": "valid.txt", "extra": "rejected"},
            422,
        ),
        (
            {
                "type": "object",
                "required": ["path"],
                "properties": {"path": {"type": "string", "enum": ["safe.txt"]}},
            },
            {"path": "safe.txt"},
            422,
        ),
        (
            {
                "type": "object",
                "required": ["path", "content"],
                "additionalProperties": False,
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            },
            {"path": "valid.txt", "content": "approved content"},
            200,
        ),
    ],
)
def test_postgres_gateway_submission_enforces_registered_schema_before_persisting(
    postgres_mcp_pending_route_runtime,
    input_schema: dict,
    arguments: dict,
    expected_status: int,
) -> None:
    connect, _workspace, project, user, _pending = postgres_mcp_pending_route_runtime
    repositories = main.app.state.postgres_repositories
    server = repositories.mcp_child_servers.list_for_workspace(
        project.workspace_id,
        project_id=project.id,
        enabled=True,
    )[0]
    updated_server = server.model_copy(
        update={
            "tools": [server.tools[0].model_copy(update={"input_schema": input_schema})],
            "updated_at": now_utc(),
        }
    )
    repositories.mcp_child_servers.update(
        server=updated_server,
        expected_updated_at=server.updated_at.isoformat(),
        audit_event=AuditEvent(
            event_type="mcp.child_server.updated",
            actor=user.email,
            entity_id=server.id,
            workspace_id=server.workspace_id,
            details={"project_id": server.project_id, "name": server.name},
        ),
    )
    with connect() as connection:
        before = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("approval_requests", "mcp_pending_requests", "audit_events", "notifications")
        }
    before_sqlite = (
        dict(store.approval_requests),
        dict(store.mcp_pending_requests),
        {event.id for event in store.audit_events},
    )

    response = TestClient(main.app).post(
        "/mcp-gateway/requests",
        headers={"Idempotency-Key": f"schema-validation-{uuid.uuid4().hex}"},
        json={
            "project_id": project.id,
            "request": {
                "server_name": server.name,
                "tool_name": "write_file",
                "arguments": arguments,
                "requested_by": "schema-validation-test",
                "mutating": True,
            },
        },
    )

    assert response.status_code == expected_status, response.text
    with connect() as connection:
        after = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in before
        }
    if expected_status == 422:
        assert after == before
        assert response.json()["detail"]
    else:
        assert after["approval_requests"] == before["approval_requests"] + 1
        assert after["mcp_pending_requests"] == before["mcp_pending_requests"] + 1
        assert after["audit_events"] == before["audit_events"] + 1
        assert after["notifications"] == before["notifications"] + 1
    assert dict(store.approval_requests) == before_sqlite[0]
    assert dict(store.mcp_pending_requests) == before_sqlite[1]
    assert {event.id for event in store.audit_events} == before_sqlite[2]


@pytest.mark.parametrize("failed_table", ["audit_events", "notifications"])
def test_postgres_mcp_submission_rolls_back_all_rows_when_audit_or_notification_insert_fails(
    postgres_mcp_pending_route_runtime,
    failed_table: str,
) -> None:
    connect, _workspace, project, _user, _pending = postgres_mcp_pending_route_runtime
    # Table is selected from a fixed test-only allowlist above.
    with connect() as connection:
        connection.execute(
            f"CREATE FUNCTION fail_mcp_insert() RETURNS trigger LANGUAGE plpgsql AS $$ "
            f"BEGIN RAISE EXCEPTION 'forced {failed_table} insert failure'; END $$"
        )
        connection.execute(
            f"CREATE TRIGGER fail_mcp_insert BEFORE INSERT ON {failed_table} "
            "FOR EACH ROW EXECUTE FUNCTION fail_mcp_insert()"
        )
    client = TestClient(main.app)
    response = client.post(
        "/mcp-gateway/requests",
        headers={"Idempotency-Key": f"rollback-{failed_table}"},
        json={
            "project_id": project.id,
            "request": {
                "server_name": "test-child",
                "tool_name": "write_file",
                "arguments": {"path": "atomic.txt"},
                "requested_by": "test",
                "mutating": True,
            },
        },
    )
    assert response.status_code == 503
    with connect() as connection:
        assert connection.execute("SELECT count(*) FROM mcp_pending_requests").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM approval_requests").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM audit_events").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM notifications").fetchone()[0] == 0


def test_postgres_mcp_resolve_forwards_one_claim_and_persists_final_audit(
    postgres_mcp_pending_route_runtime,
) -> None:
    connect, _workspace, project, user, _pending = postgres_mcp_pending_route_runtime
    approval_id, server = _create_approved_mcp_forwarding_case(connect, project, user)
    approval = main.app.state.postgres_repositories.approval_decisions.get(approval_id)
    assert approval is not None
    approved_action = json.loads(approval.files[0].new_content)
    assert approved_action["target_server_id"] == server.id
    assert approved_action["target_server_revision"] == server.updated_at.isoformat()
    assert "endpoint" not in approved_action
    client = TestClient(main.app)
    before_calls = mcp_gateway_routes.child_received_count_for_server_for_test(server.name)

    resolved = client.post(f"/mcp-gateway/approvals/{approval_id}/resolve")

    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["forwarded"] is True
    assert resolved.json()["status"] == ApprovalStatus.APPROVED
    repository = main.app.state.postgres_repositories.mcp_pending_requests
    finished = repository.get_for_approval(approval_id)
    assert finished is not None
    assert finished.status == MCPPendingStatus.FORWARDED
    assert finished.attempts == 1
    assert mcp_gateway_routes.child_received_count_for_server_for_test(server.name) == before_calls + 1
    with connect() as connection:
        audit_types = connection.execute(
            "SELECT event_type FROM audit_events WHERE approval_id=%s ORDER BY created_at",
            (approval_id,),
        ).fetchall()
    assert any(row[0] == "mcp.forwarded_after_approval" for row in audit_types)

    replay = client.post(f"/mcp-gateway/approvals/{approval_id}/resolve")
    assert replay.status_code == 409
    assert mcp_gateway_routes.child_received_count_for_server_for_test(server.name) == before_calls + 1
    assert repository.get_for_approval(approval_id).attempts == 1


def test_postgres_mcp_resolve_rejects_unbound_action_before_child_call(
    postgres_mcp_pending_route_runtime,
) -> None:
    connect, _workspace, project, user, _pending = postgres_mcp_pending_route_runtime
    approval_id, server = _create_approved_mcp_forwarding_case(connect, project, user)
    repository = main.app.state.postgres_repositories.mcp_pending_requests
    pending = repository.get_for_approval(approval_id)
    assert pending is not None
    tampered = pending.model_copy(update={"arguments": {"path": "different.txt"}})
    with connect() as connection:
        connection.execute(
            "UPDATE mcp_pending_requests SET payload=%s WHERE pending_id=%s",
            (Jsonb(tampered.model_dump(mode="json")), pending.id),
        )
    before_calls = mcp_gateway_routes.child_received_count_for_server_for_test(server.name)

    response = TestClient(main.app).post(f"/mcp-gateway/approvals/{approval_id}/resolve")

    assert response.status_code in {409, 503}
    assert mcp_gateway_routes.child_received_count_for_server_for_test(server.name) == before_calls
    with connect() as connection:
        row = connection.execute(
            "SELECT status,attempts FROM mcp_pending_requests WHERE pending_id=%s",
            (pending.id,),
        ).fetchone()
    assert row == (MCPPendingStatus.WAITING_FOR_APPROVAL.value, 0)


def test_postgres_mcp_resolve_rejects_server_revision_changed_before_claim(
    postgres_mcp_pending_route_runtime,
) -> None:
    connect, _workspace, project, user, _pending = postgres_mcp_pending_route_runtime
    approval_id, server = _create_approved_mcp_forwarding_case(connect, project, user)
    _update_mcp_server_endpoint(server, user, "https://changed-before-claim.example/mcp")
    before_calls = mcp_gateway_routes.child_received_count_for_server_for_test(server.name)

    response = TestClient(main.app).post(f"/mcp-gateway/approvals/{approval_id}/resolve")

    assert response.status_code == 409
    pending = main.app.state.postgres_repositories.mcp_pending_requests.get_for_approval(approval_id)
    assert pending is not None
    assert pending.status == MCPPendingStatus.WAITING_FOR_APPROVAL
    assert pending.attempts == 0
    assert mcp_gateway_routes.child_received_count_for_server_for_test(server.name) == before_calls


def test_postgres_mcp_resolve_dispatches_claimed_server_snapshot_after_registry_change(
    postgres_mcp_pending_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, _workspace, project, user, _pending = postgres_mcp_pending_route_runtime
    original_endpoint = "https://approved-target.example/mcp"
    changed_endpoint = "https://retargeted.example/mcp"
    approval_id, server = _create_approved_mcp_forwarding_case(
        connect,
        project,
        user,
        transport="http",
        endpoint=original_endpoint,
    )
    pending_repository = main.app.state.postgres_repositories.mcp_pending_requests
    claim_forwarding = pending_repository.claim_forwarding
    dispatched_endpoints: list[str] = []

    def claim_then_change_registry(**kwargs):
        claimed = claim_forwarding(**kwargs)
        assert claimed is not None
        _update_mcp_server_endpoint(server, user, changed_endpoint)
        return claimed

    def record_dispatch(child_server, _call):
        dispatched_endpoints.append(child_server.endpoint)
        return {"ok": True, "server_name": child_server.name, "tool_name": "write_file"}

    monkeypatch.setattr(pending_repository, "claim_forwarding", claim_then_change_registry)
    monkeypatch.setattr(
        mcp_gateway_routes.MCPChildServerRouter,
        "_call_http_server",
        staticmethod(record_dispatch),
    )

    response = TestClient(main.app).post(f"/mcp-gateway/approvals/{approval_id}/resolve")

    assert response.status_code == 200, response.text
    assert dispatched_endpoints == [original_endpoint]
    current_server = main.app.state.postgres_repositories.mcp_child_servers.get(server.id)
    assert current_server is not None
    assert current_server.endpoint == changed_endpoint


def test_postgres_mcp_resolve_marks_failed_child_response_unknown_and_never_replays(
    postgres_mcp_pending_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, _workspace, project, user, _pending = postgres_mcp_pending_route_runtime
    approval_id, server = _create_approved_mcp_forwarding_case(connect, project, user)
    calls = 0
    before_child_calls = mcp_gateway_routes.child_received_count_for_server_for_test(server.name)

    def child_effect_then_lost_response(_server, _call):
        nonlocal calls
        calls += 1
        return {"ok": False, "error": "response lost after dispatch"}

    monkeypatch.setattr(mcp_gateway_routes._child_router, "_call_test_server", child_effect_then_lost_response)
    client = TestClient(main.app)

    first = client.post(f"/mcp-gateway/approvals/{approval_id}/resolve")

    assert first.status_code == 503
    repository = main.app.state.postgres_repositories.mcp_pending_requests
    pending = repository.get_for_approval(approval_id)
    assert pending is not None
    assert pending.status == MCPPendingStatus.DEAD_LETTER
    assert pending.last_error == mcp_gateway_routes.MCP_PENDING_FORWARD_OUTCOME_UNKNOWN
    assert pending.attempts == 1
    with connect() as connection:
        audit_types = connection.execute(
            "SELECT event_type FROM audit_events WHERE approval_id=%s",
            (approval_id,),
        ).fetchall()
    assert any(row[0] == "mcp.forward_outcome_unknown" for row in audit_types)

    replay = client.post(f"/mcp-gateway/approvals/{approval_id}/resolve")
    assert replay.status_code == 409
    assert calls == 1
    assert mcp_gateway_routes.child_received_count_for_server_for_test(server.name) == before_child_calls


def test_postgres_mcp_concurrent_idempotent_submissions_create_one_complete_record_set(
    postgres_mcp_pending_route_runtime,
) -> None:
    connect, _workspace, project, _user, _pending = postgres_mcp_pending_route_runtime
    barrier = Barrier(2)
    payload = {
        "project_id": project.id,
        "request": {
            "server_name": "test-child",
            "tool_name": "write_file",
            "arguments": {"path": "concurrent.txt"},
            "requested_by": "test",
            "mutating": True,
        },
    }
    client = TestClient(main.app)

    def submit() -> object:
        barrier.wait(timeout=5)
        return client.post(
            "/mcp-gateway/requests",
            headers={"Idempotency-Key": "concurrent-mcp-call"},
            json=payload,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _index: submit(), range(2)))
    assert [response.status_code for response in responses] == [200, 200], [response.text for response in responses]
    bodies = [response.json() for response in responses]
    assert bodies[0]["approval_request_id"] == bodies[1]["approval_request_id"]
    with connect() as connection:
        assert connection.execute("SELECT count(*) FROM mcp_pending_requests").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM approval_requests").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM audit_events").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM notifications").fetchone()[0] == 1


def test_postgres_mcp_idempotency_key_isolated_between_workspaces(
    postgres_mcp_pending_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, _workspace_a, _project_a, user_a, _pending = postgres_mcp_pending_route_runtime
    workspace_b = Workspace(
        id=f"workspace_{uuid.uuid4().hex}",
        name="Second MCP workspace",
        owner_user_id=f"user_{uuid.uuid4().hex}",
    )
    project_b = Project(
        id=f"project_{uuid.uuid4().hex}",
        name="Second MCP project",
        workspace_id=workspace_b.id,
    )
    child_server_b = MCPChildServer(
        id=f"mcp_server_{uuid.uuid4().hex}",
        project_id=project_b.id,
        workspace_id=workspace_b.id,
        name="test-child",
        transport="test",
        enabled=True,
        tools=[MCPToolDefinition(name="write_file", mutating=True)],
    )
    user_b = AuthUser(
        id=workspace_b.owner_user_id,
        email=f"mcp-owner-{uuid.uuid4().hex}@example.test",
        display_name="Second MCP Owner",
        role=UserRole.OWNER,
        workspace_id=workspace_b.id,
    )
    persisted_user_b = User(
        id=user_b.id,
        email=user_b.email,
        display_name=user_b.display_name,
        role=user_b.role,
        password_hash="test-password-hash",
        password_salt="test-password-salt",
        workspace_id=workspace_b.id,
        email_verified=True,
    )
    with connect() as connection:
        connection.execute(
            "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
            (workspace_b.id, Jsonb(workspace_b.model_dump(mode="json"))),
        )
        connection.execute(
            "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,email_verified,payload) "
            "VALUES(%s,%s,%s,%s,%s,FALSE,TRUE,%s)",
            (
                persisted_user_b.id,
                workspace_b.id,
                persisted_user_b.email,
                persisted_user_b.email.lower(),
                persisted_user_b.role.value,
                Jsonb(persisted_user_b.model_dump(mode="json")),
            ),
        )
        connection.execute(
            "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
            (project_b.id, workspace_b.id, Jsonb(project_b.model_dump(mode="json"))),
        )
        connection.execute(
            "INSERT INTO mcp_child_servers(server_id,project_id,workspace_id,name,enabled,"
            "created_at,updated_at,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                child_server_b.id,
                child_server_b.project_id,
                child_server_b.workspace_id,
                child_server_b.name,
                child_server_b.enabled,
                child_server_b.created_at,
                child_server_b.updated_at,
                Jsonb(child_server_b.model_dump(mode="json")),
            ),
        )
    for dependency in (mcp_gateway_routes.ReviewerDependency, mcp_gateway_routes.MaintainerDependency):
        monkeypatch.setitem(main.app.dependency_overrides, dependency.dependency, lambda: user_a)
    client = TestClient(main.app)
    body = {
        "project_id": _project_a.id,
        "request": {"server_name": "test-child", "tool_name": "write_file", "arguments": {"path": "same.txt"}, "mutating": True},
    }
    first = client.post("/mcp-gateway/requests", headers={"Idempotency-Key": "shared-key"}, json=body)
    assert first.status_code == 200, first.text
    for dependency in (mcp_gateway_routes.ReviewerDependency, mcp_gateway_routes.MaintainerDependency):
        monkeypatch.setitem(main.app.dependency_overrides, dependency.dependency, lambda: user_b)
    body["project_id"] = project_b.id
    second = client.post("/mcp-gateway/requests", headers={"Idempotency-Key": "shared-key"}, json=body)
    assert second.status_code == 200, second.text
    assert second.json()["approval_request_id"] != first.json()["approval_request_id"]
    with connect() as connection:
        assert connection.execute("SELECT count(*) FROM mcp_pending_requests").fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM approval_requests").fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM audit_events").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM notifications").fetchone()[0] == 2


def test_postgres_mcp_pending_primary_key_race_rolls_back_the_losing_approval(
    postgres_mcp_pending_route_runtime,
) -> None:
    connect, _workspace, project, _user, _pending = postgres_mcp_pending_route_runtime
    with connect() as connection:
        connection.execute(
            "CREATE FUNCTION duplicate_mcp_pending_before_insert() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN "
            "IF pg_trigger_depth() > 1 THEN RETURN NEW; END IF; "
            "INSERT INTO mcp_pending_requests(pending_id,approval_id,project_id,workspace_id,status,attempts,"
            "lease_expires_at,created_at,updated_at,payload) VALUES(NEW.pending_id,NEW.approval_id,"
            "NEW.project_id,NEW.workspace_id,NEW.status,NEW.attempts,NEW.lease_expires_at,NEW.created_at,"
            "NEW.updated_at,NEW.payload); RETURN NEW; END $$"
        )
        connection.execute(
            "CREATE TRIGGER duplicate_mcp_pending_before_insert BEFORE INSERT ON mcp_pending_requests "
            "FOR EACH ROW EXECUTE FUNCTION duplicate_mcp_pending_before_insert()"
        )
    client = TestClient(main.app)
    response = client.post(
        "/mcp-gateway/requests",
        headers={"Idempotency-Key": "forced-pending-pk-race"},
        json={
            "project_id": project.id,
            "request": {
                "server_name": "test-child",
                "tool_name": "write_file",
                "arguments": {"path": "rollback.txt"},
                "requested_by": "test",
                "mutating": True,
            },
        },
    )
    assert response.status_code == 409, response.text
    with connect() as connection:
        assert connection.execute("SELECT count(*) FROM mcp_pending_requests").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM approval_requests").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM audit_events").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM notifications").fetchone()[0] == 0


def test_postgres_mcp_retry_dead_letter_is_atomic_and_idempotent(
    postgres_mcp_pending_route_runtime,
) -> None:
    connect, _workspace, _project, _user, pending = postgres_mcp_pending_route_runtime
    dead_letter = pending.model_copy(update={
        "status": MCPPendingStatus.DEAD_LETTER,
        "attempts": 2,
        "last_error": "temporary transport failure",
        "updated_at": now_utc(),
    })
    with connect() as connection:
        connection.execute(
            "UPDATE mcp_pending_requests SET status=%s,attempts=%s,last_error=%s,updated_at=%s,payload=%s "
            "WHERE pending_id=%s",
            (
                dead_letter.status.value,
                dead_letter.attempts,
                dead_letter.last_error,
                dead_letter.updated_at,
                Jsonb(dead_letter.model_dump(mode="json")),
                dead_letter.id,
            ),
        )

    before_sqlite_pending = dict(store.mcp_pending_requests)
    before_sqlite_approvals = dict(store.approval_requests)
    before_sqlite_audits = {event.id for event in store.audit_events}
    client = TestClient(main.app)
    payload = {"reason": "retry after transient failure", "reset_attempts": False}
    first = client.post(f"/mcp-gateway/pending-requests/{pending.id}/retry", json=payload)
    second = client.post(f"/mcp-gateway/pending-requests/{pending.id}/retry", json=payload)

    assert first.status_code == 200, first.text
    assert first.json()["status"] == MCPPendingStatus.WAITING_FOR_APPROVAL
    assert first.json()["attempts"] == 2
    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    with connect() as connection:
        row = connection.execute(
            "SELECT status,attempts,last_error,payload FROM mcp_pending_requests WHERE pending_id=%s",
            (pending.id,),
        ).fetchone()
        assert row is not None
        assert row[0] == MCPPendingStatus.WAITING_FOR_APPROVAL.value
        assert row[1] == 2
        assert row[2] is None
        events = connection.execute(
            "SELECT event_type,details FROM audit_events WHERE approval_id=%s",
            (pending.approval_request_id,),
        ).fetchall()
        assert len(events) == 1
        assert events[0][0] == "MCP_PENDING_RETRIED"
        assert events[0][1]["mcp_pending_request_id"] == pending.id
        assert connection.execute("SELECT count(*) FROM notifications").fetchone()[0] == 0
    assert store.mcp_pending_requests == before_sqlite_pending
    assert store.approval_requests == before_sqlite_approvals
    assert {event.id for event in store.audit_events} == before_sqlite_audits


def test_postgres_mcp_retry_hides_cross_workspace_request(
    postgres_mcp_pending_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, _workspace, _project, _user, pending = postgres_mcp_pending_route_runtime
    monkeypatch.setattr(
        mcp_gateway_routes,
        "workspace_id_for_request",
        lambda _user, _request: "workspace_not_owner",
    )
    before_sqlite_pending = dict(store.mcp_pending_requests)

    response = TestClient(main.app).post(
        f"/mcp-gateway/pending-requests/{pending.id}/retry",
        json={"reason": "should not be visible"},
    )

    assert response.status_code == 404
    with connect() as connection:
        row = connection.execute(
            "SELECT status,attempts FROM mcp_pending_requests WHERE pending_id=%s",
            (pending.id,),
        ).fetchone()
        assert row == (MCPPendingStatus.WAITING_FOR_APPROVAL.value, pending.attempts)
        assert connection.execute("SELECT count(*) FROM audit_events").fetchone()[0] == 0
    assert store.mcp_pending_requests == before_sqlite_pending


def test_postgres_mcp_retry_rolls_back_pending_when_audit_insert_fails(
    postgres_mcp_pending_route_runtime,
) -> None:
    connect, _workspace, _project, _user, pending = postgres_mcp_pending_route_runtime
    dead_letter = pending.model_copy(update={
        "status": MCPPendingStatus.DEAD_LETTER,
        "attempts": 1,
        "last_error": "temporary transport failure",
        "updated_at": now_utc(),
    })
    with connect() as connection:
        connection.execute(
            "UPDATE mcp_pending_requests SET status=%s,attempts=%s,last_error=%s,updated_at=%s,payload=%s "
            "WHERE pending_id=%s",
            (
                dead_letter.status.value,
                dead_letter.attempts,
                dead_letter.last_error,
                dead_letter.updated_at,
                Jsonb(dead_letter.model_dump(mode="json")),
                dead_letter.id,
            ),
        )
        connection.execute(
            "CREATE FUNCTION reject_mcp_retry_audit() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'injected MCP retry audit failure'; END; $$"
        )
        connection.execute(
            "CREATE TRIGGER reject_mcp_retry_audit BEFORE INSERT ON audit_events "
            "FOR EACH ROW EXECUTE FUNCTION reject_mcp_retry_audit()"
        )

    response = TestClient(main.app).post(
        f"/mcp-gateway/pending-requests/{pending.id}/retry",
        json={"reason": "rollback check"},
    )

    assert response.status_code == 503
    with connect() as connection:
        row = connection.execute(
            "SELECT status,attempts,last_error FROM mcp_pending_requests WHERE pending_id=%s",
            (pending.id,),
        ).fetchone()
        assert row == (
            MCPPendingStatus.DEAD_LETTER.value,
            dead_letter.attempts,
            dead_letter.last_error,
        )
        assert connection.execute("SELECT count(*) FROM audit_events").fetchone()[0] == 0


def test_postgres_mcp_retry_expired_forwarding_lease_is_never_replayed(
    postgres_mcp_pending_route_runtime,
) -> None:
    connect, _workspace, _project, _user, pending = postgres_mcp_pending_route_runtime
    expired = pending.model_copy(update={
        "status": MCPPendingStatus.FORWARDING,
        "attempts": 1,
        "lease_owner": "mcp-worker",
        "lease_expires_at": now_utc() - timedelta(minutes=1),
        "updated_at": now_utc() - timedelta(minutes=1),
    })
    with connect() as connection:
        connection.execute(
            "UPDATE mcp_pending_requests SET status=%s,attempts=%s,lease_owner=%s,lease_expires_at=%s,"
            "updated_at=%s,payload=%s WHERE pending_id=%s",
            (
                expired.status.value,
                expired.attempts,
                expired.lease_owner,
                expired.lease_expires_at,
                expired.updated_at,
                Jsonb(expired.model_dump(mode="json")),
                expired.id,
            ),
        )

    before_sqlite_pending = dict(store.mcp_pending_requests)
    before_child_calls = mcp_gateway_routes.child_received_count_for_test()
    response = TestClient(main.app).post(
        f"/mcp-gateway/pending-requests/{pending.id}/retry",
        json={"reason": "expired lease"},
    )

    assert response.status_code == 409
    with connect() as connection:
        row = connection.execute(
            "SELECT status,lease_owner,lease_expires_at,last_error FROM mcp_pending_requests "
            "WHERE pending_id=%s",
            (pending.id,),
        ).fetchone()
        assert row == (
            MCPPendingStatus.DEAD_LETTER.value,
            None,
            None,
            "FORWARD_OUTCOME_UNKNOWN",
        )
        assert connection.execute(
            "SELECT count(*) FROM audit_events WHERE approval_id=%s AND event_type='mcp.forward_outcome_unknown'",
            (pending.approval_request_id,),
        ).fetchone()[0] == 1
    assert store.mcp_pending_requests == before_sqlite_pending
    assert mcp_gateway_routes.child_received_count_for_test() == before_child_calls


@pytest.mark.parametrize("binding_failure", ["missing_repositories", "mismatched_runtime"])
def test_postgres_mcp_retry_and_resolve_fail_closed_for_missing_or_mixed_bundle(
    postgres_mcp_pending_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
    binding_failure: str,
) -> None:
    connect, _workspace, _project, _user, pending = postgres_mcp_pending_route_runtime
    if binding_failure == "missing_repositories":
        monkeypatch.setattr(main.app.state, "postgres_repositories", None, raising=False)
    else:
        monkeypatch.setattr(
            main.app.state,
            "postgres_runtime",
            SimpleNamespace(repositories=object()),
            raising=False,
        )

    before_pending = dict(store.mcp_pending_requests)
    before_approvals = dict(store.approval_requests)
    before_audits = {event.id for event in store.audit_events}
    client = TestClient(main.app)

    retried = client.post(f"/mcp-gateway/pending-requests/{pending.id}/retry", json={})
    resolved = client.post(f"/mcp-gateway/approvals/{pending.approval_request_id}/resolve")

    assert retried.status_code == 503
    assert resolved.status_code == 503
    with connect() as connection:
        row = connection.execute(
            "SELECT status,attempts,payload FROM mcp_pending_requests WHERE pending_id=%s",
            (pending.id,),
        ).fetchone()
        assert row is not None
        assert row[0] == MCPPendingStatus.WAITING_FOR_APPROVAL.value
        assert row[1] == pending.attempts
        assert connection.execute("SELECT count(*) FROM audit_events").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM notifications").fetchone()[0] == 0
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


def test_production_pending_read_does_not_select_process_local_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ForbiddenLocalMap:
        def values(self):
            raise AssertionError("production MCP pending read touched process-local state")

    user = AuthUser(
        id="production-mcp-reader",
        email="production-mcp-reader@example.test",
        display_name="Production MCP Reader",
        role=UserRole.OWNER,
        workspace_id="production-mcp-workspace",
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    monkeypatch.setattr(
        mcp_gateway_routes,
        "get_settings",
        lambda: SimpleNamespace(is_production=True, postgres_auth_enabled=False),
    )
    monkeypatch.setattr(store, "mcp_pending_requests", ForbiddenLocalMap())

    with pytest.raises(HTTPException) as raised:
        mcp_gateway_routes.list_pending_gateway_requests(user=user, request=request)

    assert raised.value.status_code == 503
    assert raised.value.headers == {"Retry-After": "1"}
    assert raised.value.detail == (
        "Production MCP gateway requires an active PostgreSQL runtime "
        "with PostgreSQL-backed authentication"
    )


def test_postgres_mcp_rejects_sqlite_pending_repository_in_mixed_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A matching runtime pointer does not make a SQLite adapter PostgreSQL-authoritative."""
    mixed = object.__new__(ApplicationRepositories)
    object.__setattr__(mixed, "mcp_pending_requests", SQLiteMCPPendingRepository(store._connect))
    object.__setattr__(mixed, "projects", PostgresProjectRepository(lambda: None))
    object.__setattr__(
        mixed,
        "approval_decisions",
        PostgresApprovalDecisionRepository(lambda: None),
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                postgres_runtime=SimpleNamespace(repositories=mixed),
                postgres_repositories=mixed,
            )
        )
    )
    monkeypatch.setattr(
        mcp_gateway_routes,
        "get_settings",
        lambda: SimpleNamespace(postgres_auth_enabled=True),
    )

    with pytest.raises(HTTPException, match="PostgreSQL MCP submission repository bundle is incomplete"):
        mcp_gateway_routes._postgres_pending_repository_for_request(request)


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


@pytest.mark.parametrize("mode", ["production", "postgres_auth"])
@pytest.mark.parametrize(
    "route_name",
    ["submit", "list", "health", "get", "retry", "resolve"],
)
def test_durable_mode_direct_mcp_routes_require_app_request_before_local_store_access(
    route_name: str,
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Optional Request defaults must not expose the local store to in-process callers."""

    class ForbiddenLocalMap(dict):
        def _forbidden(self, *_args, **_kwargs):
            raise AssertionError("MCP PostgreSQL mode accessed process-local persistence")

        __getitem__ = _forbidden
        __contains__ = _forbidden
        get = _forbidden
        values = _forbidden
        items = _forbidden
        keys = _forbidden
        __iter__ = _forbidden

    monkeypatch.setattr(
        mcp_gateway_routes,
        "get_settings",
        lambda: SimpleNamespace(
            is_production=mode == "production",
            postgres_auth_enabled=mode == "postgres_auth",
        ),
    )
    for field in ("projects", "mcp_pending_requests", "approval_requests", "audit_events"):
        monkeypatch.setattr(store, field, ForbiddenLocalMap(), raising=True)

    user = AuthUser(
        id="postgres-mcp-no-context-user",
        email="mcp-no-context@example.test",
        display_name="MCP No Context",
        role=UserRole.OWNER,
        workspace_id="postgres-mcp-no-context-workspace",
    )
    with pytest.raises(HTTPException) as error:
        if route_name == "submit":
            mcp_gateway_routes.submit_gateway_request(
                payload=mcp_gateway_routes.GatewaySubmitRequest(
                    project_id="postgres-mcp-no-context-project",
                    request=mcp_gateway_routes.MCPGatewayToolCall(
                        tool_name="write_file",
                        arguments={"path": "x", "content": "y"},
                    ),
                ),
                user=user,
            )
        elif route_name == "list":
            mcp_gateway_routes.list_pending_gateway_requests(user=user)
        elif route_name == "health":
            mcp_gateway_routes.pending_gateway_health(user=user)
        elif route_name == "get":
            mcp_gateway_routes.get_pending_gateway_request("missing", user=user)
        elif route_name == "retry":
            mcp_gateway_routes.retry_pending_gateway_request("missing", user=user)
        else:
            mcp_gateway_routes.resolve_gateway_request("missing", user=user)

    assert error.value.status_code == 503
    assert error.value.detail == "MCP route requires an app-bound request context in production/PostgreSQL mode"
    assert error.value.headers == {"Retry-After": "1"}


def test_local_direct_mcp_route_keeps_compatibility_store_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mcp_gateway_routes,
        "get_settings",
        lambda: SimpleNamespace(is_production=False, postgres_auth_enabled=False),
    )
    mcp_gateway_routes._require_mcp_request_context(None)


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
