from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from .agent_task_models import AgentTask, AgentTaskEvent, AgentTaskEventType
from .agent_worker_claims import claim_agent_task_for_worker, claim_first_approved_agent_task_for_worker
from .models import ApprovalRequest, AuditEvent, FileChange
from .store import store

ALLOWED_CHANGE_TYPES = {"create", "update", "delete"}
BLOCKED_PATH_PARTS = {".git", ".github/workflows", "node_modules", "venv", ".venv", "__pycache__"}
BLOCKED_FILENAMES = {".env", ".env.local", ".env.production", "credentials.py", "secrets.json"}
MAX_FILE_CHANGES = 25
MAX_NEW_CONTENT_BYTES = 250_000


@dataclass
class FilePatchPlanItem:
    path: str
    change_type: str
    has_new_content: bool
    new_content_bytes: int = 0


@dataclass
class AgentWorkerFilePlanResult:
    success: bool
    task_id: str | None = None
    approval_request_id: str | None = None
    worker_id: str = "agent-worker-file-plan"
    lease_token: str | None = None
    file_count: int = 0
    files: list[FilePatchPlanItem] = field(default_factory=list)
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


def _linked_approval(task: AgentTask) -> ApprovalRequest | None:
    if not task.approval_request_id:
        return None
    return store.approval_requests.get(task.approval_request_id)


def _is_safe_relative_path(path: str) -> bool:
    clean = path.strip()
    if not clean or clean.startswith("/") or "\x00" in clean:
        return False
    posix_path = PurePosixPath(clean)
    if any(part in {"", ".", ".."} for part in posix_path.parts):
        return False
    if clean.endswith("/"):
        return False
    path_lower = clean.lower()
    if any(path_lower == blocked or path_lower.startswith(f"{blocked}/") for blocked in BLOCKED_PATH_PARTS):
        return False
    if PurePosixPath(path_lower).name in BLOCKED_FILENAMES:
        return False
    return True


def validate_file_change(change: FileChange) -> str | None:
    change_type = change.change_type.lower().strip()
    if change_type not in ALLOWED_CHANGE_TYPES:
        return f"Unsupported file change type for {change.path}: {change.change_type}"
    if not _is_safe_relative_path(change.path):
        return f"Unsafe file path: {change.path}"
    if change_type in {"create", "update"} and change.new_content is None:
        return f"File change requires new_content for {change.path}"
    if change_type == "delete" and change.new_content not in (None, ""):
        return f"Delete change must not include new_content for {change.path}"
    if change.new_content is not None and len(change.new_content.encode("utf-8")) > MAX_NEW_CONTENT_BYTES:
        return f"File change is too large for {change.path}"
    return None


def validate_file_patch_plan(approval: ApprovalRequest | None) -> str | None:
    if approval is None:
        return "Linked approval request not found."
    if not approval.files:
        return "Linked approval has no file changes."
    if len(approval.files) > MAX_FILE_CHANGES:
        return f"Too many file changes: {len(approval.files)} > {MAX_FILE_CHANGES}"
    seen_paths: set[str] = set()
    for change in approval.files:
        normalized_path = change.path.strip()
        if normalized_path in seen_paths:
            return f"Duplicate file change path: {normalized_path}"
        seen_paths.add(normalized_path)
        failure = validate_file_change(change)
        if failure:
            return failure
    return None


def _plan_items(approval: ApprovalRequest) -> list[FilePatchPlanItem]:
    items: list[FilePatchPlanItem] = []
    for change in approval.files:
        new_content_bytes = len(change.new_content.encode("utf-8")) if change.new_content is not None else 0
        items.append(
            FilePatchPlanItem(
                path=change.path.strip(),
                change_type=change.change_type.lower().strip(),
                has_new_content=change.new_content is not None,
                new_content_bytes=new_content_bytes,
            )
        )
    return items


def _append_file_plan_event(
    task: AgentTask,
    approval: ApprovalRequest,
    worker_id: str,
    lease_token: str | None,
    items: list[FilePatchPlanItem],
) -> AgentTaskEvent:
    event = AgentTaskEvent(
        task_id=task.id,
        event_type=AgentTaskEventType.NOTE,
        message="Worker dry-run planned safe file patches. No repository files were changed.",
        status=task.status,
        actor=worker_id,
        metadata={
            "dry_run": True,
            "worker_id": worker_id,
            "lease_token": lease_token,
            "approval_request_id": approval.id,
            "file_count": len(items),
            "files": [asdict(item) for item in items],
            "repository_mutated": False,
            "files_written": False,
            "plan_type": "file_patch_dry_run",
        },
    )
    store.agent_task_events[event.id] = event
    return event


def run_agent_worker_file_plan_dry_run(
    *,
    task_id: str | None = None,
    first_approved: bool = False,
    worker_id: str = "agent-worker-file-plan",
    lease_seconds: int = 300,
) -> AgentWorkerFilePlanResult:
    if not task_id and not first_approved:
        return AgentWorkerFilePlanResult(
            success=False,
            worker_id=worker_id,
            reason="Provide task_id or set first_approved=true.",
        )

    claim = (
        claim_first_approved_agent_task_for_worker(worker_id=worker_id, lease_seconds=lease_seconds)
        if first_approved and not task_id
        else claim_agent_task_for_worker(task_id=str(task_id), worker_id=worker_id, lease_seconds=lease_seconds)
    )
    if not claim.success or claim.task is None:
        return AgentWorkerFilePlanResult(
            success=False,
            task_id=claim.task_id,
            worker_id=worker_id,
            lease_token=claim.lease_token,
            final_status=claim.task.status if claim.task else None,
            reason=claim.reason,
        )

    task = claim.task
    lease_token = claim.lease_token
    approval = _linked_approval(task)
    validation_error = validate_file_patch_plan(approval)
    if validation_error:
        _audit(
            "agent_worker.file_plan_refused",
            task.id,
            {"worker_id": worker_id, "lease_token": lease_token, "reason": validation_error},
            actor=worker_id,
        )
        _release_task(task, lease_token, worker_id)
        store.persist()
        return AgentWorkerFilePlanResult(
            success=False,
            task_id=task.id,
            approval_request_id=task.approval_request_id,
            worker_id=worker_id,
            lease_token=lease_token,
            final_status=task.status,
            reason=validation_error,
        )

    assert approval is not None
    items = _plan_items(approval)
    event = _append_file_plan_event(task, approval, worker_id, lease_token, items)
    _audit(
        "agent_worker.file_plan_dry_run_completed",
        task.id,
        {
            "worker_id": worker_id,
            "lease_token": lease_token,
            "approval_request_id": approval.id,
            "file_count": len(items),
            "repository_mutated": False,
            "files_written": False,
        },
        actor=worker_id,
    )
    _release_task(task, lease_token, worker_id)
    store.persist()

    return AgentWorkerFilePlanResult(
        success=True,
        task_id=task.id,
        approval_request_id=approval.id,
        worker_id=worker_id,
        lease_token=lease_token,
        file_count=len(items),
        files=items,
        event_id=event.id,
        final_status=task.status,
        reason="File patch dry-run plan completed. No repository mutation performed.",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plan safe worker file patches without writing them.")
    parser.add_argument("--task-id", default=None, help="Specific AgentTask id to plan against.")
    parser.add_argument("--first-approved", action="store_true", help="Use the first approved task with a linked approval.")
    parser.add_argument("--worker-id", default="agent-worker-file-plan", help="Worker id written into task events.")
    parser.add_argument("--lease-seconds", type=int, default=300, help="Claim lease duration in seconds.")
    parser.add_argument("--json", action="store_true", help="Print JSON result.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    result = run_agent_worker_file_plan_dry_run(
        task_id=args.task_id,
        first_approved=args.first_approved,
        worker_id=args.worker_id,
        lease_seconds=args.lease_seconds,
    )
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        decision = "PASS" if result.success else "REFUSED"
        print(f"Agent worker file patch dry-run: {decision}")
        print(f"Task: {result.task_id or 'n/a'}")
        print(f"Approval: {result.approval_request_id or 'n/a'}")
        print(f"File count: {result.file_count}")
        print(f"Reason: {result.reason}")
    return 0 if result.success else 1
