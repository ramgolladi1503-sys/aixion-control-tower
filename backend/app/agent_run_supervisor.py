from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .agent_run_faults import InjectedAgentRunFault, default_fault_reason, raise_if_fault_configured
from .agent_run_creation import build_agent_run_creation_records
from .agent_run_models import (
    DEFAULT_AGENT_RUN_STEPS,
    AgentRun,
    AgentRunCreate,
    AgentRunEvent,
    AgentRunEventType,
    AgentRunStatus,
    AgentRunStep,
    AgentRunStepExecutionResult,
    AgentRunStepStatus,
    AgentRunStepType,
    VerificationDecision,
)
from .agent_run_state_machine import (
    InvalidAgentRunTransition,
    derive_run_status,
    transition_run,
    transition_step,
)
from .agent_run_verifier import (
    canonical_evidence_hash,
    classify_execution_result,
    validate_scope_contract,
)
from .agent_task_models import (
    AgentTask,
    AgentTaskEvent,
    AgentTaskEventType,
    AgentTaskStatus,
)
from .agent_run_repository import (
    AgentRunRepository,
    AgentRunRepositoryConflict,
    PostgresAgentRunRepository,
    SQLiteAgentRunRepository,
)
from .agent_task_repository import (
    AgentTaskRepository,
    PostgresAgentTaskRepository,
    SQLiteAgentTaskRepository,
)
from .approval_repository import (
    ApprovalDecisionRepository,
    PostgresApprovalDecisionRepository,
    SQLiteApprovalDecisionRepository,
)
from .approval_decisions import (
    agent_run_repository,
    agent_task_repository,
    approval_decision_repository,
)
from .approval_integrity import compute_approval_payload_hash
from .agent_worker_container_validation import (
    ContainerRuntime,
    ContainerValidationExecutor,
    ContainerValidationPolicy,
)
from .agent_worker_github_branch import GitHubBranchClient, run_agent_worker_github_branch_creation
from .agent_worker_github_files import GitHubFileClient, run_agent_worker_github_file_application
from .agent_worker_github_pr import GitHubPullRequestClient, run_agent_worker_github_pr_creation
from .agent_worker_validation_runner import run_agent_worker_validation_commands
from .agent_worker_workspace import (
    WorkspaceCommandRunner,
    cleanup_agent_worker_workspace,
    prepare_agent_worker_workspace,
)
from .models import ApprovalRequest, ApprovalStatus, AuditEvent, now_utc
from .store import store
from .workspace_access import agent_run_scope_is_consistent

StepExecutor = Callable[[AgentRun, AgentRunStep, int], AgentRunStepExecutionResult]


class AgentRunConflict(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _run_steps(run_id: str) -> list[AgentRunStep]:
    return sorted(
        (step for step in store.agent_run_steps.values() if step.run_id == run_id),
        key=lambda step: step.sequence,
    )


def _run_events(run_id: str) -> list[AgentRunEvent]:
    return sorted(
        (event for event in store.agent_run_events.values() if event.run_id == run_id),
        key=lambda event: event.created_at,
    )


def _audit(event_type: str, entity_id: str, details: dict, actor: str) -> AuditEvent:
    event = AuditEvent(event_type=event_type, entity_id=entity_id, details=details, actor=actor)
    store.audit_events.append(event)
    return event


def _append_event(
    run: AgentRun,
    event_type: AgentRunEventType,
    *,
    step: AgentRunStep | None = None,
    previous_status: str | None = None,
    new_status: str | None = None,
    message: str = "",
    reason: str = "",
    actor: str = "system",
    metadata: dict | None = None,
    input_evidence_hash: str | None = None,
    output_evidence_hash: str | None = None,
    publish: bool = True,
) -> AgentRunEvent:
    event = AgentRunEvent(
        run_id=run.id,
        task_id=run.task_id,
        step_id=step.id if step else None,
        correlation_id=run.correlation_id,
        event_type=event_type,
        previous_status=previous_status,
        new_status=new_status,
        message=message,
        reason=reason,
        actor=actor,
        attempt_number=step.attempt_count if step else None,
        input_evidence_hash=input_evidence_hash,
        output_evidence_hash=output_evidence_hash,
        metadata=metadata or {},
    )
    if publish:
        store.agent_run_events[event.id] = event
    return event


def create_agent_run(
    payload: AgentRunCreate,
    *,
    actor: str = "system",
    task_repository: AgentTaskRepository | None = None,
    approval_repository: ApprovalDecisionRepository | None = None,
    run_repository: AgentRunRepository | None = None,
) -> AgentRun:
    active_task_repository = task_repository or agent_task_repository
    active_approval_repository = approval_repository or approval_decision_repository
    active_run_repository = run_repository or agent_run_repository
    sqlite_adapters = (
        isinstance(active_task_repository, SQLiteAgentTaskRepository),
        isinstance(active_approval_repository, SQLiteApprovalDecisionRepository),
        isinstance(active_run_repository, SQLiteAgentRunRepository),
    )
    if any(sqlite_adapters) and not all(sqlite_adapters):
        raise AgentRunConflict("AgentTask, approval, and AgentRun repositories must use one store")

    task = active_task_repository.get(payload.task_id)
    if task is None:
        raise ValueError("Agent task not found.")
    if task.status != AgentTaskStatus.APPROVED:
        raise AgentRunConflict(
            f"Agent task must be APPROVED before a run is created; got {task.status}."
        )
    if not task.approval_request_id:
        raise AgentRunConflict("A linked APPROVED approval is required before a run is created.")
    approval = active_approval_repository.get(task.approval_request_id)
    if approval is None or approval.status != ApprovalStatus.APPROVED:
        raise AgentRunConflict("A linked APPROVED approval is required before a run is created.")
    if approval.project_id != task.project_id:
        raise AgentRunConflict("Approved request project does not match the AgentTask project.")
    if (
        approval.workspace_id is not None
        and task.workspace_id is not None
        and approval.workspace_id != task.workspace_id
    ):
        raise AgentRunConflict("Approved request workspace does not match the AgentTask workspace.")
    if (
        not approval.approved_payload_hash
        or approval.approved_payload_hash != compute_approval_payload_hash(approval)
    ):
        raise AgentRunConflict(
            "Approved request payload is missing or fails its frozen hash check."
        )
    existing = active_run_repository.get_active_for_task(task.id)
    if existing is not None:
        return existing

    records = build_agent_run_creation_records(
        task=task,
        approval=approval,
        payload=payload,
        actor=actor,
    )
    try:
        active_run_repository.create(
            run=records.run,
            steps=records.steps,
            events=records.events,
            audit=records.audit,
            scheduled_audit=records.scheduled_audit,
        )
    except AgentRunRepositoryConflict as error:
        existing = active_run_repository.get_active_for_task(task.id)
        if existing is not None:
            return existing
        raise AgentRunConflict(str(error)) from error
    if isinstance(active_run_repository, SQLiteAgentRunRepository):
        store.project_agent_run_creation(records.run, records.steps, records.events, records.audit)
        if records.scheduled_audit is not None:
            store.audit_events.append(records.scheduled_audit)
    return records.run


def acquire_run_lease(
    run: AgentRun,
    *,
    worker_id: str,
    lease_seconds: int,
    run_repository: AgentRunRepository | None = None,
) -> str:
    active_repository = run_repository or agent_run_repository
    now = _now()
    try:
        updated, event, audit = active_repository.acquire_lease(
            run_id=run.id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            now=now,
        )
    except AgentRunRepositoryConflict as error:
        raise AgentRunConflict(str(error)) from error
    for field_name in AgentRun.model_fields:
        setattr(run, field_name, getattr(updated, field_name))
    if isinstance(active_repository, SQLiteAgentRunRepository):
        if event is not None and audit is not None:
            store.project_agent_run_lease(run, event, audit)
    if not run.lease_token:
        raise AgentRunConflict("Durable AgentRun lease result is missing its token")
    return run.lease_token


def heartbeat_run(
    run: AgentRun,
    *,
    worker_id: str,
    lease_token: str,
    lease_seconds: int,
    run_repository: AgentRunRepository | None = None,
) -> AgentRun:
    active_repository = run_repository or agent_run_repository
    try:
        updated_run, updated_step, event, audit = active_repository.update_lease(
            run_id=run.id,
            worker_id=worker_id,
            lease_token=lease_token,
            lease_seconds=lease_seconds,
            now=_now(),
        )
    except (AgentRunRepositoryConflict, ValueError) as error:
        raise AgentRunConflict(f"Run heartbeat rejected: {error}") from error
    if isinstance(active_repository, SQLiteAgentRunRepository):
        store.project_agent_run_lease(updated_run, event, audit, step=updated_step)
        return store.agent_runs[run.id]
    return updated_run


def release_run_lease(
    run: AgentRun,
    *,
    worker_id: str,
    lease_token: str | None,
    run_repository: AgentRunRepository | None = None,
) -> AgentRun:
    if not lease_token:
        return run
    active_repository = run_repository or agent_run_repository
    try:
        updated_run, updated_step, event, audit = active_repository.update_lease(
            run_id=run.id,
            worker_id=worker_id,
            lease_token=lease_token,
            lease_seconds=None,
            now=_now(),
        )
    except AgentRunRepositoryConflict:
        # A newer owner or the stale-lease recovery path already superseded it.
        return run
    for field_name in AgentRun.model_fields:
        setattr(run, field_name, getattr(updated_run, field_name))
    if isinstance(active_repository, SQLiteAgentRunRepository):
        store.project_agent_run_lease(updated_run, event, audit, step=updated_step)
        return store.agent_runs[run.id]
    return updated_run


def _ready_step(
    run: AgentRun,
    *,
    pending_events: list[AgentRunEvent] | None = None,
) -> AgentRunStep | None:
    now = _now()
    for step in _run_steps(run.id):
        if (
            step.status == AgentRunStepStatus.RETRY_WAIT
            and step.next_retry_at
            and step.next_retry_at <= now
        ):
            previous, new = transition_step(
                step, AgentRunStepStatus.READY, reason="Retry backoff elapsed."
            )
            ready_event = _append_event(
                run,
                AgentRunEventType.STEP_READY,
                step=step,
                previous_status=previous.value,
                new_status=new.value,
                message="Retry backoff elapsed; step is ready again.",
                publish=pending_events is None,
            )
            if pending_events is not None:
                pending_events.append(ready_event)
        if step.status == AgentRunStepStatus.READY:
            return step
        if step.status not in {AgentRunStepStatus.SUCCEEDED, AgentRunStepStatus.SKIPPED}:
            return None
    return None


def _next_pending_step(run: AgentRun, completed: AgentRunStep) -> AgentRunStep | None:
    for step in _run_steps(run.id):
        if step.sequence > completed.sequence and step.status == AgentRunStepStatus.PENDING:
            previous, new = transition_step(step, AgentRunStepStatus.READY)
            run.current_step_id = step.id
            run.current_step_index = step.sequence
            _append_event(
                run,
                AgentRunEventType.STEP_READY,
                step=step,
                previous_status=previous.value,
                new_status=new.value,
                message="Previous step passed; next step is ready.",
            )
            return step
    return None


def _classify_injected_fault(error: InjectedAgentRunFault) -> AgentRunStepExecutionResult:
    reason = error.fault.message or default_fault_reason(error.fault.fault_kind)
    return classify_execution_result(
        success=False,
        reason=reason,
        evidence={"fault_kind": error.fault.fault_kind.value},
    )


def _apply_execution_result(
    run: AgentRun,
    step: AgentRunStep,
    result: AgentRunStepExecutionResult,
    *,
    actor: str,
) -> None:
    step.decision = result.decision
    step.reason = result.reason
    step.evidence = result.evidence
    step.evidence_hash = canonical_evidence_hash(result.evidence)
    step.output_reference = result.output_reference
    step.updated_at = _now()

    if result.decision == VerificationDecision.PASS:
        previous, new = transition_step(step, AgentRunStepStatus.SUCCEEDED, reason=result.reason)
        _append_event(
            run,
            AgentRunEventType.STEP_SUCCEEDED,
            step=step,
            previous_status=previous.value,
            new_status=new.value,
            message=result.reason,
            actor=actor,
            output_evidence_hash=step.evidence_hash,
            metadata={
                "decision": result.decision.value,
                "output_reference": result.output_reference,
            },
        )
        if _next_pending_step(run, step) is None:
            previous_run, new_run = transition_run(run, AgentRunStatus.SUCCEEDED)
            _append_event(
                run,
                AgentRunEventType.RUN_STATE_CHANGED,
                previous_status=previous_run.value,
                new_status=new_run.value,
                message="Every Mission Control step passed.",
                actor=actor,
            )
        else:
            run.status = AgentRunStatus.RUNNING
            run.updated_at = _now()
        return

    if result.decision == VerificationDecision.RETRY_TRANSIENT:
        if step.attempt_count < step.max_attempts:
            previous, new = transition_step(
                step, AgentRunStepStatus.RETRY_WAIT, reason=result.reason
            )
            delay_seconds = min(300, 2 ** max(1, step.attempt_count))
            step.next_retry_at = _now() + timedelta(seconds=delay_seconds)
            previous_run, new_run = transition_run(
                run, AgentRunStatus.RETRY_WAIT, reason=result.reason
            )
            _append_event(
                run,
                AgentRunEventType.STEP_RETRY_SCHEDULED,
                step=step,
                previous_status=previous.value,
                new_status=new.value,
                message="Transient failure scheduled for bounded retry.",
                reason=result.reason,
                actor=actor,
                output_evidence_hash=step.evidence_hash,
                metadata={
                    "decision": result.decision.value,
                    "next_retry_at": step.next_retry_at.isoformat(),
                    "run_previous_status": previous_run.value,
                    "run_new_status": new_run.value,
                },
            )
        else:
            previous, new = transition_step(
                step,
                AgentRunStepStatus.NEEDS_HUMAN,
                reason="Retry budget exhausted. " + result.reason,
            )
            previous_run, new_run = transition_run(
                run,
                AgentRunStatus.NEEDS_HUMAN,
                reason=result.reason,
            )
            _append_event(
                run,
                AgentRunEventType.STEP_NEEDS_HUMAN,
                step=step,
                previous_status=previous.value,
                new_status=new.value,
                message="Retry budget exhausted; operator action is required.",
                reason=result.reason,
                actor=actor,
                metadata={
                    "run_previous_status": previous_run.value,
                    "run_new_status": new_run.value,
                },
            )
        return

    if result.decision in {
        VerificationDecision.NEEDS_REVISION,
        VerificationDecision.NEEDS_HUMAN,
    }:
        previous, new = transition_step(
            step,
            AgentRunStepStatus.NEEDS_HUMAN,
            reason=result.reason,
        )
        previous_run, new_run = transition_run(
            run,
            AgentRunStatus.NEEDS_HUMAN,
            reason=result.reason,
        )
        _append_event(
            run,
            AgentRunEventType.STEP_NEEDS_HUMAN,
            step=step,
            previous_status=previous.value,
            new_status=new.value,
            message="Execution requires an operator decision or revised plan.",
            reason=result.reason,
            actor=actor,
            metadata={
                "decision": result.decision.value,
                "run_previous_status": previous_run.value,
                "run_new_status": new_run.value,
            },
        )
        return

    if result.decision == VerificationDecision.BLOCK_POLICY:
        previous, new = transition_step(
            step,
            AgentRunStepStatus.BLOCKED,
            reason=result.reason,
        )
        previous_run, new_run = transition_run(
            run,
            AgentRunStatus.BLOCKED,
            reason=result.reason,
        )
        _append_event(
            run,
            AgentRunEventType.STEP_BLOCKED,
            step=step,
            previous_status=previous.value,
            new_status=new.value,
            message="Policy violation blocked the run permanently.",
            reason=result.reason,
            actor=actor,
            metadata={
                "run_previous_status": previous_run.value,
                "run_new_status": new_run.value,
            },
        )
        return

    previous, new = transition_step(step, AgentRunStepStatus.FAILED, reason=result.reason)
    previous_run, new_run = transition_run(
        run,
        AgentRunStatus.FAILED,
        reason=result.reason,
    )
    _append_event(
        run,
        AgentRunEventType.STEP_FAILED,
        step=step,
        previous_status=previous.value,
        new_status=new.value,
        message="Execution failed permanently.",
        reason=result.reason,
        actor=actor,
        metadata={
            "run_previous_status": previous_run.value,
            "run_new_status": new_run.value,
        },
    )


def _build_postgres_scope_validation_records(
    *,
    task: AgentTask,
    approval: ApprovalRequest,
    run: AgentRun,
    steps: list[AgentRunStep],
    worker_id: str,
    lease_token: str,
) -> tuple[AgentRun, list[AgentRunStep], list[AgentRunEvent], AuditEvent]:
    """Build scope validation output, then use the shared pure completion builder."""
    return _build_postgres_agent_run_step_completion_records(
        run=run,
        steps=steps,
        step_type=AgentRunStepType.VALIDATE_SCOPE,
        result=validate_scope_contract(task, approval),
        worker_id=worker_id,
        lease_token=lease_token,
    )


def _build_postgres_agent_run_step_completion_records(
    *,
    run: AgentRun,
    steps: list[AgentRunStep],
    step_type: AgentRunStepType,
    result: AgentRunStepExecutionResult,
    worker_id: str,
    lease_token: str,
    already_running: bool = False,
) -> tuple[AgentRun, list[AgentRunStep], list[AgentRunEvent], AuditEvent]:
    """Build governed-step completion records without touching process state."""
    updated_run = run.model_copy(deep=True)
    updated_steps = [item.model_copy(deep=True) for item in steps]
    current = next((item for item in updated_steps if item.id == run.current_step_id), None)
    if (
        current is None
        or current.step_type != step_type
        or current.status
        != (AgentRunStepStatus.RUNNING if already_running else AgentRunStepStatus.READY)
        or run.lease_owner != worker_id
        or run.lease_token != lease_token
        or (
            already_running
            and (current.lease_owner != worker_id or current.lease_token != lease_token)
        )
    ):
        raise AgentRunRepositoryConflict("Scope validation snapshot is no longer executable.")

    events: list[AgentRunEvent] = []

    def append(
        event_type: AgentRunEventType,
        *,
        step: AgentRunStep | None = None,
        previous_status: str | None = None,
        new_status: str | None = None,
        message: str,
        reason: str = "",
        input_hash: str | None = None,
        output_hash: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        events.append(
            AgentRunEvent(
                run_id=updated_run.id,
                task_id=updated_run.task_id,
                step_id=step.id if step else None,
                correlation_id=updated_run.correlation_id,
                event_type=event_type,
                previous_status=previous_status,
                new_status=new_status,
                message=message,
                reason=reason,
                actor=worker_id,
                attempt_number=step.attempt_count if step else None,
                input_evidence_hash=input_hash,
                output_evidence_hash=output_hash,
                metadata=metadata or {},
            )
        )

    if not already_running and updated_run.status in {
        AgentRunStatus.SCHEDULED,
        AgentRunStatus.RETRY_WAIT,
    }:
        previous_run, new_run = transition_run(updated_run, AgentRunStatus.RUNNING)
        append(
            AgentRunEventType.RUN_STATE_CHANGED,
            previous_status=previous_run.value,
            new_status=new_run.value,
            message="Mission Control worker started or resumed execution.",
        )

    if not already_running:
        previous_step, new_step = transition_step(current, AgentRunStepStatus.RUNNING)
        current.lease_owner = worker_id
        current.lease_token = lease_token
        current.lease_expires_at = updated_run.lease_expires_at
        current.heartbeat_at = _now()
    else:
        previous_step, new_step = AgentRunStepStatus.READY, AgentRunStepStatus.RUNNING
    updated_run.current_step_id = current.id
    updated_run.current_step_index = current.sequence
    if not already_running:
        append(
            AgentRunEventType.STEP_STARTED,
            step=current,
            previous_status=previous_step.value,
            new_status=new_step.value,
            message=f"Executing {current.step_type.value}.",
            input_hash=canonical_evidence_hash(
                {
                    "run_id": updated_run.id,
                    "task_id": updated_run.task_id,
                    "step_type": current.step_type.value,
                    "attempt": current.attempt_count,
                }
            ),
            metadata={"idempotency_key": current.idempotency_key},
        )

    current.decision = result.decision
    current.reason = result.reason
    current.evidence = result.evidence
    current.evidence_hash = canonical_evidence_hash(result.evidence)
    current.output_reference = result.output_reference
    current.updated_at = _now()

    if result.decision == VerificationDecision.PASS:
        previous, new = transition_step(
            current, AgentRunStepStatus.SUCCEEDED, reason=result.reason
        )
        append(
            AgentRunEventType.STEP_SUCCEEDED,
            step=current,
            previous_status=previous.value,
            new_status=new.value,
            message=result.reason,
            output_hash=current.evidence_hash,
            metadata={
                "decision": result.decision.value,
                "output_reference": result.output_reference,
            },
        )
        next_step = next(
            (
                item
                for item in updated_steps
                if item.sequence > current.sequence
                and item.status == AgentRunStepStatus.PENDING
            ),
            None,
        )
        if next_step is None:
            previous_run, new_run = transition_run(updated_run, AgentRunStatus.SUCCEEDED)
            append(
                AgentRunEventType.RUN_STATE_CHANGED,
                previous_status=previous_run.value,
                new_status=new_run.value,
                message="Every Mission Control step passed.",
            )
        else:
            previous, new = transition_step(next_step, AgentRunStepStatus.READY)
            updated_run.current_step_id = next_step.id
            updated_run.current_step_index = next_step.sequence
            updated_run.status = AgentRunStatus.RUNNING
            updated_run.updated_at = _now()
            append(
                AgentRunEventType.STEP_READY,
                step=next_step,
                previous_status=previous.value,
                new_status=new.value,
                message="Previous step passed; next step is ready.",
            )
    elif result.decision == VerificationDecision.BLOCK_POLICY:
        previous, new = transition_step(
            current, AgentRunStepStatus.BLOCKED, reason=result.reason
        )
        previous_run, new_run = transition_run(
            updated_run, AgentRunStatus.BLOCKED, reason=result.reason
        )
        append(
            AgentRunEventType.STEP_BLOCKED,
            step=current,
            previous_status=previous.value,
            new_status=new.value,
            message="Policy violation blocked the run permanently.",
            reason=result.reason,
            metadata={
                "run_previous_status": previous_run.value,
                "run_new_status": new_run.value,
            },
        )
    elif result.decision in {
        VerificationDecision.NEEDS_REVISION,
        VerificationDecision.NEEDS_HUMAN,
    }:
        previous, new = transition_step(
            current, AgentRunStepStatus.NEEDS_HUMAN, reason=result.reason
        )
        previous_run, new_run = transition_run(
            updated_run, AgentRunStatus.NEEDS_HUMAN, reason=result.reason
        )
        append(
            AgentRunEventType.STEP_NEEDS_HUMAN,
            step=current,
            previous_status=previous.value,
            new_status=new.value,
            message="Execution requires an operator decision or revised plan.",
            reason=result.reason,
            metadata={
                "decision": result.decision.value,
                "run_previous_status": previous_run.value,
                "run_new_status": new_run.value,
            },
        )
    elif result.decision == VerificationDecision.RETRY_TRANSIENT:
        if current.attempt_count < current.max_attempts:
            previous, new = transition_step(
                current, AgentRunStepStatus.RETRY_WAIT, reason=result.reason
            )
            delay_seconds = min(300, 2 ** max(1, current.attempt_count))
            current.next_retry_at = _now() + timedelta(seconds=delay_seconds)
            previous_run, new_run = transition_run(
                updated_run, AgentRunStatus.RETRY_WAIT, reason=result.reason
            )
            append(
                AgentRunEventType.STEP_RETRY_SCHEDULED,
                step=current,
                previous_status=previous.value,
                new_status=new.value,
                message="Transient failure scheduled for bounded retry.",
                reason=result.reason,
                output_hash=current.evidence_hash,
                metadata={
                    "decision": result.decision.value,
                    "next_retry_at": current.next_retry_at.isoformat(),
                    "run_previous_status": previous_run.value,
                    "run_new_status": new_run.value,
                },
            )
        else:
            reason = "Retry budget exhausted. " + result.reason
            previous, new = transition_step(
                current, AgentRunStepStatus.NEEDS_HUMAN, reason=reason
            )
            previous_run, new_run = transition_run(
                updated_run, AgentRunStatus.NEEDS_HUMAN, reason=result.reason
            )
            append(
                AgentRunEventType.STEP_NEEDS_HUMAN,
                step=current,
                previous_status=previous.value,
                new_status=new.value,
                message="Retry budget exhausted; operator action is required.",
                reason=result.reason,
                metadata={
                    "run_previous_status": previous_run.value,
                    "run_new_status": new_run.value,
                },
            )
    else:
        previous, new = transition_step(
            current, AgentRunStepStatus.FAILED, reason=result.reason
        )
        previous_run, new_run = transition_run(
            updated_run, AgentRunStatus.FAILED, reason=result.reason
        )
        append(
            AgentRunEventType.STEP_FAILED,
            step=current,
            previous_status=previous.value,
            new_status=new.value,
            message="Execution failed permanently.",
            reason=result.reason,
            metadata={
                "run_previous_status": previous_run.value,
                "run_new_status": new_run.value,
            },
        )

    # Any non-terminal result leaves the run active, but this worker's lease
    # ends with this atomic step completion.
    if updated_run.status not in {
        AgentRunStatus.BLOCKED,
        AgentRunStatus.SUCCEEDED,
        AgentRunStatus.FAILED,
        AgentRunStatus.CANCELLED,
    }:
        updated_run.lease_owner = None
        updated_run.lease_token = None
        updated_run.lease_expires_at = None
    updated_run.updated_at = _now()
    audit = AuditEvent(
        event_type="agent_run.step_completed",
        entity_id=updated_run.id,
        workspace_id=updated_run.workspace_id,
        actor=worker_id,
        details={
            "task_id": updated_run.task_id,
            "step_id": current.id,
            "step_type": current.step_type,
            "decision": result.decision,
            "run_status": updated_run.status,
            "attempt_count": current.attempt_count,
            "evidence_hash": current.evidence_hash,
        },
    )
    return updated_run, updated_steps, events, audit


def execute_next_step(
    run_id: str,
    *,
    worker_id: str,
    lease_seconds: int = 300,
    timeout_seconds: int = 120,
    executor: StepExecutor | None = None,
    task_repository: AgentTaskRepository | None = None,
    approval_repository: ApprovalDecisionRepository | None = None,
    run_repository: AgentRunRepository | None = None,
) -> AgentRunStepExecutionResult:
    active_run_repository = run_repository
    if active_run_repository is not None and not isinstance(
        active_run_repository,
        (SQLiteAgentRunRepository, PostgresAgentRunRepository),
    ):
        raise AgentRunConflict("AgentRun execution is unavailable for the configured repository.")
    if active_run_repository is None:
        run = store.agent_runs.get(run_id)
        if run is None:
            raise ValueError("Agent run not found.")
        active_run_repository = agent_run_repository
    else:
        run = active_run_repository.get(run_id)
        if run is None:
            raise ValueError("Agent run not found.")
        run_steps = active_run_repository.list_steps_for_run(run_id)
        run_events = active_run_repository.list_events_for_run(run_id)
        store.project_agent_run_snapshot(run, run_steps, run_events)
    postgres_repository_execution = isinstance(active_run_repository, PostgresAgentRunRepository)
    if postgres_repository_execution:
        current_step = store.agent_run_steps.get(run.current_step_id or "")
        if current_step is None or current_step.step_type not in {
            AgentRunStepType.VALIDATE_SCOPE,
            AgentRunStepType.VERIFY_RESULT,
            AgentRunStepType.SEAL_EVIDENCE,
        }:
            raise AgentRunConflict(
                "PostgreSQL AgentRun execution currently supports only the "
                "VALIDATE_SCOPE, VERIFY_RESULT, and SEAL_EVIDENCE steps."
            )
        if current_step.step_type in {
            AgentRunStepType.VALIDATE_SCOPE,
            AgentRunStepType.VERIFY_RESULT,
        } and (
            not isinstance(task_repository, PostgresAgentTaskRepository)
            or not isinstance(approval_repository, PostgresApprovalDecisionRepository)
        ):
            raise AgentRunConflict(
                "PostgreSQL scope validation and result verification require "
                "repository-bound task and approval reads."
            )
        if current_step.step_type == AgentRunStepType.VERIFY_RESULT and [
            step.step_type for step in active_run_repository.list_steps_for_run(run.id)
        ] != list(DEFAULT_AGENT_RUN_STEPS):
            raise AgentRunConflict(
                "PostgreSQL VERIFY_RESULT requires the complete canonical AgentRun step plan."
            )
        if current_step.step_type == AgentRunStepType.SEAL_EVIDENCE:
            durable_steps = active_run_repository.list_steps_for_run(run.id)
            if (
                [item.step_type for item in durable_steps] != list(DEFAULT_AGENT_RUN_STEPS)
                or durable_steps[-1].id != current_step.id
                or any(
                    item.status != AgentRunStepStatus.SUCCEEDED
                    for item in durable_steps[:-1]
                )
            ):
                raise AgentRunConflict(
                    "PostgreSQL SEAL_EVIDENCE requires the canonical step plan "
                    "with every prior step succeeded."
                )
    if run.status in {
        AgentRunStatus.BLOCKED,
        AgentRunStatus.SUCCEEDED,
        AgentRunStatus.FAILED,
        AgentRunStatus.CANCELLED,
    }:
        raise AgentRunConflict(f"Terminal run cannot execute: {run.status}.")
    if run.pause_requested or run.status == AgentRunStatus.PAUSED:
        raise AgentRunConflict("Paused run cannot execute until resumed.")
    if run.cancel_requested:
        cancel_agent_run(run, actor=worker_id, reason="Cancellation was already requested.")
        raise AgentRunConflict("Cancelled run cannot execute.")

    lease_token = acquire_run_lease(
        run,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        run_repository=active_run_repository,
    )
    if postgres_repository_execution:
        # Lease acquisition may have waited behind another process. Refresh the
        # durable run and step snapshot before deciding what code may execute.
        durable_run = active_run_repository.get(run_id)
        if durable_run is None:
            raise AgentRunConflict("AgentRun disappeared after its lease was acquired.")
        store.project_agent_run_snapshot(
            durable_run,
            active_run_repository.list_steps_for_run(run_id),
            active_run_repository.list_events_for_run(run_id),
        )
        run = store.agent_runs[run_id]
    retry_ready_events: list[AgentRunEvent] = []
    step = _ready_step(run, pending_events=retry_ready_events)
    if step is None:
        release_run_lease(
            run,
            worker_id=worker_id,
            lease_token=lease_token,
            run_repository=active_run_repository,
        )
        if run.status == AgentRunStatus.RETRY_WAIT:
            return AgentRunStepExecutionResult(
                success=False,
                decision=VerificationDecision.RETRY_TRANSIENT,
                reason="No step is ready because retry backoff has not elapsed.",
            )
        raise AgentRunConflict("No executable step is ready.")
    if postgres_repository_execution and step.step_type not in {
        AgentRunStepType.VALIDATE_SCOPE,
        AgentRunStepType.VERIFY_RESULT,
        AgentRunStepType.SEAL_EVIDENCE,
    }:
        release_run_lease(
            run,
            worker_id=worker_id,
            lease_token=lease_token,
            run_repository=active_run_repository,
        )
        raise AgentRunConflict(
            "PostgreSQL AgentRun execution currently supports only the "
            "VALIDATE_SCOPE, VERIFY_RESULT, and SEAL_EVIDENCE steps."
        )

    if (
        postgres_repository_execution
        and step.step_type == AgentRunStepType.VERIFY_RESULT
        and (
            [item.step_type for item in active_run_repository.list_steps_for_run(run.id)]
            != list(DEFAULT_AGENT_RUN_STEPS)
            or any(
                item.sequence < step.sequence and item.status != AgentRunStepStatus.SUCCEEDED
                for item in active_run_repository.list_steps_for_run(run.id)
            )
        )
    ):
        release_run_lease(
            run,
            worker_id=worker_id,
            lease_token=lease_token,
            run_repository=active_run_repository,
        )
        raise AgentRunConflict(
            "PostgreSQL VERIFY_RESULT requires every preceding AgentRun step to have succeeded."
        )

    if postgres_repository_execution and step.step_type == AgentRunStepType.SEAL_EVIDENCE:
        durable_steps = active_run_repository.list_steps_for_run(run.id)
        if (
            [item.step_type for item in durable_steps] != list(DEFAULT_AGENT_RUN_STEPS)
            or durable_steps[-1].id != step.id
            or any(
                item.status != AgentRunStepStatus.SUCCEEDED for item in durable_steps[:-1]
            )
        ):
            release_run_lease(
                run,
                worker_id=worker_id,
                lease_token=lease_token,
                run_repository=active_run_repository,
            )
            raise AgentRunConflict(
                "PostgreSQL SEAL_EVIDENCE requires the canonical step plan "
                "with every prior step succeeded."
            )

    if (
        postgres_repository_execution
        and step.step_type == AgentRunStepType.VALIDATE_SCOPE
        and isinstance(task_repository, PostgresAgentTaskRepository)
        and isinstance(approval_repository, PostgresApprovalDecisionRepository)
    ):
        # Scope authority and completion must be one PostgreSQL unit. The
        # repository locks and reads the current task/approval before asking
        # this pure builder to produce the completion records.
        durable_run = active_run_repository.get(run.id)
        durable_steps = active_run_repository.list_steps_for_run(run.id)
        if durable_run is None:
            release_run_lease(
                run,
                worker_id=worker_id,
                lease_token=lease_token,
                run_repository=active_run_repository,
            )
            raise AgentRunConflict("AgentRun disappeared before scope validation.")

        def build_scope_records(
            task: AgentTask,
            approval: ApprovalRequest,
            locked_run: AgentRun,
            locked_steps: list[AgentRunStep],
        ) -> tuple[AgentRun, list[AgentRunStep], list[AgentRunEvent], AuditEvent]:
            if locked_run != durable_run or locked_steps != durable_steps:
                raise AgentRunRepositoryConflict(
                    "AgentRun changed while preparing scope validation."
                )
            return _build_postgres_scope_validation_records(
                task=task,
                approval=approval,
                run=locked_run,
                steps=locked_steps,
                worker_id=worker_id,
                lease_token=lease_token,
            )

        try:
            updated_run, updated_steps, events, audit = active_run_repository.commit_scope_validation(
                expected_run=durable_run,
                expected_steps=durable_steps,
                worker_id=worker_id,
                lease_token=lease_token,
                build_records=build_scope_records,
            )
        except Exception as error:
            # The completion transaction rolls back as a unit. Release only
            # this worker's still-current lease; a replacement lease is never
            # touched because update_lease validates the exact token.
            release_run_lease(
                run,
                worker_id=worker_id,
                lease_token=lease_token,
                run_repository=active_run_repository,
            )
            raise AgentRunConflict(
                "PostgreSQL scope validation could not be committed."
            ) from error
        store.project_agent_run_control_transition(
            updated_run,
            updated_steps,
            events,
            [audit],
        )
        completed_step = next(item for item in updated_steps if item.id == step.id)
        return AgentRunStepExecutionResult(
            success=completed_step.status == AgentRunStepStatus.SUCCEEDED,
            decision=completed_step.decision or VerificationDecision.BLOCK_POLICY,
            reason=completed_step.reason,
            evidence=completed_step.evidence,
            output_reference=completed_step.output_reference,
        )

    if (
        postgres_repository_execution
        and step.step_type == AgentRunStepType.VERIFY_RESULT
        and isinstance(task_repository, PostgresAgentTaskRepository)
        and isinstance(approval_repository, PostgresApprovalDecisionRepository)
    ):
        durable_run = active_run_repository.get(run.id)
        durable_steps = active_run_repository.list_steps_for_run(run.id)
        if durable_run is None:
            release_run_lease(
                run,
                worker_id=worker_id,
                lease_token=lease_token,
                run_repository=active_run_repository,
            )
            raise AgentRunConflict("AgentRun disappeared before result verification.")

        def build_result_records(
            task: AgentTask,
            approval: ApprovalRequest,
            task_events: list[AgentTaskEvent],
            locked_run: AgentRun,
            locked_steps: list[AgentRunStep],
        ) -> tuple[AgentRun, list[AgentRunStep], list[AgentRunEvent], AuditEvent]:
            if locked_run != durable_run or locked_steps != durable_steps:
                raise AgentRunRepositoryConflict(
                    "AgentRun changed while preparing result verification."
                )
            result = _verify_postgres_agent_run_result(
                run=locked_run,
                task=task,
                approval=approval,
                events=task_events,
                task_repository=task_repository,
                approval_repository=approval_repository,
            )
            return _build_postgres_agent_run_step_completion_records(
                run=locked_run,
                steps=locked_steps,
                step_type=AgentRunStepType.VERIFY_RESULT,
                result=result,
                worker_id=worker_id,
                lease_token=lease_token,
            )

        try:
            updated_run, updated_steps, events, audit = (
                active_run_repository.commit_authoritative_task_step(
                    expected_run=durable_run,
                    expected_steps=durable_steps,
                    worker_id=worker_id,
                    lease_token=lease_token,
                    step_type=AgentRunStepType.VERIFY_RESULT,
                    build_records=build_result_records,
                )
            )
        except Exception as error:
            release_run_lease(
                run,
                worker_id=worker_id,
                lease_token=lease_token,
                run_repository=active_run_repository,
            )
            raise AgentRunConflict(
                "PostgreSQL result verification could not be committed."
            ) from error
        store.project_agent_run_control_transition(
            updated_run,
            updated_steps,
            events,
            [audit],
        )
        completed_step = next(item for item in updated_steps if item.id == step.id)
        return AgentRunStepExecutionResult(
            success=completed_step.status == AgentRunStepStatus.SUCCEEDED,
            decision=completed_step.decision or VerificationDecision.FAIL_PERMANENT,
            reason=completed_step.reason,
            evidence=completed_step.evidence,
            output_reference=completed_step.output_reference,
        )

    durable_run_after_ready = active_run_repository.get(run.id)
    durable_steps_after_ready = active_run_repository.list_steps_for_run(run.id)
    durable_steps_by_id = {item.id: item for item in durable_steps_after_ready}
    changed_ready_steps = [
        step for step in _run_steps(run.id) if durable_steps_by_id.get(step.id) != step
    ]
    if changed_ready_steps:
        if durable_run_after_ready is None or durable_run_after_ready != run:
            release_run_lease(
                run,
                worker_id=worker_id,
                lease_token=lease_token,
                run_repository=active_run_repository,
            )
            raise AgentRunConflict("Run changed before retry readiness could be committed.")
        ready_events = retry_ready_events
        ready_audit = AuditEvent(
            event_type="agent_run.retry_ready",
            entity_id=run.id,
            workspace_id=run.workspace_id,
            actor=worker_id,
            details={
                "step_ids": [item.id for item in changed_ready_steps],
                "event_ids": [event.id for event in ready_events],
            },
        )
        try:
            active_run_repository.commit_control_transition(
                expected_run=durable_run_after_ready,
                updated_run=durable_run_after_ready,
                expected_steps=durable_steps_after_ready,
                updated_steps=[item.model_copy(deep=True) for item in _run_steps(run.id)],
                events=ready_events,
                audits=[ready_audit],
            )
        except Exception as error:
            release_run_lease(
                run,
                worker_id=worker_id,
                lease_token=lease_token,
                run_repository=active_run_repository,
            )
            raise AgentRunConflict("Retry readiness could not be committed.") from error
        store.project_agent_run_control_transition(
            run,
            [item.model_copy(deep=True) for item in _run_steps(run.id)],
            ready_events,
            [ready_audit],
        )

    if step.status == AgentRunStepStatus.SUCCEEDED:
        _append_event(
            run,
            AgentRunEventType.DUPLICATE_DELIVERY_IGNORED,
            step=step,
            message="Duplicate step delivery ignored because the step already succeeded.",
            actor=worker_id,
        )
        release_run_lease(run, worker_id=worker_id, lease_token=lease_token)
        return AgentRunStepExecutionResult(
            success=True,
            decision=VerificationDecision.PASS,
            reason="Duplicate delivery ignored; verified step result already exists.",
            evidence=step.evidence,
            output_reference=step.output_reference,
        )

    checkpoint_expected_run = active_run_repository.get(run.id)
    if checkpoint_expected_run is None or checkpoint_expected_run != run:
        release_run_lease(
            run,
            worker_id=worker_id,
            lease_token=lease_token,
            run_repository=active_run_repository,
        )
        raise AgentRunConflict("Run changed before the RUNNING checkpoint.")
    checkpoint_expected_steps = active_run_repository.list_steps_for_run(run.id)
    previous, new = transition_step(step, AgentRunStepStatus.RUNNING)
    step.lease_owner = worker_id
    step.lease_token = lease_token
    step.lease_expires_at = run.lease_expires_at
    step.heartbeat_at = _now()
    run.current_step_id = step.id
    run.current_step_index = step.sequence
    checkpoint_events: list[AgentRunEvent] = []
    if run.status in {AgentRunStatus.SCHEDULED, AgentRunStatus.RETRY_WAIT}:
        previous_run, new_run = transition_run(run, AgentRunStatus.RUNNING)
        checkpoint_events.append(
            _append_event(
                run,
                AgentRunEventType.RUN_STATE_CHANGED,
                previous_status=previous_run.value,
                new_status=new_run.value,
                message="Mission Control worker started or resumed execution.",
                actor=worker_id,
                publish=False,
            )
        )
    checkpoint_events.append(
        _append_event(
            run,
            AgentRunEventType.STEP_STARTED,
            step=step,
            previous_status=previous.value,
            new_status=new.value,
            message=f"Executing {step.step_type.value}.",
            actor=worker_id,
            input_evidence_hash=canonical_evidence_hash(
                {
                    "run_id": run.id,
                    "task_id": run.task_id,
                    "step_type": step.step_type.value,
                    "attempt": step.attempt_count,
                }
            ),
            metadata={"idempotency_key": step.idempotency_key},
            publish=False,
        )
    )

    # Commit the RUNNING boundary before invoking external work. This is a
    # compare-and-swap transaction over the run, every step, its checkpoint
    # events, and the audit record; a whole-store rewrite could overwrite
    # unrelated concurrent state.
    updated_run = run.model_copy(deep=True)
    expected_steps = checkpoint_expected_steps
    updated_steps = [item.model_copy(deep=True) for item in _run_steps(run.id)]
    checkpoint_audit = AuditEvent(
        event_type="agent_run.step_started",
        entity_id=run.id,
        workspace_id=run.workspace_id,
        actor=worker_id,
        details={
            "step_id": step.id,
            "event_ids": [event.id for event in checkpoint_events],
            "lease_owner": worker_id,
        },
    )
    try:
        active_run_repository.commit_control_transition(
            expected_run=checkpoint_expected_run,
            updated_run=updated_run,
            expected_steps=expected_steps,
            updated_steps=updated_steps,
            events=checkpoint_events,
            audits=[checkpoint_audit],
        )
    except Exception as error:
        # The lease was acquired in its own committed transaction. If the
        # RUNNING checkpoint fails, release it and restore the in-memory view;
        # never call the executor without a durable checkpoint.
        for field_name in AgentRun.model_fields:
            setattr(run, field_name, getattr(checkpoint_expected_run, field_name))
        for snapshot in expected_steps:
            current = store.agent_run_steps[snapshot.id]
            for field_name in AgentRunStep.model_fields:
                setattr(current, field_name, getattr(snapshot, field_name))
        release_run_lease(
            run,
            worker_id=worker_id,
            lease_token=lease_token,
            run_repository=active_run_repository,
        )
        raise AgentRunConflict("AgentRun RUNNING checkpoint could not be committed.") from error
    store.project_agent_run_control_transition(
        run, updated_steps, checkpoint_events, [checkpoint_audit]
    )

    # Completion compares against the committed RUNNING checkpoint. If its
    # transaction fails, that checkpoint remains durable for lease recovery.
    expected_run = run.model_copy(deep=True)
    expected_steps = [item.model_copy(deep=True) for item in _run_steps(run.id)]
    persisted_event_ids = {event.id for event in _run_events(run.id)}

    if (
        postgres_repository_execution
        and step.step_type == AgentRunStepType.SEAL_EVIDENCE
        and isinstance(active_run_repository, PostgresAgentRunRepository)
    ):
        def build_seal_records(
            locked_run: AgentRun,
            locked_steps: list[AgentRunStep],
            locked_events: list[AgentRunEvent],
        ) -> tuple[AgentRun, list[AgentRunStep], list[AgentRunEvent], AuditEvent]:
            if locked_run != expected_run or locked_steps != expected_steps:
                raise AgentRunRepositoryConflict(
                    "AgentRun changed while preparing evidence sealing."
                )
            locked_step = next(
                (item for item in locked_steps if item.id == locked_run.current_step_id),
                None,
            )
            if locked_step is None:
                raise AgentRunRepositoryConflict(
                    "SEAL_EVIDENCE current step is absent from the durable snapshot."
                )
            result = _seal_postgres_agent_run_evidence(
                run=locked_run,
                step=locked_step,
                durable_steps=locked_steps,
                durable_events=locked_events,
            )
            locked_run.final_evidence_hash = str(
                result.evidence.get("final_evidence_hash", "")
            )
            return _build_postgres_agent_run_step_completion_records(
                run=locked_run,
                steps=locked_steps,
                step_type=AgentRunStepType.SEAL_EVIDENCE,
                result=result,
                worker_id=worker_id,
                lease_token=lease_token,
                already_running=True,
            )

        try:
            updated_run, updated_steps, seal_events, seal_audit = (
                active_run_repository.commit_evidence_seal(
                    expected_run=expected_run,
                    expected_steps=expected_steps,
                    worker_id=worker_id,
                    lease_token=lease_token,
                    build_records=build_seal_records,
                )
            )
        except Exception as error:
            raise AgentRunConflict(
                "PostgreSQL SEAL_EVIDENCE completion could not be committed."
            ) from error
        store.project_agent_run_control_transition(
            updated_run,
            updated_steps,
            seal_events,
            [seal_audit],
        )
        completed_step = next(item for item in updated_steps if item.id == step.id)
        return AgentRunStepExecutionResult(
            success=completed_step.status == AgentRunStepStatus.SUCCEEDED,
            decision=completed_step.decision or VerificationDecision.FAIL_PERMANENT,
            reason=completed_step.reason,
            evidence=completed_step.evidence,
            output_reference=completed_step.output_reference,
        )

    def restore_completion_checkpoint() -> None:
        # Keep existing object references coherent while hiding any result that
        # failed to reach durable storage.
        for field_name in AgentRun.model_fields:
            setattr(run, field_name, getattr(expected_run, field_name))
        for snapshot in expected_steps:
            current = store.agent_run_steps[snapshot.id]
            for field_name in AgentRunStep.model_fields:
                setattr(current, field_name, getattr(snapshot, field_name))
        for event_id, event in list(store.agent_run_events.items()):
            if event.run_id == run.id and event_id not in persisted_event_ids:
                del store.agent_run_events[event_id]
        store.audit_events[:] = [
            event for event in store.audit_events if event.id != completion_audit.id
        ]

    try:
        raise_if_fault_configured(run.id, step.step_type)
        effective_executor = executor or DefaultAgentRunStepExecutor(
            task_repository=task_repository,
            approval_repository=approval_repository,
            run_repository=run_repository,
        )
        result = effective_executor(run, step, timeout_seconds)
    except InjectedAgentRunFault as error:
        _append_event(
            run,
            AgentRunEventType.FAULT_INJECTED,
            step=step,
            message="Controlled fault was injected for reliability validation.",
            reason=str(error),
            actor=worker_id,
            metadata={"fault_kind": error.fault.fault_kind.value},
        )
        result = _classify_injected_fault(error)
    except Exception as error:  # noqa: BLE001 - convert worker exceptions to governed outcomes.
        result = classify_execution_result(
            success=False,
            reason=str(error),
            evidence={"exception_type": type(error).__name__},
        )

    _apply_execution_result(run, step, result, actor=worker_id)
    step.lease_owner = None
    step.lease_token = None
    step.lease_expires_at = None
    run.lease_owner = None
    run.lease_token = None
    run.lease_expires_at = None
    run.updated_at = _now()
    completion_audit = _audit(
        "agent_run.step_completed",
        run.id,
        {
            "task_id": run.task_id,
            "step_id": step.id,
            "step_type": step.step_type,
            "decision": result.decision,
            "run_status": run.status,
            "attempt_count": step.attempt_count,
            "evidence_hash": step.evidence_hash,
        },
        actor=worker_id,
    )
    completion_audit.workspace_id = run.workspace_id
    updated_steps = [item.model_copy(deep=True) for item in _run_steps(run.id)]
    if all(
        updated_by_id == expected_by_id
        for updated_by_id, expected_by_id in zip(updated_steps, expected_steps, strict=True)
    ):
        restore_completion_checkpoint()
        raise AgentRunConflict("Step completion produced no durable step change.")
    completion_events = [
        event for event in _run_events(run.id) if event.id not in persisted_event_ids
    ]
    try:
        active_run_repository.commit_step_completion(
            expected_run=expected_run,
            updated_run=run.model_copy(deep=True),
            expected_steps=expected_steps,
            updated_steps=updated_steps,
            events=completion_events,
            audit=completion_audit,
            now=_now(),
        )
    except Exception as error:
        restore_completion_checkpoint()
        raise AgentRunConflict("AgentRun step completion could not be committed.") from error
    return result


def _verify_postgres_agent_run_result(
    *,
    run: AgentRun,
    task: AgentTask,
    approval: ApprovalRequest | None,
    events: list[AgentTaskEvent],
    task_repository: AgentTaskRepository | None,
    approval_repository: ApprovalDecisionRepository | None,
) -> AgentRunStepExecutionResult:
    """Verify repository-backed, run-bound worker evidence for PostgreSQL runs."""
    reasons: list[str] = []
    if not isinstance(task_repository, PostgresAgentTaskRepository) or not isinstance(
        approval_repository, PostgresApprovalDecisionRepository
    ):
        reasons.append("Durable task and approval repositories are unavailable.")
    if (
        not run.workspace_id
        or task.workspace_id != run.workspace_id
        or task.project_id != run.project_id
        or task.approval_request_id != run.approval_request_id
        or task.repository != run.repository
        or task.branch_preference != run.metadata.get("branch_preference")
        or not run.repository
        or not run.metadata.get("branch_preference")
        or not run.approval_request_id
        or approval is None
        or approval.id != run.approval_request_id
        or approval.workspace_id != run.workspace_id
        or approval.project_id != run.project_id
    ):
        reasons.append("Task, approval, project, and run scope do not match.")

    approval_hash_valid = False
    if approval is not None:
        try:
            approval_hash_valid = bool(
                approval.approved_payload_hash
                and approval.approved_payload_hash == compute_approval_payload_hash(approval)
            )
        except (TypeError, ValueError):
            approval_hash_valid = False
    if approval is None or approval.status != ApprovalStatus.APPROVED or not approval_hash_valid:
        reasons.append("The linked approved request is missing, changed, or not approved.")

    validation_worker = f"mission-control:{run.id}:validation"
    validation_events = sorted(
        (
            event
            for event in events
            if event.task_id == task.id
            and event.event_type
            in {
                AgentTaskEventType.TESTS_PASSED,
                AgentTaskEventType.TESTS_FAILED,
            }
            and event.actor == validation_worker
            and event.metadata.get("worker_id") == validation_worker
            and event.metadata.get("approval_request_id") == run.approval_request_id
        ),
        key=lambda event: (event.created_at, event.id),
    )
    validation_event = validation_events[-1] if validation_events else None
    test_plan = list(approval.test_plan) if approval is not None else []
    raw_results = validation_event.metadata.get("results") if validation_event is not None else None
    validation_evidence_valid = bool(
        validation_event is not None
        and validation_event.event_type == AgentTaskEventType.TESTS_PASSED
        and validation_event.status == AgentTaskStatus.APPROVED
        and validation_event.metadata.get("commands_executed") is True
        and isinstance(validation_event.metadata.get("command_count"), int)
        and not isinstance(validation_event.metadata.get("command_count"), bool)
        and validation_event.metadata.get("command_count") == len(test_plan)
        and test_plan
        and isinstance(raw_results, list)
        and len(raw_results) == len(test_plan)
        and all(
            isinstance(result, dict)
            and result.get("command") == command
            and isinstance(result.get("exit_code"), int)
            and not isinstance(result.get("exit_code"), bool)
            and result.get("exit_code") == 0
            and result.get("timed_out") is False
            for result, command in zip(raw_results, test_plan, strict=True)
        )
    )
    if not validation_evidence_valid:
        reasons.append("Run-bound validation evidence is missing or unsuccessful.")

    pr_worker = f"mission-control:{run.id}:pr"
    pr_events = [
        event
        for event in events
        if event.task_id == task.id and event.event_type == AgentTaskEventType.PR_CREATED
    ]
    pr_event = pr_events[0] if len(pr_events) == 1 else None
    pr_metadata = pr_event.metadata if pr_event is not None else {}
    pr_number = pr_metadata.get("pull_request_number")
    pr_url = pr_metadata.get("pull_request_url")
    parsed_url = urlsplit(pr_url) if isinstance(pr_url, str) else None
    expected_path = (
        f"/{run.repository}/pull/{pr_number}"
        if run.repository and isinstance(pr_number, int) and not isinstance(pr_number, bool)
        else None
    )
    pr_evidence_valid = bool(
        len(pr_events) == 1
        and pr_event is not None
        and pr_event.actor == pr_worker
        and pr_metadata.get("worker_id") == pr_worker
        and pr_metadata.get("approval_request_id") == run.approval_request_id
        and pr_metadata.get("repository") == run.repository
        and pr_metadata.get("head_branch") == run.metadata.get("branch_preference")
        and isinstance(pr_number, int)
        and not isinstance(pr_number, bool)
        and pr_number > 0
        and pr_metadata.get("pull_request_opened") is True
        and pr_metadata.get("merged") is False
        and pr_metadata.get("operation_type") == "github_pull_request_creation"
        and parsed_url is not None
        and parsed_url.scheme == "https"
        and parsed_url.hostname == "github.com"
        and parsed_url.netloc == "github.com"
        and parsed_url.path == expected_path
        and not parsed_url.query
        and not parsed_url.fragment
        and pr_event.status == AgentTaskStatus.READY_FOR_PR
        and (validation_event is not None and validation_event.created_at <= pr_event.created_at)
    )
    if not pr_evidence_valid:
        reasons.append("Run-bound, unmerged pull-request evidence is missing or inconsistent.")

    if task.status not in {AgentTaskStatus.READY_FOR_PR, AgentTaskStatus.DONE}:
        reasons.append("Task status does not confirm a ready or completed pull request.")

    evidence = {
        "task_id": task.id,
        "workspace_id": run.workspace_id,
        "project_id": run.project_id,
        "approval_request_id": run.approval_request_id,
        "task_status": task.status.value,
        "approval_hash_valid": approval_hash_valid,
        "validation_event_id": validation_event.id if validation_event else None,
        "validation_passed": validation_evidence_valid,
        "pr_event_id": pr_event.id if pr_event else None,
        "pr_event_count": len(pr_events),
        "pull_request_url": pr_url if pr_evidence_valid else None,
    }
    success = not reasons
    result = classify_execution_result(
        success=success,
        reason=(
            "Run-bound validation and pull-request evidence are consistent."
            if success
            else "Objective incomplete: " + " ".join(reasons)
        ),
        evidence=evidence,
    )
    return result.model_copy(update={"output_reference": pr_url if success else None})


def _seal_postgres_agent_run_evidence(
    *,
    run: AgentRun,
    step: AgentRunStep,
    durable_steps: list[AgentRunStep],
    durable_events: list[AgentRunEvent],
) -> AgentRunStepExecutionResult:
    """Build a seal only from the repository's locked durable snapshot."""
    if (
        [item.step_type for item in durable_steps] != list(DEFAULT_AGENT_RUN_STEPS)
        or [item.sequence for item in durable_steps] != list(range(len(DEFAULT_AGENT_RUN_STEPS)))
        or durable_steps[-1].id != step.id
        or durable_steps[-1] != step
    ):
        raise AgentRunRepositoryConflict(
            "PostgreSQL evidence does not match the canonical AgentRun step plan."
        )

    prior_steps = durable_steps[:-1]
    event_step_ids = {item.id for item in durable_steps}
    event_manifest: list[dict[str, object]] = []
    for event in durable_events:
        if (
            event.run_id != run.id
            or event.task_id != run.task_id
            or event.correlation_id != run.correlation_id
            or (event.step_id is not None and event.step_id not in event_step_ids)
        ):
            raise AgentRunRepositoryConflict(
                "PostgreSQL AgentRun event scope is inconsistent with the run."
            )
        event_manifest.append(
            {
                "event_id": event.id,
                "event_type": event.event_type.value,
                "step_id": event.step_id,
                "previous_status": event.previous_status,
                "new_status": event.new_status,
                "actor": event.actor,
                "attempt_number": event.attempt_number,
                "input_evidence_hash": event.input_evidence_hash,
                "output_evidence_hash": event.output_evidence_hash,
                "metadata_hash": canonical_evidence_hash(event.metadata),
                "reason_hash": canonical_evidence_hash({"reason": event.reason}),
                "created_at": event.created_at.isoformat(),
            }
        )

    for prior_step in prior_steps:
        if (
            prior_step.status != AgentRunStepStatus.SUCCEEDED
            or prior_step.decision != VerificationDecision.PASS
            or not prior_step.evidence_hash
            or prior_step.evidence_hash != canonical_evidence_hash(prior_step.evidence)
        ):
            raise AgentRunRepositoryConflict(
                "A prior PostgreSQL AgentRun step is incomplete or its evidence hash is invalid."
            )
        matching_success_events = [
            event
            for event in durable_events
            if event.step_id == prior_step.id
            and event.event_type == AgentRunEventType.STEP_SUCCEEDED
            and event.attempt_number == prior_step.attempt_count
            and event.output_evidence_hash == prior_step.evidence_hash
        ]
        if len(matching_success_events) != 1:
            raise AgentRunRepositoryConflict(
                "A prior PostgreSQL AgentRun step lacks one matching durable success event."
            )

    evidence_payload: dict[str, object] = {
        "run_id": run.id,
        "workspace_id": run.workspace_id,
        "project_id": run.project_id,
        "task_id": run.task_id,
        "approval_request_id": run.approval_request_id,
        "correlation_id": run.correlation_id,
        "steps": [
            {
                "step_id": item.id,
                "sequence": item.sequence,
                "step_type": item.step_type.value,
                "status": item.status.value,
                "decision": item.decision.value if item.decision else None,
                "attempt_count": item.attempt_count,
                "evidence_hash": item.evidence_hash,
                "output_reference": item.output_reference,
            }
            for item in prior_steps
        ],
        "events": event_manifest,
    }
    final_hash = canonical_evidence_hash(evidence_payload)
    return classify_execution_result(
        success=True,
        reason="Mission Control evidence chain sealed from PostgreSQL records.",
        evidence={**evidence_payload, "final_evidence_hash": final_hash},
    )


class DefaultAgentRunStepExecutor:
    """Adapter from durable Mission Control steps to the existing safe GitHub worker path."""

    def __init__(
        self,
        *,
        branch_client: GitHubBranchClient | None = None,
        file_client: GitHubFileClient | None = None,
        pr_client: GitHubPullRequestClient | None = None,
        workspace_runner: WorkspaceCommandRunner | None = None,
        container_runtime: ContainerRuntime | None = None,
        workspace_parent: Path | None = None,
        task_repository: AgentTaskRepository | None = None,
        approval_repository: ApprovalDecisionRepository | None = None,
        run_repository: AgentRunRepository | None = None,
    ) -> None:
        self.branch_client = branch_client
        self.file_client = file_client
        self.pr_client = pr_client
        self.workspace_runner = workspace_runner
        self.container_runtime = container_runtime
        self.workspace_parent = workspace_parent
        self.task_repository = task_repository
        self.approval_repository = approval_repository
        self.run_repository = run_repository

    def __call__(
        self,
        run: AgentRun,
        step: AgentRunStep,
        timeout_seconds: int,
    ) -> AgentRunStepExecutionResult:
        task = (
            self.task_repository.get(run.task_id)
            if self.task_repository is not None
            else store.agent_tasks.get(run.task_id)
        )
        if task is None:
            return classify_execution_result(
                success=False,
                reason="Agent task not found during execution.",
            )
        approval = (
            self.approval_repository.get(run.approval_request_id or "")
            if self.approval_repository is not None
            else approval_decision_repository.get(run.approval_request_id or "")
        )

        if step.step_type == AgentRunStepType.VALIDATE_SCOPE:
            return validate_scope_contract(task, approval)

        if step.step_type == AgentRunStepType.CREATE_BRANCH:
            result = run_agent_worker_github_branch_creation(
                task_id=task.id,
                worker_id=f"mission-control:{run.id}:branch",
                client=self.branch_client,
                task_repository=self.task_repository,
            )
            return classify_execution_result(
                success=result.success,
                reason=result.reason,
                evidence=result.to_dict(),
                duplicate_safe=False,
            )

        if step.step_type == AgentRunStepType.APPLY_CHANGES:
            result = run_agent_worker_github_file_application(
                task_id=task.id,
                worker_id=f"mission-control:{run.id}:files",
                client=self.file_client,
                task_repository=self.task_repository,
                approval_repository=self.approval_repository,
            )
            return classify_execution_result(
                success=result.success,
                reason=result.reason,
                evidence=result.to_dict(),
                duplicate_safe=False,
            )

        if step.step_type == AgentRunStepType.RUN_VALIDATION:
            branch = task.branch_preference or ""
            workspace = prepare_agent_worker_workspace(
                task=task,
                branch=branch,
                worker_id=f"mission-control:{run.id}:workspace",
                workspace_parent=self.workspace_parent,
                runner=self.workspace_runner,
            )
            if not workspace.success or workspace.repository_path is None:
                return classify_execution_result(
                    success=False,
                    reason=workspace.reason,
                    evidence={
                        "workspace_success": workspace.success,
                        "workspace_isolated": workspace.workspace_root is not None,
                        "workspace_cleaned": workspace.cleaned,
                    },
                )
            try:
                executor = ContainerValidationExecutor(
                    cwd=workspace.repository_path,
                    timeout_seconds=timeout_seconds,
                    policy=ContainerValidationPolicy(),
                    runtime=self.container_runtime,
                )
                result = run_agent_worker_validation_commands(
                    task_id=task.id,
                    worker_id=f"mission-control:{run.id}:validation",
                    cwd=workspace.repository_path,
                    timeout_seconds=timeout_seconds,
                    executor=executor,
                    task_repository=self.task_repository,
                    approval_repository=self.approval_repository,
                )
                evidence = result.to_dict()
                evidence.update(
                    {
                        "workspace_isolated": True,
                        "workspace_path_redacted": True,
                        "containerized": True,
                    }
                )
                return classify_execution_result(
                    success=result.success,
                    reason=result.reason,
                    evidence=evidence,
                )
            finally:
                cleanup_agent_worker_workspace(workspace)

        if step.step_type == AgentRunStepType.CREATE_PULL_REQUEST:
            result = run_agent_worker_github_pr_creation(
                task_id=task.id,
                worker_id=f"mission-control:{run.id}:pr",
                client=self.pr_client,
                task_repository=self.task_repository,
                approval_repository=self.approval_repository,
            )
            classified = classify_execution_result(
                success=result.success,
                reason=result.reason,
                evidence=result.to_dict(),
                duplicate_safe=False,
            )
            return AgentRunStepExecutionResult(
                **classified.model_dump(),
                output_reference=result.pull_request_url,
            )

        if step.step_type == AgentRunStepType.VERIFY_RESULT:
            task_events = (
                self.task_repository.list_events(task.id, workspace_id=task.workspace_id)
                if self.task_repository is not None
                else [
                    event for event in store.agent_task_events.values() if event.task_id == task.id
                ]
            )
            if isinstance(self.run_repository, PostgresAgentRunRepository):
                return _verify_postgres_agent_run_result(
                    run=run,
                    task=task,
                    approval=approval,
                    events=task_events,
                    task_repository=self.task_repository,
                    approval_repository=self.approval_repository,
                )
            pr_events = [event for event in task_events if event.event_type.value == "PR_CREATED"]
            validation_passed = any(
                event.event_type.value == "TESTS_PASSED" for event in task_events
            )
            pr_url = next(
                (
                    event.metadata.get("pull_request_url")
                    for event in reversed(sorted(pr_events, key=lambda event: event.created_at))
                    if event.metadata.get("pull_request_url")
                ),
                None,
            )
            success = (
                validation_passed
                and bool(pr_events)
                and task.status in {AgentTaskStatus.READY_FOR_PR, AgentTaskStatus.DONE}
            )
            reason = (
                "Validation evidence and exactly one pull-request transition are present."
                if success
                else "Objective incomplete: validation or pull-request evidence is missing."
            )
            return classify_execution_result(
                success=success,
                reason=reason,
                evidence={
                    "task_status": task.status,
                    "validation_passed": validation_passed,
                    "pr_event_count": len(pr_events),
                    "pull_request_url": pr_url,
                },
            )

        if step.step_type == AgentRunStepType.SEAL_EVIDENCE:
            if isinstance(self.run_repository, PostgresAgentRunRepository):
                return _seal_postgres_agent_run_evidence(
                    run=run,
                    step=step,
                    durable_steps=self.run_repository.list_steps_for_run(run.id),
                    durable_events=self.run_repository.list_events_for_run(run.id),
                )
            steps = (
                self.run_repository.list_steps_for_run(run.id)
                if self.run_repository is not None
                else _run_steps(run.id)
            )
            run_events = (
                self.run_repository.list_events_for_run(run.id)
                if self.run_repository is not None
                else _run_events(run.id)
            )
            prior_steps = [item for item in steps if item.sequence < step.sequence]
            all_prior_passed = all(
                item.status == AgentRunStepStatus.SUCCEEDED for item in prior_steps
            )
            evidence_payload = {
                "run_id": run.id,
                "task_id": run.task_id,
                "correlation_id": run.correlation_id,
                "steps": [
                    {
                        "sequence": item.sequence,
                        "step_type": item.step_type.value,
                        "status": item.status.value,
                        "decision": item.decision.value if item.decision else None,
                        "attempt_count": item.attempt_count,
                        "evidence_hash": item.evidence_hash,
                        "output_reference": item.output_reference,
                    }
                    for item in prior_steps
                ],
                "event_count": len(run_events),
            }
            final_hash = canonical_evidence_hash(evidence_payload)
            run.final_evidence_hash = final_hash
            return classify_execution_result(
                success=all_prior_passed,
                reason=(
                    "Mission Control evidence chain sealed."
                    if all_prior_passed
                    else "Evidence chain cannot be sealed because a prior step did not pass."
                ),
                evidence={
                    **evidence_payload,
                    "final_evidence_hash": final_hash,
                },
            )

        return classify_execution_result(
            success=False,
            reason=f"Unsupported step type: {step.step_type}",
        )


def recover_stale_leases(
    *,
    actor: str = "mission-control-watchdog",
    workspace_id: str | None = None,
    run_repository: AgentRunRepository | None = None,
) -> int:
    active_repository = run_repository or agent_run_repository
    now = _now()
    recovered = 0
    candidates = active_repository.list_expired_leases(
        now=now,
        workspace_id=workspace_id,
    )
    for expected_run, expected_step in candidates:
        # Keep malformed or cross-scope compatibility data from becoming an
        # authorization or recovery source. Durable repository checks remain
        # authoritative at commit time.
        if isinstance(
            active_repository, SQLiteAgentRunRepository
        ) and not agent_run_scope_is_consistent(expected_run):
            continue
        if workspace_id is not None and expected_run.workspace_id != workspace_id:
            continue

        updated_run = expected_run.model_copy(deep=True)
        expired_owner = expected_run.lease_owner
        updated_run.lease_owner = None
        updated_run.lease_token = None
        updated_run.lease_expires_at = None
        updated_run.heartbeat_at = None
        updated_run.updated_at = now

        updated_step = expected_step.model_copy(deep=True) if expected_step else None
        previous_status = None
        new_status = None
        if updated_step and updated_step.status == AgentRunStepStatus.RUNNING:
            updated_step.lease_owner = None
            updated_step.lease_token = None
            updated_step.lease_expires_at = None
            if updated_step.attempt_count < updated_step.max_attempts:
                previous, new = transition_step(
                    updated_step,
                    AgentRunStepStatus.RETRY_WAIT,
                    reason="Worker lease expired.",
                )
                updated_step.next_retry_at = now
                if updated_run.status == AgentRunStatus.RUNNING:
                    transition_run(
                        updated_run,
                        AgentRunStatus.RETRY_WAIT,
                        reason="Worker lease expired.",
                    )
            else:
                previous, new = transition_step(
                    updated_step,
                    AgentRunStepStatus.NEEDS_HUMAN,
                    reason="Worker lease expired and retry budget is exhausted.",
                )
                if updated_run.status in {
                    AgentRunStatus.RUNNING,
                    AgentRunStatus.RETRY_WAIT,
                }:
                    transition_run(
                        updated_run,
                        AgentRunStatus.NEEDS_HUMAN,
                        reason=updated_step.reason,
                    )
            previous_status = previous.value
            new_status = new.value
            updated_step.updated_at = now
        updated_run.updated_at = now

        event = AgentRunEvent(
            run_id=updated_run.id,
            task_id=updated_run.task_id,
            step_id=updated_step.id if updated_step else None,
            correlation_id=updated_run.correlation_id,
            event_type=AgentRunEventType.STALE_LEASE_RECOVERED,
            previous_status=previous_status,
            new_status=new_status,
            message="Watchdog recovered an expired worker lease without claiming completion.",
            actor=actor,
            metadata={"expired_owner": expired_owner},
        )
        audit_event = AuditEvent(
            event_type="agent_run.stale_lease_recovered",
            entity_id=updated_run.id,
            workspace_id=updated_run.workspace_id,
            actor=actor,
            details={"event_id": event.id, "step_id": event.step_id},
        )
        try:
            active_repository.recover_expired_lease(
                expected_run=expected_run,
                updated_run=updated_run,
                expected_step=expected_step,
                updated_step=updated_step,
                event=event,
                audit=audit_event,
                now=now,
            )
        except AgentRunRepositoryConflict:
            # Another worker changed or recovered this exact lease first.
            continue
        if isinstance(active_repository, SQLiteAgentRunRepository):
            store.project_agent_run_recovery(updated_run, updated_step, event, audit_event)
        recovered += 1
    return recovered


def pause_agent_run(
    run: AgentRun,
    *,
    actor: str,
    reason: str = "",
    run_repository: AgentRunRepository | None = None,
) -> AgentRun:
    active_repository = run_repository or agent_run_repository
    durable_run = active_repository.get(run.id)
    if durable_run is None or durable_run != run:
        raise AgentRunConflict("Run changed; refresh before pausing it.")
    if durable_run.status in {
        AgentRunStatus.BLOCKED,
        AgentRunStatus.SUCCEEDED,
        AgentRunStatus.FAILED,
        AgentRunStatus.CANCELLED,
    }:
        raise AgentRunConflict("Terminal run cannot be paused.")
    if (
        durable_run.lease_owner is not None
        or durable_run.lease_token is not None
        or durable_run.lease_expires_at is not None
    ):
        raise AgentRunConflict("Run cannot be paused while a worker lease is active.")
    expected_run = durable_run
    updated_run = durable_run.model_copy(deep=True)
    expected_steps = active_repository.list_steps_for_run(run.id)
    expected_step = next(
        (step for step in expected_steps if step.id == durable_run.current_step_id),
        None,
    )
    if durable_run.current_step_id is not None and expected_step is None:
        raise AgentRunConflict("Run current step is missing; pause was refused.")
    if expected_step and expected_step.status == AgentRunStepStatus.RUNNING:
        raise AgentRunConflict("Run cannot be paused while a step is running.")
    updated_steps = [step.model_copy(deep=True) for step in expected_steps]
    previous, new = transition_run(updated_run, AgentRunStatus.PAUSED, reason=reason)
    updated_run.pause_requested = True
    event = AgentRunEvent(
        run_id=updated_run.id,
        task_id=updated_run.task_id,
        step_id=updated_run.current_step_id,
        correlation_id=updated_run.correlation_id,
        event_type=AgentRunEventType.OPERATOR_ACTION,
        previous_status=previous.value,
        new_status=new.value,
        message="Operator paused the run.",
        reason=reason,
        actor=actor,
    )
    audit = AuditEvent(
        event_type="agent_run.paused",
        entity_id=updated_run.id,
        workspace_id=updated_run.workspace_id,
        actor=actor,
        details={"event_id": event.id, "reason": reason},
    )
    try:
        active_repository.commit_control_transition(
            expected_run=expected_run,
            updated_run=updated_run,
            expected_steps=expected_steps,
            updated_steps=updated_steps,
            events=[event],
            audits=[audit],
        )
    except AgentRunRepositoryConflict as error:
        raise AgentRunConflict(f"Pause was not committed: {error}") from error
    if isinstance(active_repository, SQLiteAgentRunRepository):
        store.project_agent_run_control_transition(updated_run, updated_steps, [event], [audit])
        return store.agent_runs[run.id]
    return updated_run


def resume_agent_run(
    run: AgentRun,
    *,
    actor: str,
    reason: str = "",
    run_repository: AgentRunRepository | None = None,
) -> AgentRun:
    active_repository = run_repository or agent_run_repository
    durable_run = active_repository.get(run.id)
    if durable_run is None or durable_run != run:
        raise AgentRunConflict("Run changed; refresh before resuming it.")
    if durable_run.status not in {
        AgentRunStatus.PAUSED,
        AgentRunStatus.NEEDS_HUMAN,
        AgentRunStatus.RETRY_WAIT,
    }:
        raise AgentRunConflict(f"Run cannot resume from {durable_run.status}.")
    if (
        durable_run.lease_owner is not None
        or durable_run.lease_token is not None
        or durable_run.lease_expires_at is not None
    ):
        raise AgentRunConflict("Run cannot resume while a worker lease is active.")
    expected_run = durable_run
    updated_run = durable_run.model_copy(deep=True)
    expected_steps = active_repository.list_steps_for_run(run.id)
    expected_step = next(
        (step for step in expected_steps if step.id == durable_run.current_step_id),
        None,
    )
    if durable_run.current_step_id is not None and expected_step is None:
        raise AgentRunConflict("Run current step is missing; resume was refused.")
    updated_steps = [step.model_copy(deep=True) for step in expected_steps]
    events: list[AgentRunEvent] = []
    current_step_index = next(
        (
            index
            for index, step in enumerate(updated_steps)
            if step.id == durable_run.current_step_id
        ),
        None,
    )
    if current_step_index is not None and updated_steps[current_step_index].status in {
        AgentRunStepStatus.PAUSED,
        AgentRunStepStatus.NEEDS_HUMAN,
        AgentRunStepStatus.RETRY_WAIT,
    }:
        step = updated_steps[current_step_index]
        previous_step, new_step = transition_step(
            step,
            AgentRunStepStatus.READY,
            reason=reason or "Operator resumed step.",
        )
        step.next_retry_at = None
        events.append(
            AgentRunEvent(
                run_id=updated_run.id,
                task_id=updated_run.task_id,
                step_id=step.id,
                correlation_id=updated_run.correlation_id,
                event_type=AgentRunEventType.STEP_READY,
                previous_status=previous_step.value,
                new_status=new_step.value,
                message="Operator returned the current step to READY.",
                reason=reason,
                actor=actor,
                attempt_number=step.attempt_count,
            )
        )
    updated_run.pause_requested = False
    previous, new = transition_run(updated_run, AgentRunStatus.SCHEDULED, reason=reason)
    events.append(
        AgentRunEvent(
            run_id=updated_run.id,
            task_id=updated_run.task_id,
            step_id=updated_run.current_step_id,
            correlation_id=updated_run.correlation_id,
            event_type=AgentRunEventType.OPERATOR_ACTION,
            previous_status=previous.value,
            new_status=new.value,
            message="Operator resumed the run.",
            reason=reason,
            actor=actor,
        )
    )
    audit = AuditEvent(
        event_type="agent_run.resumed",
        entity_id=updated_run.id,
        workspace_id=updated_run.workspace_id,
        actor=actor,
        details={"event_ids": [event.id for event in events], "reason": reason},
    )
    try:
        active_repository.commit_control_transition(
            expected_run=expected_run,
            updated_run=updated_run,
            expected_steps=expected_steps,
            updated_steps=updated_steps,
            events=events,
            audits=[audit],
        )
    except AgentRunRepositoryConflict as error:
        raise AgentRunConflict(f"Resume was not committed: {error}") from error
    if isinstance(active_repository, SQLiteAgentRunRepository):
        store.project_agent_run_control_transition(updated_run, updated_steps, events, [audit])
        return store.agent_runs[run.id]
    return updated_run


def cancel_agent_run(
    run: AgentRun,
    *,
    actor: str,
    reason: str = "",
    run_repository: AgentRunRepository | None = None,
) -> AgentRun:
    active_repository = run_repository or agent_run_repository
    durable_run = active_repository.get(run.id)
    if durable_run is None or durable_run != run:
        raise AgentRunConflict("Run changed; refresh before cancelling it.")
    if durable_run.status == AgentRunStatus.CANCELLED:
        return run
    if durable_run.status in {
        AgentRunStatus.BLOCKED,
        AgentRunStatus.SUCCEEDED,
        AgentRunStatus.FAILED,
    }:
        raise AgentRunConflict("Completed terminal run cannot be cancelled.")
    if (
        durable_run.lease_owner is not None
        or durable_run.lease_token is not None
        or durable_run.lease_expires_at is not None
    ):
        raise AgentRunConflict("Run cannot be cancelled while a worker lease is active.")
    expected_run = durable_run
    updated_run = durable_run.model_copy(deep=True)
    expected_steps = active_repository.list_steps_for_run(run.id)
    if any(step.status == AgentRunStepStatus.RUNNING for step in expected_steps):
        raise AgentRunConflict("Run cannot be cancelled while a governed step is running.")
    updated_steps = [step.model_copy(deep=True) for step in expected_steps]
    events: list[AgentRunEvent] = []
    for step in updated_steps:
        if step.status not in {
            AgentRunStepStatus.SUCCEEDED,
            AgentRunStepStatus.BLOCKED,
            AgentRunStepStatus.FAILED,
            AgentRunStepStatus.CANCELLED,
            AgentRunStepStatus.SKIPPED,
        }:
            previous, new = transition_step(
                step,
                AgentRunStepStatus.CANCELLED,
                reason=reason or "Run cancelled.",
            )
            events.append(
                AgentRunEvent(
                    run_id=updated_run.id,
                    task_id=updated_run.task_id,
                    step_id=step.id,
                    correlation_id=updated_run.correlation_id,
                    event_type=AgentRunEventType.STEP_CANCELLED,
                    previous_status=previous.value,
                    new_status=new.value,
                    message="Pending work was cancelled.",
                    reason=reason,
                    actor=actor,
                    attempt_number=step.attempt_count,
                )
            )
    updated_run.cancel_requested = True
    previous, new = transition_run(updated_run, AgentRunStatus.CANCELLED, reason=reason)
    events.append(
        AgentRunEvent(
            run_id=updated_run.id,
            task_id=updated_run.task_id,
            step_id=updated_run.current_step_id,
            correlation_id=updated_run.correlation_id,
            event_type=AgentRunEventType.OPERATOR_ACTION,
            previous_status=previous.value,
            new_status=new.value,
            message="Operator cancelled the run.",
            reason=reason,
            actor=actor,
        )
    )
    audit = AuditEvent(
        event_type="agent_run.cancelled",
        entity_id=updated_run.id,
        workspace_id=updated_run.workspace_id,
        actor=actor,
        details={"event_ids": [event.id for event in events], "reason": reason},
    )
    try:
        active_repository.commit_control_transition(
            expected_run=expected_run,
            updated_run=updated_run,
            expected_steps=expected_steps,
            updated_steps=updated_steps,
            events=events,
            audits=[audit],
        )
    except AgentRunRepositoryConflict as error:
        raise AgentRunConflict(f"Cancellation was not committed: {error}") from error
    if isinstance(active_repository, SQLiteAgentRunRepository):
        store.project_agent_run_control_transition(updated_run, updated_steps, events, [audit])
        return store.agent_runs[run.id]
    return updated_run


def retry_agent_run_step(
    run: AgentRun,
    step: AgentRunStep,
    *,
    actor: str,
    reason: str = "",
    run_repository: AgentRunRepository | None = None,
) -> AgentRunStep:
    active_repository = run_repository or agent_run_repository
    if step.run_id != run.id:
        raise AgentRunConflict("Step does not belong to run.")
    durable_run = active_repository.get(run.id)
    if durable_run is None or durable_run != run:
        raise AgentRunConflict("Run changed; refresh before retrying the step.")
    expected_steps = active_repository.list_steps_for_run(run.id)
    durable_step = next((item for item in expected_steps if item.id == step.id), None)
    if durable_step is None or durable_step != step:
        raise AgentRunConflict("Step changed or no longer belongs to the durable run.")
    if durable_run.status in {
        AgentRunStatus.BLOCKED,
        AgentRunStatus.SUCCEEDED,
        AgentRunStatus.FAILED,
        AgentRunStatus.CANCELLED,
    }:
        raise AgentRunConflict(f"Terminal run cannot be retried: {durable_run.status}.")
    if (
        durable_run.lease_owner is not None
        or durable_run.lease_token is not None
        or durable_run.lease_expires_at is not None
    ):
        raise AgentRunConflict("Run cannot be retried while a worker lease is active.")
    if durable_step.status not in {
        AgentRunStepStatus.RETRY_WAIT,
        AgentRunStepStatus.NEEDS_HUMAN,
        AgentRunStepStatus.FAILED,
    }:
        raise AgentRunConflict(f"Step cannot be retried from {durable_step.status}.")
    if durable_step.attempt_count >= durable_step.max_attempts:
        raise AgentRunConflict("Step retry budget is exhausted.")
    expected_run = durable_run
    updated_run = durable_run.model_copy(deep=True)
    updated_steps = [item.model_copy(deep=True) for item in expected_steps]
    updated_step = next(item for item in updated_steps if item.id == durable_step.id)
    if updated_step.status == AgentRunStepStatus.FAILED:
        updated_step.status = AgentRunStepStatus.NEEDS_HUMAN
    previous, new = transition_step(
        updated_step,
        AgentRunStepStatus.READY,
        reason=reason or "Operator approved retry.",
    )
    updated_step.next_retry_at = None
    updated_run.current_step_id = updated_step.id
    updated_run.current_step_index = updated_step.sequence
    updated_run.pause_requested = False
    if updated_run.status in {
        AgentRunStatus.NEEDS_HUMAN,
        AgentRunStatus.RETRY_WAIT,
        AgentRunStatus.PAUSED,
    }:
        transition_run(updated_run, AgentRunStatus.SCHEDULED, reason=reason)
    event = AgentRunEvent(
        run_id=updated_run.id,
        task_id=updated_run.task_id,
        step_id=updated_step.id,
        correlation_id=updated_run.correlation_id,
        event_type=AgentRunEventType.OPERATOR_ACTION,
        previous_status=previous.value,
        new_status=new.value,
        message="Operator approved a bounded step retry.",
        reason=reason,
        actor=actor,
        attempt_number=updated_step.attempt_count,
    )
    audit = AuditEvent(
        event_type="agent_run.step_retry_requested",
        entity_id=updated_run.id,
        workspace_id=updated_run.workspace_id,
        actor=actor,
        details={"event_id": event.id, "step_id": updated_step.id, "reason": reason},
    )
    try:
        active_repository.commit_control_transition(
            expected_run=expected_run,
            updated_run=updated_run,
            expected_steps=expected_steps,
            updated_steps=updated_steps,
            events=[event],
            audits=[audit],
        )
    except AgentRunRepositoryConflict as error:
        raise AgentRunConflict(f"Step retry was not committed: {error}") from error
    if isinstance(active_repository, SQLiteAgentRunRepository):
        store.project_agent_run_control_transition(updated_run, updated_steps, [event], [audit])
        return store.agent_run_steps[updated_step.id]
    return updated_step


def recompute_run_status(run: AgentRun) -> AgentRunStatus:
    derived = derive_run_status(
        _run_steps(run.id),
        pause_requested=run.pause_requested,
        cancel_requested=run.cancel_requested,
    )
    if derived != run.status:
        try:
            transition_run(run, derived)
        except InvalidAgentRunTransition:
            run.status = derived
            run.updated_at = now_utc()
    return run.status
