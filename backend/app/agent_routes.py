from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .agent_auth import assert_agent_can, register_external_agent, require_external_agent, to_public_agent
from .auth import require_user
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
)
from .notifications import create_notification
from .risk_engine import assess_approval_request
from .store import store

router = APIRouter(prefix="/agents", tags=["agents"])


def audit(event_type: str, entity_id: str, details: dict, actor: str = "system") -> AuditEvent:
    event = AuditEvent(event_type=event_type, entity_id=entity_id, details=details, actor=actor)
    store.audit_events.append(event)
    return event


@router.post("", response_model=AgentRegistrationResponse)
def create_agent(payload: AgentCreate, user: AuthUser = Depends(require_user)) -> AgentRegistrationResponse:
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
def list_agents(_: AuthUser = Depends(require_user)) -> list[ExternalAgentPublic]:
    return [to_public_agent(agent) for agent in store.external_agents.values()]


@router.get("/{agent_id}", response_model=ExternalAgentPublic)
def get_agent(agent_id: str, _: AuthUser = Depends(require_user)) -> ExternalAgentPublic:
    agent = store.external_agents.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="External agent not found")
    return to_public_agent(agent)


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
