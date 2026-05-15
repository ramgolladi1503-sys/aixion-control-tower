from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .agent_task_models import AgentTask, AgentTaskEvent, AgentTaskEventType, AgentTaskStatus
from .agent_worker_claims import claim_agent_task_for_worker, claim_first_approved_agent_task_for_worker
from .agent_worker_validation_plan import (
    allowed_prefix_for_command,
    validate_validation_plan,
    validation_policy_metadata,
)
from .models import ApprovalRequest, AuditEvent
from .store import store

DEFAULT_TIMEOUT_SECONDS = 120
MAX_OUTPUT_CHARS = 4000


@dataclass
class CommandExecutionResult:
    command: str
    exit_code: int
    timed_out: bool = False
    output_summary: str = ""
    duration_ms: int = 0
    output_truncated: bool = False
    output_chars: int = 0
    allowed_prefix: str = "UNKNOWN"

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass
class AgentWorkerValidationRunResult:
    success: bool
    task_id: str | None = None
    approval_request_id: str | None = None
    worker_id: str = "agent-worker-validation-runner"
    lease_token: str | None = None
    command_count: int = 0
    results: list[CommandExecutionResult] = field(default_factory=list)
    started_event_id: str | None = None
    finished_event_id: str | None = None
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


def _summarize_output(output: str) -> tuple[str, bool, int]:
    cleaned = output.strip()
    output_chars = len(cleaned)
    if output_chars <= MAX_OUTPUT_CHARS:
        return cleaned, False, output_chars
    return cleaned[-MAX_OUTPUT_CHARS:], True, output_chars


def _normalize_result_artifact(result: CommandExecutionResult) -> CommandExecutionResult:
    if not result.allowed_prefix or result.allowed_prefix == "UNKNOWN":
        result.allowed_prefix = allowed_prefix_for_command(result.command)
    if result.output_chars == 0 and result.output_summary:
        result.output_chars = len(result.output_summary)
    if len(result.output_summary) > MAX_OUTPUT_CHARS:
        result.output_summary = result.output_summary[-MAX_OUTPUT_CHARS:]
        result.output_truncated = True
    return result


def execute_validation_command(
    command: str,
    *,
    cwd: Path,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> CommandExecutionResult:
    args = shlex.split(command)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        combined_output = "\n".join(part for part in [completed.stdout, completed.stderr] if part)
        summary, truncated, output_chars = _summarize_output(combined_output)
        return CommandExecutionResult(
            command=command,
            exit_code=completed.returncode,
            timed_out=False,
            output_summary=summary,
            duration_ms=duration_ms,
            output_truncated=truncated,
            output_chars=output_chars,
            allowed_prefix=allowed_prefix_for_command(command),
        )
    except subprocess.TimeoutExpired as error:
        duration_ms = int((time.monotonic() - started) * 1000)
        output = "\n".join(
            part.decode("utf-8", errors="replace") if isinstance(part, bytes) else part
            for part in [error.stdout, error.stderr]
            if part
        )
        summary, truncated, output_chars = _summarize_output(output or f"Command timed out after {timeout_seconds}s.")
        return CommandExecutionResult(
            command=command,
            exit_code=124,
            timed_out=True,
            output_summary=summary,
            duration_ms=duration_ms,
            output_truncated=truncated,
            output_chars=output_chars,
            allowed_prefix=allowed_prefix_for_command(command),
        )


def _artifact_summary(results: list[CommandExecutionResult]) -> dict[str, Any]:
    return {
        "artifact_type": "validation_command_artifact_summary",
        "command_count": len(results),
        "passed_count": sum(1 for result in results if result.passed),
        "failed_count": sum(1 for result in results if not result.passed),
        "timed_out_count": sum(1 for result in results if result.timed_out),
        "total_duration_ms": sum(max(result.duration_ms, 0) for result in results),
        "output_truncated": any(result.output_truncated for result in results),
        "max_output_chars": MAX_OUTPUT_CHARS,
    }


def _append_validation_event(
    task: AgentTask,
    approval: ApprovalRequest,
    worker_id: str,
    lease_token: str | None,
    event_type: AgentTaskEventType,
    status: AgentTaskStatus,
    message: str,
    results: list[CommandExecutionResult] | None = None,
) -> AgentTaskEvent:
    task.status = status
    task.updated_at = _now()
    normalized_results = [_normalize_result_artifact(result) for result in results] if results is not None else []
    event = AgentTaskEvent(
        task_id=task.id,
        event_type=event_type,
        message=message,
        status=status,
        actor=worker_id,
        metadata={
            "worker_id": worker_id,
            "lease_token": lease_token,
            "approval_request_id": approval.id,
            "command_count": len(normalized_results) if results is not None else len(approval.test_plan),
            "commands_executed": results is not None,
            "results": [asdict(result) for result in normalized_results],
            "artifact_summary": _artifact_summary(normalized_results) if results is not None else {},
            "validation_policy": validation_policy_metadata(),
            "runner_type": "validation_command_runner",
        },
    )
    store.agent_task_events[event.id] = event
    return event


def run_agent_worker_validation_commands(
    *,
    task_id: str | None = None,
    first_approved: bool = False,
    worker_id: str = "agent-worker-validation-runner",
    lease_seconds: int = 300,
    cwd: Path | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    executor: Callable[[str], CommandExecutionResult] | None = None,
) -> AgentWorkerValidationRunResult:
    if not task_id and not first_approved:
        return AgentWorkerValidationRunResult(
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
        return AgentWorkerValidationRunResult(
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
    validation_error = validate_validation_plan(approval)
    if validation_error:
        _audit(
            "agent_worker.validation_run_refused",
            task.id,
            {"worker_id": worker_id, "lease_token": lease_token, "reason": validation_error, "validation_policy": validation_policy_metadata()},
            actor=worker_id,
        )
        _release_task(task, lease_token, worker_id)
        store.persist()
        return AgentWorkerValidationRunResult(
            success=False,
            task_id=task.id,
            approval_request_id=task.approval_request_id,
            worker_id=worker_id,
            lease_token=lease_token,
            final_status=task.status,
            reason=validation_error,
        )

    assert approval is not None
    run_cwd = cwd or Path.cwd()
    started = _append_validation_event(
        task,
        approval,
        worker_id,
        lease_token,
        AgentTaskEventType.TESTS_STARTED,
        AgentTaskStatus.TESTING,
        "Worker started allowlisted validation commands.",
        results=None,
    )
    _audit(
        "agent_worker.validation_run_started",
        task.id,
        {"worker_id": worker_id, "lease_token": lease_token, "approval_request_id": approval.id, "validation_policy": validation_policy_metadata()},
        actor=worker_id,
    )

    results: list[CommandExecutionResult] = []
    for command in approval.test_plan:
        runner = executor or (lambda cmd: execute_validation_command(cmd, cwd=run_cwd, timeout_seconds=timeout_seconds))
        result = _normalize_result_artifact(runner(command))
        results.append(result)
        if not result.passed:
            break

    all_passed = all(result.passed for result in results) and len(results) == len(approval.test_plan)
    finished_event_type = AgentTaskEventType.TESTS_PASSED if all_passed else AgentTaskEventType.TESTS_FAILED
    finished_status = AgentTaskStatus.APPROVED if all_passed else AgentTaskStatus.FAILED
    finished_message = "Worker validation commands passed." if all_passed else "Worker validation commands failed."
    finished = _append_validation_event(
        task,
        approval,
        worker_id,
        lease_token,
        finished_event_type,
        finished_status,
        finished_message,
        results=results,
    )
    _audit(
        "agent_worker.validation_run_completed",
        task.id,
        {
            "worker_id": worker_id,
            "lease_token": lease_token,
            "approval_request_id": approval.id,
            "passed": all_passed,
            "command_count": len(results),
            "artifact_summary": _artifact_summary(results),
        },
        actor=worker_id,
    )
    _release_task(task, lease_token, worker_id)
    store.persist()

    return AgentWorkerValidationRunResult(
        success=all_passed,
        task_id=task.id,
        approval_request_id=approval.id,
        worker_id=worker_id,
        lease_token=lease_token,
        command_count=len(results),
        results=results,
        started_event_id=started.id,
        finished_event_id=finished.id,
        final_status=task.status,
        reason="Validation commands passed." if all_passed else "Validation commands failed.",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run allowlisted validation commands for an approved AgentTask.")
    parser.add_argument("--task-id", default=None, help="Specific AgentTask id to validate.")
    parser.add_argument("--first-approved", action="store_true", help="Use the first approved task with a linked approval.")
    parser.add_argument("--worker-id", default="agent-worker-validation-runner", help="Worker id written into task events.")
    parser.add_argument("--lease-seconds", type=int, default=300, help="Claim lease duration in seconds.")
    parser.add_argument("--cwd", default=".", help="Working directory for command execution.")
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Timeout per command.")
    parser.add_argument("--json", action="store_true", help="Print JSON result.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    result = run_agent_worker_validation_commands(
        task_id=args.task_id,
        first_approved=args.first_approved,
        worker_id=args.worker_id,
        lease_seconds=args.lease_seconds,
        cwd=Path(args.cwd),
        timeout_seconds=args.timeout_seconds,
    )
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        decision = "PASS" if result.success else "FAILED"
        print(f"Agent worker validation runner: {decision}")
        print(f"Task: {result.task_id or 'n/a'}")
        print(f"Approval: {result.approval_request_id or 'n/a'}")
        print(f"Command count: {result.command_count}")
        print(f"Reason: {result.reason}")
    return 0 if result.success else 1
