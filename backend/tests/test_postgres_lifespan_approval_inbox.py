from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import timedelta

import psycopg
import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app import agent_auth, agent_routes, application_repository_access, main, notifications
from app.agent_credential_models import AgentCredentialRecord
from app.models import (
    AgentAction,
    AgentAuthType,
    AgentProvider,
    AuditEvent,
    AuthUser,
    ExternalAgent,
    Project,
    SessionToken,
    User,
    UserRole,
    Workspace,
    new_id,
    now_utc,
)
from app.postgres_database import run_postgres_migrations
from app.postgres_runtime import PostgresRuntime
from app.settings import get_settings
from app.store import store
from test_postgres_approval_route_flow import _post_reviewed_approval_decision


@pytest.fixture
def postgres_lifespan_approval_context(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Callable[[], Connection], Workspace, Project, AuthUser, ExternalAgent, str]]:
    """Prepare one isolated schema for a real FastAPI lifespan integration test."""
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL integration tests")

    schema = f"ct_lifespan_approval_{uuid.uuid4().hex}"
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
            name="Lifespan approval workspace",
            owner_user_id=f"user_{uuid.uuid4().hex}",
        )
        user_record = User(
            id=workspace.owner_user_id,
            email=f"owner-{uuid.uuid4().hex}@example.test",
            display_name="Lifespan approval reviewer",
            role=UserRole.OWNER,
            password_hash="0" * 64,
            password_salt="0" * 32,
            workspace_id=workspace.id,
            email_verified=True,
        )
        user = AuthUser(
            id=user_record.id,
            email=user_record.email,
            display_name=user_record.display_name,
            role=user_record.role,
            workspace_id=workspace.id,
        )
        auth_session = SessionToken(
            id=f"session_{uuid.uuid4().hex}",
            user_id=user.id,
            token_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            expires_at=now_utc() + timedelta(hours=1),
        )
        project = Project(
            id=f"project_{uuid.uuid4().hex}",
            name="Lifespan approval project",
            workspace_id=workspace.id,
            github_repo="example/lifespan-approval",
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
                    user_record.id,
                    workspace.id,
                    user_record.email,
                    user_record.email.lower(),
                    user_record.role.value,
                    user_record.disabled,
                    user_record.email_verified,
                    Jsonb(user_record.model_dump(mode="json")),
                ),
            )
            connection.execute(
                "INSERT INTO sessions(session_id,user_id,token_hash,created_at,expires_at,revoked,payload) "
                "VALUES(%s,%s,%s,%s,%s,FALSE,%s)",
                (
                    auth_session.id,
                    auth_session.user_id,
                    auth_session.token_hash,
                    auth_session.created_at,
                    auth_session.expires_at,
                    Jsonb(auth_session.model_dump(mode="json")),
                ),
            )
            connection.execute(
                "INSERT INTO projects(project_id,workspace_id,payload) VALUES (%s,%s,%s)",
                (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))),
            )

        settings = replace(
            get_settings(),
            profile="test",
            auth_enabled=True,
            postgres_auth_enabled=True,
            postgres_runtime_probe_enabled=False,
            postgres_auth_rate_limits_enabled=False,
            outbox_dispatch_mode="scheduled",
            push_dispatch_enabled=False,
            agent_approval_ttl_seconds=600,
        )
        runtime = PostgresRuntime(
            _dsn=dsn,
            profile="test",
            settings=settings,
            _connector=lambda *_args, **_kwargs: connect(),
        )
        assert runtime.probe().ready, runtime.probe().schema
        monkeypatch.setattr(main, "get_settings", lambda: settings)
        monkeypatch.setattr(agent_auth, "get_settings", lambda: settings)
        monkeypatch.setattr(agent_routes, "get_settings", lambda: settings)
        monkeypatch.setattr(application_repository_access, "get_settings", lambda: settings)
        monkeypatch.setattr(notifications, "get_settings", lambda: settings)
        monkeypatch.setattr(main.PostgresRuntime, "from_environment", lambda: runtime)
        def authenticated_test_user(request: Request) -> AuthUser:
            request.state.authenticated_session_id = auth_session.id
            return user

        monkeypatch.setitem(
            main.app.dependency_overrides,
            notifications.AuthDependency.dependency,
            authenticated_test_user,
        )
        monkeypatch.setitem(
            main.app.dependency_overrides,
            main.ReviewerDependency.dependency,
            lambda: user,
        )

        repositories = runtime.repositories
        agent_token = f"postgres-lifespan-token-{uuid.uuid4().hex}"
        agent = ExternalAgent(
            id=new_id("agent"),
            provider=AgentProvider.ANTIGRAVITY,
            display_name="Antigravity lifespan integration",
            auth_type=AgentAuthType.API_KEY,
            allowed_project_ids=[project.id],
            allowed_actions=[
                AgentAction.CREATE_APPROVAL,
                AgentAction.READ_APPROVAL_DECISION,
                AgentAction.CONSUME_APPROVAL_DECISION,
                AgentAction.CREATE_AGENT_TASK,
                AgentAction.READ_AGENT_TASK,
                AgentAction.APPEND_AGENT_TASK_EVENT,
            ],
            created_by_user_id=user.id,
            workspace_id=workspace.id,
            secret_hash=hashlib.sha256(agent_token.encode("utf-8")).hexdigest(),
        )
        repositories.external_agent_auth.create(
            agent=agent,
            credential=AgentCredentialRecord(agent_id=agent.id),
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
        yield connect, workspace, project, user, agent, agent_token
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_lifespan_routes_antigravity_approval_to_postgres_inbox_without_sqlite_projection(
    postgres_lifespan_approval_context,
) -> None:
    connect, workspace, project, user, agent, agent_token = postgres_lifespan_approval_context
    repositories = None
    before_approvals = set(store.approval_requests)
    before_notifications = set(store.notifications)
    before_devices = set(store.device_registrations)
    before_audits = {event.id for event in store.audit_events}
    invocation_id = f"lifespan-invocation-{uuid.uuid4().hex}"
    original_notification_repository = getattr(
        main.app.state, "notification_outbox_repository", None
    )
    original_repository_bindings = {
        state_name: getattr(main.app.state, state_name, None)
        for state_name in main._POSTGRES_APP_STATE_BINDINGS
    }

    with TestClient(main.app) as client:
        active_runtime = client.app.state.postgres_runtime
        repositories = active_runtime.repositories
        assert client.app.state.postgres_repositories is repositories
        for state_name, repository_name in main._POSTGRES_APP_STATE_BINDINGS.items():
            assert getattr(client.app.state, state_name) is getattr(
                repositories, repository_name
            )

        created = client.post(
            "/agents/approvals",
            headers={
                "X-Aixion-Agent-Id": agent.id,
                "X-Aixion-Agent-Token": agent_token,
            },
            json={
                "project_id": project.id,
                "title": "Review Antigravity command",
                "summary": "Approval must reach the registered mobile inbox.",
                "agent_name": agent.display_name,
                "target_branch": "feature/lifespan-approval-test",
                "files": [
                    {
                        "path": "antigravity://tool/run_command",
                        "change_type": "tool_call",
                        "diff": '{"command":"echo lifespan-approval-test"}',
                    }
                ],
                "source_session_id": "lifespan-session",
                "source_invocation_id": invocation_id,
            },
        )
        assert created.status_code == 200, created.text
        approval = created.json()
        assert approval["workspace_id"] == workspace.id
        assert approval["source_agent_id"] == agent.id
        assert approval["source_invocation_id"] == invocation_id
        assert approval["status"] == "PENDING_REVIEW"

        durable_approval = repositories.approval_decisions.get(approval["id"])
        assert durable_approval is not None
        assert durable_approval.workspace_id == workspace.id
        with connect() as connection:
            assert connection.execute(
                "SELECT count(*) FROM audit_events WHERE entity_id=%s AND event_type='approval.created_by_agent'",
                (approval["id"],),
            ).fetchone()[0] == 1
            notification_id = connection.execute(
                "SELECT notification_id FROM notifications WHERE approval_id=%s AND workspace_id=%s",
                (approval["id"], workspace.id),
            ).fetchone()[0]

        registered = client.post(
            "/notifications/devices",
            json={
                "token": f"device-token-{uuid.uuid4().hex}",
                "platform": "android",
                "app_version": "integration-test",
            },
        )
        assert registered.status_code == 200, registered.text
        device = registered.json()
        assert device["user_id"] == user.id
        assert device["workspace_id"] == workspace.id

        inbox = client.get("/notifications")
        assert inbox.status_code == 200, inbox.text
        inbox_rows = inbox.json()
        assert [item["id"] for item in inbox_rows] == [notification_id]
        assert inbox_rows[0]["entity_id"] == approval["id"]
        assert inbox_rows[0]["workspace_id"] == workspace.id

        decision = _post_reviewed_approval_decision(
            client,
            approval["id"],
            decision="approve",
            reason="Reviewed on the phone",
        )
        assert decision.status_code == 200, decision.text
        assert decision.json()["status"] == "APPROVED"
        frozen_hash = decision.json()["approved_payload_hash"]
        assert frozen_hash

        headers = {
            "X-Aixion-Agent-Id": agent.id,
            "X-Aixion-Agent-Token": agent_token,
        }
        polled = client.get(
            f"/agents/approvals/{approval['id']}/decision", headers=headers
        )
        assert polled.status_code == 200, polled.text
        assert polled.json()["source_invocation_id"] == invocation_id
        assert polled.json()["approved_payload_hash"] == frozen_hash
        consumed = client.post(
            f"/agents/approvals/{approval['id']}/consume", headers=headers
        )
        assert consumed.status_code == 200, consumed.text
        assert consumed.json() == {"consumed": True}
        replay = client.post(
            f"/agents/approvals/{approval['id']}/consume", headers=headers
        )
        assert replay.status_code == 409, replay.text

        assert repositories.approval_decisions.get(approval["id"]).status == "APPROVED"
        with connect() as connection:
            assert connection.execute(
                "SELECT count(*) FROM audit_events WHERE entity_id=%s AND event_type='approval.decision'",
                (approval["id"],),
            ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT count(*) FROM approval_delivery_claims WHERE approval_id=%s",
                (approval["id"],),
            ).fetchone()[0] == 1

    assert repositories is not None
    assert main.app.state.postgres_runtime is None
    assert main.app.state.postgres_repositories is None
    assert (
        getattr(main.app.state, "notification_outbox_repository", None)
        is original_notification_repository
    )
    for state_name, value in original_repository_bindings.items():
        assert getattr(main.app.state, state_name, None) is value
    assert set(store.approval_requests) == before_approvals
    assert set(store.notifications) == before_notifications
    assert set(store.device_registrations) == before_devices
    assert {event.id for event in store.audit_events} == before_audits


def test_lifespan_routes_external_agent_tasks_to_postgres_without_sqlite_projection(
    postgres_lifespan_approval_context,
) -> None:
    connect, workspace, project, _user, agent, agent_token = postgres_lifespan_approval_context
    before_tasks = set(store.agent_tasks)
    before_events = set(store.agent_task_events)
    before_audits = {event.id for event in store.audit_events}
    headers = {
        "X-Aixion-Agent-Id": agent.id,
        "X-Aixion-Agent-Token": agent_token,
    }

    with TestClient(main.app) as client:
        repositories = client.app.state.postgres_repositories
        assert repositories.workspace_membership.get_workspace(workspace.id) == workspace
        assert repositories.projects.get(project.id) == project
        assert repositories.external_agent_auth.get(agent.id)[0] == agent
        task_response = client.post(
            "/agents/tasks",
            headers=headers,
            json={
                "project_id": project.id,
                "title": "Persist external task",
                "goal": "Keep task and approval state in one PostgreSQL authority",
                "requested_action": "GENERATE_DOCS",
                "requires_approval": True,
            },
        )
        assert task_response.status_code == 200, task_response.text
        task = task_response.json()
        assert task["workspace_id"] == workspace.id
        assert task["external_agent_id"] == agent.id
        assert task["provider"] == "ANTIGRAVITY"
        assert repositories.agent_tasks.get(task["id"]) is not None

        task_read = client.get(f"/agents/tasks/{task['id']}", headers=headers)
        assert task_read.status_code == 200, task_read.text
        assert task_read.json()["id"] == task["id"]
        initial_events = client.get(f"/agents/tasks/{task['id']}/events", headers=headers)
        assert initial_events.status_code == 200, initial_events.text
        assert [event["event_type"] for event in initial_events.json()] == ["TASK_CREATED"]

        appended = client.post(
            f"/agents/tasks/{task['id']}/events",
            headers=headers,
            json={
                "event_type": "PLAN_RECEIVED",
                "message": "Plan ready",
                "status": "PLANNING",
                "metadata": {"phase": "plan"},
            },
        )
        assert appended.status_code == 200, appended.text
        assert appended.json()["actor"] == f"agent:{agent.id}"
        assert repositories.agent_tasks.get(task["id"]).status == "PLANNING"
        assert len(repositories.agent_tasks.list_events(task["id"], workspace_id=workspace.id)) == 2

        with connect() as connection:
            assert connection.execute(
                "SELECT count(*) FROM agent_tasks WHERE task_id=%s AND workspace_id=%s",
                (task["id"], workspace.id),
            ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT count(*) FROM agent_task_events WHERE task_id=%s AND workspace_id=%s",
                (task["id"], workspace.id),
            ).fetchone()[0] == 2
            assert connection.execute(
                "SELECT count(*) FROM audit_events WHERE entity_id=%s AND workspace_id=%s",
                (task["id"], workspace.id),
            ).fetchone()[0] == 2

        assert set(store.agent_tasks) == before_tasks
        assert set(store.agent_task_events) == before_events
        assert {event.id for event in store.audit_events} == before_audits

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
                    foreign_workspace.id,
                    Jsonb(foreign_project.model_dump(mode="json")),
                ),
            )
        wrong_project = client.post(
            "/agents/tasks",
            headers=headers,
            json={
                "project_id": foreign_project.id,
                "title": "Must not cross workspace scope",
                "goal": "Fail closed when the selected project is outside agent scope",
            },
        )
        assert wrong_project.status_code == 403
