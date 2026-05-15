from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from fastapi.testclient import TestClient

from app.connector_credentials import verify_connector_secret
from app.main import app
from app.models import Project
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


def test_connector_health_success_updates_usage_and_health() -> None:
    project = _project()
    connector = _connector(project.id)

    response = client.post(f"/connectors/{connector['id']}/health/success")

    assert response.status_code == 200
    body = response.json()
    assert body["health_status"] == "HEALTHY"
    assert body["last_used_at"] is not None
    assert body["last_health_check_at"] is not None
    assert body["last_error"] in {None, ""}
    assert any(event.event_type == "connector.health_success" for event in store.audit_events)


def test_connector_health_failure_increments_failures_and_records_reason() -> None:
    project = _project()
    connector = _connector(project.id)

    response = client.post(f"/connectors/{connector['id']}/health/failure", json={"note": "bad signature"})

    assert response.status_code == 200
    body = response.json()
    assert body["health_status"] == "DEGRADED"
    assert body["failed_auth_count"] == 1
    assert body["last_error"] == "bad signature"
    assert body["last_health_check_at"] is not None
    assert any(event.event_type == "connector.health_failure" for event in store.audit_events)


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
