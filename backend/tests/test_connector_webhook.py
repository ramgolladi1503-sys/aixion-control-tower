from __future__ import annotations

import hashlib
import hmac
import json
import os
import time

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from fastapi.testclient import TestClient

from app.main import app
from app.models import Project
from app.store import store

client = TestClient(app)
REPOSITORY = "ramgolladi1503-sys/aixion-control-tower"


def setup_function() -> None:
    store.reset()


def _project() -> Project:
    project = Project(name="Connector Webhook", description="demo")
    store.projects[project.id] = project
    store.persist()
    return project


def _connector(project_id: str, *, auth_type: str = "BEARER", rate_limit: int = 60, actions: list[str] | None = None) -> tuple[dict, str]:
    response = client.post(
        "/connectors",
        json={
            "name": "OpenClaw Bridge",
            "connector_type": "WEBHOOK",
            "provider_label": "OPENCLAW",
            "endpoint_url": "https://example.com/openclaw",
            "auth_type": auth_type,
            "allowed_project_ids": [project_id],
            "allowed_repositories": [REPOSITORY],
            "allowed_actions": actions or ["CREATE_AGENT_TASK", "APPEND_AGENT_TASK_EVENT", "READ_AGENT_TASK"],
            "rate_limit_per_minute": rate_limit,
            "enabled": True,
            "config": {"profile": "openclaw"},
        },
    )
    assert response.status_code == 200
    connector = response.json()
    secret_response = client.post(f"/connectors/{connector['id']}/secret/issue")
    assert secret_response.status_code == 200
    return connector, secret_response.json()["secret"]


def _task_payload(project_id: str) -> dict:
    return {
        "action": "CREATE_AGENT_TASK",
        "project_id": project_id,
        "title": "Fix Android approval flow",
        "goal": "Create a scoped task from a connector webhook.",
        "context": "Submitted by OpenClaw bridge.",
        "requested_action": "GENERATE_DOCS",
        "repository": REPOSITORY,
        "branch_preference": "feature/webhook-task",
        "requires_approval": True,
        "metadata": {"source": "openclaw"},
    }


def _bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def _compact_json(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"))


def _hmac_v1_headers(secret: str, body: str, *, nonce: str = "nonce_1", timestamp: int | None = None) -> dict[str, str]:
    timestamp_value = str(timestamp or int(time.time()))
    secret_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    signing_payload = f"v1.{timestamp_value}.{nonce}.{body_hash}"
    signature = hmac.new(secret_hash.encode("utf-8"), signing_payload.encode("utf-8"), "sha256").hexdigest()
    return {
        "X-Aixion-Signature-Version": "v1",
        "X-Aixion-Timestamp": timestamp_value,
        "X-Aixion-Nonce": nonce,
        "X-Aixion-Connector-Signature": f"v1={signature}",
    }


def test_bearer_connector_webhook_creates_agent_task() -> None:
    project = _project()
    connector, secret = _connector(project.id)

    response = client.post(f"/connectors/{connector['id']}/webhook", json=_task_payload(project.id), headers=_bearer(secret))

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["action"] == "CREATE_AGENT_TASK"
    assert body["task_id"] in store.agent_tasks
    task = store.agent_tasks[body["task_id"]]
    assert task.external_agent_id == connector["id"]
    assert task.external_agent_name == "OpenClaw Bridge"
    assert task.metadata["connector_webhook"] is True
    assert task.metadata["source"] == "openclaw"
    assert task.repository == REPOSITORY
    assert any(event.event_type == "connector.webhook_task_created" for event in store.audit_events)
    assert store.agent_connectors[connector["id"]].last_used_at is not None


def test_hmac_connector_webhook_creates_agent_task_with_v1_signature() -> None:
    project = _project()
    connector, secret = _connector(project.id, auth_type="HMAC")
    payload = _task_payload(project.id)
    body = _compact_json(payload)

    response = client.post(
        f"/connectors/{connector['id']}/webhook",
        content=body,
        headers={**_hmac_v1_headers(secret, body), "Content-Type": "application/json"},
    )

    assert response.status_code == 200
    assert response.json()["task_id"] in store.agent_tasks


def test_hmac_connector_webhook_rejects_replayed_nonce() -> None:
    project = _project()
    connector, secret = _connector(project.id, auth_type="HMAC")
    payload = _task_payload(project.id)
    body = _compact_json(payload)
    headers = {**_hmac_v1_headers(secret, body, nonce="replay_nonce"), "Content-Type": "application/json"}

    first = client.post(f"/connectors/{connector['id']}/webhook", content=body, headers=headers)
    second = client.post(f"/connectors/{connector['id']}/webhook", content=body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 401
    assert second.json()["detail"] == "Connector HMAC nonce has already been used"


def test_hmac_connector_webhook_rejects_stale_timestamp() -> None:
    project = _project()
    connector, secret = _connector(project.id, auth_type="HMAC")
    payload = _task_payload(project.id)
    body = _compact_json(payload)
    stale_timestamp = int(time.time()) - 1_000

    response = client.post(
        f"/connectors/{connector['id']}/webhook",
        content=body,
        headers={**_hmac_v1_headers(secret, body, nonce="stale_nonce", timestamp=stale_timestamp), "Content-Type": "application/json"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Connector HMAC timestamp is stale"


def test_hmac_connector_webhook_rejects_missing_signature_version() -> None:
    project = _project()
    connector, secret = _connector(project.id, auth_type="HMAC")
    payload = _task_payload(project.id)
    body = _compact_json(payload)
    headers = _hmac_v1_headers(secret, body, nonce="missing_version")
    headers.pop("X-Aixion-Signature-Version")

    response = client.post(
        f"/connectors/{connector['id']}/webhook",
        content=body,
        headers={**headers, "Content-Type": "application/json"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Unsupported connector HMAC signature version"


def test_connector_webhook_appends_event_to_owned_task() -> None:
    project = _project()
    connector, secret = _connector(project.id)
    created = client.post(f"/connectors/{connector['id']}/webhook", json=_task_payload(project.id), headers=_bearer(secret)).json()

    response = client.post(
        f"/connectors/{connector['id']}/webhook",
        json={
            "action": "APPEND_AGENT_TASK_EVENT",
            "task_id": created["task_id"],
            "event_type": "PLAN_RECEIVED",
            "message": "Connector produced a plan.",
            "status": "PLANNING",
            "metadata": {"artifact": "plan"},
        },
        headers=_bearer(secret),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["event_id"] in store.agent_task_events
    assert store.agent_tasks[created["task_id"]].status == "PLANNING"
    event = store.agent_task_events[body["event_id"]]
    assert event.message == "Connector produced a plan."
    assert event.metadata["connector_webhook"] is True
    assert any(event.event_type == "connector.webhook_task_event_appended" for event in store.audit_events)


def test_connector_webhook_rejects_missing_secret() -> None:
    project = _project()
    connector = client.post(
        "/connectors",
        json={
            "name": "No Secret",
            "connector_type": "WEBHOOK",
            "provider_label": "CUSTOM",
            "endpoint_url": "https://example.com/no-secret",
            "auth_type": "BEARER",
            "allowed_project_ids": [project.id],
            "allowed_repositories": [REPOSITORY],
            "allowed_actions": ["CREATE_AGENT_TASK"],
        },
    ).json()

    response = client.post(f"/connectors/{connector['id']}/webhook", json=_task_payload(project.id), headers=_bearer("bad"))

    assert response.status_code == 401
    assert response.json()["detail"] == "Connector secret is not configured"
    assert store.agent_connectors[connector["id"]].failed_auth_count == 1


def test_connector_webhook_rejects_wrong_project_scope() -> None:
    allowed_project = _project()
    other_project = Project(name="Other", description="not allowed")
    store.projects[other_project.id] = other_project
    store.persist()
    connector, secret = _connector(allowed_project.id)
    payload = _task_payload(other_project.id)

    response = client.post(f"/connectors/{connector['id']}/webhook", json=payload, headers=_bearer(secret))

    assert response.status_code == 403
    assert response.json()["detail"] == "Connector is not allowed for this project"
    assert any(event.event_type == "connector.webhook_refused" for event in store.audit_events)


def test_connector_webhook_rejects_unauthorized_action() -> None:
    project = _project()
    connector, secret = _connector(project.id, actions=["READ_AGENT_TASK"])

    response = client.post(f"/connectors/{connector['id']}/webhook", json=_task_payload(project.id), headers=_bearer(secret))

    assert response.status_code == 403
    assert response.json()["detail"] == "Connector is not allowed to perform CREATE_AGENT_TASK"


def test_connector_webhook_rate_limit_blocks_excess_requests() -> None:
    project = _project()
    connector, secret = _connector(project.id, rate_limit=1)
    first = client.post(f"/connectors/{connector['id']}/webhook", json=_task_payload(project.id), headers=_bearer(secret))
    assert first.status_code == 200

    second = client.post(f"/connectors/{connector['id']}/webhook", json=_task_payload(project.id), headers=_bearer(secret))

    assert second.status_code == 429
    assert second.json()["detail"] == "Connector webhook rate limit exceeded"
    assert any(event.event_type == "connector.webhook_rate_limited" for event in store.audit_events)
