from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from psycopg import Connection

from .models import AuditEvent


class PostgresAuditRepositoryError(RuntimeError):
    """A PostgreSQL audit query returned an invalid or inconsistent record."""


class PostgresAuditEventRepository:
    """Workspace-scoped reads from the immutable PostgreSQL audit ledger."""

    def __init__(self, connect: Callable[[], Connection]) -> None:
        self._connect = connect

    @staticmethod
    def _decode(row: tuple[Any, ...], workspace_id: str) -> AuditEvent:
        event_id, row_workspace_id, event_type, entity_id, actor, created_at, details, payload = row
        try:
            event = AuditEvent.model_validate(payload)
        except Exception as error:
            raise PostgresAuditRepositoryError("Stored audit payload is invalid") from error
        if (
            event.id != event_id
            or event.workspace_id != row_workspace_id
            or event.workspace_id != workspace_id
            or event.event_type != event_type
            or event.entity_id != entity_id
            or event.actor != actor
            or event.created_at != created_at
            or event.details != details
        ):
            raise PostgresAuditRepositoryError(
                "Stored audit payload does not match its indexed columns or workspace"
            )
        return event

    @staticmethod
    def _validate_query(
        *,
        workspace_id: str,
        limit: int,
        detail_key: str | None,
        detail_value: str | None,
        before: tuple[datetime, str] | None,
    ) -> None:
        if not workspace_id:
            raise ValueError("PostgreSQL audit reads require a workspace")
        if not 1 <= limit <= 1_001:
            raise ValueError("Audit query limit must be between 1 and 1001")
        if detail_value is not None and detail_key is None:
            raise ValueError("Audit detail-value filtering requires a detail key")
        if before is not None:
            if (
                not isinstance(before, tuple)
                or len(before) != 2
                or not isinstance(before[0], datetime)
                or before[0].utcoffset() is None
                or not isinstance(before[1], str)
                or not before[1]
            ):
                raise ValueError("Audit cursor boundary is invalid")

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
        before: tuple[datetime, str] | None = None,
        limit: int = 1_000,
    ) -> list[AuditEvent]:
        self._validate_query(
            workspace_id=workspace_id,
            limit=limit,
            detail_key=detail_key,
            detail_value=detail_value,
            before=before,
        )
        if created_after is not None and created_before is not None and created_after > created_before:
            raise ValueError("Audit created_after must not be later than created_before")

        clauses = ["workspace_id = %s"]
        params: list[Any] = [workspace_id]
        for column, value in (
            ("event_type", event_type),
            ("entity_id", entity_id),
            ("actor", actor),
        ):
            if value is not None:
                clauses.append(f"{column} = %s")
                params.append(value)
        if detail_key is not None:
            clauses.append("details ? %s")
            params.append(detail_key)
        if detail_key is not None and detail_value is not None:
            clauses.append("details ->> %s = %s")
            params.extend((detail_key, detail_value))
        if created_after is not None:
            clauses.append("created_at >= %s")
            params.append(created_after)
        if created_before is not None:
            clauses.append("created_at <= %s")
            params.append(created_before)
        if before is not None:
            clauses.append("(created_at,event_id) < (%s,%s)")
            params.extend(before)
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT event_id,workspace_id,event_type,entity_id,actor,created_at,details,payload "
                "FROM audit_events WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at DESC,event_id DESC LIMIT %s",
                params,
            ).fetchall()
        return [self._decode(row, workspace_id) for row in rows]

    def get_for_workspace(
        self, audit_event_id: str, workspace_id: str
    ) -> AuditEvent | None:
        if not workspace_id:
            raise ValueError("PostgreSQL audit reads require a workspace")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT event_id,workspace_id,event_type,entity_id,actor,created_at,details,payload "
                "FROM audit_events WHERE event_id=%s AND workspace_id=%s",
                (audit_event_id, workspace_id),
            ).fetchone()
        return self._decode(row, workspace_id) if row is not None else None
