from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from .agent_task_models import (
    AgentTask,
    AgentTaskCreate,
    AgentTaskEvent,
    AgentTaskEventCreate,
    AgentTaskStatus,
)
from .auth import require_maintainer, require_reviewer
from .models import AgentProvider, AuditEvent, AuthUser, now_utc
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


@router.post("", response_model=AgentTask)
def create_agent_task(payload: AgentTaskCreate, user: AuthUser = MaintainerDependency) -> AgentTask:
    if payload.project_id and payload.project_id not in store.projects:
        raise HTTPException(status_code=404, detail="Project not found")

    task = AgentTask(**payload.model_dump(), created_by_user_id=user.id)
    store.agent_tasks[task.id] = task

    event = AgentTaskEvent(
        task_id=task.id,
        event_type="TASK_CREATED",
        message="Agent task received by Aixion.",
        status=task.status,
        actor=user.email,
        metadata={"provider": task.provider, "requested_action": task.requested_action},
    )
    store.agent_task_events[event.id] = event
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


@router.post("/{task_id}/events", response_model=AgentTaskEvent)
def append_agent_task_event(
    task_id: str,
    payload: AgentTaskEventCreate,
    user: AuthUser = MaintainerDependency,
) -> AgentTaskEvent:
    task = store.agent_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Agent task not found")

    if payload.status is not None:
        task.status = payload.status
        task.updated_at = now_utc()

    event = AgentTaskEvent(**payload.model_dump(), task_id=task.id, actor=user.email)
    store.agent_task_events[event.id] = event
    audit(
        "agent_task.event_recorded",
        task.id,
        {"event_type": event.event_type, "status": event.status, "message": event.message},
        actor=user.email,
    )
    store.persist()
    return event
