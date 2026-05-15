from __future__ import annotations

from pydantic import BaseModel, Field

from .agent_task_models import AgentTask, AgentTaskEvent, AgentTaskEventType, AgentTaskStatus
from .models import AuditEvent, now_utc
from .store import store

CANCELLABLE_STATUSES = {
    AgentTaskStatus.RECEIVED,
    AgentTaskStatus.PLANNING,
    AgentTaskStatus.WAITING_FOR_APPROVAL,
    AgentTaskStatus.APPROVED,
    AgentTaskStatus.EXECUTING,
    AgentTaskStatus.TESTING,
    AgentTaskStatus.FAILED,
}
TERMINAL_OR_REVIEW_STATUSES = {
    AgentTaskStatus.CANCELLED,
    AgentTaskStatus.DENIED,
    AgentTaskStatus.DONE,
    AgentTaskStatus.READY_FOR_PR,
}
CANCEL_REQUESTED_OPERATION = "agent_task_cancel_requested"


class AgentTaskCancelRequest(BaseModel):
    reason: str = Field(default="", max_length=500)


class AgentTaskCancelSummary(BaseModel):
    task_id: str
    current_status: AgentTaskStatus
    cancel_allowed: bool
    reason: str


def cancel_summary(task: AgentTask) -> AgentTaskCancelSummary:
    if task.status in CANCELLABLE_STATUSES:
        return AgentTaskCancelSummary(
            task_id=task.id,
            current_status=task.status,
            cancel_allowed=True,
            reason="Agent task can be cancelled.",
        )
    if task.status == AgentTaskStatus.READY_FOR_PR:
        return AgentTaskCancelSummary(
            task_id=task.id,
            current_status=task.status,
            cancel_allowed=False,
            reason="READY_FOR_PR tasks require PR/branch cleanup, not simple cancellation.",
        )
    if task.status in TERMINAL_OR_REVIEW_STATUSES:
        return AgentTaskCancelSummary(
            task_id=task.id,
            current_status=task.status,
            cancel_allowed=False,
            reason=f"Agent task is already terminal or review-ready: {task.status}.",
        )
    return AgentTaskCancelSummary(
        task_id=task.id,
        current_status=task.status,
        cancel_allowed=False,
        reason=f"Agent task cannot be cancelled from status: {task.status}.",
    )


def append_cancel_requested_event(task: AgentTask, *, actor: str, reason: str) -> AgentTaskEvent:
    previous_status = task.status
    task.status = AgentTaskStatus.CANCELLED
    task.worker_lease_owner = None
    task.worker_lease_expires_at = None
    task.worker_lease_token = None
    task.updated_at = now_utc()
    event = AgentTaskEvent(
        task_id=task.id,
        event_type=AgentTaskEventType.CANCELLED,
        message="Agent task cancelled by operator.",
        status=task.status,
        actor=actor,
        metadata={
            "operation_type": CANCEL_REQUESTED_OPERATION,
            "previous_status": previous_status,
            "new_status": task.status,
            "reason": reason,
            "worker_lease_cleared": True,
            "approval_decision_changed": False,
            "worker_cleanup_performed": False,
        },
    )
    store.agent_task_events[event.id] = event
    return event


def audit_cancel_requested(task: AgentTask, *, actor: str, event: AgentTaskEvent) -> AuditEvent:
    audit = AuditEvent(
        event_type="agent_task.cancelled",
        entity_id=task.id,
        actor=actor,
        details={
            "event_id": event.id,
            "approval_request_id": task.approval_request_id,
            "task_status": task.status,
            "previous_status": event.metadata.get("previous_status"),
            "worker_lease_cleared": True,
            "approval_decision_changed": False,
            "worker_cleanup_performed": False,
        },
    )
    store.audit_events.append(audit)
    return audit
