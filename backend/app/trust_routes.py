from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from .agent_auth import assert_agent_can, require_external_agent
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
    CredentialGrantResponse,
    ExceptionQueueItem,
    FlightRecorderVerification,
    LeaseRevocationRequest,
    ProposedAction,
    ReviewerAttestation,
    ReviewerAttestationCreate,
    TrustEvent,
)
from .trust_service import (
    TrustControlConflict,
    build_exception_queue,
    build_reliability_scorecards,
    consume_gateway_action,
    evaluate_gateway_action,
    issue_capability_lease,
    issue_credential_grant,
    record_reviewer_attestation,
    refresh_lease_status,
    revoke_capability_lease,
)

router = APIRouter(prefix="/trust", tags=["agent-trust"])
ReviewerDependency = Depends(require_reviewer)
MaintainerDependency = Depends(require_maintainer)
OwnerDependency = Depends(require_owner)


def _lease_or_404(lease_id: str) -> CapabilityLease:
    lease = store.capability_leases.get(lease_id)
    if lease is None:
        raise HTTPException(status_code=404, detail="Capability lease not found")
    return refresh_lease_status(lease)


def _credential_or_404(grant_id: str) -> CredentialGrant:
    grant = store.credential_grants.get(grant_id)
    if grant is None:
        raise HTTPException(status_code=404, detail="Credential grant not found")
    return grant


def _public(lease: CapabilityLease) -> CapabilityLeasePublic:
    return CapabilityLeasePublic.model_validate(lease.model_dump())


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
) -> ReviewerAttestation:
    try:
        return record_reviewer_attestation(payload, user=user)
    except (ValueError, TrustControlConflict) as error:
        raise _trust_error(error) from error


@router.get("/attestations", response_model=list[ReviewerAttestation])
def list_attestations(
    approval_request_id: str | None = None,
    _: AuthUser = ReviewerDependency,
) -> list[ReviewerAttestation]:
    items = list(store.reviewer_attestations.values())
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
    try:
        return _public(issue_capability_lease(payload, user=user))
    except (ValueError, TrustControlConflict) as error:
        raise _trust_error(error) from error


@router.get("/leases", response_model=list[CapabilityLeasePublic])
def list_leases(
    status_filter: CapabilityLeaseStatus | None = Query(default=None, alias="status"),
    approval_request_id: str | None = None,
    agent_id: str | None = None,
    _: AuthUser = ReviewerDependency,
) -> list[CapabilityLeasePublic]:
    leases = [refresh_lease_status(item) for item in store.capability_leases.values()]
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
    _: AuthUser = ReviewerDependency,
) -> CapabilityLeasePublic:
    return _public(_lease_or_404(lease_id))


@router.post("/leases/{lease_id}/revoke", response_model=CapabilityLeasePublic)
def revoke_lease(
    lease_id: str,
    payload: LeaseRevocationRequest,
    user: AuthUser = MaintainerDependency,
) -> CapabilityLeasePublic:
    try:
        return _public(
            revoke_capability_lease(
                _lease_or_404(lease_id),
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
        }
    )
    lease = store.capability_leases.get(normalized.lease_id or "")
    if lease and lease.agent_id and lease.agent_id != agent.id:
        raise HTTPException(status_code=403, detail="Capability lease belongs to another agent")
    return evaluate_gateway_action(normalized, actor=f"agent:{agent.id}")


@router.post("/gateway/actions/manual", response_model=AgentGatewayResult)
def evaluate_manual_action(
    payload: ProposedAction,
    user: AuthUser = MaintainerDependency,
) -> AgentGatewayResult:
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
    _: AuthUser = ReviewerDependency,
) -> ActionAuthorization | None:
    if action_id not in store.proposed_actions:
        raise HTTPException(status_code=404, detail="Proposed action not found")
    return next(
        (
            item
            for item in store.action_authorizations.values()
            if item.action_id == action_id
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
    if action is None:
        raise HTTPException(status_code=404, detail="Proposed action not found")
    if action.agent_id != agent.id:
        raise HTTPException(status_code=403, detail="Agent does not own this action")
    assert_agent_can(
        agent,
        AgentAction.EXECUTE_GITHUB,
        project_id=action.project_id,
        repository_full_name=action.repository,
    )
    try:
        return consume_gateway_action(action_id, payload, actor=f"agent:{agent.id}")
    except (ValueError, TrustControlConflict) as error:
        raise _trust_error(error) from error


@router.post("/gateway/actions/{action_id}/consume/manual", response_model=ActionConsumption)
def consume_manual_action(
    action_id: str,
    payload: ActionConsumption,
    user: AuthUser = MaintainerDependency,
) -> ActionConsumption:
    try:
        return consume_gateway_action(action_id, payload, actor=user.email)
    except (ValueError, TrustControlConflict) as error:
        raise _trust_error(error) from error


@router.post("/credentials", response_model=CredentialGrantResponse)
def create_credential_grant(
    payload: CredentialGrantCreate,
    user: AuthUser = OwnerDependency,
) -> CredentialGrantResponse:
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


@router.post("/credentials/{grant_id}/revoke", response_model=CredentialGrant)
def revoke_capability_credential(
    grant_id: str,
    payload: CredentialRevocationRequest,
    user: AuthUser = OwnerDependency,
) -> CredentialGrant:
    return revoke_credential_grant(
        _credential_or_404(grant_id),
        user=user,
        reason=payload.reason,
    )


@router.get("/scorecards", response_model=list[AgentReliabilityScorecard])
def list_scorecards(_: AuthUser = ReviewerDependency) -> list[AgentReliabilityScorecard]:
    return build_reliability_scorecards()


@router.get("/exceptions", response_model=list[ExceptionQueueItem])
def list_exceptions(_: AuthUser = ReviewerDependency) -> list[ExceptionQueueItem]:
    return build_exception_queue()


@router.get("/flight-recorder", response_model=list[TrustEvent])
def list_flight_recorder_events(
    entity_type: str | None = None,
    entity_id: str | None = None,
    correlation_id: str | None = None,
    limit: int = Query(default=500, ge=1, le=5000),
    _: AuthUser = ReviewerDependency,
) -> list[TrustEvent]:
    events = list(store.trust_events.values())
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
    _: AuthUser = ReviewerDependency,
) -> FlightRecorderVerification:
    return verify_flight_recorder()
