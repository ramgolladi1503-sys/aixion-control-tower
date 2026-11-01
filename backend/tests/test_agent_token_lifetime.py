from __future__ import annotations

import os
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from app.main import app
from app.agent_credential_models import AgentCredentialRecord
from app.agent_token_lifetime import effective_agent_token_expiry
from app.models import Project, now_utc
from app.settings import get_settings
from app.store import store


client = TestClient(app)


def setup_function() -> None:
    store.reset()


def _project() -> Project:
    project = Project(name="Token lifetime", description="credential lifetime test")
    store.projects[project.id] = project
    return project


def _register_agent(project: Project, *, expiry: str | None = None) -> tuple[str, str]:
    payload = {
        "provider": "ANTIGRAVITY",
        "display_name": "Antigravity local connector",
        "auth_type": "API_KEY",
        "allowed_project_ids": [project.id],
        "allowed_repositories": ["ramgolladi1503-sys/aixion-control-tower"],
        "allowed_actions": ["CREATE_APPROVAL", "READ_APPROVAL_DECISION"],
        "enabled": True,
    }
    if expiry is not None:
        payload["token_expires_at"] = expiry
    response = client.post("/agents", json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    return body["agent"]["id"], body["agent_token"]


def _auth_headers(agent_id: str, token: str) -> dict[str, str]:
    return {
        "X-Aixion-Agent-Id": agent_id,
        "X-Aixion-Agent-Token": token,
        "X-Aixion-Approval-Protocol": "1",
    }


def test_new_connector_tokens_receive_configured_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "false")
    monkeypatch.setenv("AIXION_EXTERNAL_AGENT_TOKEN_TTL_SECONDS", "900")
    project = _project()

    agent_id, _ = _register_agent(project)

    credentials = client.get(f"/agents/{agent_id}/credentials")
    assert credentials.status_code == 200
    body = credentials.json()
    assert body["credential_state"] == "ACTIVE"
    expiry = now_utc().fromisoformat(body["token_expires_at"])
    assert now_utc() < expiry <= now_utc() + timedelta(seconds=900)


def test_rotation_without_expiry_uses_configured_lifetime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "false")
    monkeypatch.setenv("AIXION_EXTERNAL_AGENT_TOKEN_TTL_SECONDS", "1200")
    project = _project()
    agent_id, _ = _register_agent(project)

    rotated = client.post(f"/agents/{agent_id}/token/rotate")

    assert rotated.status_code == 200, rotated.text
    credentials = client.get(f"/agents/{agent_id}/credentials").json()
    expiry = now_utc().fromisoformat(credentials["token_expires_at"])
    assert now_utc() < expiry <= now_utc() + timedelta(seconds=1200)


@pytest.mark.parametrize(
    "expiry",
    [
        (now_utc() - timedelta(seconds=1)).isoformat(),
        (now_utc() + timedelta(days=31)).isoformat(),
        (now_utc() + timedelta(minutes=5)).replace(tzinfo=None).isoformat(),
    ],
)
def test_registration_rejects_expired_overlong_or_timezone_free_expiry(
    monkeypatch: pytest.MonkeyPatch,
    expiry: str,
) -> None:
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "false")
    monkeypatch.setenv("AIXION_EXTERNAL_AGENT_TOKEN_TTL_SECONDS", "2592000")
    project = _project()

    response = client.post(
        "/agents",
        json={
            "provider": "ANTIGRAVITY",
            "display_name": "Invalid lifetime connector",
            "auth_type": "API_KEY",
            "allowed_project_ids": [project.id],
            "allowed_repositories": ["ramgolladi1503-sys/aixion-control-tower"],
            "allowed_actions": ["CREATE_APPROVAL"],
            "token_expires_at": expiry,
        },
    )

    assert response.status_code == 422
    assert store.external_agents == {}


def test_legacy_token_without_expiry_ages_from_creation_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "false")
    monkeypatch.setenv("AIXION_EXTERNAL_AGENT_TOKEN_TTL_SECONDS", "300")
    project = _project()
    agent_id, token = _register_agent(project)
    credential = store.external_agent_credentials[agent_id]
    credential.token_expires_at = None
    credential.token_rotated_at = None
    credential.created_at = now_utc() - timedelta(seconds=301)
    store.persist()

    response = client.get(
        f"/agents/identity?provider=ANTIGRAVITY&project_id={project.id}",
        headers=_auth_headers(agent_id, token),
    )
    credentials = client.get(f"/agents/{agent_id}/credentials").json()

    assert response.status_code == 401
    assert response.json()["detail"] == "External agent token expired"
    assert credentials["credential_state"] == "EXPIRED"
    assert credentials["token_expires_at"] is not None


def test_legacy_explicit_expiry_cannot_outlive_the_configured_maximum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIXION_EXTERNAL_AGENT_TOKEN_TTL_SECONDS", "300")
    issued_at = now_utc() - timedelta(seconds=301)
    credential = AgentCredentialRecord(
        agent_id="legacy-agent",
        token_expires_at=now_utc() + timedelta(days=365),
        created_at=issued_at,
    )

    assert effective_agent_token_expiry(credential) <= now_utc()


@pytest.mark.parametrize("value", ["299", "2592001", "invalid"])
def test_token_lifetime_setting_rejects_out_of_range_values(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("AIXION_EXTERNAL_AGENT_TOKEN_TTL_SECONDS", value)

    with pytest.raises(ValueError, match="AIXION_EXTERNAL_AGENT_TOKEN_TTL_SECONDS"):
        get_settings()
