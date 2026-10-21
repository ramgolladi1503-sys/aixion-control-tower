from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from pydantic import ValidationError

from .models import AuditEvent


class AuditEventRepositoryIntegrityError(RuntimeError):
    """A durable audit row is malformed or disagrees with its indexed identity."""


AUDIT_REPOSITORY_MAX_LIMIT = 1_001


class AuditEventRepository(Protocol):
    def list_for_workspace(
        self,
        workspace_id: str,
        *,
        event_type: str | None = None,
        entity_id: str | None = None,
        actor: str | None = None,
        detail_key: str | None = None,
        detail_value: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        limit: int = 1_000,
    ) -> list[AuditEvent]: ...

    def get_for_workspace(self, audit_event_id: str, workspace_id: str) -> AuditEvent | None: ...


class SQLiteAuditEventRepository:
    """Read immutable audit rows from SQLite without relying on process caches."""

    def __init__(
        self,
        connect: Callable[[], sqlite3.Connection],
        *,
        resolve_workspace: Callable[[AuditEvent], str | None] | None = None,
    ) -> None:
        self._connect = connect
        self._resolve_workspace = resolve_workspace or self._event_workspace

    @staticmethod
    def _event_workspace(event: AuditEvent) -> str | None:
        if event.workspace_id is not None:
            return event.workspace_id
        # Resolve legacy scope only through durable repositories. Unknown
        # unscoped events stay invisible to workspace-scoped reads.
        from .approval_decisions import (
            agent_run_repository,
            agent_task_repository,
            approval_decision_repository,
            project_repository,
        )

        details = event.details
        project_ids = [details.get("project_id"), event.entity_id]
        approval_ids = [details.get("approval_request_id"), event.entity_id]
        task_ids = [
            details.get("agent_task_id"),
            details.get("task_id"),
            event.entity_id,
        ]
        for project_id in project_ids:
            if isinstance(project_id, str):
                project = project_repository.get(project_id)
                if project is not None:
                    return project.workspace_id
        for approval_id in approval_ids:
            if isinstance(approval_id, str):
                approval = approval_decision_repository.get(approval_id)
                if approval is not None:
                    project = project_repository.get(approval.project_id)
                    if project is None:
                        return None
                    if approval.workspace_id not in (None, project.workspace_id):
                        return None
                    return approval.workspace_id or project.workspace_id
        for task_id in task_ids:
            if isinstance(task_id, str):
                task = agent_task_repository.get(task_id)
                if task is not None:
                    project = project_repository.get(task.project_id or "")
                    if project is None or task.workspace_id not in (None, project.workspace_id):
                        return None
                    return task.workspace_id or project.workspace_id
                run = agent_run_repository.get(task_id)
                if run is not None:
                    task = agent_task_repository.get(run.task_id)
                    project = project_repository.get(run.project_id or "")
                    if (
                        task is None
                        or project is None
                        or task.project_id != project.id
                        or run.workspace_id not in (None, project.workspace_id)
                    ):
                        return None
                    return run.workspace_id or task.workspace_id or project.workspace_id
        return None

    @staticmethod
    def _decode(row: tuple[str, str]) -> AuditEvent:
        row_id, payload = row
        try:
            event = AuditEvent.model_validate_json(payload)
        except (TypeError, ValueError, ValidationError) as error:
            raise AuditEventRepositoryIntegrityError("Stored audit payload is invalid") from error
        if event.id != row_id:
            raise AuditEventRepositoryIntegrityError(
                "Stored audit payload does not match its indexed identity"
            )
        return event

    @staticmethod
    def _validate_query(
        *,
        workspace_id: str,
        limit: int,
        detail_key: str | None,
        detail_value: str | None,
    ) -> None:
        if not 1 <= limit <= AUDIT_REPOSITORY_MAX_LIMIT:
            raise ValueError(
                f"Audit query limit must be between 1 and {AUDIT_REPOSITORY_MAX_LIMIT}"
            )
        if detail_value is not None and detail_key is None:
            raise ValueError("Audit detail-value filtering requires a detail key")

    def list_for_workspace(
        self,
        workspace_id: str,
        *,
        event_type: str | None = None,
        entity_id: str | None = None,
        actor: str | None = None,
        detail_key: str | None = None,
        detail_value: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        limit: int = 1_000,
    ) -> list[AuditEvent]:
        self._validate_query(
            workspace_id=workspace_id,
            limit=limit,
            detail_key=detail_key,
            detail_value=detail_value,
        )
        if created_after is not None and created_before is not None and created_after > created_before:
            raise ValueError("Audit created_after must not be later than created_before")

        clauses = ["entity_type='audit_event'"]
        params: list[object] = []
        if workspace_id:
            clauses.append(
                "(json_extract(payload,'$.workspace_id')=? "
                "OR json_extract(payload,'$.workspace_id') IS NULL)"
            )
            params.append(workspace_id)
        for field, value in (
            ("event_type", event_type),
            ("entity_id", entity_id),
            ("actor", actor),
        ):
            if value is not None:
                clauses.append(f"json_extract(payload,'$.{field}')=?")
                params.append(value)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT entity_id,payload FROM kv_store WHERE "
                + " AND ".join(clauses)
                + " ORDER BY entity_id",
                params,
            ).fetchall()

        events: list[AuditEvent] = []
        for row in rows:
            event = self._decode((row[0], row[1]))
            if workspace_id and self._resolve_workspace(event) != workspace_id:
                continue
            if detail_key is not None and detail_key not in event.details:
                continue
            if detail_key is not None and detail_value is not None:
                if str(event.details.get(detail_key)) != detail_value:
                    continue
            if created_after is not None and event.created_at < created_after:
                continue
            if created_before is not None and event.created_at > created_before:
                continue
            events.append(event)
        events.sort(key=lambda event: (event.created_at, event.id), reverse=True)
        return events[:limit]

    def get_for_workspace(self, audit_event_id: str, workspace_id: str) -> AuditEvent | None:
        if not audit_event_id:
            raise ValueError("Audit event identity is required")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT entity_id,payload FROM kv_store "
                "WHERE entity_type='audit_event' AND entity_id=?",
                (audit_event_id,),
            ).fetchone()
        if row is None:
            return None
        event = self._decode((row[0], row[1]))
        if workspace_id and self._resolve_workspace(event) != workspace_id:
            return None
        return event
