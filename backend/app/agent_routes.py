from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .agent_auth import assert_agent_can, register_external_agent, require_external_agent, to_public_agent
from .agent_task_models import AgentTask, AgentTaskCreate, AgentTaskEvent, AgentTaskEventCreate, AgentTaskEventType
from .auth import require_owner
from .models import (
    AgentAction,
    AgentCreate,
    AgentRegistrationResponse,
    ApprovalRequest,
    ApprovalRequestCreate,
    ApprovalStatus,
    AuditEvent,
    AuthUser,
    ExternalAgent,
    ExternalAgentPublic,
    now_utc,
)
from .notifications import create_notification
from .risk_engine import assess_approval_request
from .store import store

router = APIRouter(prefix="/agents", tags=["agents"])
OwnerDependency = Depends(require_owner)


def audit(event_type: str, entity_id: str, details: dict, actor: str = "system") -> AuditEvent:
    event = AuditEvent(event_type=event_type, entity_id=entity_id, details=details, actor=actor)
    store.audit_events.append(event)
    return event


def _task_events(task_id: str) -> list[AgentTaskEvent]:
    return [event for event in store.agent_task_events.values() if event.task_id == task_id]


def _assert_agent_owns_task(agent: ExternalAgent, task: AgentTask) -> None:
    if task.external_agent_id != agent.id:
        raise HTTPException(status_code=403, detail="Agent is not allowed for this task")


@router.post("", response_model=AgentRegistrationResponse)
def create_agent(payload: AgentCreate, user: AuthUser = OwnerDependency) -> AgentRegistrationResponse:
    response = register_external_agent(payload, user)
    audit(
        "agent.registered",
        response.agent.id,
        {
            "provider": response.agent.provider,
            "display_name": response.agent.display_name,
            "auth_type": response.agent.auth_type,
            "allowed_project_ids": response.agent.allowed_project_ids,
            "allowed_repositories": response.agent.allowed_repositories,
            "allowed_actions": response.agent.allowed_actions,
        },
        actor=user.email,
    )
    return response


@router.get("", response_model=list[ExternalAgentPublic])
def list_agents(_: AuthUser = OwnerDependency) -> list[ExternalAgentPublic]:
    return [to_public_agent(agent) for agent in store.external_agents.values()]


@router.post("/approvals", response_model=ApprovalRequest)
def create_agent_approval(payload: ApprovalRequestCreate, agent: ExternalAgent = Depends(require_external_agent)) -> ApprovalRequest:
    assert_agent_can(agent, AgentAction.CREATE_APPROVAL, project_id=payload.project_id)
    if payload.project_id not in store.projects:
        raise HTTPException(status_code=404, detail="Project not found")
    if payload.work_order_id and payload.work_order_id not in store.work_orders:
        raise HTTPException(status_code=404, detail="Work order not found")

    risk = assess_approval_request(payload)
    status = ApprovalStatus.BLOCKED if risk.blocked else ApprovalStatus.PENDING_REVIEW
    request = ApprovalRequest(
        **payload.model_dump(exclude={"source_provider", "source_agent_id", "source_agent_name"}),
        risk=risk,
        status=status,
        source_provider=agent.provider,
        source_agent_id=agent.id,
        source_agent_name=agent.display_name,
        verified_source=True,
    )
    store.approval_requests[request.id] = request
    audit(
        "approval.created_by_agent",
        request.id,
        {
            "project_id": request.project_id,
            "risk_level": request.risk.level,
            "status": request.status,
            "blocked": request.risk.blocked,
            "source_provider": request.source_provider,
            "source_agent_id": request.source_agent_id,
            "source_agent_name": request.source_agent_name,
            "source_session_id": request.source_session_id,
            "source_task_url": request.source_task_url,
            "verified_source": request.verified_source,
        },
        actor=f"agent:{agent.id}",
    )
    create_notification(
        title=f"Approval from {agent.provider}: {request.title}",
        body=f"{request.risk.level} risk request from {agent.display_name} on {request.target_branch}",
        entity_type="approval_request",
        entity_id=request.id,
    )
    store.persist()
    return request


@router.post("/tasks", response_model=AgentTask)
def create_agent_task(payload: AgentTaskCreate, agent: ExternalAgent = Depends(require_external_agent)) -> AgentTask:
    assert_agent_can(agent, AgentAction.CREATE_AGENT_TASK, project_id=payload.project_id, repository_full_name=payload.repository)
    if payload.project_id and payload.project_id not in store.projects:
        raise HTTPException(status_code=404, detail="Project not found")

    task_payload = payload.model_dump(exclude={"provider", "metadata"})
    task = AgentTask(
        **task_payload,
        provider=agent.provider,
        external_agent_id=agent.id,
        external_agent_name=agent.display_name,
        metadata={**(payload.metadata or {}), "external_agent_scoped": True},
    )
    store.agent_tasks[task.id] = task
    event = AgentTaskEvent(
        task_id=task.id,
        event_type=AgentTaskEventType.TASK_CREATED,
        message="Agent task received from scoped external agent.",
        status=task.status,
        actor=f"agent:{agent.id}",
        metadata={"provider": agent.provider, "agent_id": agent.id, "requested_action": task.requested_action},
    )
    store.agent_task_events[event.id] = event
    audit(
        "agent_task.created_by_agent",
        task.id,
        {
            "provider": agent.provider,
            "agent_id": agent.id,
            "project_id": task.project_id,
            "repository": task.repository,
            "requested_action": task.requested_action,
            "requires_approval": task.requires_approval,
        },
        actor=f"agent:{agent.id}",
    )
    store.persist()
    return task


@router.get("/tasks/{task_id}", response_model=AgentTask)
def get_agent_owned_task(task_id: str, agent: ExternalAgent = Depends(require_external_agent)) -> AgentTask:
    assert_agent_can(agent, AgentAction.READ_AGENT_TASK)
    task = store.agent_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Agent task not found")
    _assert_agent_owns_task(agent, task)
    return task


@router.get("/tasks/{task_id}/events", response_model=list[AgentTaskEvent])
def list_agent_owned_task_events(task_id: str, agent: ExternalAgent = Depends(require_external_agent)) -> list[AgentTaskEvent]:
    assert_agent_can(agent, AgentAction.READ_AGENT_TASK)
    task = store.agent_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Agent task not found")
    _assert_agent_owns_task(agent, task)
    return sorted(_task_events(task_id), key=lambda event: event.created_at)


@router.post("/tasks/{task_id}/events", response_model=AgentTaskEvent)
def append_agent_owned_task_event(
    task_id: str,
    payload: AgentTaskEventCreate,
    agent: ExternalAgent = Depends(require_external_agent),
) -> AgentTaskEvent:
    assert_agent_can(agent, AgentAction.APPEND_AGENT_TASK_EVENT)
    task = store.agent_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Agent task not found")
    _assert_agent_owns_task(agent, task)
    if payload.status is not None:
        task.status = payload.status
        task.updated_at = now_utc()
    event = AgentTaskEvent(
        task_id=task.id,
        event_type=payload.event_type,
        message=payload.message,
        status=payload.status,
        actor=f"agent:{agent.id}",
        metadata={**(payload.metadata or {}), "agent_id": agent.id, "external_agent_scoped": True},
    )
    store.agent_task_events[event.id] = event
    audit(
        "agent_task.event_recorded_by_agent",
        task.id,
        {"event_type": event.event_type, "status": event.status, "agent_id": agent.id},
        actor=f"agent:{agent.id}",
    )
    store.persist()
    return event


@router.get("/{agent_id}", response_model=ExternalAgentPublic)
def get_agent(agent_id: str, _: AuthUser = OwnerDependency) -> ExternalAgentPublic:
    agent = store.external_agents.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="External agent not found")
    return to_public_agent(agent)
