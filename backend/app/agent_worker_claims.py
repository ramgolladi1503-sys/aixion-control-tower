from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .agent_task_models import AgentTask, AgentTaskStatus
from .models import new_id
from .store import store

TERMINAL_STATUSES = {
    AgentTaskStatus.DENIED,
    AgentTaskStatus.FAILED,
    AgentTaskStatus.CANCELLED,
    AgentTaskStatus.DONE,
}


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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_lease_available(task: AgentTask) -> bool:
    if not task.worker_lease_owner:
        return True
    if task.worker_lease_expires_at is None:
        return False
    return task.worker_lease_expires_at <= _now()


def _eligibility_failure(task: AgentTask | None, task_id: str | None) -> str | None:
    if task is None:
        return f"Agent task not found or unavailable: {task_id or 'first-approved'}"
    if task.status in TERMINAL_STATUSES:
        return f"Agent task is terminal: {task.status}"
    if task.status != AgentTaskStatus.APPROVED:
        return f"Agent task is not approved: {task.status}"
    if not task.approval_request_id:
        return "Agent task is approved but missing approval_request_id"
    if not _is_lease_available(task):
        return f"Agent task already leased by {task.worker_lease_owner} until {task.worker_lease_expires_at}"
    return None


def _claim_loaded_task(task: AgentTask, worker_id: str, lease_seconds: int) -> AgentWorkerClaimResult:
    lease_token = new_id("agent_task_lease")
    lease_expires_at = _now() + timedelta(seconds=lease_seconds)
    task.worker_lease_owner = worker_id
    task.worker_lease_token = lease_token
    task.worker_lease_expires_at = lease_expires_at
    task.updated_at = _now()
    return AgentWorkerClaimResult(
        success=True,
        task=task,
        task_id=task.id,
        worker_id=worker_id,
        lease_token=lease_token,
        lease_expires_at=lease_expires_at,
        reason="Agent task claimed.",
    )


def claim_agent_task_for_worker(
    *,
    task_id: str,
    worker_id: str,
    lease_seconds: int = 300,
) -> AgentWorkerClaimResult:
    """Claim one approved task using SQLite BEGIN IMMEDIATE.

    This is intentionally stronger than the previous in-memory check. It serializes
    claim attempts at the SQLite writer level before reading and updating the task
    JSON payload.
    """

    with store._connect() as conn:  # noqa: SLF001 - store owns the SQLite-backed MVP persistence boundary.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT payload FROM kv_store WHERE entity_type = ? AND entity_id = ?",
            ("agent_task", task_id),
        ).fetchone()
        task = AgentTask.model_validate_json(row[0]) if row else None
        failure = _eligibility_failure(task, task_id)
        if failure or task is None:
            return AgentWorkerClaimResult(
                success=False,
                task=task,
                task_id=task.id if task else task_id,
                worker_id=worker_id,
                reason=failure or "Agent task not eligible for claim.",
            )

        result = _claim_loaded_task(task, worker_id, lease_seconds)
        conn.execute(
            "UPDATE kv_store SET payload = ? WHERE entity_type = ? AND entity_id = ?",
            (task.model_dump_json(), "agent_task", task.id),
        )
        store.agent_tasks[task.id] = task
        return result


def claim_first_approved_agent_task_for_worker(
    *,
    worker_id: str,
    lease_seconds: int = 300,
) -> AgentWorkerClaimResult:
    """Transactionally claim the oldest approved, linked, lease-available task."""

    with store._connect() as conn:  # noqa: SLF001
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT entity_id, payload FROM kv_store WHERE entity_type = ? ORDER BY entity_id",
            ("agent_task",),
        ).fetchall()
        candidates: list[AgentTask] = []
        for _, payload in rows:
            task = AgentTask.model_validate_json(payload)
            if _eligibility_failure(task, task.id) is None:
                candidates.append(task)

        if not candidates:
            return AgentWorkerClaimResult(
                success=False,
                worker_id=worker_id,
                reason="No approved, linked, lease-available agent task found.",
            )

        task = sorted(candidates, key=lambda candidate: candidate.updated_at)[0]
        result = _claim_loaded_task(task, worker_id, lease_seconds)
        conn.execute(
            "UPDATE kv_store SET payload = ? WHERE entity_type = ? AND entity_id = ?",
            (task.model_dump_json(), "agent_task", task.id),
        )
        store.agent_tasks[task.id] = task
        return result
