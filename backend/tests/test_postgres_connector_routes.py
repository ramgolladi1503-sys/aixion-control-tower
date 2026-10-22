from __future__ import annotations

import os
import json
import time
import uuid
from collections.abc import Callable, Iterator
from types import SimpleNamespace

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app import main
import app.application_repository_access as repository_access
import app.connector_routes as connector_routes
from app.application_repositories import ApplicationRepositories
from app.auth import require_owner
from app.connector_models import AgentConnector
from app.connector_repository import PostgresConnectorRepository
from app.connector_credentials import hash_connector_secret
from app.connector_webhook import expected_hmac_v1_signature
from app.models import AuditEvent, AuthUser, Project, User, UserRole, Workspace
from app.postgres_database import run_postgres_migrations
from app.store import store


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
    healthy = client.post(f"/connectors/{connector_id}/health/success")
    assert healthy.status_code == 200 and healthy.json()["health_status"] == "HEALTHY"
    failing = client.post(
        f"/connectors/{connector_id}/health/failure", json={"note": "test failure"}
    )
    assert failing.status_code == 200 and failing.json()["health_status"] == "DEGRADED"
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
        "connector.health_success",
        "connector.health_failure",
        "connector.schema_mapper_updated",
    ]
    assert set(store.agent_connectors) == before_connectors
    assert {event.id for event in store.audit_events} == before_audits


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
    assert replay_nonce in connector_payload["config"]["hmac_nonce_history"]
    assert limited_nonce in connector_payload["config"]["hmac_nonce_history"]
    assert connector_payload["config"]["rate_limit_request_count"] == 2
    assert [row[0] for row in connector_audits].count("connector.webhook_authenticated") == 1
    assert [row[0] for row in connector_audits].count("connector.webhook_auth_failed") == 1
    assert [row[0] for row in connector_audits].count("connector.webhook_rate_limited") == 1
    assert set(store.agent_tasks) == before_task_ids
    assert connector_id not in store.agent_connectors
    assert set(store.agent_connectors) == before_connector_ids


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
