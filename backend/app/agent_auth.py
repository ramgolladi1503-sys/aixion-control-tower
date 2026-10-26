from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

import psycopg
from fastapi import Header, HTTPException, Request, status

from .agent_credential_models import (
    AgentCredentialRecord,
    AgentCredentialState,
    AgentCredentialStatus,
    AgentCreateWithCredentialPolicy,
)
from .agent_auth_repository import SQLiteExternalAgentAuthRepository
from .postgres_external_agent_auth_repository import PostgresExternalAgentConflictError
from .agent_token_lifetime import effective_agent_token_expiry, resolve_agent_token_expiry
from .approval_decisions import project_repository
from .application_repository_access import (
    external_agent_auth_repository_for_request,
    is_sqlite_external_agent_auth_repository,
    project_repository_for_request,
    workspace_membership_repository_for_request,
    workspace_id_for_request,
)
from .models import (
    AgentAction,
    AgentAuthType,
    AgentRegistrationResponse,
    AuthUser,
    ExternalAgent,
    AuditEvent,
    ExternalAgentPublic,
    now_utc,
)
from .settings import get_settings
from .store import store
from .workspace_access import (
    external_agent_scope_is_consistent,
    workspace_id_for_user,
)

AGENT_RATE_LIMIT_WINDOW = timedelta(minutes=1)
agent_auth_repository = SQLiteExternalAgentAuthRepository(store._connect)


def _require_authoritative_repository(repository, operation: str) -> None:
    """Reject service calls that would use local-only credential state in authoritative modes."""
    settings = get_settings()
    if (
        (
            getattr(settings, "is_production", False)
            or getattr(settings, "postgres_auth_enabled", False)
        )
        and (repository is None or is_sqlite_external_agent_auth_repository(repository))
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"External agent {operation} requires the PostgreSQL auth repository",
            headers={"Retry-After": "1"},
        )


def _hash_agent_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _action_value(action: AgentAction | str) -> str:
    return action.value if isinstance(action, AgentAction) else str(action)


def _issue_agent_token() -> tuple[str, str]:
    raw_token = f"aixion_agent_{secrets.token_urlsafe(40)}"
    return raw_token, _hash_agent_token(raw_token)


def _credential_for(agent: ExternalAgent) -> AgentCredentialRecord:
    credential = store.external_agent_credentials.get(agent.id)
    if credential is None:
        credential = AgentCredentialRecord(agent_id=agent.id)
        store.external_agent_credentials[agent.id] = credential
    return credential


def _credential_state(
    agent: ExternalAgent, credential: AgentCredentialRecord
) -> AgentCredentialState:
    if not agent.enabled:
        return AgentCredentialState.DISABLED
    if agent.auth_type == AgentAuthType.MANUAL:
        return AgentCredentialState.MANUAL
    if credential.token_revoked_at is not None:
        return AgentCredentialState.REVOKED
    if not agent.secret_hash:
        return AgentCredentialState.NOT_CONFIGURED
    if effective_agent_token_expiry(credential) <= now_utc():
        return AgentCredentialState.EXPIRED
    return AgentCredentialState.ACTIVE


def to_credential_status(
    agent: ExternalAgent, credential: AgentCredentialRecord | None = None
) -> AgentCredentialStatus:
    credential = credential or _credential_for(agent)
    effective_expiry = (
        effective_agent_token_expiry(credential)
        if agent.secret_hash and agent.auth_type != AgentAuthType.MANUAL
        else credential.token_expires_at
    )
    return AgentCredentialStatus(
        agent_id=agent.id,
        auth_type=agent.auth_type,
        credential_state=_credential_state(agent, credential),
        token_present=bool(agent.secret_hash),
        token_expires_at=effective_expiry,
        token_revoked_at=credential.token_revoked_at,
        token_rotated_at=credential.token_rotated_at,
        last_used_at=credential.last_used_at,
        failed_auth_count=credential.failed_auth_count,
        last_auth_failed_at=credential.last_auth_failed_at,
        rate_limit_per_minute=credential.rate_limit_per_minute,
        rate_limit_window_started_at=credential.rate_limit_window_started_at,
        rate_limit_request_count=credential.rate_limit_request_count,
    )


def to_public_agent(agent: ExternalAgent) -> ExternalAgentPublic:
    return ExternalAgentPublic(
        id=agent.id,
        provider=agent.provider,
        display_name=agent.display_name,
        auth_type=agent.auth_type,
        allowed_project_ids=agent.allowed_project_ids,
        allowed_repositories=agent.allowed_repositories,
        allowed_actions=agent.allowed_actions,
        created_by_user_id=agent.created_by_user_id,
        enabled=agent.enabled,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
    )


def register_external_agent(
    payload: AgentCreateWithCredentialPolicy,
    user: AuthUser,
    *,
    http_request: Request | None = None,
) -> AgentRegistrationResponse:
    repository = (
        agent_auth_repository if http_request is None
        else external_agent_auth_repository_for_request(http_request)
    )
    _require_authoritative_repository(repository, "registration")
    workspace_id = (
        workspace_id_for_user(user) if http_request is None
        else workspace_id_for_request(user, http_request)
    )
    if get_settings().is_production and not workspace_id:
        raise HTTPException(status_code=403, detail="Workspace membership is required")
    projects = (
        None if http_request is None
        else project_repository_for_request(http_request)
    )
    if is_sqlite_external_agent_auth_repository(repository):
        projects = None
    invalid_project_ids = []
    for project_id in payload.allowed_project_ids:
        if projects is None:
            project = store.projects.get(project_id)
            valid = project is not None and (
                workspace_id is None or project.workspace_id == workspace_id
            )
        else:
            project = projects.get(project_id)
            valid = project is not None and project.workspace_id == workspace_id
        if not valid:
            invalid_project_ids.append(project_id)
    if invalid_project_ids:
        raise HTTPException(status_code=404, detail="One or more allowed projects were not found")

    raw_token = None
    secret_hash = None
    if payload.auth_type in {AgentAuthType.API_KEY, AgentAuthType.WEBHOOK_SECRET}:
        raw_token, secret_hash = _issue_agent_token()

    agent = ExternalAgent(
        provider=payload.provider,
        display_name=payload.display_name.strip(),
        auth_type=payload.auth_type,
        allowed_project_ids=payload.allowed_project_ids,
        allowed_repositories=payload.allowed_repositories,
        allowed_actions=payload.allowed_actions,
        created_by_user_id=user.id,
        workspace_id=workspace_id,
        enabled=payload.enabled,
        secret_hash=secret_hash,
    )
    token_expires_at = None
    if raw_token is not None:
        try:
            token_expires_at = resolve_agent_token_expiry(payload.token_expires_at)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
    credential = AgentCredentialRecord(
        agent_id=agent.id,
        token_expires_at=token_expires_at,
        rate_limit_per_minute=payload.rate_limit_per_minute,
    )
    event = AuditEvent(
        event_type="agent.registered", entity_id=agent.id, workspace_id=workspace_id,
        actor=user.email,
        details={"provider": agent.provider, "display_name": agent.display_name,
                 "auth_type": agent.auth_type, "allowed_project_ids": agent.allowed_project_ids,
                 "allowed_repositories": agent.allowed_repositories,
                 "allowed_actions": agent.allowed_actions},
    )
    if is_sqlite_external_agent_auth_repository(repository):
        store.external_agents[agent.id] = agent
        store.external_agent_credentials[agent.id] = credential
        store.audit_events.append(event)
        store.persist()
    else:
        repository.create(agent=agent, credential=credential, audit_event=event)
    return AgentRegistrationResponse(agent=to_public_agent(agent), agent_token=raw_token)


def rotate_external_agent_token(
    agent: ExternalAgent,
    *,
    credential: AgentCredentialRecord | None = None,
    repository=None,
    actor: str = "system",
    token_expires_at=None,
    rate_limit_per_minute: int | None = None,
) -> AgentRegistrationResponse:
    _require_authoritative_repository(repository, "token rotation")
    if agent.auth_type == AgentAuthType.MANUAL:
        raise HTTPException(status_code=400, detail="Manual agents do not have rotatable tokens")
    now = now_utc()
    try:
        resolved_expiry = resolve_agent_token_expiry(token_expires_at, issued_at=now)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    raw_token, secret_hash = _issue_agent_token()
    credential = credential or _credential_for(agent)
    expected_agent = agent.model_copy(deep=True)
    expected_credential = credential.model_copy(deep=True)
    agent.secret_hash = secret_hash
    agent.updated_at = now
    credential.token_expires_at = resolved_expiry
    credential.token_revoked_at = None
    credential.token_rotated_at = now
    credential.rate_limit_window_started_at = None
    credential.rate_limit_request_count = 0
    if rate_limit_per_minute is not None:
        credential.rate_limit_per_minute = rate_limit_per_minute
    credential.updated_at = now
    event = AuditEvent(
        event_type="agent.token_rotated", entity_id=agent.id,
        workspace_id=agent.workspace_id, actor=actor,
        details={"token_expires_at": credential.token_expires_at,
                 "token_rotated_at": credential.token_rotated_at,
                 "rate_limit_per_minute": credential.rate_limit_per_minute},
        created_at=now,
    )
    if repository is not None and not is_sqlite_external_agent_auth_repository(repository):
        try:
            repository.update(
                agent=agent,
                credential=credential,
                audit_event=event,
                expected_agent=expected_agent,
                expected_credential=expected_credential,
            )
        except PostgresExternalAgentConflictError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Agent credentials changed; reload and retry",
            ) from error
    elif repository is not None:
        store.external_agents[agent.id] = agent
        store.external_agent_credentials[agent.id] = credential
        store.audit_events.append(event)
        store.persist()
    return AgentRegistrationResponse(agent=to_public_agent(agent), agent_token=raw_token)


def revoke_external_agent_token(
    agent: ExternalAgent, *, credential: AgentCredentialRecord | None = None,
    repository=None, actor: str = "system",
) -> ExternalAgentPublic:
    _require_authoritative_repository(repository, "token revocation")
    if agent.auth_type == AgentAuthType.MANUAL:
        raise HTTPException(status_code=400, detail="Manual agents do not have revokable tokens")
    now = now_utc()
    credential = credential or _credential_for(agent)
    expected_agent = agent.model_copy(deep=True)
    expected_credential = credential.model_copy(deep=True)
    agent.secret_hash = None
    agent.updated_at = now
    credential.token_revoked_at = now
    credential.updated_at = now
    event = AuditEvent(
        event_type="agent.token_revoked", entity_id=agent.id,
        workspace_id=agent.workspace_id, actor=actor,
        details={"token_revoked_at": credential.token_revoked_at}, created_at=now,
    )
    if repository is not None and not is_sqlite_external_agent_auth_repository(repository):
        try:
            repository.update(
                agent=agent,
                credential=credential,
                audit_event=event,
                expected_agent=expected_agent,
                expected_credential=expected_credential,
            )
        except PostgresExternalAgentConflictError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Agent credentials changed; reload and retry",
            ) from error
    elif repository is not None:
        store.external_agents[agent.id] = agent
        store.external_agent_credentials[agent.id] = credential
        store.audit_events.append(event)
        store.persist()
    return to_public_agent(agent)


def require_external_agent(
    x_aixion_agent_id: str | None = Header(default=None),
    x_aixion_agent_token: str | None = Header(default=None),
    http_request: Request = None,  # type: ignore[assignment]
) -> ExternalAgent:
    settings = get_settings()
    if http_request is None and (
        settings.is_production or settings.postgres_auth_enabled
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="External agent authentication runtime is unavailable",
            headers={"Retry-After": "5"},
        )
    try:
        repository = (
            agent_auth_repository
            if http_request is None
            else external_agent_auth_repository_for_request(http_request)
        )
    except RuntimeError as error:
        # A missing or mismatched lifespan binding is an unavailable
        # authentication authority, not an invalid credential. Keep the
        # response retryable and fail before the local credential map is read.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="External agent authentication runtime is unavailable",
            headers={"Retry-After": "1"},
        ) from error
    try:
        result = repository.authenticate_and_consume(
            agent_id=x_aixion_agent_id,
            token=x_aixion_agent_token,
            occurred_at=now_utc(),
            window_seconds=int(AGENT_RATE_LIMIT_WINDOW.total_seconds()),
            require_workspace=settings.is_production,
        )
    except psycopg.Error as error:
        # A selected PostgreSQL repository that cannot answer is an unavailable
        # auth authority, never an invalid credential or reason to retry SQLite.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="External agent authentication runtime is unavailable",
            headers={"Retry-After": "1"},
        ) from error
    if isinstance(repository, SQLiteExternalAgentAuthRepository):
        if result.agent is not None:
            store.external_agents[result.agent.id] = result.agent
        if result.credential is not None and result.agent is not None:
            store.external_agent_credentials[result.agent.id] = result.credential
        if result.audit_event is not None:
            store.audit_events.append(result.audit_event)

    if result.reason == "rate_limited":
        retry_after = max(1, result.retry_after_seconds or 1)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="External agent rate limit exceeded",
            headers={"Retry-After": str(retry_after)},
        )
    if result.reason is not None:
        detail_by_reason = {
            "missing_credentials": "Missing external agent credentials",
            "unknown_agent": "External agent is not active",
            "invalid_workspace_scope": "External agent is not active",
            "agent_disabled": "External agent is not active",
            "manual_agent_token_auth": "Manual agents cannot authenticate through agent token",
            "token_revoked_or_missing": "External agent token is revoked or not configured",
            "token_expired": "External agent token expired",
            "invalid_token": "Invalid external agent token",
        }
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail_by_reason.get(result.reason, "External agent is not active"),
        )
    if result.agent is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="External agent is not active",
        )
    return result.agent


def assert_agent_can(
    agent: ExternalAgent,
    action: AgentAction,
    project_id: str | None = None,
    repository_full_name: str | None = None,
    *,
    http_request: Request | None = None,
) -> None:
    settings = get_settings()
    if http_request is None and (
        getattr(settings, "is_production", False)
        or getattr(settings, "postgres_auth_enabled", False)
    ):
        # Scope validation without a request would use module-level SQLite
        # project and membership repositories. Authoritative modes must use
        # the repositories bound to the active application runtime.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="External agent authorization requires an app-bound repository",
            headers={"Retry-After": "1"},
        )
    projects = (
        project_repository_for_request(http_request)
        if http_request is not None
        else None
    )
    memberships = (
        workspace_membership_repository_for_request(http_request)
        if http_request is not None
        else None
    )
    if not external_agent_scope_is_consistent(
        agent,
        projects=projects,
        memberships=memberships,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="External agent ownership scope is inconsistent",
        )
    action_value = _action_value(action)
    allowed_actions = {_action_value(allowed_action) for allowed_action in agent.allowed_actions}
    if action_value not in allowed_actions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Agent is not allowed to perform {action_value}",
        )
    if project_id and agent.allowed_project_ids and project_id not in agent.allowed_project_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Agent is not allowed for this project",
        )
    if project_id:
        project = (projects or project_repository).get(project_id)
        if project is None or project.workspace_id != agent.workspace_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Agent is not allowed for this project",
            )
    if (
        repository_full_name
        and agent.allowed_repositories
        and repository_full_name not in agent.allowed_repositories
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Agent is not allowed for this repository",
        )


def mark_agent_updated(agent: ExternalAgent) -> None:
    agent.updated_at = now_utc()
    store.persist()
