from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from fastapi.testclient import TestClient

from app.agent_task_models import AgentTask, AgentTaskStatus
from app.main import app
from app.models import AgentProvider, Project
from app.store import store

client = TestClient(app)
REPOSITORY = "ramgolladi1503-sys/aixion-control-tower"


def setup_function() -> None:
    store.reset()


def _project() -> Project:
    project = Project(name="Connector Simulator", description="demo")
    store.projects[project.id] = project
    store.persist()
    return project


def _connector(project_id: str) -> dict:
    response = client.post(
        "/connectors",
        json={
            "name": "OpenClaw Simulator",
            "connector_type": "WEBHOOK",
            "provider_label": "OPENCLAW",
            "endpoint_url": "https://example.com/openclaw",
            "auth_type": "BEARER",
            "allowed_project_ids": [project_id],
            "allowed_repositories": [REPOSITORY],
            "allowed_actions": ["CREATE_AGENT_TASK", "APPEND_AGENT_TASK_EVENT", "READ_AGENT_TASK"],
            "rate_limit_per_minute": 60,
            "enabled": True,
        },
    )
    assert response.status_code == 200
    secret_response = client.post(f"/connectors/{response.json()['id']}/secret/issue")
    assert secret_response.status_code == 200
    return response.json()


def test_simulator_accepts_create_task_preview_without_creating_task() -> None:
    project = _project()
    connector = _connector(project.id)

    response = client.post(
        f"/connectors/{connector['id']}/simulate",
        json={
            "sample_payload": {
                "action": "CREATE_AGENT_TASK",
                "project_id": project.id,
                "title": "Preview task",
                "goal": "Validate a connector payload safely.",
                "repository": REPOSITORY,
                "metadata": {"source": "simulator"},
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["auth_ready"] is True
    assert body["scope_ready"] is True
    assert body["action"] == "CREATE_AGENT_TASK"
    assert body["task_preview"]["title"] == "Preview task"
    assert body["task_preview"]["metadata"]["connector_webhook"] is True
    assert body["errors"] == []
    assert store.agent_tasks == {}
    assert store.agent_task_events == {}


def test_simulator_rejects_scope_without_mutating_connector_health() -> None:
    project = _project()
    other_project = Project(name="Other", description="not allowed")
    store.projects[other_project.id] = other_project
    store.persist()
    connector = _connector(project.id)

    response = client.post(
        f"/connectors/{connector['id']}/simulate",
        json={
            "sample_payload": {
                "action": "CREATE_AGENT_TASK",
                "project_id": other_project.id,
                "title": "Wrong scope",
                "goal": "Should not pass scope.",
                "repository": REPOSITORY,
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is False
    assert "Connector is not allowed for this project." in body["errors"]
    assert store.agent_connectors[connector["id"]].failed_auth_count == 0
    assert store.agent_tasks == {}


def test_simulator_accepts_append_event_preview_for_owned_task_without_adding_event() -> None:
    project = _project()
    connector = _connector(project.id)
    task = AgentTask(
        provider=AgentProvider.OTHER,
        project_id=project.id,
        title="Owned task",
        goal="Owned by connector",
        repository=REPOSITORY,
        status=AgentTaskStatus.PLANNING,
        external_agent_id=connector["id"],
        external_agent_name=connector["name"],
        metadata={"connector_id": connector["id"]},
    )
    store.agent_tasks[task.id] = task
    store.persist()

    response = client.post(
        f"/connectors/{connector['id']}/simulate",
        json={
            "sample_payload": {
                "action": "APPEND_AGENT_TASK_EVENT",
                "task_id": task.id,
                "event_type": "PLAN_RECEIVED",
                "message": "Plan ready",
                "status": "PLANNING",
                "metadata": {"artifact": "plan"},
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["action"] == "APPEND_AGENT_TASK_EVENT"
    assert body["event_preview"]["task_id"] == task.id
    assert body["event_preview"]["message"] == "Plan ready"
    assert body["event_preview"]["metadata"]["artifact"] == "plan"
    assert store.agent_task_events == {}
    assert store.agent_tasks[task.id].status == AgentTaskStatus.PLANNING


def test_simulator_warns_when_secret_is_missing() -> None:
    project = _project()
    response = client.post(
        "/connectors",
        json={
            "name": "No Secret Simulator",
            "connector_type": "WEBHOOK",
            "provider_label": "CUSTOM",
            "endpoint_url": "https://example.com/custom",
            "auth_type": "BEARER",
            "allowed_project_ids": [project.id],
            "allowed_repositories": [REPOSITORY],
            "allowed_actions": ["CREATE_AGENT_TASK"],
            "enabled": True,
        },
    )
    connector = response.json()

    simulation = client.post(
        f"/connectors/{connector['id']}/simulate",
        json={
            "sample_payload": {
                "action": "CREATE_AGENT_TASK",
                "project_id": project.id,
                "title": "Missing secret",
                "goal": "Warn only.",
                "repository": REPOSITORY,
            }
        },
    )

    assert simulation.status_code == 200
    body = simulation.json()
    assert body["accepted"] is False
    assert body["auth_ready"] is False
    assert "Connector secret is not configured; live webhook calls will be refused." in body["warnings"]
    assert body["task_preview"]["title"] == "Missing secret"
