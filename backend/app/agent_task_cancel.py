from __future__ import annotations

from pydantic import BaseModel, Field

from .agent_task_models import AgentTask, AgentTaskEvent, AgentTaskEventType, AgentTaskStatus
from .models import (
    ApprovalRequest,
    ApprovalStatus,
    AuditEvent,
    Notification,
    now_utc,
)

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
    if any((task.worker_lease_owner, task.worker_lease_token, task.worker_lease_expires_at)):
        return AgentTaskCancelSummary(
            task_id=task.id,
            current_status=task.status,
            cancel_allowed=False,
            reason=(
                "Agent task has a recorded worker lease. Confirm the worker has stopped "
                "and reconcile the lease before cancelling."
            ),
        )
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


def prepare_linked_approval_cancellation(
    task: AgentTask,
    approval: ApprovalRequest,
    *,
    actor: str,
    reason: str,
) -> tuple[ApprovalRequest | None, AuditEvent | None, Notification | None]:
    if (
        task.status != AgentTaskStatus.WAITING_FOR_APPROVAL
        or not task.approval_request_id
        or approval.id != task.approval_request_id
        or approval.project_id != task.project_id
        or approval.workspace_id != task.workspace_id
    ):
        raise ValueError("Linked approval identity or scope does not match the waiting AgentTask.")
    if approval.status in {
        ApprovalStatus.DENIED,
        ApprovalStatus.EXPIRED,
        ApprovalStatus.CANCELLED,
    }:
        return None, None, None
    if approval.status not in {
        ApprovalStatus.REQUESTED,
        ApprovalStatus.PENDING_REVIEW,
        ApprovalStatus.BLOCKED,
    }:
        raise ValueError("Linked approval is no longer pending; reconcile its decision before cancelling the task.")

    cancelled = approval.model_copy(
        update={"status": ApprovalStatus.CANCELLED, "updated_at": now_utc()}
    )
    audit = AuditEvent(
        event_type="approval.cancelled",
        entity_id=approval.id,
        workspace_id=approval.workspace_id,
        actor=actor,
        created_at=cancelled.updated_at,
        details={
            "project_id": approval.project_id,
            "source_task_id": task.id,
            "previous_status": approval.status,
            "new_status": ApprovalStatus.CANCELLED,
            "approved_payload_hash": approval.approved_payload_hash,
            "reason": reason,
        },
    )
    notification = Notification(
        title=f"Approval cancelled: {approval.title}",
        body="The linked agent task was cancelled. This approval can no longer be acted on.",
        entity_type="approval_request",
        entity_id=approval.id,
        approval_request_id=approval.id,
        workspace_id=approval.workspace_id,
        user_id=approval.created_by_user_id,
        created_at=cancelled.updated_at,
    )
    return cancelled, audit, notification


def append_cancel_requested_event(
    task: AgentTask,
    *,
    actor: str,
    reason: str,
    approval_decision_changed: bool = False,
) -> AgentTaskEvent:
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
            "approval_decision_changed": approval_decision_changed,
            "worker_cleanup_performed": False,
        },
    )
    return event


def audit_cancel_requested(task: AgentTask, *, actor: str, event: AgentTaskEvent) -> AuditEvent:
    audit = AuditEvent(
        event_type="agent_task.cancelled",
        entity_id=task.id,
        workspace_id=task.workspace_id,
        actor=actor,
        details={
            "event_id": event.id,
            "approval_request_id": task.approval_request_id,
            "task_status": task.status,
            "previous_status": event.metadata.get("previous_status"),
            "worker_lease_cleared": True,
            "approval_decision_changed": event.metadata.get("approval_decision_changed") is True,
            "worker_cleanup_performed": False,
        },
    )
    return audit
