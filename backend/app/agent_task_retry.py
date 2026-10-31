from __future__ import annotations

from pydantic import BaseModel, Field

from .approval_decisions import agent_task_repository
from .agent_task_models import AgentTask, AgentTaskEvent, AgentTaskEventType, AgentTaskStatus
from .agent_task_repository import AgentTaskRepository
from .approval_integrity import compute_approval_payload_hash
from .models import ApprovalRequest, ApprovalStatus, AuditEvent, now_utc
from .agent_task_repository import RETRY_REQUESTED_OPERATION

DEFAULT_MAX_RETRIES = 3


class AgentTaskRetryRequest(BaseModel):
    reason: str = Field(default="", max_length=500)
    max_retries: int = Field(default=DEFAULT_MAX_RETRIES, ge=0, le=10)


class AgentTaskRetrySummary(BaseModel):
    task_id: str
    retry_count: int
    max_retries: int
    retry_allowed: bool
    reason: str


def count_retry_requests(
    task_id: str,
    *,
    repository: AgentTaskRepository | None = None,
) -> int:
    # Request routes always pass their selected repository. The module-level
    # adapter remains only for direct local/test callers that have no request.
    selected_repository = (
        agent_task_repository if repository is None else repository
    )
    return selected_repository.count_retries(task_id)


def retry_summary(
    task: AgentTask,
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    repository: AgentTaskRepository | None = None,
    approval: ApprovalRequest | None = None,
    require_approved_linked_approval: bool = False,
) -> AgentTaskRetrySummary:
    retry_count = count_retry_requests(task.id, repository=repository)
    if task.status != AgentTaskStatus.FAILED:
        return AgentTaskRetrySummary(
            task_id=task.id,
            retry_count=retry_count,
            max_retries=max_retries,
            retry_allowed=False,
            reason="Only FAILED agent tasks can be retried.",
        )
    if not task.approval_request_id:
        return AgentTaskRetrySummary(
            task_id=task.id,
            retry_count=retry_count,
            max_retries=max_retries,
            retry_allowed=False,
            reason="Agent task has no linked approval request.",
        )
    if require_approved_linked_approval:
        if approval is None:
            return AgentTaskRetrySummary(
                task_id=task.id,
                retry_count=retry_count,
                max_retries=max_retries,
                retry_allowed=False,
                reason="Linked approval request was not found.",
            )
        if (
            approval.id != task.approval_request_id
            or approval.project_id != task.project_id
            or approval.workspace_id != task.workspace_id
        ):
            return AgentTaskRetrySummary(
                task_id=task.id,
                retry_count=retry_count,
                max_retries=max_retries,
                retry_allowed=False,
                reason="Linked approval scope does not match the AgentTask.",
            )
        if approval.status != ApprovalStatus.APPROVED:
            return AgentTaskRetrySummary(
                task_id=task.id,
                retry_count=retry_count,
                max_retries=max_retries,
                retry_allowed=False,
                reason=(
                    "Linked approval is not approved; obtain approval again before retrying."
                ),
            )
        if (
            not approval.approved_payload_hash
            or approval.approved_payload_hash != compute_approval_payload_hash(approval)
        ):
            return AgentTaskRetrySummary(
                task_id=task.id,
                retry_count=retry_count,
                max_retries=max_retries,
                retry_allowed=False,
                reason="Linked approval is missing a valid frozen action hash.",
            )
    if retry_count >= max_retries:
        return AgentTaskRetrySummary(
            task_id=task.id,
            retry_count=retry_count,
            max_retries=max_retries,
            retry_allowed=False,
            reason=f"Retry limit reached: {retry_count} >= {max_retries}.",
        )
    return AgentTaskRetrySummary(
        task_id=task.id,
        retry_count=retry_count,
        max_retries=max_retries,
        retry_allowed=True,
        reason="Agent task can be retried.",
    )


def append_retry_requested_event(
    task: AgentTask,
    *,
    actor: str,
    reason: str,
    retry_count: int,
    max_retries: int,
) -> AgentTaskEvent:
    task.status = AgentTaskStatus.APPROVED
    task.worker_lease_owner = None
    task.worker_lease_expires_at = None
    task.worker_lease_token = None
    task.updated_at = now_utc()
    event = AgentTaskEvent(
        task_id=task.id,
        event_type=AgentTaskEventType.NOTE,
        message="Agent task retry requested by operator.",
        status=task.status,
        actor=actor,
        metadata={
            "operation_type": RETRY_REQUESTED_OPERATION,
            "retry_count": retry_count + 1,
            "previous_retry_count": retry_count,
            "max_retries": max_retries,
            "reason": reason,
            "worker_lease_cleared": True,
        },
    )
    return event


def audit_retry_requested(task: AgentTask, *, actor: str, event: AgentTaskEvent) -> AuditEvent:
    audit = AuditEvent(
        event_type="agent_task.retry_requested",
        entity_id=task.id,
        workspace_id=task.workspace_id,
        actor=actor,
        details={
            "event_id": event.id,
            "approval_request_id": task.approval_request_id,
            "task_status": task.status,
            "retry_count": event.metadata.get("retry_count"),
            "max_retries": event.metadata.get("max_retries"),
            "worker_lease_cleared": True,
        },
    )
    return audit
