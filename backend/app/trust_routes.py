from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from .agent_auth import assert_agent_can, require_external_agent
from .approval_decisions import approval_decision_repository
from .application_repository_access import workspace_id_for_request
from .auth import require_maintainer, require_owner, require_reviewer
from .models import AgentAction, AuthUser, ExternalAgent
from .store import store
from .trust_action_authorization import (
    ActionAuthorizationConflict,
    record_action_authorization,
)
from .trust_action_authorization_models import (
    ActionAuthorization,
    ActionAuthorizationCreate,
)
from .trust_consumption import consume_effective_action
from .trust_credentials import (
    CredentialIntrospectionRequest,
    CredentialIntrospectionResult,
    CredentialRevocationRequest,
    introspect_credential,
    revoke_credential_grant,
)
from .trust_flight_recorder import verify_flight_recorder
from .trust_models import (
    ActionConsumption,
    AgentGatewayResult,
    AgentReliabilityScorecard,
    CapabilityLease,
    CapabilityLeaseCreate,
    CapabilityLeasePublic,
    CapabilityLeaseStatus,
    CredentialGrant,
    CredentialGrantCreate,
    CredentialGrantPublic,
    CredentialGrantResponse,
    ExceptionQueueItem,
    FlightRecorderVerification,
    LeaseRevocationRequest,
    ProposedAction,
    ReviewerAttestation,
    ReviewerAttestationCreate,
    TrustEvent,
)
from .trust_reporting import (
    build_exception_queue,
    build_reliability_scorecards,
)
from .trust_service import (
    TrustControlConflict,
    evaluate_gateway_action,
    issue_capability_lease,
    issue_credential_grant,
    record_reviewer_attestation_postgres,
    record_reviewer_attestation,
    refresh_lease_status,
    revoke_capability_lease,
)
from .workspace_access import (
    approval_scope_is_consistent,
    approval_workspace_id,
    action_authorization_scope_is_consistent,
    capability_lease_scope_is_consistent,
    capability_lease_workspace_id,
    credential_grant_scope_is_consistent,
    proposed_action_scope_is_consistent,
    proposed_action_workspace_id,
    reviewer_attestation_scope_is_consistent,
    project_workspace_id,
    workspace_id_for_user,
)

router = APIRouter(prefix="/trust", tags=["agent-trust"])
ReviewerDependency = Depends(require_reviewer)
MaintainerDependency = Depends(require_maintainer)
OwnerDependency = Depends(require_owner)


def _postgres_attestation_runtime(request: Request | None):
    """Select PostgreSQL only when every attestation authority shares its runtime."""
    if request is None:
        return None
    state = request.app.state
    runtime = getattr(state, "postgres_runtime", None)
    repositories = getattr(state, "postgres_repositories", None)
    if runtime is None or repositories is None:
        return None
    bindings = (
        ("approval_decision_repository", "approval_decisions"),
        ("workspace_membership_repository", "workspace_membership"),
        ("trust_repository", "trust"),
        ("trust_event_repository", "trust_events"),
    )
    if any(
        getattr(state, state_name, None) is not getattr(repositories, repository_name, None)
        for state_name, repository_name in bindings
    ):
        return None
    return runtime


def _approval_for_user(approval_id: str, workspace_id: str | None):
    approval = approval_decision_repository.get(approval_id)
    if (
        approval is None
        or not approval_scope_is_consistent(approval)
        or (workspace_id is not None and approval_workspace_id(approval) != workspace_id)
    ):
        raise HTTPException(status_code=404, detail="Approval request not found")
    store.approval_requests[approval.id] = approval
    return approval


def _lease_or_404(lease_id: str, user: AuthUser | None = None) -> CapabilityLease:
    lease = store.capability_leases.get(lease_id)
    if lease is None:
        raise HTTPException(status_code=404, detail="Capability lease not found")
    workspace_id = workspace_id_for_user(user) if user else None
    if not capability_lease_scope_is_consistent(lease) or (
        workspace_id is not None and capability_lease_workspace_id(lease) != workspace_id
    ):
        raise HTTPException(status_code=404, detail="Capability lease not found")
    return refresh_lease_status(lease)


def _credential_or_404(grant_id: str, user: AuthUser | None = None) -> CredentialGrant:
    grant = store.credential_grants.get(grant_id)
    if grant is None:
        raise HTTPException(status_code=404, detail="Credential grant not found")
    workspace_id = workspace_id_for_user(user) if user else None
    lease = store.capability_leases.get(grant.lease_id)
    if not credential_grant_scope_is_consistent(grant) or (
        workspace_id is not None
        and (lease is None or capability_lease_workspace_id(lease) != workspace_id)
    ):
        raise HTTPException(status_code=404, detail="Credential grant not found")
    return grant


def _public(lease: CapabilityLease) -> CapabilityLeasePublic:
    return CapabilityLeasePublic.model_validate(lease.model_dump())


def _credential_public(grant: CredentialGrant) -> CredentialGrantPublic:
    return CredentialGrantPublic.model_validate(grant)


def _trust_error(error: Exception) -> HTTPException:
    if isinstance(error, ValueError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, NotImplementedError):
        return HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(error))
    return HTTPException(status_code=409, detail=str(error))


@router.post("/attestations", response_model=ReviewerAttestation)
def create_attestation(
    payload: ReviewerAttestationCreate,
    user: AuthUser = ReviewerDependency,
    request: Request = None,  # type: ignore[assignment]
) -> ReviewerAttestation:
    try:
        runtime = _postgres_attestation_runtime(request)
        if runtime is not None:
            workspace_id = workspace_id_for_request(user, request)
            if workspace_id is None:
                raise HTTPException(status_code=403, detail="Workspace membership is required")
            repositories = request.app.state.postgres_repositories
            return record_reviewer_attestation_postgres(
                payload,
                user=user,
                workspace_id=workspace_id,
                connect=runtime.connect,
                trust_repository=repositories.trust,
                trust_event_repository=repositories.trust_events,
            )
        workspace_id = workspace_id_for_user(user)
        _approval_for_user(payload.approval_request_id, workspace_id)
        return record_reviewer_attestation(payload, user=user)
    except (ValueError, TrustControlConflict) as error:
        raise _trust_error(error) from error


@router.get("/attestations", response_model=list[ReviewerAttestation])
def list_attestations(
    approval_request_id: str | None = None,
    user: AuthUser = ReviewerDependency,
    request: Request = None,  # type: ignore[assignment]
) -> list[ReviewerAttestation]:
    runtime = _postgres_attestation_runtime(request)
    if runtime is not None:
        workspace_id = workspace_id_for_request(user, request)
        if workspace_id is None:
            raise HTTPException(status_code=403, detail="Workspace membership is required")
        repositories = request.app.state.postgres_repositories
        with runtime.connect() as conn:
            return repositories.trust.list_reviewer_attestations(
                conn,
                workspace_id=workspace_id,
                approval_request_id=approval_request_id,
            )

    items = list(store.reviewer_attestations.values())
    workspace_id = workspace_id_for_user(user)
    if workspace_id is not None:
        scoped_items = []
        for item in items:
            approval = approval_decision_repository.get(item.approval_request_id)
            if approval is None or not approval_scope_is_consistent(approval):
                continue
            approval_workspace = approval_workspace_id(approval)
            if item.workspace_id is not None and item.workspace_id != approval_workspace:
                continue
            if (item.workspace_id or approval_workspace) == workspace_id:
                scoped_items.append(item)
        items = scoped_items
    else:
        items = [item for item in items if reviewer_attestation_scope_is_consistent(item)]
    if approval_request_id:
        items = [
            item
            for item in items
            if item.approval_request_id == approval_request_id
        ]
    return sorted(items, key=lambda item: item.created_at, reverse=True)


@router.post("/leases", response_model=CapabilityLeasePublic)
def create_lease(
    payload: CapabilityLeaseCreate,
    user: AuthUser = MaintainerDependency,
) -> CapabilityLeasePublic:
    workspace_id = workspace_id_for_user(user)
    _approval_for_user(payload.approval_request_id, workspace_id)
    try:
        return _public(issue_capability_lease(payload, user=user))
    except (ValueError, TrustControlConflict) as error:
        raise _trust_error(error) from error


@router.get("/leases", response_model=list[CapabilityLeasePublic])
def list_leases(
    status_filter: CapabilityLeaseStatus | None = Query(default=None, alias="status"),
    approval_request_id: str | None = None,
    agent_id: str | None = None,
    user: AuthUser = ReviewerDependency,
) -> list[CapabilityLeasePublic]:
    leases = [
        refresh_lease_status(item)
        for item in store.capability_leases.values()
        if capability_lease_scope_is_consistent(item)
    ]
    workspace_id = workspace_id_for_user(user)
    if workspace_id is not None:
        leases = [
            item for item in leases
            if capability_lease_workspace_id(item) == workspace_id
        ]
    if status_filter:
        leases = [item for item in leases if item.status == status_filter]
    if approval_request_id:
        leases = [
            item
            for item in leases
            if item.approval_request_id == approval_request_id
        ]
    if agent_id:
        leases = [item for item in leases if item.agent_id == agent_id]
    return [
        _public(item)
        for item in sorted(leases, key=lambda item: item.created_at, reverse=True)
    ]


@router.get("/leases/{lease_id}", response_model=CapabilityLeasePublic)
def get_lease(
    lease_id: str,
    user: AuthUser = ReviewerDependency,
) -> CapabilityLeasePublic:
    return _public(_lease_or_404(lease_id, user))


@router.post("/leases/{lease_id}/revoke", response_model=CapabilityLeasePublic)
def revoke_lease(
    lease_id: str,
    payload: LeaseRevocationRequest,
    user: AuthUser = MaintainerDependency,
) -> CapabilityLeasePublic:
    try:
        return _public(
            revoke_capability_lease(
                _lease_or_404(lease_id, user),
                user=user,
                reason=payload.reason,
            )
        )
    except TrustControlConflict as error:
        raise _trust_error(error) from error


@router.post("/gateway/actions", response_model=AgentGatewayResult)
def evaluate_external_agent_action(
    payload: ProposedAction,
    agent: ExternalAgent = Depends(require_external_agent),
) -> AgentGatewayResult:
    assert_agent_can(
        agent,
        AgentAction.EXECUTE_GITHUB,
        project_id=payload.project_id,
        repository_full_name=payload.repository,
    )
    normalized = payload.model_copy(
        update={
            "provider": agent.provider,
            "agent_id": agent.id,
            "workspace_id": agent.workspace_id,
        }
    )
    lease = store.capability_leases.get(normalized.lease_id or "")
    if lease and (
        not capability_lease_scope_is_consistent(lease)
        or capability_lease_workspace_id(lease) != agent.workspace_id
    ):
        raise HTTPException(status_code=403, detail="Capability lease belongs to another workspace")
    if lease and lease.agent_id and lease.agent_id != agent.id:
        raise HTTPException(status_code=403, detail="Capability lease belongs to another agent")
    return evaluate_gateway_action(normalized, actor=f"agent:{agent.id}")


@router.post("/gateway/actions/manual", response_model=AgentGatewayResult)
def evaluate_manual_action(
    payload: ProposedAction,
    user: AuthUser = MaintainerDependency,
) -> AgentGatewayResult:
    workspace_id = workspace_id_for_user(user)
    if payload.project_id:
        if workspace_id is not None and project_workspace_id(payload.project_id) != workspace_id:
            raise HTTPException(status_code=404, detail="Project not found")
    payload = payload.model_copy(update={"workspace_id": workspace_id})
    if payload.lease_id:
        lease = store.capability_leases.get(payload.lease_id)
        if lease is None or not capability_lease_scope_is_consistent(lease):
            raise HTTPException(status_code=404, detail="Capability lease not found")
        if payload.workspace_id is not None and capability_lease_workspace_id(lease) != payload.workspace_id:
            raise HTTPException(status_code=404, detail="Capability lease not found")
    return evaluate_gateway_action(payload, actor=user.email)


@router.post(
    "/gateway/actions/{action_id}/decision",
    response_model=ActionAuthorization,
)
def decide_exact_action(
    action_id: str,
    payload: ActionAuthorizationCreate,
    user: AuthUser = ReviewerDependency,
) -> ActionAuthorization:
    action = store.proposed_actions.get(action_id)
    workspace_id = workspace_id_for_user(user)
    if action is None or not proposed_action_scope_is_consistent(action) or (
        workspace_id is not None and proposed_action_workspace_id(action) != workspace_id
    ):
        raise HTTPException(status_code=404, detail="Proposed action not found")
    try:
        return record_action_authorization(action_id, payload, user=user)
    except (ValueError, ActionAuthorizationConflict) as error:
        raise _trust_error(error) from error


@router.get(
    "/gateway/actions/{action_id}/decision",
    response_model=ActionAuthorization | None,
)
def get_exact_action_decision(
    action_id: str,
    user: AuthUser = ReviewerDependency,
) -> ActionAuthorization | None:
    action = store.proposed_actions.get(action_id)
    workspace_id = workspace_id_for_user(user)
    if action is None or not proposed_action_scope_is_consistent(action) or (
        workspace_id is not None and proposed_action_workspace_id(action) != workspace_id
    ):
        raise HTTPException(status_code=404, detail="Proposed action not found")
    return next(
        (
            item
            for item in store.action_authorizations.values()
            if item.action_id == action_id
            and action_authorization_scope_is_consistent(item)
        ),
        None,
    )


@router.post("/gateway/actions/{action_id}/consume", response_model=ActionConsumption)
def consume_external_agent_action(
    action_id: str,
    payload: ActionConsumption,
    agent: ExternalAgent = Depends(require_external_agent),
) -> ActionConsumption:
    action = store.proposed_actions.get(action_id)
    if action is None or not proposed_action_scope_is_consistent(action):
        raise HTTPException(status_code=404, detail="Proposed action not found")
    if action.agent_id != agent.id or action.workspace_id != agent.workspace_id:
        raise HTTPException(status_code=403, detail="Agent does not own this action")
    assert_agent_can(
        agent,
        AgentAction.EXECUTE_GITHUB,
        project_id=action.project_id,
        repository_full_name=action.repository,
    )
    try:
        return consume_effective_action(
            action_id,
            payload,
            actor=f"agent:{agent.id}",
        )
    except (ValueError, TrustControlConflict) as error:
        raise _trust_error(error) from error


@router.post("/gateway/actions/{action_id}/consume/manual", response_model=ActionConsumption)
def consume_manual_action(
    action_id: str,
    payload: ActionConsumption,
    user: AuthUser = MaintainerDependency,
) -> ActionConsumption:
    action = store.proposed_actions.get(action_id)
    workspace_id = workspace_id_for_user(user)
    if action is None or not proposed_action_scope_is_consistent(action) or (
        workspace_id is not None and proposed_action_workspace_id(action) != workspace_id
    ):
        raise HTTPException(status_code=404, detail="Proposed action not found")
    try:
        return consume_effective_action(action_id, payload, actor=user.email)
    except (ValueError, TrustControlConflict) as error:
        raise _trust_error(error) from error


@router.post("/credentials", response_model=CredentialGrantResponse)
def create_credential_grant(
    payload: CredentialGrantCreate,
    user: AuthUser = OwnerDependency,
) -> CredentialGrantResponse:
    lease = store.capability_leases.get(payload.lease_id)
    workspace_id = workspace_id_for_user(user)
    if lease is None or not capability_lease_scope_is_consistent(lease) or (
        workspace_id is not None and capability_lease_workspace_id(lease) != workspace_id
    ):
        raise HTTPException(status_code=404, detail="Capability lease not found")
    try:
        return issue_credential_grant(payload, user=user)
    except (ValueError, TrustControlConflict, NotImplementedError) as error:
        raise _trust_error(error) from error


@router.post(
    "/credentials/introspect",
    response_model=CredentialIntrospectionResult,
)
def introspect_capability_credential(
    payload: CredentialIntrospectionRequest,
) -> CredentialIntrospectionResult:
    return introspect_credential(payload)


@router.post(
    "/credentials/{grant_id}/revoke",
    response_model=CredentialGrantPublic,
)
def revoke_capability_credential(
    grant_id: str,
    payload: CredentialRevocationRequest,
    user: AuthUser = OwnerDependency,
) -> CredentialGrantPublic:
    grant = revoke_credential_grant(
        _credential_or_404(grant_id, user),
        user=user,
        reason=payload.reason,
    )
    return _credential_public(grant)


@router.get("/scorecards", response_model=list[AgentReliabilityScorecard])
def list_scorecards(user: AuthUser = ReviewerDependency) -> list[AgentReliabilityScorecard]:
    return build_reliability_scorecards(workspace_id_for_user(user))


@router.get("/exceptions", response_model=list[ExceptionQueueItem])
def list_exceptions(user: AuthUser = ReviewerDependency) -> list[ExceptionQueueItem]:
    return build_exception_queue(workspace_id_for_user(user))


@router.get("/flight-recorder", response_model=list[TrustEvent])
def list_flight_recorder_events(
    entity_type: str | None = None,
    entity_id: str | None = None,
    correlation_id: str | None = None,
    limit: int = Query(default=500, ge=1, le=5000),
    user: AuthUser = ReviewerDependency,
) -> list[TrustEvent]:
    events = list(store.trust_events.values())
    workspace_id = workspace_id_for_user(user)
    if workspace_id is not None:
        events = [item for item in events if item.workspace_id == workspace_id]
    if entity_type:
        events = [item for item in events if item.entity_type == entity_type]
    if entity_id:
        events = [item for item in events if item.entity_id == entity_id]
    if correlation_id:
        events = [item for item in events if item.correlation_id == correlation_id]
    return sorted(events, key=lambda item: item.sequence)[:limit]


@router.get(
    "/flight-recorder/verify",
    response_model=FlightRecorderVerification,
)
def verify_flight_recorder_chain(
    user: AuthUser = ReviewerDependency,
) -> FlightRecorderVerification:
    workspace_id_for_user(user)
    return verify_flight_recorder()
