from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from fastapi.testclient import TestClient

from app import main
from app.agent_task_routes import audit
from app.main import app
from app.models import Project
from app.store import store

client = TestClient(app)


def setup_function() -> None:
    store.reset()


def test_agent_task_audit_helper_records_workspace_scope() -> None:
    event = audit(
        "agent_task.approval_decision_propagated",
        "task_scoped",
        {"approval_request_id": "approval_scoped"},
        actor="reviewer@example.com",
        workspace_id="workspace_scoped",
    )

    assert event.workspace_id == "workspace_scoped"
    assert store.audit_events[-1] == event


def _project() -> Project:
    project = Project(name="Connected Agent Demo", description="demo")
    store.projects[project.id] = project
    store.persist()
    return project


def _task(project: Project) -> dict:
    response = client.post(
        "/agent/tasks",
        json={
            "project_id": project.id,
            "provider": "CHATGPT",
            "title": "Prepare implementation plan",
            "goal": "Create an approval-backed implementation plan.",
            "requested_action": "CREATE_APPROVAL",
            "source_url": "https://chat.openai.com/demo-task",
            "source_session_id": "chat_session_1",
        },
    )
    assert response.status_code == 200
    return response.json()


def _approval_payload(project: Project) -> dict:
    return {
        "project_id": project.id,
        "title": "Approve implementation plan",
        "summary": "Approval created from an agent task.",
        "agent_name": "chatgpt-agent",
        "target_branch": "feature/agent-task-demo",
        "files": [
            {
                "path": "docs/agent-task-demo.md",
                "change_type": "update",
                "diff": "+ agent task demo",
                "new_content": "agent task demo\n",
            }
        ],
        "test_plan": ["python -m pytest"],
        "rollback_plan": "Deny the approval or revert the branch.",
    }


def test_create_agent_task_approval_links_task_moves_to_waiting_and_notifies() -> None:
    project = _project()
    task = _task(project)
    store.projects.clear()

    response = client.post(f"/agent/tasks/{task['id']}/approval", json=_approval_payload(project))

    assert response.status_code == 200
    approval = response.json()
    updated_task = client.get(f"/agent/tasks/{task['id']}").json()
    events = client.get(f"/agent/tasks/{task['id']}/events").json()
    approval_event = next(event for event in events if event["event_type"] == "APPROVAL_CREATED")
    notification_id = approval_event["metadata"]["notification_id"]
    notification = store.notifications[notification_id]

    assert updated_task["approval_request_id"] == approval["id"]
    assert updated_task["status"] == "WAITING_FOR_APPROVAL"
    assert approval["source_provider"] == "CHATGPT"
    assert approval["source_task_url"] == "https://chat.openai.com/demo-task"
    assert notification.entity_type == "agent_task"
    assert notification.entity_id == task["id"]
    assert notification.approval_request_id == approval["id"]
    assert notification.title == "Approval needed: Prepare implementation plan"
    assert notification.user_id == task["created_by_user_id"]
    assert any(event.event_type == "agent_task.approval_created" for event in store.audit_events)
    assert any(event.details.get("notification_id") == notification_id for event in store.audit_events)


def test_linked_approval_approve_updates_agent_task_status_and_notifies() -> None:
    project = _project()
    task = _task(project)
    approval = client.post(f"/agent/tasks/{task['id']}/approval", json=_approval_payload(project)).json()

    decision = client.post(f"/approvals/{approval['id']}/approve")
    updated_task = client.get(f"/agent/tasks/{task['id']}").json()
    events = client.get(f"/agent/tasks/{task['id']}/events").json()
    approval_event = next(event for event in events if event["event_type"] == "APPROVED")
    notification_id = approval_event["metadata"]["notification_id"]
    notification = store.notifications[notification_id]

    assert decision.status_code == 200
    assert decision.json()["status"] == "APPROVED"
    assert updated_task["status"] == "APPROVED"
    assert notification.entity_type == "agent_task"
    assert notification.entity_id == task["id"]
    assert notification.approval_request_id == approval["id"]
    assert notification.title == "Agent task approved: Prepare implementation plan"
    assert any(event.event_type == "agent_task.approval_decision_propagated" for event in store.audit_events)
    assert any(event.details.get("notification_id") == notification_id for event in store.audit_events)


def test_linked_approval_deny_updates_agent_task_status_and_notifies() -> None:
    project = _project()
    task = _task(project)
    approval = client.post(f"/agent/tasks/{task['id']}/approval", json=_approval_payload(project)).json()

    decision = client.post(f"/approvals/{approval['id']}/deny", json={"decision": "deny", "reason": "Not ready"})
    updated_task = client.get(f"/agent/tasks/{task['id']}").json()
    events = client.get(f"/agent/tasks/{task['id']}/events").json()
    denied_event = next(event for event in events if event["event_type"] == "DENIED")
    notification_id = denied_event["metadata"]["notification_id"]
    notification = store.notifications[notification_id]

    assert decision.status_code == 200
    assert decision.json()["status"] == "DENIED"
    assert updated_task["status"] == "DENIED"
    assert notification.entity_type == "agent_task"
    assert notification.entity_id == task["id"]
    assert notification.approval_request_id == approval["id"]
    assert notification.title == "Agent task denied: Prepare implementation plan"


def test_decision_propagation_uses_committed_task_without_cache(monkeypatch) -> None:
    project = _project()
    task = _task(project)
    approval = client.post(
        f"/agent/tasks/{task['id']}/approval", json=_approval_payload(project)
    ).json()
    original_propagation = main.propagate_approval_decision_to_agent_task

    def propagate_without_compatibility_task_cache(
        approval_request, previous_status, actor, committed=None, **repository_kwargs
    ):
        store.agent_tasks.pop(task["id"], None)
        return original_propagation(
            approval_request,
            previous_status,
            actor,
            committed=committed,
            **repository_kwargs,
        )

    monkeypatch.setattr(
        main,
        "propagate_approval_decision_to_agent_task",
        propagate_without_compatibility_task_cache,
    )
    decision = client.post(
        f"/approvals/{approval['id']}/deny",
        json={"decision": "deny", "reason": "cache projection absent"},
    )

    assert decision.status_code == 200, decision.text
    assert store.agent_tasks[task["id"]].status == "DENIED"
    assert any(
        event.task_id == task["id"] and event.event_type.value == "DENIED"
        for event in store.agent_task_events.values()
    )


def test_agent_task_approval_rejects_duplicate_link() -> None:
    project = _project()
    task = _task(project)
    first = client.post(f"/agent/tasks/{task['id']}/approval", json=_approval_payload(project))
    second = client.post(f"/agent/tasks/{task['id']}/approval", json=_approval_payload(project))

    assert first.status_code == 200
    assert second.status_code == 409


def test_agent_task_approval_requires_matching_project() -> None:
    project = _project()
    other_project = Project(name="Other", description="other")
    store.projects[other_project.id] = other_project
    store.persist()
    task = _task(project)
    payload = _approval_payload(other_project)

    response = client.post(f"/agent/tasks/{task['id']}/approval", json=payload)

    assert response.status_code == 409
