from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Callable, Iterator
from dataclasses import replace

import psycopg
import pytest
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
    User,
    UserRole,
    Workspace,
    new_id,
)
from app.postgres_database import run_postgres_migrations
from app.postgres_runtime import PostgresRuntime
from app.settings import get_settings
from app.store import store


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
        monkeypatch.setitem(
            main.app.dependency_overrides,
            notifications.AuthDependency.dependency,
            lambda: user,
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

        decision = client.post(
            f"/approvals/{approval['id']}/decision",
            json={"decision": "approve", "reason": "Reviewed on the phone"},
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
