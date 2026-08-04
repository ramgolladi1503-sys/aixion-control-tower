from __future__ import annotations

from threading import Lock

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from .agent_run_faults import default_fault_reason
from .agent_run_metrics import build_agent_run_summary, prometheus_agent_run_metrics
from .agent_run_models import (
    AgentRun,
    AgentRunCapabilityLeaseRequest,
    AgentRunControlRequest,
    AgentRunCreate,
    AgentRunDetail,
    AgentRunEvent,
    AgentRunExecuteRequest,
    AgentRunFaultConfig,
    AgentRunFaultCreate,
    AgentRunHeartbeatRequest,
    AgentRunRetryRequest,
    AgentRunStatus,
    AgentRunStep,
    AgentRunStepStatus,
    AgentRunSummary,
)
from .agent_run_supervisor import (
    AgentRunConflict,
    cancel_agent_run,
    create_agent_run,
    execute_next_step,
    heartbeat_run,
    pause_agent_run,
    recover_stale_leases,
    resume_agent_run,
    retry_agent_run_step,
)
from .agent_run_trust import (
    AgentRunTrustConflict,
    attach_capability_lease,
    authorize_run_step,
    consume_run_step_actions,
)
from .agent_worker_validation_plan import MAX_VALIDATION_COMMANDS
from .auth import require_maintainer, require_reviewer
from .models import AuditEvent, AuthUser
from .store import store
from .trust_run_capability import RunCapabilityConflict, ensure_run_capability

router = APIRouter(prefix="/agent/runs", tags=["agent-runs"])
ReviewerDependency = Depends(require_reviewer)
MaintainerDependency = Depends(require_maintainer)
MAX_RUN_LEASE_SECONDS = 3600
RUN_LEASE_OVERHEAD_SECONDS = 60
TERMINAL_RUN_STATUSES = {
    AgentRunStatus.BLOCKED,
    AgentRunStatus.SUCCEEDED,
    AgentRunStatus.FAILED,
    AgentRunStatus.CANCELLED,
}
NON_RETRYABLE_STEP_STATUSES = {
    AgentRunStepStatus.RUNNING,
    AgentRunStepStatus.BLOCKED,
    AgentRunStepStatus.SUCCEEDED,
    AgentRunStepStatus.FAILED,
    AgentRunStepStatus.CANCELLED,
    AgentRunStepStatus.SKIPPED,
}
_EXECUTION_LOCK_GUARD = Lock()
_EXECUTION_LOCKS: dict[str, Lock] = {}


def _audit(event_type: str, entity_id: str, details: dict, actor: str) -> None:
    store.audit_events.append(
        AuditEvent(
            event_type=event_type,
            entity_id=entity_id,
            details=details,
            actor=actor,
        )
    )


def _run_or_404(run_id: str) -> AgentRun:
    run = store.agent_runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Agent run not found")
    return run


def _step_or_404(step_id: str) -> AgentRunStep:
    step = store.agent_run_steps.get(step_id)
    if step is None:
        raise HTTPException(status_code=404, detail="Agent run step not found")
    return step


def _steps(run_id: str) -> list[AgentRunStep]:
    return sorted(
        [step for step in store.agent_run_steps.values() if step.run_id == run_id],
        key=lambda step: step.sequence,
    )


def _events(run_id: str) -> list[AgentRunEvent]:
    return sorted(
        [event for event in store.agent_run_events.values() if event.run_id == run_id],
        key=lambda event: event.created_at,
    )


def _detail(run: AgentRun) -> AgentRunDetail:
    return AgentRunDetail(run=run, steps=_steps(run.id), events=_events(run.id))


def _safe_lease_seconds(payload: AgentRunExecuteRequest) -> int:
    required = payload.timeout_seconds * MAX_VALIDATION_COMMANDS + RUN_LEASE_OVERHEAD_SECONDS
    if required > MAX_RUN_LEASE_SECONDS:
        raise HTTPException(
            status_code=422,
            detail=(
                "timeout_seconds is too large for the bounded Mission Control lease. "
                f"Maximum safe value is "
                f"{(MAX_RUN_LEASE_SECONDS - RUN_LEASE_OVERHEAD_SECONDS) // MAX_VALIDATION_COMMANDS}."
            ),
        )
    return max(payload.lease_seconds, required)


def _require_step_boundary(run: AgentRun, action: str) -> None:
    current = store.agent_run_steps.get(run.current_step_id or "")
    if (
        (current is not None and current.status == AgentRunStepStatus.RUNNING)
        or run.lease_owner is not None
        or run.lease_token is not None
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot {action} while a governed step is in flight. "
                "Wait for the step boundary or recover an expired lease first."
            ),
        )


def _acquire_execution_lock(run_id: str) -> Lock:
    with _EXECUTION_LOCK_GUARD:
        lock = _EXECUTION_LOCKS.setdefault(run_id, Lock())
    if not lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail="Another execution request is already active for this AgentRun.",
        )
    return lock


def _release_execution_lock(run_id: str, lock: Lock) -> None:
    lock.release()
    with _EXECUTION_LOCK_GUARD:
        if not lock.locked():
            _EXECUTION_LOCKS.pop(run_id, None)


@router.post("", response_model=AgentRunDetail)
def create_run(
    payload: AgentRunCreate,
    user: AuthUser = MaintainerDependency,
) -> AgentRunDetail:
    try:
        run = create_agent_run(payload, actor=user.email)
        if payload.capability_lease_id:
            attach_capability_lease(
                run,
                payload.capability_lease_id,
                actor=user.email,
            )
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (AgentRunConflict, AgentRunTrustConflict) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _detail(run)


@router.get("", response_model=list[AgentRun])
def list_runs(
    status: AgentRunStatus | None = None,
    task_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    _: AuthUser = ReviewerDependency,
) -> list[AgentRun]:
    runs = list(store.agent_runs.values())
    if status is not None:
        runs = [run for run in runs if run.status == status]
    if task_id is not None:
        runs = [run for run in runs if run.task_id == task_id]
    return sorted(runs, key=lambda run: run.updated_at, reverse=True)[:limit]


@router.get("/summary", response_model=AgentRunSummary)
def get_run_summary(_: AuthUser = ReviewerDependency) -> AgentRunSummary:
    return build_agent_run_summary()


@router.get("/metrics")
def get_run_metrics(_: AuthUser = MaintainerDependency) -> Response:
    return Response(
        content=prometheus_agent_run_metrics(),
        media_type="text/plain; version=0.0.4",
    )


@router.post("/watchdog/recover-stale")
def recover_stale_run_leases(user: AuthUser = MaintainerDependency) -> dict[str, int]:
    return {"recovered": recover_stale_leases(actor=user.email)}


@router.get("/faults", response_model=list[AgentRunFaultConfig])
def list_faults(_: AuthUser = MaintainerDependency) -> list[AgentRunFaultConfig]:
    return sorted(
        store.agent_run_faults.values(),
        key=lambda fault: fault.created_at,
        reverse=True,
    )


@router.post("/faults", response_model=AgentRunFaultConfig)
def create_fault(
    payload: AgentRunFaultCreate,
    user: AuthUser = MaintainerDependency,
) -> AgentRunFaultConfig:
    if payload.run_id and payload.run_id not in store.agent_runs:
        raise HTTPException(status_code=404, detail="Agent run not found")
    fault = AgentRunFaultConfig(
        **payload.model_dump(exclude={"message"}),
        message=payload.message or default_fault_reason(payload.fault_kind),
        created_by=user.email,
    )
    store.agent_run_faults[fault.id] = fault
    _audit(
        "agent_run.fault_configured",
        fault.id,
        {
            "run_id": fault.run_id,
            "step_type": fault.step_type,
            "fault_kind": fault.fault_kind,
            "remaining_uses": fault.remaining_uses,
        },
        actor=user.email,
    )
    store.persist()
    return fault


@router.delete("/faults/{fault_id}", response_model=AgentRunFaultConfig)
def disable_fault(
    fault_id: str,
    user: AuthUser = MaintainerDependency,
) -> AgentRunFaultConfig:
    fault = store.agent_run_faults.get(fault_id)
    if fault is None:
        raise HTTPException(status_code=404, detail="Fault configuration not found")
    fault.enabled = False
    _audit(
        "agent_run.fault_disabled",
        fault.id,
        {"fault_kind": fault.fault_kind},
        actor=user.email,
    )
    store.persist()
    return fault


@router.get("/{run_id}", response_model=AgentRunDetail)
def get_run(run_id: str, _: AuthUser = ReviewerDependency) -> AgentRunDetail:
    return _detail(_run_or_404(run_id))


@router.get("/{run_id}/steps", response_model=list[AgentRunStep])
def list_run_steps(run_id: str, _: AuthUser = ReviewerDependency) -> list[AgentRunStep]:
    _run_or_404(run_id)
    return _steps(run_id)


@router.get("/{run_id}/events", response_model=list[AgentRunEvent])
def list_run_events(run_id: str, _: AuthUser = ReviewerDependency) -> list[AgentRunEvent]:
    _run_or_404(run_id)
    return _events(run_id)


@router.post("/{run_id}/capability-lease", response_model=AgentRunDetail)
def bind_run_capability_lease(
    run_id: str,
    payload: AgentRunCapabilityLeaseRequest,
    user: AuthUser = MaintainerDependency,
) -> AgentRunDetail:
    run = _run_or_404(run_id)
    _require_step_boundary(run, "bind a capability lease")
    try:
        attach_capability_lease(
            run,
            payload.capability_lease_id,
            actor=user.email,
        )
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except AgentRunTrustConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _detail(run)


@router.post("/{run_id}/execute-next", response_model=AgentRunDetail)
def execute_run_next_step(
    run_id: str,
    payload: AgentRunExecuteRequest,
    user: AuthUser = MaintainerDependency,
) -> AgentRunDetail:
    run = _run_or_404(run_id)
    execution_lock = _acquire_execution_lock(run.id)
    try:
        _require_step_boundary(run, "start another execution")
        lease_seconds = _safe_lease_seconds(payload)
        current_step = store.agent_run_steps.get(run.current_step_id or "")
        if current_step is None:
            raise HTTPException(status_code=409, detail="Run has no current step")
        try:
            ensure_run_capability(run, user=user)
            trust_actions = authorize_run_step(
                run,
                current_step,
                actor=user.email,
                timeout_seconds=payload.timeout_seconds,
            )
            execute_next_step(
                run.id,
                worker_id=payload.worker_id,
                lease_seconds=lease_seconds,
                timeout_seconds=payload.timeout_seconds,
            )
            consume_run_step_actions(
                trust_actions,
                current_step,
                actor=user.email,
            )
        except (
            AgentRunConflict,
            AgentRunTrustConflict,
            RunCapabilityConflict,
        ) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return _detail(run)
    finally:
        _release_execution_lock(run.id, execution_lock)


@router.post("/{run_id}/heartbeat", response_model=AgentRun)
def heartbeat_agent_run(
    run_id: str,
    payload: AgentRunHeartbeatRequest,
    _: AuthUser = MaintainerDependency,
) -> AgentRun:
    run = _run_or_404(run_id)
    try:
        return heartbeat_run(
            run,
            worker_id=payload.worker_id,
            lease_token=payload.lease_token,
            lease_seconds=payload.lease_seconds,
        )
    except AgentRunConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/{run_id}/pause", response_model=AgentRunDetail)
def pause_run(
    run_id: str,
    payload: AgentRunControlRequest,
    user: AuthUser = MaintainerDependency,
) -> AgentRunDetail:
    run = _run_or_404(run_id)
    _require_step_boundary(run, "pause the run")
    try:
        pause_agent_run(run, actor=user.email, reason=payload.reason)
    except AgentRunConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _detail(run)


@router.post("/{run_id}/resume", response_model=AgentRunDetail)
def resume_run(
    run_id: str,
    payload: AgentRunControlRequest,
    user: AuthUser = MaintainerDependency,
) -> AgentRunDetail:
    run = _run_or_404(run_id)
    try:
        resume_agent_run(run, actor=user.email, reason=payload.reason)
    except AgentRunConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _detail(run)


@router.post("/{run_id}/cancel", response_model=AgentRunDetail)
def cancel_run(
    run_id: str,
    payload: AgentRunControlRequest,
    user: AuthUser = MaintainerDependency,
) -> AgentRunDetail:
    run = _run_or_404(run_id)
    _require_step_boundary(run, "cancel the run")
    try:
        cancel_agent_run(run, actor=user.email, reason=payload.reason)
    except AgentRunConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _detail(run)


@router.post("/{run_id}/retry", response_model=AgentRunDetail)
def retry_run(
    run_id: str,
    payload: AgentRunRetryRequest,
    user: AuthUser = MaintainerDependency,
) -> AgentRunDetail:
    run = _run_or_404(run_id)
    if run.status in TERMINAL_RUN_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Terminal run {run.status} cannot be retried. "
                "Create a revised approval and a new run instead."
            ),
        )
    step = (
        _step_or_404(payload.step_id)
        if payload.step_id
        else store.agent_run_steps.get(run.current_step_id or "")
    )
    if step is None:
        raise HTTPException(status_code=409, detail="Run has no current step to retry")
    if step.status in NON_RETRYABLE_STEP_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Step {step.status} is not eligible for bounded retry.",
        )
    try:
        retry_agent_run_step(run, step, actor=user.email, reason=payload.reason)
    except AgentRunConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _detail(run)
