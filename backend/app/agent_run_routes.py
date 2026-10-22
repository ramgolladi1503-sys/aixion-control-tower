from __future__ import annotations

from threading import Lock

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from .agent_run_faults import default_fault_reason
from .agent_run_metrics import (
    build_agent_run_summary,
    prometheus_agent_run_metrics,
)
from .application_repository_access import (
    agent_task_repository_for_request,
    approval_decision_repository_for_request,
    agent_run_repository_for_request,
    workspace_id_for_request,
)
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
    AgentRunStepType,
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
from .agent_run_repository import (
    AgentRunRepositoryConflict,
    AgentRunRepositoryNotFound,
    PostgresAgentRunRepository,
    SQLiteAgentRunRepository,
)
from .approval_decisions import (
    agent_task_repository,
    approval_decision_repository,
)
from .agent_worker_validation_plan import MAX_VALIDATION_COMMANDS
from .auth import require_maintainer, require_reviewer
from .metrics_auth import METRICS_SCRAPER_USER_ID, require_metrics_or_maintainer
from .models import AuditEvent, AuthUser, now_utc
from .settings import get_settings
from .store import store
from .trust_run_capability import RunCapabilityConflict, ensure_run_capability
from .workspace_access import (
    agent_run_scope_is_consistent,
    agent_run_workspace_id,
    workspace_id_for_user,
)

router = APIRouter(prefix="/agent/runs", tags=["agent-runs"])
ReviewerDependency = Depends(require_reviewer)
MaintainerDependency = Depends(require_maintainer)
MetricsDependency = Depends(require_metrics_or_maintainer)
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


def _run_or_404(run_id: str, user: AuthUser | None = None) -> AgentRun:
    run = store.agent_runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Agent run not found")
    if not agent_run_scope_is_consistent(run):
        raise HTTPException(status_code=404, detail="Agent run not found")
    if user is not None:
        workspace_id = workspace_id_for_user(user)
        run_workspace = agent_run_workspace_id(run)
        if workspace_id is not None and run_workspace != workspace_id:
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


def _durable_run_or_404(
    run_id: str,
    *,
    user: AuthUser,
    request: Request,
) -> AgentRun:
    repository = agent_run_repository_for_request(request)
    run = repository.get(run_id)
    workspace_id = workspace_id_for_request(user, request)
    if (
        run is None
        or (get_settings().auth_enabled and workspace_id is None)
        or (workspace_id is not None and run.workspace_id != workspace_id)
    ):
        raise HTTPException(status_code=404, detail="Agent run not found")
    return run


def _durable_detail(run: AgentRun, request: Request) -> AgentRunDetail:
    repository = agent_run_repository_for_request(request)
    return AgentRunDetail(
        run=run,
        steps=repository.list_steps_for_run(run.id),
        events=repository.list_events_for_run(run.id),
    )


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


def _require_step_boundary(
    run: AgentRun,
    action: str,
    run_repository=None,
) -> None:
    current_step_id = run.current_step_id or ""
    if run_repository is None:
        current = store.agent_run_steps.get(current_step_id)
    else:
        current = next(
            (step for step in run_repository.list_steps_for_run(run.id) if step.id == current_step_id),
            None,
        )
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
    request: Request = None,  # type: ignore[assignment]
) -> AgentRunDetail:
    task_repository = (
        agent_task_repository_for_request(request)
        if request is not None
        else agent_task_repository
    )
    approval_repository = (
        approval_decision_repository_for_request(request)
        if request is not None
        else approval_decision_repository
    )
    run_repository = (
        agent_run_repository_for_request(request)
        if request is not None
        else None
    )
    workspace_id = (
        workspace_id_for_request(user, request)
        if request is not None
        else workspace_id_for_user(user)
    )
    if get_settings().auth_enabled and workspace_id is None:
        raise HTTPException(status_code=404, detail="Agent task not found")
    task = task_repository.get(payload.task_id)
    if (
        task is None
        or (workspace_id is not None and task.workspace_id != workspace_id)
    ):
        raise HTTPException(status_code=404, detail="Agent task not found")
    if (
        payload.capability_lease_id
        and run_repository is not None
        and not isinstance(run_repository, SQLiteAgentRunRepository)
    ):
        raise HTTPException(
            status_code=409,
            detail="Capability lease binding is unavailable for the configured AgentRun repository",
        )
    try:
        run = create_agent_run(
            payload,
            actor=user.email,
            task_repository=task_repository,
            approval_repository=approval_repository,
            run_repository=run_repository,
        )
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
    return _durable_detail(run, request) if request is not None else _detail(run)


@router.get("", response_model=list[AgentRun])
def list_runs(
    status: AgentRunStatus | None = None,
    task_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    user: AuthUser = ReviewerDependency,
    request: Request = None,  # type: ignore[assignment]
) -> list[AgentRun]:
    if request is None:
        # Direct route-function calls are retained for focused unit tests; FastAPI
        # always supplies Request for HTTP traffic, which uses durable storage.
        workspace_id = workspace_id_for_user(user)
        runs = [run for run in store.agent_runs.values() if agent_run_scope_is_consistent(run)]
        if workspace_id is not None:
            runs = [run for run in runs if agent_run_workspace_id(run) == workspace_id]
        if status is not None:
            runs = [run for run in runs if run.status == status]
        if task_id is not None:
            runs = [run for run in runs if run.task_id == task_id]
        return sorted(runs, key=lambda run: run.updated_at, reverse=True)[:limit]
    workspace_id = workspace_id_for_request(user, request)
    if get_settings().auth_enabled and workspace_id is None:
        raise HTTPException(status_code=404, detail="Agent run not found")
    return agent_run_repository_for_request(request).list_for_workspace(
        workspace_id=workspace_id,
        status=status.value if status is not None else None,
        task_id=task_id,
        limit=limit,
    )


def _run_workspace_id(run: AgentRun) -> str | None:
    return agent_run_workspace_id(run)


def _require_non_production_fault_injection() -> None:
    if get_settings().is_production:
        # Keep diagnostic controls out of the production API surface.
        raise HTTPException(status_code=404, detail="Not found")


@router.get("/summary", response_model=AgentRunSummary)
def get_run_summary(
    user: AuthUser = ReviewerDependency,
    request: Request = None,  # type: ignore[assignment]
) -> AgentRunSummary:
    if request is None:
        return build_agent_run_summary(workspace_id_for_user(user))
    workspace_id = workspace_id_for_request(user, request)
    if get_settings().auth_enabled and workspace_id is None:
        raise HTTPException(status_code=404, detail="Agent run not found")
    return build_agent_run_summary(
        workspace_id,
        repository=agent_run_repository_for_request(request),
    )


@router.get("/metrics")
def get_run_metrics(
    user: AuthUser = MetricsDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> Response:
    workspace_id = (
        user.workspace_id
        if user.id == METRICS_SCRAPER_USER_ID
        else workspace_id_for_user(user)
    )
    notification_repository = (
        getattr(http_request.app.state, "notification_outbox_repository", None)
        if http_request is not None
        else None
    )
    if http_request is not None:
        run_repository = agent_run_repository_for_request(http_request)
        postgres_runtime = getattr(http_request.app.state, "postgres_runtime", None)
        if (
            postgres_runtime is not None
            or not isinstance(run_repository, SQLiteAgentRunRepository)
            or notification_repository is not None
        ):
            raise HTTPException(
                status_code=503,
                detail=(
                    "Agent-run metrics are unavailable while PostgreSQL is selected "
                    "until run, trust, and notification metric families use the "
                    "same durable runtime."
                ),
            )
    content = prometheus_agent_run_metrics(workspace_id)
    return Response(
        content=content,
        media_type="text/plain; version=0.0.4",
    )


@router.post("/watchdog/recover-stale")
def recover_stale_run_leases(
    user: AuthUser = MaintainerDependency,
    request: Request = None,  # type: ignore[assignment]
) -> dict[str, int]:
    repository = agent_run_repository_for_request(request) if request is not None else None
    workspace_id = (
        workspace_id_for_request(user, request)
        if request is not None
        else workspace_id_for_user(user)
    )
    return {
        "recovered": recover_stale_leases(
            actor=user.email,
            workspace_id=workspace_id,
            run_repository=repository,
        )
    }


@router.get("/faults", response_model=list[AgentRunFaultConfig])
def list_faults(user: AuthUser = MaintainerDependency) -> list[AgentRunFaultConfig]:
    _require_non_production_fault_injection()
    workspace_id = workspace_id_for_user(user)
    return sorted(
        [fault for fault in store.agent_run_faults.values()
         if fault.run_id is not None and fault.run_id in store.agent_runs
         and agent_run_scope_is_consistent(store.agent_runs[fault.run_id])
         and (workspace_id is None or _run_workspace_id(store.agent_runs[fault.run_id]) == workspace_id)],
        key=lambda fault: fault.created_at,
        reverse=True,
    )


@router.post("/faults", response_model=AgentRunFaultConfig)
def create_fault(
    payload: AgentRunFaultCreate,
    user: AuthUser = MaintainerDependency,
) -> AgentRunFaultConfig:
    _require_non_production_fault_injection()
    workspace_id = workspace_id_for_user(user)
    if not payload.run_id:
        raise HTTPException(status_code=422, detail="A run_id is required for workspace-scoped fault injection")
    if payload.run_id not in store.agent_runs:
        raise HTTPException(status_code=404, detail="Agent run not found")
    run = store.agent_runs[payload.run_id]
    if not agent_run_scope_is_consistent(run) or (
        workspace_id is not None and _run_workspace_id(run) != workspace_id
    ):
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
    _require_non_production_fault_injection()
    fault = store.agent_run_faults.get(fault_id)
    workspace_id = workspace_id_for_user(user)
    if fault is None or fault.run_id not in store.agent_runs or (
        not agent_run_scope_is_consistent(store.agent_runs[fault.run_id])
        or (workspace_id is not None and _run_workspace_id(store.agent_runs[fault.run_id]) != workspace_id)
    ):
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
def get_run(
    run_id: str,
    user: AuthUser = ReviewerDependency,
    request: Request = None,  # type: ignore[assignment]
) -> AgentRunDetail:
    if request is None:
        return _detail(_run_or_404(run_id, user))
    return _durable_detail(_durable_run_or_404(run_id, user=user, request=request), request)


@router.get("/{run_id}/steps", response_model=list[AgentRunStep])
def list_run_steps(
    run_id: str,
    user: AuthUser = ReviewerDependency,
    request: Request = None,  # type: ignore[assignment]
) -> list[AgentRunStep]:
    if request is None:
        _run_or_404(run_id, user)
        return _steps(run_id)
    _durable_run_or_404(run_id, user=user, request=request)
    return agent_run_repository_for_request(request).list_steps_for_run(run_id)


@router.get("/{run_id}/events", response_model=list[AgentRunEvent])
def list_run_events(
    run_id: str,
    user: AuthUser = ReviewerDependency,
    request: Request = None,  # type: ignore[assignment]
) -> list[AgentRunEvent]:
    if request is None:
        _run_or_404(run_id, user)
        return _events(run_id)
    _durable_run_or_404(run_id, user=user, request=request)
    return agent_run_repository_for_request(request).list_events_for_run(run_id)


@router.post("/{run_id}/capability-lease", response_model=AgentRunDetail)
def bind_run_capability_lease(
    run_id: str,
    payload: AgentRunCapabilityLeaseRequest,
    user: AuthUser = MaintainerDependency,
    request: Request = None,  # type: ignore[assignment]
) -> AgentRunDetail:
    run_repository = (
        agent_run_repository_for_request(request) if request is not None else None
    )
    if isinstance(run_repository, PostgresAgentRunRepository):
        workspace_id = workspace_id_for_request(user, request)
        if workspace_id is None:
            raise HTTPException(status_code=404, detail="Agent run not found")
        try:
            run = run_repository.attach_capability_lease(
                run_id=run_id,
                workspace_id=workspace_id,
                lease_id=payload.capability_lease_id,
                actor=user.email,
                now=now_utc(),
            )
        except AgentRunRepositoryNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except AgentRunRepositoryConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return _durable_detail(run, request)
    if run_repository is not None and not isinstance(run_repository, SQLiteAgentRunRepository):
        raise HTTPException(
            status_code=409,
            detail=(
                "Capability lease binding is unavailable for the configured AgentRun "
                "repository until capability and trust records share its durable transaction path."
            ),
        )
    if request is None:
        run = _run_or_404(run_id, user)
    else:
        run = _durable_run_or_404(run_id, user=user, request=request)
        store.project_agent_run_snapshot(
            run,
            run_repository.list_steps_for_run(run.id),
            run_repository.list_events_for_run(run.id),
        )
        run = store.agent_runs[run.id]
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
    request: Request = None,  # type: ignore[assignment]
) -> AgentRunDetail:
    run_repository = (
        agent_run_repository_for_request(request) if request is not None else None
    )
    if run_repository is not None and not isinstance(
        run_repository, (SQLiteAgentRunRepository, PostgresAgentRunRepository)
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "AgentRun execution is unavailable until capability, trust, and "
                "worker effects are supported by the configured repository."
            ),
        )
    if run_repository is not None:
        durable_run = run_repository.get(run_id)
        workspace_id = (
            workspace_id_for_request(user, request)
            if request is not None
            else workspace_id_for_user(user)
        )
        if (
            durable_run is None
            or (get_settings().auth_enabled and workspace_id is None)
            or (workspace_id is not None and durable_run.workspace_id != workspace_id)
        ):
            raise HTTPException(status_code=404, detail="Agent run not found")
        store.project_agent_run_snapshot(
            durable_run,
            run_repository.list_steps_for_run(run_id),
            run_repository.list_events_for_run(run_id),
        )
        run = store.agent_runs[run_id]
    else:
        run = _run_or_404(run_id, user)
    task_repository = (
        agent_task_repository_for_request(request) if request is not None else None
    )
    approval_repository = (
        approval_decision_repository_for_request(request) if request is not None else None
    )
    execution_lock = _acquire_execution_lock(run.id)
    try:
        _require_step_boundary(run, "start another execution")
        lease_seconds = _safe_lease_seconds(payload)
        current_step = (
            next(
                (
                    step
                    for step in run_repository.list_steps_for_run(run.id)
                    if step.id == run.current_step_id
                ),
                None,
            )
            if run_repository is not None
            else store.agent_run_steps.get(run.current_step_id or "")
        )
        if current_step is None:
            raise HTTPException(status_code=409, detail="Run has no current step")
        if (
            isinstance(run_repository, PostgresAgentRunRepository)
            and current_step.step_type != AgentRunStepType.VALIDATE_SCOPE
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "PostgreSQL AgentRun execution currently supports only the "
                    "side-effect-free VALIDATE_SCOPE step."
                ),
            )
        try:
            postgres_scope_only = (
                isinstance(run_repository, PostgresAgentRunRepository)
                and current_step.step_type == AgentRunStepType.VALIDATE_SCOPE
            )
            trust_actions = []
            if not postgres_scope_only:
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
                task_repository=task_repository,
                approval_repository=approval_repository,
                run_repository=run_repository,
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
        if isinstance(run_repository, PostgresAgentRunRepository):
            run = run_repository.get(run.id)
            if run is None:
                raise HTTPException(status_code=503, detail="AgentRun disappeared after execution")
        return _durable_detail(run, request) if request is not None else _detail(run)
    finally:
        _release_execution_lock(run.id, execution_lock)


@router.post("/{run_id}/heartbeat", response_model=AgentRun)
def heartbeat_agent_run(
    run_id: str,
    payload: AgentRunHeartbeatRequest,
    user: AuthUser = MaintainerDependency,
    request: Request = None,  # type: ignore[assignment]
) -> AgentRun:
    repository = agent_run_repository_for_request(request) if request is not None else None
    run = (
        _durable_run_or_404(run_id, user=user, request=request)
        if request is not None
        else _run_or_404(run_id, user)
    )
    try:
        return heartbeat_run(
            run,
            worker_id=payload.worker_id,
            lease_token=payload.lease_token,
            lease_seconds=payload.lease_seconds,
            run_repository=repository,
        )
    except AgentRunConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/{run_id}/pause", response_model=AgentRunDetail)
def pause_run(
    run_id: str,
    payload: AgentRunControlRequest,
    user: AuthUser = MaintainerDependency,
    request: Request = None,  # type: ignore[assignment]
) -> AgentRunDetail:
    repository = agent_run_repository_for_request(request) if request is not None else None
    run = (
        _durable_run_or_404(run_id, user=user, request=request)
        if request is not None
        else _run_or_404(run_id, user)
    )
    _require_step_boundary(run, "pause the run", repository)
    try:
        run = pause_agent_run(
            run, actor=user.email, reason=payload.reason, run_repository=repository
        )
    except AgentRunConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _durable_detail(run, request) if request is not None else _detail(run)


@router.post("/{run_id}/resume", response_model=AgentRunDetail)
def resume_run(
    run_id: str,
    payload: AgentRunControlRequest,
    user: AuthUser = MaintainerDependency,
    request: Request = None,  # type: ignore[assignment]
) -> AgentRunDetail:
    repository = agent_run_repository_for_request(request) if request is not None else None
    run = (
        _durable_run_or_404(run_id, user=user, request=request)
        if request is not None
        else _run_or_404(run_id, user)
    )
    try:
        run = resume_agent_run(
            run, actor=user.email, reason=payload.reason, run_repository=repository
        )
    except AgentRunConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _durable_detail(run, request) if request is not None else _detail(run)


@router.post("/{run_id}/cancel", response_model=AgentRunDetail)
def cancel_run(
    run_id: str,
    payload: AgentRunControlRequest,
    user: AuthUser = MaintainerDependency,
    request: Request = None,  # type: ignore[assignment]
) -> AgentRunDetail:
    repository = agent_run_repository_for_request(request) if request is not None else None
    run = (
        _durable_run_or_404(run_id, user=user, request=request)
        if request is not None
        else _run_or_404(run_id, user)
    )
    _require_step_boundary(run, "cancel the run", repository)
    try:
        run = cancel_agent_run(
            run, actor=user.email, reason=payload.reason, run_repository=repository
        )
    except AgentRunConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _durable_detail(run, request) if request is not None else _detail(run)


@router.post("/{run_id}/retry", response_model=AgentRunDetail)
def retry_run(
    run_id: str,
    payload: AgentRunRetryRequest,
    user: AuthUser = MaintainerDependency,
    request: Request = None,  # type: ignore[assignment]
) -> AgentRunDetail:
    repository = agent_run_repository_for_request(request) if request is not None else None
    run = (
        _durable_run_or_404(run_id, user=user, request=request)
        if request is not None
        else _run_or_404(run_id, user)
    )
    if run.status in TERMINAL_RUN_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Terminal run {run.status} cannot be retried. "
                "Create a revised approval and a new run instead."
            ),
        )
    if repository is None:
        step = (
            _step_or_404(payload.step_id)
            if payload.step_id
            else store.agent_run_steps.get(run.current_step_id or "")
        )
    else:
        step = next(
            (
                candidate
                for candidate in repository.list_steps_for_run(run.id)
                if candidate.id == (payload.step_id or run.current_step_id)
            ),
            None,
        )
    if step is None:
        raise HTTPException(status_code=409, detail="Run has no current step to retry")
    if step.status in NON_RETRYABLE_STEP_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Step {step.status} is not eligible for bounded retry.",
        )
    try:
        retry_agent_run_step(
            run,
            step,
            actor=user.email,
            reason=payload.reason,
            run_repository=repository,
        )
    except AgentRunConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _durable_detail(run, request) if request is not None else _detail(run)
