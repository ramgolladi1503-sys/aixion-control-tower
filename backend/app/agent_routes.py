from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .agent_auth import (
    assert_agent_can,
    register_external_agent,
    require_external_agent,
    revoke_external_agent_token,
    rotate_external_agent_token,
    to_credential_status,
    to_public_agent,
)
from .agent_credential_models import (
    AgentCreateWithCredentialPolicy,
    AgentCredentialStatus,
    AgentTokenRotateRequest,
)
from .agent_task_models import (
    AgentTask,
    AgentTaskCreate,
    AgentTaskEvent,
    AgentTaskEventCreate,
    AgentTaskEventType,
)
from .auth import require_owner
from .models import (
    AgentAction,
    AgentRegistrationResponse,
    AgentWorkOrderCreate,
    ApprovalRequest,
    ApprovalRequestCreate,
    ApprovalStatus,
    AuditEvent,
    AuthUser,
    ExternalAgent,
    ExternalAgentPublic,
    WorkOrder,
    WorkOrderSourceType,
    now_utc,
)
from .notifications import create_notification
from .risk_engine import assess_approval_request, assess_work_order
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


def _get_agent_or_404(agent_id: str) -> ExternalAgent:
    agent = store.external_agents.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="External agent not found")
    return agent


@router.post("", response_model=AgentRegistrationResponse)
def create_agent(
    payload: AgentCreateWithCredentialPolicy,
    user: AuthUser = OwnerDependency,
) -> AgentRegistrationResponse:
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
            "credential_state": to_credential_status(
                _get_agent_or_404(response.agent.id)
            ).credential_state,
        },
        actor=user.email,
    )
    store.persist()
    return response


@router.get("", response_model=list[ExternalAgentPublic])
def list_agents(_: AuthUser = OwnerDependency) -> list[ExternalAgentPublic]:
    return [to_public_agent(agent) for agent in store.external_agents.values()]


@router.post("/approvals", response_model=ApprovalRequest)
def create_agent_approval(
    payload: ApprovalRequestCreate,
    agent: ExternalAgent = Depends(require_external_agent),
) -> ApprovalRequest:
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
        body=(
            f"{request.risk.level} risk request from "
            f"{agent.display_name} on {request.target_branch}"
        ),
        entity_type="approval_request",
        entity_id=request.id,
    )
    store.persist()
    return request


@router.post("/work-orders", response_model=WorkOrder)
def create_agent_work_order(
    payload: AgentWorkOrderCreate,
    agent: ExternalAgent = Depends(require_external_agent),
) -> WorkOrder:
    assert_agent_can(agent, AgentAction.CREATE_WORK_ORDER, project_id=payload.project_id)
    if payload.project_id not in store.projects:
        raise HTTPException(status_code=404, detail="Project not found")
    if payload.idea_id and payload.idea_id not in store.ideas:
        raise HTTPException(status_code=404, detail="Idea not found")

    risk_level = assess_work_order(payload)
    work_order = WorkOrder(
        **payload.model_dump(),
        risk_level=risk_level,
        source_type=WorkOrderSourceType.AGENT_TASK,
        source_provider=agent.provider,
        source_agent_id=agent.id,
        source_agent_name=agent.display_name,
        verified_source=True,
    )
    store.work_orders[work_order.id] = work_order
    audit(
        "work_order.created_by_agent",
        work_order.id,
        {
            "project_id": work_order.project_id,
            "risk_level": work_order.risk_level,
            "source_type": work_order.source_type,
            "source_provider": work_order.source_provider,
            "source_agent_id": work_order.source_agent_id,
            "source_agent_name": work_order.source_agent_name,
            "source_task_id": work_order.source_task_id,
            "source_session_id": work_order.source_session_id,
            "verified_source": work_order.verified_source,
        },
        actor=f"agent:{agent.id}",
    )
    store.persist()
    return work_order


@router.post("/tasks", response_model=AgentTask)
def create_agent_task(
    payload: AgentTaskCreate,
    agent: ExternalAgent = Depends(require_external_agent),
) -> AgentTask:
    assert_agent_can(
        agent,
        AgentAction.CREATE_AGENT_TASK,
        project_id=payload.project_id,
        repository_full_name=payload.repository,
    )
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
        metadata={
            "provider": agent.provider,
            "agent_id": agent.id,
            "requested_action": task.requested_action,
        },
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
def get_agent_owned_task(
    task_id: str,
    agent: ExternalAgent = Depends(require_external_agent),
) -> AgentTask:
    assert_agent_can(agent, AgentAction.READ_AGENT_TASK)
    task = store.agent_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Agent task not found")
    _assert_agent_owns_task(agent, task)
    return task


@router.get("/tasks/{task_id}/events", response_model=list[AgentTaskEvent])
def list_agent_owned_task_events(
    task_id: str,
    agent: ExternalAgent = Depends(require_external_agent),
) -> list[AgentTaskEvent]:
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


@router.get("/{agent_id}/credentials", response_model=AgentCredentialStatus)
def get_agent_credentials(
    agent_id: str,
    _: AuthUser = OwnerDependency,
) -> AgentCredentialStatus:
    return to_credential_status(_get_agent_or_404(agent_id))


@router.post("/{agent_id}/token/rotate", response_model=AgentRegistrationResponse)
def rotate_agent_token(
    agent_id: str,
    payload: AgentTokenRotateRequest | None = None,
    user: AuthUser = OwnerDependency,
) -> AgentRegistrationResponse:
    agent = _get_agent_or_404(agent_id)
    response = rotate_external_agent_token(
        agent,
        token_expires_at=payload.token_expires_at if payload else None,
        rate_limit_per_minute=payload.rate_limit_per_minute if payload else None,
    )
    credential_status = to_credential_status(agent)
    audit(
        "agent.token_rotated",
        agent.id,
        {
            "credential_state": credential_status.credential_state,
            "token_expires_at": credential_status.token_expires_at,
            "token_rotated_at": credential_status.token_rotated_at,
            "rate_limit_per_minute": credential_status.rate_limit_per_minute,
        },
        actor=user.email,
    )
    store.persist()
    return response


@router.post("/{agent_id}/token/revoke", response_model=ExternalAgentPublic)
def revoke_agent_token(agent_id: str, user: AuthUser = OwnerDependency) -> ExternalAgentPublic:
    agent = _get_agent_or_404(agent_id)
    public_agent = revoke_external_agent_token(agent)
    credential_status = to_credential_status(agent)
    audit(
        "agent.token_revoked",
        agent.id,
        {
            "credential_state": credential_status.credential_state,
            "token_revoked_at": credential_status.token_revoked_at,
        },
        actor=user.email,
    )
    store.persist()
    return public_agent


@router.get("/{agent_id}", response_model=ExternalAgentPublic)
def get_agent(agent_id: str, _: AuthUser = OwnerDependency) -> ExternalAgentPublic:
    return to_public_agent(_get_agent_or_404(agent_id))
