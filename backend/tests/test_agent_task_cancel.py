from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from fastapi.testclient import TestClient

from app.agent_run_models import AgentRun, AgentRunStatus
from app.agent_task_cancel import CANCEL_REQUESTED_OPERATION, cancel_summary
from app.agent_task_models import AgentTask, AgentTaskStatus
from app.main import app
from app.models import (
    AgentProvider,
    ApprovalRequest,
    ApprovalStatus,
    FileChange,
    Project,
    RiskAssessment,
    RiskLevel,
)
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


def test_cancel_summary_and_route_refuse_task_with_linked_agent_run() -> None:
    task = _task(AgentTaskStatus.APPROVED)
    run = AgentRun(
        task_id=task.id,
        project_id=task.project_id,
        workspace_id=task.workspace_id,
        approval_request_id=task.approval_request_id,
        status=AgentRunStatus.RUNNING,
    )
    store.agent_runs[run.id] = run
    store.persist()

    summary = client.get(f"/agent/tasks/{task.id}/cancel")
    cancelled = client.post(f"/agent/tasks/{task.id}/cancel", json={"reason": "stop"})

    assert summary.status_code == 200
    assert summary.json()["cancel_allowed"] is False
    assert "Cancel or reconcile the run first" in summary.json()["reason"]
    assert cancelled.status_code == 409
    assert "task cancellation does not stop its worker" in cancelled.json()["detail"]
    assert store.agent_tasks[task.id].status == AgentTaskStatus.APPROVED
    assert store.agent_runs[run.id].status == AgentRunStatus.RUNNING


def test_cancel_task_without_worker_lease_moves_to_cancelled() -> None:
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


def test_cancel_waiting_task_cancels_its_linked_approval_atomically() -> None:
    task = _task(AgentTaskStatus.WAITING_FOR_APPROVAL)
    approval = ApprovalRequest(
        id=task.approval_request_id,
        project_id=task.project_id,
        workspace_id=task.workspace_id,
        title="Linked approval",
        summary="Review before work continues.",
        target_branch="feature/cancel-task",
        files=[FileChange(path="README.md", diff="+change")],
        risk=RiskAssessment(level=RiskLevel.MEDIUM),
    )
    store.approval_requests[approval.id] = approval
    store.persist()

    response = client.post(f"/agent/tasks/{task.id}/cancel", json={"reason": "operator stopped task"})

    assert response.status_code == 200, response.text
    event = response.json()
    assert store.agent_tasks[task.id].status == AgentTaskStatus.CANCELLED
    assert store.approval_requests[approval.id].status == ApprovalStatus.CANCELLED
    assert event["metadata"]["approval_decision_changed"] is True
    assert sum(a.event_type == "approval.cancelled" for a in store.audit_events) == 1
    assert any(
        notification.approval_request_id == approval.id
        and notification.entity_type == "approval_request"
        for notification in store.notifications.values()
    )


def test_cancel_waiting_task_closes_a_blocked_linked_approval() -> None:
    task = _task(AgentTaskStatus.WAITING_FOR_APPROVAL)
    approval = ApprovalRequest(
        id=task.approval_request_id,
        project_id=task.project_id,
        workspace_id=task.workspace_id,
        title="Blocked linked approval",
        summary="A policy-blocked request cannot be approved.",
        target_branch="feature/cancel-task",
        files=[FileChange(path="README.md", diff="+change")],
        risk=RiskAssessment(level=RiskLevel.CRITICAL, blocked=True),
        status=ApprovalStatus.BLOCKED,
    )
    store.approval_requests[approval.id] = approval
    store.persist()

    response = client.post(f"/agent/tasks/{task.id}/cancel", json={"reason": "close blocked work"})

    assert response.status_code == 200, response.text
    assert store.agent_tasks[task.id].status == AgentTaskStatus.CANCELLED
    assert store.approval_requests[approval.id].status == ApprovalStatus.CANCELLED


def test_cancel_waiting_task_fails_closed_when_approval_is_already_approved() -> None:
    task = _task(AgentTaskStatus.WAITING_FOR_APPROVAL)
    approval = ApprovalRequest(
        id=task.approval_request_id,
        project_id=task.project_id,
        workspace_id=task.workspace_id,
        title="Already approved",
        summary="Do not claim cancellation can revoke an approval.",
        target_branch="feature/cancel-task",
        files=[FileChange(path="README.md", diff="+change")],
        risk=RiskAssessment(level=RiskLevel.MEDIUM),
        status=ApprovalStatus.APPROVED,
        approved_payload_hash="sha256:legacy-test-hash",
    )
    store.approval_requests[approval.id] = approval
    store.persist()

    response = client.post(f"/agent/tasks/{task.id}/cancel", json={"reason": "too late"})

    assert response.status_code == 409
    assert store.agent_tasks[task.id].status == AgentTaskStatus.WAITING_FOR_APPROVAL
    assert store.approval_requests[approval.id].status == ApprovalStatus.APPROVED


def test_cancel_waiting_task_fails_closed_when_linked_approval_is_missing() -> None:
    task = _task(AgentTaskStatus.WAITING_FOR_APPROVAL)

    response = client.post(f"/agent/tasks/{task.id}/cancel", json={"reason": "stop orphaned task"})

    assert response.status_code == 409
    assert "Linked approval is missing" in response.json()["detail"]
    assert store.agent_tasks[task.id].status == AgentTaskStatus.WAITING_FOR_APPROVAL


def test_cancel_refuses_task_with_active_worker_lease() -> None:
    task = _task(AgentTaskStatus.EXECUTING)
    task.worker_lease_owner = "worker-a"
    task.worker_lease_token = "lease-a"
    task.worker_lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    store.persist()

    response = client.post(f"/agent/tasks/{task.id}/cancel", json={"reason": "stop"})

    assert response.status_code == 409
    assert "worker lease" in response.json()["detail"]
    assert store.agent_tasks[task.id].status == AgentTaskStatus.EXECUTING
    assert store.agent_tasks[task.id].worker_lease_token == "lease-a"


def test_cancel_summary_rejects_expired_or_incomplete_worker_lease() -> None:
    task = _task(AgentTaskStatus.APPROVED)
    task.worker_lease_owner = "worker-a"
    task.worker_lease_token = "lease-a"
    task.worker_lease_expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    store.persist()

    response = client.get(f"/agent/tasks/{task.id}/cancel")

    assert response.status_code == 200
    assert response.json()["cancel_allowed"] is False
    assert "reconcile the lease" in response.json()["reason"]


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
