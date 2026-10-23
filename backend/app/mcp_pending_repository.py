from __future__ import annotations

import ast
import json
import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

import psycopg
from psycopg.types.json import Jsonb

from .approval_integrity import compute_approval_payload_hash
from .models import (
    AgentProvider,
    ApprovalRequest,
    ApprovalStatus,
    AuditEvent,
    MCPPendingRequest,
    MCPPendingStatus,
    MCPChildServer,
    Notification,
    Project,
    WorkOrder,
    new_id,
)


class MCPPendingRepositoryError(RuntimeError):
    """An MCP approval request could not be persisted or read safely."""


class MCPPendingRepositoryConflict(MCPPendingRepositoryError):
    """An MCP request changed concurrently or has an invalid transition."""


class MCPPendingRepositoryScopeError(MCPPendingRepositoryError):
    """An MCP request, approval, project, or workspace scope is inconsistent."""


class MCPPendingRepository(Protocol):
    def create_request(
        self,
        *,
        approval: ApprovalRequest,
        pending: MCPPendingRequest,
        audit_event: AuditEvent,
        notification: Notification,
    ) -> None: ...

    def get(self, pending_id: str) -> MCPPendingRequest | None: ...

    def get_for_approval(self, approval_id: str) -> MCPPendingRequest | None: ...

    def list_for_workspace(
        self,
        workspace_id: str | None,
        *,
        project_id: str | None = None,
        status: MCPPendingStatus | None = None,
        approval_id: str | None = None,
    ) -> list[MCPPendingRequest]: ...

    def claim_forwarding(
        self,
        *,
        pending_id: str,
        owner: str,
        now: datetime,
        lease_expires_at: datetime,
        workspace_id: str | None = None,
        approval_id: str | None = None,
        project_id: str | None = None,
        server_name: str | None = None,
    ) -> MCPPendingRequest | None: ...

    def finish_forwarding(
        self,
        *,
        expected: MCPPendingRequest,
        updated: MCPPendingRequest,
        audit_event: AuditEvent,
    ) -> None: ...


def _validate_request_records(
    *,
    approval: ApprovalRequest,
    pending: MCPPendingRequest,
    audit_event: AuditEvent,
    notification: Notification,
    require_workspace: bool,
) -> None:
    if require_workspace and not approval.workspace_id:
        raise MCPPendingRepositoryScopeError("PostgreSQL MCP approvals require a workspace")
    if (
        approval.status not in {ApprovalStatus.REQUESTED, ApprovalStatus.BLOCKED}
        or pending.status != MCPPendingStatus.WAITING_FOR_APPROVAL
        or approval.id != pending.approval_request_id
        or approval.project_id != pending.project_id
        or approval.workspace_id != pending.workspace_id
        or audit_event.event_type != "mcp.approval_requested"
        or audit_event.entity_id != approval.id
        or audit_event.workspace_id != approval.workspace_id
        or audit_event.details.get("mcp_pending_request_id") != pending.id
        or audit_event.details.get("server_name") != pending.server_name
        or audit_event.details.get("tool_name") != pending.tool_name
        or audit_event.details.get("mutating") is not True
        or notification.entity_type != "approval_request"
        or notification.entity_id != approval.id
        or notification.workspace_id != approval.workspace_id
    ):
        raise ValueError("MCP approval, pending request, audit, and notification identities must match")


def _validate_pending_audit(
    expected: MCPPendingRequest,
    updated: MCPPendingRequest,
    event: AuditEvent,
) -> None:
    if (
        expected.id != updated.id
        or expected.approval_request_id != updated.approval_request_id
        or expected.project_id != updated.project_id
        or expected.workspace_id != updated.workspace_id
        or event.entity_id != updated.approval_request_id
        or event.workspace_id != updated.workspace_id
        or event.details.get("mcp_pending_request_id") != updated.id
        or event.details.get("new_status") != updated.status
    ):
        raise ValueError("MCP pending transition audit identity and scope must match")


def _decode(
    row: tuple[object, ...] | None,
    pending_id: str | None = None,
) -> MCPPendingRequest | None:
    if row is None:
        return None
    try:
        pending = (
            MCPPendingRequest.model_validate_json(row[1])
            if isinstance(row[1], str)
            else MCPPendingRequest.model_validate(row[1])
        )
    except Exception as error:
        raise MCPPendingRepositoryError("Stored MCP pending payload is invalid") from error
    if pending.id != row[0] or (pending_id is not None and pending.id != pending_id):
        raise MCPPendingRepositoryError("Stored MCP pending identity is inconsistent")
    if len(row) > 3 and (
        pending.approval_request_id != row[2]
        or pending.workspace_id != row[3]
        or pending.project_id != row[4]
    ):
        raise MCPPendingRepositoryScopeError("Stored MCP pending row scope does not match payload")
    return pending


def apply_postgres_approval_state_transition(
    conn: psycopg.Connection,
    approval: ApprovalRequest,
) -> None:
    """Keep a linked MCP request aligned with its approval in the caller's transaction.

    Approval decisions and forwarding claims both lock the approval row before
    the pending row. This fixed order prevents the decision/claim deadlock that
    would result from taking the pending row first in one path.
    """
    rows = conn.execute(
        "SELECT pending_id,approval_id,workspace_id,project_id,payload "
        "FROM mcp_pending_requests WHERE approval_id=%s FOR UPDATE",
        (approval.id,),
    ).fetchall()
    if len(rows) > 1:
        raise MCPPendingRepositoryConflict(
            "Approval is linked to multiple MCP pending requests"
        )
    if not rows:
        return
    row = rows[0]
    pending = _decode((row[0], row[4], row[1], row[2], row[3]))
    if pending is None or (
        pending.approval_request_id != approval.id
        or pending.workspace_id != approval.workspace_id
        or pending.project_id != approval.project_id
    ):
        raise MCPPendingRepositoryScopeError(
            "MCP pending scope does not match its approval"
        )
    if pending.status != MCPPendingStatus.WAITING_FOR_APPROVAL:
        raise MCPPendingRepositoryConflict(
            "MCP pending request is not waiting for its approval decision"
        )
    if approval.status not in {
        ApprovalStatus.APPROVED,
        ApprovalStatus.DENIED,
        ApprovalStatus.REVISION_REQUESTED,
        ApprovalStatus.EXPIRED,
    }:
        raise MCPPendingRepositoryConflict(
            "MCP pending request received an unsupported approval decision"
        )
    if approval.status == ApprovalStatus.APPROVED:
        # It remains claimable only after the approval transaction commits.
        return

    updated = pending.model_copy(update={
        "status": MCPPendingStatus.BLOCKED_BY_DECISION,
        "lease_owner": None,
        "lease_expires_at": None,
        "updated_at": approval.updated_at,
    })
    result = conn.execute(
        "UPDATE mcp_pending_requests SET status=%s,lease_owner=NULL,lease_expires_at=NULL,"
        "updated_at=%s,payload=%s WHERE pending_id=%s AND approval_id=%s "
        "AND workspace_id=%s AND status=%s",
        (
            updated.status.value,
            updated.updated_at,
            Jsonb(updated.model_dump(mode="json")),
            updated.id,
            approval.id,
            approval.workspace_id,
            MCPPendingStatus.WAITING_FOR_APPROVAL.value,
        ),
    )
    if result.rowcount != 1:
        raise MCPPendingRepositoryConflict(
            "MCP pending request changed while committing its approval decision"
        )


class SQLiteMCPPendingRepository:
    """Transactional MCP approval and pending-request persistence for local mode."""

    def __init__(self, connect: Callable[[], sqlite3.Connection]) -> None:
        self._connect = connect

    def create_request(
        self,
        *,
        approval: ApprovalRequest,
        pending: MCPPendingRequest,
        audit_event: AuditEvent,
        notification: Notification,
    ) -> None:
        _validate_request_records(
            approval=approval, pending=pending, audit_event=audit_event,
            notification=notification, require_workspace=False,
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            project_row = conn.execute(
                "SELECT entity_id,payload FROM kv_store WHERE entity_type=? AND entity_id=?",
                ("project", approval.project_id),
            ).fetchone()
            if project_row is None:
                raise MCPPendingRepositoryScopeError("MCP approval project does not exist")
            project = Project.model_validate_json(project_row[1])
            if project.id != project_row[0] or project.workspace_id != approval.workspace_id:
                raise MCPPendingRepositoryScopeError("MCP approval project workspace does not match")
            if approval.work_order_id:
                row = conn.execute(
                    "SELECT payload FROM kv_store WHERE entity_type=? AND entity_id=?",
                    ("work_order", approval.work_order_id),
                ).fetchone()
                if row is None:
                    raise MCPPendingRepositoryScopeError("MCP approval work order does not exist")
                work_order = WorkOrder.model_validate_json(row[0])
                if work_order.project_id != approval.project_id or work_order.workspace_id != approval.workspace_id:
                    raise MCPPendingRepositoryScopeError("MCP approval work order scope does not match")
            for entity_type, entity_id in (
                ("approval_request", approval.id),
                ("mcp_pending_request", pending.id),
                ("audit_event", audit_event.id),
                ("notification", notification.id),
            ):
                if conn.execute(
                    "SELECT 1 FROM kv_store WHERE entity_type=? AND entity_id=?",
                    (entity_type, entity_id),
                ).fetchone():
                    raise MCPPendingRepositoryConflict(f"{entity_type} identifier already exists")
            conn.executemany(
                "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
                (
                    ("approval_request", approval.id, approval.model_dump_json()),
                    ("mcp_pending_request", pending.id, pending.model_dump_json()),
                    ("audit_event", audit_event.id, audit_event.model_dump_json()),
                    ("notification", notification.id, notification.model_dump_json()),
                ),
            )
            conn.commit()

    def get(self, pending_id: str) -> MCPPendingRequest | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT entity_id,payload FROM kv_store WHERE entity_type=? AND entity_id=?",
                ("mcp_pending_request", pending_id),
            ).fetchone()
        return _decode(row, pending_id)

    def get_for_approval(self, approval_id: str) -> MCPPendingRequest | None:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT entity_id,payload FROM kv_store WHERE entity_type=? "
                "AND json_extract(payload,'$.approval_request_id')=?",
                ("mcp_pending_request", approval_id),
            ).fetchall()
        if len(rows) > 1:
            raise MCPPendingRepositoryConflict("Approval is linked to multiple MCP pending requests")
        return _decode(rows[0]) if rows else None

    def list_for_workspace(
        self,
        workspace_id: str | None,
        *,
        project_id: str | None = None,
        status: MCPPendingStatus | None = None,
        approval_id: str | None = None,
    ) -> list[MCPPendingRequest]:
        query = "SELECT entity_id,payload FROM kv_store WHERE entity_type=?"
        params: list[object] = ["mcp_pending_request"]
        if workspace_id is not None:
            query += " AND json_extract(payload,'$.workspace_id')=?"
            params.append(workspace_id)
        if project_id is not None:
            query += " AND json_extract(payload,'$.project_id')=?"
            params.append(project_id)
        if status is not None:
            query += " AND json_extract(payload,'$.status')=?"
            params.append(status.value)
        if approval_id is not None:
            query += " AND json_extract(payload,'$.approval_request_id')=?"
            params.append(approval_id)
        query += " ORDER BY entity_id"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        result = [_decode(row) for row in rows]
        requests = [item for item in result if item is not None]
        if any(
            item.workspace_id != workspace_id
            or (project_id is not None and item.project_id != project_id)
            or (status is not None and item.status != status)
            or (approval_id is not None and item.approval_request_id != approval_id)
            for item in requests
        ):
            raise MCPPendingRepositoryScopeError("MCP pending query returned an inconsistent scope")
        return requests

    def claim_forwarding(
        self,
        *,
        pending_id: str,
        owner: str,
        now: datetime,
        lease_expires_at: datetime,
        workspace_id: str | None = None,
        approval_id: str | None = None,
        project_id: str | None = None,
        server_name: str | None = None,
    ) -> MCPPendingRequest | None:
        if not owner or lease_expires_at <= now:
            raise ValueError("MCP forwarding lease owner and future expiry are required")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT entity_id,payload FROM kv_store WHERE entity_type=? AND entity_id=?",
                ("mcp_pending_request", pending_id),
            ).fetchone()
            current = _decode(row, pending_id)
            if current is None:
                return None
            if (
                (workspace_id is not None and current.workspace_id != workspace_id)
                or (approval_id is not None and current.approval_request_id != approval_id)
                or (project_id is not None and current.project_id != project_id)
                or (server_name is not None and current.server_name != server_name)
            ):
                return None
            if current.status == MCPPendingStatus.FORWARDING and current.lease_expires_at and current.lease_expires_at > now:
                return None
            if current.status == MCPPendingStatus.FORWARDING:
                self._mark_outcome_unknown_sqlite(conn, current, now)
                conn.commit()
                return None
            if current.status not in {MCPPendingStatus.WAITING_FOR_APPROVAL, MCPPendingStatus.FORWARDING}:
                return None
            approval_row = conn.execute(
                "SELECT payload FROM kv_store WHERE entity_type=? AND entity_id=?",
                ("approval_request", current.approval_request_id),
            ).fetchone()
            if approval_row is None or not self._approval_is_currently_approved(approval_row[0], current):
                return None
            claimed = current.model_copy(update={
                "status": MCPPendingStatus.FORWARDING,
                "attempts": current.attempts + 1,
                "lease_owner": owner,
                "lease_expires_at": lease_expires_at,
                "updated_at": now,
            })
            conn.execute(
                "UPDATE kv_store SET payload=? WHERE entity_type=? AND entity_id=?",
                (claimed.model_dump_json(), "mcp_pending_request", pending_id),
            )
            conn.commit()
            return claimed

    @staticmethod
    def _approval_is_currently_approved(raw: str, pending: MCPPendingRequest) -> bool:
        approval = ApprovalRequest.model_validate_json(raw)
        return (
            approval.id == pending.approval_request_id
            and approval.project_id == pending.project_id
            and approval.workspace_id == pending.workspace_id
            and approval.status == ApprovalStatus.APPROVED
            and approval.approved_payload_hash_version == 2
            and approval.approved_payload_hash is not None
            and approval.approved_payload_hash == compute_approval_payload_hash(approval)
        )

    @staticmethod
    def _mark_outcome_unknown_sqlite(
        conn: sqlite3.Connection,
        current: MCPPendingRequest,
        now: datetime,
    ) -> None:
        updated = current.model_copy(update={
            "status": MCPPendingStatus.DEAD_LETTER,
            "lease_owner": None,
            "lease_expires_at": None,
            "last_error": "FORWARD_OUTCOME_UNKNOWN",
            "updated_at": now,
        })
        event = AuditEvent(
            id=new_id("audit"),
            event_type="mcp.forward_outcome_unknown",
            actor="system:mcp-recovery",
            entity_id=current.approval_request_id,
            workspace_id=current.workspace_id,
            details={
                "mcp_pending_request_id": current.id,
                "project_id": current.project_id,
                "previous_status": current.status,
                "new_status": updated.status,
                "reason_code": "FORWARD_OUTCOME_UNKNOWN",
            },
            created_at=now,
        )
        conn.execute(
            "UPDATE kv_store SET payload=? WHERE entity_type=? AND entity_id=?",
            (updated.model_dump_json(), "mcp_pending_request", current.id),
        )
        conn.execute(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            ("audit_event", event.id, event.model_dump_json()),
        )

    def finish_forwarding(
        self,
        *,
        expected: MCPPendingRequest,
        updated: MCPPendingRequest,
        audit_event: AuditEvent,
    ) -> None:
        _validate_pending_audit(expected, updated, audit_event)
        if expected.status != MCPPendingStatus.FORWARDING or updated.status not in {
            MCPPendingStatus.FORWARDED,
            MCPPendingStatus.WAITING_FOR_APPROVAL,
            MCPPendingStatus.DEAD_LETTER,
            MCPPendingStatus.ORPHANED,
            MCPPendingStatus.BLOCKED_BY_DECISION,
        }:
            raise MCPPendingRepositoryConflict("MCP pending transition is not allowed")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload FROM kv_store WHERE entity_type=? AND entity_id=?",
                ("mcp_pending_request", expected.id),
            ).fetchone()
            current = _decode((expected.id, row[0])) if row else None
            if current != expected:
                raise MCPPendingRepositoryConflict("MCP pending request changed before finalization")
            conn.execute(
                "UPDATE kv_store SET payload=? WHERE entity_type=? AND entity_id=?",
                (updated.model_dump_json(), "mcp_pending_request", updated.id),
            )
            conn.execute(
                "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
                ("audit_event", audit_event.id, audit_event.model_dump_json()),
            )
            conn.commit()


class PostgresMCPPendingRepository:
    """Workspace-scoped MCP approval and pending-forwarding transactions."""

    def __init__(self, connect: Callable[[], psycopg.Connection]) -> None:
        self._connect = connect

    def create_request(
        self,
        *,
        approval: ApprovalRequest,
        pending: MCPPendingRequest,
        audit_event: AuditEvent,
        notification: Notification,
    ) -> None:
        _validate_request_records(
            approval=approval, pending=pending, audit_event=audit_event,
            notification=notification, require_workspace=True,
        )
        with self._connect() as conn:
            project = conn.execute(
                "SELECT workspace_id FROM projects WHERE project_id=%s FOR KEY SHARE",
                (approval.project_id,),
            ).fetchone()
            if project is None or project[0] != approval.workspace_id:
                raise MCPPendingRepositoryScopeError("MCP approval project workspace does not match")
            if approval.work_order_id:
                order = conn.execute(
                    "SELECT project_id,workspace_id FROM work_orders WHERE work_order_id=%s FOR KEY SHARE",
                    (approval.work_order_id,),
                ).fetchone()
                if order is None or order != (approval.project_id, approval.workspace_id):
                    raise MCPPendingRepositoryScopeError("MCP approval work-order scope does not match")
            conn.execute(
                "INSERT INTO approval_requests(approval_id,project_id,workspace_id,work_order_id,"
                "source_agent_id,source_invocation_id,status,expires_at,created_at,updated_at,version,payload) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s)",
                (approval.id, approval.project_id, approval.workspace_id, approval.work_order_id,
                 approval.source_agent_id, approval.source_invocation_id, approval.status.value,
                 approval.expires_at, approval.created_at, approval.updated_at,
                 Jsonb(approval.model_dump(mode="json"))),
            )
            conn.execute(
                "INSERT INTO mcp_pending_requests(pending_id,approval_id,project_id,workspace_id,"
                "status,attempts,lease_expires_at,created_at,updated_at,payload) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (pending.id, approval.id, pending.project_id, pending.workspace_id,
                 pending.status.value, pending.attempts, pending.lease_expires_at,
                 pending.created_at, pending.updated_at,
                 Jsonb(pending.model_dump(mode="json"))),
            )
            self._insert_audit(conn, audit_event, approval.id)
            conn.execute(
                "INSERT INTO notifications(notification_id,workspace_id,user_id,approval_id,entity_type,"
                "status,attempts,next_attempt_at,lease_expires_at,created_at,payload) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (notification.id, notification.workspace_id, notification.user_id, approval.id,
                 notification.entity_type, notification.push_status, notification.push_attempts,
                 notification.push_next_attempt_at, notification.push_lease_expires_at,
                 notification.created_at, Jsonb(notification.model_dump(mode="json"))),
            )

    def get(self, pending_id: str) -> MCPPendingRequest | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT pending_id,approval_id,workspace_id,project_id,payload "
                "FROM mcp_pending_requests WHERE pending_id=%s",
                (pending_id,),
            ).fetchone()
        return _decode((row[0], row[4], row[1], row[2], row[3]), pending_id) if row else None

    def get_for_approval(self, approval_id: str) -> MCPPendingRequest | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT pending_id,approval_id,workspace_id,project_id,payload "
                "FROM mcp_pending_requests WHERE approval_id=%s",
                (approval_id,),
            ).fetchone()
        return _decode((row[0], row[4], row[1], row[2], row[3])) if row else None

    def list_for_workspace(
        self,
        workspace_id: str | None,
        *,
        project_id: str | None = None,
        status: MCPPendingStatus | None = None,
        approval_id: str | None = None,
    ) -> list[MCPPendingRequest]:
        if not workspace_id:
            raise MCPPendingRepositoryScopeError("PostgreSQL MCP pending listing requires a workspace")
        query = (
            "SELECT pending_id,approval_id,workspace_id,project_id,payload "
            "FROM mcp_pending_requests WHERE workspace_id=%s"
        )
        params: list[object] = [workspace_id]
        if project_id is not None:
            query += " AND project_id=%s"
            params.append(project_id)
        if status is not None:
            query += " AND status=%s"
            params.append(status.value)
        if approval_id is not None:
            query += " AND approval_id=%s"
            params.append(approval_id)
        query += " ORDER BY created_at DESC,pending_id"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        result = []
        for row in rows:
            pending = _decode((row[0], row[4], row[1], row[2], row[3]))
            if pending is None or pending.workspace_id != workspace_id:
                raise MCPPendingRepositoryScopeError("Stored MCP pending workspace is inconsistent")
            result.append(pending)
        return result

    def retry_for_workspace(
        self,
        *,
        pending_id: str,
        workspace_id: str,
        reason: str,
        reset_attempts: bool,
        actor: str,
        now: datetime,
    ) -> MCPPendingRequest | None:
        """Reset a retryable pending request and append its audit atomically.

        A retry already in WAITING_FOR_APPROVAL is an idempotent no-op. An
        expired FORWARDING lease is marked outcome-unknown and committed before
        returning a conflict so callers can never replay an uncertain child
        invocation.
        """
        if not workspace_id or not actor:
            raise MCPPendingRepositoryScopeError("PostgreSQL MCP retry requires workspace and actor")

        expired_lease = False
        updated: MCPPendingRequest | None = None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT pending_id,approval_id,workspace_id,project_id,payload "
                "FROM mcp_pending_requests WHERE pending_id=%s AND workspace_id=%s FOR UPDATE",
                (pending_id, workspace_id),
            ).fetchone()
            if row is None:
                return None
            current = _decode((row[0], row[4], row[1], row[2], row[3]), pending_id)
            if current is None:
                return None
            if current.workspace_id != workspace_id:
                raise MCPPendingRepositoryScopeError("MCP pending workspace is inconsistent")
            if current.last_error == "FORWARD_OUTCOME_UNKNOWN":
                raise MCPPendingRepositoryConflict(
                    "MCP forwarding outcome is unknown; create a new request instead of replaying this invocation"
                )
            if current.status in {
                MCPPendingStatus.FORWARDED,
                MCPPendingStatus.BLOCKED_BY_DECISION,
                MCPPendingStatus.ORPHANED,
            }:
                raise MCPPendingRepositoryConflict(
                    f"MCP pending request cannot be retried from {current.status}"
                )
            if current.status == MCPPendingStatus.FORWARDING:
                if current.lease_expires_at is not None and current.lease_expires_at > now:
                    raise MCPPendingRepositoryConflict("MCP pending request has an active lease")
                self._mark_outcome_unknown_postgres(conn, current, now)
                expired_lease = True
            elif current.status == MCPPendingStatus.WAITING_FOR_APPROVAL:
                # Repeated retry requests do not append duplicate audit events.
                return current
            elif current.status == MCPPendingStatus.DEAD_LETTER:
                updated = current.model_copy(update={
                    "status": MCPPendingStatus.WAITING_FOR_APPROVAL,
                    "attempts": 0 if reset_attempts else current.attempts,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "last_error": None,
                    "updated_at": now,
                })
                event = AuditEvent(
                    event_type="MCP_PENDING_RETRIED",
                    actor=actor,
                    entity_id=current.approval_request_id,
                    workspace_id=current.workspace_id,
                    details={
                        "mcp_pending_request_id": current.id,
                        "project_id": current.project_id,
                        "server_name": current.server_name,
                        "tool_name": current.tool_name,
                        "reason": reason,
                        "previous_status": current.status,
                        "new_status": updated.status,
                        "reset_attempts": reset_attempts,
                        "attempts": updated.attempts,
                    },
                    created_at=now,
                )
                conn.execute(
                    "UPDATE mcp_pending_requests SET status=%s,attempts=%s,lease_owner=NULL,"
                    "lease_expires_at=NULL,last_error=NULL,updated_at=%s,payload=%s "
                    "WHERE pending_id=%s AND workspace_id=%s",
                    (
                        updated.status.value,
                        updated.attempts,
                        updated.updated_at,
                        Jsonb(updated.model_dump(mode="json")),
                        updated.id,
                        workspace_id,
                    ),
                )
                self._insert_audit(conn, event, current.approval_request_id)
            else:
                raise MCPPendingRepositoryConflict(
                    f"MCP pending request cannot be retried from {current.status}"
                )

        if expired_lease:
            raise MCPPendingRepositoryConflict(
                "MCP forwarding lease expired with an unknown child-server outcome; create a new request instead of replaying"
            )
        return updated

    def claim_forwarding(
        self,
        *,
        pending_id: str,
        approval_id: str,
        workspace_id: str,
        project_id: str,
        server_name: str,
        owner: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> MCPPendingRequest | None:
        """Claim one exact, approved MCP action inside a workspace transaction.

        Approval, pending, and child-server rows are locked in that order. The
        decision writer takes approval before pending, and the server lock
        prevents a concurrent disable from racing past this claim check.
        """
        if not all((pending_id, approval_id, workspace_id, project_id, server_name, owner)):
            raise MCPPendingRepositoryScopeError(
                "PostgreSQL MCP forwarding claims require complete request scope"
            )
        if lease_expires_at <= now:
            raise ValueError("MCP forwarding lease must expire in the future")

        with self._connect() as conn:
            identity = conn.execute(
                "SELECT approval_id FROM mcp_pending_requests WHERE pending_id=%s",
                (pending_id,),
            ).fetchone()
            if identity is None or identity[0] != approval_id:
                return None

            approval_row = conn.execute(
                "SELECT approval_id,status,workspace_id,project_id,expires_at,payload "
                "FROM approval_requests WHERE approval_id=%s FOR UPDATE",
                (approval_id,),
            ).fetchone()
            if approval_row is None:
                return None

            pending_row = conn.execute(
                "SELECT pending_id,approval_id,workspace_id,project_id,status,attempts,"
                "lease_owner,lease_expires_at,last_error,payload "
                "FROM mcp_pending_requests WHERE pending_id=%s AND approval_id=%s "
                "AND workspace_id=%s AND project_id=%s FOR UPDATE",
                (pending_id, approval_id, workspace_id, project_id),
            ).fetchone()
            if pending_row is None:
                return None
            current = _decode(
                (pending_row[0], pending_row[9], pending_row[1], pending_row[2], pending_row[3]),
                pending_id,
            )
            if current is None:
                return None
            if (
                pending_row[1] != approval_id
                or pending_row[2] != workspace_id
                or pending_row[3] != project_id
                or current.workspace_id != workspace_id
                or current.project_id != project_id
            ):
                raise MCPPendingRepositoryScopeError(
                    "Stored MCP pending row scope does not match forwarding claim"
                )
            if (
                pending_row[4] != current.status.value
                or pending_row[5] != current.attempts
                or pending_row[6] != current.lease_owner
                or pending_row[7] != current.lease_expires_at
                or pending_row[8] != current.last_error
            ):
                raise MCPPendingRepositoryError(
                    "Stored MCP pending columns do not match their payload"
                )
            if (
                approval_row[0] != approval_id
                or approval_row[2] != workspace_id
                or approval_row[3] != project_id
            ):
                raise MCPPendingRepositoryScopeError(
                    "Stored approval scope does not match MCP forwarding claim"
                )
            try:
                approval = ApprovalRequest.model_validate(approval_row[5])
            except Exception as error:
                raise MCPPendingRepositoryError(
                    "Stored MCP approval payload is invalid"
                ) from error
            if (
                approval.id != approval_id
                or approval.workspace_id != workspace_id
                or approval.project_id != project_id
                or approval.status.value != approval_row[1]
                or approval.expires_at != approval_row[4]
            ):
                raise MCPPendingRepositoryScopeError(
                    "Stored approval columns do not match their payload"
                )

            if current.status == MCPPendingStatus.FORWARDING:
                if current.lease_expires_at is not None and current.lease_expires_at > now:
                    return None
                self._mark_outcome_unknown_postgres(conn, current, now)
                return None
            if current.status != MCPPendingStatus.WAITING_FOR_APPROVAL:
                return None
            if current.attempts >= current.max_attempts:
                return None
            if (
                approval.status != ApprovalStatus.APPROVED
                or approval.expires_at is not None and approval.expires_at <= now
                or approval.approved_payload_hash_version != 2
                or approval.approved_payload_hash is None
                or approval.approved_payload_hash != compute_approval_payload_hash(approval)
                or not self._approved_mcp_action_matches(approval, current)
            ):
                return None

            child_row = conn.execute(
                "SELECT server_id,project_id,workspace_id,name,enabled,payload "
                "FROM mcp_child_servers WHERE project_id=%s AND workspace_id=%s "
                "AND name=%s FOR UPDATE",
                (project_id, workspace_id, server_name),
            ).fetchone()
            if child_row is None:
                return None
            try:
                child = MCPChildServer.model_validate(child_row[5])
            except Exception as error:
                raise MCPPendingRepositoryError(
                    "Stored MCP child-server payload is invalid"
                ) from error
            if (
                child.id != child_row[0]
                or child.project_id != child_row[1]
                or child.workspace_id != child_row[2]
                or child.name != child_row[3]
                or child.enabled != child_row[4]
            ):
                raise MCPPendingRepositoryScopeError(
                    "Stored MCP child-server columns do not match their payload"
                )
            if not child.enabled:
                return None
            tool = next((item for item in child.tools if item.name == current.tool_name), None)
            if tool is None or not tool.mutating or current.server_name != server_name:
                return None

            claimed = current.model_copy(update={
                "status": MCPPendingStatus.FORWARDING,
                "attempts": current.attempts + 1,
                "lease_owner": owner,
                "lease_expires_at": lease_expires_at,
                "updated_at": now,
            })
            result = conn.execute(
                "UPDATE mcp_pending_requests SET status=%s,attempts=%s,lease_owner=%s,"
                "lease_expires_at=%s,updated_at=%s,payload=%s "
                "WHERE pending_id=%s AND approval_id=%s AND workspace_id=%s AND project_id=%s "
                "AND status=%s AND attempts=%s",
                (claimed.status.value, claimed.attempts, claimed.lease_owner,
                 claimed.lease_expires_at, claimed.updated_at,
                 Jsonb(claimed.model_dump(mode="json")), pending_id, approval_id,
                 workspace_id, project_id, MCPPendingStatus.WAITING_FOR_APPROVAL.value,
                 current.attempts),
            )
            if result.rowcount != 1:
                raise MCPPendingRepositoryConflict(
                    "MCP pending request changed during forwarding claim"
                )
            return claimed

    @staticmethod
    def _approved_mcp_action_matches(
        approval: ApprovalRequest,
        pending: MCPPendingRequest,
    ) -> bool:
        """Bind the approved, hash-frozen MCP tool call to the pending row."""
        prefix = "MCP mutating tool call requires approval: "
        if (
            approval.source_provider != AgentProvider.OTHER
            or approval.source_agent_name != "mcp-gateway"
            or approval.source_session_id != pending.session_id
            or len(approval.files) != 1
        ):
            return False
        file_change = approval.files[0]
        if file_change.path != f"mcp://{pending.server_name}/{pending.tool_name}":
            return False
        try:
            new_content = json.loads(file_change.new_content or "")
            diff = file_change.diff
            if not diff.startswith(prefix):
                return False
            diff_call = ast.literal_eval(diff[len(prefix):])
        except (json.JSONDecodeError, SyntaxError, ValueError, TypeError):
            return False
        if not isinstance(new_content, dict) or not isinstance(diff_call, dict):
            return False
        expected = {
            "server_name": pending.server_name,
            "tool_name": pending.tool_name,
            "arguments": pending.arguments,
            "session_id": pending.session_id,
            "requested_by": pending.requested_by,
            "mutating": True,
        }
        return set(new_content) == set(expected) and new_content == diff_call == expected

    @staticmethod
    def _mark_outcome_unknown_postgres(
        conn: psycopg.Connection,
        current: MCPPendingRequest,
        now: datetime,
    ) -> None:
        updated = current.model_copy(update={
            "status": MCPPendingStatus.DEAD_LETTER,
            "lease_owner": None,
            "lease_expires_at": None,
            "last_error": "FORWARD_OUTCOME_UNKNOWN",
            "updated_at": now,
        })
        event = AuditEvent(
            id=new_id("audit"),
            event_type="mcp.forward_outcome_unknown",
            actor="system:mcp-recovery",
            entity_id=current.approval_request_id,
            workspace_id=current.workspace_id,
            details={
                "mcp_pending_request_id": current.id,
                "project_id": current.project_id,
                "previous_status": current.status,
                "new_status": updated.status,
                "reason_code": "FORWARD_OUTCOME_UNKNOWN",
            },
            created_at=now,
        )
        conn.execute(
            "UPDATE mcp_pending_requests SET status=%s,lease_owner=NULL,lease_expires_at=NULL,"
            "last_error=%s,updated_at=%s,payload=%s WHERE pending_id=%s",
            (updated.status.value, updated.last_error, updated.updated_at,
             Jsonb(updated.model_dump(mode="json")), current.id),
        )
        PostgresMCPPendingRepository._insert_audit(
            conn, event, current.approval_request_id
        )

    def finish_forwarding(
        self,
        *,
        expected: MCPPendingRequest,
        updated: MCPPendingRequest,
        audit_event: AuditEvent,
    ) -> None:
        _validate_pending_audit(expected, updated, audit_event)
        if expected.status != MCPPendingStatus.FORWARDING or updated.status not in {
            MCPPendingStatus.FORWARDED,
            MCPPendingStatus.WAITING_FOR_APPROVAL,
            MCPPendingStatus.DEAD_LETTER,
            MCPPendingStatus.ORPHANED,
            MCPPendingStatus.BLOCKED_BY_DECISION,
        }:
            raise MCPPendingRepositoryConflict("MCP pending transition is not allowed")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM mcp_pending_requests WHERE pending_id=%s FOR UPDATE",
                (expected.id,),
            ).fetchone()
            current = _decode((expected.id, row[0])) if row else None
            if current != expected:
                raise MCPPendingRepositoryConflict("MCP pending request changed before finalization")
            conn.execute(
                "UPDATE mcp_pending_requests SET status=%s,attempts=%s,lease_owner=%s,"
                "lease_expires_at=%s,last_error=%s,updated_at=%s,payload=%s WHERE pending_id=%s",
                (updated.status.value, updated.attempts, updated.lease_owner,
                 updated.lease_expires_at, updated.last_error, updated.updated_at,
                 Jsonb(updated.model_dump(mode="json")), updated.id),
            )
            self._insert_audit(conn, audit_event, expected.approval_request_id)

    @staticmethod
    def _insert_audit(conn: psycopg.Connection, event: AuditEvent, approval_id: str) -> None:
        conn.execute(
            "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,"
            "event_type,actor,details,created_at,payload) "
            "VALUES(%s,%s,%s,'approval_request',%s,%s,%s,%s,%s,%s)",
            (event.id, event.workspace_id, approval_id, approval_id,
             event.event_type, event.actor, Jsonb(event.details), event.created_at,
             Jsonb(event.model_dump(mode="json"))),
        )
