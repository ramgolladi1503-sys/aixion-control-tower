from __future__ import annotations

import os
import hashlib
import importlib.util
import multiprocessing
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

import psycopg
import pytest
import httpx
from fastapi.testclient import TestClient
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app import main
from app.agent_auth import revoke_external_agent_token
from app.agent_credential_models import AgentCredentialRecord
from app.application_repositories import ApplicationRepositories
from app.models import (
    AgentAction,
    AgentAuthType,
    ApprovalRequest,
    ApprovalStatus,
    ExternalAgent,
    AgentProvider,
    AuthUser,
    AuditEvent,
    FileChange,
    Notification,
    Project,
    RiskAssessment,
    RiskLevel,
    User,
    UserRole,
    Workspace,
    new_id,
)
from app.postgres_database import run_postgres_migrations
from app.store import store

ANTIGRAVITY_HOOK_PATH = (
    Path(__file__).parents[2] / "integrations" / "antigravity" / "aixion_pre_tool_hook.py"
)
ANTIGRAVITY_HOOK_SPEC = importlib.util.spec_from_file_location(
    "antigravity_postgres_route_hook", ANTIGRAVITY_HOOK_PATH
)
assert ANTIGRAVITY_HOOK_SPEC and ANTIGRAVITY_HOOK_SPEC.loader
antigravity_hook = importlib.util.module_from_spec(ANTIGRAVITY_HOOK_SPEC)
ANTIGRAVITY_HOOK_SPEC.loader.exec_module(antigravity_hook)


@pytest.fixture
def postgres_approval_route_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Callable[[], Connection], Workspace, Project, AuthUser, object, object, str, str]]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL approval route tests")

    schema = f"ct_approval_routes_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    try:
        run_postgres_migrations(connect)
        workspace_id = f"workspace_{uuid.uuid4().hex}"
        workspace = Workspace(
            id=workspace_id,
            name="Approval route integration workspace",
            owner_user_id=f"user_{uuid.uuid4().hex}",
        )
        persisted_user = User(
            id=workspace.owner_user_id,
            email=f"reviewer-{uuid.uuid4().hex}@example.test",
            display_name="Reviewer",
            role=UserRole.OWNER,
            password_hash="0" * 64,
            password_salt="0" * 32,
            workspace_id=workspace.id,
        )
        project = Project(
            id=f"project_{uuid.uuid4().hex}",
            name="Approval route integration project",
            workspace_id=workspace.id,
            github_repo="example/approval-route-flow",
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
        state_bindings = {
            "approval_decision_repository": repositories.approval_decisions,
            "project_repository": repositories.projects,
            "workspace_membership_repository": repositories.workspace_membership,
            "work_order_repository": repositories.work_orders,
            "agent_task_repository": repositories.agent_tasks,
            "external_agent_auth_repository": repositories.external_agent_auth,
        }
        for name, value in state_bindings.items():
            monkeypatch.setattr(main.app.state, name, value, raising=False)

        user = AuthUser(
            id=workspace.owner_user_id,
            email="reviewer@example.test",
            display_name="Reviewer",
            role=UserRole.OWNER,
            workspace_id=workspace.id,
        )
        agent_token = f"postgres-route-token-{uuid.uuid4().hex}"
        agent = ExternalAgent(
            id=new_id("agent"),
            provider=AgentProvider.ANTIGRAVITY,
            display_name="Antigravity route test",
            auth_type=AgentAuthType.API_KEY,
            allowed_project_ids=[project.id],
            allowed_actions=[
                AgentAction.CREATE_APPROVAL,
                AgentAction.READ_APPROVAL_DECISION,
                AgentAction.CONSUME_APPROVAL_DECISION,
            AgentAction.REPORT_APPROVAL_DELIVERY_FAILURE,
            AgentAction.EXPIRE_OWN_APPROVAL,
            ],
            created_by_user_id=user.id,
            workspace_id=workspace.id,
            secret_hash=hashlib.sha256(agent_token.encode("utf-8")).hexdigest(),
        )
        credential = AgentCredentialRecord(agent_id=agent.id)
        repositories.external_agent_auth.create(
            agent=agent,
            credential=credential,
            audit_event=AuditEvent(
                event_type="agent.registered",
                entity_id=agent.id,
                workspace_id=workspace.id,
                actor=f"user:{user.id}",
                details={
                    "provider": agent.provider,
                    "display_name": agent.display_name,
                    "auth_type": agent.auth_type,
                    "allowed_project_ids": agent.allowed_project_ids,
                    "allowed_repositories": agent.allowed_repositories,
                    "allowed_actions": agent.allowed_actions,
                },
            ),
        )
        monkeypatch.setattr(
            main,
            "_workspace_id_for_route",
            lambda _user, _request: workspace.id,
        )
        monkeypatch.setitem(
            main.app.dependency_overrides,
            main.ReviewerDependency.dependency,
            lambda: user,
        )
        yield connect, workspace, project, user, repositories, agent, agent_token, schema
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _race_postgres_approval_http_decision(
    dsn: str,
    schema: str,
    approval_id: str,
    workspace_id: str,
    user_id: str,
    user_email: str,
    decision: str,
    barrier: multiprocessing.synchronize.Barrier,
    results: multiprocessing.queues.Queue,
) -> None:
    """Issue one HTTP decision from an independent app process."""
    try:
        def connect() -> Connection:
            connection = psycopg.connect(dsn)
            connection.execute(
                sql.SQL("SET search_path TO {}").format(sql.Identifier(schema))
            )
            return connection

        repositories = ApplicationRepositories.from_connector(connect)
        for name, value in {
            "approval_decision_repository": repositories.approval_decisions,
            "project_repository": repositories.projects,
            "workspace_membership_repository": repositories.workspace_membership,
            "work_order_repository": repositories.work_orders,
            "agent_task_repository": repositories.agent_tasks,
            "agent_run_repository": repositories.agent_runs,
        }.items():
            setattr(main.app.state, name, value)
        user = AuthUser(
            id=user_id,
            email=user_email,
            display_name="Concurrent reviewer",
            role=UserRole.OWNER,
            workspace_id=workspace_id,
        )
        main.app.dependency_overrides[main.ReviewerDependency.dependency] = lambda: user
        barrier.wait(timeout=20)
        with TestClient(main.app) as client:
            response = client.post(
                f"/approvals/{approval_id}/decision",
                json={"decision": decision, "reason": "concurrent route test"},
            )
        results.put((response.status_code, response.text[:500]))
    except BaseException as error:
        results.put((0, f"{type(error).__name__}: {error}"))


def _retry_antigravity_hook_in_fresh_process(
    dsn: str,
    schema: str,
    agent_id: str,
    agent_token: str,
    project_id: str,
    payload: dict,
    results: multiprocessing.queues.Queue,
) -> None:
    """Load persisted connector state in a fresh API process and retry the hook."""
    try:
        def connect() -> Connection:
            connection = psycopg.connect(dsn)
            connection.execute(
                sql.SQL("SET search_path TO {}").format(sql.Identifier(schema))
            )
            return connection

        repositories = ApplicationRepositories.from_connector(connect)
        for name, value in {
            "approval_decision_repository": repositories.approval_decisions,
            "external_agent_auth_repository": repositories.external_agent_auth,
            "project_repository": repositories.projects,
            "workspace_membership_repository": repositories.workspace_membership,
        }.items():
            setattr(main.app.state, name, value)

        client = TestClient(main.app)

        def request(
            method: str,
            url: str,
            request_agent_id: str,
            request_token: str,
            body: dict | None = None,
        ) -> dict:
            if request_agent_id != agent_id or request_token != agent_token:
                raise antigravity_hook.GateError("Unexpected connector identity")
            response = client.request(
                method,
                urlparse(url).path,
                headers={
                    "X-Aixion-Agent-Id": agent_id,
                    "X-Aixion-Agent-Token": agent_token,
                    "X-Aixion-Approval-Protocol": "1",
                },
                json=body,
            )
            if response.status_code >= 400:
                raise antigravity_hook.GateError(
                    f"Control Tower returned HTTP {response.status_code}"
                )
            return response.json()

        environment = {
            "AIXION_API_BASE_URL": "http://localhost:8000",
            "AIXION_AGENT_ID": agent_id,
            "AIXION_AGENT_TOKEN": agent_token,
            "AIXION_ALLOW_AGENT_TOKEN_ENV": "true",
            "AIXION_PROJECT_ID": project_id,
            "AIXION_APPROVAL_TIMEOUT_SECONDS": "5",
        }
        with patch.object(antigravity_hook, "_request", side_effect=request), patch.object(
            antigravity_hook.time, "sleep"
        ):
            retry = antigravity_hook.run(payload, environment)
        client.close()
        results.put(("ok", retry))
    except BaseException as error:
        results.put(("error", f"{type(error).__name__}: {error}"))


def _serve_postgres_approval_http_api(
    dsn: str,
    schema: str,
    reviewer_id: str,
    reviewer_email: str,
    workspace_id: str,
    status_queue: multiprocessing.queues.Queue,
    stop_event: multiprocessing.synchronize.Event,
) -> None:
    """Run one real Uvicorn process bound only to an ephemeral loopback port."""
    import asyncio
    import socket

    import uvicorn

    listener: socket.socket | None = None
    status_reported = False
    try:
        def connect() -> Connection:
            connection = psycopg.connect(dsn)
            connection.execute(
                sql.SQL("SET search_path TO {}").format(sql.Identifier(schema))
            )
            return connection

        repositories = ApplicationRepositories.from_connector(connect)
        for name, value in {
            "approval_decision_repository": repositories.approval_decisions,
            "project_repository": repositories.projects,
            "workspace_membership_repository": repositories.workspace_membership,
            "work_order_repository": repositories.work_orders,
            "agent_task_repository": repositories.agent_tasks,
            "agent_run_repository": repositories.agent_runs,
            "external_agent_auth_repository": repositories.external_agent_auth,
        }.items():
            setattr(main.app.state, name, value)

        reviewer = AuthUser(
            id=reviewer_id,
            email=reviewer_email,
            display_name="Restart regression reviewer",
            role=UserRole.OWNER,
            workspace_id=workspace_id,
        )
        main.app.dependency_overrides[main.ReviewerDependency.dependency] = (
            lambda: reviewer
        )
        main._workspace_id_for_route = lambda user, _request: user.workspace_id

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        config = uvicorn.Config(
            main.app,
            host="127.0.0.1",
            port=port,
            lifespan="on",
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)

        async def serve_until_stopped() -> None:
            nonlocal status_reported
            server_task = asyncio.create_task(server.serve(sockets=[listener]))
            startup_deadline = asyncio.get_running_loop().time() + 20
            while (
                not server.started
                and not server_task.done()
                and asyncio.get_running_loop().time() < startup_deadline
            ):
                await asyncio.sleep(0.02)
            if not server.started:
                status_queue.put(("error", "Uvicorn did not complete lifespan startup"))
                status_reported = True
                server.should_exit = True
                await server_task
                return

            status_queue.put(("ready", port))
            status_reported = True
            await asyncio.to_thread(stop_event.wait)
            server.should_exit = True
            await server_task

        asyncio.run(serve_until_stopped())
    except BaseException as error:
        if not status_reported:
            status_queue.put(("error", f"{type(error).__name__}: {error}"))
    finally:
        if listener is not None:
            listener.close()


def _start_postgres_approval_http_api(
    *,
    dsn: str,
    schema: str,
    reviewer: AuthUser,
) -> tuple[multiprocessing.Process, multiprocessing.synchronize.Event, str]:
    """Start an isolated app process and wait until its real HTTP server is ready."""
    process_context = multiprocessing.get_context("spawn")
    status_queue = process_context.Queue()
    stop_event = process_context.Event()
    process = process_context.Process(
        target=_serve_postgres_approval_http_api,
        args=(
            dsn,
            schema,
            reviewer.id,
            reviewer.email,
            reviewer.workspace_id or "",
            status_queue,
            stop_event,
        ),
    )
    process.start()
    try:
        status, value = status_queue.get(timeout=30)
        status_queue.close()
        if status != "ready":
            pytest.fail(f"PostgreSQL approval HTTP server failed to start: {value}")
        api_base = f"http://127.0.0.1:{value}"
        with httpx.Client(timeout=1) as client:
            for _ in range(100):
                if not process.is_alive():
                    pytest.fail(
                        f"PostgreSQL approval HTTP server exited during startup: "
                        f"{process.exitcode}"
                    )
                try:
                    response = client.get(f"{api_base}/health")
                    if response.status_code == 200 and response.json().get("status") == "ok":
                        return process, stop_event, api_base
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
        pytest.fail("Uvicorn reported startup but its health endpoint stayed unavailable")
    except BaseException:
        stop_event.set()
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        raise


def _stop_postgres_approval_http_api(
    handle: tuple[multiprocessing.Process, multiprocessing.synchronize.Event, str],
) -> None:
    """Request graceful Uvicorn shutdown and reap only this test-owned process."""
    process, stop_event, _api_base = handle
    stop_event.set()
    process.join(timeout=20)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        pytest.fail("Isolated PostgreSQL approval Uvicorn process did not stop gracefully")
    assert process.exitcode == 0


def test_postgres_http_approval_flow_stays_in_bound_store(
    postgres_approval_route_runtime,
) -> None:
    connect, workspace, project, _user, repositories, agent, agent_token, _schema = postgres_approval_route_runtime

    client = TestClient(main.app)
    initial_store_approval_ids = set(store.approval_requests)
    initial_store_agent_ids = set(store.external_agents)
    invocation_id = f"invocation-{uuid.uuid4().hex}"
    invalid = client.post(
        "/agents/approvals",
        headers={
            "X-Aixion-Agent-Id": agent.id,
            "X-Aixion-Agent-Token": "invalid-token",
        },
        json={
            "project_id": project.id,
            "title": "Must not be created",
            "summary": "Invalid connector credentials must fail closed.",
            "target_branch": "feature/approval-route-test",
            "files": [
                {
                    "path": "antigravity://tool/run_command",
                    "change_type": "tool_call",
                    "diff": '{"command":"echo must-not-run"}',
                }
            ],
            "source_invocation_id": f"invalid-{uuid.uuid4().hex}",
        },
    )
    assert invalid.status_code == 401, invalid.text
    assert repositories.approval_decisions.list_for_workspace(workspace.id) == []

    response = client.post(
        "/agents/approvals",
        headers={
            "X-Aixion-Agent-Id": agent.id,
            "X-Aixion-Agent-Token": agent_token,
        },
        json={
            "project_id": project.id,
            "title": "Review Antigravity command",
            "summary": "A command requires a human approval.",
            "agent_name": agent.display_name,
            "target_branch": "feature/approval-route-test",
            "files": [
                {
                    "path": "antigravity://tool/run_command",
                    "change_type": "tool_call",
                    "diff": '{"command":"echo approval-route-test"}',
                }
            ],
            "source_session_id": "session-route-test",
            "source_invocation_id": invocation_id,
        },
    )
    assert response.status_code == 200, response.text
    approval = response.json()
    assert approval["workspace_id"] == workspace.id
    assert approval["source_agent_id"] == agent.id
    assert approval["status"] == "PENDING_REVIEW"

    decision = client.post(
        f"/approvals/{approval['id']}/decision",
        json={"decision": "approve"},
    )
    assert decision.status_code == 200, decision.text
    assert decision.json()["status"] == "APPROVED"
    assert decision.json()["approved_payload_hash"]

    headers = {
        "X-Aixion-Agent-Id": agent.id,
        "X-Aixion-Agent-Token": agent_token,
    }
    read = client.get(f"/agents/approvals/{approval['id']}/decision", headers=headers)
    assert read.status_code == 200, read.text
    assert read.json()["approved_payload_hash"] == decision.json()["approved_payload_hash"]

    consumed = client.post(f"/agents/approvals/{approval['id']}/consume", headers=headers)
    assert consumed.status_code == 200, consumed.text
    assert consumed.json() == {"consumed": True}
    duplicate = client.post(f"/agents/approvals/{approval['id']}/consume", headers=headers)
    assert duplicate.status_code == 409, duplicate.text

    durable = repositories.approval_decisions.get(approval["id"])
    assert durable is not None and durable.status == "APPROVED"
    with connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM audit_events WHERE entity_id=%s",
            (approval["id"],),
        ).fetchone()[0] >= 2
        assert connection.execute(
            "SELECT count(*) FROM approval_delivery_claims WHERE approval_id=%s",
            (approval["id"],),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM audit_events WHERE entity_id=%s AND event_type='agent.auth_failed'",
            (agent.id,),
        ).fetchone()[0] == 1
    assert set(store.approval_requests) == initial_store_approval_ids
    assert set(store.external_agents) == initial_store_agent_ids


def test_postgres_http_duplicate_invocation_create_is_idempotent_and_conflicts_on_change(
    postgres_approval_route_runtime,
) -> None:
    connect, workspace, project, _user, repositories, agent, agent_token, _schema = (
        postgres_approval_route_runtime
    )
    client = TestClient(main.app)
    headers = {
        "X-Aixion-Agent-Id": agent.id,
        "X-Aixion-Agent-Token": agent_token,
    }
    invocation_id = f"duplicate-create-{uuid.uuid4().hex}"
    payload = {
        "project_id": project.id,
        "title": "Review Antigravity command",
        "summary": "Same invocation and exact action payload must resolve once.",
        "agent_name": agent.display_name,
        "target_branch": "feature/approval-route-test",
        "files": [
            {
                "path": "antigravity://tool/run_command",
                "change_type": "tool_call",
                "diff": '{"command":"echo idempotent-create"}',
            }
        ],
        "source_session_id": "session-duplicate-create-test",
        "source_invocation_id": invocation_id,
    }

    first = client.post("/agents/approvals", headers=headers, json=payload)
    assert first.status_code == 200, first.text
    first_approval = first.json()

    with connect() as connection:
        initial_audit_count = connection.execute(
            "SELECT count(*) FROM audit_events WHERE entity_id=%s "
            "AND event_type='approval.created_by_agent'",
            (first_approval["id"],),
        ).fetchone()[0]
        initial_notification_count = connection.execute(
            "SELECT count(*) FROM notifications WHERE approval_id=%s",
            (first_approval["id"],),
        ).fetchone()[0]
        initial_approval_count = connection.execute(
            "SELECT count(*) FROM approval_requests WHERE workspace_id=%s "
            "AND source_agent_id=%s AND source_invocation_id=%s",
            (workspace.id, agent.id, invocation_id),
        ).fetchone()[0]

    retry = client.post("/agents/approvals", headers=headers, json=payload)
    assert retry.status_code == 200, retry.text
    assert retry.json()["id"] == first_approval["id"]

    changed = client.post(
        "/agents/approvals",
        headers=headers,
        json={**payload, "summary": "Changed action for the same invocation."},
    )
    assert changed.status_code == 409, changed.text

    durable = repositories.approval_decisions.get(first_approval["id"])
    assert durable is not None
    assert durable.summary == payload["summary"]
    with connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM approval_requests WHERE workspace_id=%s "
            "AND source_agent_id=%s AND source_invocation_id=%s",
            (workspace.id, agent.id, invocation_id),
        ).fetchone()[0] == initial_approval_count == 1
        assert connection.execute(
            "SELECT count(*) FROM audit_events WHERE entity_id=%s "
            "AND event_type='approval.created_by_agent'",
            (first_approval["id"],),
        ).fetchone()[0] == initial_audit_count == 1
        assert connection.execute(
            "SELECT count(*) FROM notifications WHERE approval_id=%s",
            (first_approval["id"],),
        ).fetchone()[0] == initial_notification_count == 1


def test_postgres_reviewer_from_other_workspace_cannot_decide_approval(
    postgres_approval_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, _workspace, project, _owner, _repositories, agent, agent_token, _schema = (
        postgres_approval_route_runtime
    )
    client = TestClient(main.app)
    created = client.post(
        "/agents/approvals",
        headers={
            "X-Aixion-Agent-Id": agent.id,
            "X-Aixion-Agent-Token": agent_token,
        },
        json={
            "project_id": project.id,
            "title": "Must remain pending",
            "summary": "A reviewer from another workspace must not decide this request.",
            "agent_name": agent.display_name,
            "target_branch": "feature/cross-workspace-denial",
            "files": [
                {
                    "path": "antigravity://tool/run_command",
                    "change_type": "tool_call",
                    "diff": '{"command":"echo workspace-scope-test"}',
                }
            ],
            "source_invocation_id": f"invocation-{uuid.uuid4().hex}",
        },
    )
    assert created.status_code == 200, created.text
    approval_id = created.json()["id"]

    other_workspace = Workspace(
        id=f"workspace_{uuid.uuid4().hex}",
        name="Unrelated reviewer workspace",
        owner_user_id=f"user_{uuid.uuid4().hex}",
    )
    other_user = User(
        id=other_workspace.owner_user_id,
        email=f"other-reviewer-{uuid.uuid4().hex}@example.test",
        display_name="Other workspace reviewer",
        role=UserRole.OWNER,
        password_hash="0" * 64,
        password_salt="0" * 32,
        workspace_id=other_workspace.id,
    )
    other_auth_user = AuthUser(
        id=other_user.id,
        email=other_user.email,
        display_name=other_user.display_name,
        role=UserRole.OWNER,
        workspace_id=other_workspace.id,
    )
    with connect() as connection:
        connection.execute(
            "INSERT INTO workspaces(workspace_id,payload) VALUES (%s,%s)",
            (other_workspace.id, Jsonb(other_workspace.model_dump(mode="json"))),
        )
        connection.execute(
            "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,email_verified,payload) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                other_user.id,
                other_user.workspace_id,
                other_user.email,
                other_user.email.lower(),
                other_user.role,
                other_user.disabled,
                other_user.email_verified,
                Jsonb(other_user.model_dump(mode="json")),
            ),
        )

    # Route through an authenticated identity whose durable PostgreSQL
    # membership and claimed workspace both belong to the second workspace.
    monkeypatch.setitem(
        main.app.dependency_overrides,
        main.ReviewerDependency.dependency,
        lambda: other_auth_user,
    )
    monkeypatch.setattr(
        main,
        "_workspace_id_for_route",
        lambda _user, _request: other_workspace.id,
    )

    with connect() as connection:
        approval_before = connection.execute(
            "SELECT status,payload FROM approval_requests WHERE approval_id=%s",
            (approval_id,),
        ).fetchone()
        audit_before = connection.execute(
            "SELECT event_id,event_type,workspace_id,actor,details,payload "
            "FROM audit_events WHERE approval_id=%s ORDER BY event_id",
            (approval_id,),
        ).fetchall()
        notifications_before = connection.execute(
            "SELECT notification_id,status,payload FROM notifications WHERE approval_id=%s "
            "ORDER BY notification_id",
            (approval_id,),
        ).fetchall()
        outbox_before = connection.execute(
            "SELECT notification_id,status,attempts,next_attempt_at,lease_expires_at,payload "
            "FROM notifications WHERE approval_id=%s ORDER BY notification_id",
            (approval_id,),
        ).fetchall()

    response = client.post(
        f"/approvals/{approval_id}/decision",
        json={"decision": "approve", "reason": "cross-workspace attempt"},
    )
    assert response.status_code == 404, response.text

    with connect() as connection:
        approval_after = connection.execute(
            "SELECT status,payload FROM approval_requests WHERE approval_id=%s",
            (approval_id,),
        ).fetchone()
        audit_after = connection.execute(
            "SELECT event_id,event_type,workspace_id,actor,details,payload "
            "FROM audit_events WHERE approval_id=%s ORDER BY event_id",
            (approval_id,),
        ).fetchall()
        notifications_after = connection.execute(
            "SELECT notification_id,status,payload FROM notifications WHERE approval_id=%s "
            "ORDER BY notification_id",
            (approval_id,),
        ).fetchall()
        outbox_after = connection.execute(
            "SELECT notification_id,status,attempts,next_attempt_at,lease_expires_at,payload "
            "FROM notifications WHERE approval_id=%s ORDER BY notification_id",
            (approval_id,),
        ).fetchall()

    assert approval_before is not None and approval_before[0] == ApprovalStatus.PENDING_REVIEW.value
    assert approval_after == approval_before
    assert audit_after == audit_before
    assert notifications_after == notifications_before
    assert outbox_after == outbox_before
    assert not any(row[1] == "approval.decision" for row in audit_after)
    assert len(notifications_after) == 1  # creation notification only; no decision notification


def test_postgres_hook_denies_lost_consume_response_and_same_invocation_retry(
    postgres_approval_route_runtime,
) -> None:
    _connect, _workspace, project, _user, repositories, agent, agent_token, schema = (
        postgres_approval_route_runtime
    )
    client = TestClient(main.app)
    state = {"approval_id": None, "drop_consume_response": True, "consume_calls": 0}

    def request(
        method: str,
        url: str,
        request_agent_id: str,
        request_token: str,
        body: dict | None = None,
    ) -> dict:
        assert request_agent_id == agent.id
        assert request_token == agent_token
        parsed = urlparse(url)
        response = client.request(
            method,
            parsed.path,
            headers={
                "X-Aixion-Agent-Id": agent.id,
                "X-Aixion-Agent-Token": agent_token,
                "X-Aixion-Approval-Protocol": "1",
            },
            json=body,
        )
        if method == "POST" and parsed.path == "/agents/approvals":
            assert response.status_code == 200, response.text
            approval = response.json()
            if state["approval_id"] is None:
                state["approval_id"] = approval["id"]
                decision = client.post(
                    f"/approvals/{approval['id']}/decision",
                    json={"decision": "approve"},
                )
                assert decision.status_code == 200, decision.text
            else:
                assert approval["id"] == state["approval_id"]
        if method == "POST" and parsed.path.endswith("/consume"):
            state["consume_calls"] += 1
            if response.status_code == 200 and state["drop_consume_response"]:
                state["drop_consume_response"] = False
                raise antigravity_hook.AmbiguousRequestError(
                    "simulated connection loss after PostgreSQL commit"
                )
        if response.status_code >= 400:
            raise antigravity_hook.GateError(
                f"Control Tower returned HTTP {response.status_code}"
            )
        return response.json()

    payload = {
        "conversationId": "session-postgres-lost-response",
        "stepIdx": 11,
        "toolCall": {"name": "run_command", "args": {"CommandLine": "echo gated"}},
    }
    environment = {
        "AIXION_API_BASE_URL": "http://localhost:8000",
        "AIXION_AGENT_ID": agent.id,
        "AIXION_AGENT_TOKEN": agent_token,
        "AIXION_ALLOW_AGENT_TOKEN_ENV": "true",
        "AIXION_PROJECT_ID": project.id,
        "AIXION_APPROVAL_TIMEOUT_SECONDS": "5",
    }
    with patch.object(antigravity_hook, "_request", side_effect=request), patch.object(
        antigravity_hook.time, "sleep"
    ):
        first = antigravity_hook.run(payload, environment)

    assert first["decision"] == "deny"
    assert "ambiguous" in first["reason"]
    process_context = multiprocessing.get_context("spawn")
    results = process_context.Queue()
    process = process_context.Process(
        target=_retry_antigravity_hook_in_fresh_process,
        args=(
            os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip(),
            schema,
            agent.id,
            agent_token,
            project.id,
            payload,
            results,
        ),
    )
    process.start()
    process.join(timeout=30)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        pytest.fail("Fresh API process did not finish the connector retry within 30 seconds")
    assert process.exitcode == 0
    process_outcome = results.get(timeout=5)
    assert process_outcome[0] == "ok", process_outcome[1]
    retry = process_outcome[1]
    assert retry["decision"] == "deny"
    assert state["consume_calls"] == 1
    approval_id = state["approval_id"]
    assert isinstance(approval_id, str)
    durable = repositories.approval_decisions.get(approval_id)
    assert durable is not None
    assert durable.status == ApprovalStatus.APPROVED
    with _connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM approval_requests WHERE approval_id=%s "
            "AND workspace_id=%s AND source_agent_id=%s AND source_invocation_id=%s",
            (
                approval_id,
                durable.workspace_id,
                agent.id,
                durable.source_invocation_id,
            ),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=%s",
            (approval_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM approval_delivery_failures WHERE approval_id=%s",
            (approval_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_events WHERE entity_id=%s "
            "AND event_type='approval.delivery_claimed'",
            (approval_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_events WHERE entity_id=%s "
            "AND event_type='approval.delivery_failed'",
            (approval_id,),
        ).fetchone()[0] == 1


def test_postgres_same_invocation_recovers_after_fresh_api_process(
    postgres_approval_route_runtime,
) -> None:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    connect, workspace, project, _user, repositories, agent, agent_token, schema = (
        postgres_approval_route_runtime
    )
    client = TestClient(main.app)
    state: dict[str, object] = {"approval": None, "create_bodies": []}
    payload = {
        "conversationId": "session-postgres-create-response-loss",
        "stepIdx": 23,
        "toolCall": {"name": "run_command", "args": {"CommandLine": "echo restart-safe"}},
    }
    environment = {
        "AIXION_API_BASE_URL": "http://localhost:8000",
        "AIXION_AGENT_ID": agent.id,
        "AIXION_AGENT_TOKEN": agent_token,
        "AIXION_ALLOW_AGENT_TOKEN_ENV": "true",
        "AIXION_PROJECT_ID": project.id,
        "AIXION_APPROVAL_TIMEOUT_SECONDS": "5",
    }

    def lose_committed_create_response(
        method: str,
        url: str,
        request_agent_id: str,
        request_token: str,
        body: dict | None = None,
    ) -> dict:
        assert method == "POST"
        assert urlparse(url).path == "/agents/approvals"
        assert request_agent_id == agent.id
        assert request_token == agent_token
        assert body is not None
        create_bodies = state["create_bodies"]
        assert isinstance(create_bodies, list)
        create_bodies.append(body.copy())
        response = client.post(
            "/agents/approvals",
            headers={
                "X-Aixion-Agent-Id": agent.id,
                "X-Aixion-Agent-Token": agent_token,
                "X-Aixion-Approval-Protocol": "1",
            },
            json=body,
        )
        assert response.status_code == 200, response.text
        received_approval = response.json()
        prior_approval = state["approval"]
        if prior_approval is not None:
            assert received_approval["id"] == prior_approval["id"]
        state["approval"] = received_approval
        raise antigravity_hook.AmbiguousRequestError(
            "simulated response loss after approval commit in PostgreSQL"
        )

    with patch.object(
        antigravity_hook, "_request", side_effect=lose_committed_create_response
    ), patch.object(antigravity_hook.time, "sleep"):
        first_attempt = antigravity_hook.run(payload, environment)

    assert first_attempt["decision"] == "deny"
    assert "remained ambiguous" in first_attempt["reason"]
    create_bodies = state["create_bodies"]
    assert len(create_bodies) == antigravity_hook.MAX_CREATE_ATTEMPTS == 2
    assert create_bodies[0] == create_bodies[1]

    created = state["approval"]
    assert created is not None
    approval_id = created["id"]
    invocation_id = created["source_invocation_id"]
    assert created["status"] == ApprovalStatus.PENDING_REVIEW

    decision = client.post(
        f"/approvals/{approval_id}/decision",
        json={"decision": "approve", "reason": "approve after simulated host restart"},
    )
    assert decision.status_code == 200, decision.text
    assert decision.json()["status"] == ApprovalStatus.APPROVED
    client.close()


    process_context = multiprocessing.get_context("spawn")
    results = process_context.Queue()
    process = process_context.Process(
        target=_retry_antigravity_hook_in_fresh_process,
        args=(
            dsn,
            schema,
            agent.id,
            agent_token,
            project.id,
            payload,
            results,
        ),
    )
    process.start()
    try:
        process.join(timeout=30)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
            pytest.fail("Fresh API process did not recover the invocation within 30 seconds")
        assert process.exitcode == 0
        process_outcome = results.get(timeout=5)
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        results.close()

    assert process_outcome[0] == "ok", process_outcome[1]
    recovered = process_outcome[1]
    assert recovered["decision"] == "allow"
    durable = repositories.approval_decisions.get(approval_id)
    assert durable is not None
    assert durable.status == ApprovalStatus.APPROVED
    assert durable.source_invocation_id == invocation_id
    with connect() as connection:
        rows = connection.execute(
            "SELECT approval_id,status,source_invocation_id FROM approval_requests "
            "WHERE workspace_id=%s AND source_agent_id=%s AND source_invocation_id=%s",
            (workspace.id, agent.id, invocation_id),
        ).fetchall()
        assert rows == [(approval_id, ApprovalStatus.APPROVED, invocation_id)]
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_events WHERE entity_id=%s "
            "AND event_type='approval.created_by_agent'",
            (approval_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_events WHERE entity_id=%s "
            "AND event_type='approval.decision'",
            (approval_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=%s",
            (approval_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_events WHERE entity_id=%s "
            "AND event_type='approval.delivery_claimed'",
            (approval_id,),
        ).fetchone()[0] == 1


def test_postgres_http_consume_rejects_revocation_after_authentication(
    postgres_approval_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, workspace, project, _user, repositories, agent, agent_token, _schema = (
        postgres_approval_route_runtime
    )
    client = TestClient(main.app)
    headers = {
        "X-Aixion-Agent-Id": agent.id,
        "X-Aixion-Agent-Token": agent_token,
        "X-Aixion-Approval-Protocol": "1",
    }
    created = client.post(
        "/agents/approvals",
        headers=headers,
        json={
            "project_id": project.id,
            "title": "Revoke during approval consume",
            "summary": "A connector revoked after authentication must not claim delivery.",
            "agent_name": agent.display_name,
            "target_branch": "feature/revocation-race",
            "files": [
                {
                    "path": "antigravity://tool/run_command",
                    "change_type": "tool_call",
                    "diff": '{"command":"echo must-not-be-authorized"}',
                }
            ],
            "source_session_id": "session-revocation-race",
            "source_invocation_id": f"invocation-{uuid.uuid4().hex}",
        },
    )
    assert created.status_code == 200, created.text
    approval_id = created.json()["id"]
    decided = client.post(
        f"/approvals/{approval_id}/decision",
        json={"decision": "approve"},
    )
    assert decided.status_code == 200, decided.text

    auth_repository = repositories.external_agent_auth

    class RevokeImmediatelyAfterAuthentication:
        def authenticate_and_consume(self, **kwargs):
            result = auth_repository.authenticate_and_consume(**kwargs)
            if result.reason is None:
                assert result.agent is not None and result.credential is not None
                revoke_external_agent_token(
                    result.agent,
                    credential=result.credential,
                    repository=auth_repository,
                    actor="test:revoke-between-auth-and-claim",
                )
            return result

    monkeypatch.setattr(
        main.app.state,
        "external_agent_auth_repository",
        RevokeImmediatelyAfterAuthentication(),
    )
    consumed = client.post(f"/agents/approvals/{approval_id}/consume", headers=headers)

    assert consumed.status_code == 401, consumed.text
    assert consumed.json()["detail"] == "External agent credential is no longer active"
    with connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM approval_delivery_claims WHERE approval_id=%s",
            (approval_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM audit_events "
            "WHERE entity_id=%s AND event_type='approval.delivery_claimed'",
            (approval_id,),
        ).fetchone()[0] == 0
    client.close()

def test_postgres_antigravity_invocation_recovers_after_http_api_server_restart(
    postgres_approval_route_runtime,
) -> None:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    connect, workspace, project, user, repositories, agent, agent_token, schema = (
        postgres_approval_route_runtime
    )
    payload = {
        "conversationId": f"session-postgres-uvicorn-restart-{uuid.uuid4().hex}",
        "stepIdx": 31,
        "toolCall": {"name": "run_command", "args": {"CommandLine": "echo restart-safe"}},
    }
    environment = {
        "AIXION_API_BASE_URL": "",
        "AIXION_AGENT_ID": agent.id,
        "AIXION_AGENT_TOKEN": agent_token,
        "AIXION_ALLOW_AGENT_TOKEN_ENV": "true",
        "AIXION_PROJECT_ID": project.id,
        "AIXION_APPROVAL_TIMEOUT_SECONDS": "5",
    }
    servers: list[
        tuple[multiprocessing.Process, multiprocessing.synchronize.Event, str]
    ] = []

    def start_server() -> tuple[multiprocessing.Process, multiprocessing.synchronize.Event, str]:
        handle = _start_postgres_approval_http_api(
            dsn=dsn,
            schema=schema,
            reviewer=user,
        )
        servers.append(handle)
        return handle

    try:
        first_server = start_server()
        environment["AIXION_API_BASE_URL"] = first_server[2]
        real_request = antigravity_hook._request
        created: dict[str, dict] = {}
        create_bodies: list[dict] = []

        def lose_committed_create_response(
            method: str,
            url: str,
            request_agent_id: str,
            request_token: str,
            body: dict | None = None,
        ) -> dict:
            if method == "POST" and urlparse(url).path == "/agents/approvals":
                assert body is not None
                create_bodies.append(body.copy())
            result = real_request(
                method, url, request_agent_id, request_token, body
            )
            if method == "POST" and urlparse(url).path == "/agents/approvals":
                prior = created.get("approval")
                if prior is not None:
                    assert result["id"] == prior["id"]
                created["approval"] = result
                raise antigravity_hook.AmbiguousRequestError(
                    "simulated lost HTTP response after PostgreSQL commit"
                )
            return result

        with patch.object(
            antigravity_hook, "_request", side_effect=lose_committed_create_response
        ), patch.object(antigravity_hook.time, "sleep"):
            first_attempt = antigravity_hook.run(payload, environment)

        assert first_attempt["decision"] == "deny"
        assert "remained ambiguous" in first_attempt["reason"]
        assert len(create_bodies) == antigravity_hook.MAX_CREATE_ATTEMPTS == 2
        assert create_bodies[0] == create_bodies[1]

        first_approval = created["approval"]
        approval_id = first_approval["id"]
        invocation_id = first_approval["source_invocation_id"]
        assert first_approval["status"] == ApprovalStatus.PENDING_REVIEW
        _stop_postgres_approval_http_api(first_server)

        second_server = start_server()
        with httpx.Client(timeout=10) as client:
            decision = client.post(
                f"{second_server[2]}/approvals/{approval_id}/decision",
                json={
                    "decision": "approve",
                    "reason": "approve after actual API server restart",
                },
            )
        assert decision.status_code == 200, decision.text
        assert decision.json()["status"] == ApprovalStatus.APPROVED
        _stop_postgres_approval_http_api(second_server)

        third_server = start_server()
        environment["AIXION_API_BASE_URL"] = third_server[2]
        with patch.object(antigravity_hook.time, "sleep"):
            recovered = antigravity_hook.run(payload, environment)
        assert recovered["decision"] == "allow"
        assert recovered["reason"] == "Approved from Aixion Control Tower"

        durable = repositories.approval_decisions.get(approval_id)
        assert durable is not None
        assert durable.status == ApprovalStatus.APPROVED
        assert durable.source_invocation_id == invocation_id
        with connect() as connection:
            rows = connection.execute(
                "SELECT approval_id,status,source_invocation_id FROM approval_requests "
                "WHERE workspace_id=%s AND source_agent_id=%s AND source_invocation_id=%s",
                (workspace.id, agent.id, invocation_id),
            ).fetchall()
            assert rows == [(approval_id, ApprovalStatus.APPROVED, invocation_id)]
            assert connection.execute(
                "SELECT COUNT(*) FROM audit_events WHERE entity_id=%s "
                "AND event_type='approval.created_by_agent'",
                (approval_id,),
            ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT COUNT(*) FROM audit_events WHERE entity_id=%s "
                "AND event_type='approval.decision'",
                (approval_id,),
            ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=%s",
                (approval_id,),
            ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT COUNT(*) FROM audit_events WHERE entity_id=%s "
                "AND event_type='approval.delivery_claimed'",
                (approval_id,),
            ).fetchone()[0] == 1
    finally:
        for server in reversed(servers):
            if server[0].is_alive():
                _stop_postgres_approval_http_api(server)


def test_postgres_standard_approval_creation_does_not_project_to_sqlite(
    postgres_approval_route_runtime,
    monkeypatch,
) -> None:
    _connect, workspace, project, user, repositories, _agent, _agent_token, _schema = (
        postgres_approval_route_runtime
    )
    monkeypatch.setitem(
        main.app.dependency_overrides,
        main.MaintainerDependency.dependency,
        lambda: user,
    )
    client = TestClient(main.app)
    initial_approval_ids = set(store.approval_requests)
    initial_audit_ids = {event.id for event in store.audit_events}
    initial_notification_ids = set(store.notifications)

    response = client.post(
        "/approvals",
        json={
            "project_id": project.id,
            "title": "Review standard approval",
            "summary": "This route must keep PostgreSQL as the sole authority.",
            "target_branch": "feature/approval-route-test",
            "files": [
                {
                    "path": "src/reviewed.txt",
                    "change_type": "modified",
                    "diff": "-before\n+after",
                }
            ],
        },
    )

    assert response.status_code == 200, response.text
    approval_id = response.json()["id"]
    durable = repositories.approval_decisions.get(approval_id)
    assert durable is not None
    assert durable.workspace_id == workspace.id
    assert approval_id not in store.approval_requests
    assert set(store.approval_requests) == initial_approval_ids
    assert {event.id for event in store.audit_events} == initial_audit_ids
    assert set(store.notifications) == initial_notification_ids


def test_postgres_http_decision_race_across_independent_app_processes(
    postgres_approval_route_runtime,
) -> None:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    connect, workspace, project, user, repositories, _agent, _token, schema = (
        postgres_approval_route_runtime
    )
    approval = ApprovalRequest(
        project_id=project.id,
        workspace_id=workspace.id,
        title="Concurrent approval decision",
        summary="Two app processes race on one durable approval.",
        target_branch="feature/process-race",
        files=[FileChange(path="README.md", diff="+reviewed")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.PENDING_REVIEW,
    )
    repositories.approval_decisions.create(
        approval=approval,
        audit_event=AuditEvent(
            event_type="approval.created",
            entity_id=approval.id,
            workspace_id=workspace.id,
            details={"project_id": project.id},
        ),
        notification=Notification(
            title="Approval needed",
            body="Open Control Tower to review.",
            entity_type="approval_request",
            entity_id=approval.id,
            workspace_id=workspace.id,
        ),
    )

    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(
            target=_race_postgres_approval_http_decision,
            args=(
                dsn,
                schema,
                approval.id,
                workspace.id,
                user.id,
                user.email,
                decision,
                barrier,
                results,
            ),
        )
        for decision in ("approve", "deny")
    ]
    try:
        for process in processes:
            process.start()
        outcomes = [results.get(timeout=40) for _ in processes]
    finally:
        for process in processes:
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        results.close()

    assert all(process.exitcode == 0 for process in processes)
    assert sorted(status for status, _body in outcomes) == [200, 409], outcomes
    durable = repositories.approval_decisions.get(approval.id)
    assert durable is not None
    assert durable.status in {ApprovalStatus.APPROVED, ApprovalStatus.DENIED}
    with connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM audit_events WHERE entity_id=%s AND event_type='approval.decision'",
            (approval.id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM notifications WHERE approval_id=%s",
            (approval.id,),
        ).fetchone()[0] == 2
