from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .agent_task_models import AgentTask, AgentTaskEvent, AgentTaskEventType, AgentTaskStatus
from .agent_worker_claims import claim_agent_task_for_worker, claim_first_approved_agent_task_for_worker
from .models import AuditEvent
from .store import store

PROTECTED_BRANCHES = {"main", "master", "prod", "production", "release"}
SAFE_BRANCH_PREFIXES = ("feature/", "fix/", "chore/", "docs/", "test/", "refactor/")
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+$")


@dataclass
class AgentWorkerBranchPlanResult:
    success: bool
    task_id: str | None = None
    worker_id: str = "agent-worker-branch-plan"
    lease_token: str | None = None
    repository: str | None = None
    planned_branch: str | None = None
    source_branch: str = "main"
    event_id: str | None = None
    final_status: str | None = None
    reason: str = ""
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["generated_at"] = self.generated_at.isoformat()
        return payload


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _slug(value: str, fallback: str = "task") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)
    return (slug or fallback)[:48].strip("-") or fallback


def _short_task_id(task_id: str) -> str:
    return task_id.replace("agent_task_", "")[:12] or task_id[:12]


def _is_protected_branch(branch: str) -> bool:
    normalized = branch.strip().strip("/").lower()
    return normalized in PROTECTED_BRANCHES


def _is_safe_branch_name(branch: str) -> bool:
    clean = branch.strip()
    if not clean:
        return False
    if clean.startswith("/") or clean.endswith("/") or ".." in clean or "//" in clean:
        return False
    if not BRANCH_PATTERN.fullmatch(clean):
        return False
    if _is_protected_branch(clean):
        return False
    return clean.startswith(SAFE_BRANCH_PREFIXES)


def _planned_branch_for_task(task: AgentTask) -> str:
    if task.branch_preference:
        return task.branch_preference.strip()
    return f"feature/agent-task-{_short_task_id(task.id)}-{_slug(task.title)}"


def validate_branch_plan(task: AgentTask) -> str | None:
    if not task.repository or not task.repository.strip():
        return "Agent task is missing repository."
    if not REPOSITORY_PATTERN.fullmatch(task.repository.strip()):
        return f"Agent task repository is not in owner/repo format: {task.repository}"
    planned_branch = _planned_branch_for_task(task)
    if not _is_safe_branch_name(planned_branch):
        return f"Planned branch is unsafe or protected: {planned_branch}"
    return None


def _append_branch_plan_event(
    task: AgentTask,
    worker_id: str,
    lease_token: str | None,
    planned_branch: str,
    source_branch: str,
) -> AgentTaskEvent:
    event = AgentTaskEvent(
        task_id=task.id,
        event_type=AgentTaskEventType.NOTE,
        message="Worker dry-run planned a safe feature branch. No repository branch was created.",
        status=task.status,
        actor=worker_id,
        metadata={
            "dry_run": True,
            "worker_id": worker_id,
            "lease_token": lease_token,
            "repository": task.repository,
            "planned_branch": planned_branch,
            "source_branch": source_branch,
            "branch_created": False,
            "repository_mutated": False,
            "plan_type": "branch_creation_dry_run",
        },
    )
    store.agent_task_events[event.id] = event
    return event


def _audit(event_type: str, entity_id: str, details: dict[str, Any], actor: str) -> AuditEvent:
    event = AuditEvent(event_type=event_type, entity_id=entity_id, details=details, actor=actor)
    store.audit_events.append(event)
    return event


def _release_task(task: AgentTask, lease_token: str | None, worker_id: str) -> None:
    if lease_token and task.worker_lease_token != lease_token:
        _audit(
            "agent_worker.lease_release_skipped",
            task.id,
            {"worker_id": worker_id, "lease_token": lease_token, "current_lease_token": task.worker_lease_token},
            actor=worker_id,
        )
        return
    task.worker_lease_owner = None
    task.worker_lease_expires_at = None
    task.worker_lease_token = None
    task.updated_at = _now()
    _audit("agent_worker.task_released", task.id, {"worker_id": worker_id, "lease_token": lease_token}, actor=worker_id)


def run_agent_worker_branch_plan_dry_run(
    *,
    task_id: str | None = None,
    first_approved: bool = False,
    worker_id: str = "agent-worker-branch-plan",
    lease_seconds: int = 300,
    source_branch: str = "main",
) -> AgentWorkerBranchPlanResult:
    if not task_id and not first_approved:
        return AgentWorkerBranchPlanResult(
            success=False,
            worker_id=worker_id,
            source_branch=source_branch,
            reason="Provide task_id or set first_approved=true.",
        )

    claim = (
        claim_first_approved_agent_task_for_worker(worker_id=worker_id, lease_seconds=lease_seconds)
        if first_approved and not task_id
        else claim_agent_task_for_worker(task_id=str(task_id), worker_id=worker_id, lease_seconds=lease_seconds)
    )
    if not claim.success or claim.task is None:
        return AgentWorkerBranchPlanResult(
            success=False,
            task_id=claim.task_id,
            worker_id=worker_id,
            lease_token=claim.lease_token,
            source_branch=source_branch,
            final_status=claim.task.status if claim.task else None,
            reason=claim.reason,
        )

    task = claim.task
    lease_token = claim.lease_token
    planned_branch = _planned_branch_for_task(task)
    validation_error = validate_branch_plan(task)
    if validation_error:
        _audit(
            "agent_worker.branch_plan_refused",
            task.id,
            {"worker_id": worker_id, "lease_token": lease_token, "reason": validation_error},
            actor=worker_id,
        )
        _release_task(task, lease_token, worker_id)
        store.persist()
        return AgentWorkerBranchPlanResult(
            success=False,
            task_id=task.id,
            worker_id=worker_id,
            lease_token=lease_token,
            repository=task.repository,
            planned_branch=planned_branch,
            source_branch=source_branch,
            final_status=task.status,
            reason=validation_error,
        )

    event = _append_branch_plan_event(task, worker_id, lease_token, planned_branch, source_branch)
    _audit(
        "agent_worker.branch_plan_dry_run_completed",
        task.id,
        {
            "worker_id": worker_id,
            "lease_token": lease_token,
            "repository": task.repository,
            "planned_branch": planned_branch,
            "source_branch": source_branch,
            "branch_created": False,
            "repository_mutated": False,
        },
        actor=worker_id,
    )
    _release_task(task, lease_token, worker_id)
    store.persist()

    return AgentWorkerBranchPlanResult(
        success=True,
        task_id=task.id,
        worker_id=worker_id,
        lease_token=lease_token,
        repository=task.repository,
        planned_branch=planned_branch,
        source_branch=source_branch,
        event_id=event.id,
        final_status=task.status,
        reason="Branch creation dry-run plan completed. No repository mutation performed.",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plan a safe worker branch without creating it.")
    parser.add_argument("--task-id", default=None, help="Specific AgentTask id to plan against.")
    parser.add_argument("--first-approved", action="store_true", help="Use the first approved task with a linked approval.")
    parser.add_argument("--worker-id", default="agent-worker-branch-plan", help="Worker id written into task events.")
    parser.add_argument("--lease-seconds", type=int, default=300, help="Claim lease duration in seconds.")
    parser.add_argument("--source-branch", default="main", help="Source branch the real worker would branch from.")
    parser.add_argument("--json", action="store_true", help="Print JSON result.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    result = run_agent_worker_branch_plan_dry_run(
        task_id=args.task_id,
        first_approved=args.first_approved,
        worker_id=args.worker_id,
        lease_seconds=args.lease_seconds,
        source_branch=args.source_branch,
    )
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        decision = "PASS" if result.success else "REFUSED"
        print(f"Agent worker branch plan dry-run: {decision}")
        print(f"Task: {result.task_id or 'n/a'}")
        print(f"Repository: {result.repository or 'n/a'}")
        print(f"Planned branch: {result.planned_branch or 'n/a'}")
        print(f"Reason: {result.reason}")
    return 0 if result.success else 1
