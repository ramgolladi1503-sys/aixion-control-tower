from __future__ import annotations

import os
import json
import time
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

from app import main
import app.application_repository_access as repository_access
import app.connector_routes as connector_routes
from app.application_repositories import ApplicationRepositories
from app.agent_task_models import AgentTask, AgentTaskEvent, AgentTaskEventType, AgentTaskStatus
from app.auth import require_owner
from app.connector_models import AgentConnector
from app.connector_repository import PostgresConnectorRepository
from app.connector_credentials import hash_connector_secret
from app.connector_webhook import (
    _assert_hmac_v1_not_replayed,
    expected_hmac_v1_signature,
)
from app.models import (
    ApprovalRequest,
    ApprovalStatus,
    AgentAuthType,
    AgentProvider,
    AuditEvent,
    AuthUser,
    ExternalAgent,
    Project,
    RiskAssessment,
    RiskLevel,
    User,
    UserRole,
    Workspace,
    now_utc,
)
from app.postgres_database import run_postgres_migrations
from app.store import store


def test_hmac_nonce_history_retains_unexpired_nonce_beyond_200_entries() -> None:
    now_epoch = int(time.time())
    first_nonce = f"first_{uuid.uuid4().hex}"
    connector = AgentConnector(
        name="Nonce retention",
        config={"hmac_nonce_history": {first_nonce: now_epoch}},
    )

    for index in range(201):
        _assert_hmac_v1_not_replayed(connector, f"fresh_{index}_{uuid.uuid4().hex}", now_epoch)

    assert first_nonce in connector.config["hmac_nonce_history"]
    with pytest.raises(HTTPException) as replay_error:
        _assert_hmac_v1_not_replayed(connector, first_nonce, now_epoch)

    assert replay_error.value.status_code == 401


@pytest.fixture
def postgres_connector_route_runtime(monkeypatch: pytest.MonkeyPatch) -> Iterator[
    tuple[Callable[[], Connection], Workspace, AuthUser]
]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL connector route tests")
    schema = f"ct_connector_routes_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    try:
        run_postgres_migrations(connect)
        user_id = f"user_{uuid.uuid4().hex}"
        workspace = Workspace(
            id=f"workspace_{uuid.uuid4().hex}", name="Connector routes", owner_user_id=user_id
        )
        user = User(
            id=user_id,
            email=f"owner-{uuid.uuid4().hex}@example.test",
            display_name="Owner",
            role=UserRole.OWNER,
            password_hash="hash",
            password_salt="salt",
            workspace_id=workspace.id,
            email_verified=True,
        )
        auth_user = AuthUser(
            id=user.id, email=user.email, display_name=user.display_name,
            role=user.role, workspace_id=workspace.id,
        )
        with connect() as conn:
            conn.execute(
                "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
                (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
            )
            conn.execute(
                "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled," 
                "email_verified,payload) VALUES(%s,%s,%s,%s,%s,FALSE,TRUE,%s)",
                (user.id, workspace.id, user.email, user.email.lower(), user.role.value,
                 Jsonb(user.model_dump(mode="json"))),
            )
        repositories = ApplicationRepositories.from_connector(connect)
        monkeypatch.setattr(
            repository_access,
            "get_settings",
            lambda: SimpleNamespace(postgres_auth_enabled=True),
        )
        monkeypatch.setattr(
            connector_routes,
            "get_settings",
            lambda: SimpleNamespace(postgres_auth_enabled=True),
        )
        monkeypatch.setattr(main.app.state, "postgres_repositories", repositories, raising=False)
        monkeypatch.setattr(
            main.app.state,
            "postgres_runtime",
            SimpleNamespace(repositories=repositories),
            raising=False,
        )
        # A stale local binding must not become the connector authority in PG auth mode.
        monkeypatch.setattr(main.app.state, "connector_repository", object(), raising=False)
        monkeypatch.setattr(main.app.state, "agent_task_repository", repositories.agent_tasks, raising=False)
        monkeypatch.setattr(main.app.state, "project_repository", repositories.projects, raising=False)
        monkeypatch.setattr(main.app.state, "workspace_membership_repository",
                            repositories.workspace_membership, raising=False)
        monkeypatch.setitem(main.app.dependency_overrides, require_owner, lambda: auth_user)
        yield connect, workspace, auth_user
    finally:
        main.app.dependency_overrides.pop(require_owner, None)
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_connector_management_fails_closed_without_or_with_mismatched_postgres_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        repository_access,
        "get_settings",
        lambda: SimpleNamespace(postgres_auth_enabled=True),
    )
    request = type("Request", (), {"app": type("App", (), {"state": type("State", (), {
        "postgres_repositories": None,
        "connector_repository": object(),
    })()})()})()
    with pytest.raises(RuntimeError, match="repository runtime is unavailable"):
        repository_access.connector_repository_for_request(request)

    request.app.state.postgres_repositories = SimpleNamespace(connectors=object())
    request.app.state.postgres_runtime = SimpleNamespace(
        repositories=request.app.state.postgres_repositories
    )
    with pytest.raises(RuntimeError, match="runtime is unavailable or mismatched"):
        repository_access.connector_repository_for_request(request)


def test_connector_mutation_without_request_context_does_not_fall_back_to_sqlite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        connector_routes,
        "get_settings",
        lambda: SimpleNamespace(postgres_auth_enabled=True),
    )
    connector = AgentConnector(name="Must remain unchanged")
    monkeypatch.setitem(store.agent_connectors, connector.id, connector)
    before_audits = {event.id for event in store.audit_events}

    with pytest.raises(HTTPException) as error:
        connector_routes.disable_connector(connector.id, user=None)

    assert error.value.status_code == 503
    assert error.value.detail == "PostgreSQL connector repository requires an app-bound request"
    assert store.agent_connectors[connector.id] is connector
    assert connector.status.value == "ENABLED"
    assert {event.id for event in store.audit_events} == before_audits


def test_production_connector_routes_without_request_context_do_not_fall_back_to_sqlite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        connector_routes,
        "get_settings",
        lambda: SimpleNamespace(is_production=True, postgres_auth_enabled=False),
    )
    user = AuthUser(
        id="production-owner",
        email="owner@example.test",
        display_name="Owner",
        role=UserRole.OWNER,
        workspace_id="workspace-production",
    )
    before_connectors = dict(store.agent_connectors)
    before_audits = list(store.audit_events)

    with pytest.raises(HTTPException) as create_error:
        connector_routes.create_connector(
            connector_routes.ConnectorCreate(name="Must not be stored"), user
        )
    with pytest.raises(HTTPException) as list_error:
        connector_routes.list_connectors(user)
    with pytest.raises(HTTPException) as read_error:
        connector_routes._get_connector_or_404("missing-connector", user)

    assert create_error.value.status_code == 503
    assert list_error.value.status_code == 503
    assert read_error.value.status_code == 503
    assert store.agent_connectors == before_connectors
    assert store.audit_events == before_audits


def test_connector_route_rejects_repository_bundle_from_another_runtime_without_mutation(
    postgres_connector_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, _workspace, _user = postgres_connector_route_runtime
    repositories_a = main.app.state.postgres_repositories
    repositories_b = ApplicationRepositories.from_connector(connect)
    monkeypatch.setattr(
        main.app.state,
        "postgres_runtime",
        SimpleNamespace(repositories=repositories_b),
    )
    with pytest.raises(RuntimeError, match="runtime is unavailable or mismatched"):
        repository_access.connector_repository_for_request(
            SimpleNamespace(app=main.app)
        )
    before_connectors = set(store.agent_connectors)
    before_audits = {event.id for event in store.audit_events}
    with connect() as conn:
        before_db_connectors = conn.execute(
            "SELECT count(*) FROM agent_connectors"
        ).fetchone()[0]
        before_db_audits = conn.execute(
            "SELECT count(*) FROM audit_events WHERE entity_type='connector'"
        ).fetchone()[0]

    response = TestClient(main.app).post(
        "/connectors",
        json={"name": "Must not be written through a mismatched runtime"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "PostgreSQL workspace membership runtime is unavailable"
    assert main.app.state.postgres_repositories is repositories_a
    assert set(store.agent_connectors) == before_connectors
    assert {event.id for event in store.audit_events} == before_audits
    with connect() as conn:
        assert conn.execute("SELECT count(*) FROM agent_connectors").fetchone()[0] == before_db_connectors
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE entity_type='connector'"
        ).fetchone()[0] == before_db_audits


def test_connector_management_routes_use_workspace_bound_postgres_and_atomic_audit(
    postgres_connector_route_runtime,
) -> None:
    connect, workspace, _user = postgres_connector_route_runtime
    before_connectors = set(store.agent_connectors)
    before_audits = {event.id for event in store.audit_events}
    client = TestClient(main.app)

    created = client.post(
        "/connectors",
        json={
            "name": "Antigravity local connector",
            "provider_label": "ANTIGRAVITY",
            "allowed_actions": ["CREATE_AGENT_TASK"],
        },
    )

    assert created.status_code == 200, created.text
    connector = created.json()
    connector_id = connector["id"]
    assert client.get("/connectors").json() == [connector]
    assert client.get(f"/connectors/{connector_id}").json() == connector
    updated = client.patch(
        f"/connectors/{connector_id}",
        json={"name": "Antigravity connector updated", "enabled": False},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "Antigravity connector updated"
    assert updated.json()["status"] == "DISABLED"

    with connect() as conn:
        row = conn.execute(
            "SELECT workspace_id,payload FROM agent_connectors WHERE connector_id=%s",
            (connector_id,),
        ).fetchone()
        audit_rows = conn.execute(
            "SELECT event_type FROM audit_events WHERE workspace_id=%s AND entity_id=%s "
            "ORDER BY created_at,event_id",
            (workspace.id, connector_id),
        ).fetchall()
    assert row is not None and row[0] == workspace.id
    assert row[1]["name"] == "Antigravity connector updated"
    assert [item[0] for item in audit_rows] == ["connector.created", "connector.updated"]
    assert set(store.agent_connectors) == before_connectors
    assert {event.id for event in store.audit_events} == before_audits


def test_connector_credentials_projects_exact_postgres_invocation_lease_liveness(
    postgres_connector_route_runtime,
) -> None:
    connect, workspace, user = postgres_connector_route_runtime
    client = TestClient(main.app)
    created_connector = client.post(
        "/connectors",
        json={"name": "Invocation status", "provider_label": "ANTIGRAVITY"},
    )
    assert created_connector.status_code == 200, created_connector.text
    connector_id = created_connector.json()["id"]

    now = now_utc()
    project = Project(
        name="Invocation status project",
        workspace_id=workspace.id,
        created_by_user_id=user.id,
    )
    external_agent = ExternalAgent(
        id=connector_id,
        provider=AgentProvider.ANTIGRAVITY,
        display_name="Invocation status connector",
        auth_type=AgentAuthType.API_KEY,
        workspace_id=workspace.id,
        created_by_user_id=user.id,
    )
    invocation_id = f"antigravity:v1:{uuid.uuid4().hex}"
    approval = ApprovalRequest(
        id=f"approval_{uuid.uuid4().hex}",
        project_id=project.id,
        title="Invocation liveness",
        summary="Exact-scope status projection fixture",
        agent_name=external_agent.display_name,
        target_branch="test",
        files=[],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.PENDING_REVIEW,
        source_provider=AgentProvider.ANTIGRAVITY,
        source_agent_id=connector_id,
        source_agent_name=external_agent.display_name,
        source_invocation_id=invocation_id,
        workspace_id=workspace.id,
        verified_source=True,
        expires_at=now + timedelta(minutes=5),
    )
    lease_expires_at = now + timedelta(seconds=60)
    with connect() as conn:
        conn.execute(
            "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
            (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))),
        )
        conn.execute(
            "INSERT INTO external_agents(agent_id,workspace_id,created_by_user_id,provider,"
            "auth_type,enabled,secret_hash,payload) VALUES(%s,%s,%s,%s,%s,TRUE,NULL,%s)",
            (external_agent.id, workspace.id, user.id, external_agent.provider.value,
             external_agent.auth_type.value, Jsonb(external_agent.model_dump(mode="json"))),
        )
        conn.execute(
            "INSERT INTO approval_requests(approval_id,project_id,workspace_id,work_order_id,"
            "source_agent_id,source_invocation_id,status,expires_at,created_at,updated_at,version,payload) "
            "VALUES(%s,%s,%s,NULL,%s,%s,%s,%s,%s,%s,0,%s)",
            (approval.id, project.id, workspace.id, connector_id, invocation_id, approval.status.value,
             approval.expires_at, approval.created_at, approval.updated_at,
             Jsonb(approval.model_dump(mode="json"))),
        )
        conn.execute(
            "INSERT INTO approval_invocation_leases(approval_id,workspace_id,source_agent_id,"
            "source_invocation_id,heartbeat_at,expires_at) VALUES(%s,%s,%s,%s,%s,%s)",
            (approval.id, workspace.id, connector_id, invocation_id, now, lease_expires_at),
        )

    live = client.get(
        f"/connectors/{connector_id}/credentials",
        params={"source_invocation_id": invocation_id},
    )
    assert live.status_code == 200, live.text
    assert live.json()["invocation_live"] is True
    assert live.json()["source_invocation_id"] == invocation_id
    assert live.json()["invocation_expires_at"]

    assert main.app.state.postgres_repositories.approval_decisions.get_live_invocation_lease(
        workspace_id="another-workspace",
        source_agent_id=connector_id,
        source_invocation_id=invocation_id,
    ) is None
    assert main.app.state.postgres_repositories.approval_decisions.get_live_invocation_lease(
        workspace_id=workspace.id,
        source_agent_id="another-connector",
        source_invocation_id=invocation_id,
    ) is None
    assert main.app.state.postgres_repositories.approval_decisions.get_live_invocation_lease(
        workspace_id=workspace.id,
        source_agent_id=connector_id,
        source_invocation_id="another-invocation",
    ) is None

    with connect() as conn:
        conn.execute(
            "UPDATE approval_invocation_leases SET heartbeat_at=%s,expires_at=%s "
            "WHERE approval_id=%s",
            (now - timedelta(seconds=2), now - timedelta(seconds=1), approval.id),
        )
    expired = client.get(
        f"/connectors/{connector_id}/credentials",
        params={"source_invocation_id": invocation_id},
    )
    assert expired.status_code == 200
    assert expired.json()["invocation_live"] is False


def test_connector_management_routes_hide_other_workspaces(postgres_connector_route_runtime) -> None:
    connect, workspace, _user = postgres_connector_route_runtime
    client = TestClient(main.app)
    other_user_id = f"user_other_{uuid.uuid4().hex}"
    other_workspace = Workspace(
        id=f"workspace_other_{uuid.uuid4().hex}",
        name="Other workspace",
        owner_user_id=other_user_id,
    )
    other_user = User(
        id=other_user_id,
        email=f"other-{uuid.uuid4().hex}@example.test",
        role=UserRole.OWNER,
        password_hash="hash",
        password_salt="salt",
        workspace_id=other_workspace.id,
    )
    connector = AgentConnector(
        name="Other workspace connector",
        workspace_id=other_workspace.id,
        created_by_user_id=other_user.id,
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
            (other_workspace.id, Jsonb(other_workspace.model_dump(mode="json"))),
        )
        conn.execute(
            "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled," 
            "email_verified,payload) VALUES(%s,%s,%s,%s,%s,FALSE,FALSE,%s)",
            (other_user.id, other_workspace.id, other_user.email, other_user.email.lower(),
             other_user.role.value, Jsonb(other_user.model_dump(mode="json"))),
        )
    PostgresConnectorRepository(connect).create(
        connector=connector,
        audit_event=AuditEvent(
            event_type="connector.created",
            entity_id=connector.id,
            workspace_id=other_workspace.id,
            actor=other_user.email,
            details={"test": True},
        ),
    )
    with connect() as conn:
        assert conn.execute(
            "SELECT workspace_id FROM agent_connectors WHERE connector_id=%s",
            (connector.id,),
        ).fetchone() == (other_workspace.id,)

    assert workspace.id != other_workspace.id
    assert client.get(f"/connectors/{connector.id}").status_code == 404


def test_connector_credentials_and_status_transitions_commit_to_postgres(
    postgres_connector_route_runtime,
) -> None:
    connect, workspace, _user = postgres_connector_route_runtime
    before_connectors = set(store.agent_connectors)
    before_audits = {event.id for event in store.audit_events}
    client = TestClient(main.app)
    created = client.post("/connectors", json={"name": "Credential connector"})
    assert created.status_code == 200, created.text
    connector_id = created.json()["id"]

    issued = client.post(f"/connectors/{connector_id}/secret/issue", json={"note": "initial"})
    assert issued.status_code == 200, issued.text
    first_secret = issued.json()["secret"]
    with connect() as conn:
        stored = conn.execute(
            "SELECT payload FROM agent_connectors WHERE connector_id=%s AND workspace_id=%s",
            (connector_id, workspace.id),
        ).fetchone()[0]
    first_hash = stored["config"]["secret_hash"]
    assert first_hash != first_secret
    assert first_secret not in str(stored)

    rotated = client.post(f"/connectors/{connector_id}/secret/rotate", json={"note": "rotation"})
    assert rotated.status_code == 200, rotated.text
    second_secret = rotated.json()["secret"]
    assert second_secret != first_secret
    with connect() as conn:
        stored = conn.execute(
            "SELECT payload FROM agent_connectors WHERE connector_id=%s AND workspace_id=%s",
            (connector_id, workspace.id),
        ).fetchone()[0]
        audit_types = conn.execute(
            "SELECT event_type FROM audit_events WHERE workspace_id=%s AND entity_id=%s "
            "ORDER BY created_at,event_id",
            (workspace.id, connector_id),
        ).fetchall()
    assert stored["config"]["secret_hash"] not in {first_secret, second_secret, first_hash}
    assert second_secret not in str(stored)

    revoked = client.post(f"/connectors/{connector_id}/secret/revoke")
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["secret_configured"] is False
    credentials = client.get(f"/connectors/{connector_id}/credentials")
    assert credentials.status_code == 200
    assert credentials.json()["secret_configured"] is False
    disabled = client.post(f"/connectors/{connector_id}/disable")
    assert disabled.status_code == 200 and disabled.json()["status"] == "DISABLED"
    enabled = client.post(f"/connectors/{connector_id}/enable")
    assert enabled.status_code == 200 and enabled.json()["status"] == "ENABLED"
    with connect() as conn:
        before_manual_success = conn.execute(
            "SELECT payload FROM agent_connectors WHERE connector_id=%s", (connector_id,)
        ).fetchone()[0]
        before_health_audits = conn.execute(
            "SELECT count(*) FROM audit_events WHERE entity_id=%s AND event_type='connector.health_success'",
            (connector_id,),
        ).fetchone()[0]
    for outcome in ("success", "failure"):
        manual_health = client.post(f"/connectors/{connector_id}/health/{outcome}")
        assert manual_health.status_code == 409
        assert "authenticated connector traffic" in manual_health.json()["detail"]
    with connect() as conn:
        after_manual_success = conn.execute(
            "SELECT payload FROM agent_connectors WHERE connector_id=%s", (connector_id,)
        ).fetchone()[0]
        after_health_audits = conn.execute(
            "SELECT count(*) FROM audit_events WHERE entity_id=%s AND event_type='connector.health_success'",
            (connector_id,),
        ).fetchone()[0]
    assert after_manual_success == before_manual_success
    assert after_manual_success["last_used_at"] is None
    assert after_manual_success == before_manual_success
    assert after_health_audits == before_health_audits == 0
    mapper = client.put(
        f"/connectors/{connector_id}/schema-mapper",
        json={"enabled": True, "field_paths": {"title": "task.title"}, "defaults": {}},
    )
    assert mapper.status_code == 200, mapper.text
    assert mapper.json()["mapper_enabled"] is True
    mapper_read = client.get(f"/connectors/{connector_id}/schema-mapper")
    assert mapper_read.status_code == 200 and mapper_read.json()["mapper_enabled"] is True

    with connect() as conn:
        stored = conn.execute(
            "SELECT payload FROM agent_connectors WHERE connector_id=%s",
            (connector_id,),
        ).fetchone()[0]
        audit_types = conn.execute(
            "SELECT event_type FROM audit_events WHERE workspace_id=%s AND entity_id=%s "
            "ORDER BY created_at,event_id",
            (workspace.id, connector_id),
        ).fetchall()
    assert stored["secret_ref"] is None
    assert stored["config"]["secret_hash"] is None
    assert stored["config"]["secret_revoked_at"]
    assert [item[0] for item in audit_types] == [
        "connector.created",
        "connector.secret_issued",
        "connector.secret_rotated",
        "connector.secret_revoked",
        "connector.disabled",
        "connector.enabled",
        "connector.schema_mapper_updated",
    ]
    assert set(store.agent_connectors) == before_connectors
    assert {event.id for event in store.audit_events} == before_audits


def test_connector_simulation_reads_only_from_request_bound_postgres_repositories(
    postgres_connector_route_runtime,
) -> None:
    connect, workspace, user = postgres_connector_route_runtime
    repositories = main.app.state.postgres_repositories
    project = Project(
        name="PostgreSQL-only simulation project",
        description="Must not require a SQLite projection",
        workspace_id=workspace.id,
        created_by_user_id=user.id,
    )
    repositories.projects.create(
        project=project,
        audit_event=AuditEvent(
            event_type="project.created",
            entity_id=project.id,
            workspace_id=workspace.id,
            actor=user.email,
            details={"name": project.name, "mode": project.mode},
        ),
    )
    client = TestClient(main.app)
    created = client.post(
        "/connectors",
        json={
            "name": "PostgreSQL-only simulator",
            "provider_label": "ANTIGRAVITY",
            "allowed_project_ids": [project.id],
            "allowed_actions": ["CREATE_AGENT_TASK", "APPEND_AGENT_TASK_EVENT"],
        },
    )
    assert created.status_code == 200, created.text
    connector_id = created.json()["id"]
    assert client.post(f"/connectors/{connector_id}/secret/issue").status_code == 200

    task = AgentTask(
        provider=AgentProvider.OTHER,
        project_id=project.id,
        workspace_id=workspace.id,
        title="PostgreSQL-only task",
        goal="Prove simulation reads the app-bound task repository.",
        repository="ramgolladi1503-sys/aixion-control-tower",
        status=AgentTaskStatus.PLANNING,
        metadata={"connector_id": connector_id},
    )
    repositories.agent_tasks.create(
        task=task,
        event=AgentTaskEvent(
            task_id=task.id,
            event_type=AgentTaskEventType.TASK_CREATED,
            actor=f"connector:{connector_id}",
        ),
        audit=AuditEvent(
            event_type="connector.webhook_task_created",
            entity_id=task.id,
            workspace_id=workspace.id,
            actor=f"connector:{connector_id}",
            details={"connector_id": connector_id},
        ),
    )

    before_local_connectors = set(store.agent_connectors)
    before_local_audits = {event.id for event in store.audit_events}
    assert project.id not in store.projects
    assert task.id not in store.agent_tasks

    create_preview = client.post(
        f"/connectors/{connector_id}/simulate",
        json={
            "sample_payload": {
                "action": "CREATE_AGENT_TASK",
                "project_id": project.id,
                "title": "Preview from PostgreSQL",
                "goal": "Use the PostgreSQL project row.",
            }
        },
    )
    assert create_preview.status_code == 200, create_preview.text
    assert create_preview.json()["accepted"] is True
    assert create_preview.json()["task_preview"]["project_id"] == project.id

    append_preview = client.post(
        f"/connectors/{connector_id}/simulate",
        json={
            "sample_payload": {
                "action": "APPEND_AGENT_TASK_EVENT",
                "task_id": task.id,
                "event_type": "PLAN_RECEIVED",
                "message": "Preview from PostgreSQL",
                "status": "PLANNING",
            }
        },
    )
    assert append_preview.status_code == 200, append_preview.text
    assert append_preview.json()["accepted"] is True
    assert append_preview.json()["event_preview"]["task_id"] == task.id
    assert set(store.agent_connectors) == before_local_connectors
    assert {event.id for event in store.audit_events} == before_local_audits
    assert project.id not in store.projects
    assert task.id not in store.agent_tasks
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM agent_task_events WHERE task_id=%s", (task.id,)
        ).fetchone()[0] == 1


def test_connector_simulation_fails_closed_for_mismatched_postgres_bundle(
    postgres_connector_route_runtime,
) -> None:
    connect, _workspace, _user = postgres_connector_route_runtime
    client = TestClient(main.app)
    created = client.post("/connectors", json={"name": "Bound connector"})
    assert created.status_code == 200, created.text
    other_repositories = ApplicationRepositories.from_connector(connect)
    main.app.state.postgres_runtime = SimpleNamespace(repositories=other_repositories)

    response = client.post(
        f"/connectors/{created.json()['id']}/simulate",
        json={"sample_payload": {"action": "CREATE_AGENT_TASK"}},
    )

    assert response.status_code == 503
    assert "unavailable or mismatched" in response.json()["detail"]


def test_postgres_webhook_consumes_nonce_and_rate_limit_before_task_creation(
    postgres_connector_route_runtime,
) -> None:
    connect, workspace, user = postgres_connector_route_runtime
    project = Project(
        id=f"project_{uuid.uuid4().hex}",
        name="Webhook project",
        workspace_id=workspace.id,
        created_by_user_id=user.id,
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
            (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))),
        )
    client = TestClient(main.app)
    before_task_ids = set(store.agent_tasks)
    before_connector_ids = set(store.agent_connectors)
    created = client.post(
        "/connectors",
        json={
            "name": "PostgreSQL Antigravity hook",
            "provider_label": "ANTIGRAVITY",
            "auth_type": "HMAC",
            "allowed_project_ids": [project.id],
            "allowed_actions": ["CREATE_AGENT_TASK"],
            "rate_limit_per_minute": 1,
        },
    )
    assert created.status_code == 200, created.text
    connector_id = created.json()["id"]
    issued = client.post(f"/connectors/{connector_id}/secret/issue")
    assert issued.status_code == 200, issued.text
    secret = issued.json()["secret"]
    body = json.dumps(
        {
            "action": "CREATE_AGENT_TASK",
            "project_id": project.id,
            "title": "Persist connector task",
            "goal": "Create the task through PostgreSQL repositories.",
            "requires_approval": True,
        },
        separators=(",", ":"),
    ).encode()
    timestamp = str(int(time.time()))
    replay_nonce = f"nonce_{uuid.uuid4().hex}"
    def signature_for(nonce: str) -> str:
        return expected_hmac_v1_signature(
            hash_connector_secret(secret), timestamp=timestamp, nonce=nonce, body=body
        )
    headers = {
        "X-Aixion-Signature-Version": "v1",
        "X-Aixion-Timestamp": timestamp,
        "X-Aixion-Nonce": replay_nonce,
        "X-Aixion-Connector-Signature": f"v1={signature_for(replay_nonce)}",
        "Content-Type": "application/json",
    }

    accepted = client.post(f"/connectors/{connector_id}/webhook", content=body, headers=headers)
    replayed = client.post(f"/connectors/{connector_id}/webhook", content=body, headers=headers)
    mismatched_body = json.dumps(
        {
            "action": "CREATE_AGENT_TASK",
            "project_id": project.id,
            "title": "Different request reusing the nonce",
            "goal": "A nonce remains single-use even when a new valid body is signed.",
        },
        separators=(",", ":"),
    ).encode()
    mismatched_signature = expected_hmac_v1_signature(
        hash_connector_secret(secret),
        timestamp=timestamp,
        nonce=replay_nonce,
        body=mismatched_body,
    )
    mismatched = client.post(
        f"/connectors/{connector_id}/webhook",
        content=mismatched_body,
        headers={
            **headers,
            "X-Aixion-Connector-Signature": f"v1={mismatched_signature}",
        },
    )
    limited_nonce = f"nonce_{uuid.uuid4().hex}"
    limited_headers = {
        **headers,
        "X-Aixion-Nonce": limited_nonce,
        "X-Aixion-Connector-Signature": f"v1={signature_for(limited_nonce)}",
    }
    limited = client.post(
        f"/connectors/{connector_id}/webhook", content=body, headers=limited_headers
    )

    assert accepted.status_code == 200, accepted.text
    assert replayed.status_code == 401
    assert replayed.json()["detail"] == "Connector HMAC nonce has already been used"
    assert mismatched.status_code == 401
    assert mismatched.json()["detail"] == "Connector HMAC nonce has already been used"
    assert limited.status_code == 429
    task_id = accepted.json()["task_id"]
    with connect() as conn:
        task_rows = conn.execute(
            "SELECT task_id,workspace_id,payload FROM agent_tasks WHERE task_id=%s", (task_id,)
        ).fetchall()
        connector_payload = conn.execute(
            "SELECT payload FROM agent_connectors WHERE connector_id=%s", (connector_id,)
        ).fetchone()[0]
        connector_audits = conn.execute(
            "SELECT event_type FROM audit_events WHERE entity_id=%s ORDER BY created_at,event_id",
            (connector_id,),
        ).fetchall()
    assert len(task_rows) == 1
    assert task_rows[0][0:2] == (task_id, workspace.id)
    assert task_rows[0][2]["provider"] == "ANTIGRAVITY"
    assert task_rows[0][2]["external_agent_id"] is None
    assert task_rows[0][2]["metadata"]["connector_id"] == connector_id
    assert connector_payload["last_used_at"] is not None
    assert replay_nonce in connector_payload["config"]["hmac_nonce_history"]
    assert limited_nonce in connector_payload["config"]["hmac_nonce_history"]
    assert connector_payload["config"]["rate_limit_request_count"] == 2
    assert [row[0] for row in connector_audits].count("connector.webhook_authenticated") == 1
    assert [row[0] for row in connector_audits].count("connector.webhook_auth_failed") == 2
    assert [row[0] for row in connector_audits].count("connector.webhook_rate_limited") == 1
    assert set(store.agent_tasks) == before_task_ids
    assert connector_id not in store.agent_connectors
    assert set(store.agent_connectors) == before_connector_ids


@pytest.mark.parametrize(
    ("failure_entity_type", "failure_event_type"),
    [
        ("agent_task", "connector.webhook_task_created"),
        ("connector", "connector.webhook_authenticated"),
    ],
)
def test_postgres_webhook_create_rolls_back_auth_nonce_task_and_audits_on_write_failure(
    postgres_connector_route_runtime,
    failure_entity_type: str,
    failure_event_type: str,
) -> None:
    connect, workspace, user = postgres_connector_route_runtime
    project = Project(
        id=f"project_{uuid.uuid4().hex}",
        name="Atomic webhook project",
        workspace_id=workspace.id,
        created_by_user_id=user.id,
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
            (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))),
        )
    client = TestClient(main.app)
    created = client.post(
        "/connectors",
        json={
            "name": "Atomic webhook connector",
            "provider_label": "ANTIGRAVITY",
            "auth_type": "HMAC",
            "allowed_project_ids": [project.id],
            "allowed_actions": ["CREATE_AGENT_TASK"],
        },
    )
    assert created.status_code == 200, created.text
    connector_id = created.json()["id"]
    issued = client.post(f"/connectors/{connector_id}/secret/issue")
    assert issued.status_code == 200, issued.text
    secret = issued.json()["secret"]
    with connect() as conn:
        original_connector_payload = conn.execute(
            "SELECT payload FROM agent_connectors WHERE connector_id=%s", (connector_id,)
        ).fetchone()[0]
        conn.execute(
            "CREATE FUNCTION reject_webhook_audit() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN IF NEW.entity_type = '" + failure_entity_type + "' AND NEW.event_type = '"
            + failure_event_type + "' THEN RAISE EXCEPTION 'injected webhook audit failure'; "
            "END IF; RETURN NEW; END $$"
        )
        conn.execute(
            "CREATE TRIGGER reject_webhook_audit BEFORE INSERT ON audit_events "
            "FOR EACH ROW EXECUTE FUNCTION reject_webhook_audit()"
        )
    body = json.dumps(
        {
            "action": "CREATE_AGENT_TASK",
            "project_id": project.id,
            "title": "Must roll back",
            "goal": "A failed acceptance transaction must leave no partial state.",
        },
        separators=(",", ":"),
    ).encode()
    timestamp = str(int(time.time()))
    nonce = f"atomic_{uuid.uuid4().hex}"
    signature = expected_hmac_v1_signature(
        hash_connector_secret(secret), timestamp=timestamp, nonce=nonce, body=body
    )
    headers = {
        "X-Aixion-Signature-Version": "v1",
        "X-Aixion-Timestamp": timestamp,
        "X-Aixion-Nonce": nonce,
        "X-Aixion-Connector-Signature": f"v1={signature}",
        "Content-Type": "application/json",
    }

    response = TestClient(main.app, raise_server_exceptions=False).post(
        f"/connectors/{connector_id}/webhook", content=body, headers=headers
    )

    assert response.status_code == 500
    with connect() as conn:
        stored_connector_payload = conn.execute(
            "SELECT payload FROM agent_connectors WHERE connector_id=%s", (connector_id,)
        ).fetchone()[0]
        task_count = conn.execute(
            "SELECT count(*) FROM agent_tasks WHERE workspace_id=%s", (workspace.id,)
        ).fetchone()[0]
        webhook_audits = conn.execute(
            "SELECT count(*) FROM audit_events WHERE entity_id=%s AND event_type LIKE 'connector.webhook_%%'",
            (connector_id,),
        ).fetchone()[0]
    assert stored_connector_payload == original_connector_payload
    assert task_count == 0
    assert webhook_audits == 0
    assert nonce not in stored_connector_payload["config"].get("hmac_nonce_history", {})


def test_postgres_webhook_append_failure_rolls_back_connector_and_task_event(
    postgres_connector_route_runtime,
) -> None:
    connect, workspace, user = postgres_connector_route_runtime
    project = Project(
        id=f"project_{uuid.uuid4().hex}",
        name="Atomic append project",
        workspace_id=workspace.id,
        created_by_user_id=user.id,
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
            (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))),
        )
    client = TestClient(main.app)
    created = client.post(
        "/connectors",
        json={
            "name": "Atomic append connector",
            "provider_label": "ANTIGRAVITY",
            "auth_type": "HMAC",
            "allowed_project_ids": [project.id],
            "allowed_actions": ["CREATE_AGENT_TASK", "APPEND_AGENT_TASK_EVENT"],
        },
    )
    assert created.status_code == 200, created.text
    connector_id = created.json()["id"]
    issued = client.post(f"/connectors/{connector_id}/secret/issue")
    assert issued.status_code == 200, issued.text
    secret = issued.json()["secret"]

    def send(body: bytes, nonce: str):
        timestamp = str(int(time.time()))
        signature = expected_hmac_v1_signature(
            hash_connector_secret(secret), timestamp=timestamp, nonce=nonce, body=body
        )
        return client.post(
            f"/connectors/{connector_id}/webhook",
            content=body,
            headers={
                "X-Aixion-Signature-Version": "v1",
                "X-Aixion-Timestamp": timestamp,
                "X-Aixion-Nonce": nonce,
                "X-Aixion-Connector-Signature": f"v1={signature}",
                "Content-Type": "application/json",
            },
        )

    create_body = json.dumps(
        {
            "action": "CREATE_AGENT_TASK",
            "project_id": project.id,
            "title": "Append rollback target",
            "goal": "Retain its original status and event list.",
        },
        separators=(",", ":"),
    ).encode()
    accepted = send(create_body, f"create_{uuid.uuid4().hex}")
    assert accepted.status_code == 200, accepted.text
    task_id = accepted.json()["task_id"]
    with connect() as conn:
        original_connector_payload = conn.execute(
            "SELECT payload FROM agent_connectors WHERE connector_id=%s", (connector_id,)
        ).fetchone()[0]
        original_task = conn.execute(
            "SELECT status,payload FROM agent_tasks WHERE task_id=%s", (task_id,)
        ).fetchone()
        original_event_count = conn.execute(
            "SELECT count(*) FROM agent_task_events WHERE task_id=%s", (task_id,)
        ).fetchone()[0]
        conn.execute(
            "CREATE FUNCTION reject_append_audit() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN IF NEW.entity_type = 'agent_task' AND NEW.event_type = "
            "'connector.webhook_task_event_appended' THEN RAISE EXCEPTION 'injected append audit failure'; "
            "END IF; RETURN NEW; END $$"
        )
        conn.execute(
            "CREATE TRIGGER reject_append_audit BEFORE INSERT ON audit_events "
            "FOR EACH ROW EXECUTE FUNCTION reject_append_audit()"
        )
    append_body = json.dumps(
        {
            "action": "APPEND_AGENT_TASK_EVENT",
            "task_id": task_id,
            "event_type": "NOTE",
            "message": "This append must roll back.",
            "status": "EXECUTING",
        },
        separators=(",", ":"),
    ).encode()
    append_nonce = f"append_{uuid.uuid4().hex}"
    append_timestamp = str(int(time.time()))
    append_signature = expected_hmac_v1_signature(
        hash_connector_secret(secret), timestamp=append_timestamp,
        nonce=append_nonce, body=append_body,
    )
    failed = TestClient(main.app, raise_server_exceptions=False).post(
        f"/connectors/{connector_id}/webhook",
        content=append_body,
        headers={
            "X-Aixion-Signature-Version": "v1",
            "X-Aixion-Timestamp": append_timestamp,
            "X-Aixion-Nonce": append_nonce,
            "X-Aixion-Connector-Signature": f"v1={append_signature}",
            "Content-Type": "application/json",
        },
    )
    assert failed.status_code == 500
    with connect() as conn:
        final_connector_payload = conn.execute(
            "SELECT payload FROM agent_connectors WHERE connector_id=%s", (connector_id,)
        ).fetchone()[0]
        final_task = conn.execute(
            "SELECT status,payload FROM agent_tasks WHERE task_id=%s", (task_id,)
        ).fetchone()
        final_event_count = conn.execute(
            "SELECT count(*) FROM agent_task_events WHERE task_id=%s", (task_id,)
        ).fetchone()[0]
    assert final_connector_payload == original_connector_payload
    assert final_task == original_task
    assert final_event_count == original_event_count
    assert append_nonce not in final_connector_payload["config"].get("hmac_nonce_history", {})


@pytest.mark.parametrize(
    ("body", "expected_status"),
    [
        (b"{malformed-json", 400),
        (b'{"action":"APPEND_AGENT_TASK_EVENT","task_id":"unused"}', 403),
    ],
)
def test_postgres_webhook_refusals_keep_authentication_and_refusal_audits(
    postgres_connector_route_runtime,
    body: bytes,
    expected_status: int,
) -> None:
    connect, workspace, user = postgres_connector_route_runtime
    project = Project(
        id=f"project_{uuid.uuid4().hex}",
        name="Refused webhook project",
        workspace_id=workspace.id,
        created_by_user_id=user.id,
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
            (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))),
        )
    client = TestClient(main.app)
    created = client.post(
        "/connectors",
        json={
            "name": "Refused webhook connector",
            "provider_label": "ANTIGRAVITY",
            "auth_type": "HMAC",
            "allowed_project_ids": [project.id],
            "allowed_actions": ["CREATE_AGENT_TASK"],
        },
    )
    assert created.status_code == 200, created.text
    connector_id = created.json()["id"]
    issued = client.post(f"/connectors/{connector_id}/secret/issue")
    assert issued.status_code == 200, issued.text
    secret = issued.json()["secret"]
    nonce = f"refused_{uuid.uuid4().hex}"
    timestamp = str(int(time.time()))
    signature = expected_hmac_v1_signature(
        hash_connector_secret(secret), timestamp=timestamp, nonce=nonce, body=body
    )

    response = client.post(
        f"/connectors/{connector_id}/webhook",
        content=body,
        headers={
            "X-Aixion-Signature-Version": "v1",
            "X-Aixion-Timestamp": timestamp,
            "X-Aixion-Nonce": nonce,
            "X-Aixion-Connector-Signature": f"v1={signature}",
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == expected_status, response.text
    with connect() as conn:
        payload = conn.execute(
            "SELECT payload FROM agent_connectors WHERE connector_id=%s", (connector_id,)
        ).fetchone()[0]
        webhook_audits = conn.execute(
            "SELECT event_type FROM audit_events WHERE entity_id=%s "
            "AND event_type LIKE 'connector.webhook_%%' ORDER BY created_at,event_id",
            (connector_id,),
        ).fetchall()
        task_count = conn.execute(
            "SELECT count(*) FROM agent_tasks WHERE workspace_id=%s", (workspace.id,)
        ).fetchone()[0]
    assert nonce in payload["config"]["hmac_nonce_history"]
    assert [row[0] for row in webhook_audits] == [
        "connector.webhook_authenticated",
        "connector.webhook_refused",
    ]
    assert task_count == 0


def test_postgres_webhook_concurrent_duplicate_nonce_accepts_at_most_once(
    postgres_connector_route_runtime,
) -> None:
    connect, workspace, user = postgres_connector_route_runtime
    project = Project(
        id=f"project_{uuid.uuid4().hex}",
        name="Concurrent webhook project",
        workspace_id=workspace.id,
        created_by_user_id=user.id,
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
            (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))),
        )
    setup_client = TestClient(main.app)
    created = setup_client.post(
        "/connectors",
        json={
            "name": "Concurrent webhook connector",
            "provider_label": "ANTIGRAVITY",
            "auth_type": "HMAC",
            "allowed_project_ids": [project.id],
            "allowed_actions": ["CREATE_AGENT_TASK"],
        },
    )
    assert created.status_code == 200, created.text
    connector_id = created.json()["id"]
    issued = setup_client.post(f"/connectors/{connector_id}/secret/issue")
    assert issued.status_code == 200, issued.text
    secret = issued.json()["secret"]
    body = json.dumps(
        {
            "action": "CREATE_AGENT_TASK",
            "project_id": project.id,
            "title": "Concurrent replay target",
            "goal": "Only one transaction may consume this nonce and create the task.",
        },
        separators=(",", ":"),
    ).encode()
    nonce = f"concurrent_{uuid.uuid4().hex}"
    timestamp = str(int(time.time()))
    signature = expected_hmac_v1_signature(
        hash_connector_secret(secret), timestamp=timestamp, nonce=nonce, body=body
    )
    headers = {
        "X-Aixion-Signature-Version": "v1",
        "X-Aixion-Timestamp": timestamp,
        "X-Aixion-Nonce": nonce,
        "X-Aixion-Connector-Signature": f"v1={signature}",
        "Content-Type": "application/json",
    }
    start = Barrier(2)
    clients = [TestClient(main.app), TestClient(main.app)]

    def request_once(client: TestClient) -> int:
        start.wait(timeout=5)
        return client.post(
            f"/connectors/{connector_id}/webhook", content=body, headers=headers
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(request_once, clients))

    assert responses.count(200) == 1
    assert responses.count(401) == 1
    with connect() as conn:
        tasks = conn.execute(
            "SELECT task_id FROM agent_tasks WHERE workspace_id=%s", (workspace.id,)
        ).fetchall()
        task_events = conn.execute(
            "SELECT event_id FROM agent_task_events WHERE workspace_id=%s", (workspace.id,)
        ).fetchall()
        accepted_audits = conn.execute(
            "SELECT count(*) FROM audit_events WHERE event_type='connector.webhook_task_created' "
            "AND entity_type='agent_task'"
        ).fetchone()[0]
        authenticated_audits = conn.execute(
            "SELECT count(*) FROM audit_events WHERE entity_id=%s "
            "AND event_type='connector.webhook_authenticated'",
            (connector_id,),
        ).fetchone()[0]
    assert len(tasks) == 1
    assert len(task_events) == 1
    assert accepted_audits == 1
    assert authenticated_audits == 1


def test_postgres_webhook_stale_connector_with_unrelated_update_returns_conflict(
    postgres_connector_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, workspace, user = postgres_connector_route_runtime
    project = Project(
        id=f"project_{uuid.uuid4().hex}",
        name="Stale webhook project",
        workspace_id=workspace.id,
        created_by_user_id=user.id,
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
            (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))),
        )
    client = TestClient(main.app)
    created = client.post(
        "/connectors",
        json={
            "name": "Stale webhook connector",
            "provider_label": "ANTIGRAVITY",
            "auth_type": "HMAC",
            "allowed_project_ids": [project.id],
            "allowed_actions": ["CREATE_AGENT_TASK"],
        },
    )
    assert created.status_code == 200, created.text
    connector_id = created.json()["id"]
    issued = client.post(f"/connectors/{connector_id}/secret/issue")
    assert issued.status_code == 200, issued.text
    secret = issued.json()["secret"]

    def send(title: str, nonce: str):
        body = json.dumps(
            {
                "action": "CREATE_AGENT_TASK",
                "project_id": project.id,
                "title": title,
                "goal": "Detect stale connector state without misclassifying old nonce history.",
            },
            separators=(",", ":"),
        ).encode()
        timestamp = str(int(time.time()))
        signature = expected_hmac_v1_signature(
            hash_connector_secret(secret), timestamp=timestamp, nonce=nonce, body=body
        )
        return client.post(
            f"/connectors/{connector_id}/webhook",
            content=body,
            headers={
                "X-Aixion-Signature-Version": "v1",
                "X-Aixion-Timestamp": timestamp,
                "X-Aixion-Nonce": nonce,
                "X-Aixion-Connector-Signature": f"v1={signature}",
                "Content-Type": "application/json",
            },
        )

    previously_consumed_nonce = f"prior_{uuid.uuid4().hex}"
    accepted = send("Previously committed task", previously_consumed_nonce)
    assert accepted.status_code == 200, accepted.text

    original_commit = PostgresConnectorRepository.commit_webhook_action

    def apply_unrelated_concurrent_update(
        repository: PostgresConnectorRepository,
        *,
        connector: AgentConnector,
        request_nonce: str | None,
        expected_updated_at,
        audit_event: AuditEvent,
        write_action,
    ):
        durable = repository.get_for_authentication(connector.id)
        assert durable is not None
        prior_updated_at = durable.updated_at
        durable.config = {**durable.config, "concurrent_test_marker": "updated"}
        durable.updated_at = min(
            prior_updated_at + timedelta(seconds=1),
            connector.updated_at - timedelta(microseconds=1),
        )
        assert durable.updated_at > prior_updated_at
        repository.update(
            connector=durable,
            expected_updated_at=prior_updated_at,
            audit_event=AuditEvent(
                event_type="connector.concurrent_test_update",
                entity_id=durable.id,
                workspace_id=durable.workspace_id,
                actor=f"connector:{durable.id}",
                details={"test_only": True},
            ),
        )
        return original_commit(
            repository,
            connector=connector,
            request_nonce=request_nonce,
            expected_updated_at=expected_updated_at,
            audit_event=audit_event,
            write_action=write_action,
        )

    monkeypatch.setattr(
        PostgresConnectorRepository,
        "commit_webhook_action",
        apply_unrelated_concurrent_update,
    )
    fresh_nonce = f"fresh_{uuid.uuid4().hex}"
    stale_request = send("Must not be created", fresh_nonce)
    assert stale_request.status_code == 409

    with connect() as conn:
        payload = conn.execute(
            "SELECT payload FROM agent_connectors WHERE connector_id=%s", (connector_id,)
        ).fetchone()[0]
        task_count = conn.execute(
            "SELECT count(*) FROM agent_tasks WHERE workspace_id=%s", (workspace.id,)
        ).fetchone()[0]
    assert payload["config"]["concurrent_test_marker"] == "updated"
    assert fresh_nonce not in payload["config"].get("hmac_nonce_history", {})
    assert task_count == 1

    replay = send("Previously committed task", previously_consumed_nonce)
    assert replay.status_code == 401, replay.text


def test_postgres_hmac_failure_updates_postgres_without_sqlite_persist(
    postgres_connector_route_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect, workspace, _user = postgres_connector_route_runtime
    client = TestClient(main.app)
    created = client.post(
        "/connectors",
        json={
            "name": "PostgreSQL HMAC failure test",
            "provider_label": "ANTIGRAVITY",
            "auth_type": "HMAC",
            "allowed_actions": ["CREATE_AGENT_TASK"],
        },
    )
    assert created.status_code == 200, created.text
    connector_id = created.json()["id"]
    issued = client.post(f"/connectors/{connector_id}/secret/issue")
    assert issued.status_code == 200, issued.text
    secret = issued.json()["secret"]
    body = json.dumps({"action": "CREATE_AGENT_TASK"}, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    nonce = f"bad_signature_{uuid.uuid4().hex}"
    valid_signature = expected_hmac_v1_signature(
        hash_connector_secret(secret), timestamp=timestamp, nonce=nonce, body=body
    )
    invalid_signature = ("0" if valid_signature[0] != "0" else "1") + valid_signature[1:]
    headers = {
        "X-Aixion-Signature-Version": "v1",
        "X-Aixion-Timestamp": timestamp,
        "X-Aixion-Nonce": nonce,
        "X-Aixion-Connector-Signature": f"v1={invalid_signature}",
        "Content-Type": "application/json",
    }
    before_connector_ids = set(store.agent_connectors)
    before_audit_ids = {event.id for event in store.audit_events}
    persist_calls: list[str] = []
    monkeypatch.setattr(store, "persist", lambda: persist_calls.append("called"))

    response = client.post(
        f"/connectors/{connector_id}/webhook", content=body, headers=headers
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid connector HMAC signature"
    assert persist_calls == []
    assert set(store.agent_connectors) == before_connector_ids
    assert {event.id for event in store.audit_events} == before_audit_ids
    with connect() as conn:
        connector_payload = conn.execute(
            "SELECT payload FROM agent_connectors WHERE connector_id=%s AND workspace_id=%s",
            (connector_id, workspace.id),
        ).fetchone()[0]
        audit_rows = conn.execute(
            "SELECT event_type,workspace_id FROM audit_events "
            "WHERE entity_id=%s AND workspace_id=%s "
            "AND event_type='connector.webhook_auth_failed'",
            (connector_id, workspace.id),
        ).fetchall()
    assert connector_payload["failed_auth_count"] == 1
    assert connector_payload["last_error"] == "Invalid connector HMAC signature"
    assert [(row[0], row[1]) for row in audit_rows] == [
        ("connector.webhook_auth_failed", workspace.id)
    ]


def test_postgres_webhook_rejects_mixed_repository_runtime(
    postgres_connector_route_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    repositories = main.app.state.postgres_repositories
    monkeypatch.setattr(
        main.app.state,
        "postgres_repositories",
        SimpleNamespace(
            connectors=repositories.connectors,
            agent_tasks=None,
            projects=repositories.projects,
            workspace_membership=repositories.workspace_membership,
        ),
    )
    response = TestClient(main.app).post(
        "/connectors/missing/webhook",
        json={"action": "CREATE_AGENT_TASK"},
    )
    assert response.status_code == 503
    assert "PostgreSQL connector repository runtime is unavailable or mismatched" in response.json()["detail"]
