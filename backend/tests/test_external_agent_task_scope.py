from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from fastapi.testclient import TestClient

from app.agent_task_models import AgentTaskStatus
from app.main import app
from app.models import AgentAction, AgentAuthType, AgentProvider, ExternalAgent, Project
from app.store import store

client = TestClient(app)


def setup_function() -> None:
    store.reset()


def _project() -> Project:
    project = Project(name="Scoped Agent", description="demo")
    store.projects[project.id] = project
    return project


def _register_agent(project: Project, *, actions: list[str] | None = None, repositories: list[str] | None = None) -> tuple[str, str]:
    response = client.post(
        "/agents",
        json={
            "provider": "CHATGPT",
            "display_name": "Scoped GPT",
            "auth_type": "API_KEY",
            "allowed_project_ids": [project.id],
            "allowed_repositories": repositories if repositories is not None else ["ramgolladi1503-sys/aixion-control-tower"],
            "allowed_actions": actions
            if actions is not None
            else ["CREATE_AGENT_TASK", "APPEND_AGENT_TASK_EVENT", "READ_AGENT_TASK"],
            "enabled": True,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    return payload["agent"]["id"], payload["agent_token"]


def _headers(agent_id: str, token: str) -> dict[str, str]:
    return {"X-Aixion-Agent-Id": agent_id, "X-Aixion-Agent-Token": token}


def _task_payload(project_id: str, repository: str = "ramgolladi1503-sys/aixion-control-tower") -> dict:
    return {
        "provider": "MANUAL",
        "project_id": project_id,
        "title": "Create scoped task",
        "goal": "Submit work through scoped external agent credentials.",
        "context": "External agent should not need an owner session.",
        "requested_action": "GENERATE_DOCS",
        "repository": repository,
        "branch_preference": "feature/scoped-agent-task",
        "requires_approval": True,
        "metadata": {"source": "test"},
    }


def test_external_agent_can_create_owned_agent_task_with_scoped_token() -> None:
    project = _project()
    agent_id, token = _register_agent(project)

    response = client.post("/agents/tasks", json=_task_payload(project.id), headers=_headers(agent_id, token))

    assert response.status_code == 200
    task = response.json()
    assert task["provider"] == "CHATGPT"
    assert task["external_agent_id"] == agent_id
    assert task["external_agent_name"] == "Scoped GPT"
    assert task["metadata"]["external_agent_scoped"] is True
    assert store.agent_tasks[task["id"]].external_agent_id == agent_id
    assert any(event.event_type == "TASK_CREATED" for event in store.agent_task_events.values())
    assert any(audit.event_type == "agent_task.created_by_agent" for audit in store.audit_events)


def test_external_agent_can_read_own_task_and_events() -> None:
    project = _project()
    agent_id, token = _register_agent(project)
    created = client.post("/agents/tasks", json=_task_payload(project.id), headers=_headers(agent_id, token)).json()

    task_response = client.get(f"/agents/tasks/{created['id']}", headers=_headers(agent_id, token))
    events_response = client.get(f"/agents/tasks/{created['id']}/events", headers=_headers(agent_id, token))

    assert task_response.status_code == 200
    assert events_response.status_code == 200
    assert task_response.json()["id"] == created["id"]
    assert events_response.json()[0]["event_type"] == "TASK_CREATED"


def test_external_agent_can_append_event_to_own_task() -> None:
    project = _project()
    agent_id, token = _register_agent(project)
    created = client.post("/agents/tasks", json=_task_payload(project.id), headers=_headers(agent_id, token)).json()

    response = client.post(
        f"/agents/tasks/{created['id']}/events",
        json={"event_type": "PLAN_RECEIVED", "message": "Plan ready", "status": "PLANNING", "metadata": {"phase": "plan"}},
        headers=_headers(agent_id, token),
    )

    assert response.status_code == 200
    event = response.json()
    assert event["event_type"] == "PLAN_RECEIVED"
    assert event["actor"] == f"agent:{agent_id}"
    assert event["metadata"]["agent_id"] == agent_id
    assert store.agent_tasks[created["id"]].status == AgentTaskStatus.PLANNING
    assert any(audit.event_type == "agent_task.event_recorded_by_agent" for audit in store.audit_events)


def test_external_agent_requires_allowed_create_task_action() -> None:
    project = _project()
    agent_id, token = _register_agent(project, actions=["READ_AGENT_TASK"])

    response = client.post("/agents/tasks", json=_task_payload(project.id), headers=_headers(agent_id, token))

    assert response.status_code == 403
    assert response.json()["detail"] == "Agent is not allowed to perform CREATE_AGENT_TASK"


def test_external_agent_project_scope_is_enforced() -> None:
    allowed_project = _project()
    blocked_project = Project(name="Blocked", description="demo")
    store.projects[blocked_project.id] = blocked_project
    agent_id, token = _register_agent(allowed_project)

    response = client.post("/agents/tasks", json=_task_payload(blocked_project.id), headers=_headers(agent_id, token))

    assert response.status_code == 403
    assert response.json()["detail"] == "Agent is not allowed for this project"


def test_external_agent_repository_scope_is_enforced() -> None:
    project = _project()
    agent_id, token = _register_agent(project, repositories=["ramgolladi1503-sys/aixion-control-tower"])

    response = client.post("/agents/tasks", json=_task_payload(project.id, repository="other/repo"), headers=_headers(agent_id, token))

    assert response.status_code == 403
    assert response.json()["detail"] == "Agent is not allowed for this repository"


def test_external_agent_cannot_read_other_agent_task() -> None:
    project = _project()
    agent_a_id, agent_a_token = _register_agent(project)
    agent_b_id, agent_b_token = _register_agent(project)
    created = client.post("/agents/tasks", json=_task_payload(project.id), headers=_headers(agent_a_id, agent_a_token)).json()

    response = client.get(f"/agents/tasks/{created['id']}", headers=_headers(agent_b_id, agent_b_token))

    assert response.status_code == 403
    assert response.json()["detail"] == "Agent is not allowed for this task"


def test_external_agent_invalid_token_is_rejected() -> None:
    project = _project()
    agent_id, _ = _register_agent(project)

    response = client.post("/agents/tasks", json=_task_payload(project.id), headers=_headers(agent_id, "bad-token"))

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid external agent token"


def test_manual_external_agent_cannot_authenticate_with_token() -> None:
    project = _project()
    agent = ExternalAgent(
        provider=AgentProvider.MANUAL,
        display_name="Manual Agent",
        auth_type=AgentAuthType.MANUAL,
        allowed_project_ids=[project.id],
        allowed_actions=[AgentAction.CREATE_AGENT_TASK],
        enabled=True,
        secret_hash="not-used",
    )
    store.external_agents[agent.id] = agent
    store.persist()

    response = client.post("/agents/tasks", json=_task_payload(project.id), headers=_headers(agent.id, "not-used"))

    assert response.status_code == 401
    assert response.json()["detail"] == "Manual agents cannot authenticate through agent token"
