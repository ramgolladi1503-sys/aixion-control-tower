from __future__ import annotations

import hashlib
import sqlite3
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from .agent_auth import (
    agent_auth_repository,
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
from .agent_task_repository import (
    AgentTaskRepositoryConflict,
    PostgresAgentTaskRepository,
    SQLiteAgentTaskRepository,
)
from .approval_expiry import (
    approval_repository_publishes_compatibility_state,
    approval_repository_scope,
    expire_approval,
    expire_due_approval,
)
from .application_repository_access import (
    agent_task_repository_for_request,
    approval_decision_repository_for_request,
    external_agent_auth_repository_for_request,
    idea_repository_for_request,
    is_sqlite_external_agent_auth_repository,
    project_repository_for_request,
    workspace_membership_repository_for_request,
    work_order_repository_for_request,
    workspace_id_for_request,
)
from .approval_decisions import (
    agent_task_repository,
    approval_decision_repository,
    project_repository,
)
from .approval_repository import (
    ApprovalDecisionConflict,
    ApprovalDeliveryCredentialRejected,
    ApprovalInvocationLeaseRejected,
)
from .approval_integrity import compute_approval_payload_hash
from .idea_repository import IdeaRepository, PostgresIdeaRepository
from .auth import require_owner
from .models import (
    AgentAction,
    AgentProvider,
    AgentRegistrationResponse,
    AgentWorkOrderCreate,
    ApprovalRequest,
    ApprovalRequestCreate,
    ApprovalStatus,
    AuditEvent,
    AuthUser,
    ExternalAgent,
    ExternalAgentPublic,
    Notification,
    WorkOrder,
    WorkOrderSourceType,
    now_utc,
)
from .risk_engine import assess_approval_request, assess_work_order
from .settings import get_settings
from .store import store
from .work_order_repository import (
    PostgresWorkOrderRepository,
    SQLiteWorkOrderRepository,
    WorkOrderRepositoryError,
)
from .workspace_access import (
    external_agent_scope_is_consistent,
    work_order_scope_is_consistent,
    work_order_workspace_id,
    workspace_id_for_user,
)

router = APIRouter(prefix="/agents", tags=["agents"])
OwnerDependency = Depends(require_owner)
APPROVAL_PROTOCOL_VERSION = "1"
APPROVAL_CONNECTOR_ACTIONS = frozenset({
    AgentAction.CREATE_APPROVAL,
    AgentAction.READ_APPROVAL_DECISION,
    AgentAction.CONSUME_APPROVAL_DECISION,
    AgentAction.REPORT_APPROVAL_DELIVERY_FAILURE,
    AgentAction.EXPIRE_OWN_APPROVAL,
})


class AgentConnectorIdentity(BaseModel):
    agent_id: str
    provider: AgentProvider
    workspace_id: str | None
    project_id: str


def require_approval_protocol_v1(
    protocol_version: str | None = Header(
        default=APPROVAL_PROTOCOL_VERSION,
        alias="X-Aixion-Approval-Protocol",
    ),
) -> None:
    """Reject explicit protocol versions this server does not implement.

    Missing headers are treated as v1 for compatibility with previously
    installed local hooks. New hooks send the version explicitly.
    """
    if protocol_version != APPROVAL_PROTOCOL_VERSION:
        raise HTTPException(
            status_code=426,
            detail="Unsupported approval protocol version",
            headers={"X-Aixion-Supported-Approval-Protocols": APPROVAL_PROTOCOL_VERSION},
        )


def _require_request_for_authoritative_repository(
    http_request: Request | None,
    repository_name: str,
) -> None:
    """Prevent direct route calls from selecting module-level SQLite adapters."""
    settings = get_settings()
    if http_request is None and (settings.is_production or settings.postgres_auth_enabled):
        raise HTTPException(
            status_code=503,
            detail=f"{repository_name} repository is unavailable",
            headers={"Retry-After": "1"},
        )


def _approval_repository_for_route(
    http_request: Request | None,
):
    if http_request is None:
        # Direct calls in unit tests use the local/test adapter.
        _require_request_for_authoritative_repository(http_request, "Approval")
        return approval_decision_repository
    return approval_decision_repository_for_request(http_request)


def _project_repository_for_route(http_request: Request | None):
    if http_request is None:
        _require_request_for_authoritative_repository(http_request, "Project")
        return project_repository
    return project_repository_for_request(http_request)


def _work_order_repository_for_route(http_request: Request | None):
    if http_request is None:
        # Direct function calls in unit tests use the explicit local adapter.
        _require_request_for_authoritative_repository(http_request, "Work order")
        return store.work_order_repository
    return work_order_repository_for_request(http_request)


def _agent_task_repository_for_route(http_request: Request | None):
    settings = get_settings()
    if http_request is None and (settings.is_production or settings.postgres_auth_enabled):
        # A direct route invocation has no application lifespan bundle from
        # which to select the authoritative PostgreSQL repository. Never let
        # it inherit the module-level SQLite adapter in an authoritative mode.
        raise HTTPException(
            status_code=503,
            detail="AgentTask repository is unavailable",
            headers={"Retry-After": "1"},
        )
    try:
        repository = (
            agent_task_repository
            if http_request is None
            else agent_task_repository_for_request(http_request)
        )
    except RuntimeError:
        # Repository selection is an authority boundary. In PostgreSQL mode a
        # missing or mismatched lifespan bundle must not fall back to the
        # module-level SQLite adapter; return an explicit retryable service
        # failure instead of leaking an unhandled runtime error to the route.
        raise HTTPException(
            status_code=503,
            detail="AgentTask repository is unavailable",
            headers={"Retry-After": "1"},
        ) from None
    if not isinstance(repository, (SQLiteAgentTaskRepository, PostgresAgentTaskRepository)):
        raise HTTPException(status_code=503, detail="AgentTask repository is unsupported")
    return repository


class AgentDeliveryFailureReport(BaseModel):
    source_invocation_id: str = Field(min_length=1, max_length=256)
    approved_payload_hash: str = Field(pattern=r"^sha256:(?:v2:)?[0-9a-f]{64}$")
    reason_code: Literal["CLAIM_RESPONSE_AMBIGUOUS"]


class AgentInvocationHeartbeatRequest(BaseModel):
    source_invocation_id: str = Field(min_length=1, max_length=256)


class AgentInvocationHeartbeatResponse(BaseModel):
    approval_id: str
    source_agent_id: str
    source_invocation_id: str
    heartbeat_at: str
    expires_at: str
    live: Literal[True]


def audit(event_type: str, entity_id: str, details: dict, actor: str = "system") -> AuditEvent:
    event = AuditEvent(event_type=event_type, entity_id=entity_id, details=details, actor=actor)
    store.audit_events.append(event)
    return event


def _task_events(task_id: str) -> list[AgentTaskEvent]:
    task = agent_task_repository.get(task_id)
    if task is None:
        return []
    return agent_task_repository.list_events(task_id, workspace_id=task.workspace_id)


def _assert_agent_owns_task(agent: ExternalAgent, task: AgentTask) -> None:
    if task.external_agent_id != agent.id:
        raise HTTPException(status_code=403, detail="Agent is not allowed for this task")


def _get_agent_or_404(
    agent_id: str, user: AuthUser | None = None,
    http_request: Request = None,  # type: ignore[assignment]
) -> tuple[ExternalAgent, object | None]:
    _require_request_for_authoritative_repository(http_request, "External agent")
    repository = (
        None if http_request is None
        else external_agent_auth_repository_for_request(http_request)
    )
    workspace_id = None
    if repository is not None and not is_sqlite_external_agent_auth_repository(repository):
        if user is None:
            raise HTTPException(status_code=403, detail="Workspace membership is required")
        workspace_id = workspace_id_for_request(user, http_request)
        if not workspace_id:
            raise HTTPException(status_code=403, detail="Workspace membership is required")
    record = (
        None if repository is None or is_sqlite_external_agent_auth_repository(repository)
        else repository.get_for_workspace(agent_id, workspace_id=workspace_id)
    )
    agent = (
        store.external_agents.get(agent_id)
        if repository is None or is_sqlite_external_agent_auth_repository(repository)
        else (record[0] if record else None)
    )
    if agent is None:
        raise HTTPException(status_code=404, detail="External agent not found")
    if http_request is None:
        consistent = external_agent_scope_is_consistent(agent)
    else:
        consistent = external_agent_scope_is_consistent(
            agent,
            projects=project_repository_for_request(http_request),
            memberships=workspace_membership_repository_for_request(http_request),
        )
    if not consistent:
        raise HTTPException(status_code=404, detail="External agent not found")
    if user is not None:
        if workspace_id is None:
            workspace_id = (
                workspace_id_for_user(user) if http_request is None
                else workspace_id_for_request(user, http_request)
            )
        if workspace_id is not None and agent.workspace_id != workspace_id:
            raise HTTPException(status_code=404, detail="External agent not found")
    return agent, (record[1] if record else None)


@router.post("", response_model=AgentRegistrationResponse)
def create_agent(
    payload: AgentCreateWithCredentialPolicy,
    user: AuthUser = OwnerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> AgentRegistrationResponse:
    _require_request_for_authoritative_repository(http_request, "External agent")
    return register_external_agent(payload, user, http_request=http_request)


@router.get("", response_model=list[ExternalAgentPublic])
def list_agents(
    user: AuthUser = OwnerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> list[ExternalAgentPublic]:
    if http_request is None:
        _require_request_for_authoritative_repository(http_request, "Agent")
        workspace_id = workspace_id_for_user(user)
        return [to_public_agent(agent) for agent in store.external_agents.values()
                if external_agent_scope_is_consistent(agent)
                and (workspace_id is None or agent.workspace_id == workspace_id)]
    repository = external_agent_auth_repository_for_request(http_request)
    workspace_id = workspace_id_for_request(user, http_request)
    if not workspace_id:
        if not is_sqlite_external_agent_auth_repository(repository):
            raise HTTPException(status_code=403, detail="Workspace membership is required")
        return [to_public_agent(agent) for agent in store.external_agents.values()
                if external_agent_scope_is_consistent(agent)]
    records = repository.list_for_workspace(workspace_id)
    return [to_public_agent(agent) for agent, _credential in records
            if external_agent_scope_is_consistent(
                agent,
                projects=project_repository_for_request(http_request),
                memberships=workspace_membership_repository_for_request(http_request),
            )]


@router.get(
    "/identity",
    response_model=AgentConnectorIdentity,
    dependencies=[Depends(require_approval_protocol_v1)],
)
def get_agent_connector_identity(
    provider: AgentProvider,
    project_id: str,
    http_request: Request,
    agent: ExternalAgent = Depends(require_external_agent),
) -> AgentConnectorIdentity:
    """Verify a provider hook's credential, project, and minimum capabilities."""
    if agent.provider != provider:
        raise HTTPException(status_code=403, detail="External agent provider does not match")
    if project_id not in agent.allowed_project_ids or (
        get_settings().is_production and not agent.workspace_id
    ):
        raise HTTPException(status_code=403, detail="External agent is not scoped to this project")
    if not APPROVAL_CONNECTOR_ACTIONS.issubset(set(agent.allowed_actions)):
        raise HTTPException(
            status_code=403,
            detail="External agent lacks required approval connector capabilities",
        )
    assert_agent_can(
        agent,
        AgentAction.CREATE_APPROVAL,
        project_id=project_id,
        http_request=http_request,
    )
    return AgentConnectorIdentity(
        agent_id=agent.id,
        provider=agent.provider,
        workspace_id=agent.workspace_id,
        project_id=project_id,
    )


@router.post(
    "/approvals",
    response_model=ApprovalRequest,
    dependencies=[Depends(require_approval_protocol_v1)],
)
def create_agent_approval(
    payload: ApprovalRequestCreate,
    agent: ExternalAgent = Depends(require_external_agent),
    http_request: Request = None,  # type: ignore[assignment]
) -> ApprovalRequest:
    if not payload.source_invocation_id:
        raise HTTPException(
            status_code=422,
            detail="Connector approval requests require a source_invocation_id",
        )
    repository = _approval_repository_for_route(http_request)
    work_order_repository = _work_order_repository_for_route(http_request)
    publish_compatibility_state = approval_repository_publishes_compatibility_state(
        repository
    )
    assert_agent_can(
        agent,
        AgentAction.CREATE_APPROVAL,
        project_id=payload.project_id,
        http_request=http_request,
    )
    project = _project_repository_for_route(http_request).get(payload.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if not project.workspace_id:
        raise HTTPException(
            status_code=409,
            detail="Connector approvals require a workspace-scoped project",
        )
    if not agent.workspace_id or project.workspace_id != agent.workspace_id:
        raise HTTPException(status_code=404, detail="Project not found")
    if agent.workspace_id is None and get_settings().is_production:
        raise HTTPException(status_code=403, detail="Agent workspace is required")
    if payload.work_order_id:
        work_order = work_order_repository.get(payload.work_order_id)
        if isinstance(work_order_repository, PostgresWorkOrderRepository):
            # PostgreSQL reads validate the normalized work-order scope against
            # its payload. Compare it with the project already resolved from
            # the same request-selected repository; workspace_access helpers
            # use module-level SQLite repositories and are not authoritative
            # for this runtime.
            work_order_scope_valid = (
                work_order is not None
                and work_order.workspace_id == project.workspace_id
            )
        elif isinstance(work_order_repository, SQLiteWorkOrderRepository):
            work_order_scope_valid = (
                work_order is not None
                and work_order_scope_is_consistent(work_order)
                and work_order_workspace_id(work_order) == project.workspace_id
            )
        else:
            raise HTTPException(
                status_code=503,
                detail="Work order persistence runtime is unavailable",
            )
        if work_order is None or not work_order_scope_valid or work_order.project_id != project.id:
            raise HTTPException(status_code=404, detail="Work order not found")

    risk = assess_approval_request(payload)
    status = ApprovalStatus.BLOCKED if risk.blocked else ApprovalStatus.PENDING_REVIEW
    created_at = now_utc()
    expires_at = (
        created_at + timedelta(seconds=get_settings().agent_approval_ttl_seconds)
        if status == ApprovalStatus.PENDING_REVIEW
        else None
    )
    request = ApprovalRequest(
        **payload.model_dump(exclude={"source_provider", "source_agent_id", "source_agent_name"}),
        risk=risk,
        status=status,
        source_provider=agent.provider,
        source_agent_id=agent.id,
        source_agent_name=agent.display_name,
        workspace_id=project.workspace_id,
        verified_source=True,
        created_at=created_at,
        updated_at=created_at,
        expires_at=expires_at,
    )
    audit_event = AuditEvent(
        event_type="approval.created_by_agent",
        entity_id=request.id,
        workspace_id=request.workspace_id,
        actor=f"agent:{agent.id}",
        details={
            "project_id": request.project_id,
            "risk_level": request.risk.level,
            "status": request.status,
            "blocked": request.risk.blocked,
            "source_provider": request.source_provider,
            "source_agent_id": request.source_agent_id,
            "source_agent_name": request.source_agent_name,
            "source_session_id": request.source_session_id,
            "source_invocation_id": request.source_invocation_id,
            "source_task_url": request.source_task_url,
            "expires_at": request.expires_at,
            "verified_source": request.verified_source,
        },
    )
    notification = Notification(
        title=f"Approval from {agent.provider}: {request.title}",
        body=(
            f"{request.risk.level} risk request from "
            f"{agent.display_name} on {request.target_branch}"
        ),
        entity_type="approval_request",
        entity_id=request.id,
        approval_request_id=request.id,
        workspace_id=request.workspace_id,
    )
    def resolve_invocation_retry() -> ApprovalRequest | None:
        if not request.source_invocation_id or not request.source_agent_id:
            return None
        existing = repository.get_by_invocation(
            workspace_id=request.workspace_id,
            source_agent_id=request.source_agent_id,
            source_invocation_id=request.source_invocation_id,
        )
        if existing is None:
            return None
        retry_request = request.model_copy(
            update={"approved_payload_hash_version": existing.approved_payload_hash_version}
        )
        same_request = (
            existing.source_provider == request.source_provider
            and existing.risk == request.risk
            and compute_approval_payload_hash(existing) == compute_approval_payload_hash(retry_request)
        )
        if not same_request:
            raise HTTPException(
                status_code=409,
                detail="Invocation identity was already used for a different approval payload",
            )
        if publish_compatibility_state:
            store.approval_requests[existing.id] = existing
        return existing

    prior = resolve_invocation_retry()
    if prior is not None:
        return prior
    try:
        repository.create(
            approval=request,
            audit_event=audit_event,
            notification=notification,
            invocation_lease_ttl_seconds=(
                get_settings().external_agent_invocation_lease_ttl_seconds
                if request.source_invocation_id
                else None
            ),
        )
    except (ApprovalDecisionConflict, sqlite3.IntegrityError) as exc:
        try:
            prior = resolve_invocation_retry()
        except HTTPException:
            raise
        if prior is not None:
            return prior
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if publish_compatibility_state:
        store.approval_requests[request.id] = request
        store.audit_events.append(audit_event)
        store.notifications[notification.id] = notification
    return request


@router.get(
    "/approvals/{approval_id}/decision",
    response_model=ApprovalRequest,
    dependencies=[Depends(require_approval_protocol_v1)],
)
def get_agent_approval_decision(
    approval_id: str,
    agent: ExternalAgent = Depends(require_external_agent),
    http_request: Request = None,  # type: ignore[assignment]
) -> ApprovalRequest:
    """Return an approval only to its creating agent, for synchronous host hooks."""
    repository = _approval_repository_for_route(http_request)
    publish_compatibility_state = approval_repository_publishes_compatibility_state(
        repository
    )
    assert_agent_can(
        agent, AgentAction.READ_APPROVAL_DECISION, http_request=http_request
    )
    with approval_repository_scope(repository):
        # The Mac hook must read the durable decision. A process-local approval
        # map can be stale when another API process commits a decision.
        request = repository.get(approval_id)
        if not request:
            raise HTTPException(status_code=404, detail="Approval request not found")
        _assert_agent_owns_approval(request, agent)
        assert_agent_can(
            agent,
            AgentAction.READ_APPROVAL_DECISION,
            project_id=request.project_id,
            http_request=http_request,
        )
        if publish_compatibility_state:
            store.approval_requests[approval_id] = request
        expire_due_approval(
            request,
            actor="system:approval-deadline",
            repository=repository,
        )
        latest = repository.get(approval_id)
        if latest is None:
            raise HTTPException(status_code=404, detail="Approval request not found")
        _assert_agent_owns_approval(latest, agent)
        if publish_compatibility_state:
            store.approval_requests[approval_id] = latest
        return latest


@router.post(
    "/approvals/{approval_id}/heartbeat",
    response_model=AgentInvocationHeartbeatResponse,
    dependencies=[Depends(require_approval_protocol_v1)],
)
def heartbeat_agent_approval_invocation(
    approval_id: str,
    payload: AgentInvocationHeartbeatRequest,
    agent: ExternalAgent = Depends(require_external_agent),
    http_request: Request = None,  # type: ignore[assignment]
    x_aixion_agent_token: str | None = Header(default=None),
) -> AgentInvocationHeartbeatResponse:
    """Refresh the short-lived liveness lease for one pending native invocation."""
    repository = _approval_repository_for_route(http_request)
    assert_agent_can(agent, AgentAction.READ_APPROVAL_DECISION, http_request=http_request)
    request = repository.get(approval_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    _assert_agent_owns_approval(request, agent)
    assert_agent_can(
        agent,
        AgentAction.READ_APPROVAL_DECISION,
        project_id=request.project_id,
        http_request=http_request,
    )
    if payload.source_invocation_id != request.source_invocation_id:
        raise HTTPException(status_code=409, detail="Approval invocation identity did not match")
    try:
        lease = repository.heartbeat_invocation(
            approval_id=approval_id,
            workspace_id=request.workspace_id or "",
            source_agent_id=agent.id,
            source_invocation_id=payload.source_invocation_id,
            presented_token_hash=hashlib.sha256(
                (x_aixion_agent_token or "").encode("utf-8")
            ).hexdigest(),
            occurred_at=now_utc(),
            ttl_seconds=get_settings().external_agent_invocation_lease_ttl_seconds,
        )
    except ApprovalDeliveryCredentialRejected as exc:
        raise HTTPException(
            status_code=401,
            detail="External agent credential is no longer active",
        ) from exc
    except ApprovalInvocationLeaseRejected as exc:
        raise HTTPException(status_code=409, detail="Approval invocation is no longer live") from exc
    return AgentInvocationHeartbeatResponse(
        approval_id=lease.approval_id,
        source_agent_id=lease.source_agent_id,
        source_invocation_id=lease.source_invocation_id,
        heartbeat_at=lease.heartbeat_at.isoformat(),
        expires_at=lease.expires_at.isoformat(),
        live=True,
    )


@router.post(
    "/approvals/{approval_id}/consume",
    dependencies=[Depends(require_approval_protocol_v1)],
)
def consume_agent_approval_decision(
    approval_id: str,
    agent: ExternalAgent = Depends(require_external_agent),
    http_request: Request = None,  # type: ignore[assignment]
    x_aixion_agent_token: str | None = Header(default=None),
) -> dict[str, bool]:
    """Claim an approved invocation once before returning allow to a host hook."""
    repository = _approval_repository_for_route(http_request)
    publish_compatibility_state = approval_repository_publishes_compatibility_state(
        repository
    )
    assert_agent_can(
        agent, AgentAction.CONSUME_APPROVAL_DECISION, http_request=http_request
    )
    request = repository.get(approval_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    _assert_agent_owns_approval(request, agent)
    assert_agent_can(
        agent,
        AgentAction.CONSUME_APPROVAL_DECISION,
        project_id=request.project_id,
        http_request=http_request,
    )
    if not request.approved_payload_hash:
        raise HTTPException(status_code=409, detail="Approval has no valid approved payload")
    audit_event = AuditEvent(
        event_type="approval.delivery_claimed",
        entity_id=approval_id,
        workspace_id=request.workspace_id,
        actor=f"agent:{agent.id}",
        details={
            "source_provider": request.source_provider,
            "source_agent_id": agent.id,
            "source_invocation_id": request.source_invocation_id,
            "approved_payload_hash": request.approved_payload_hash,
            "delivery": "at_most_once",
        },
    )
    try:
        claimed = repository.claim_approved_delivery(
            approval_id=approval_id,
            workspace_id=request.workspace_id,
            source_agent_id=agent.id,
            source_invocation_id=request.source_invocation_id or "",
            expected_payload_hash=request.approved_payload_hash,
            presented_token_hash=hashlib.sha256(
                (x_aixion_agent_token or "").encode("utf-8")
            ).hexdigest(),
            audit_event=audit_event,
        )
    except ApprovalDeliveryCredentialRejected as exc:
        raise HTTPException(
            status_code=401,
            detail="External agent credential is no longer active",
        ) from exc
    except ApprovalInvocationLeaseRejected as exc:
        raise HTTPException(
            status_code=409,
            detail="Approval invocation liveness expired; resume requires a new request",
        ) from exc
    except ApprovalDecisionConflict as exc:
        raise HTTPException(status_code=409, detail="Approval identity or payload changed") from exc
    if not claimed:
        raise HTTPException(status_code=409, detail="Approval is not approved or was already delivered")
    if publish_compatibility_state:
        store.audit_events.append(audit_event)
    return {"consumed": True}


@router.post(
    "/approvals/{approval_id}/delivery-failure",
    dependencies=[Depends(require_approval_protocol_v1)],
)
def report_agent_approval_delivery_failure(
    approval_id: str,
    payload: AgentDeliveryFailureReport,
    agent: ExternalAgent = Depends(require_external_agent),
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, bool]:
    """Record an ambiguous claim response; this endpoint never grants permission."""
    repository = _approval_repository_for_route(http_request)
    publish_compatibility_state = approval_repository_publishes_compatibility_state(
        repository
    )
    assert_agent_can(
        agent,
        AgentAction.REPORT_APPROVAL_DELIVERY_FAILURE,
        http_request=http_request,
    )
    request = repository.get(approval_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    _assert_agent_owns_approval(request, agent)
    assert_agent_can(
        agent,
        AgentAction.REPORT_APPROVAL_DELIVERY_FAILURE,
        project_id=request.project_id,
        http_request=http_request,
    )
    expected_hash = request.approved_payload_hash
    if (
        not expected_hash
        or payload.source_invocation_id != request.source_invocation_id
        or payload.approved_payload_hash != expected_hash
        or expected_hash != compute_approval_payload_hash(request)
    ):
        raise HTTPException(status_code=409, detail="Delivery failure identity or payload changed")
    audit_event = AuditEvent(
        event_type="approval.delivery_failed",
        entity_id=approval_id,
        workspace_id=request.workspace_id,
        actor=f"agent:{agent.id}",
        details={
            "source_provider": request.source_provider,
            "source_agent_id": agent.id,
            "source_invocation_id": request.source_invocation_id,
            "approved_payload_hash": expected_hash,
            "reason_code": payload.reason_code,
        },
    )
    try:
        is_new_report = repository.record_approved_delivery_failure(
            approval_id=approval_id,
            workspace_id=request.workspace_id,
            source_agent_id=agent.id,
            source_invocation_id=payload.source_invocation_id,
            expected_payload_hash=expected_hash,
            reason_code=payload.reason_code,
            audit_event=audit_event,
        )
    except ApprovalDecisionConflict as exc:
        raise HTTPException(status_code=409, detail="Delivery failure cannot be recorded") from exc
    if is_new_report and publish_compatibility_state:
        store.audit_events.append(audit_event)
    return {"recorded": True}


@router.post(
    "/approvals/{approval_id}/expire",
    response_model=ApprovalRequest,
    dependencies=[Depends(require_approval_protocol_v1)],
)
def expire_agent_approval(
    approval_id: str,
    agent: ExternalAgent = Depends(require_external_agent),
    http_request: Request = None,  # type: ignore[assignment]
) -> ApprovalRequest:
    """Let an agent close only its own pending request when its host wait ends."""
    repository = _approval_repository_for_route(http_request)
    publish_compatibility_state = approval_repository_publishes_compatibility_state(
        repository
    )
    assert_agent_can(
        agent, AgentAction.EXPIRE_OWN_APPROVAL, http_request=http_request
    )
    with approval_repository_scope(repository):
        request = repository.get(approval_id)
        if not request:
            raise HTTPException(status_code=404, detail="Approval request not found")
        if publish_compatibility_state:
            store.approval_requests[approval_id] = request
        _assert_agent_owns_approval(request, agent)
        assert_agent_can(
            agent,
            AgentAction.EXPIRE_OWN_APPROVAL,
            project_id=request.project_id,
            http_request=http_request,
        )
        expire_approval(
            request,
            actor=f"agent:{agent.id}",
            reason="agent_wait_ended",
            force=True,
            repository=repository,
        )
        latest = repository.get(approval_id)
        if latest is None:
            raise HTTPException(status_code=404, detail="Approval request not found")
        _assert_agent_owns_approval(latest, agent)
        if publish_compatibility_state:
            store.approval_requests[approval_id] = latest
        return latest


def _assert_agent_owns_approval(request: ApprovalRequest, agent: ExternalAgent) -> None:
    if request.source_agent_id != agent.id:
        raise HTTPException(status_code=403, detail="Agent is not allowed for this approval")
    agent_workspace_id = getattr(agent, "workspace_id", None)
    if agent_workspace_id is None and get_settings().is_production:
        raise HTTPException(status_code=403, detail="Agent workspace is required")
    if agent_workspace_id is not None and request.workspace_id != agent_workspace_id:
        raise HTTPException(status_code=404, detail="Approval request not found")


@router.post("/work-orders", response_model=WorkOrder)
def create_agent_work_order(
    payload: AgentWorkOrderCreate,
    agent: ExternalAgent = Depends(require_external_agent),
    http_request: Request = None,  # type: ignore[assignment]
) -> WorkOrder:
    assert_agent_can(
        agent,
        AgentAction.CREATE_WORK_ORDER,
        project_id=payload.project_id,
        http_request=http_request,
    )
    project_repository_for_route = _project_repository_for_route(http_request)
    project = (
        project_repository_for_route.get(payload.project_id)
        if http_request is not None
        else store.projects.get(payload.project_id)
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if agent.workspace_id is not None and project.workspace_id != agent.workspace_id:
        raise HTTPException(status_code=404, detail="Project not found")
    if agent.workspace_id is None and get_settings().is_production:
        raise HTTPException(status_code=403, detail="Agent workspace is required")
    idea_repository: IdeaRepository | None = (
        None if http_request is None else idea_repository_for_request(http_request)
    )
    if payload.idea_id:
        idea = (
            store.ideas.get(payload.idea_id)
            if idea_repository is None
            else idea_repository.get(payload.idea_id)
        )
        idea_project = (
            None
            if idea is None or idea.project_id is None
            else project_repository_for_route.get(idea.project_id)
        )
        resolved_idea_workspace = (
            idea.workspace_id
            if idea is not None and idea.workspace_id is not None
            else (idea_project.workspace_id if idea_project is not None else None)
        )
        if (
            idea is None
            or (idea.project_id is not None and idea_project is None)
            or (idea.project_id is not None and idea.project_id != project.id)
            or resolved_idea_workspace != project.workspace_id
        ):
            raise HTTPException(status_code=404, detail="Idea not found")

    risk_level = assess_work_order(payload)
    work_order = WorkOrder(
        **payload.model_dump(),
        risk_level=risk_level,
        source_type=WorkOrderSourceType.AGENT_TASK,
        source_provider=agent.provider,
        source_agent_id=agent.id,
        source_agent_name=agent.display_name,
        workspace_id=project.workspace_id,
        verified_source=True,
    )
    audit_event = AuditEvent(
        event_type="work_order.created_by_agent",
        entity_id=work_order.id,
        workspace_id=work_order.workspace_id,
        actor=f"agent:{agent.id}",
        details={
            "project_id": work_order.project_id,
            "risk_level": work_order.risk_level,
            "source_type": work_order.source_type,
            "source_provider": work_order.source_provider,
            "source_agent_id": work_order.source_agent_id,
            "source_agent_name": work_order.source_agent_name,
            "source_task_id": work_order.source_task_id,
            "source_session_id": work_order.source_session_id,
            "created_by_user_id": work_order.created_by_user_id,
            "verified_source": work_order.verified_source,
        },
    )
    repository = _work_order_repository_for_route(http_request)
    if not isinstance(repository, (SQLiteWorkOrderRepository, PostgresWorkOrderRepository)):
        raise HTTPException(status_code=503, detail="WorkOrder repository is unsupported")
    if isinstance(repository, PostgresWorkOrderRepository) and not isinstance(
        idea_repository, PostgresIdeaRepository
    ):
        raise HTTPException(status_code=503, detail="Idea repository is unsupported")
    try:
        repository.create(work_order=work_order, audit_event=audit_event)
    except (ValueError, sqlite3.IntegrityError, WorkOrderRepositoryError) as exc:
        raise HTTPException(status_code=409, detail="Work order could not be committed safely") from exc
    if isinstance(repository, SQLiteWorkOrderRepository):
        store.project_work_order_creation(work_order, audit_event)
    return work_order


@router.post("/tasks", response_model=AgentTask)
def create_agent_task(
    payload: AgentTaskCreate,
    agent: ExternalAgent = Depends(require_external_agent),
    http_request: Request = None,  # type: ignore[assignment]
) -> AgentTask:
    assert_agent_can(
        agent,
        AgentAction.CREATE_AGENT_TASK,
        project_id=payload.project_id,
        repository_full_name=payload.repository,
        http_request=http_request,
    )
    task_repository = _agent_task_repository_for_route(http_request)
    project_repository_for_route = (
        None if http_request is None else project_repository_for_request(http_request)
    )
    postgres_runtime = isinstance(task_repository, PostgresAgentTaskRepository)
    sqlite_runtime = isinstance(task_repository, SQLiteAgentTaskRepository)
    if not (postgres_runtime or sqlite_runtime):
        raise HTTPException(status_code=503, detail="AgentTask repository is unsupported")
    projects = store.projects if project_repository_for_route is None else None
    project = (
        project_repository_for_route.get(payload.project_id)
        if project_repository_for_route is not None and payload.project_id
        else (projects.get(payload.project_id) if projects is not None and payload.project_id else None)
    )
    if payload.project_id and project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if agent.workspace_id is not None and project and project.workspace_id != agent.workspace_id:
        raise HTTPException(status_code=404, detail="Project not found")
    if postgres_runtime and (not payload.project_id or project is None or not project.workspace_id):
        raise HTTPException(status_code=403, detail="A workspace-scoped project is required")
    if agent.workspace_id is None and get_settings().is_production:
        raise HTTPException(status_code=403, detail="Agent workspace is required")

    task_payload = payload.model_dump(exclude={"provider", "metadata"})
    task = AgentTask(
        **task_payload,
        provider=agent.provider,
        external_agent_id=agent.id,
        external_agent_name=agent.display_name,
        workspace_id=project.workspace_id if project else agent.workspace_id,
        metadata={**(payload.metadata or {}), "external_agent_scoped": True},
    )
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
    audit_event = AuditEvent(
        event_type="agent_task.created_by_agent",
        entity_id=task.id,
        workspace_id=task.workspace_id,
        actor=f"agent:{agent.id}",
        details={
            "provider": agent.provider,
            "agent_id": agent.id,
            "project_id": task.project_id,
            "repository": task.repository,
            "requested_action": task.requested_action,
            "requires_approval": task.requires_approval,
        },
    )
    try:
        task_repository.create(task=task, event=event, audit=audit_event)
    except (AgentTaskRepositoryConflict, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if sqlite_runtime:
        store.project_agent_task_creation(task, event, audit_event)
    return task


@router.get("/tasks/{task_id}", response_model=AgentTask)
def get_agent_owned_task(
    task_id: str,
    agent: ExternalAgent = Depends(require_external_agent),
    http_request: Request = None,  # type: ignore[assignment]
) -> AgentTask:
    assert_agent_can(agent, AgentAction.READ_AGENT_TASK, http_request=http_request)
    repository = _agent_task_repository_for_route(http_request)
    task = repository.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Agent task not found")
    _assert_agent_owns_task(agent, task)
    return task


@router.get("/tasks/{task_id}/events", response_model=list[AgentTaskEvent])
def list_agent_owned_task_events(
    task_id: str,
    agent: ExternalAgent = Depends(require_external_agent),
    http_request: Request = None,  # type: ignore[assignment]
) -> list[AgentTaskEvent]:
    assert_agent_can(agent, AgentAction.READ_AGENT_TASK, http_request=http_request)
    repository = _agent_task_repository_for_route(http_request)
    task = repository.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Agent task not found")
    _assert_agent_owns_task(agent, task)
    return sorted(
        repository.list_events(task_id, workspace_id=task.workspace_id),
        key=lambda event: event.created_at,
    )


@router.post("/tasks/{task_id}/events", response_model=AgentTaskEvent)
def append_agent_owned_task_event(
    task_id: str,
    payload: AgentTaskEventCreate,
    agent: ExternalAgent = Depends(require_external_agent),
    http_request: Request = None,  # type: ignore[assignment]
) -> AgentTaskEvent:
    assert_agent_can(
        agent, AgentAction.APPEND_AGENT_TASK_EVENT, http_request=http_request
    )
    repository = _agent_task_repository_for_route(http_request)
    expected_task = repository.get(task_id)
    if not expected_task:
        raise HTTPException(status_code=404, detail="Agent task not found")
    _assert_agent_owns_task(agent, expected_task)
    updated_task = expected_task.model_copy(deep=True)
    if payload.status is not None:
        updated_task.status = payload.status
        updated_task.updated_at = now_utc()
    metadata = {
        **(payload.metadata or {}),
        "agent_id": agent.id,
        "external_agent_scoped": True,
    }
    metadata.pop("notification_id", None)
    event = AgentTaskEvent(
        task_id=updated_task.id,
        event_type=payload.event_type,
        message=payload.message,
        status=payload.status,
        actor=f"agent:{agent.id}",
        metadata=metadata,
    )
    audit_event = AuditEvent(
        event_type="agent_task.event_recorded_by_agent",
        entity_id=updated_task.id,
        workspace_id=updated_task.workspace_id,
        actor=f"agent:{agent.id}",
        details={
            "event_id": event.id,
            "event_type": event.event_type,
            "status": event.status,
            "message": event.message,
            "notification_id": None,
            "agent_id": agent.id,
        },
    )
    try:
        repository.commit_event(
            expected=expected_task,
            updated=updated_task,
            event=event,
            audit=audit_event,
        )
    except (AgentTaskRepositoryConflict, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(repository, SQLiteAgentTaskRepository):
        store.project_agent_task_transition(updated_task, event, audit_event)
    return event


@router.get("/{agent_id}/credentials", response_model=AgentCredentialStatus)
def get_agent_credentials(
    agent_id: str,
    user: AuthUser = OwnerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> AgentCredentialStatus:
    agent, credential = _get_agent_or_404(agent_id, user, http_request)
    return to_credential_status(agent, credential)


@router.post("/{agent_id}/token/rotate", response_model=AgentRegistrationResponse)
def rotate_agent_token(
    agent_id: str,
    payload: AgentTokenRotateRequest | None = None,
    user: AuthUser = OwnerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> AgentRegistrationResponse:
    _require_request_for_authoritative_repository(http_request, "External agent")
    repository = agent_auth_repository if http_request is None else external_agent_auth_repository_for_request(http_request)
    agent, credential = _get_agent_or_404(agent_id, user, http_request)
    response = rotate_external_agent_token(
        agent,
        credential=credential,
        repository=repository,
        actor=user.email,
        token_expires_at=payload.token_expires_at if payload else None,
        rate_limit_per_minute=payload.rate_limit_per_minute if payload else None,
    )
    if is_sqlite_external_agent_auth_repository(repository):
        store.persist()
    return response


@router.post("/{agent_id}/token/revoke", response_model=ExternalAgentPublic)
def revoke_agent_token(
    agent_id: str, user: AuthUser = OwnerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> ExternalAgentPublic:
    _require_request_for_authoritative_repository(http_request, "External agent")
    repository = agent_auth_repository if http_request is None else external_agent_auth_repository_for_request(http_request)
    agent, credential = _get_agent_or_404(agent_id, user, http_request)
    public_agent = revoke_external_agent_token(
        agent, credential=credential, repository=repository, actor=user.email
    )
    if is_sqlite_external_agent_auth_repository(repository):
        store.persist()
    return public_agent


@router.get("/{agent_id}", response_model=ExternalAgentPublic)
def get_agent(
    agent_id: str, user: AuthUser = OwnerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> ExternalAgentPublic:
    agent, _credential = _get_agent_or_404(agent_id, user, http_request)
    return to_public_agent(agent)
