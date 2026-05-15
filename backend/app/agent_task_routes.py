from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from .agent_task_models import (
    AgentTask,
    AgentTaskCreate,
    AgentTaskEvent,
    AgentTaskEventCreate,
    AgentTaskEventType,
    AgentTaskStatus,
)
from .agent_task_notifications import (
    notify_agent_task_approval_created,
    notify_agent_task_approval_decision,
    notify_agent_task_worker_status,
)
from .agent_task_retry import (
    AgentTaskRetryRequest,
    AgentTaskRetrySummary,
    append_retry_requested_event,
    audit_retry_requested,
    retry_summary,
)
from .auth import require_maintainer, require_reviewer
from .models import AgentProvider, ApprovalRequest, ApprovalRequestCreate, ApprovalStatus, AuditEvent, AuthUser, now_utc
from .risk_engine import assess_approval_request
from .store import store

router = APIRouter(prefix="/agent/tasks", tags=["agent-tasks"])
ReviewerDependency = Depends(require_reviewer)
MaintainerDependency = Depends(require_maintainer)


def audit(event_type: str, entity_id: str, details: dict, actor: str = "system") -> AuditEvent:
    event = AuditEvent(event_type=event_type, entity_id=entity_id, details=details, actor=actor)
    store.audit_events.append(event)
    return event


def _task_events(task_id: str) -> list[AgentTaskEvent]:
    return [event for event in store.agent_task_events.values() if event.task_id == task_id]


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
) -> AgentTask | None:
    linked_tasks = [task for task in store.agent_tasks.values() if task.approval_request_id == approval_request.id]
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
        message = f"Linked approval moved from {previous_status} to {approval_request.status}."

    notification = notify_agent_task_approval_decision(task, approval_request)
    append_system_task_event(
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
    )
    return task


@router.post("", response_model=AgentTask)
def create_agent_task(payload: AgentTaskCreate, user: AuthUser = MaintainerDependency) -> AgentTask:
    if payload.project_id and payload.project_id not in store.projects:
        raise HTTPException(status_code=404, detail="Project not found")

    task = AgentTask(**payload.model_dump(), created_by_user_id=user.id)
    store.agent_tasks[task.id] = task

    append_system_task_event(
        task,
        AgentTaskEventType.TASK_CREATED,
        "Agent task received by Aixion.",
        status=task.status,
        actor=user.email,
        metadata={"provider": task.provider, "requested_action": task.requested_action},
    )
    audit(
        "agent_task.created",
        task.id,
        {
            "provider": task.provider,
            "project_id": task.project_id,
            "status": task.status,
            "requested_action": task.requested_action,
            "requires_approval": task.requires_approval,
        },
        actor=user.email,
    )
    store.persist()
    return task


@router.get("", response_model=list[AgentTask])
def list_agent_tasks(
    provider: AgentProvider | None = None,
    status: AgentTaskStatus | None = None,
    project_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    _: AuthUser = ReviewerDependency,
) -> list[AgentTask]:
    tasks = list(store.agent_tasks.values())
    if provider is not None:
        tasks = [task for task in tasks if task.provider == provider]
    if status is not None:
        tasks = [task for task in tasks if task.status == status]
    if project_id is not None:
        tasks = [task for task in tasks if task.project_id == project_id]
    return sorted(tasks, key=lambda task: task.updated_at, reverse=True)[:limit]


@router.get("/{task_id}", response_model=AgentTask)
def get_agent_task(task_id: str, _: AuthUser = ReviewerDependency) -> AgentTask:
    task = store.agent_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Agent task not found")
    return task


@router.get("/{task_id}/events", response_model=list[AgentTaskEvent])
def list_agent_task_events(task_id: str, _: AuthUser = ReviewerDependency) -> list[AgentTaskEvent]:
    if task_id not in store.agent_tasks:
        raise HTTPException(status_code=404, detail="Agent task not found")
    return sorted(_task_events(task_id), key=lambda event: event.created_at)


@router.get("/{task_id}/retry", response_model=AgentTaskRetrySummary)
def get_agent_task_retry_summary(
    task_id: str,
    max_retries: int = Query(default=3, ge=0, le=10),
    _: AuthUser = ReviewerDependency,
) -> AgentTaskRetrySummary:
    task = store.agent_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Agent task not found")
    return retry_summary(task, max_retries=max_retries)


@router.post("/{task_id}/retry", response_model=AgentTaskEvent)
def retry_agent_task(
    task_id: str,
    payload: AgentTaskRetryRequest,
    user: AuthUser = MaintainerDependency,
) -> AgentTaskEvent:
    task = store.agent_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Agent task not found")
    summary = retry_summary(task, max_retries=payload.max_retries)
    if not summary.retry_allowed:
        raise HTTPException(status_code=409, detail=summary.reason)
    event = append_retry_requested_event(
        task,
        actor=user.email,
        reason=payload.reason,
        retry_count=summary.retry_count,
        max_retries=payload.max_retries,
    )
    audit_retry_requested(task, actor=user.email, event=event)
    store.persist()
    return event


@router.post("/{task_id}/events", response_model=AgentTaskEvent)
def append_agent_task_event(
    task_id: str,
    payload: AgentTaskEventCreate,
    user: AuthUser = MaintainerDependency,
) -> AgentTaskEvent:
    task = store.agent_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Agent task not found")

    event = append_system_task_event(
        task,
        payload.event_type,
        payload.message,
        status=payload.status,
        actor=user.email,
        metadata=payload.metadata,
        notify_worker_status=True,
    )
    audit(
        "agent_task.event_recorded",
        task.id,
        {
            "event_type": event.event_type,
            "status": event.status,
            "message": event.message,
            "notification_id": event.metadata.get("notification_id"),
        },
        actor=user.email,
    )
    store.persist()
    return event


@router.post("/{task_id}/approval", response_model=ApprovalRequest)
def create_agent_task_approval(
    task_id: str,
    payload: ApprovalRequestCreate,
    user: AuthUser = MaintainerDependency,
) -> ApprovalRequest:
    task = store.agent_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Agent task not found")
    if task.approval_request_id:
        raise HTTPException(status_code=409, detail="Agent task already has a linked approval")
    if payload.project_id not in store.projects:
        raise HTTPException(status_code=404, detail="Project not found")
    if task.project_id and payload.project_id != task.project_id:
        raise HTTPException(status_code=409, detail="Approval project must match agent task project")
    if payload.work_order_id and payload.work_order_id not in store.work_orders:
        raise HTTPException(status_code=404, detail="Work order not found")

    risk = assess_approval_request(payload)
    status = ApprovalStatus.BLOCKED if risk.blocked else ApprovalStatus.REQUESTED
    request = ApprovalRequest(
        **payload.model_dump(exclude={"source_provider", "source_agent_id", "source_agent_name", "source_session_id", "source_task_url"}),
        risk=risk,
        status=status,
        source_provider=task.provider,
        source_agent_id=task.external_agent_id,
        source_agent_name=task.external_agent_name or task.provider,
        source_session_id=task.source_session_id,
        source_task_url=task.source_url,
        created_by_user_id=user.id,
        verified_source=task.provider != AgentProvider.MANUAL or task.external_agent_id is not None,
    )
    store.approval_requests[request.id] = request
    task.approval_request_id = request.id
    task.status = AgentTaskStatus.WAITING_FOR_APPROVAL
    task.updated_at = now_utc()
    notification = notify_agent_task_approval_created(task, request)

    append_system_task_event(
        task,
        AgentTaskEventType.APPROVAL_CREATED,
        "Approval request created for agent task.",
        status=task.status,
        actor=user.email,
        metadata={"approval_request_id": request.id, "risk_level": request.risk.level, "notification_id": notification.id},
    )
    audit(
        "agent_task.approval_created",
        task.id,
        {
            "approval_request_id": request.id,
            "project_id": request.project_id,
            "risk_level": request.risk.level,
            "approval_status": request.status,
            "task_status": task.status,
            "notification_id": notification.id,
        },
        actor=user.email,
    )
    store.persist()
    return request
