from __future__ import annotations

import os
from datetime import timedelta
import uuid

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from fastapi.testclient import TestClient

from app.connector_credentials import verify_connector_secret
from app.approval_decisions import approval_decision_repository
from app.main import app
from app.models import (
    ApprovalRequest,
    ApprovalStatus,
    AgentProvider,
    Project,
    RiskAssessment,
    RiskLevel,
    Workspace,
    now_utc,
)
from app.store import store

client = TestClient(app)


def setup_function() -> None:
    store.reset()


def _project() -> Project:
    project = Project(name="Connector Credentials", description="demo")
    store.projects[project.id] = project
    store.persist()
    return project


def _connector(project_id: str) -> dict:
    response = client.post(
        "/connectors",
        json={
            "name": "OpenClaw Bridge",
            "connector_type": "LOCAL_BRIDGE",
            "provider_label": "OPENCLAW",
            "endpoint_url": "http://localhost:8787",
            "auth_type": "HMAC",
            "allowed_project_ids": [project_id],
            "allowed_repositories": ["ramgolladi1503-sys/aixion-control-tower"],
            "allowed_actions": ["CREATE_AGENT_TASK"],
            "rate_limit_per_minute": 30,
            "enabled": True,
            "config": {"profile": "openclaw"},
        },
    )
    assert response.status_code == 200
    return response.json()


def test_issue_connector_secret_returns_secret_once_and_redacts_public_config() -> None:
    project = _project()
    connector = _connector(project.id)

    response = client.post(f"/connectors/{connector['id']}/secret/issue", json={"note": "initial"})

    assert response.status_code == 200
    body = response.json()
    assert body["secret"].startswith("aixion_connector_")
    assert body["secret_hint"] == body["secret"][-8:]
    assert body["connector"]["secret_configured"] is True
    assert "secret_hash" not in body["connector"]["config"]
    assert "secret_hint" not in body["connector"]["config"]
    stored = store.agent_connectors[connector["id"]]
    assert verify_connector_secret(stored, body["secret"]) is True
    assert any(event.event_type == "connector.secret_issued" for event in store.audit_events)


def test_connector_credential_status_reflects_rotation_and_revocation() -> None:
    project = _project()
    connector = _connector(project.id)
    first = client.post(f"/connectors/{connector['id']}/secret/issue").json()
    rotated = client.post(f"/connectors/{connector['id']}/secret/rotate", json={"note": "rotate"}).json()

    assert rotated["secret"] != first["secret"]
    credentials = client.get(f"/connectors/{connector['id']}/credentials")
    assert credentials.status_code == 200
    assert credentials.json()["secret_configured"] is True
    assert credentials.json()["secret_rotated_at"] is not None
    assert verify_connector_secret(store.agent_connectors[connector["id"]], rotated["secret"]) is True
    assert verify_connector_secret(store.agent_connectors[connector["id"]], first["secret"]) is False

    revoked = client.post(f"/connectors/{connector['id']}/secret/revoke")
    assert revoked.status_code == 200
    assert revoked.json()["secret_configured"] is False
    credentials_after_revoke = client.get(f"/connectors/{connector['id']}/credentials").json()
    assert credentials_after_revoke["secret_configured"] is False
    assert credentials_after_revoke["secret_revoked_at"] is not None
    assert any(event.event_type == "connector.secret_rotated" for event in store.audit_events)
    assert any(event.event_type == "connector.secret_revoked" for event in store.audit_events)


def test_manual_connector_health_success_cannot_assert_liveness_or_clear_auth_error() -> None:
    project = _project()
    connector = _connector(project.id)
    before = store.agent_connectors[connector["id"]].model_copy(deep=True)
    audit_ids = {event.id for event in store.audit_events}

    for outcome in ("success", "failure"):
        response = client.post(f"/connectors/{connector['id']}/health/{outcome}")
        assert response.status_code == 409
        assert "authenticated connector traffic" in response.json()["detail"]
    after = store.agent_connectors[connector["id"]]
    assert after == before
    assert before.last_used_at is None
    assert after.last_used_at is None
    assert {event.id for event in store.audit_events} == audit_ids


def _seed_invocation_lease(connector_id: str, project_id: str, invocation_id: str) -> str:
    connector = store.agent_connectors[connector_id]
    now = now_utc()
    approval = ApprovalRequest(
        id=f"approval_{invocation_id}",
        project_id=project_id,
        title="Invocation liveness test",
        summary="Scoped liveness fixture",
        agent_name=connector.name,
        target_branch="test",
        files=[],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.PENDING_REVIEW,
        source_provider=AgentProvider.ANTIGRAVITY,
        source_agent_id=connector.id,
        source_agent_name=connector.name,
        source_invocation_id=invocation_id,
        workspace_id=connector.workspace_id,
        verified_source=True,
        expires_at=now + timedelta(minutes=5),
    )
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES('approval_request',?,?)",
            (approval.id, approval.model_dump_json()),
        )
        conn.execute(
            "INSERT INTO approval_invocation_leases "
            "(approval_id,workspace_id,source_agent_id,source_invocation_id,heartbeat_at,expires_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                approval.id,
                connector.workspace_id,
                connector.id,
                invocation_id,
                now.isoformat(),
                (now + timedelta(seconds=60)).isoformat(),
            ),
        )
    return approval.id


def test_connector_credentials_reports_only_exact_live_invocation_lease() -> None:
    project = _project()
    connector = _connector(project.id)
    workspace = Workspace(name="Connector invocation status", owner_user_id="dev_user")
    store.workspaces[workspace.id] = workspace
    store.projects[project.id].workspace_id = workspace.id
    store.agent_connectors[connector["id"]].workspace_id = workspace.id
    store.persist()
    invocation_id = f"antigravity:v1:credential-status:{uuid.uuid4().hex}"
    approval_id = _seed_invocation_lease(connector["id"], project.id, invocation_id)

    live = client.get(
        f"/connectors/{connector['id']}/credentials",
        params={"source_invocation_id": invocation_id},
    )
    assert live.status_code == 200, live.text
    assert live.json()["source_invocation_id"] == invocation_id
    assert live.json()["invocation_live"] is True
    assert live.json()["invocation_expires_at"]

    wrong_invocation = client.get(
        f"/connectors/{connector['id']}/credentials",
        params={"source_invocation_id": "another-invocation"},
    )
    assert wrong_invocation.status_code == 200
    assert wrong_invocation.json()["invocation_live"] is False
    assert wrong_invocation.json()["invocation_heartbeat_at"] is None
    assert approval_decision_repository.get_live_invocation_lease(
        workspace_id="another-workspace",
        source_agent_id=connector["id"],
        source_invocation_id=invocation_id,
    ) is None
    assert approval_decision_repository.get_live_invocation_lease(
        workspace_id=store.agent_connectors[connector["id"]].workspace_id,
        source_agent_id="another-connector",
        source_invocation_id=invocation_id,
    ) is None

    with store._connect() as conn:
        conn.execute(
            "UPDATE approval_invocation_leases SET expires_at=? WHERE approval_id=?",
            ((now_utc() - timedelta(seconds=1)).isoformat(), approval_id),
        )
    expired = client.get(
        f"/connectors/{connector['id']}/credentials",
        params={"source_invocation_id": invocation_id},
    )
    assert expired.status_code == 200
    assert expired.json()["invocation_live"] is False
    assert expired.json()["health_status"] == "UNKNOWN"
    assert expired.json()["last_used_at"] is None


def test_public_connector_response_never_exposes_secret_material() -> None:
    project = _project()
    connector = _connector(project.id)
    issued = client.post(f"/connectors/{connector['id']}/secret/issue").json()

    fetched = client.get(f"/connectors/{connector['id']}").json()
    listed = client.get("/connectors").json()[0]

    assert issued["secret"] not in str(fetched)
    assert issued["secret"] not in str(listed)
    assert "secret_hash" not in str(fetched)
    assert "secret_hash" not in str(listed)
    assert fetched["secret_configured"] is True
    assert listed["secret_configured"] is True
