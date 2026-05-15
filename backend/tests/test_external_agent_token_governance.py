from __future__ import annotations

import os
from datetime import timedelta

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from fastapi.testclient import TestClient

from app.main import app
from app.models import Project, now_utc
from app.store import store

client = TestClient(app)
REPOSITORY = "ramgolladi1503-sys/aixion-control-tower"


def setup_function() -> None:
    store.reset()


def _project() -> Project:
    project = Project(name="Token Governance", description="demo")
    store.projects[project.id] = project
    return project


def _headers(agent_id: str, token: str) -> dict[str, str]:
    return {"X-Aixion-Agent-Id": agent_id, "X-Aixion-Agent-Token": token}


def _task_payload(project_id: str) -> dict:
    return {
        "provider": "MANUAL",
        "project_id": project_id,
        "title": "Token-governed task",
        "goal": "Prove token governance works before external task intake.",
        "requested_action": "GENERATE_DOCS",
        "repository": REPOSITORY,
        "requires_approval": True,
    }


def _register_agent(
    project: Project,
    *,
    token_expires_at: str | None = None,
    rate_limit_per_minute: int = 60,
) -> tuple[str, str]:
    payload = {
        "provider": "CHATGPT",
        "display_name": "Governed GPT",
        "auth_type": "API_KEY",
        "allowed_project_ids": [project.id],
        "allowed_repositories": [REPOSITORY],
        "allowed_actions": ["CREATE_AGENT_TASK", "READ_AGENT_TASK"],
        "enabled": True,
        "rate_limit_per_minute": rate_limit_per_minute,
    }
    if token_expires_at is not None:
        payload["token_expires_at"] = token_expires_at
    response = client.post("/agents", json=payload)
    assert response.status_code == 200
    body = response.json()
    return body["agent"]["id"], body["agent_token"]


def test_agent_registration_exposes_owner_visible_credential_status() -> None:
    project = _project()
    expires_at = (now_utc() + timedelta(days=1)).isoformat()
    agent_id, _ = _register_agent(
        project,
        token_expires_at=expires_at,
        rate_limit_per_minute=3,
    )

    response = client.get(f"/agents/{agent_id}/credentials")

    assert response.status_code == 200
    credentials = response.json()
    assert credentials["credential_state"] == "ACTIVE"
    assert credentials["token_present"] is True
    assert credentials["token_expires_at"] is not None
    assert credentials["rate_limit_per_minute"] == 3


def test_successful_external_agent_auth_updates_last_used_timestamp() -> None:
    project = _project()
    agent_id, token = _register_agent(project)

    response = client.post(
        "/agents/tasks",
        json=_task_payload(project.id),
        headers=_headers(agent_id, token),
    )

    assert response.status_code == 200
    credentials = client.get(f"/agents/{agent_id}/credentials").json()
    assert credentials["last_used_at"] is not None
    assert store.external_agent_credentials[agent_id].last_used_at is not None


def test_invalid_external_agent_token_records_failed_auth_audit() -> None:
    project = _project()
    agent_id, _ = _register_agent(project)

    response = client.post(
        "/agents/tasks",
        json=_task_payload(project.id),
        headers=_headers(agent_id, "bad-token"),
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid external agent token"
    assert store.external_agent_credentials[agent_id].failed_auth_count == 1
    assert any(
        event.event_type == "agent.auth_failed" and event.details["reason"] == "invalid_token"
        for event in store.audit_events
    )


def test_external_agent_token_rotation_invalidates_old_token() -> None:
    project = _project()
    agent_id, old_token = _register_agent(project)
    expires_at = (now_utc() + timedelta(days=1)).isoformat()

    rotate_response = client.post(
        f"/agents/{agent_id}/token/rotate",
        json={"token_expires_at": expires_at, "rate_limit_per_minute": 5},
    )
    assert rotate_response.status_code == 200
    new_token = rotate_response.json()["agent_token"]
    assert new_token
    assert new_token != old_token

    old_token_response = client.post(
        "/agents/tasks",
        json=_task_payload(project.id),
        headers=_headers(agent_id, old_token),
    )
    new_token_response = client.post(
        "/agents/tasks",
        json=_task_payload(project.id),
        headers=_headers(agent_id, new_token),
    )
    credentials = client.get(f"/agents/{agent_id}/credentials").json()

    assert old_token_response.status_code == 401
    assert new_token_response.status_code == 200
    assert credentials["credential_state"] == "ACTIVE"
    assert credentials["token_expires_at"] is not None
    assert credentials["rate_limit_per_minute"] == 5
    assert any(event.event_type == "agent.token_rotated" for event in store.audit_events)


def test_external_agent_token_revoke_blocks_future_agent_auth() -> None:
    project = _project()
    agent_id, token = _register_agent(project)

    revoke_response = client.post(f"/agents/{agent_id}/token/revoke")
    blocked_response = client.post(
        "/agents/tasks",
        json=_task_payload(project.id),
        headers=_headers(agent_id, token),
    )
    credentials = client.get(f"/agents/{agent_id}/credentials").json()

    assert revoke_response.status_code == 200
    assert credentials["credential_state"] == "REVOKED"
    assert blocked_response.status_code == 401
    assert blocked_response.json()["detail"] == "External agent token is revoked or not configured"
    assert any(event.event_type == "agent.token_revoked" for event in store.audit_events)


def test_external_agent_token_expiry_is_enforced() -> None:
    project = _project()
    agent_id, old_token = _register_agent(project)
    expired_at = (now_utc() - timedelta(minutes=1)).isoformat()
    rotated = client.post(
        f"/agents/{agent_id}/token/rotate",
        json={"token_expires_at": expired_at},
    )
    assert rotated.status_code == 200
    token = rotated.json()["agent_token"]
    assert token != old_token

    response = client.post(
        "/agents/tasks",
        json=_task_payload(project.id),
        headers=_headers(agent_id, token),
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "External agent token expired"
    assert client.get(f"/agents/{agent_id}/credentials").json()["credential_state"] == "EXPIRED"
    assert any(
        event.event_type == "agent.auth_failed" and event.details["reason"] == "token_expired"
        for event in store.audit_events
    )


def test_external_agent_registration_expiry_is_enforced() -> None:
    project = _project()
    expired_at = (now_utc() - timedelta(minutes=1)).isoformat()
    agent_id, token = _register_agent(project, token_expires_at=expired_at)

    response = client.post(
        "/agents/tasks",
        json=_task_payload(project.id),
        headers=_headers(agent_id, token),
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "External agent token expired"
    assert client.get(f"/agents/{agent_id}/credentials").json()["credential_state"] == "EXPIRED"


def test_external_agent_rate_limit_blocks_excess_requests() -> None:
    project = _project()
    agent_id, _ = _register_agent(project)
    rotated = client.post(
        f"/agents/{agent_id}/token/rotate",
        json={"rate_limit_per_minute": 1},
    )
    token = rotated.json()["agent_token"]
    created = client.post(
        "/agents/tasks",
        json=_task_payload(project.id),
        headers=_headers(agent_id, token),
    )
    assert created.status_code == 200

    response = client.get(
        f"/agents/tasks/{created.json()['id']}",
        headers=_headers(agent_id, token),
    )

    assert response.status_code == 429
    assert response.json()["detail"] == "External agent rate limit exceeded"
    assert any(event.event_type == "agent.rate_limited" for event in store.audit_events)
