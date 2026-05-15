from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta

from fastapi import Header, HTTPException, status

from .agent_credential_models import (
    AgentCredentialRecord,
    AgentCredentialState,
    AgentCredentialStatus,
    AgentCreateWithCredentialPolicy,
)
from .models import (
    AgentAction,
    AgentAuthType,
    AgentRegistrationResponse,
    AuditEvent,
    AuthUser,
    ExternalAgent,
    ExternalAgentPublic,
    now_utc,
)
from .store import store

AGENT_RATE_LIMIT_WINDOW = timedelta(minutes=1)


def _hash_agent_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _safe_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


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


def _credential_state(agent: ExternalAgent) -> AgentCredentialState:
    credential = _credential_for(agent)
    if not agent.enabled:
        return AgentCredentialState.DISABLED
    if agent.auth_type == AgentAuthType.MANUAL:
        return AgentCredentialState.MANUAL
    if credential.token_revoked_at is not None:
        return AgentCredentialState.REVOKED
    if not agent.secret_hash:
        return AgentCredentialState.NOT_CONFIGURED
    if credential.token_expires_at is not None and credential.token_expires_at <= now_utc():
        return AgentCredentialState.EXPIRED
    return AgentCredentialState.ACTIVE


def to_credential_status(agent: ExternalAgent) -> AgentCredentialStatus:
    credential = _credential_for(agent)
    return AgentCredentialStatus(
        agent_id=agent.id,
        auth_type=agent.auth_type,
        credential_state=_credential_state(agent),
        token_present=bool(agent.secret_hash),
        token_expires_at=credential.token_expires_at,
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


def _audit_agent_auth_failure(
    *,
    agent_id: str | None,
    reason: str,
    agent: ExternalAgent | None = None,
) -> None:
    if agent is not None:
        credential = _credential_for(agent)
        credential.failed_auth_count += 1
        credential.last_auth_failed_at = now_utc()
        credential.updated_at = credential.last_auth_failed_at
    store.audit_events.append(
        AuditEvent(
            event_type="agent.auth_failed",
            entity_id=agent_id or "unknown_agent",
            actor="external-agent-auth",
            details={"agent_id": agent_id, "reason": reason},
        )
    )
    store.persist()


def _audit_agent_rate_limit(agent: ExternalAgent, credential: AgentCredentialRecord) -> None:
    store.audit_events.append(
        AuditEvent(
            event_type="agent.rate_limited",
            entity_id=agent.id,
            actor=f"agent:{agent.id}",
            details={
                "agent_id": agent.id,
                "limit": credential.rate_limit_per_minute,
                "window_started_at": credential.rate_limit_window_started_at,
                "request_count": credential.rate_limit_request_count,
            },
        )
    )
    store.persist()


def _enforce_agent_rate_limit(agent: ExternalAgent) -> None:
    credential = _credential_for(agent)
    now = now_utc()
    window_started = credential.rate_limit_window_started_at
    if window_started is None or window_started + AGENT_RATE_LIMIT_WINDOW <= now:
        credential.rate_limit_window_started_at = now
        credential.rate_limit_request_count = 1
        credential.updated_at = now
        return

    if credential.rate_limit_request_count >= credential.rate_limit_per_minute:
        credential.updated_at = now
        _audit_agent_rate_limit(agent, credential)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="External agent rate limit exceeded",
        )

    credential.rate_limit_request_count += 1
    credential.updated_at = now


def register_external_agent(
    payload: AgentCreateWithCredentialPolicy,
    user: AuthUser,
) -> AgentRegistrationResponse:
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
        enabled=payload.enabled,
        secret_hash=secret_hash,
    )
    store.external_agents[agent.id] = agent
    store.external_agent_credentials[agent.id] = AgentCredentialRecord(
        agent_id=agent.id,
        token_expires_at=payload.token_expires_at,
        rate_limit_per_minute=payload.rate_limit_per_minute,
    )
    store.persist()
    return AgentRegistrationResponse(agent=to_public_agent(agent), agent_token=raw_token)


def rotate_external_agent_token(
    agent: ExternalAgent,
    *,
    token_expires_at=None,
    rate_limit_per_minute: int | None = None,
) -> AgentRegistrationResponse:
    if agent.auth_type == AgentAuthType.MANUAL:
        raise HTTPException(status_code=400, detail="Manual agents do not have rotatable tokens")
    raw_token, secret_hash = _issue_agent_token()
    now = now_utc()
    credential = _credential_for(agent)
    agent.secret_hash = secret_hash
    agent.updated_at = now
    credential.token_expires_at = token_expires_at
    credential.token_revoked_at = None
    credential.token_rotated_at = now
    credential.rate_limit_window_started_at = None
    credential.rate_limit_request_count = 0
    if rate_limit_per_minute is not None:
        credential.rate_limit_per_minute = rate_limit_per_minute
    credential.updated_at = now
    return AgentRegistrationResponse(agent=to_public_agent(agent), agent_token=raw_token)


def revoke_external_agent_token(agent: ExternalAgent) -> ExternalAgentPublic:
    if agent.auth_type == AgentAuthType.MANUAL:
        raise HTTPException(status_code=400, detail="Manual agents do not have revokable tokens")
    now = now_utc()
    credential = _credential_for(agent)
    agent.secret_hash = None
    agent.updated_at = now
    credential.token_revoked_at = now
    credential.updated_at = now
    return to_public_agent(agent)


def require_external_agent(
    x_aixion_agent_id: str | None = Header(default=None),
    x_aixion_agent_token: str | None = Header(default=None),
) -> ExternalAgent:
    if not x_aixion_agent_id or not x_aixion_agent_token:
        _audit_agent_auth_failure(agent_id=x_aixion_agent_id, reason="missing_credentials")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing external agent credentials",
        )

    agent = store.external_agents.get(x_aixion_agent_id)
    if not agent:
        _audit_agent_auth_failure(agent_id=x_aixion_agent_id, reason="unknown_agent")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="External agent is not active",
        )
    if not agent.enabled:
        _audit_agent_auth_failure(agent_id=agent.id, reason="agent_disabled", agent=agent)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="External agent is not active",
        )
    if agent.auth_type == AgentAuthType.MANUAL:
        _audit_agent_auth_failure(agent_id=agent.id, reason="manual_agent_token_auth", agent=agent)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Manual agents cannot authenticate through agent token",
        )

    credential = _credential_for(agent)
    if not agent.secret_hash or credential.token_revoked_at is not None:
        _audit_agent_auth_failure(agent_id=agent.id, reason="token_revoked_or_missing", agent=agent)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="External agent token is revoked or not configured",
        )
    if credential.token_expires_at is not None and credential.token_expires_at <= now_utc():
        _audit_agent_auth_failure(agent_id=agent.id, reason="token_expired", agent=agent)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="External agent token expired",
        )
    if not _safe_equal(agent.secret_hash, _hash_agent_token(x_aixion_agent_token)):
        _audit_agent_auth_failure(agent_id=agent.id, reason="invalid_token", agent=agent)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid external agent token",
        )

    _enforce_agent_rate_limit(agent)
    credential.last_used_at = now_utc()
    credential.updated_at = credential.last_used_at
    store.persist()
    return agent


def assert_agent_can(
    agent: ExternalAgent,
    action: AgentAction,
    project_id: str | None = None,
    repository_full_name: str | None = None,
) -> None:
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
