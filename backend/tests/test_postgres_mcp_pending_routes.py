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


def test_postgres_mcp_mutation_routes_fail_closed_without_sqlite_projection(
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

    # Retrying an already waiting request is an idempotent read-like no-op.
    retried = client.post(f"/mcp-gateway/pending-requests/{pending.id}/retry", json={})
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == MCPPendingStatus.WAITING_FOR_APPROVAL
    resolved = client.post(f"/mcp-gateway/approvals/{pending.approval_request_id}/resolve")
    assert resolved.status_code == 503

    with connect() as connection:
        assert connection.execute("SELECT count(*) FROM mcp_pending_requests").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM approval_requests").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM audit_events").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM notifications").fetchone()[0] == 0
    assert store.mcp_pending_requests == before_pending
    assert store.approval_requests == before_approvals
    assert {event.id for event in store.audit_events} == before_audits
    assert mcp_gateway_routes.child_received_count_for_test() == before_child_calls


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
