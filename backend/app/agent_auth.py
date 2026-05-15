from __future__ import annotations

import hashlib
import hmac
import secrets

from fastapi import Header, HTTPException, status

from .models import AgentAction, AgentAuthType, AgentRegistrationResponse, AgentCreate, AuthUser, ExternalAgent, ExternalAgentPublic, now_utc
from .store import store


def _hash_agent_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _safe_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _action_value(action: AgentAction | str) -> str:
    return action.value if isinstance(action, AgentAction) else str(action)


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


def register_external_agent(payload: AgentCreate, user: AuthUser) -> AgentRegistrationResponse:
    raw_token = None
    secret_hash = None
    if payload.auth_type in {AgentAuthType.API_KEY, AgentAuthType.WEBHOOK_SECRET}:
        raw_token = f"aixion_agent_{secrets.token_urlsafe(40)}"
        secret_hash = _hash_agent_token(raw_token)

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
    store.persist()
    return AgentRegistrationResponse(agent=to_public_agent(agent), agent_token=raw_token)


def require_external_agent(x_aixion_agent_id: str | None = Header(default=None), x_aixion_agent_token: str | None = Header(default=None)) -> ExternalAgent:
    if not x_aixion_agent_id or not x_aixion_agent_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing external agent credentials")

    agent = store.external_agents.get(x_aixion_agent_id)
    if not agent or not agent.enabled:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="External agent is not active")
    if agent.auth_type == AgentAuthType.MANUAL:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Manual agents cannot authenticate through agent token")
    if not agent.secret_hash or not _safe_equal(agent.secret_hash, _hash_agent_token(x_aixion_agent_token)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid external agent token")
    return agent


def assert_agent_can(agent: ExternalAgent, action: AgentAction, project_id: str | None = None, repository_full_name: str | None = None) -> None:
    action_value = _action_value(action)
    allowed_actions = {_action_value(allowed_action) for allowed_action in agent.allowed_actions}
    if action_value not in allowed_actions:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Agent is not allowed to perform {action_value}")
    if project_id and agent.allowed_project_ids and project_id not in agent.allowed_project_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Agent is not allowed for this project")
    if repository_full_name and agent.allowed_repositories and repository_full_name not in agent.allowed_repositories:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Agent is not allowed for this repository")


def mark_agent_updated(agent: ExternalAgent) -> None:
    agent.updated_at = now_utc()
    store.persist()
