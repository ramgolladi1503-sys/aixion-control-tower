from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from fastapi.testclient import TestClient

from app.agent_task_models import AgentTask, AgentTaskEvent, AgentTaskEventType, AgentTaskStatus
from app.agent_task_retry import RETRY_REQUESTED_OPERATION, retry_summary
from app.main import app
from app.models import AgentProvider, ApprovalRequest, ApprovalStatus, Project, RiskAssessment, RiskLevel
from app.store import store

client = TestClient(app)


def setup_function() -> None:
    store.reset()


def _project() -> Project:
    project = Project(name="Retry Demo", description="demo")
    store.projects[project.id] = project
    return project


def _failed_task(*, with_approval: bool = True) -> AgentTask:
    project = _project()
    approval_id = None
    if with_approval:
        approval = ApprovalRequest(
            project_id=project.id,
            title="Retry approved work",
            summary="Approved work that failed in worker execution.",
            agent_name="retry-worker",
            target_branch="feature/retry-worker",
            files=[],
            test_plan=[],
            rollback_plan="Retry or close branch.",
            risk=RiskAssessment(level=RiskLevel.LOW),
            status=ApprovalStatus.APPROVED,
        )
        store.approval_requests[approval.id] = approval
        approval_id = approval.id
    task = AgentTask(
        provider=AgentProvider.CODEX,
        project_id=project.id,
        title="Retry failed task",
        goal="Retry failed worker execution.",
        repository="ramgolladi1503-sys/aixion-control-tower",
        branch_preference="feature/retry-worker",
        status=AgentTaskStatus.FAILED,
        approval_request_id=approval_id,
    )
    task.worker_lease_owner = "stale-worker"
    task.worker_lease_token = "stale-token"
    store.agent_tasks[task.id] = task
    store.persist()
    return task


def _retry_event(task: AgentTask, retry_count: int) -> AgentTaskEvent:
    event = AgentTaskEvent(
        task_id=task.id,
        event_type=AgentTaskEventType.NOTE,
        message="Retry requested.",
        status=AgentTaskStatus.APPROVED,
        actor="tester",
        metadata={"operation_type": RETRY_REQUESTED_OPERATION, "retry_count": retry_count},
    )
    store.agent_task_events[event.id] = event
    return event


def test_retry_summary_allows_failed_task_with_linked_approval() -> None:
    task = _failed_task()

    response = client.get(f"/agent/tasks/{task.id}/retry")

    assert response.status_code == 200
    payload = response.json()
    assert payload["task_id"] == task.id
    assert payload["retry_count"] == 0
    assert payload["max_retries"] == 3
    assert payload["retry_allowed"] is True
    assert payload["reason"] == "Agent task can be retried."


def test_retry_failed_task_resets_to_approved_and_clears_lease() -> None:
    task = _failed_task()

    response = client.post(f"/agent/tasks/{task.id}/retry", json={"reason": "validation flake", "max_retries": 3})

    assert response.status_code == 200
    event = response.json()
    updated = store.agent_tasks[task.id]
    assert updated.status == AgentTaskStatus.APPROVED
    assert updated.worker_lease_owner is None
    assert updated.worker_lease_token is None
    assert event["event_type"] == "NOTE"
    assert event["status"] == "APPROVED"
    assert event["metadata"]["operation_type"] == RETRY_REQUESTED_OPERATION
    assert event["metadata"]["retry_count"] == 1
    assert event["metadata"]["previous_retry_count"] == 0
    assert event["metadata"]["reason"] == "validation flake"
    assert event["metadata"]["worker_lease_cleared"] is True
    assert any(audit.event_type == "agent_task.retry_requested" for audit in store.audit_events)


def test_retry_refuses_non_failed_task() -> None:
    task = _failed_task()
    task.status = AgentTaskStatus.APPROVED
    store.persist()

    response = client.post(f"/agent/tasks/{task.id}/retry", json={"reason": "not failed"})

    assert response.status_code == 409
    assert response.json()["detail"] == "Only FAILED agent tasks can be retried."


def test_retry_refuses_failed_task_without_approval() -> None:
    task = _failed_task(with_approval=False)

    response = client.post(f"/agent/tasks/{task.id}/retry", json={"reason": "missing approval"})

    assert response.status_code == 409
    assert response.json()["detail"] == "Agent task has no linked approval request."


def test_retry_refuses_after_retry_limit() -> None:
    task = _failed_task()
    _retry_event(task, 1)

    response = client.post(f"/agent/tasks/{task.id}/retry", json={"reason": "too many", "max_retries": 1})

    assert response.status_code == 409
    assert response.json()["detail"] == "Retry limit reached: 1 >= 1."


def test_retry_summary_counts_retry_events() -> None:
    task = _failed_task()
    _retry_event(task, 1)
    _retry_event(task, 2)

    summary = retry_summary(task, max_retries=3)

    assert summary.retry_count == 2
    assert summary.retry_allowed is True


def test_retry_summary_missing_task_returns_404() -> None:
    response = client.get("/agent/tasks/agent_task_missing/retry")

    assert response.status_code == 404
