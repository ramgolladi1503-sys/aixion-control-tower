from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from psycopg import Connection
from psycopg.types.json import Jsonb

from .connector_models import AgentConnector
from .models import AuditEvent


class ConnectorRepositoryError(RuntimeError):
    """A connector record could not be read or committed safely."""


class ConnectorRepositoryScopeError(ConnectorRepositoryError):
    """Connector, actor, allowed project, or audit workspace does not match."""


class ConnectorRepositoryConflict(ConnectorRepositoryError):
    """A connector changed after the caller read it."""


class ConnectorRepository(Protocol):
    def get_for_workspace(
        self, connector_id: str, *, workspace_id: str
    ) -> AgentConnector | None: ...

    def get_for_authentication(self, connector_id: str) -> AgentConnector | None: ...

    def list_for_workspace(self, workspace_id: str) -> list[AgentConnector]: ...

    def create(self, *, connector: AgentConnector, audit_event: AuditEvent) -> None: ...

    def update(
        self,
        *,
        connector: AgentConnector,
        expected_updated_at: datetime,
        audit_event: AuditEvent,
    ) -> None: ...

    def commit_webhook_action(
        self,
        *,
        connector: AgentConnector,
        request_nonce: str | None,
        expected_updated_at: datetime,
        audit_event: AuditEvent,
        write_action: Callable[[Connection], object],
    ) -> object: ...


def _validate_audit(connector: AgentConnector, audit_event: AuditEvent) -> None:
    if (
        not connector.workspace_id
        or audit_event.entity_id != connector.id
        or audit_event.workspace_id != connector.workspace_id
        or not audit_event.event_type.startswith("connector.")
        or not audit_event.actor.strip()
    ):
        raise ConnectorRepositoryScopeError(
            "Connector audit event must match the connector workspace and identity"
        )


def _decode(row: tuple[object, ...] | None) -> AgentConnector | None:
    if row is None:
        return None
    connector_id, workspace_id, payload = row
    try:
        connector = AgentConnector.model_validate(payload)
    except Exception as error:
        raise ConnectorRepositoryError("Stored connector payload is invalid") from error
    if connector.id != connector_id or connector.workspace_id != workspace_id:
        raise ConnectorRepositoryScopeError("Stored connector identity or workspace is inconsistent")
    return connector


class PostgresConnectorRepository:
    """PostgreSQL connector storage with workspace checks and atomic audit writes."""

    def __init__(self, connect: Callable[[], Connection]) -> None:
        self._connect = connect

    @staticmethod
    def _validate_allowed_projects(
        conn: Connection, *, workspace_id: str, project_ids: list[str]
    ) -> None:
        if len(project_ids) != len(set(project_ids)):
            raise ConnectorRepositoryScopeError("Connector project scope contains duplicates")
        if not project_ids:
            return
        rows = conn.execute(
            "SELECT project_id FROM projects WHERE workspace_id=%s AND project_id=ANY(%s) "
            "FOR KEY SHARE",
            (workspace_id, project_ids),
        ).fetchall()
        if {row[0] for row in rows} != set(project_ids):
            raise ConnectorRepositoryScopeError(
                "Connector allowed projects must exist in its workspace"
            )

    @staticmethod
    def _insert_audit(conn: Connection, audit_event: AuditEvent) -> None:
        conn.execute(
            "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,"
            "event_type,actor,details,created_at,payload) "
            "VALUES(%s,%s,NULL,'connector',%s,%s,%s,%s,%s,%s)",
            (
                audit_event.id,
                audit_event.workspace_id,
                audit_event.entity_id,
                audit_event.event_type,
                audit_event.actor,
                Jsonb(audit_event.details),
                audit_event.created_at,
                Jsonb(audit_event.model_dump(mode="json")),
            ),
        )

    @staticmethod
    def _validate_actor(conn: Connection, connector: AgentConnector) -> None:
        actor = conn.execute(
            "SELECT 1 FROM users WHERE user_id=%s AND workspace_id=%s AND disabled=FALSE",
            (connector.created_by_user_id, connector.workspace_id),
        ).fetchone()
        if actor is None:
            raise ConnectorRepositoryScopeError(
                "Connector creator must be an active user in its workspace"
            )

    def get_for_workspace(
        self, connector_id: str, *, workspace_id: str
    ) -> AgentConnector | None:
        if not workspace_id:
            raise ConnectorRepositoryScopeError("Connector reads require a workspace")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT connector_id,workspace_id,payload FROM agent_connectors "
                "WHERE connector_id=%s AND workspace_id=%s",
                (connector_id, workspace_id),
            ).fetchone()
        return _decode(row)

    def get_for_authentication(self, connector_id: str) -> AgentConnector | None:
        """Resolve a connector ID before webhook authentication.

        Callers must authenticate the request before acting on or returning this
        record. Owner and management reads must use `get_for_workspace`.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT connector_id,workspace_id,payload FROM agent_connectors "
                "WHERE connector_id=%s",
                (connector_id,),
            ).fetchone()
        return _decode(row)

    def list_for_workspace(self, workspace_id: str) -> list[AgentConnector]:
        if not workspace_id:
            raise ConnectorRepositoryScopeError("PostgreSQL connector listing requires a workspace")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT connector_id,workspace_id,payload FROM agent_connectors "
                "WHERE workspace_id=%s ORDER BY created_at,connector_id",
                (workspace_id,),
            ).fetchall()
        connectors = [_decode(row) for row in rows]
        if any(connector is None for connector in connectors):
            raise ConnectorRepositoryError("Connector listing returned an empty row")
        return [connector for connector in connectors if connector is not None]

    def create(self, *, connector: AgentConnector, audit_event: AuditEvent) -> None:
        _validate_audit(connector, audit_event)
        if not connector.created_by_user_id:
            raise ConnectorRepositoryScopeError("PostgreSQL connectors require an owner")
        with self._connect() as conn:
            workspace = conn.execute(
                "SELECT 1 FROM workspaces WHERE workspace_id=%s FOR KEY SHARE",
                (connector.workspace_id,),
            ).fetchone()
            if workspace is None:
                raise ConnectorRepositoryScopeError("Connector workspace does not exist")
            self._validate_actor(conn, connector)
            self._validate_allowed_projects(
                conn,
                workspace_id=connector.workspace_id,
                project_ids=connector.allowed_project_ids,
            )
            conn.execute(
                "INSERT INTO agent_connectors(connector_id,workspace_id,created_by_user_id,"
                "provider_label,auth_type,status,created_at,updated_at,payload) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    connector.id,
                    connector.workspace_id,
                    connector.created_by_user_id,
                    connector.provider_label.value,
                    connector.auth_type.value,
                    connector.status.value,
                    connector.created_at,
                    connector.updated_at,
                    Jsonb(connector.model_dump(mode="json")),
                ),
            )
            self._insert_audit(conn, audit_event)

    def update(
        self,
        *,
        connector: AgentConnector,
        expected_updated_at: datetime,
        audit_event: AuditEvent,
    ) -> None:
        _validate_audit(connector, audit_event)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT connector_id,workspace_id,payload FROM agent_connectors "
                "WHERE connector_id=%s AND workspace_id=%s FOR UPDATE",
                (connector.id, connector.workspace_id),
            ).fetchone()
            stored = _decode(row)
            if stored is None:
                raise ConnectorRepositoryConflict("Connector changed or was removed")
            if stored.updated_at != expected_updated_at:
                raise ConnectorRepositoryConflict("Connector changed before update")
            if (
                connector.created_by_user_id != stored.created_by_user_id
                or connector.created_at != stored.created_at
                or connector.updated_at <= expected_updated_at
            ):
                raise ConnectorRepositoryScopeError("Connector update changed immutable identity")
            self._validate_allowed_projects(
                conn,
                workspace_id=connector.workspace_id,
                project_ids=connector.allowed_project_ids,
            )
            result = conn.execute(
                "UPDATE agent_connectors SET provider_label=%s,auth_type=%s,status=%s,"
                "updated_at=%s,payload=%s WHERE connector_id=%s AND workspace_id=%s "
                "AND updated_at=%s",
                (
                    connector.provider_label.value,
                    connector.auth_type.value,
                    connector.status.value,
                    connector.updated_at,
                    Jsonb(connector.model_dump(mode="json")),
                    connector.id,
                    connector.workspace_id,
                    expected_updated_at,
                ),
            )
            if result.rowcount != 1:
                raise ConnectorRepositoryConflict("Connector changed before update")
            self._insert_audit(conn, audit_event)

    def commit_webhook_action(
        self,
        *,
        connector: AgentConnector,
        request_nonce: str | None,
        expected_updated_at: datetime,
        audit_event: AuditEvent,
        write_action: Callable[[Connection], object],
    ) -> object:
        """Commit authenticated connector state and its accepted task action atomically."""
        _validate_audit(connector, audit_event)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT connector_id,workspace_id,payload FROM agent_connectors "
                "WHERE connector_id=%s AND workspace_id=%s FOR UPDATE",
                (connector.id, connector.workspace_id),
            ).fetchone()
            stored = _decode(row)
            if stored is None:
                raise ConnectorRepositoryConflict("Connector changed or was removed")
            if stored.updated_at != expected_updated_at:
                if _webhook_nonce_replayed(stored, request_nonce):
                    raise ConnectorWebhookReplay("Connector HMAC nonce has already been used")
                raise ConnectorRepositoryConflict("Connector changed before webhook action committed")
            if (
                connector.created_by_user_id != stored.created_by_user_id
                or connector.created_at != stored.created_at
                or connector.updated_at <= expected_updated_at
            ):
                raise ConnectorRepositoryScopeError("Connector update changed immutable identity")
            self._validate_allowed_projects(
                conn,
                workspace_id=connector.workspace_id,
                project_ids=connector.allowed_project_ids,
            )
            result = write_action(conn)
            changed = conn.execute(
                "UPDATE agent_connectors SET provider_label=%s,auth_type=%s,status=%s,"
                "updated_at=%s,payload=%s WHERE connector_id=%s AND workspace_id=%s "
                "AND updated_at=%s",
                (
                    connector.provider_label.value,
                    connector.auth_type.value,
                    connector.status.value,
                    connector.updated_at,
                    Jsonb(connector.model_dump(mode="json")),
                    connector.id,
                    connector.workspace_id,
                    expected_updated_at,
                ),
            )
            if changed.rowcount != 1:
                raise ConnectorRepositoryConflict("Connector changed before webhook action committed")
            self._insert_audit(conn, audit_event)
            return result


class ConnectorWebhookReplay(ConnectorRepositoryError):
    """A concurrently committed webhook already consumed the supplied nonce."""


def _webhook_nonce_replayed(stored: AgentConnector, request_nonce: str | None) -> bool:
    history = stored.config.get("hmac_nonce_history")
    return bool(request_nonce and isinstance(history, dict) and request_nonce in history)
