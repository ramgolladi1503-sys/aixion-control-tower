from __future__ import annotations

from pydantic import BaseModel, Field

from .agent_task_models import AgentTask, AgentTaskEvent, AgentTaskEventType, AgentTaskStatus
from .models import AuditEvent, now_utc
from .store import store

RETRY_REQUESTED_OPERATION = "agent_task_retry_requested"
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


def count_retry_requests(task_id: str) -> int:
    return sum(
        1
        for event in store.agent_task_events.values()
        if event.task_id == task_id and event.metadata.get("operation_type") == RETRY_REQUESTED_OPERATION
    )


def retry_summary(task: AgentTask, *, max_retries: int = DEFAULT_MAX_RETRIES) -> AgentTaskRetrySummary:
    retry_count = count_retry_requests(task.id)
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
    store.agent_task_events[event.id] = event
    return event


def audit_retry_requested(task: AgentTask, *, actor: str, event: AgentTaskEvent) -> AuditEvent:
    audit = AuditEvent(
        event_type="agent_task.retry_requested",
        entity_id=task.id,
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
    store.audit_events.append(audit)
    return audit
