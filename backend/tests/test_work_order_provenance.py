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


def _project(name: str = "WorkOrder Provenance") -> Project:
    project = Project(name=name, description="demo")
    store.projects[project.id] = project
    store.persist()
    return project


def _work_order_payload(project_id: str) -> dict:
    return {
        "project_id": project_id,
        "goal": "Create a traced work order.",
        "context": "Work order provenance must be visible and audit-backed.",
        "tasks": ["Add provenance fields", "Expose source metadata"],
        "affected_areas": ["backend", "android"],
        "required_tests": ["python -m pytest tests/test_work_order_provenance.py"],
        "rollback_plan": "Revert the provenance branch.",
    }


def _register_agent(project: Project, *, actions: list[str] | None = None) -> tuple[str, str]:
    response = client.post(
        "/agents",
        json={
            "provider": "CHATGPT",
            "display_name": "Scoped GPT",
            "auth_type": "API_KEY",
            "allowed_project_ids": [project.id],
            "allowed_repositories": ["ramgolladi1503-sys/aixion-control-tower"],
            "allowed_actions": actions if actions is not None else ["CREATE_WORK_ORDER"],
            "enabled": True,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    return payload["agent"]["id"], payload["agent_token"]


def _headers(agent_id: str, token: str) -> dict[str, str]:
    return {"X-Aixion-Agent-Id": agent_id, "X-Aixion-Agent-Token": token}


def test_manual_work_order_stores_created_by_user_and_manual_source() -> None:
    project = _project()

    response = client.post("/work-orders", json=_work_order_payload(project.id))

    assert response.status_code == 200
    work_order = response.json()
    assert work_order["source_type"] == "MANUAL"
    assert work_order["source_provider"] == "MANUAL"
    assert work_order["source_agent_id"] is None
    assert work_order["source_agent_name"] is None
    assert work_order["created_by_user_id"] == "dev_user"
    assert work_order["verified_source"] is False
    audit_event = next(event for event in store.audit_events if event.event_type == "work_order.created")
    assert audit_event.details["created_by_user_id"] == "dev_user"
    assert audit_event.details["source_type"] == "MANUAL"


def test_work_order_list_exposes_source_metadata() -> None:
    project = _project()
    created = client.post("/work-orders", json=_work_order_payload(project.id)).json()

    response = client.get("/work-orders")

    assert response.status_code == 200
    work_order = next(item for item in response.json() if item["id"] == created["id"])
    assert work_order["source_type"] == "MANUAL"
    assert work_order["source_provider"] == "MANUAL"
    assert work_order["created_by_user_id"] == "dev_user"
    assert work_order["verified_source"] is False


def test_agent_created_work_order_derives_source_from_scoped_agent() -> None:
    project = _project()
    agent_id, token = _register_agent(project)
    payload = {
        **_work_order_payload(project.id),
        "source_task_id": "chatgpt-task-123",
        "source_session_id": "chatgpt-session-456",
    }

    response = client.post("/agents/work-orders", json=payload, headers=_headers(agent_id, token))

    assert response.status_code == 200
    work_order = response.json()
    assert work_order["source_type"] == "AGENT_TASK"
    assert work_order["source_provider"] == "CHATGPT"
    assert work_order["source_agent_id"] == agent_id
    assert work_order["source_agent_name"] == "Scoped GPT"
    assert work_order["source_task_id"] == "chatgpt-task-123"
    assert work_order["source_session_id"] == "chatgpt-session-456"
    assert work_order["created_by_user_id"] is None
    assert work_order["verified_source"] is True
    audit_event = next(event for event in store.audit_events if event.event_type == "work_order.created_by_agent")
    assert audit_event.actor == f"agent:{agent_id}"
    assert audit_event.details["source_agent_id"] == agent_id
    assert audit_event.details["verified_source"] is True


def test_agent_created_work_order_ignores_client_supplied_agent_identity() -> None:
    project = _project()
    agent_id, token = _register_agent(project)
    payload = {
        **_work_order_payload(project.id),
        "source_provider": "CLAUDE",
        "source_agent_id": "agent_fake",
        "source_agent_name": "Fake Agent",
        "source_type": "MANUAL",
    }

    response = client.post("/agents/work-orders", json=payload, headers=_headers(agent_id, token))

    assert response.status_code == 200
    work_order = response.json()
    assert work_order["source_type"] == "AGENT_TASK"
    assert work_order["source_provider"] == "CHATGPT"
    assert work_order["source_agent_id"] == agent_id
    assert work_order["source_agent_name"] == "Scoped GPT"
    assert work_order["verified_source"] is True


def test_agent_cannot_create_work_order_for_unauthorized_project() -> None:
    allowed_project = _project("Allowed")
    blocked_project = _project("Blocked")
    agent_id, token = _register_agent(allowed_project)

    response = client.post(
        "/agents/work-orders",
        json=_work_order_payload(blocked_project.id),
        headers=_headers(agent_id, token),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Agent is not allowed for this project"


def test_agent_requires_create_work_order_permission() -> None:
    project = _project()
    agent_id, token = _register_agent(project, actions=["CREATE_AGENT_TASK"])

    response = client.post("/agents/work-orders", json=_work_order_payload(project.id), headers=_headers(agent_id, token))

    assert response.status_code == 403
    assert response.json()["detail"] == "Agent is not allowed to perform CREATE_WORK_ORDER"
