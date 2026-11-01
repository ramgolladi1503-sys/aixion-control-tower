from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from psycopg import Connection
from psycopg.types.json import Jsonb

from .models import AuditEvent, MCPChildServer, Project


class MCPChildServerRepositoryError(RuntimeError):
    """An MCP child-server read or write could not be completed safely."""


class MCPChildServerScopeError(MCPChildServerRepositoryError):
    """The MCP server, project, workspace, or actor scope is inconsistent."""


class MCPChildServerConflict(MCPChildServerRepositoryError):
    """The MCP server changed or collided with another registry operation."""


class MCPChildServerRepository(Protocol):
    def get(self, server_id: str) -> MCPChildServer | None: ...

    def list_for_workspace(
        self,
        workspace_id: str | None,
        *,
        project_id: str | None = None,
        enabled: bool | None = None,
    ) -> list[MCPChildServer]: ...

    def create(self, *, server: MCPChildServer, audit_event: AuditEvent) -> None: ...

    def update(
        self,
        *,
        server: MCPChildServer,
        audit_event: AuditEvent,
        expected_updated_at: str,
    ) -> None: ...


def _validate_audit(server: MCPChildServer, event: AuditEvent) -> None:
    if (
        not event.event_type.startswith("mcp.child_server.")
        or event.entity_id != server.id
        or event.workspace_id != server.workspace_id
        or event.details.get("project_id") != server.project_id
        or event.details.get("name") != server.name
    ):
        raise ValueError("MCP child-server audit event must match its identity and scope")


def _decode(
    row: tuple[object, ...] | None,
    server_id: str | None = None,
) -> MCPChildServer | None:
    if row is None:
        return None
    try:
        server = (
            MCPChildServer.model_validate_json(row[1])
            if isinstance(row[1], str)
            else MCPChildServer.model_validate(row[1])
        )
    except Exception as error:
        raise MCPChildServerRepositoryError("Stored MCP child-server payload is invalid") from error
    if server.id != row[0] or (server_id is not None and server.id != server_id):
        raise MCPChildServerRepositoryError("Stored MCP child-server identity is inconsistent")
    if len(row) > 3 and (server.project_id != row[2] or server.workspace_id != row[3]):
        raise MCPChildServerScopeError("Stored MCP child-server scope does not match its row")
    return server


class SQLiteMCPChildServerRepository:
    """Transactional MCP registry adapter for the local SQLite store."""

    def __init__(self, connect: Callable[[], sqlite3.Connection]) -> None:
        self._connect = connect

    def get(self, server_id: str) -> MCPChildServer | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT entity_id,payload FROM kv_store "
                "WHERE entity_type=? AND entity_id=?",
                ("mcp_child_server", server_id),
            ).fetchone()
        return _decode(row, server_id)

    def list_for_workspace(
        self,
        workspace_id: str | None,
        *,
        project_id: str | None = None,
        enabled: bool | None = None,
    ) -> list[MCPChildServer]:
        query = "SELECT entity_id,payload FROM kv_store WHERE entity_type=?"
        params: list[object] = ["mcp_child_server"]
        if workspace_id is not None:
            query += " AND json_extract(payload,'$.workspace_id')=?"
            params.append(workspace_id)
        if project_id is not None:
            query += " AND json_extract(payload,'$.project_id')=?"
            params.append(project_id)
        if enabled is not None:
            query += " AND json_extract(payload,'$.enabled')=?"
            params.append(1 if enabled else 0)
        query += " ORDER BY entity_id"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        servers = [_decode(row) for row in rows]
        result = [server for server in servers if server is not None]
        if any(
            server.workspace_id != workspace_id
            or (project_id is not None and server.project_id != project_id)
            or (enabled is not None and server.enabled != enabled)
            for server in result
        ):
            raise MCPChildServerScopeError("Stored MCP child-server query scope is inconsistent")
        return result

    def create(self, *, server: MCPChildServer, audit_event: AuditEvent) -> None:
        _validate_audit(server, audit_event)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT entity_id,payload FROM kv_store WHERE entity_type=? AND entity_id=?",
                ("project", server.project_id),
            ).fetchone()
            if row is None:
                raise MCPChildServerScopeError("MCP child-server project does not exist")
            project = Project.model_validate_json(row[1])
            if project.id != row[0] or project.workspace_id != server.workspace_id:
                raise MCPChildServerScopeError("MCP child-server project workspace does not match")
            self._assert_unique_name(conn, server)
            conn.execute(
                "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
                ("mcp_child_server", server.id, server.model_dump_json()),
            )
            conn.execute(
                "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
                ("audit_event", audit_event.id, audit_event.model_dump_json()),
            )
            conn.commit()

    def update(
        self,
        *,
        server: MCPChildServer,
        audit_event: AuditEvent,
        expected_updated_at: str,
    ) -> None:
        _validate_audit(server, audit_event)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload FROM kv_store WHERE entity_type=? AND entity_id=?",
                ("mcp_child_server", server.id),
            ).fetchone()
            current = _decode((server.id, row[0])) if row else None
            if current is None:
                raise MCPChildServerConflict("MCP child server no longer exists")
            if (
                current.project_id != server.project_id
                or current.workspace_id != server.workspace_id
                or current.updated_at.isoformat() != expected_updated_at
            ):
                raise MCPChildServerConflict("MCP child server changed before update")
            self._assert_unique_name(conn, server)
            conn.execute(
                "UPDATE kv_store SET payload=? WHERE entity_type=? AND entity_id=?",
                (server.model_dump_json(), "mcp_child_server", server.id),
            )
            conn.execute(
                "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
                ("audit_event", audit_event.id, audit_event.model_dump_json()),
            )
            conn.commit()

    @staticmethod
    def _assert_unique_name(conn: sqlite3.Connection, server: MCPChildServer) -> None:
        row = conn.execute(
            "SELECT entity_id FROM kv_store WHERE entity_type=? "
            "AND json_extract(payload,'$.project_id')=? "
            "AND json_extract(payload,'$.name')=? AND entity_id<>? LIMIT 1",
            ("mcp_child_server", server.project_id, server.name, server.id),
        ).fetchone()
        if row is not None:
            raise MCPChildServerConflict("MCP child-server name already exists for project")


class PostgresMCPChildServerRepository:
    """Workspace-checked MCP registry persistence with transactional audit."""

    def __init__(self, connect: Callable[[], Connection]) -> None:
        self._connect = connect

    def get(self, server_id: str) -> MCPChildServer | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT server_id,project_id,workspace_id,payload "
                "FROM mcp_child_servers WHERE server_id=%s",
                (server_id,),
            ).fetchone()
        return _decode((row[0], row[3], row[1], row[2]), server_id) if row else None

    def list_for_workspace(
        self,
        workspace_id: str | None,
        *,
        project_id: str | None = None,
        enabled: bool | None = None,
    ) -> list[MCPChildServer]:
        if not workspace_id:
            raise MCPChildServerScopeError("PostgreSQL MCP registry listing requires a workspace")
        query = (
            "SELECT server_id,project_id,workspace_id,payload FROM mcp_child_servers "
            "WHERE workspace_id=%s"
        )
        params: list[object] = [workspace_id]
        if project_id is not None:
            query += " AND project_id=%s"
            params.append(project_id)
        if enabled is not None:
            query += " AND enabled=%s"
            params.append(enabled)
        query += " ORDER BY created_at DESC,server_id"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        result: list[MCPChildServer] = []
        for row in rows:
            server = _decode((row[0], row[3], row[1], row[2]))
            if server is None or server.workspace_id != workspace_id:
                raise MCPChildServerScopeError("Stored MCP child-server workspace is inconsistent")
            result.append(server)
        return result

    def create(self, *, server: MCPChildServer, audit_event: AuditEvent) -> None:
        _validate_audit(server, audit_event)
        if not server.workspace_id:
            raise MCPChildServerScopeError("PostgreSQL MCP child servers require a workspace")
        with self._connect() as conn:
            project = conn.execute(
                "SELECT workspace_id FROM projects WHERE project_id=%s FOR KEY SHARE",
                (server.project_id,),
            ).fetchone()
            if project is None or project[0] != server.workspace_id:
                raise MCPChildServerScopeError("MCP child-server project workspace does not match")
            conn.execute(
                "INSERT INTO mcp_child_servers"
                "(server_id,project_id,workspace_id,name,enabled,created_at,updated_at,payload) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (server.id, server.project_id, server.workspace_id, server.name,
                 server.enabled, server.created_at, server.updated_at,
                 Jsonb(server.model_dump(mode="json"))),
            )
            self._insert_audit(conn, audit_event)

    def update(
        self,
        *,
        server: MCPChildServer,
        audit_event: AuditEvent,
        expected_updated_at: str,
    ) -> None:
        _validate_audit(server, audit_event)
        if not server.workspace_id:
            raise MCPChildServerScopeError("PostgreSQL MCP child servers require a workspace")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT updated_at FROM mcp_child_servers WHERE server_id=%s "
                "AND project_id=%s AND workspace_id=%s FOR UPDATE",
                (server.id, server.project_id, server.workspace_id),
            ).fetchone()
            try:
                expected_timestamp = datetime.fromisoformat(expected_updated_at)
            except ValueError as error:
                raise MCPChildServerConflict("Expected MCP child-server timestamp is invalid") from error
            if (
                expected_timestamp.tzinfo is None
                or row is None
                or row[0] != expected_timestamp
            ):
                raise MCPChildServerConflict("MCP child server changed before update")
            conn.execute(
                "UPDATE mcp_child_servers SET name=%s,enabled=%s,updated_at=%s,payload=%s "
                "WHERE server_id=%s AND project_id=%s AND workspace_id=%s",
                (server.name, server.enabled, server.updated_at,
                 Jsonb(server.model_dump(mode="json")), server.id,
                 server.project_id, server.workspace_id),
            )
            self._insert_audit(conn, audit_event)

    @staticmethod
    def _insert_audit(conn: Connection, event: AuditEvent) -> None:
        conn.execute(
            "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,"
            "event_type,actor,details,created_at,payload) "
            "VALUES(%s,%s,NULL,'mcp_child_server',%s,%s,%s,%s,%s,%s)",
            (event.id, event.workspace_id, event.entity_id, event.event_type,
             event.actor, Jsonb(event.details), event.created_at,
             Jsonb(event.model_dump(mode="json"))),
        )
