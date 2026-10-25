from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .agent_run_models import AgentRunCreate
from .agent_run_repository import AgentRunRepository, PostgresAgentRunRepository
from .agent_run_supervisor import create_agent_run
from .agent_task_cancel import (
    AgentTaskCancelRequest,
    AgentTaskCancelSummary,
    append_cancel_requested_event,
    audit_cancel_requested,
    cancel_summary,
    prepare_linked_approval_cancellation,
)
from .agent_task_models import (
    AgentTask,
    AgentTaskCreate,
    AgentTaskEvent,
    AgentTaskEventCreate,
    AgentTaskEventType,
    AgentTaskStatus,
)
from .agent_task_repository import AgentTaskRepositoryConflict
from .agent_task_repository import (
    AgentTaskRepository,
    PostgresAgentTaskRepository,
    SQLiteAgentTaskRepository,
)
from .agent_task_notifications import (
    build_agent_task_worker_status_notification,
    notify_agent_task_approval_decision,
    notify_agent_task_worker_status,
)
from .approval_decisions import (
    agent_task_repository,
    approval_decision_repository,
    project_repository,
)
from .approval_expiry import approval_repository_publishes_compatibility_state
from .approval_repository import (
    ApprovalDecisionCommit,
    ApprovalDecisionConflict,
    ApprovalDecisionRepository,
)
from .application_repository_access import (
    agent_run_repository_for_request,
    agent_task_repository_for_request,
    approval_decision_repository_for_request,
    project_repository_for_request,
    work_order_repository_for_request,
    workspace_id_for_request,
)
from .agent_task_retry import (
    AgentTaskRetryRequest,
    AgentTaskRetrySummary,
    append_retry_requested_event,
    audit_retry_requested,
    retry_summary,
)
from .auth import require_maintainer, require_reviewer
from .models import (
    AgentProvider,
    ApprovalRequest,
    ApprovalRequestCreate,
    ApprovalStatus,
    AuditEvent,
    AuthUser,
    Notification,
    now_utc,
)
from .risk_engine import assess_approval_request
from .settings import get_settings
from .store import store
from .workspace_access import (
    agent_task_scope_is_consistent,
    agent_task_workspace_id,
    project_workspace_id,
    workspace_id_for_user,
)
from .work_order_repository import PostgresWorkOrderRepository

router = APIRouter(prefix="/agent/tasks", tags=["agent-tasks"])
ReviewerDependency = Depends(require_reviewer)
MaintainerDependency = Depends(require_maintainer)


def audit(
    event_type: str,
    entity_id: str,
    details: dict,
    actor: str = "system",
    workspace_id: str | None = None,
) -> AuditEvent:
    event = AuditEvent(
        event_type=event_type,
        entity_id=entity_id,
        workspace_id=workspace_id,
        details=details,
        actor=actor,
    )
    store.audit_events.append(event)
    return event


def _task_repository_for_route(http_request: Request | None) -> AgentTaskRepository:
    if http_request is None:
        settings = get_settings()
        if settings.is_production or settings.postgres_auth_enabled:
            raise HTTPException(
                status_code=503,
                detail="PostgreSQL AgentTask repository requires an app-bound request",
                headers={"Retry-After": "1"},
            )
        # Direct function calls in unit tests use the explicit local adapter.
        return agent_task_repository
    try:
        repository = agent_task_repository_for_request(http_request)
    except RuntimeError:
        # Repository selection is an authority boundary. A missing or
        # mismatched lifespan binding must not become an unhandled 500 (or
        # trigger a caller-side compatibility fallback).
        raise HTTPException(
            status_code=503,
            detail="AgentTask repository is unavailable",
            headers={"Retry-After": "1"},
        ) from None
    if not isinstance(repository, (SQLiteAgentTaskRepository, PostgresAgentTaskRepository)):
        raise HTTPException(
            status_code=503,
            detail="AgentTask repository is unsupported",
            headers={"Retry-After": "1"},
        )
    return repository


def _approval_repository_for_route(
    http_request: Request | None,
) -> ApprovalDecisionRepository:
    if http_request is None:
        settings = get_settings()
        if settings.is_production or settings.postgres_auth_enabled:
            raise HTTPException(
                status_code=503,
                detail="PostgreSQL approval repository requires an app-bound request",
                headers={"Retry-After": "1"},
            )
        # Direct function calls in unit tests use the explicit local adapter.
        return approval_decision_repository
    return approval_decision_repository_for_request(http_request)


def _project_repository_for_route(http_request: Request | None):
    if http_request is None:
        settings = get_settings()
        if settings.is_production or settings.postgres_auth_enabled:
            raise HTTPException(
                status_code=503,
                detail="PostgreSQL project repository requires an app-bound request",
                headers={"Retry-After": "1"},
            )
        # Direct function calls in unit tests use the explicit local adapter.
        return project_repository
    return project_repository_for_request(http_request)


def _work_order_repository_for_route(http_request: Request | None):
    if http_request is None:
        settings = get_settings()
        if settings.is_production or settings.postgres_auth_enabled:
            raise HTTPException(
                status_code=503,
                detail="PostgreSQL WorkOrder repository requires an app-bound request",
                headers={"Retry-After": "1"},
            )
        # Direct function calls in unit tests use the explicit local adapter.
        return store.work_order_repository
    return work_order_repository_for_request(http_request)


def _task_events(
    task_id: str, repository: AgentTaskRepository | None = None
) -> list[AgentTaskEvent]:
    active_repository = repository if repository is not None else agent_task_repository
    task = active_repository.get(task_id)
    if task is None:
        return []
    return active_repository.list_events(task_id, workspace_id=task.workspace_id)


def _publish_task_transition(
    repository: AgentTaskRepository,
    task: AgentTask,
    event: AgentTaskEvent,
    audit_event: AuditEvent,
    notification: Notification | None = None,
) -> None:
    if isinstance(repository, SQLiteAgentTaskRepository):
        store.project_agent_task_transition(task, event, audit_event, notification)


def _task_for_user(
    task_id: str, user: AuthUser, repository: AgentTaskRepository | None = None,
    http_request: Request | None = None,
) -> AgentTask:
    active_repository = repository if repository is not None else agent_task_repository
    task = active_repository.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Agent task not found")
    workspace_id = (
        workspace_id_for_user(user) if http_request is None
        else workspace_id_for_request(user, http_request)
    )
    if not isinstance(active_repository, SQLiteAgentTaskRepository) and not workspace_id:
        raise HTTPException(status_code=403, detail="Workspace membership is required")
    if isinstance(active_repository, SQLiteAgentTaskRepository):
        scope_consistent = agent_task_scope_is_consistent(task)
        task_workspace_id = agent_task_workspace_id(task)
    else:
        project = (
            _project_repository_for_route(http_request).get(task.project_id)
            if task.project_id else None
        )
        scope_consistent = bool(
            task.workspace_id and project and project.workspace_id == task.workspace_id
        )
        task_workspace_id = task.workspace_id
    if not scope_consistent or (
        workspace_id is not None and task_workspace_id != workspace_id
    ):
        raise HTTPException(status_code=404, detail="Agent task not found")
    if isinstance(active_repository, SQLiteAgentTaskRepository):
        store.agent_tasks[task.id] = task
    return task


def append_system_task_event(
    task: AgentTask,
    event_type: AgentTaskEventType,
    message: str,
    status: AgentTaskStatus | None = None,
    actor: str = "system",
    metadata: dict | None = None,
    notify_worker_status: bool = False,
) -> AgentTaskEvent:
    if status is not None:
        task.status = status
        task.updated_at = now_utc()
    event = AgentTaskEvent(
        task_id=task.id,
        event_type=event_type,
        message=message,
        status=status,
        actor=actor,
        metadata=metadata or {},
    )
    store.agent_task_events[event.id] = event
    if notify_worker_status and status is not None:
        notification = notify_agent_task_worker_status(task)
        if notification:
            event.metadata = {**event.metadata, "notification_id": notification.id}
    return event


def propagate_approval_decision_to_agent_task(
    approval_request: ApprovalRequest,
    previous_status: ApprovalStatus,
    actor: str,
    committed: ApprovalDecisionCommit | None = None,
    task_repository: AgentTaskRepository | None = None,
    approval_repository: ApprovalDecisionRepository | None = None,
    run_repository: AgentRunRepository | None = None,
) -> AgentTask | None:
    if committed is None and isinstance(task_repository, PostgresAgentTaskRepository):
        # PostgreSQL approval decisions must propagate only from the atomic
        # repository commit result. Falling through to the legacy store map
        # would let process-local task state become authoritative.
        raise ApprovalDecisionConflict(
            "PostgreSQL AgentTask approval propagation requires its committed transaction result."
        )
    if committed is not None:
        # The repository transaction is authoritative. PostgreSQL-backed route
        # paths intentionally do not require a process-local task projection.
        task = committed.linked_task
        if task is None:
            return None
        if (
            task.approval_request_id != approval_request.id
            or task.project_id != approval_request.project_id
            or task.workspace_id != approval_request.workspace_id
            or committed.linked_task_event is None
            or committed.linked_task_audit_event is None
            or committed.linked_task_notification is None
        ):
            raise ApprovalDecisionConflict(
                "Committed linked AgentTask records are incomplete or outside approval scope."
            )
        task_event = committed.linked_task_event
        task_audit = committed.linked_task_audit_event
        task_notification = committed.linked_task_notification
        expected_task_state = {
            ApprovalStatus.APPROVED: (AgentTaskStatus.APPROVED, AgentTaskEventType.APPROVED),
            ApprovalStatus.DENIED: (AgentTaskStatus.DENIED, AgentTaskEventType.DENIED),
            ApprovalStatus.REVISION_REQUESTED: (AgentTaskStatus.PLANNING, AgentTaskEventType.NOTE),
        }.get(approval_request.status)
        if (
            expected_task_state is None
            or task.status != expected_task_state[0]
            or task_event.task_id != task.id
            or task_event.event_type != expected_task_state[1]
            or task_event.status != task.status
            or task_event.metadata.get("approval_request_id") != approval_request.id
            or task_event.metadata.get("new_status") != approval_request.status
            or task_audit.event_type != "agent_task.approval_decision_propagated"
            or task_audit.entity_id != task.id
            or task_audit.workspace_id != task.workspace_id
            or task_audit.details.get("approval_request_id") != approval_request.id
            or task_notification.entity_type != "agent_task"
            or task_notification.entity_id != task.id
            or task_notification.workspace_id != task.workspace_id
            or task_event.metadata.get("notification_id") != task_notification.id
            or task_audit.details.get("notification_id") != task_notification.id
        ):
            raise ApprovalDecisionConflict(
                "Committed linked AgentTask decision records failed identity or transition validation."
            )
    else:
        linked_tasks = [
            task
            for task in store.agent_tasks.values()
            if task.approval_request_id == approval_request.id
        ]
        if not linked_tasks:
            return None
        task = linked_tasks[0]
    if approval_request.status == ApprovalStatus.APPROVED:
        event_type = AgentTaskEventType.APPROVED
        task_status = AgentTaskStatus.APPROVED
        message = "Linked approval was approved."
    elif approval_request.status == ApprovalStatus.DENIED:
        event_type = AgentTaskEventType.DENIED
        task_status = AgentTaskStatus.DENIED
        message = "Linked approval was denied."
    elif approval_request.status == ApprovalStatus.REVISION_REQUESTED:
        event_type = AgentTaskEventType.NOTE
        task_status = AgentTaskStatus.PLANNING
        message = "Linked approval requested revision."
    else:
        event_type = AgentTaskEventType.NOTE
        task_status = task.status
        message = (
            f"Linked approval moved from {previous_status} to {approval_request.status}."
        )

    if committed and committed.linked_task_event:
        decision_event = committed.linked_task_event
    else:
        notification = notify_agent_task_approval_decision(task, approval_request)
        decision_event = append_system_task_event(
            task,
            event_type,
            message,
            status=task_status,
            actor=actor,
            metadata={
                "approval_request_id": approval_request.id,
                "previous_status": previous_status,
                "new_status": approval_request.status,
                "notification_id": notification.id,
            },
        )
        audit(
            "agent_task.approval_decision_propagated",
            task.id,
            {
                "approval_request_id": approval_request.id,
                "previous_status": previous_status,
                "new_status": approval_request.status,
                "task_status": task.status,
                "notification_id": notification.id,
            },
            actor=actor,
            workspace_id=task.workspace_id,
        )

    if task_status == AgentTaskStatus.APPROVED:
        if committed is not None and committed.linked_run is not None:
            run = committed.linked_run
            if run.task_id != task.id or run.approval_request_id != approval_request.id:
                raise ApprovalDecisionConflict(
                    "Committed AgentRun is outside the linked task approval scope."
                )
        else:
            if isinstance(run_repository, PostgresAgentRunRepository):
                raise ApprovalDecisionConflict(
                    "PostgreSQL approval commit did not atomically create the linked AgentRun."
                )
            run = create_agent_run(
                AgentRunCreate(
                    task_id=task.id,
                    metadata={
                        "scheduled_from": "approval_decision",
                        "approval_decision_actor": actor,
                    },
                ),
                actor=actor,
                task_repository=task_repository,
                approval_repository=approval_repository,
                run_repository=run_repository,
            )
        if committed is None:
            decision_event.metadata = {
                **decision_event.metadata,
                "agent_run_id": run.id,
                "agent_run_status": run.status,
            }

    # Persist the task decision timeline for denied/revision outcomes as well
    # as the approved-run path, which persists again after run creation.
    if committed is None:
        store.persist()
    return task


@router.post("", response_model=AgentTask)
def create_agent_task(
    payload: AgentTaskCreate,
    user: AuthUser = MaintainerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> AgentTask:
    task_repository = _task_repository_for_route(http_request)
    workspace_id = workspace_id_for_request(user, http_request)
    if not isinstance(task_repository, SQLiteAgentTaskRepository) and not payload.project_id:
        raise HTTPException(status_code=422, detail="PostgreSQL agent tasks require a project")
    project = (
        _project_repository_for_route(http_request).get(payload.project_id)
        if payload.project_id
        else None
    )
    if payload.project_id and (
        project is None
        or (workspace_id is not None and project.workspace_id != workspace_id)
    ):
        raise HTTPException(status_code=404, detail="Project not found")

    task = AgentTask(
        **payload.model_dump(),
        created_by_user_id=user.id,
        workspace_id=project.workspace_id if project else workspace_id,
    )
    task_event = AgentTaskEvent(
        task_id=task.id,
        event_type=AgentTaskEventType.TASK_CREATED,
        message="Agent task received by Aixion.",
        status=task.status,
        actor=user.email,
        metadata={"provider": task.provider, "requested_action": task.requested_action},
    )
    task_audit_event = AuditEvent(
        event_type="agent_task.created",
        entity_id=task.id,
        workspace_id=task.workspace_id,
        actor=user.email,
        details={
            "provider": task.provider,
            "project_id": task.project_id,
            "status": task.status,
            "requested_action": task.requested_action,
            "requires_approval": task.requires_approval,
        },
    )
    try:
        task_repository.create(task=task, event=task_event, audit=task_audit_event)
    except (AgentTaskRepositoryConflict, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(task_repository, SQLiteAgentTaskRepository):
        store.project_agent_task_creation(task, task_event, task_audit_event)
    return task


@router.get("", response_model=list[AgentTask])
def list_agent_tasks(
    provider: AgentProvider | None = None,
    status: AgentTaskStatus | None = None,
    project_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    user: AuthUser = ReviewerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> list[AgentTask]:
    task_repository = _task_repository_for_route(http_request)
    workspace_id = (
        workspace_id_for_user(user) if http_request is None
        else workspace_id_for_request(user, http_request)
    )
    if not isinstance(task_repository, SQLiteAgentTaskRepository) and not workspace_id:
        raise HTTPException(status_code=403, detail="Workspace membership is required")
    tasks = task_repository.list(
        workspace_id=workspace_id,
        provider=provider,
        status=status,
        project_id=project_id,
        limit=limit,
    )
    if isinstance(task_repository, SQLiteAgentTaskRepository):
        tasks = [task for task in tasks if agent_task_scope_is_consistent(task)]
        if workspace_id is not None:
            tasks = [task for task in tasks if agent_task_workspace_id(task) == workspace_id]
    else:
        tasks = [task for task in tasks if task.workspace_id == workspace_id]
    return tasks[:limit]


@router.get("/{task_id}", response_model=AgentTask)
def get_agent_task(
    task_id: str, user: AuthUser = ReviewerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> AgentTask:
    return _task_for_user(
        task_id, user, _task_repository_for_route(http_request), http_request
    )


@router.get("/{task_id}/events", response_model=list[AgentTaskEvent])
def list_agent_task_events(
    task_id: str,
    user: AuthUser = ReviewerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> list[AgentTaskEvent]:
    task_repository = _task_repository_for_route(http_request)
    _task_for_user(task_id, user, task_repository, http_request)
    return _task_events(task_id, task_repository)


@router.get("/{task_id}/cancel", response_model=AgentTaskCancelSummary)
def get_agent_task_cancel_summary(
    task_id: str,
    user: AuthUser = ReviewerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> AgentTaskCancelSummary:
    task_repository = _task_repository_for_route(http_request)
    task = _task_for_user(
        task_id, user, task_repository, http_request
    )
    summary = cancel_summary(task)
    if http_request is not None and agent_run_repository_for_request(
        http_request
    ).get_active_for_task(task.id) is not None:
        return summary.model_copy(
            update={
                "cancel_allowed": False,
                "reason": (
                    "An AgentRun is linked to this task. Cancel or reconcile the run first; "
                    "task cancellation does not stop its worker."
                ),
            }
        )
    return summary


@router.post("/{task_id}/cancel", response_model=AgentTaskEvent)
def cancel_agent_task(
    task_id: str,
    payload: AgentTaskCancelRequest,
    user: AuthUser = MaintainerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> AgentTaskEvent:
    task_repository = _task_repository_for_route(http_request)
    expected_task = _task_for_user(task_id, user, task_repository, http_request)
    summary = cancel_summary(expected_task)
    if not summary.cancel_allowed:
        raise HTTPException(status_code=409, detail=summary.reason)
    if http_request is not None and agent_run_repository_for_request(
        http_request
    ).get_active_for_task(expected_task.id) is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "An AgentRun is linked to this task. Cancel or reconcile the run first; "
                "task cancellation does not stop its worker."
            ),
        )
    expected_approval: ApprovalRequest | None = None
    cancelled_approval: ApprovalRequest | None = None
    approval_audit: AuditEvent | None = None
    approval_notification: Notification | None = None
    if expected_task.status == AgentTaskStatus.WAITING_FOR_APPROVAL:
        if not expected_task.approval_request_id:
            raise HTTPException(status_code=409, detail="Waiting AgentTask has no linked approval.")
        approval_repository = _approval_repository_for_route(http_request)
        expected_approval = approval_repository.get(expected_task.approval_request_id)
        if expected_approval is None:
            raise HTTPException(status_code=409, detail="Linked approval is missing; task cancellation denied.")
        try:
            (
                cancelled_approval,
                approval_audit,
                approval_notification,
            ) = prepare_linked_approval_cancellation(
                expected_task,
                expected_approval,
                actor=user.email,
                reason=payload.reason,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    updated_task = expected_task.model_copy(deep=True)
    event = append_cancel_requested_event(
        updated_task,
        actor=user.email,
        reason=payload.reason,
        approval_decision_changed=cancelled_approval is not None,
    )
    audit_event = audit_cancel_requested(updated_task, actor=user.email, event=event)
    try:
        task_repository.commit_cancel(
            expected=expected_task,
            updated=updated_task,
            event=event,
            audit=audit_event,
            expected_approval=expected_approval,
            cancelled_approval=cancelled_approval,
            approval_audit=approval_audit,
            approval_notification=approval_notification,
        )
    except (AgentTaskRepositoryConflict, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if (
        cancelled_approval is not None
        and isinstance(task_repository, SQLiteAgentTaskRepository)
        and approval_repository_publishes_compatibility_state(
            _approval_repository_for_route(http_request)
        )
    ):
        store.project_agent_task_cancellation(
            updated_task,
            event,
            audit_event,
            cancelled_approval,
            approval_audit,
            approval_notification,
        )
    else:
        _publish_task_transition(task_repository, updated_task, event, audit_event)
    return event


@router.get("/{task_id}/retry", response_model=AgentTaskRetrySummary)
def get_agent_task_retry_summary(
    task_id: str,
    max_retries: int = Query(default=3, ge=0, le=10),
    user: AuthUser = ReviewerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> AgentTaskRetrySummary:
    task_repository = _task_repository_for_route(http_request)
    task = _task_for_user(task_id, user, task_repository, http_request)
    approval = (
        _approval_repository_for_route(http_request).get(task.approval_request_id)
        if task.approval_request_id
        else None
    )
    return retry_summary(
        task,
        max_retries=max_retries,
        repository=task_repository,
        approval=approval,
        require_approved_linked_approval=http_request is not None,
    )


@router.post("/{task_id}/retry", response_model=AgentTaskEvent)
def retry_agent_task(
    task_id: str,
    payload: AgentTaskRetryRequest,
    user: AuthUser = MaintainerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> AgentTaskEvent:
    task_repository = _task_repository_for_route(http_request)
    expected_task = _task_for_user(task_id, user, task_repository, http_request)
    approval = (
        _approval_repository_for_route(http_request).get(expected_task.approval_request_id)
        if expected_task.approval_request_id
        else None
    )
    summary = retry_summary(
        expected_task,
        max_retries=payload.max_retries,
        repository=task_repository,
        approval=approval,
        require_approved_linked_approval=http_request is not None,
    )
    if not summary.retry_allowed:
        raise HTTPException(status_code=409, detail=summary.reason)
    updated_task = expected_task.model_copy(deep=True)
    event = append_retry_requested_event(
        updated_task,
        actor=user.email,
        reason=payload.reason,
        retry_count=summary.retry_count,
        max_retries=payload.max_retries,
    )
    audit_event = audit_retry_requested(updated_task, actor=user.email, event=event)
    try:
        task_repository.commit_retry(
            expected=expected_task,
            updated=updated_task,
            event=event,
            audit=audit_event,
            max_retries=payload.max_retries,
        )
    except (AgentTaskRepositoryConflict, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _publish_task_transition(task_repository, updated_task, event, audit_event)
    return event


@router.post("/{task_id}/events", response_model=AgentTaskEvent)
def append_agent_task_event(
    task_id: str,
    payload: AgentTaskEventCreate,
    user: AuthUser = MaintainerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> AgentTaskEvent:
    task_repository = _task_repository_for_route(http_request)
    expected_task = _task_for_user(task_id, user, task_repository, http_request)
    updated_task = expected_task.model_copy(deep=True)
    if payload.status is not None:
        updated_task.status = payload.status
        updated_task.updated_at = now_utc()
    notification = (
        build_agent_task_worker_status_notification(
            updated_task,
            workspace_id=updated_task.workspace_id,
        )
        if payload.status is not None
        else None
    )
    metadata = dict(payload.metadata)
    metadata.pop("notification_id", None)
    if notification is not None:
        metadata["notification_id"] = notification.id
    event = AgentTaskEvent(
        task_id=updated_task.id,
        event_type=payload.event_type,
        message=payload.message,
        status=payload.status,
        actor=user.email,
        metadata=metadata,
    )
    audit_event = AuditEvent(
        event_type="agent_task.event_recorded",
        entity_id=updated_task.id,
        workspace_id=updated_task.workspace_id,
        actor=user.email,
        details={
            "event_id": event.id,
            "event_type": event.event_type,
            "status": event.status,
            "message": event.message,
            "notification_id": notification.id if notification else None,
        },
    )
    try:
        task_repository.commit_event(
            expected=expected_task,
            updated=updated_task,
            event=event,
            audit=audit_event,
            notification=notification,
        )
    except (AgentTaskRepositoryConflict, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _publish_task_transition(task_repository, updated_task, event, audit_event, notification)
    return event


@router.post("/{task_id}/approval", response_model=ApprovalRequest)
def create_agent_task_approval(
    task_id: str,
    payload: ApprovalRequestCreate,
    user: AuthUser = MaintainerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> ApprovalRequest:
    task_repository = _task_repository_for_route(http_request)
    approval_repository = _approval_repository_for_route(http_request)
    if approval_repository_publishes_compatibility_state(approval_repository) != isinstance(
        task_repository, SQLiteAgentTaskRepository
    ):
        raise HTTPException(
            status_code=503,
            detail="Approval and AgentTask persistence adapters are not aligned",
        )
    work_order_repository = _work_order_repository_for_route(http_request)
    workspace_id = workspace_id_for_request(user, http_request)
    task = _task_for_user(task_id, user, task_repository, http_request)
    if task.approval_request_id:
        raise HTTPException(status_code=409, detail="Agent task already has a linked approval")
    project = _project_repository_for_route(http_request).get(payload.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if workspace_id is not None and project.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Project not found")
    if task.project_id and payload.project_id != task.project_id:
        raise HTTPException(
            status_code=409,
            detail="Approval project must match agent task project",
        )
    if payload.work_order_id:
        work_order = work_order_repository.get(payload.work_order_id)
        if isinstance(work_order_repository, PostgresWorkOrderRepository):
            # A PostgreSQL row is authoritative for its workspace. Do not fill
            # a missing indexed workspace from the module-level compatibility
            # project repository, which may still be SQLite.
            work_order_workspace_matches = bool(
                work_order
                and work_order.workspace_id
                and work_order.workspace_id == project.workspace_id
            )
        else:
            # Preserve the legacy local adapter's compatibility with older
            # WorkOrders whose payload omitted workspace_id.
            work_order_workspace_matches = bool(
                work_order
                and (work_order.workspace_id or project_workspace_id(work_order.project_id))
                == project.workspace_id
            )
        if (
            work_order is None
            or work_order.project_id != project.id
            or not work_order_workspace_matches
        ):
            raise HTTPException(status_code=404, detail="Work order not found")

    risk = assess_approval_request(payload)
    status = ApprovalStatus.BLOCKED if risk.blocked else ApprovalStatus.REQUESTED
    request = ApprovalRequest(
        **payload.model_dump(
            exclude={
                "source_provider",
                "source_agent_id",
                "source_agent_name",
                "source_session_id",
                "source_task_url",
            }
        ),
        risk=risk,
        status=status,
        source_provider=task.provider,
        source_agent_id=task.external_agent_id,
        source_agent_name=task.external_agent_name or task.provider,
        source_session_id=task.source_session_id,
        source_task_url=task.source_url,
        created_by_user_id=user.id,
        workspace_id=project.workspace_id,
        verified_source=(
            task.provider != AgentProvider.MANUAL or task.external_agent_id is not None
        ),
    )
    task_candidate = task.model_copy(
        update={
            "approval_request_id": request.id,
            "status": AgentTaskStatus.WAITING_FOR_APPROVAL,
            "updated_at": now_utc(),
        }
    )
    notification = Notification(
        title=f"Approval needed: {task_candidate.title}",
        body=f"{request.risk.level} risk approval is waiting for review from {task_candidate.provider}.",
        entity_type="agent_task",
        entity_id=task_candidate.id,
        user_id=task_candidate.created_by_user_id,
        workspace_id=request.workspace_id,
        approval_request_id=request.id,
    )
    task_event = AgentTaskEvent(
        task_id=task_candidate.id,
        event_type=AgentTaskEventType.APPROVAL_CREATED,
        message="Approval request created for agent task.",
        status=task_candidate.status,
        actor=user.email,
        metadata={
            "approval_request_id": request.id,
            "risk_level": request.risk.level,
            "notification_id": notification.id,
        },
    )
    approval_audit_event = AuditEvent(
        event_type="approval.created",
        entity_id=request.id,
        workspace_id=request.workspace_id,
        actor=user.email,
        details={
            "project_id": request.project_id,
            "risk_level": request.risk.level,
            "status": request.status,
            "blocked": request.risk.blocked,
            "source_provider": request.source_provider,
            "source_agent_id": request.source_agent_id,
            "source_agent_name": request.source_agent_name,
            "verified_source": request.verified_source,
            "created_by_user_id": request.created_by_user_id,
        },
    )
    task_audit_event = AuditEvent(
        event_type="agent_task.approval_created",
        entity_id=task_candidate.id,
        workspace_id=task_candidate.workspace_id,
        actor=user.email,
        details={
            "approval_request_id": request.id,
            "project_id": request.project_id,
            "risk_level": request.risk.level,
            "approval_status": request.status,
            "task_status": task_candidate.status,
            "notification_id": notification.id,
        },
    )
    try:
        approval_repository.create_for_agent_task(
            approval=request,
            audit_event=approval_audit_event,
            task=task_candidate,
            task_event=task_event,
            task_audit_event=task_audit_event,
            notification=notification,
        )
    except ApprovalDecisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if (
        approval_repository_publishes_compatibility_state(approval_repository)
        and isinstance(task_repository, SQLiteAgentTaskRepository)
    ):
        store.approval_requests[request.id] = request
        store.agent_tasks[task_candidate.id] = task_candidate
        store.agent_task_events[task_event.id] = task_event
        store.audit_events.extend((approval_audit_event, task_audit_event))
        store.notifications[notification.id] = notification
    return request
