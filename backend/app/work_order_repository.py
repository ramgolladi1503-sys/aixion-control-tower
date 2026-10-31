from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Protocol

from psycopg import Connection
from psycopg.types.json import Jsonb

from .models import (
    AuditEvent,
    ExternalAgent,
    Idea,
    Project,
    WorkOrder,
    WorkOrderSourceType,
    Workspace,
)


class WorkOrderRepositoryError(RuntimeError):
    """A work-order read or write could not be completed safely."""


class WorkOrderRepositoryScopeError(WorkOrderRepositoryError):
    """The work order, project, workspace, source, or actor scope is inconsistent."""


class WorkOrderRepository(Protocol):
    def get(self, work_order_id: str) -> WorkOrder | None: ...

    def list_for_workspace(self, workspace_id: str | None = None) -> list[WorkOrder]: ...

    def create(self, *, work_order: WorkOrder, audit_event: AuditEvent) -> None: ...


def _validate_creation(work_order: WorkOrder, audit_event: AuditEvent) -> None:
    expected_details = {
        "project_id": work_order.project_id,
        "risk_level": work_order.risk_level,
        "source_type": work_order.source_type,
        "source_provider": work_order.source_provider,
        "source_agent_id": work_order.source_agent_id,
        "source_agent_name": work_order.source_agent_name,
        "created_by_user_id": work_order.created_by_user_id,
        "verified_source": work_order.verified_source,
    }
    expected_event_type = (
        "work_order.created_by_agent"
        if work_order.source_type == WorkOrderSourceType.AGENT_TASK
        else "work_order.created"
    )
    expected_actor = (
        f"agent:{work_order.source_agent_id}"
        if work_order.source_type == WorkOrderSourceType.AGENT_TASK
        else None
    )
    if expected_event_type == "work_order.created_by_agent":
        expected_details.update(
            {
                "source_task_id": work_order.source_task_id,
                "source_session_id": work_order.source_session_id,
            }
        )
    if (
        audit_event.event_type != expected_event_type
        or audit_event.entity_id != work_order.id
        or audit_event.workspace_id != work_order.workspace_id
        or (expected_actor is not None and audit_event.actor != expected_actor)
        or (work_order.source_type == WorkOrderSourceType.AGENT_TASK and not work_order.source_agent_id)
        or any(audit_event.details.get(key) != value for key, value in expected_details.items())
    ):
        raise ValueError("WorkOrder audit event must match its identity, workspace, and provenance")


def _decode(row: tuple[object, ...] | None, work_order_id: str | None = None) -> WorkOrder | None:
    if row is None:
        return None
    try:
        work_order = (
            WorkOrder.model_validate_json(row[1])
            if isinstance(row[1], str)
            else WorkOrder.model_validate(row[1])
        )
    except Exception as error:
        raise WorkOrderRepositoryError("Stored WorkOrder payload is invalid") from error
    if work_order.id != row[0] or (work_order_id is not None and work_order.id != work_order_id):
        raise WorkOrderRepositoryError("Stored WorkOrder identity is inconsistent")
    if len(row) > 2 and (
        work_order.project_id != row[2] or work_order.workspace_id != row[3]
    ):
        raise WorkOrderRepositoryScopeError("Stored WorkOrder scope does not match its row")
    return work_order


class SQLiteWorkOrderRepository:
    """Transactional WorkOrder writes for the local SQLite application store."""

    def __init__(self, connect: Callable[[], sqlite3.Connection]) -> None:
        self._connect = connect

    def get(self, work_order_id: str) -> WorkOrder | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT entity_id,payload FROM kv_store WHERE entity_type=? AND entity_id=?",
                ("work_order", work_order_id),
            ).fetchone()
        return _decode(row, work_order_id)

    def list_for_workspace(self, workspace_id: str | None = None) -> list[WorkOrder]:
        with self._connect() as conn:
            if workspace_id is None:
                rows = conn.execute(
                    "SELECT entity_id,payload FROM kv_store WHERE entity_type=? ORDER BY entity_id",
                    ("work_order",),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT entity_id,payload FROM kv_store WHERE entity_type=? "
                    "AND json_extract(payload,'$.workspace_id')=? ORDER BY entity_id",
                    ("work_order", workspace_id),
                ).fetchall()
        orders = [_decode(row) for row in rows]
        return [order for order in orders if order is not None]

    def create(self, *, work_order: WorkOrder, audit_event: AuditEvent) -> None:
        _validate_creation(work_order, audit_event)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            project_row = conn.execute(
                "SELECT entity_id,payload FROM kv_store WHERE entity_type=? AND entity_id=?",
                ("project", work_order.project_id),
            ).fetchone()
            if project_row is None:
                raise WorkOrderRepositoryScopeError("WorkOrder project does not exist")
            project = Project.model_validate_json(project_row[1])
            if project.id != project_row[0] or project.workspace_id != work_order.workspace_id:
                raise WorkOrderRepositoryScopeError("WorkOrder project workspace does not match")
            if work_order.workspace_id is not None:
                workspace_row = conn.execute(
                    "SELECT entity_id,payload FROM kv_store WHERE entity_type=? AND entity_id=?",
                    ("workspace", work_order.workspace_id),
                ).fetchone()
                if workspace_row is None:
                    raise WorkOrderRepositoryScopeError("WorkOrder workspace does not exist")
                workspace = Workspace.model_validate_json(workspace_row[1])
                if workspace.id != workspace_row[0]:
                    raise WorkOrderRepositoryScopeError("WorkOrder workspace identity is inconsistent")
            if work_order.idea_id:
                idea_row = conn.execute(
                    "SELECT payload FROM kv_store WHERE entity_type=? AND entity_id=?",
                    ("idea", work_order.idea_id),
                ).fetchone()
                if idea_row is None:
                    raise WorkOrderRepositoryScopeError("WorkOrder idea does not exist")
                idea = Idea.model_validate_json(idea_row[0])
                if idea.project_id not in (None, work_order.project_id) or idea.workspace_id != work_order.workspace_id:
                    raise WorkOrderRepositoryScopeError("WorkOrder idea scope does not match")
            if work_order.source_agent_id:
                agent_row = conn.execute(
                    "SELECT payload FROM kv_store WHERE entity_type=? AND entity_id=?",
                    ("external_agent", work_order.source_agent_id),
                ).fetchone()
                if agent_row is None:
                    raise WorkOrderRepositoryScopeError("WorkOrder source agent does not exist")
                agent = ExternalAgent.model_validate_json(agent_row[0])
                if agent.workspace_id != work_order.workspace_id:
                    raise WorkOrderRepositoryScopeError("WorkOrder source agent workspace does not match")
            conn.execute(
                "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
                ("work_order", work_order.id, work_order.model_dump_json()),
            )
            conn.execute(
                "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
                ("audit_event", audit_event.id, audit_event.model_dump_json()),
            )
            conn.commit()


class PostgresWorkOrderRepository:
    """Workspace and provenance checked WorkOrder writes in PostgreSQL."""

    def __init__(self, connect: Callable[[], Connection]) -> None:
        self._connect = connect

    def get(self, work_order_id: str) -> WorkOrder | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT work_order_id,payload,project_id,workspace_id FROM work_orders "
                "WHERE work_order_id=%s",
                (work_order_id,),
            ).fetchone()
        return _decode(row, work_order_id)

    def list_for_workspace(self, workspace_id: str | None = None) -> list[WorkOrder]:
        if workspace_id is None:
            raise WorkOrderRepositoryScopeError("PostgreSQL WorkOrder listing requires a workspace")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT work_order_id,payload,project_id,workspace_id FROM work_orders "
                "WHERE workspace_id=%s ORDER BY created_at DESC,work_order_id DESC",
                (workspace_id,),
            ).fetchall()
        orders = [_decode(row) for row in rows]
        return [order for order in orders if order is not None]

    def create(self, *, work_order: WorkOrder, audit_event: AuditEvent) -> None:
        _validate_creation(work_order, audit_event)
        if not work_order.workspace_id:
            raise WorkOrderRepositoryScopeError("PostgreSQL WorkOrders require a workspace")
        with self._connect() as conn:
            project = conn.execute(
                "SELECT workspace_id FROM projects WHERE project_id=%s FOR KEY SHARE",
                (work_order.project_id,),
            ).fetchone()
            if project is None or project[0] != work_order.workspace_id:
                raise WorkOrderRepositoryScopeError("WorkOrder project workspace does not match")
            if work_order.idea_id:
                idea = conn.execute(
                    "SELECT project_id,workspace_id FROM ideas WHERE idea_id=%s FOR KEY SHARE",
                    (work_order.idea_id,),
                ).fetchone()
                if idea is None or idea[0] != work_order.project_id or idea[1] != work_order.workspace_id:
                    raise WorkOrderRepositoryScopeError("WorkOrder idea scope does not match")
            if work_order.source_agent_id:
                agent = conn.execute(
                    "SELECT workspace_id FROM external_agents WHERE agent_id=%s FOR KEY SHARE",
                    (work_order.source_agent_id,),
                ).fetchone()
                if agent is None or agent[0] != work_order.workspace_id:
                    raise WorkOrderRepositoryScopeError("WorkOrder source agent scope does not match")
            if work_order.created_by_user_id:
                actor = conn.execute(
                    "SELECT workspace_id FROM users WHERE user_id=%s AND disabled=FALSE FOR KEY SHARE",
                    (work_order.created_by_user_id,),
                ).fetchone()
                if actor is None or actor[0] != work_order.workspace_id:
                    raise WorkOrderRepositoryScopeError("WorkOrder actor is disabled or outside its workspace")
            conn.execute(
                "INSERT INTO work_orders(work_order_id,project_id,workspace_id,idea_id,source_agent_id,created_at,payload) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (work_order.id, work_order.project_id, work_order.workspace_id, work_order.idea_id,
                 work_order.source_agent_id, work_order.created_at,
                 Jsonb(work_order.model_dump(mode="json"))),
            )
            conn.execute(
                "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,event_type,"
                "actor,details,created_at,payload) VALUES(%s,%s,NULL,'work_order',%s,%s,%s,%s,%s,%s)",
                (audit_event.id, work_order.workspace_id, work_order.id, audit_event.event_type,
                 audit_event.actor, Jsonb(audit_event.details), audit_event.created_at,
                 Jsonb(audit_event.model_dump(mode="json"))),
            )
