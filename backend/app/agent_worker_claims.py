from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .agent_task_models import AgentTask, AgentTaskEvent, AgentTaskStatus
from .agent_task_repository import (
    AgentTaskRepository,
    PostgresAgentTaskRepository,
    SQLiteAgentTaskRepository,
)
from .approval_decisions import agent_task_repository
from .models import AuditEvent, now_utc
from .settings import get_settings
from .store import store


@dataclass
class AgentWorkerClaimResult:
    success: bool
    task: AgentTask | None = None
    task_id: str | None = None
    worker_id: str = "agent-worker"
    lease_token: str | None = None
    reason: str = ""
    lease_expires_at: datetime | None = None
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_metadata(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "lease_token": self.lease_token,
            "reason": self.reason,
            "lease_expires_at": self.lease_expires_at.isoformat() if self.lease_expires_at else None,
            "generated_at": self.generated_at.isoformat(),
        }


def _claim(
    task_id: str | None,
    worker_id: str,
    lease_seconds: int,
    repository: AgentTaskRepository | None = None,
) -> AgentWorkerClaimResult:
    settings = get_settings()
    active_repository = repository or agent_task_repository
    if (settings.is_production or settings.postgres_auth_enabled) and not isinstance(
        active_repository, PostgresAgentTaskRepository
    ):
        raise RuntimeError(
            "Agent worker claims require a PostgreSQL task repository in production "
            "or PostgreSQL-auth mode"
        )
    task, reason, claim_audit = active_repository.claim(
        task_id=task_id,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
    )
    if claim_audit is not None and isinstance(active_repository, SQLiteAgentTaskRepository):
        _project_claim_audit(claim_audit)
    if task is None or reason:
        return AgentWorkerClaimResult(
            success=False,
            task=task,
            task_id=task.id if task else task_id,
            worker_id=worker_id,
            reason=reason or "Agent task not eligible for claim.",
        )
    # The durable transaction is authoritative. Update this process-local projection
    # only after the repository confirms the lease commit.
    if isinstance(active_repository, SQLiteAgentTaskRepository):
        store.agent_tasks[task.id] = task
    return AgentWorkerClaimResult(
        success=True,
        task=task,
        task_id=task.id,
        worker_id=worker_id,
        lease_token=task.worker_lease_token,
        lease_expires_at=task.worker_lease_expires_at,
        reason="Agent task claimed.",
    )


def _project_claim_audit(event: AuditEvent) -> None:
    if not any(existing.id == event.id for existing in store.audit_events):
        store.audit_events.append(event)


def commit_agent_task_worker_outcome(
    task: AgentTask,
    *,
    worker_id: str,
    repository: AgentTaskRepository,
    audit_type: str,
    details: dict[str, Any],
    event: AgentTaskEvent | None = None,
    updated_status: AgentTaskStatus | None = None,
) -> AgentTask:
    """Commit a claimed worker's outcome and lease release as one durable unit."""
    updated = task.model_copy(deep=True)
    updated.worker_lease_owner = None
    updated.worker_lease_expires_at = None
    updated.worker_lease_token = None
    if updated_status is not None:
        updated.status = updated_status
    updated.updated_at = now_utc()
    audit_details = {"worker_id": worker_id, **details}
    if event is not None:
        audit_details["event_id"] = event.id
    audit = AuditEvent(
        event_type=audit_type,
        entity_id=task.id,
        workspace_id=task.workspace_id,
        actor=worker_id,
        details=audit_details,
    )
    repository.commit_worker_outcome(
        expected=task, updated=updated, event=event, audit=audit,
    )
    if isinstance(repository, SQLiteAgentTaskRepository):
        for field_name in AgentTask.model_fields:
            setattr(task, field_name, getattr(updated, field_name))
        if event is not None:
            store.agent_task_events[event.id] = event
        store.audit_events.append(audit)
    return updated


def claim_agent_task_for_worker(
    *,
    task_id: str,
    worker_id: str,
    lease_seconds: int = 300,
    repository: AgentTaskRepository | None = None,
) -> AgentWorkerClaimResult:
    """Claim a specific task through the configured durable repository."""
    return _claim(task_id, worker_id, lease_seconds, repository)


def claim_first_approved_agent_task_for_worker(
    *,
    worker_id: str,
    lease_seconds: int = 300,
    repository: AgentTaskRepository | None = None,
) -> AgentWorkerClaimResult:
    """Claim the oldest eligible approved task through the configured repository."""
    return _claim(None, worker_id, lease_seconds, repository)
