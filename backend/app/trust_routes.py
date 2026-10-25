from __future__ import annotations

import hashlib
from datetime import timedelta

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from .agent_auth import assert_agent_can, require_external_agent
from .approval_decisions import approval_decision_repository
from .approval_repository import SQLiteApprovalDecisionRepository
from .application_repository_access import (
    application_repositories_for_request,
    external_agent_auth_repository_for_request,
    is_sqlite_external_agent_auth_repository,
    workspace_id_for_request,
)
from .auth import require_maintainer, require_owner, require_reviewer
from .models import AgentAction, AuthUser, ExternalAgent, now_utc
from .store import store
from .trust_action_authorization import (
    ActionAuthorizationConflict,
    record_action_authorization,
    record_action_authorization_postgres,
    record_agent_run_action_authorization_postgres,
    verify_action_authorization_payload,
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
    introspect_credential_record,
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
    CredentialGrantType,
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
    issue_capability_lease_postgres,
    issue_credential_grant,
    record_reviewer_attestation_postgres,
    record_reviewer_attestation,
    refresh_lease_status,
    revoke_capability_lease,
)
from .trust_crypto import issue_bearer_token
from .workspace_membership_repository import SQLiteWorkspaceMembershipRepository
from .postgres_trust_repository import (
    PostgresTrustRepositoryConflict,
    PostgresTrustRepositoryError,
)
from .settings import get_settings
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


def _postgres_trust_is_selected() -> bool:
    settings = get_settings()
    return bool(
        getattr(settings, "is_production", False)
        or getattr(settings, "postgres_auth_enabled", False)
    )


def _postgres_trust_authority_is_selected(request: Request | None) -> bool:
    """Treat partial app bindings as selected durable authority, never local mode."""
    state = getattr(getattr(request, "app", None), "state", None)
    return bool(
        _postgres_trust_is_selected()
        or getattr(state, "postgres_runtime", None) is not None
        or getattr(state, "postgres_repositories", None) is not None
    )


def _require_postgres_trust_runtime(runtime, request: Request | None) -> None:
    """Prevent process-local trust authority when PostgreSQL is authoritative."""
    if _postgres_trust_is_selected() and (runtime is None or request is None):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PostgreSQL trust authority requires a complete app-bound request runtime",
        )


def _postgres_attestation_runtime(request: Request | None):
    """Select PostgreSQL only when every attestation authority shares its runtime."""
    if request is None:
        _require_postgres_trust_runtime(None, request)
        return None
    state = request.app.state
    runtime = getattr(state, "postgres_runtime", None)
    if runtime is None:
        _require_postgres_trust_runtime(None, request)
        return None
    try:
        repositories = application_repositories_for_request(request)
    except RuntimeError:
        _require_postgres_trust_runtime(None, request)
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
        _require_postgres_trust_runtime(None, request)
        return None
    return runtime


def _require_local_attestation_runtime(request: Request | None) -> None:
    """Refuse SQLite attestation access when PostgreSQL is selected or partially bound."""
    state = getattr(request.app, "state", None) if request is not None else None
    selected_repository = any(
        repository is not None and not isinstance(repository, sqlite_repository_type)
        for repository, sqlite_repository_type in (
            (
                getattr(state, "approval_decision_repository", None),
                SQLiteApprovalDecisionRepository,
            ),
            (
                getattr(state, "workspace_membership_repository", None),
                SQLiteWorkspaceMembershipRepository,
            ),
        )
    ) or any(
        getattr(state, name, None) is not None
        for name in ("trust_repository", "trust_event_repository")
    )
    if (
        get_settings().postgres_auth_enabled
        or getattr(state, "postgres_runtime", None) is not None
        or getattr(state, "postgres_repositories", None) is not None
        or selected_repository
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PostgreSQL reviewer-attestation repository is unavailable",
        )


def _postgres_lease_runtime(request: Request | None):
    """Select lease writes only when every required authority shares one runtime."""
    runtime = _postgres_attestation_runtime(request)
    if runtime is None or request is None:
        return None
    repositories = request.app.state.postgres_repositories
    bindings = (
        ("project_repository", "projects"),
        ("agent_task_repository", "agent_tasks"),
        ("external_agent_auth_repository", "external_agent_auth"),
    )
    if any(
        getattr(request.app.state, state_name, None)
        is not getattr(repositories, repository_name, None)
        for state_name, repository_name in bindings
    ):
        _require_postgres_trust_runtime(None, request)
        return None
    return runtime


def _postgres_trust_runtime(request: Request | None):
    """Select PostgreSQL trust mutations only with the complete app-bound bundle."""
    return _postgres_attestation_runtime(request)


def _postgres_reporting_bundle(request: Request | None):
    """Select the complete durable reporting bundle or fail closed.

    Reporting may use the local projection only when PostgreSQL auth and every
    PostgreSQL app-state binding are absent. In particular, a selected
    PostgreSQL profile with a missing runtime must never expose stale process
    maps as if they were authoritative.
    """
    state = getattr(getattr(request, "app", None), "state", None)
    runtime = getattr(state, "postgres_runtime", None)
    repositories = getattr(state, "postgres_repositories", None)
    postgres_selected = (
        _postgres_trust_is_selected()
        or runtime is not None
        or repositories is not None
    )
    if not postgres_selected:
        return None
    try:
        selected = application_repositories_for_request(request)
    except RuntimeError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Trust reporting requires the complete app-bound PostgreSQL runtime",
        ) from error
    if (
        runtime is None
        or selected is not repositories
        or getattr(runtime, "repositories", None) is not selected
        or getattr(state, "trust_repository", None) is not selected.trust
        or getattr(state, "agent_task_repository", None) is not selected.agent_tasks
        or getattr(state, "agent_run_repository", None) is not selected.agent_runs
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Trust reporting requires the complete app-bound PostgreSQL runtime",
        )
    return runtime, selected


def _postgres_action_runtime(request: Request | None):
    runtime = _postgres_trust_runtime(request)
    if runtime is None or request is None:
        return None
    repositories = request.app.state.postgres_repositories
    if getattr(request.app.state, "project_repository", None) is not repositories.projects:
        _require_postgres_trust_runtime(None, request)
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
        _require_local_attestation_runtime(request)
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

    _require_local_attestation_runtime(request)
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
    request: Request = None,  # type: ignore[assignment]
) -> CapabilityLeasePublic:
    try:
        runtime = _postgres_lease_runtime(request)
        if runtime is not None:
            workspace_id = workspace_id_for_request(user, request)
            if workspace_id is None:
                raise HTTPException(status_code=403, detail="Workspace membership is required")
            repositories = request.app.state.postgres_repositories
            return _public(
                issue_capability_lease_postgres(
                    payload,
                    user=user,
                    workspace_id=workspace_id,
                    connect=runtime.connect,
                    trust_repository=repositories.trust,
                    trust_event_repository=repositories.trust_events,
                )
            )
        state = getattr(getattr(request, "app", None), "state", None)
        postgres_selected = (
            get_settings().postgres_auth_enabled
            or getattr(state, "postgres_runtime", None) is not None
            or getattr(state, "postgres_repositories", None) is not None
        )
        if postgres_selected:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="PostgreSQL lease issuance requires the complete durable approval, task, project, and trust repository bundle",
            )
        workspace_id = workspace_id_for_user(user)
        _approval_for_user(payload.approval_request_id, workspace_id)
        return _public(issue_capability_lease(payload, user=user))
    except (ValueError, TrustControlConflict, PostgresTrustRepositoryConflict) as error:
        raise _trust_error(error) from error


@router.get("/leases", response_model=list[CapabilityLeasePublic])
def list_leases(
    status_filter: CapabilityLeaseStatus | None = Query(default=None, alias="status"),
    approval_request_id: str | None = None,
    agent_id: str | None = None,
    user: AuthUser = ReviewerDependency,
    request: Request = None,  # type: ignore[assignment]
) -> list[CapabilityLeasePublic]:
    runtime = _postgres_lease_runtime(request)
    if runtime is not None:
        workspace_id = workspace_id_for_request(user, request)
        if workspace_id is None:
            raise HTTPException(status_code=403, detail="Workspace membership is required")
        repositories = request.app.state.postgres_repositories
        with runtime.connect() as conn:
            with conn.transaction():
                leases = repositories.trust.list_capability_leases(
                    conn,
                    workspace_id=workspace_id,
                    now=now_utc(),
                    trust_event_repository=repositories.trust_events,
                    status=status_filter,
                    approval_id=approval_request_id,
                    agent_id=agent_id,
                )
        return [_public(item) for item in leases]
    if _postgres_trust_authority_is_selected(request):
        raise HTTPException(
            status_code=409,
            detail="PostgreSQL lease listing requires the complete durable trust and approval repository bundle",
        )
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
    request: Request = None,  # type: ignore[assignment]
) -> CapabilityLeasePublic:
    runtime = _postgres_lease_runtime(request)
    if runtime is not None:
        workspace_id = workspace_id_for_request(user, request)
        if workspace_id is None:
            raise HTTPException(status_code=403, detail="Workspace membership is required")
        repositories = request.app.state.postgres_repositories
        with runtime.connect() as conn:
            with conn.transaction():
                lease = repositories.trust.get_capability_lease(
                    conn, lease_id, workspace_id, for_update=True
                )
                if lease is None:
                    raise HTTPException(status_code=404, detail="Capability lease not found")
                if (
                    lease.status == CapabilityLeaseStatus.ACTIVE
                    and lease.expires_at <= now_utc()
                ):
                    lease = repositories.trust.expire_active_capability_lease(
                        conn,
                        lease=lease,
                        now=now_utc(),
                        trust_event_repository=repositories.trust_events,
                    )
        return _public(lease)
    if _postgres_trust_authority_is_selected(request):
        raise HTTPException(
            status_code=409,
            detail="PostgreSQL lease lookup requires the complete durable trust and approval repository bundle",
        )
    return _public(_lease_or_404(lease_id, user))


@router.post("/leases/{lease_id}/revoke", response_model=CapabilityLeasePublic)
def revoke_lease(
    lease_id: str,
    payload: LeaseRevocationRequest,
    user: AuthUser = MaintainerDependency,
    request: Request = None,  # type: ignore[assignment]
) -> CapabilityLeasePublic:
    try:
        runtime = _postgres_lease_runtime(request)
        if runtime is not None:
            workspace_id = workspace_id_for_request(user, request)
            if workspace_id is None:
                raise HTTPException(status_code=403, detail="Workspace membership is required")
            repositories = request.app.state.postgres_repositories
            with runtime.connect() as conn:
                with conn.transaction():
                    lease = repositories.trust.revoke_capability_lease(
                        conn,
                        lease_id=lease_id,
                        workspace_id=workspace_id,
                        actor_user_id=user.id,
                        actor=user.email,
                        reason=payload.reason,
                        now=now_utc(),
                        trust_event_repository=repositories.trust_events,
                    )
            return _public(lease)
        if _postgres_trust_authority_is_selected(request):
            raise HTTPException(
                status_code=409,
                detail="PostgreSQL lease revocation requires the complete durable trust repository bundle",
            )
        return _public(
            revoke_capability_lease(
                _lease_or_404(lease_id, user),
                user=user,
                reason=payload.reason,
            )
        )
    except (ValueError, TrustControlConflict, PostgresTrustRepositoryConflict) as error:
        raise _trust_error(error) from error


@router.post("/gateway/actions", response_model=AgentGatewayResult)
def evaluate_external_agent_action(
    payload: ProposedAction,
    agent: ExternalAgent = Depends(require_external_agent),
    request: Request = None,  # type: ignore[assignment]
) -> AgentGatewayResult:
    assert_agent_can(
        agent,
        AgentAction.EXECUTE_GITHUB,
        project_id=payload.project_id,
        repository_full_name=payload.repository,
        http_request=request,
    )
    normalized = payload.model_copy(
        update={
            "provider": agent.provider,
            "agent_id": agent.id,
            "workspace_id": agent.workspace_id,
        }
    )
    runtime = _postgres_action_runtime(request)
    if runtime is not None:
        repositories = request.app.state.postgres_repositories
        external_auth = external_agent_auth_repository_for_request(request)
        if external_auth is not repositories.external_agent_auth:
            raise HTTPException(
                status_code=409,
                detail="PostgreSQL gateway actions require the external-agent auth repository from the same runtime",
            )
        if normalized.run_id:
            raise HTTPException(
                status_code=409,
                detail="Run-linked external-agent actions require the durable AgentRun execution transaction",
            )
        if normalized.task_id and not repositories.trust.task_actions_enabled():
            raise HTTPException(status_code=409, detail="PostgreSQL task-only actions are disabled")
        if normalized.project_id:
            project = repositories.projects.get(normalized.project_id)
            if project is None or project.workspace_id != agent.workspace_id:
                raise HTTPException(status_code=404, detail="Project not found")
        try:
            with runtime.connect() as conn:
                with conn.transaction():
                    decision, lease = repositories.trust.evaluate_proposed_action(
                        conn,
                        action=normalized,
                        actor=f"agent:{agent.id}",
                        now=now_utc(),
                        trust_event_repository=repositories.trust_events,
                        expected_agent_id=agent.id,
                    )
            return AgentGatewayResult(
                action=normalized,
                decision=decision,
                lease=_public(lease) if lease else None,
            )
        except (ValueError, PostgresTrustRepositoryConflict) as error:
            raise _trust_error(error) from error
    if request is not None:
        external_auth = external_agent_auth_repository_for_request(request)
        if not is_sqlite_external_agent_auth_repository(external_auth):
            raise HTTPException(
                status_code=409,
                detail="PostgreSQL-authenticated gateway actions require the complete durable trust repository bundle",
            )
        if _postgres_trust_authority_is_selected(request):
            raise HTTPException(
                status_code=409,
                detail="PostgreSQL gateway actions require the complete durable project and trust repository bundle",
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
    request: Request = None,  # type: ignore[assignment]
) -> AgentGatewayResult:
    runtime = _postgres_action_runtime(request)
    if runtime is not None:
        workspace_id = workspace_id_for_request(user, request)
        if workspace_id is None:
            raise HTTPException(status_code=403, detail="Workspace membership is required")
        repositories = request.app.state.postgres_repositories
        if payload.project_id:
            project = repositories.projects.get(payload.project_id)
            if project is None or project.workspace_id != workspace_id:
                raise HTTPException(status_code=404, detail="Project not found")
        if payload.run_id:
            raise HTTPException(
                status_code=409,
                detail="Run-linked action evaluation requires the durable AgentRun execution transaction",
            )
        if payload.task_id and not repositories.trust.task_actions_enabled():
            raise HTTPException(status_code=409, detail="PostgreSQL task-only actions are disabled")
        action = payload.model_copy(update={"workspace_id": workspace_id})
        try:
            with runtime.connect() as conn:
                with conn.transaction():
                    decision, lease = repositories.trust.evaluate_proposed_action(
                        conn,
                        action=action,
                        actor=user.email,
                        now=now_utc(),
                        trust_event_repository=repositories.trust_events,
                    )
            return AgentGatewayResult(
                action=action,
                decision=decision,
                lease=_public(lease) if lease else None,
            )
        except (ValueError, PostgresTrustRepositoryConflict) as error:
            raise _trust_error(error) from error
    if _postgres_trust_authority_is_selected(request):
        raise HTTPException(
            status_code=409,
            detail="PostgreSQL action evaluation requires the complete durable project and trust repository bundle",
        )
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
    request: Request = None,  # type: ignore[assignment]
) -> ActionAuthorization:
    runtime = _postgres_action_runtime(request)
    if runtime is not None:
        workspace_id = workspace_id_for_request(user, request)
        if workspace_id is None:
            raise HTTPException(status_code=403, detail="Workspace membership is required")
        repositories = request.app.state.postgres_repositories
        # Run-linked actions use a separate transaction primitive that locks
        # and validates the durable run -> current step -> action binding. Derive
        # that scope from the authenticated workspace's stored action; never
        # accept run or step identifiers from the reviewer request.
        try:
            with runtime.connect() as conn:
                with conn.transaction():
                    action = repositories.trust.get_proposed_action(
                        conn, action_id, workspace_id, for_update=True
                    )
        except (PostgresTrustRepositoryConflict, PostgresTrustRepositoryError) as error:
            raise _trust_error(error) from error
        if action is None:
            raise HTTPException(status_code=404, detail="Proposed action not found")
        if action.run_id is not None:
            metadata = action.metadata
            run_id = action.run_id
            task_id = action.task_id
            step_id = metadata.get("step_id") if isinstance(metadata, dict) else None
            step_type = metadata.get("step_type") if isinstance(metadata, dict) else None
            idempotency_key = (
                metadata.get("idempotency_key") if isinstance(metadata, dict) else None
            )
            if (
                not isinstance(run_id, str)
                or not run_id.strip()
                or not isinstance(task_id, str)
                or not task_id.strip()
                or not isinstance(step_id, str)
                or not step_id.strip()
                or not isinstance(step_type, str)
                or not step_type.strip()
                or not isinstance(idempotency_key, str)
                or not idempotency_key.strip()
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Run-linked action is missing its exact durable AgentRun binding",
                )
            try:
                return record_agent_run_action_authorization_postgres(
                    action_id,
                    payload,
                    expected_run_id=run_id,
                    expected_step_id=step_id,
                    user=user,
                    workspace_id=workspace_id,
                    connect=runtime.connect,
                    trust_repository=repositories.trust,
                    trust_event_repository=repositories.trust_events,
                    now=now_utc(),
                )
            except (
                ValueError,
                ActionAuthorizationConflict,
                PostgresTrustRepositoryConflict,
                PostgresTrustRepositoryError,
            ) as error:
                raise _trust_error(error) from error
        try:
            return record_action_authorization_postgres(
                action_id,
                payload,
                user=user,
                workspace_id=workspace_id,
                connect=runtime.connect,
                trust_repository=repositories.trust,
                trust_event_repository=repositories.trust_events,
                now=now_utc(),
            )
        except (ValueError, ActionAuthorizationConflict, PostgresTrustRepositoryConflict) as error:
            raise _trust_error(error) from error
    if _postgres_trust_authority_is_selected(request):
        raise HTTPException(
            status_code=409,
            detail="PostgreSQL action decisions require the complete durable project and trust repository bundle",
        )
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
    request: Request = None,  # type: ignore[assignment]
) -> ActionAuthorization | None:
    runtime = _postgres_action_runtime(request)
    if runtime is not None:
        workspace_id = workspace_id_for_request(user, request)
        if workspace_id is None:
            raise HTTPException(status_code=403, detail="Workspace membership is required")
        repositories = request.app.state.postgres_repositories
        with runtime.connect() as conn:
            with conn.transaction():
                action = repositories.trust.get_proposed_action(conn, action_id, workspace_id)
                if action is None:
                    raise HTTPException(status_code=404, detail="Proposed action not found")
                authorization = repositories.trust.get_action_authorization_for_action(
                    conn, action_id, workspace_id
                )
                if authorization is not None and not verify_action_authorization_payload(
                    authorization, action
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="Stored action authorization does not match its signed action payload",
                    )
                return authorization
    if _postgres_trust_authority_is_selected(request):
        raise HTTPException(
            status_code=409,
            detail="PostgreSQL action decisions require the complete durable project and trust repository bundle",
        )
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
    request: Request = None,  # type: ignore[assignment]
) -> ActionConsumption:
    runtime = _postgres_action_runtime(request)
    if runtime is not None:
        repositories = request.app.state.postgres_repositories
        external_auth = external_agent_auth_repository_for_request(request)
        if external_auth is not repositories.external_agent_auth:
            raise HTTPException(
                status_code=409,
                detail="PostgreSQL action consumption requires the external-agent auth repository from the same runtime",
            )
        if not agent.workspace_id:
            raise HTTPException(status_code=403, detail="External agent workspace is required")
        try:
            with runtime.connect() as conn:
                action = repositories.trust.get_proposed_action(
                    conn, action_id, agent.workspace_id
                )
            if action is None:
                raise HTTPException(status_code=404, detail="Proposed action not found")
            if action.agent_id != agent.id or action.workspace_id != agent.workspace_id:
                raise HTTPException(status_code=403, detail="Agent does not own this action")
            if action.run_id:
                raise HTTPException(
                    status_code=409,
                    detail="Run-linked action consumption requires the durable AgentRun execution transaction",
                )
            if action.task_id and not repositories.trust.task_actions_enabled():
                raise HTTPException(status_code=409, detail="PostgreSQL task-only actions are disabled")
            assert_agent_can(
                agent,
                AgentAction.EXECUTE_GITHUB,
                project_id=action.project_id,
                repository_full_name=action.repository,
                http_request=request,
            )
            with runtime.connect() as conn:
                with conn.transaction():
                    if action.task_id:
                        return repositories.trust.consume_task_action(
                            conn,
                            action_id=action_id,
                            workspace_id=agent.workspace_id,
                            task_id=action.task_id,
                            consumption=payload,
                            actor=f"agent:{agent.id}",
                            now=now_utc(),
                            expected_agent_id=agent.id,
                        )
                    locked_action = repositories.trust.get_proposed_action(
                        conn, action_id, agent.workspace_id, for_update=True
                    )
                    if locked_action is None:
                        raise HTTPException(
                            status_code=404, detail="Proposed action not found"
                        )
                    if (
                        locked_action.agent_id != agent.id
                        or locked_action.workspace_id != agent.workspace_id
                    ):
                        raise HTTPException(
                            status_code=403, detail="Agent does not own this action"
                        )
                    if locked_action.run_id:
                        raise HTTPException(
                            status_code=409,
                            detail="Run-linked action consumption requires the durable AgentRun execution transaction",
                        )
                    return repositories.trust.consume_effective_action(
                        conn,
                        action_id=action_id,
                        workspace_id=agent.workspace_id,
                        consumption=payload,
                        actor=f"agent:{agent.id}",
                        now=now_utc(),
                    )
        except (ValueError, TrustControlConflict, PostgresTrustRepositoryConflict) as error:
            raise _trust_error(error) from error
    if _postgres_trust_authority_is_selected(request):
        raise HTTPException(
            status_code=409,
            detail="PostgreSQL action consumption requires the complete durable project and trust repository bundle",
        )
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
        http_request=request,
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
    request: Request = None,  # type: ignore[assignment]
) -> ActionConsumption:
    runtime = _postgres_trust_runtime(request)
    if runtime is not None:
        workspace_id = workspace_id_for_request(user, request)
        if workspace_id is None:
            raise HTTPException(status_code=403, detail="Workspace membership is required")
        repositories = request.app.state.postgres_repositories
        try:
            with runtime.connect() as conn:
                with conn.transaction():
                    action = repositories.trust.get_proposed_action(
                        conn, action_id, workspace_id
                    )
                    if action is None:
                        raise HTTPException(
                            status_code=404, detail="Proposed action not found"
                        )
                    return repositories.trust.consume_effective_action(
                        conn,
                        action_id=action_id,
                        workspace_id=workspace_id,
                        consumption=payload,
                        actor=user.email,
                        now=now_utc(),
                    )
        except PostgresTrustRepositoryConflict as error:
            raise _trust_error(error) from error
    if _postgres_trust_authority_is_selected(request):
        raise HTTPException(
            status_code=409,
            detail="PostgreSQL action consumption requires the complete durable trust repository bundle",
        )
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
    request: Request = None,  # type: ignore[assignment]
) -> CredentialGrantResponse:
    try:
        runtime = _postgres_trust_runtime(request)
        if runtime is not None:
            if payload.grant_type != CredentialGrantType.INTERNAL_CAPABILITY_TOKEN:
                raise NotImplementedError(
                    "External GitHub App and OIDC brokers require deployment-specific issuer integration."
                )
            workspace_id = workspace_id_for_request(user, request)
            if workspace_id is None:
                raise HTTPException(
                    status_code=403, detail="Workspace membership is required"
                )
            now = now_utc()
            raw_token, token_hash = issue_bearer_token()
            grant = CredentialGrant(
                lease_id=payload.lease_id,
                workspace_id=workspace_id,
                grant_type=payload.grant_type,
                audience=payload.audience,
                subject=payload.subject,
                scope=payload.scope,
                expires_at=now + timedelta(seconds=payload.expires_in_seconds),
                token_hash=token_hash,
                metadata={"issued_by_user_id": user.id},
            )
            repositories = request.app.state.postgres_repositories
            with runtime.connect() as conn:
                with conn.transaction():
                    repositories.trust.issue_credential_grant(
                        conn,
                        record=grant,
                        actor_user_id=user.id,
                        actor=user.email,
                        now=now,
                        trust_event_repository=repositories.trust_events,
                    )
            return CredentialGrantResponse(grant=grant, bearer_token=raw_token)
        if _postgres_trust_authority_is_selected(request):
            raise HTTPException(
                status_code=409,
                detail="PostgreSQL credential issuance requires the complete durable trust repository bundle",
            )
        lease = store.capability_leases.get(payload.lease_id)
        workspace_id = workspace_id_for_user(user)
        if lease is None or not capability_lease_scope_is_consistent(lease) or (
            workspace_id is not None and capability_lease_workspace_id(lease) != workspace_id
        ):
            raise HTTPException(status_code=404, detail="Capability lease not found")
        return issue_credential_grant(payload, user=user)
    except (
        ValueError,
        TrustControlConflict,
        PostgresTrustRepositoryConflict,
        NotImplementedError,
    ) as error:
        raise _trust_error(error) from error


@router.post(
    "/credentials/introspect",
    response_model=CredentialIntrospectionResult,
)
def introspect_capability_credential(
    payload: CredentialIntrospectionRequest,
    request: Request = None,  # type: ignore[assignment]
) -> CredentialIntrospectionResult:
    runtime = _postgres_trust_runtime(request)
    if runtime is not None:
        repositories = request.app.state.postgres_repositories
        token_hash = hashlib.sha256(payload.bearer_token.encode("utf-8")).hexdigest()
        with runtime.connect() as conn:
            found = repositories.trust.get_credential_grant_by_token_hash(
                conn, token_hash
            )
        if found is None:
            return introspect_credential_record(payload, None, None)
        grant, lease = found
        return introspect_credential_record(payload, grant, lease)
    if _postgres_trust_authority_is_selected(request):
        raise HTTPException(
            status_code=503,
            detail="PostgreSQL credential introspection requires the complete durable trust repository bundle",
        )
    return introspect_credential(payload)


@router.post(
    "/credentials/{grant_id}/revoke",
    response_model=CredentialGrantPublic,
)
def revoke_capability_credential(
    grant_id: str,
    payload: CredentialRevocationRequest,
    user: AuthUser = OwnerDependency,
    request: Request = None,  # type: ignore[assignment]
) -> CredentialGrantPublic:
    try:
        runtime = _postgres_trust_runtime(request)
        if runtime is not None:
            workspace_id = workspace_id_for_request(user, request)
            if workspace_id is None:
                raise HTTPException(
                    status_code=403, detail="Workspace membership is required"
                )
            repositories = request.app.state.postgres_repositories
            with runtime.connect() as conn:
                with conn.transaction():
                    grant = repositories.trust.revoke_credential_grant(
                        conn,
                        grant_id=grant_id,
                        workspace_id=workspace_id,
                        actor_user_id=user.id,
                        actor=user.email,
                        reason=payload.reason,
                        now=now_utc(),
                        trust_event_repository=repositories.trust_events,
                    )
            return _credential_public(grant)
        if _postgres_trust_authority_is_selected(request):
            raise HTTPException(
                status_code=409,
                detail="PostgreSQL credential revocation requires the complete durable trust repository bundle",
            )
        grant = revoke_credential_grant(
            _credential_or_404(grant_id, user),
            user=user,
            reason=payload.reason,
        )
        return _credential_public(grant)
    except (ValueError, TrustControlConflict, PostgresTrustRepositoryConflict) as error:
        raise _trust_error(error) from error


@router.get("/scorecards", response_model=list[AgentReliabilityScorecard])
def list_scorecards(
    user: AuthUser = ReviewerDependency,
    request: Request = None,  # type: ignore[assignment]
) -> list[AgentReliabilityScorecard]:
    reporting_bundle = _postgres_reporting_bundle(request)
    if reporting_bundle is not None:
        runtime, repositories = reporting_bundle
        workspace_id = workspace_id_for_request(user, request)
        if not workspace_id:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Trust scorecards require a resolved workspace",
            )
        try:
            with runtime.connect() as conn:
                with conn.transaction():
                    conn.execute(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                    )
                    return repositories.trust.list_reliability_scorecards(
                        conn,
                        workspace_id=workspace_id,
                    )
        except (psycopg.Error, PostgresTrustRepositoryError, ValueError) as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="PostgreSQL trust scorecards are unavailable",
            ) from error
    return build_reliability_scorecards(workspace_id_for_user(user))


@router.get("/exceptions", response_model=list[ExceptionQueueItem])
def list_exceptions(
    user: AuthUser = ReviewerDependency,
    request: Request = None,  # type: ignore[assignment]
) -> list[ExceptionQueueItem]:
    reporting_bundle = _postgres_reporting_bundle(request)
    if reporting_bundle is not None:
        runtime, repositories = reporting_bundle
        workspace_id = workspace_id_for_request(user, request)
        if not workspace_id:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Trust exception queue requires a resolved workspace",
            )
        try:
            with runtime.connect() as conn:
                with conn.transaction():
                    conn.execute(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                    )
                    return repositories.trust.list_exception_queue(
                        conn,
                        workspace_id=workspace_id,
                    )
        except (psycopg.Error, PostgresTrustRepositoryError, ValueError) as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="PostgreSQL trust exception queue is unavailable",
            ) from error
    return build_exception_queue(workspace_id_for_user(user))


@router.get("/flight-recorder", response_model=list[TrustEvent])
def list_flight_recorder_events(
    entity_type: str | None = None,
    entity_id: str | None = None,
    correlation_id: str | None = None,
    limit: int = Query(default=500, ge=1, le=5000),
    user: AuthUser = ReviewerDependency,
    request: Request = None,  # type: ignore[assignment]
) -> list[TrustEvent]:
    runtime = _postgres_trust_runtime(request)
    if runtime is not None:
        workspace_id = workspace_id_for_request(user, request)
        if workspace_id is None:
            raise HTTPException(status_code=403, detail="Workspace membership is required")
        repositories = request.app.state.postgres_repositories
        with runtime.connect() as conn:
            with conn.transaction():
                return repositories.trust_events.list_workspace_events(
                    conn,
                    workspace_id=workspace_id,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    correlation_id=correlation_id,
                    limit=limit,
                )
    if _postgres_trust_authority_is_selected(request):
        raise HTTPException(
            status_code=503,
            detail="PostgreSQL flight recorder requires the complete durable trust repository bundle",
        )
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
    request: Request = None,  # type: ignore[assignment]
) -> FlightRecorderVerification:
    runtime = _postgres_trust_runtime(request)
    if runtime is not None:
        workspace_id = workspace_id_for_request(user, request)
        if workspace_id is None:
            raise HTTPException(status_code=403, detail="Workspace membership is required")
        repositories = request.app.state.postgres_repositories
        with runtime.connect() as conn:
            with conn.transaction():
                return repositories.trust_events.verify_in_transaction(conn)
    if _postgres_trust_authority_is_selected(request):
        raise HTTPException(
            status_code=503,
            detail="PostgreSQL flight recorder verification requires the complete durable trust repository bundle",
        )
    workspace_id_for_user(user)
    return verify_flight_recorder()
