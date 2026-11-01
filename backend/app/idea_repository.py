from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Protocol

from psycopg import Connection
from psycopg.types.json import Jsonb

from .models import AuditEvent, Idea, Project, Workspace


class IdeaRepositoryError(RuntimeError):
    """An idea read or write could not be completed safely."""


class IdeaRepositoryScopeError(IdeaRepositoryError):
    """The idea, project, workspace, or actor scope is inconsistent."""


class IdeaRepository(Protocol):
    def get(self, idea_id: str) -> Idea | None: ...

    def list_for_workspace(self, workspace_id: str | None = None) -> list[Idea]: ...

    def create(self, *, idea: Idea, audit_event: AuditEvent) -> None: ...


def _validate_creation(idea: Idea, audit_event: AuditEvent) -> None:
    expected_details = {"title": idea.title, "project_id": idea.project_id}
    if (
        audit_event.event_type != "idea.created"
        or audit_event.entity_id != idea.id
        or audit_event.workspace_id != idea.workspace_id
        or any(audit_event.details.get(key) != value for key, value in expected_details.items())
    ):
        raise ValueError("Idea audit event must match the idea identity, workspace, and project")


def _decode(row: tuple[object, ...] | None, idea_id: str | None = None) -> Idea | None:
    if row is None:
        return None
    try:
        idea = Idea.model_validate_json(row[1]) if isinstance(row[1], str) else Idea.model_validate(row[1])
    except Exception as error:
        raise IdeaRepositoryError("Stored idea payload is invalid") from error
    if idea.id != row[0] or (idea_id is not None and idea.id != idea_id):
        raise IdeaRepositoryError("Stored idea identity is inconsistent")
    if len(row) > 2 and (idea.project_id != row[2] or idea.workspace_id != row[3]):
        raise IdeaRepositoryScopeError("Stored idea scope does not match its row")
    return idea


class SQLiteIdeaRepository:
    """Transactional idea writes for the local SQLite application store."""

    def __init__(self, connect: Callable[[], sqlite3.Connection]) -> None:
        self._connect = connect

    def get(self, idea_id: str) -> Idea | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT entity_id,payload FROM kv_store WHERE entity_type=? AND entity_id=?",
                ("idea", idea_id),
            ).fetchone()
        return _decode(row, idea_id)

    def list_for_workspace(self, workspace_id: str | None = None) -> list[Idea]:
        with self._connect() as conn:
            if workspace_id is None:
                rows = conn.execute(
                    "SELECT entity_id,payload FROM kv_store WHERE entity_type=? ORDER BY entity_id",
                    ("idea",),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT entity_id,payload FROM kv_store WHERE entity_type=? "
                    "AND json_extract(payload,'$.workspace_id')=? ORDER BY entity_id",
                    ("idea", workspace_id),
                ).fetchall()
        ideas = [_decode(row) for row in rows]
        result = [idea for idea in ideas if idea is not None]
        if workspace_id is not None and any(idea.workspace_id != workspace_id for idea in result):
            raise IdeaRepositoryScopeError("Stored idea workspace does not match its row")
        return result

    def create(self, *, idea: Idea, audit_event: AuditEvent) -> None:
        _validate_creation(idea, audit_event)
        if not idea.workspace_id:
            raise IdeaRepositoryScopeError("Ideas require a workspace")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            workspace_row = conn.execute(
                "SELECT entity_id,payload FROM kv_store WHERE entity_type=? AND entity_id=?",
                ("workspace", idea.workspace_id),
            ).fetchone()
            if workspace_row is None:
                raise IdeaRepositoryScopeError("Idea workspace does not exist")
            workspace = Workspace.model_validate_json(workspace_row[1])
            if workspace.id != workspace_row[0] or workspace.id != idea.workspace_id:
                raise IdeaRepositoryScopeError("Idea workspace identity is inconsistent")
            if idea.project_id is not None:
                project_row = conn.execute(
                    "SELECT entity_id,payload FROM kv_store WHERE entity_type=? AND entity_id=?",
                    ("project", idea.project_id),
                ).fetchone()
                if project_row is None:
                    raise IdeaRepositoryScopeError("Idea project does not exist")
                project = Project.model_validate_json(project_row[1])
                if project.id != project_row[0] or project.workspace_id != idea.workspace_id:
                    raise IdeaRepositoryScopeError("Idea project workspace does not match")
            conn.execute(
                "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
                ("idea", idea.id, idea.model_dump_json()),
            )
            conn.execute(
                "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
                ("audit_event", audit_event.id, audit_event.model_dump_json()),
            )
            conn.commit()


class PostgresIdeaRepository:
    """Workspace-checked idea and audit writes in one PostgreSQL transaction."""

    def __init__(self, connect: Callable[[], Connection]) -> None:
        self._connect = connect

    def get(self, idea_id: str) -> Idea | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT idea_id,workspace_id,project_id,payload FROM ideas WHERE idea_id=%s",
                (idea_id,),
            ).fetchone()
        return _decode((row[0], row[3], row[2], row[1]), idea_id) if row else None

    def list_for_workspace(self, workspace_id: str | None = None) -> list[Idea]:
        if not workspace_id:
            raise IdeaRepositoryScopeError("PostgreSQL idea listing requires a workspace")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT idea_id,workspace_id,project_id,payload FROM ideas "
                "WHERE workspace_id=%s ORDER BY created_at,idea_id",
                (workspace_id,),
            ).fetchall()
        result: list[Idea] = []
        for row in rows:
            idea = _decode((row[0], row[3], row[2], row[1]))
            if idea is None or idea.workspace_id != workspace_id:
                raise IdeaRepositoryScopeError("Stored idea workspace does not match its row")
            result.append(idea)
        return result

    def create(self, *, idea: Idea, audit_event: AuditEvent) -> None:
        _validate_creation(idea, audit_event)
        if not idea.workspace_id:
            raise IdeaRepositoryScopeError("PostgreSQL ideas require a workspace")
        with self._connect() as conn:
            workspace = conn.execute(
                "SELECT workspace_id FROM workspaces WHERE workspace_id=%s FOR KEY SHARE",
                (idea.workspace_id,),
            ).fetchone()
            if workspace is None:
                raise IdeaRepositoryScopeError("Idea workspace does not exist")
            if idea.project_id is not None:
                project = conn.execute(
                    "SELECT workspace_id FROM projects WHERE project_id=%s FOR KEY SHARE",
                    (idea.project_id,),
                ).fetchone()
                if project is None or project[0] != idea.workspace_id:
                    raise IdeaRepositoryScopeError("Idea project workspace does not match")
            conn.execute(
                "INSERT INTO ideas(idea_id,project_id,workspace_id,created_at,payload) "
                "VALUES(%s,%s,%s,%s,%s)",
                (idea.id, idea.project_id, idea.workspace_id, idea.created_at,
                 Jsonb(idea.model_dump(mode="json"))),
            )
            conn.execute(
                "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,"
                "event_type,actor,details,created_at,payload) "
                "VALUES(%s,%s,NULL,'idea',%s,%s,%s,%s,%s,%s)",
                (audit_event.id, idea.workspace_id, idea.id, audit_event.event_type,
                 audit_event.actor, Jsonb(audit_event.details), audit_event.created_at,
                 Jsonb(audit_event.model_dump(mode="json"))),
            )
