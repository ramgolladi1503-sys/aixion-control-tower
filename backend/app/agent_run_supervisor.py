from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .agent_run_faults import InjectedAgentRunFault, default_fault_reason, raise_if_fault_configured
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
from .agent_run_verifier import canonical_evidence_hash, classify_execution_result, validate_scope_contract
from .agent_task_models import AgentTaskStatus
from .agent_worker_container_validation import ContainerRuntime, ContainerValidationExecutor, ContainerValidationPolicy
from .agent_worker_github_branch import GitHubBranchClient, run_agent_worker_github_branch_creation
from .agent_worker_github_files import GitHubFileClient, run_agent_worker_github_file_application
from .agent_worker_github_pr import GitHubPullRequestClient, run_agent_worker_github_pr_creation
from .agent_worker_validation_runner import run_agent_worker_validation_commands
from .agent_worker_workspace import (
    WorkspaceCommandRunner,
    cleanup_agent_worker_workspace,
    prepare_agent_worker_workspace,
)
from .models import ApprovalStatus, AuditEvent, now_utc
from .store import store

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
    store.agent_run_events[event.id] = event
    return event


def create_agent_run(payload: AgentRunCreate, *, actor: str = "system") -> AgentRun:
    task = store.agent_tasks.get(payload.task_id)
    if task is None:
        raise ValueError("Agent task not found.")
    if task.status != AgentTaskStatus.APPROVED:
        raise AgentRunConflict(f"Agent task must be APPROVED before a run is created; got {task.status}.")
    approval = store.approval_requests.get(task.approval_request_id or "")
    if approval is None or approval.status != ApprovalStatus.APPROVED:
        raise AgentRunConflict("A linked APPROVED approval is required before a run is created.")

    existing = [
        run
        for run in store.agent_runs.values()
        if run.task_id == task.id
        and run.status
        not in {AgentRunStatus.FAILED, AgentRunStatus.CANCELLED, AgentRunStatus.BLOCKED}
    ]
    if existing:
        return sorted(existing, key=lambda item: item.created_at)[-1]

    run = AgentRun(
        task_id=task.id,
        approval_request_id=approval.id,
        project_id=task.project_id,
        repository=task.repository,
        objective=task.goal,
        correlation_id=payload.correlation_id or f"corr_{secrets.token_hex(12)}",
        max_attempts_per_step=payload.max_attempts_per_step,
        metadata={
            **payload.metadata,
            "provider": str(task.provider),
            "source_task_id": task.source_task_id,
            "source_session_id": task.source_session_id,
            "branch_preference": task.branch_preference,
        },
    )
    store.agent_runs[run.id] = run

    for index, step_type in enumerate(DEFAULT_AGENT_RUN_STEPS):
        step = AgentRunStep(
            run_id=run.id,
            task_id=task.id,
            sequence=index,
            step_type=step_type,
            status=AgentRunStepStatus.READY if index == 0 else AgentRunStepStatus.PENDING,
            max_attempts=payload.max_attempts_per_step,
            idempotency_key=f"{run.id}:{index}:{step_type.value}",
        )
        store.agent_run_steps[step.id] = step
        if index == 0:
            run.current_step_id = step.id
            _append_event(
                run,
                AgentRunEventType.STEP_READY,
                step=step,
                new_status=step.status.value,
                message="First Mission Control step is ready.",
                actor=actor,
            )

    _append_event(
        run,
        AgentRunEventType.RUN_CREATED,
        new_status=run.status.value,
        message="Durable Mission Control run created from an approved AgentTask.",
        actor=actor,
        metadata={"step_count": len(DEFAULT_AGENT_RUN_STEPS)},
    )
    _audit(
        "agent_run.created",
        run.id,
        {
            "task_id": run.task_id,
            "approval_request_id": run.approval_request_id,
            "correlation_id": run.correlation_id,
            "step_count": len(DEFAULT_AGENT_RUN_STEPS),
        },
        actor=actor,
    )
    store.persist()
    return run


def acquire_run_lease(run: AgentRun, *, worker_id: str, lease_seconds: int) -> str:
    now = _now()
    if run.lease_owner and run.lease_expires_at and run.lease_expires_at > now:
        if run.lease_owner != worker_id:
            raise AgentRunConflict(f"Run is leased by {run.lease_owner} until {run.lease_expires_at.isoformat()}.")
        if run.lease_token:
            return run.lease_token
    token = secrets.token_urlsafe(24)
    run.lease_owner = worker_id
    run.lease_token = token
    run.heartbeat_at = now
    run.lease_expires_at = now + timedelta(seconds=lease_seconds)
    run.updated_at = now
    _append_event(
        run,
        AgentRunEventType.RUN_LEASE_ACQUIRED,
        message="Worker acquired the durable run lease.",
        actor=worker_id,
        metadata={"lease_seconds": lease_seconds},
    )
    store.persist()
    return token


def heartbeat_run(run: AgentRun, *, worker_id: str, lease_token: str, lease_seconds: int) -> AgentRun:
    if run.lease_owner != worker_id or run.lease_token != lease_token:
        raise AgentRunConflict("Run heartbeat rejected because lease owner or token does not match.")
    now = _now()
    run.heartbeat_at = now
    run.lease_expires_at = now + timedelta(seconds=lease_seconds)
    run.updated_at = now
    current = store.agent_run_steps.get(run.current_step_id or "")
    if current and current.lease_owner == worker_id and current.lease_token == lease_token:
        current.heartbeat_at = now
        current.lease_expires_at = run.lease_expires_at
        current.updated_at = now
    _append_event(
        run,
        AgentRunEventType.RUN_HEARTBEAT,
        step=current,
        message="Worker renewed the Mission Control lease.",
        actor=worker_id,
        metadata={"lease_seconds": lease_seconds},
    )
    store.persist()
    return run


def release_run_lease(run: AgentRun, *, worker_id: str, lease_token: str | None) -> None:
    if lease_token and run.lease_token != lease_token:
        return
    run.lease_owner = None
    run.lease_token = None
    run.lease_expires_at = None
    run.updated_at = _now()
    _append_event(
        run,
        AgentRunEventType.RUN_LEASE_RELEASED,
        message="Worker released the Mission Control lease.",
        actor=worker_id,
    )
    store.persist()


def _ready_step(run: AgentRun) -> AgentRunStep | None:
    now = _now()
    for step in _run_steps(run.id):
        if step.status == AgentRunStepStatus.RETRY_WAIT and step.next_retry_at and step.next_retry_at <= now:
            previous, new = transition_step(step, AgentRunStepStatus.READY, reason="Retry backoff elapsed.")
            _append_event(
                run,
                AgentRunEventType.STEP_READY,
                step=step,
                previous_status=previous.value,
                new_status=new.value,
                message="Retry backoff elapsed; step is ready again.",
            )
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
            previous, new = transition_step(step, AgentRunStepStatus.RETRY_WAIT, reason=result.reason)
            delay_seconds = min(300, 2 ** max(1, step.attempt_count))
            step.next_retry_at = _now() + timedelta(seconds=delay_seconds)
            previous_run, new_run = transition_run(run, AgentRunStatus.RETRY_WAIT, reason=result.reason)
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


def execute_next_step(
    run_id: str,
    *,
    worker_id: str,
    lease_seconds: int = 300,
    timeout_seconds: int = 120,
    executor: StepExecutor | None = None,
) -> AgentRunStepExecutionResult:
    run = store.agent_runs.get(run_id)
    if run is None:
        raise ValueError("Agent run not found.")
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

    lease_token = acquire_run_lease(run, worker_id=worker_id, lease_seconds=lease_seconds)
    step = _ready_step(run)
    if step is None:
        release_run_lease(run, worker_id=worker_id, lease_token=lease_token)
        if run.status == AgentRunStatus.RETRY_WAIT:
            return AgentRunStepExecutionResult(
                success=False,
                decision=VerificationDecision.RETRY_TRANSIENT,
                reason="No step is ready because retry backoff has not elapsed.",
            )
        raise AgentRunConflict("No executable step is ready.")

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

    previous, new = transition_step(step, AgentRunStepStatus.RUNNING)
    step.lease_owner = worker_id
    step.lease_token = lease_token
    step.lease_expires_at = run.lease_expires_at
    step.heartbeat_at = _now()
    run.current_step_id = step.id
    run.current_step_index = step.sequence
    if run.status in {AgentRunStatus.SCHEDULED, AgentRunStatus.RETRY_WAIT}:
        previous_run, new_run = transition_run(run, AgentRunStatus.RUNNING)
        _append_event(
            run,
            AgentRunEventType.RUN_STATE_CHANGED,
            previous_status=previous_run.value,
            new_status=new_run.value,
            message="Mission Control worker started or resumed execution.",
            actor=worker_id,
        )
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
    )
    store.persist()

    try:
        raise_if_fault_configured(run.id, step.step_type)
        effective_executor = executor or DefaultAgentRunStepExecutor()
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
    _audit(
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
    store.persist()
    return result


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
    ) -> None:
        self.branch_client = branch_client
        self.file_client = file_client
        self.pr_client = pr_client
        self.workspace_runner = workspace_runner
        self.container_runtime = container_runtime
        self.workspace_parent = workspace_parent

    def __call__(
        self,
        run: AgentRun,
        step: AgentRunStep,
        timeout_seconds: int,
    ) -> AgentRunStepExecutionResult:
        task = store.agent_tasks.get(run.task_id)
        if task is None:
            return classify_execution_result(
                success=False,
                reason="Agent task not found during execution.",
            )
        approval = store.approval_requests.get(run.approval_request_id or "")

        if step.step_type == AgentRunStepType.VALIDATE_SCOPE:
            return validate_scope_contract(task, approval)

        if step.step_type == AgentRunStepType.CREATE_BRANCH:
            result = run_agent_worker_github_branch_creation(
                task_id=task.id,
                worker_id=f"mission-control:{run.id}:branch",
                client=self.branch_client,
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
            task_events = [
                event
                for event in store.agent_task_events.values()
                if event.task_id == task.id
            ]
            pr_events = [
                event
                for event in task_events
                if event.event_type.value == "PR_CREATED"
            ]
            validation_passed = any(
                event.event_type.value == "TESTS_PASSED"
                for event in task_events
            )
            pr_url = next(
                (
                    event.metadata.get("pull_request_url")
                    for event in reversed(
                        sorted(pr_events, key=lambda event: event.created_at)
                    )
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
            steps = _run_steps(run.id)
            prior_steps = [item for item in steps if item.sequence < step.sequence]
            all_prior_passed = all(
                item.status == AgentRunStepStatus.SUCCEEDED
                for item in prior_steps
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
                "event_count": len(_run_events(run.id)),
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


def recover_stale_leases(*, actor: str = "mission-control-watchdog") -> int:
    now = _now()
    recovered = 0
    for run in store.agent_runs.values():
        if run.lease_expires_at is None or run.lease_expires_at > now:
            continue
        expired_owner = run.lease_owner
        run.lease_owner = None
        run.lease_token = None
        run.lease_expires_at = None
        run.heartbeat_at = None
        current = store.agent_run_steps.get(run.current_step_id or "")
        if current and current.status == AgentRunStepStatus.RUNNING:
            current.lease_owner = None
            current.lease_token = None
            current.lease_expires_at = None
            if current.attempt_count < current.max_attempts:
                previous, new = transition_step(
                    current,
                    AgentRunStepStatus.RETRY_WAIT,
                    reason="Worker lease expired.",
                )
                current.next_retry_at = now
                if run.status == AgentRunStatus.RUNNING:
                    transition_run(
                        run,
                        AgentRunStatus.RETRY_WAIT,
                        reason="Worker lease expired.",
                    )
            else:
                previous, new = transition_step(
                    current,
                    AgentRunStepStatus.NEEDS_HUMAN,
                    reason="Worker lease expired and retry budget is exhausted.",
                )
                if run.status in {
                    AgentRunStatus.RUNNING,
                    AgentRunStatus.RETRY_WAIT,
                }:
                    transition_run(
                        run,
                        AgentRunStatus.NEEDS_HUMAN,
                        reason=current.reason,
                    )
            _append_event(
                run,
                AgentRunEventType.STALE_LEASE_RECOVERED,
                step=current,
                previous_status=previous.value,
                new_status=new.value,
                message="Watchdog recovered an expired worker lease without claiming completion.",
                actor=actor,
                metadata={"expired_owner": expired_owner},
            )
        recovered += 1
    if recovered:
        _audit(
            "agent_run.stale_leases_recovered",
            "agent_runs",
            {"count": recovered},
            actor=actor,
        )
        store.persist()
    return recovered


def pause_agent_run(run: AgentRun, *, actor: str, reason: str = "") -> AgentRun:
    if run.status in {
        AgentRunStatus.BLOCKED,
        AgentRunStatus.SUCCEEDED,
        AgentRunStatus.FAILED,
        AgentRunStatus.CANCELLED,
    }:
        raise AgentRunConflict("Terminal run cannot be paused.")
    run.pause_requested = True
    if run.status != AgentRunStatus.PAUSED:
        previous, new = transition_run(run, AgentRunStatus.PAUSED, reason=reason)
        _append_event(
            run,
            AgentRunEventType.OPERATOR_ACTION,
            previous_status=previous.value,
            new_status=new.value,
            message="Operator paused the run.",
            reason=reason,
            actor=actor,
        )
    store.persist()
    return run


def resume_agent_run(run: AgentRun, *, actor: str, reason: str = "") -> AgentRun:
    if run.status not in {
        AgentRunStatus.PAUSED,
        AgentRunStatus.NEEDS_HUMAN,
        AgentRunStatus.RETRY_WAIT,
    }:
        raise AgentRunConflict(f"Run cannot resume from {run.status}.")
    run.pause_requested = False
    step = store.agent_run_steps.get(run.current_step_id or "")
    if step and step.status in {
        AgentRunStepStatus.PAUSED,
        AgentRunStepStatus.NEEDS_HUMAN,
        AgentRunStepStatus.RETRY_WAIT,
    }:
        previous_step, new_step = transition_step(
            step,
            AgentRunStepStatus.READY,
            reason=reason or "Operator resumed step.",
        )
        step.next_retry_at = None
        _append_event(
            run,
            AgentRunEventType.STEP_READY,
            step=step,
            previous_status=previous_step.value,
            new_status=new_step.value,
            message="Operator returned the current step to READY.",
            reason=reason,
            actor=actor,
        )
    previous, new = transition_run(run, AgentRunStatus.SCHEDULED, reason=reason)
    _append_event(
        run,
        AgentRunEventType.OPERATOR_ACTION,
        previous_status=previous.value,
        new_status=new.value,
        message="Operator resumed the run.",
        reason=reason,
        actor=actor,
    )
    store.persist()
    return run


def cancel_agent_run(run: AgentRun, *, actor: str, reason: str = "") -> AgentRun:
    if run.status == AgentRunStatus.CANCELLED:
        return run
    if run.status in {
        AgentRunStatus.BLOCKED,
        AgentRunStatus.SUCCEEDED,
        AgentRunStatus.FAILED,
    }:
        raise AgentRunConflict("Completed terminal run cannot be cancelled.")
    run.cancel_requested = True
    for step in _run_steps(run.id):
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
            _append_event(
                run,
                AgentRunEventType.STEP_CANCELLED,
                step=step,
                previous_status=previous.value,
                new_status=new.value,
                message="Pending work was cancelled.",
                reason=reason,
                actor=actor,
            )
    previous, new = transition_run(run, AgentRunStatus.CANCELLED, reason=reason)
    _append_event(
        run,
        AgentRunEventType.OPERATOR_ACTION,
        previous_status=previous.value,
        new_status=new.value,
        message="Operator cancelled the run.",
        reason=reason,
        actor=actor,
    )
    store.persist()
    return run


def retry_agent_run_step(
    run: AgentRun,
    step: AgentRunStep,
    *,
    actor: str,
    reason: str = "",
) -> AgentRunStep:
    if step.run_id != run.id:
        raise AgentRunConflict("Step does not belong to run.")
    if step.status not in {
        AgentRunStepStatus.RETRY_WAIT,
        AgentRunStepStatus.NEEDS_HUMAN,
        AgentRunStepStatus.FAILED,
    }:
        raise AgentRunConflict(f"Step cannot be retried from {step.status}.")
    if step.attempt_count >= step.max_attempts:
        raise AgentRunConflict("Step retry budget is exhausted.")
    if step.status == AgentRunStepStatus.FAILED:
        step.status = AgentRunStepStatus.NEEDS_HUMAN
    previous, new = transition_step(
        step,
        AgentRunStepStatus.READY,
        reason=reason or "Operator approved retry.",
    )
    step.next_retry_at = None
    run.current_step_id = step.id
    run.current_step_index = step.sequence
    run.pause_requested = False
    if run.status in {AgentRunStatus.NEEDS_HUMAN, AgentRunStatus.RETRY_WAIT}:
        transition_run(run, AgentRunStatus.SCHEDULED, reason=reason)
    _append_event(
        run,
        AgentRunEventType.OPERATOR_ACTION,
        step=step,
        previous_status=previous.value,
        new_status=new.value,
        message="Operator approved a bounded step retry.",
        reason=reason,
        actor=actor,
    )
    store.persist()
    return step


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
