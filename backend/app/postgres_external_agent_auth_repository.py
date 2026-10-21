from __future__ import annotations

import hashlib
import hmac
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from psycopg import Connection
from psycopg.types.json import Jsonb

from .agent_auth_repository import AgentAuthenticationResult
from .agent_credential_models import AgentCredentialRecord
from .agent_token_lifetime import effective_agent_token_expiry
from .models import AgentAuthType, AuditEvent, ExternalAgent, Project, Workspace


class PostgresExternalAgentIntegrityError(RuntimeError):
    """A normalized external-agent row disagrees with its JSON payload."""


@dataclass(frozen=True)
class PostgresExternalAgentAuthRepository:
    """Workspace-scoped external-agent persistence and atomic token accounting."""

    _connect: Callable[[], Connection]

    @staticmethod
    def _validate_registration_audit(agent: ExternalAgent, event: AuditEvent) -> None:
        expected_details = {
            "provider": agent.provider,
            "display_name": agent.display_name,
            "auth_type": agent.auth_type,
            "allowed_project_ids": agent.allowed_project_ids,
            "allowed_repositories": agent.allowed_repositories,
            "allowed_actions": agent.allowed_actions,
        }
        if (
            event.event_type != "agent.registered"
            or event.entity_id != agent.id
            or event.workspace_id != agent.workspace_id
            or not event.actor
            or any(event.details.get(key) != value for key, value in expected_details.items())
        ):
            raise ValueError(
                "External-agent registration audit identity, scope, and details must match"
            )

    @staticmethod
    def _agent_from_row(row: tuple[object, ...]) -> ExternalAgent:
        agent = ExternalAgent.model_validate(row[0])
        if (
            agent.id != row[1]
            or agent.workspace_id != row[2]
            or agent.provider.value != row[3]
            or agent.auth_type.value != row[4]
            or agent.enabled is not row[5]
            or agent.secret_hash != row[6]
        ):
            raise PostgresExternalAgentIntegrityError(
                "External-agent payload does not match normalized columns"
            )
        return agent

    @staticmethod
    def _credential_from_row(row: tuple[object, ...]) -> AgentCredentialRecord:
        credential = AgentCredentialRecord.model_validate(row[0])
        if (
            credential.id != row[1]
            or credential.agent_id != row[2]
            or credential.token_expires_at != row[4]
            or credential.token_revoked_at != row[5]
            or credential.token_rotated_at != row[6]
            or credential.last_used_at != row[7]
            or credential.failed_auth_count != row[8]
            or credential.rate_limit_per_minute != row[9]
            or credential.rate_limit_window_started_at != row[10]
            or credential.rate_limit_request_count != row[11]
            or credential.created_at != row[12]
            or credential.updated_at != row[13]
        ):
            raise PostgresExternalAgentIntegrityError(
                "External-agent credential payload does not match normalized columns"
            )
        return credential

    def create(
        self,
        *,
        agent: ExternalAgent,
        credential: AgentCredentialRecord,
        audit_event: AuditEvent,
    ) -> None:
        if (
            not agent.workspace_id
            or not agent.created_by_user_id
            or credential.agent_id != agent.id
            or credential.rate_limit_per_minute < 1
            or credential.rate_limit_request_count < 0
        ):
            raise ValueError("External agent requires a valid workspace, creator, and credential")
        self._validate_registration_audit(agent, audit_event)

        with self._connect() as conn:
            conn.execute(
                "INSERT INTO external_agents"
                "(agent_id,workspace_id,created_by_user_id,provider,auth_type,enabled,secret_hash,payload) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    agent.id,
                    agent.workspace_id,
                    agent.created_by_user_id,
                    agent.provider.value,
                    agent.auth_type.value,
                    agent.enabled,
                    agent.secret_hash,
                    Jsonb(agent.model_dump(mode="json")),
                ),
            )
            conn.execute(
                "INSERT INTO external_agent_credentials"
                "(credential_id,agent_id,workspace_id,token_expires_at,token_revoked_at,"
                "token_rotated_at,last_used_at,failed_auth_count,rate_limit_per_minute,"
                "rate_limit_window_started_at,rate_limit_request_count,created_at,updated_at,payload) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    credential.id,
                    agent.id,
                    agent.workspace_id,
                    credential.token_expires_at,
                    credential.token_revoked_at,
                    credential.token_rotated_at,
                    credential.last_used_at,
                    credential.failed_auth_count,
                    credential.rate_limit_per_minute,
                    credential.rate_limit_window_started_at,
                    credential.rate_limit_request_count,
                    credential.created_at,
                    credential.updated_at,
                    Jsonb(credential.model_dump(mode="json")),
                ),
            )
            self._insert_audit(conn, audit_event)

    def get(self, agent_id: str) -> tuple[ExternalAgent, AgentCredentialRecord] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT a.payload,a.agent_id,a.workspace_id,a.provider,a.auth_type,a.enabled,a.secret_hash,"
                "c.payload,c.credential_id,c.agent_id,c.workspace_id,c.token_expires_at,c.token_revoked_at,"
                "c.token_rotated_at,c.last_used_at,c.failed_auth_count,c.rate_limit_per_minute,"
                "c.rate_limit_window_started_at,c.rate_limit_request_count,c.created_at,c.updated_at "
                "FROM external_agents AS a LEFT JOIN external_agent_credentials AS c "
                "ON c.agent_id=a.agent_id AND c.workspace_id=a.workspace_id "
                "WHERE a.agent_id=%s",
                (agent_id,),
            ).fetchone()
        if row is None:
            return None
        if row[7] is None:
            raise PostgresExternalAgentIntegrityError(
                "External agent is missing its credential record"
            )
        agent = self._agent_from_row(row[:7])
        credential = self._credential_from_row((row[7], *row[8:21]))
        if agent.id != credential.agent_id or agent.workspace_id != row[10]:
            raise PostgresExternalAgentIntegrityError(
                "External-agent credential scope does not match"
            )
        return agent, credential

    def list_for_workspace(
        self, workspace_id: str
    ) -> list[tuple[ExternalAgent, AgentCredentialRecord]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT a.payload,a.agent_id,a.workspace_id,a.provider,a.auth_type,a.enabled,a.secret_hash,"
                "c.payload,c.credential_id,c.agent_id,c.workspace_id,c.token_expires_at,c.token_revoked_at,"
                "c.token_rotated_at,c.last_used_at,c.failed_auth_count,c.rate_limit_per_minute,"
                "c.rate_limit_window_started_at,c.rate_limit_request_count,c.created_at,c.updated_at "
                "FROM external_agents AS a LEFT JOIN external_agent_credentials AS c "
                "ON c.agent_id=a.agent_id AND c.workspace_id=a.workspace_id "
                "WHERE a.workspace_id=%s ORDER BY a.created_by_user_id,a.agent_id",
                (workspace_id,),
            ).fetchall()

        result: list[tuple[ExternalAgent, AgentCredentialRecord]] = []
        for row in rows:
            if row[7] is None:
                raise PostgresExternalAgentIntegrityError(
                    "External agent is missing its credential record"
                )
            agent = self._agent_from_row(row[:7])
            credential = self._credential_from_row((row[7], *row[8:21]))
            if agent.workspace_id != workspace_id or credential.agent_id != agent.id:
                raise PostgresExternalAgentIntegrityError(
                    "External-agent listing crossed workspace scope"
                )
            result.append((agent, credential))
        return result

    def update(
        self,
        *,
        agent: ExternalAgent,
        credential: AgentCredentialRecord,
        audit_event: AuditEvent | None = None,
    ) -> None:
        if (
            not agent.workspace_id
            or credential.agent_id != agent.id
            or credential.rate_limit_per_minute < 1
            or credential.rate_limit_request_count < 0
            or (
                audit_event is not None
                and (
                    audit_event.entity_id != agent.id
                    or audit_event.workspace_id != agent.workspace_id
                    or not audit_event.actor
                    or audit_event.event_type not in {"agent.token_rotated", "agent.token_revoked"}
                )
            )
        ):
            raise ValueError("External-agent update fields are invalid")

        with self._connect() as conn:
            agent_result = conn.execute(
                "UPDATE external_agents SET created_by_user_id=%s,provider=%s,auth_type=%s,"
                "enabled=%s,secret_hash=%s,payload=%s WHERE agent_id=%s AND workspace_id=%s",
                (
                    agent.created_by_user_id,
                    agent.provider.value,
                    agent.auth_type.value,
                    agent.enabled,
                    agent.secret_hash,
                    Jsonb(agent.model_dump(mode="json")),
                    agent.id,
                    agent.workspace_id,
                ),
            )
            credential_result = conn.execute(
                "UPDATE external_agent_credentials SET token_expires_at=%s,token_revoked_at=%s,"
                "token_rotated_at=%s,last_used_at=%s,failed_auth_count=%s,rate_limit_per_minute=%s,"
                "rate_limit_window_started_at=%s,rate_limit_request_count=%s,updated_at=%s,payload=%s "
                "WHERE agent_id=%s AND workspace_id=%s",
                (
                    credential.token_expires_at,
                    credential.token_revoked_at,
                    credential.token_rotated_at,
                    credential.last_used_at,
                    credential.failed_auth_count,
                    credential.rate_limit_per_minute,
                    credential.rate_limit_window_started_at,
                    credential.rate_limit_request_count,
                    credential.updated_at,
                    Jsonb(credential.model_dump(mode="json")),
                    agent.id,
                    agent.workspace_id,
                ),
            )
            if agent_result.rowcount != 1 or credential_result.rowcount != 1:
                raise PostgresExternalAgentIntegrityError(
                    "External agent or credential changed during update"
                )
            if audit_event is not None:
                self._insert_audit(conn, audit_event)

    def authenticate_and_consume(
        self,
        *,
        agent_id: str | None,
        token: str | None,
        occurred_at: datetime,
        window_seconds: int,
        require_workspace: bool,
    ) -> AgentAuthenticationResult:
        if occurred_at.tzinfo is None or window_seconds < 1:
            raise ValueError("Authentication time and rate-limit window must be valid")

        with self._connect() as conn:
            agent_row = None
            credential_row = None
            if agent_id:
                agent_row = conn.execute(
                    "SELECT payload,agent_id,workspace_id,provider,auth_type,enabled,secret_hash "
                    "FROM external_agents WHERE agent_id=%s FOR UPDATE",
                    (agent_id,),
                ).fetchone()
                credential_row = conn.execute(
                    "SELECT payload,credential_id,agent_id,workspace_id,token_expires_at,"
                    "token_revoked_at,token_rotated_at,last_used_at,failed_auth_count,"
                    "rate_limit_per_minute,rate_limit_window_started_at,rate_limit_request_count,"
                    "created_at,updated_at FROM external_agent_credentials "
                    "WHERE agent_id=%s FOR UPDATE",
                    (agent_id,),
                ).fetchone()

            agent = self._agent_from_row(agent_row) if agent_row is not None else None
            credential = (
                self._credential_from_row(credential_row)
                if credential_row is not None
                else None
            )
            if (
                (agent is None) != (credential is None)
                or (
                    agent is not None
                    and credential is not None
                    and (agent.id != credential.agent_id or agent.workspace_id != credential_row[3])
                )
            ):
                raise PostgresExternalAgentIntegrityError(
                    "External agent and credential records are inconsistent"
                )

            reason = self._failure_reason(
                agent=agent,
                credential=credential,
                agent_id=agent_id,
                token=token,
                occurred_at=occurred_at,
                require_workspace=require_workspace,
                conn=conn,
            )
            if reason is not None:
                updated_credential = credential
                if credential is not None and agent is not None:
                    updated_credential = credential.model_copy(
                        update={
                            "failed_auth_count": credential.failed_auth_count + 1,
                            "last_auth_failed_at": occurred_at,
                            "updated_at": occurred_at,
                        }
                    )
                    self._write_credential(conn, agent, updated_credential)
                audit_event = self._audit_event(
                    agent=agent,
                    event_type="agent.auth_failed",
                    actor="external-agent-auth",
                    details={"agent_id": agent_id, "reason": reason},
                    occurred_at=occurred_at,
                )
                if audit_event is not None:
                    self._insert_audit(conn, audit_event)
                return AgentAuthenticationResult(
                    agent=agent,
                    credential=updated_credential,
                    reason=reason,
                    audit_event=audit_event,
                )

            assert agent is not None and credential is not None
            window_started = credential.rate_limit_window_started_at
            retry_after_seconds = None
            if window_started is None or window_started + timedelta(seconds=window_seconds) <= occurred_at:
                updated = credential.model_copy(
                    update={
                        "rate_limit_window_started_at": occurred_at,
                        "rate_limit_request_count": 1,
                        "last_used_at": occurred_at,
                        "updated_at": occurred_at,
                    }
                )
            elif credential.rate_limit_request_count >= credential.rate_limit_per_minute:
                retry_after_seconds = max(
                    1,
                    math.ceil(
                        (window_started + timedelta(seconds=window_seconds) - occurred_at).total_seconds()
                    ),
                )
                updated = credential.model_copy(update={"updated_at": occurred_at})
            else:
                updated = credential.model_copy(
                    update={
                        "rate_limit_request_count": credential.rate_limit_request_count + 1,
                        "last_used_at": occurred_at,
                        "updated_at": occurred_at,
                    }
                )
            self._write_credential(conn, agent, updated)

            audit_event = None
            reason = None
            if retry_after_seconds is not None:
                audit_event = self._audit_event(
                    agent=agent,
                    event_type="agent.rate_limited",
                    actor=f"agent:{agent.id}",
                    details={
                        "agent_id": agent.id,
                        "limit": credential.rate_limit_per_minute,
                        "window_started_at": window_started,
                        "request_count": credential.rate_limit_request_count,
                    },
                    occurred_at=occurred_at,
                )
                if audit_event is not None:
                    self._insert_audit(conn, audit_event)
                reason = "rate_limited"

            return AgentAuthenticationResult(
                agent=agent,
                credential=updated,
                reason=reason,
                retry_after_seconds=retry_after_seconds,
                audit_event=audit_event,
            )

    @staticmethod
    def _failure_reason(
        *,
        agent: ExternalAgent | None,
        credential: AgentCredentialRecord | None,
        agent_id: str | None,
        token: str | None,
        occurred_at: datetime,
        require_workspace: bool,
        conn: Connection,
    ) -> str | None:
        if not agent_id or not token:
            return "missing_credentials"
        if agent is None or agent.id != agent_id:
            return "unknown_agent"
        if agent.auth_type == AgentAuthType.MANUAL:
            return "manual_agent_token_auth"
        if credential is None or credential.agent_id != agent.id:
            return "token_revoked_or_missing"
        if require_workspace and not agent.workspace_id:
            return "invalid_workspace_scope"
        if not agent.workspace_id:
            return "invalid_workspace_scope"
        workspace_row = conn.execute(
            "SELECT payload FROM workspaces WHERE workspace_id=%s",
            (agent.workspace_id,),
        ).fetchone()
        if workspace_row is None:
            return "invalid_workspace_scope"
        workspace = Workspace.model_validate(workspace_row[0])
        if workspace.id != agent.workspace_id:
            return "invalid_workspace_scope"
        for project_id in agent.allowed_project_ids:
            project_row = conn.execute(
                "SELECT payload,project_id,workspace_id FROM projects WHERE project_id=%s",
                (project_id,),
            ).fetchone()
            if project_row is None:
                return "invalid_workspace_scope"
            project = Project.model_validate(project_row[0])
            if (
                project.id != project_row[1]
                or project.workspace_id != project_row[2]
                or project.id != project_id
                or project.workspace_id != agent.workspace_id
            ):
                return "invalid_workspace_scope"
        if not agent.enabled:
            return "agent_disabled"
        if not agent.secret_hash or credential.token_revoked_at is not None:
            return "token_revoked_or_missing"
        if effective_agent_token_expiry(credential) <= occurred_at:
            return "token_expired"
        presented_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(agent.secret_hash, presented_hash):
            return "invalid_token"
        return None

    @staticmethod
    def _write_credential(
        conn: Connection,
        agent: ExternalAgent,
        credential: AgentCredentialRecord,
    ) -> None:
        result = conn.execute(
            "UPDATE external_agent_credentials SET token_expires_at=%s,token_revoked_at=%s,"
            "token_rotated_at=%s,last_used_at=%s,failed_auth_count=%s,rate_limit_per_minute=%s,"
            "rate_limit_window_started_at=%s,rate_limit_request_count=%s,updated_at=%s,payload=%s "
            "WHERE agent_id=%s AND workspace_id=%s",
            (
                credential.token_expires_at,
                credential.token_revoked_at,
                credential.token_rotated_at,
                credential.last_used_at,
                credential.failed_auth_count,
                credential.rate_limit_per_minute,
                credential.rate_limit_window_started_at,
                credential.rate_limit_request_count,
                credential.updated_at,
                Jsonb(credential.model_dump(mode="json")),
                agent.id,
                agent.workspace_id,
            ),
        )
        if result.rowcount != 1:
            raise PostgresExternalAgentIntegrityError(
                "External-agent credential disappeared during authentication"
            )

    @staticmethod
    def _audit_event(
        *,
        agent: ExternalAgent | None,
        event_type: str,
        actor: str,
        details: dict[str, object],
        occurred_at: datetime,
    ) -> AuditEvent | None:
        # PostgreSQL's audit contract requires a real workspace foreign key.
        # Never invent a workspace for unknown or unscoped credentials.
        if agent is None or not agent.workspace_id:
            return None
        return AuditEvent(
            event_type=event_type,
            actor=actor,
            entity_id=agent.id,
            workspace_id=agent.workspace_id,
            details=details,
            created_at=occurred_at,
        )

    @staticmethod
    def _insert_audit(conn: Connection, event: AuditEvent) -> None:
        conn.execute(
            "INSERT INTO audit_events"
            "(event_id,workspace_id,approval_id,entity_type,entity_id,event_type,actor,details,created_at,payload) "
            "VALUES(%s,%s,NULL,'external_agent',%s,%s,%s,%s,%s,%s)",
            (
                event.id,
                event.workspace_id,
                event.entity_id,
                event.event_type,
                event.actor,
                Jsonb(event.model_dump(mode="json")["details"]),
                event.created_at,
                Jsonb(event.model_dump(mode="json")),
            ),
        )
