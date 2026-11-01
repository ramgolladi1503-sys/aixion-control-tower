from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Protocol

from psycopg import Connection
from psycopg.types.json import Jsonb

from .models import AuditEvent, Project, Workspace


class ProjectRepositoryError(RuntimeError):
    """A project write could not be committed safely."""


class ProjectRepositoryScopeError(ProjectRepositoryError):
    """Project, workspace, and actor ownership do not match."""


class ProjectRepository(Protocol):
    def get(self, project_id: str) -> Project | None: ...

    def list_for_workspace(self, workspace_id: str | None = None) -> list[Project]: ...

    def create(self, *, project: Project, audit_event: AuditEvent) -> None: ...


def _validate_project_creation(project: Project, audit_event: AuditEvent) -> None:
    if (
        audit_event.event_type != "project.created"
        or audit_event.entity_id != project.id
        or audit_event.workspace_id != project.workspace_id
        or audit_event.details.get("name") != project.name
        or audit_event.details.get("mode") != project.mode
    ):
        raise ValueError("Project creation audit event must match the project identity and scope")


class SQLiteProjectRepository:
    """Transactional Project writes for the existing single-process SQLite adapter."""

    def __init__(self, connect: Callable[[], sqlite3.Connection]) -> None:
        self._connect = connect

    def get(self, project_id: str) -> Project | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT entity_id, payload FROM kv_store "
                "WHERE entity_type = ? AND entity_id = ?",
                ("project", project_id),
            ).fetchone()
        if row is None:
            return None
        try:
            project = Project.model_validate_json(row[1])
        except Exception as error:
            raise ProjectRepositoryError("Stored project payload is invalid") from error
        if row[0] != project.id or project.id != project_id:
            raise ProjectRepositoryError("Stored project identity is inconsistent")
        return project

    def list_for_workspace(self, workspace_id: str | None = None) -> list[Project]:
        with self._connect() as conn:
            if workspace_id is None:
                rows = conn.execute(
                    "SELECT entity_id, payload FROM kv_store "
                    "WHERE entity_type = ? ORDER BY entity_id",
                    ("project",),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT entity_id, payload FROM kv_store "
                    "WHERE entity_type = ? AND json_extract(payload, '$.workspace_id') = ? "
                    "ORDER BY entity_id",
                    ("project", workspace_id),
                ).fetchall()
        projects: list[Project] = []
        for entity_id, payload in rows:
            try:
                project = Project.model_validate_json(payload)
            except Exception as error:
                raise ProjectRepositoryError("Stored project payload is invalid") from error
            if project.id != entity_id:
                raise ProjectRepositoryError("Stored project identity is inconsistent")
            if workspace_id is not None and project.workspace_id != workspace_id:
                raise ProjectRepositoryError("Stored project workspace is inconsistent")
            projects.append(project)
        return projects

    def create(self, *, project: Project, audit_event: AuditEvent) -> None:
        _validate_project_creation(project, audit_event)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if project.workspace_id is not None:
                workspace_row = conn.execute(
                    "SELECT entity_id, payload FROM kv_store "
                    "WHERE entity_type = ? AND entity_id = ?",
                    ("workspace", project.workspace_id),
                ).fetchone()
                if workspace_row is None:
                    raise ProjectRepositoryScopeError("Project workspace does not exist")
                workspace = Workspace.model_validate_json(workspace_row[1])
                if workspace.id != project.workspace_id or workspace_row[0] != workspace.id:
                    raise ProjectRepositoryScopeError("Project workspace identity is inconsistent")

            conn.execute(
                "INSERT INTO kv_store(entity_type, entity_id, payload) VALUES (?, ?, ?)",
                ("project", project.id, project.model_dump_json()),
            )
            conn.execute(
                "INSERT INTO kv_store(entity_type, entity_id, payload) VALUES (?, ?, ?)",
                ("audit_event", audit_event.id, audit_event.model_dump_json()),
            )
            conn.commit()


class PostgresProjectRepository:
    """Workspace-checked project and audit creation in one PostgreSQL transaction."""

    def __init__(self, connect: Callable[[], Connection]) -> None:
        self._connect = connect

    def get(self, project_id: str) -> Project | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT project_id, workspace_id, payload FROM projects "
                "WHERE project_id = %s",
                (project_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            project = Project.model_validate(row[2])
        except Exception as error:
            raise ProjectRepositoryError("Stored project payload is invalid") from error
        if (
            row[0] != project.id
            or project.id != project_id
            or row[1] != project.workspace_id
        ):
            raise ProjectRepositoryError("Stored project identity is inconsistent")
        return project

    def list_for_workspace(self, workspace_id: str | None = None) -> list[Project]:
        if not workspace_id:
            raise ProjectRepositoryScopeError("PostgreSQL project listing requires a workspace")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT project_id, workspace_id, payload FROM projects "
                "WHERE workspace_id = %s ORDER BY project_id",
                (workspace_id,),
            ).fetchall()
        projects: list[Project] = []
        for row in rows:
            try:
                project = Project.model_validate(row[2])
            except Exception as error:
                raise ProjectRepositoryError("Stored project payload is invalid") from error
            if project.id != row[0] or project.workspace_id != row[1]:
                raise ProjectRepositoryError("Stored project identity or scope is inconsistent")
            if project.workspace_id != workspace_id:
                raise ProjectRepositoryScopeError("Stored project workspace is inconsistent")
            projects.append(project)
        return projects

    def create(self, *, project: Project, audit_event: AuditEvent) -> None:
        _validate_project_creation(project, audit_event)
        if project.workspace_id is None:
            raise ProjectRepositoryScopeError("PostgreSQL projects require a workspace")

        with self._connect() as conn:
            workspace_row = conn.execute(
                "SELECT workspace_id FROM workspaces WHERE workspace_id = %s FOR KEY SHARE",
                (project.workspace_id,),
            ).fetchone()
            if workspace_row is None:
                raise ProjectRepositoryScopeError("Project workspace does not exist")

            if project.created_by_user_id is not None:
                actor_row = conn.execute(
                    "SELECT workspace_id FROM users WHERE user_id = %s FOR KEY SHARE",
                    (project.created_by_user_id,),
                ).fetchone()
                if actor_row is None or actor_row[0] != project.workspace_id:
                    raise ProjectRepositoryScopeError(
                        "Project creator does not belong to its workspace"
                    )

            conn.execute(
                "INSERT INTO projects(project_id, workspace_id, payload) VALUES (%s, %s, %s)",
                (
                    project.id,
                    project.workspace_id,
                    Jsonb(project.model_dump(mode="json")),
                ),
            )
            conn.execute(
                "INSERT INTO audit_events(event_id, workspace_id, approval_id, entity_type, "
                "entity_id, event_type, actor, details, created_at, payload) "
                "VALUES (%s, %s, NULL, 'project', %s, %s, %s, %s, %s, %s)",
                (
                    audit_event.id,
                    project.workspace_id,
                    project.id,
                    audit_event.event_type,
                    audit_event.actor,
                    Jsonb(audit_event.details),
                    audit_event.created_at,
                    Jsonb(audit_event.model_dump(mode="json")),
                ),
            )
