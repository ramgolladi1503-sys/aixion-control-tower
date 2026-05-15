from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from fastapi.testclient import TestClient

from app.main import app
from app.models import Project
from app.store import store

client = TestClient(app)


def setup_function() -> None:
    store.reset()


def _project() -> Project:
    project = Project(name="Connector Project", description="demo")
    store.projects[project.id] = project
    store.persist()
    return project


def _payload(project_id: str) -> dict:
    return {
        "name": "OpenClaw Local Bridge",
        "connector_type": "LOCAL_BRIDGE",
        "provider_label": "OPENCLAW",
        "endpoint_url": "http://localhost:8787",
        "callback_url": "https://example.com/connectors/callback",
        "auth_type": "HMAC",
        "allowed_project_ids": [project_id],
        "allowed_repositories": ["ramgolladi1503-sys/aixion-control-tower"],
        "allowed_actions": ["CREATE_AGENT_TASK", "APPEND_AGENT_TASK_EVENT", "READ_AGENT_TASK"],
        "rate_limit_per_minute": 30,
        "enabled": True,
        "config": {"profile": "openclaw", "mode": "approval-gated"},
    }


def test_create_connector_records_owner_visible_config_and_audit() -> None:
    project = _project()

    response = client.post("/connectors", json=_payload(project.id))

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "OpenClaw Local Bridge"
    assert body["connector_type"] == "LOCAL_BRIDGE"
    assert body["provider_label"] == "OPENCLAW"
    assert body["auth_type"] == "HMAC"
    assert body["status"] == "ENABLED"
    assert body["health_status"] == "UNKNOWN"
    assert body["allowed_project_ids"] == [project.id]
    assert body["allowed_actions"] == ["CREATE_AGENT_TASK", "APPEND_AGENT_TASK_EVENT", "READ_AGENT_TASK"]
    assert body["rate_limit_per_minute"] == 30
    assert body["secret_configured"] is False
    assert body["config"]["profile"] == "openclaw"
    assert body["id"] in store.agent_connectors
    assert any(event.event_type == "connector.created" for event in store.audit_events)


def test_list_and_get_connectors() -> None:
    project = _project()
    created = client.post("/connectors", json=_payload(project.id)).json()

    listed = client.get("/connectors")
    fetched = client.get(f"/connectors/{created['id']}")

    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert listed.json()[0]["id"] == created["id"]
    assert fetched.status_code == 200
    assert fetched.json()["id"] == created["id"]


def test_update_connector_changes_scopes_and_status() -> None:
    project = _project()
    created = client.post("/connectors", json=_payload(project.id)).json()

    response = client.patch(
        f"/connectors/{created['id']}",
        json={
            "name": "Antigravity Workspace Bridge",
            "provider_label": "ANTIGRAVITY",
            "enabled": False,
            "allowed_actions": ["CREATE_AGENT_TASK"],
            "rate_limit_per_minute": 10,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Antigravity Workspace Bridge"
    assert body["provider_label"] == "ANTIGRAVITY"
    assert body["status"] == "DISABLED"
    assert body["health_status"] == "DISABLED"
    assert body["allowed_actions"] == ["CREATE_AGENT_TASK"]
    assert body["rate_limit_per_minute"] == 10
    assert any(event.event_type == "connector.updated" for event in store.audit_events)


def test_disable_and_enable_connector() -> None:
    project = _project()
    created = client.post("/connectors", json=_payload(project.id)).json()

    disabled = client.post(f"/connectors/{created['id']}/disable")
    enabled = client.post(f"/connectors/{created['id']}/enable")

    assert disabled.status_code == 200
    assert disabled.json()["status"] == "DISABLED"
    assert disabled.json()["health_status"] == "DISABLED"
    assert enabled.status_code == 200
    assert enabled.json()["status"] == "ENABLED"
    assert enabled.json()["health_status"] == "UNKNOWN"
    assert any(event.event_type == "connector.disabled" for event in store.audit_events)
    assert any(event.event_type == "connector.enabled" for event in store.audit_events)


def test_connector_rejects_missing_project_scope() -> None:
    payload = _payload("project_missing")

    response = client.post("/connectors", json=payload)

    assert response.status_code == 404
    assert response.json()["detail"]["message"] == "Project scope not found"


def test_connector_rejects_non_https_non_local_endpoint() -> None:
    project = _project()
    payload = _payload(project.id)
    payload["endpoint_url"] = "http://example.com/not-local"

    response = client.post("/connectors", json=payload)

    assert response.status_code == 422


def test_unknown_connector_returns_404() -> None:
    response = client.get("/connectors/connector_missing")

    assert response.status_code == 404
    assert response.json()["detail"] == "Connector not found"
