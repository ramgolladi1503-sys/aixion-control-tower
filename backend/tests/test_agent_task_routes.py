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


def test_create_agent_task_records_task_event_and_audit() -> None:
    response = client.post(
        "/agent/tasks",
        json={
            "provider": "CHATGPT",
            "title": "Prepare RTI app MVP scope",
            "goal": "Create product scope, backend plan, Android flow, risks, and demo checklist.",
            "requested_action": "GENERATE_DOCS",
            "requires_approval": True,
            "metadata": {"source": "test"},
        },
    )

    assert response.status_code == 200
    task = response.json()
    assert task["provider"] == "CHATGPT"
    assert task["status"] == "RECEIVED"
    assert task["requires_approval"] is True
    assert task["id"] in store.agent_tasks
    assert len(store.agent_task_events) == 1
    assert store.agent_task_events[next(iter(store.agent_task_events))].event_type == "TASK_CREATED"
    assert any(event.event_type == "agent_task.created" for event in store.audit_events)


def test_list_get_and_filter_agent_tasks() -> None:
    first = client.post(
        "/agent/tasks",
        json={"provider": "CHATGPT", "title": "Docs", "goal": "Generate docs", "requested_action": "GENERATE_DOCS"},
    ).json()
    client.post(
        "/agent/tasks",
        json={"provider": "CLAUDE", "title": "Review", "goal": "Review architecture", "requested_action": "REVIEW_ARCHITECTURE"},
    )

    by_provider = client.get("/agent/tasks?provider=CHATGPT")
    detail = client.get(f"/agent/tasks/{first['id']}")

    assert by_provider.status_code == 200
    assert len(by_provider.json()) == 1
    assert by_provider.json()[0]["provider"] == "CHATGPT"
    assert detail.status_code == 200
    assert detail.json()["id"] == first["id"]


def test_append_agent_task_event_updates_status_and_timeline() -> None:
    task = client.post(
        "/agent/tasks",
        json={"provider": "CODEX", "title": "Fix tests", "goal": "Fix failing tests", "requested_action": "CREATE_APPROVAL"},
    ).json()

    response = client.post(
        f"/agent/tasks/{task['id']}/events",
        json={"event_type": "TESTS_STARTED", "message": "Running backend tests", "status": "TESTING"},
    )
    events = client.get(f"/agent/tasks/{task['id']}/events")
    updated = client.get(f"/agent/tasks/{task['id']}")

    assert response.status_code == 200
    assert response.json()["event_type"] == "TESTS_STARTED"
    assert updated.json()["status"] == "TESTING"
    assert len(events.json()) == 2
    assert any(event.event_type == "agent_task.event_recorded" for event in store.audit_events)


def test_create_agent_task_rejects_missing_project() -> None:
    response = client.post(
        "/agent/tasks",
        json={"project_id": "project_missing", "title": "Bad", "goal": "Missing project"},
    )

    assert response.status_code == 404


def test_create_agent_task_accepts_existing_project() -> None:
    project = Project(name="Connected Agent Demo", description="demo")
    store.projects[project.id] = project
    store.persist()

    response = client.post(
        "/agent/tasks",
        json={"project_id": project.id, "provider": "CURSOR", "title": "UI polish", "goal": "Polish Agent Tasks screen"},
    )

    assert response.status_code == 200
    assert response.json()["project_id"] == project.id
