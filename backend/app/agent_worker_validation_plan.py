from __future__ import annotations

import argparse
import json
import shlex
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .agent_task_models import AgentTask, AgentTaskEvent, AgentTaskEventType
from .agent_worker_claims import claim_agent_task_for_worker, claim_first_approved_agent_task_for_worker
from .models import ApprovalRequest, AuditEvent
from .store import store

ALLOWED_COMMAND_PREFIXES = (
    "python -m pytest",
    "pytest",
    "./gradlew test",
    "./gradlew testDebugUnitTest",
    "./gradlew assembleDebug",
    "npm test",
    "npm run test",
    "npm run lint",
    "pnpm test",
    "pnpm lint",
    "ruff check",
    "mypy",
)
DANGEROUS_TOKENS = {"&&", "||", ";", "|", ">", ">>", "<", "`"}
DANGEROUS_SUBSTRINGS = ("$(", "rm -rf", "curl ", "wget ", "sudo ", "ssh ", "scp ", "chmod ", "chown ", "mkfs", ":(){")
MAX_VALIDATION_COMMANDS = 12
MAX_COMMAND_LENGTH = 220


@dataclass(frozen=True)
class ValidationExecutionPolicy:
    policy_version: str = "aixion-validation-execution-policy-v1"
    allowed_command_prefixes: tuple[str, ...] = ALLOWED_COMMAND_PREFIXES
    dangerous_tokens: tuple[str, ...] = tuple(sorted(DANGEROUS_TOKENS))
    dangerous_substrings: tuple[str, ...] = DANGEROUS_SUBSTRINGS
    max_validation_commands: int = MAX_VALIDATION_COMMANDS
    max_command_length: int = MAX_COMMAND_LENGTH
    shell_execution_allowed: bool = False
    network_fetch_commands_allowed: bool = False


@dataclass
class ValidationCommandPlanItem:
    command: str
    allowed_prefix: str
    policy_version: str = "aixion-validation-execution-policy-v1"


@dataclass
class AgentWorkerValidationPlanResult:
    success: bool
    task_id: str | None = None
    approval_request_id: str | None = None
    worker_id: str = "agent-worker-validation-plan"
    lease_token: str | None = None
    command_count: int = 0
    commands: list[ValidationCommandPlanItem] = field(default_factory=list)
    event_id: str | None = None
    final_status: str | None = None
    reason: str = ""
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["generated_at"] = self.generated_at.isoformat()
        return payload


def get_validation_execution_policy() -> ValidationExecutionPolicy:
    return ValidationExecutionPolicy()


def validation_policy_metadata(policy: ValidationExecutionPolicy | None = None) -> dict[str, Any]:
    return asdict(policy or get_validation_execution_policy())


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


def validate_validation_command(command: str, policy: ValidationExecutionPolicy | None = None) -> str | None:
    active_policy = policy or get_validation_execution_policy()
    clean = command.strip()
    if not clean:
        return "Validation command is empty."
    if len(clean) > active_policy.max_command_length:
        return f"Validation command is too long: {clean[:80]}"
    if any(substring in clean for substring in active_policy.dangerous_substrings):
        return f"Validation command contains dangerous operation: {clean}"
    try:
        tokens = shlex.split(clean)
    except ValueError as error:
        return f"Validation command cannot be parsed: {error}"
    if any(token in set(active_policy.dangerous_tokens) for token in tokens):
        return f"Validation command contains shell control token: {clean}"
    for prefix in active_policy.allowed_command_prefixes:
        if clean == prefix or clean.startswith(f"{prefix} "):
            return None
    return f"Validation command is not allowlisted: {clean}"


def validate_validation_plan(approval: ApprovalRequest | None, policy: ValidationExecutionPolicy | None = None) -> str | None:
    active_policy = policy or get_validation_execution_policy()
    if approval is None:
        return "Linked approval request not found."
    if not approval.test_plan:
        return "Linked approval has no validation commands."
    if len(approval.test_plan) > active_policy.max_validation_commands:
        return f"Too many validation commands: {len(approval.test_plan)} > {active_policy.max_validation_commands}"
    seen_commands: set[str] = set()
    for command in approval.test_plan:
        normalized = command.strip()
        if normalized in seen_commands:
            return f"Duplicate validation command: {normalized}"
        seen_commands.add(normalized)
        failure = validate_validation_command(command, policy=active_policy)
        if failure:
            return failure
    return None


def _allowed_prefix_for(command: str, policy: ValidationExecutionPolicy | None = None) -> str:
    active_policy = policy or get_validation_execution_policy()
    clean = command.strip()
    for prefix in active_policy.allowed_command_prefixes:
        if clean == prefix or clean.startswith(f"{prefix} "):
            return prefix
    return "UNKNOWN"


def allowed_prefix_for_command(command: str) -> str:
    return _allowed_prefix_for(command)


def _plan_items(approval: ApprovalRequest, policy: ValidationExecutionPolicy | None = None) -> list[ValidationCommandPlanItem]:
    active_policy = policy or get_validation_execution_policy()
    return [
        ValidationCommandPlanItem(
            command=command.strip(),
            allowed_prefix=_allowed_prefix_for(command, active_policy),
            policy_version=active_policy.policy_version,
        )
        for command in approval.test_plan
    ]


def _append_validation_plan_event(
    task: AgentTask,
    approval: ApprovalRequest,
    worker_id: str,
    lease_token: str | None,
    items: list[ValidationCommandPlanItem],
    policy: ValidationExecutionPolicy,
) -> AgentTaskEvent:
    event = AgentTaskEvent(
        task_id=task.id,
        event_type=AgentTaskEventType.TESTS_STARTED,
        message="Worker dry-run planned validation commands. No commands were executed.",
        status=task.status,
        actor=worker_id,
        metadata={
            "dry_run": True,
            "worker_id": worker_id,
            "lease_token": lease_token,
            "approval_request_id": approval.id,
            "command_count": len(items),
            "commands": [asdict(item) for item in items],
            "commands_executed": False,
            "plan_type": "validation_command_dry_run",
            "validation_policy": validation_policy_metadata(policy),
        },
    )
    store.agent_task_events[event.id] = event
    return event


def run_agent_worker_validation_plan_dry_run(
    *,
    task_id: str | None = None,
    first_approved: bool = False,
    worker_id: str = "agent-worker-validation-plan",
    lease_seconds: int = 300,
) -> AgentWorkerValidationPlanResult:
    if not task_id and not first_approved:
        return AgentWorkerValidationPlanResult(
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
        return AgentWorkerValidationPlanResult(
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
    policy = get_validation_execution_policy()
    validation_error = validate_validation_plan(approval, policy=policy)
    if validation_error:
        _audit(
            "agent_worker.validation_plan_refused",
            task.id,
            {"worker_id": worker_id, "lease_token": lease_token, "reason": validation_error, "validation_policy": validation_policy_metadata(policy)},
            actor=worker_id,
        )
        _release_task(task, lease_token, worker_id)
        store.persist()
        return AgentWorkerValidationPlanResult(
            success=False,
            task_id=task.id,
            approval_request_id=task.approval_request_id,
            worker_id=worker_id,
            lease_token=lease_token,
            final_status=task.status,
            reason=validation_error,
        )

    assert approval is not None
    items = _plan_items(approval, policy=policy)
    event = _append_validation_plan_event(task, approval, worker_id, lease_token, items, policy)
    _audit(
        "agent_worker.validation_plan_dry_run_completed",
        task.id,
        {
            "worker_id": worker_id,
            "lease_token": lease_token,
            "approval_request_id": approval.id,
            "command_count": len(items),
            "commands_executed": False,
            "validation_policy": validation_policy_metadata(policy),
        },
        actor=worker_id,
    )
    _release_task(task, lease_token, worker_id)
    store.persist()

    return AgentWorkerValidationPlanResult(
        success=True,
        task_id=task.id,
        approval_request_id=approval.id,
        worker_id=worker_id,
        lease_token=lease_token,
        command_count=len(items),
        commands=items,
        event_id=event.id,
        final_status=task.status,
        reason="Validation command dry-run plan completed. No commands executed.",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plan validation commands without executing them.")
    parser.add_argument("--task-id", default=None, help="Specific AgentTask id to plan against.")
    parser.add_argument("--first-approved", action="store_true", help="Use the first approved task with a linked approval.")
    parser.add_argument("--worker-id", default="agent-worker-validation-plan", help="Worker id written into task events.")
    parser.add_argument("--lease-seconds", type=int, default=300, help="Claim lease duration in seconds.")
    parser.add_argument("--json", action="store_true", help="Print JSON result.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    result = run_agent_worker_validation_plan_dry_run(
        task_id=args.task_id,
        first_approved=args.first_approved,
        worker_id=args.worker_id,
        lease_seconds=args.lease_seconds,
    )
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        decision = "PASS" if result.success else "REFUSED"
        print(f"Agent worker validation dry-run: {decision}")
        print(f"Task: {result.task_id or 'n/a'}")
        print(f"Approval: {result.approval_request_id or 'n/a'}")
        print(f"Command count: {result.command_count}")
        print(f"Reason: {result.reason}")
    return 0 if result.success else 1
