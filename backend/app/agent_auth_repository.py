from __future__ import annotations

import hashlib
import hmac
import math
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from .agent_credential_models import AgentCredentialRecord
from .models import AgentAuthType, AuditEvent, ExternalAgent, Project, Workspace


@dataclass(frozen=True)
class AgentAuthenticationResult:
    agent: ExternalAgent | None
    credential: AgentCredentialRecord | None
    reason: str | None = None
    retry_after_seconds: int | None = None
    audit_event: AuditEvent | None = None


class ExternalAgentAuthenticationRepository(Protocol):
    def create(
        self, *, agent: ExternalAgent, credential: AgentCredentialRecord,
        audit_event: AuditEvent,
    ) -> None: ...

    def get(self, agent_id: str) -> tuple[ExternalAgent, AgentCredentialRecord] | None: ...

    def list_for_workspace(
        self, workspace_id: str
    ) -> list[tuple[ExternalAgent, AgentCredentialRecord]]: ...

    def update(
        self, *, agent: ExternalAgent, credential: AgentCredentialRecord,
        audit_event: AuditEvent | None = None,
    ) -> None: ...

    def authenticate_and_consume(
        self,
        *,
        agent_id: str | None,
        token: str | None,
        occurred_at: datetime,
        window_seconds: int,
        require_workspace: bool,
    ) -> AgentAuthenticationResult: ...


class SQLiteExternalAgentAuthRepository:
    """Authenticate and account for an agent request against durable SQLite rows."""

    def __init__(self, connect: Callable[[], sqlite3.Connection]) -> None:
        self._connect = connect

    def authenticate_and_consume(
        self,
        *,
        agent_id: str | None,
        token: str | None,
        occurred_at: datetime,
        window_seconds: int,
        require_workspace: bool,
    ) -> AgentAuthenticationResult:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            agent_row = None
            credential_row = None
            if agent_id:
                agent_row = conn.execute(
                    "SELECT payload FROM kv_store WHERE entity_type=? AND entity_id=?",
                    ("external_agent", agent_id),
                ).fetchone()
                credential_row = conn.execute(
                    "SELECT payload FROM kv_store WHERE entity_type=? AND entity_id=?",
                    ("external_agent_credential", agent_id),
                ).fetchone()

            agent = ExternalAgent.model_validate_json(agent_row[0]) if agent_row else None
            credential = (
                AgentCredentialRecord.model_validate_json(credential_row[0])
                if credential_row
                else None
            )

            reason = self._failure_reason(
                agent=agent,
                credential=credential,
                agent_id=agent_id,
                token=token,
                require_workspace=require_workspace,
                occurred_at=occurred_at,
                conn=conn,
            )
            if reason is not None:
                updated_credential, auth_event = self._record_auth_failure(
                    agent_id=agent_id,
                    credential=credential,
                    reason=reason,
                    occurred_at=occurred_at,
                    conn=conn,
                )
                conn.commit()
                return AgentAuthenticationResult(
                    agent=agent,
                    credential=updated_credential,
                    reason=reason,
                    audit_event=auth_event,
                )

            assert agent is not None and credential is not None
            window_started = credential.rate_limit_window_started_at
            retry_after_seconds: int | None = None
            if window_started is None or window_started + timedelta(seconds=window_seconds) <= occurred_at:
                credential.rate_limit_window_started_at = occurred_at
                credential.rate_limit_request_count = 1
                credential.last_used_at = occurred_at
            elif credential.rate_limit_request_count >= credential.rate_limit_per_minute:
                retry_after_seconds = max(
                    1,
                    math.ceil(
                        (window_started + timedelta(seconds=window_seconds) - occurred_at).total_seconds()
                    ),
                )
            else:
                credential.rate_limit_request_count += 1
                credential.last_used_at = occurred_at
            credential.updated_at = occurred_at
            conn.execute(
                "UPDATE kv_store SET payload=? WHERE entity_type=? AND entity_id=?",
                (credential.model_dump_json(), "external_agent_credential", agent.id),
            )
            rate_limit_event = None
            if retry_after_seconds is not None:
                rate_limit_event = AuditEvent(
                    event_type="agent.rate_limited",
                    entity_id=agent.id,
                    actor=f"agent:{agent.id}",
                    details={
                        "agent_id": agent.id,
                        "limit": credential.rate_limit_per_minute,
                        "window_started_at": window_started,
                        "request_count": credential.rate_limit_request_count,
                    },
                )
                conn.execute(
                    "INSERT INTO kv_store(entity_type, entity_id, payload) VALUES (?, ?, ?)",
                    ("audit_event", rate_limit_event.id, rate_limit_event.model_dump_json()),
                )
            conn.commit()
            return AgentAuthenticationResult(
                agent=agent,
                credential=credential,
                reason="rate_limited" if retry_after_seconds is not None else None,
                retry_after_seconds=retry_after_seconds,
                audit_event=rate_limit_event,
            )

    @staticmethod
    def _failure_reason(
        *,
        agent: ExternalAgent | None,
        credential: AgentCredentialRecord | None,
        agent_id: str | None,
        token: str | None,
        require_workspace: bool,
        occurred_at: datetime,
        conn: sqlite3.Connection,
    ) -> str | None:
        if not agent_id or not token:
            return "missing_credentials"
        if agent is None or agent.id != agent_id:
            return "unknown_agent"
        if agent.auth_type == AgentAuthType.MANUAL:
            return "manual_agent_token_auth"
        if credential is None or credential.agent_id != agent.id:
            return "token_revoked_or_missing"
        if not SQLiteExternalAgentAuthRepository._scope_is_consistent(
            agent, require_workspace=require_workspace, conn=conn
        ):
            return "invalid_workspace_scope"
        if not agent.enabled:
            return "agent_disabled"
        if not agent.secret_hash or credential.token_revoked_at is not None:
            return "token_revoked_or_missing"
        if credential.token_expires_at is not None and credential.token_expires_at <= occurred_at:
            return "token_expired"
        presented_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(agent.secret_hash, presented_hash):
            return "invalid_token"
        return None

    @staticmethod
    def _scope_is_consistent(
        agent: ExternalAgent,
        *,
        require_workspace: bool,
        conn: sqlite3.Connection,
    ) -> bool:
        if require_workspace and agent.workspace_id is None:
            return False
        if agent.workspace_id is not None:
            workspace_row = conn.execute(
                "SELECT payload FROM kv_store WHERE entity_type=? AND entity_id=?",
                ("workspace", agent.workspace_id),
            ).fetchone()
            if workspace_row is None:
                return False
            workspace = Workspace.model_validate_json(workspace_row[0])
            if workspace.id != agent.workspace_id:
                return False
        for project_id in agent.allowed_project_ids:
            project_row = conn.execute(
                "SELECT payload FROM kv_store WHERE entity_type=? AND entity_id=?",
                ("project", project_id),
            ).fetchone()
            if project_row is None:
                return False
            project = Project.model_validate_json(project_row[0])
            if project.id != project_id or project.workspace_id != agent.workspace_id:
                return False
        return True

    def _record_auth_failure(
        self,
        *,
        agent_id: str | None,
        credential: AgentCredentialRecord | None,
        reason: str,
        occurred_at: datetime,
        conn: sqlite3.Connection,
    ) -> tuple[AgentCredentialRecord | None, AuditEvent]:
        updated_credential = None
        if credential is not None and agent_id is not None:
            updated_credential = credential.model_copy(
                update={
                    "failed_auth_count": credential.failed_auth_count + 1,
                    "last_auth_failed_at": occurred_at,
                    "updated_at": occurred_at,
                }
            )
            conn.execute(
                "UPDATE kv_store SET payload=? WHERE entity_type=? AND entity_id=?",
                (
                    updated_credential.model_dump_json(),
                    "external_agent_credential",
                    agent_id,
                ),
            )
        event = AuditEvent(
            event_type="agent.auth_failed",
            entity_id=agent_id or "unknown_agent",
            actor="external-agent-auth",
            details={"agent_id": agent_id, "reason": reason},
        )
        conn.execute(
            "INSERT INTO kv_store(entity_type, entity_id, payload) VALUES (?, ?, ?)",
            ("audit_event", event.id, event.model_dump_json()),
        )
        return updated_credential, event
