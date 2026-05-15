from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from fastapi.testclient import TestClient

from app.agent_task_cancel import CANCEL_REQUESTED_OPERATION, cancel_summary
from app.agent_task_models import AgentTask, AgentTaskStatus
from app.main import app
from app.models import AgentProvider, Project
from app.store import store

client = TestClient(app)


def setup_function() -> None:
    store.reset()


def _task(status: AgentTaskStatus) -> AgentTask:
    project = Project(name="Cancel Demo", description="demo")
    store.projects[project.id] = project
    task = AgentTask(
        provider=AgentProvider.CODEX,
        project_id=project.id,
        title="Cancelable task",
        goal="Cancel task safely.",
        repository="ramgolladi1503-sys/aixion-control-tower",
        branch_preference="feature/cancel-task",
        status=status,
        approval_request_id="approval_cancel_demo",
    )
    task.worker_lease_owner = "stale-worker"
    task.worker_lease_token = "stale-token"
    store.agent_tasks[task.id] = task
    store.persist()
    return task


def test_cancel_summary_allows_active_task() -> None:
    task = _task(AgentTaskStatus.EXECUTING)

    response = client.get(f"/agent/tasks/{task.id}/cancel")

    assert response.status_code == 200
    payload = response.json()
    assert payload["task_id"] == task.id
    assert payload["current_status"] == "EXECUTING"
    assert payload["cancel_allowed"] is True
    assert payload["reason"] == "Agent task can be cancelled."


def test_cancel_task_moves_to_cancelled_and_clears_lease() -> None:
    task = _task(AgentTaskStatus.TESTING)

    response = client.post(f"/agent/tasks/{task.id}/cancel", json={"reason": "operator stopped stale run"})

    assert response.status_code == 200
    event = response.json()
    updated = store.agent_tasks[task.id]
    assert updated.status == AgentTaskStatus.CANCELLED
    assert updated.worker_lease_owner is None
    assert updated.worker_lease_token is None
    assert event["event_type"] == "CANCELLED"
    assert event["status"] == "CANCELLED"
    assert event["metadata"]["operation_type"] == CANCEL_REQUESTED_OPERATION
    assert event["metadata"]["previous_status"] == "TESTING"
    assert event["metadata"]["new_status"] == "CANCELLED"
    assert event["metadata"]["reason"] == "operator stopped stale run"
    assert event["metadata"]["worker_lease_cleared"] is True
    assert event["metadata"]["approval_decision_changed"] is False
    assert event["metadata"]["worker_cleanup_performed"] is False
    assert any(audit.event_type == "agent_task.cancelled" for audit in store.audit_events)


def test_cancel_failed_task_is_allowed() -> None:
    task = _task(AgentTaskStatus.FAILED)

    response = client.post(f"/agent/tasks/{task.id}/cancel", json={"reason": "do not retry"})

    assert response.status_code == 200
    assert store.agent_tasks[task.id].status == AgentTaskStatus.CANCELLED


def test_cancel_refuses_ready_for_pr_without_cleanup() -> None:
    task = _task(AgentTaskStatus.READY_FOR_PR)

    response = client.post(f"/agent/tasks/{task.id}/cancel", json={"reason": "wrong path"})

    assert response.status_code == 409
    assert response.json()["detail"] == "READY_FOR_PR tasks require PR/branch cleanup, not simple cancellation."
    assert store.agent_tasks[task.id].status == AgentTaskStatus.READY_FOR_PR


def test_cancel_refuses_done_task() -> None:
    task = _task(AgentTaskStatus.DONE)

    response = client.post(f"/agent/tasks/{task.id}/cancel", json={"reason": "too late"})

    assert response.status_code == 409
    assert response.json()["detail"] == "Agent task is already terminal or review-ready: DONE."


def test_cancel_refuses_already_cancelled_task() -> None:
    task = _task(AgentTaskStatus.CANCELLED)

    response = client.post(f"/agent/tasks/{task.id}/cancel", json={"reason": "again"})

    assert response.status_code == 409
    assert response.json()["detail"] == "Agent task is already terminal or review-ready: CANCELLED."


def test_cancel_summary_missing_task_returns_404() -> None:
    response = client.get("/agent/tasks/agent_task_missing/cancel")

    assert response.status_code == 404


def test_cancel_summary_helper_for_ready_for_pr() -> None:
    task = _task(AgentTaskStatus.READY_FOR_PR)

    summary = cancel_summary(task)

    assert summary.cancel_allowed is False
    assert summary.reason == "READY_FOR_PR tasks require PR/branch cleanup, not simple cancellation."
