from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
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
    provider: str = "CHATGPT",
    token_expires_at: str | None = None,
    rate_limit_per_minute: int = 60,
    allowed_actions: list[str] | None = None,
) -> tuple[str, str]:
    payload = {
        "provider": provider,
        "display_name": "Governed GPT",
        "auth_type": "API_KEY",
        "allowed_project_ids": [project.id],
        "allowed_repositories": [REPOSITORY],
        "allowed_actions": allowed_actions or ["CREATE_AGENT_TASK", "READ_AGENT_TASK"],
        "enabled": True,
        "rate_limit_per_minute": rate_limit_per_minute,
    }
    if token_expires_at is not None:
        payload["token_expires_at"] = token_expires_at
    response = client.post("/agents", json=payload)
    assert response.status_code == 200
    body = response.json()
    return body["agent"]["id"], body["agent_token"]


APPROVAL_CONNECTOR_ACTIONS = [
    "CREATE_APPROVAL",
    "READ_APPROVAL_DECISION",
    "CONSUME_APPROVAL_DECISION",
    "REPORT_APPROVAL_DELIVERY_FAILURE",
    "EXPIRE_OWN_APPROVAL",
]


def test_agent_connector_identity_preflight_checks_token_provider_scope_and_capabilities() -> None:
    project = _project()
    agent_id, token = _register_agent(
        project,
        provider="ANTIGRAVITY",
        allowed_actions=APPROVAL_CONNECTOR_ACTIONS,
    )
    headers = {
        **_headers(agent_id, token),
        "X-Aixion-Approval-Protocol": "1",
    }
    path = f"/agents/identity?provider=ANTIGRAVITY&project_id={project.id}"

    response = client.get(path, headers=headers)

    assert response.status_code == 200
    assert response.json() == {
        "agent_id": agent_id,
        "provider": "ANTIGRAVITY",
        "workspace_id": project.workspace_id,
        "project_id": project.id,
    }
    assert "agent_token" not in response.json()
    invalid_token = client.get(
        path,
        headers={**headers, "X-Aixion-Agent-Token": "invalid-token"},
    )
    assert invalid_token.status_code == 401


def test_agent_connector_identity_preflight_rejects_wrong_provider_project_and_capability() -> None:
    project = _project()
    other_project = _project()
    agent_id, token = _register_agent(
        project,
        provider="CODEX",
        allowed_actions=APPROVAL_CONNECTOR_ACTIONS,
    )
    headers = {**_headers(agent_id, token), "X-Aixion-Approval-Protocol": "1"}

    wrong_provider = client.get(
        f"/agents/identity?provider=ANTIGRAVITY&project_id={project.id}",
        headers=headers,
    )
    wrong_project = client.get(
        f"/agents/identity?provider=CODEX&project_id={other_project.id}",
        headers=headers,
    )
    agent_without_expire = _register_agent(
        project,
        provider="CODEX",
        allowed_actions=APPROVAL_CONNECTOR_ACTIONS[:-1],
    )
    missing_capability = client.get(
        f"/agents/identity?provider=CODEX&project_id={project.id}",
        headers={
            **_headers(*agent_without_expire),
            "X-Aixion-Approval-Protocol": "1",
        },
    )

    assert wrong_provider.status_code == 403
    assert wrong_project.status_code == 403
    assert missing_capability.status_code == 403


def test_agent_approval_api_rejects_unknown_protocol_and_accepts_legacy_v1(monkeypatch) -> None:
    project = _project()
    agent_id, token = _register_agent(
        project,
        allowed_actions=["CREATE_APPROVAL"],
    )
    approval_payload = {
        "project_id": project.id,
        "title": "Protocol version check",
        "summary": "Verify approval protocol negotiation is fail closed.",
        "agent_name": "Antigravity test",
        "target_branch": "feature/protocol-check",
        "files": [{"path": "README.md", "change_type": "update", "diff": "+checked"}],
        "test_plan": ["Run protocol tests"],
        "rollback_plan": "Deny the request if the protocol is unsupported.",
        "source_session_id": "session-protocol-check",
        "source_invocation_id": "invocation-protocol-check",
    }

    unsupported = client.post(
        "/agents/approvals",
        json=approval_payload,
        headers={
            **_headers(agent_id, token),
            "X-Aixion-Approval-Protocol": "2",
        },
    )

    assert unsupported.status_code == 426
    assert unsupported.headers["X-Aixion-Supported-Approval-Protocols"] == "1"
    assert unsupported.json()["detail"] == "Unsupported approval protocol version"
    assert store.approval_requests == {}

    # Project authorization must resolve from the durable repository even if
    # another API process has no project in its compatibility cache.
    store.projects.clear()
    project_repository = app.state.project_repository
    membership_repository = app.state.workspace_membership_repository
    lookups = {"project": 0, "membership": 0}

    class TrackingProjectRepository:
        def get(self, project_id: str):
            lookups["project"] += 1
            return project_repository.get(project_id)

    class TrackingMembershipRepository:
        def get_workspace(self, workspace_id: str):
            lookups["membership"] += 1
            return membership_repository.get_workspace(workspace_id)

    monkeypatch.setattr(app.state, "project_repository", TrackingProjectRepository())
    monkeypatch.setattr(
        app.state,
        "workspace_membership_repository",
        TrackingMembershipRepository(),
    )
    legacy = client.post(
        "/agents/approvals",
        json={**approval_payload, "source_invocation_id": "legacy-invocation"},
        headers=_headers(agent_id, token),
    )
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["source_invocation_id"] == "legacy-invocation"
    assert lookups["project"] > 0


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


def test_agent_auth_rejects_rotated_token_when_local_cache_is_stale() -> None:
    project = _project()
    agent_id, old_token = _register_agent(project)
    stale_agent = store.external_agents[agent_id].model_copy(deep=True)
    stale_credential = store.external_agent_credentials[agent_id].model_copy(deep=True)

    rotated = client.post(f"/agents/{agent_id}/token/rotate")
    assert rotated.status_code == 200
    new_token = rotated.json()["agent_token"]

    # Simulate another API process retaining pre-rotation objects in memory.
    store.external_agents[agent_id] = stale_agent
    store.external_agent_credentials[agent_id] = stale_credential
    response = client.post(
        "/agents/tasks",
        json=_task_payload(project.id),
        headers=_headers(agent_id, old_token),
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid external agent token"
    assert store.external_agents[agent_id].secret_hash != stale_agent.secret_hash
    assert store.external_agents[agent_id].secret_hash is not None
    assert new_token != old_token


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
    assert 1 <= int(response.headers["Retry-After"]) <= 60
    assert any(event.event_type == "agent.rate_limited" for event in store.audit_events)


def test_external_agent_rate_limit_survives_store_reload() -> None:
    project = _project()
    agent_id, token = _register_agent(project, rate_limit_per_minute=1)
    headers = _headers(agent_id, token)
    first = client.post("/agents/tasks", json=_task_payload(project.id), headers=headers)
    assert first.status_code == 200

    # A process restart reloads the credential from the SQLite database.
    store.load()
    second = client.post("/agents/tasks", json=_task_payload(project.id), headers=headers)

    assert second.status_code == 429
    assert second.headers["Retry-After"]
    assert store.external_agent_credentials[agent_id].rate_limit_request_count == 1


def test_concurrent_agent_requests_cannot_exceed_durable_rate_limit() -> None:
    project = _project()
    agent_id, token = _register_agent(project, rate_limit_per_minute=1)
    headers = _headers(agent_id, token)

    def submit_task():
        return client.post(
            "/agents/tasks",
            json=_task_payload(project.id),
            headers=headers,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: submit_task(), range(2)))

    assert sorted(response.status_code for response in responses) == [200, 429]
    assert sum(
        event.event_type == "agent.rate_limited" and event.entity_id == agent_id
        for event in store.audit_events
    ) == 1
