from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .agent_task_models import AgentTask, AgentTaskEvent, AgentTaskEventType, AgentTaskStatus
from .models import AuditEvent, new_id
from .store import store

TERMINAL_STATUSES = {
    AgentTaskStatus.DENIED,
    AgentTaskStatus.FAILED,
    AgentTaskStatus.CANCELLED,
    AgentTaskStatus.DONE,
}


@dataclass
class AgentWorkerDryRunResult:
    success: bool
    task_id: str | None = None
    worker_id: str = "agent-worker-dry-run"
    lease_token: str | None = None
    started_event_id: str | None = None
    result_event_id: str | None = None
    final_status: str | None = None
    reason: str = ""
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["generated_at"] = self.generated_at.isoformat()
        return payload


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _task_events(task_id: str) -> list[AgentTaskEvent]:
    return [event for event in store.agent_task_events.values() if event.task_id == task_id]


def _append_worker_event(
    task: AgentTask,
    event_type: AgentTaskEventType,
    message: str,
    status: AgentTaskStatus,
    worker_id: str,
    lease_token: str | None,
    metadata: dict[str, Any] | None = None,
) -> AgentTaskEvent:
    task.status = status
    task.updated_at = _now()
    event = AgentTaskEvent(
        task_id=task.id,
        event_type=event_type,
        message=message,
        status=status,
        actor=worker_id,
        metadata={"dry_run": True, "worker_id": worker_id, "lease_token": lease_token, **(metadata or {})},
    )
    store.agent_task_events[event.id] = event
    return event


def _audit(event_type: str, entity_id: str, details: dict[str, Any], actor: str) -> AuditEvent:
    event = AuditEvent(event_type=event_type, entity_id=entity_id, details=details, actor=actor)
    store.audit_events.append(event)
    return event


def _first_approved_task() -> AgentTask | None:
    candidates = [
        task
        for task in store.agent_tasks.values()
        if task.status == AgentTaskStatus.APPROVED and task.approval_request_id and _is_lease_available(task)
    ]
    return sorted(candidates, key=lambda task: task.updated_at)[0] if candidates else None


def _is_lease_available(task: AgentTask) -> bool:
    if not task.worker_lease_owner:
        return True
    if task.worker_lease_expires_at is None:
        return False
    return task.worker_lease_expires_at <= _now()


def _validate_task(task: AgentTask | None, task_id: str | None) -> str | None:
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


def _claim_task(task: AgentTask, worker_id: str, lease_seconds: int) -> str:
    lease_token = new_id("agent_task_lease")
    task.worker_lease_owner = worker_id
    task.worker_lease_token = lease_token
    task.worker_lease_expires_at = _now() + timedelta(seconds=lease_seconds)
    task.updated_at = _now()
    _audit(
        "agent_worker.task_claimed",
        task.id,
        {
            "worker_id": worker_id,
            "lease_token": lease_token,
            "lease_expires_at": task.worker_lease_expires_at.isoformat(),
        },
        actor=worker_id,
    )
    return lease_token


def _release_task(task: AgentTask, lease_token: str | None, worker_id: str) -> None:
    if lease_token and task.worker_lease_token != lease_token:
        _audit(
            "agent_worker.lease_release_skipped",
            task.id,
            {
                "worker_id": worker_id,
                "lease_token": lease_token,
                "current_lease_token": task.worker_lease_token,
            },
            actor=worker_id,
        )
        return
    task.worker_lease_owner = None
    task.worker_lease_expires_at = None
    task.worker_lease_token = None
    task.updated_at = _now()
    _audit("agent_worker.task_released", task.id, {"worker_id": worker_id, "lease_token": lease_token}, actor=worker_id)


def run_agent_worker_dry_run(
    *,
    task_id: str | None = None,
    first_approved: bool = False,
    worker_id: str = "agent-worker-dry-run",
    lease_seconds: int = 300,
) -> AgentWorkerDryRunResult:
    if not task_id and not first_approved:
        return AgentWorkerDryRunResult(
            success=False,
            worker_id=worker_id,
            reason="Provide task_id or set first_approved=true.",
        )

    task = _first_approved_task() if first_approved and not task_id else store.agent_tasks.get(str(task_id))
    validation_error = _validate_task(task, task_id)
    if validation_error or task is None:
        return AgentWorkerDryRunResult(
            success=False,
            task_id=task.id if task else task_id,
            worker_id=worker_id,
            final_status=task.status if task else None,
            reason=validation_error or "Agent task not eligible for dry-run.",
        )

    lease_token = _claim_task(task, worker_id, lease_seconds)

    started = _append_worker_event(
        task,
        AgentTaskEventType.EXECUTION_STARTED,
        "Dry-run worker claimed approved task and started lifecycle proof. No repository mutation will be performed.",
        AgentTaskStatus.EXECUTING,
        worker_id,
        lease_token,
        metadata={
            "approval_request_id": task.approval_request_id,
            "repository": task.repository,
            "branch_preference": task.branch_preference,
            "event_count_before": len(_task_events(task.id)),
            "lease_expires_at": task.worker_lease_expires_at.isoformat() if task.worker_lease_expires_at else None,
        },
    )
    _audit(
        "agent_worker.dry_run_started",
        task.id,
        {"approval_request_id": task.approval_request_id, "worker_id": worker_id, "lease_token": lease_token},
        actor=worker_id,
    )

    result = _append_worker_event(
        task,
        AgentTaskEventType.RESULT_READY,
        "Dry-run worker completed lifecycle proof without changing files, branches, or pull requests.",
        AgentTaskStatus.DONE,
        worker_id,
        lease_token,
        metadata={
            "approval_request_id": task.approval_request_id,
            "repository_mutated": False,
            "branch_created": False,
            "pull_request_opened": False,
            "safe_next_step": "Implement real worker mutation behind the same claim/lease guard.",
        },
    )
    _audit(
        "agent_worker.dry_run_completed",
        task.id,
        {
            "approval_request_id": task.approval_request_id,
            "worker_id": worker_id,
            "lease_token": lease_token,
            "result_event_id": result.id,
        },
        actor=worker_id,
    )
    _release_task(task, lease_token, worker_id)
    store.persist()

    return AgentWorkerDryRunResult(
        success=True,
        task_id=task.id,
        worker_id=worker_id,
        lease_token=lease_token,
        started_event_id=started.id,
        result_event_id=result.id,
        final_status=task.status,
        reason="Dry-run lifecycle completed.",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Aixion agent worker dry-run lifecycle.")
    parser.add_argument("--task-id", default=None, help="Specific AgentTask id to dry-run.")
    parser.add_argument("--first-approved", action="store_true", help="Use the first approved task with a linked approval.")
    parser.add_argument("--worker-id", default="agent-worker-dry-run", help="Worker id written into task events.")
    parser.add_argument("--lease-seconds", type=int, default=300, help="Claim lease duration in seconds.")
    parser.add_argument("--json", action="store_true", help="Print JSON result.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    result = run_agent_worker_dry_run(
        task_id=args.task_id,
        first_approved=args.first_approved,
        worker_id=args.worker_id,
        lease_seconds=args.lease_seconds,
    )
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        decision = "PASS" if result.success else "REFUSED"
        print(f"Agent worker dry-run: {decision}")
        print(f"Task: {result.task_id or 'n/a'}")
        print(f"Lease token: {result.lease_token or 'n/a'}")
        print(f"Final status: {result.final_status or 'n/a'}")
        print(f"Reason: {result.reason}")
    return 0 if result.success else 1
