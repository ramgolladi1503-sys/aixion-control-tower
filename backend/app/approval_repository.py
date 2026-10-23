from __future__ import annotations

import sqlite3
import hmac
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

import psycopg
from psycopg.types.json import Jsonb

from .agent_run_models import AgentRun
from .agent_credential_models import AgentCredentialRecord
from .agent_token_lifetime import effective_agent_token_expiry
from .agent_task_models import (
    AgentTask,
    AgentTaskEvent,
    AgentTaskEventType,
    AgentTaskStatus,
)
from .approval_integrity import compute_approval_payload_hash
from .approval_lifecycle import can_transition
from .mcp_pending_repository import (
    MCPPendingRepositoryError,
    apply_postgres_approval_state_transition,
)
from .models import (
    ApprovalRequest,
    ApprovalStatus,
    AgentAuthType,
    AuditEvent,
    ExternalAgent,
    Notification,
    Project,
    User,
    WorkOrder,
    Workspace,
    now_utc,
)


class ApprovalRecordMissing(LookupError):
    """The approval has no durable record to transition."""


class ApprovalDecisionConflict(RuntimeError):
    """The durable approval changed before this decision could commit."""


class ApprovalDeliveryCredentialRejected(RuntimeError):
    """The connector credential is no longer valid at the delivery claim boundary."""


class ApprovalInvocationLeaseRejected(RuntimeError):
    """The connector invocation is not durably live for this approval."""


class ApprovalDeliveryEvidenceIntegrityError(RuntimeError):
    """Stored delivery evidence is malformed or detached from its approval."""


class ApprovalDeliveryEvidenceUnavailable(RuntimeError):
    """The durable delivery evidence store could not be read."""


MAX_DELIVERY_EVIDENCE_APPROVAL_IDS = 100


def _validate_delivery_evidence_query(
    *, workspace_id: str, approval_ids: list[str]
) -> list[str]:
    if not workspace_id or len(workspace_id) > 256:
        raise ValueError("Delivery evidence reads require a valid workspace")
    if len(approval_ids) > MAX_DELIVERY_EVIDENCE_APPROVAL_IDS:
        raise ValueError("Delivery evidence query exceeds the bounded approval limit")
    if any(not isinstance(item, str) or not item or len(item) > 256 for item in approval_ids):
        raise ValueError("Delivery evidence query contains an invalid approval identity")
    if len(approval_ids) != len(set(approval_ids)):
        raise ValueError("Delivery evidence query requires unique approval identities")
    return approval_ids


def _delivery_evidence_timestamp(value: object, *, postgres: bool) -> datetime:
    try:
        if postgres:
            if not isinstance(value, datetime):
                raise ValueError("timestamp type is invalid")
            timestamp = value
        else:
            if not isinstance(value, str):
                raise ValueError("timestamp type is invalid")
            timestamp = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ApprovalDeliveryEvidenceIntegrityError(
            "Stored delivery evidence timestamp is invalid"
        ) from error
    if timestamp.utcoffset() is None:
        raise ApprovalDeliveryEvidenceIntegrityError(
            "Stored delivery evidence timestamp is not timezone-aware"
        )
    return timestamp


def _validate_delivery_evidence_row(
    *,
    requested_id: str,
    workspace_id: str,
    claim_id: object,
    claim_workspace_id: object,
    claim_agent_id: object,
    claim_invocation_id: object,
    claimed_at: object,
    failure_id: object,
    failure_workspace_id: object,
    failure_agent_id: object,
    failure_invocation_id: object,
    failure_reason_code: object,
    reported_at: object,
    approval_id: object,
    approval_workspace_id: object,
    approval_payload: object,
    postgres: bool,
) -> str:
    claim_values = (
        claim_id,
        claim_workspace_id,
        claim_agent_id,
        claim_invocation_id,
        claimed_at,
    )
    failure_values = (
        failure_id,
        failure_workspace_id,
        failure_agent_id,
        failure_invocation_id,
        failure_reason_code,
        reported_at,
    )
    if claim_id is None:
        if any(value is not None for value in claim_values) or any(
            value is not None for value in failure_values
        ):
            raise ApprovalDeliveryEvidenceIntegrityError(
                "Stored delivery failure is detached from its claim"
            )
        return "NOT_CONFIRMED"
    if (
        claim_id != requested_id
        or claim_workspace_id != workspace_id
        or not isinstance(claim_agent_id, str)
        or not claim_agent_id
        or not isinstance(claim_invocation_id, str)
        or not claim_invocation_id
        or approval_id != requested_id
        or approval_workspace_id != workspace_id
        or approval_payload is None
    ):
        raise ApprovalDeliveryEvidenceIntegrityError(
            "Stored delivery claim is detached from its workspace approval"
        )
    try:
        approval = (
            ApprovalRequest.model_validate(approval_payload)
            if postgres
            else ApprovalRequest.model_validate_json(approval_payload)
        )
    except Exception as error:
        raise ApprovalDeliveryEvidenceIntegrityError(
            "Stored delivery approval payload is invalid"
        ) from error
    if (
        approval.id != requested_id
        or approval.workspace_id != workspace_id
        or approval.source_agent_id != claim_agent_id
        or approval.source_invocation_id != claim_invocation_id
        or approval.status != ApprovalStatus.APPROVED
        or not approval.approved_payload_hash
        or approval.approved_payload_hash != compute_approval_payload_hash(approval)
    ):
        raise ApprovalDeliveryEvidenceIntegrityError(
            "Stored delivery claim identity does not match its approval"
        )
    claim_time = _delivery_evidence_timestamp(claimed_at, postgres=postgres)
    if failure_id is None:
        if any(value is not None for value in failure_values):
            raise ApprovalDeliveryEvidenceIntegrityError(
                "Stored delivery failure identity is incomplete"
            )
        return "CLAIM_RECORDED"
    if (
        failure_id != requested_id
        or failure_workspace_id != claim_workspace_id
        or failure_agent_id != claim_agent_id
        or failure_invocation_id != claim_invocation_id
        or failure_reason_code != "CLAIM_RESPONSE_AMBIGUOUS"
    ):
        raise ApprovalDeliveryEvidenceIntegrityError(
            "Stored delivery failure identity does not match its claim"
        )
    failure_time = _delivery_evidence_timestamp(reported_at, postgres=postgres)
    if failure_time < claim_time:
        raise ApprovalDeliveryEvidenceIntegrityError(
            "Stored delivery failure predates its claim"
        )
    return "FAILURE_REPORTED"


@dataclass(frozen=True)
class ApprovalInvocationLease:
    approval_id: str
    source_agent_id: str
    source_invocation_id: str
    heartbeat_at: datetime
    expires_at: datetime


def _invocation_lease_deadline(
    *, approval: ApprovalRequest, heartbeat_at: datetime, ttl_seconds: int
) -> datetime:
    if not 30 <= ttl_seconds <= 300:
        raise ValueError("Invocation lease TTL must be between 30 and 300 seconds")
    approval_expiry = approval.expires_at
    if approval_expiry is None or approval_expiry.tzinfo is None:
        raise ApprovalInvocationLeaseRejected("Approval has no valid invocation deadline")
    if heartbeat_at.tzinfo is None:
        raise ValueError("Invocation heartbeat timestamp must be timezone-aware")
    return min(heartbeat_at + timedelta(seconds=ttl_seconds), approval_expiry)


def _sqlite_lease_from_row(row: tuple[object, ...]) -> ApprovalInvocationLease:
    try:
        heartbeat_at = datetime.fromisoformat(str(row[4]))
        expires_at = datetime.fromisoformat(str(row[5]))
        if heartbeat_at.tzinfo is None or expires_at.tzinfo is None:
            raise ValueError("timezone missing")
        return ApprovalInvocationLease(
            approval_id=str(row[0]),
            source_agent_id=str(row[2]),
            source_invocation_id=str(row[3]),
            heartbeat_at=heartbeat_at,
            expires_at=expires_at,
        )
    except (TypeError, ValueError) as exc:
        raise ApprovalInvocationLeaseRejected("Invocation lease record is invalid") from exc


def _postgres_lease_from_row(row: tuple[object, ...]) -> ApprovalInvocationLease:
    if (
        len(row) != 6
        or not isinstance(row[4], datetime)
        or not isinstance(row[5], datetime)
        or row[4].tzinfo is None
        or row[5].tzinfo is None
    ):
        raise ApprovalInvocationLeaseRejected("Invocation lease record is invalid")
    return ApprovalInvocationLease(
        approval_id=str(row[0]),
        source_agent_id=str(row[2]),
        source_invocation_id=str(row[3]),
        heartbeat_at=row[4],
        expires_at=row[5],
    )


def _validate_live_decision_invocation(
    *,
    approval: ApprovalRequest,
    lease_row: tuple[object, ...] | None,
    now: datetime,
    postgres: bool,
) -> None:
    """Require a live lease for connector-bound decisions; leave manual decisions unchanged."""
    # Webhook and legacy approvals may identify a source agent without being
    # tied to a paused native invocation. Only a complete invocation identity
    # opts into the live-lease decision fence below.
    if approval.source_invocation_id is None:
        return
    if (
        not approval.workspace_id
        or not approval.source_agent_id
        or not approval.source_invocation_id
        or lease_row is None
    ):
        raise ApprovalDecisionConflict("Connector invocation is no longer live; reload the request.")
    try:
        lease = _postgres_lease_from_row(lease_row) if postgres else _sqlite_lease_from_row(lease_row)
    except ApprovalInvocationLeaseRejected as exc:
        raise ApprovalDecisionConflict(
            "Connector invocation is no longer live; reload the request."
        ) from exc
    if (
        lease.approval_id != approval.id
        or lease_row[1] != approval.workspace_id
        or lease.source_agent_id != approval.source_agent_id
        or lease.source_invocation_id != approval.source_invocation_id
        or lease.expires_at <= now
    ):
        raise ApprovalDecisionConflict("Connector invocation is no longer live; reload the request.")


def _require_current_delivery_credential(
    *,
    agent: ExternalAgent | None,
    credential: AgentCredentialRecord | None,
    source_agent_id: str,
    workspace_id: str | None,
    presented_token_hash: str,
    occurred_at: datetime,
) -> None:
    if (
        agent is None
        or credential is None
        or agent.id != source_agent_id
        or credential.agent_id != source_agent_id
        or agent.workspace_id != workspace_id
        or not agent.enabled
        or agent.auth_type == AgentAuthType.MANUAL
        or not agent.secret_hash
        or credential.token_revoked_at is not None
        or effective_agent_token_expiry(credential) <= occurred_at
        or not hmac.compare_digest(agent.secret_hash, presented_token_hash)
    ):
        raise ApprovalDeliveryCredentialRejected(
            "Connector credential is no longer valid for delivery"
        )


def _validate_sqlite_delivery_credential(
    conn: sqlite3.Connection,
    *,
    source_agent_id: str,
    workspace_id: str | None,
    presented_token_hash: str,
    occurred_at: datetime,
) -> None:
    agent_row = conn.execute(
        "SELECT payload FROM kv_store WHERE entity_type='external_agent' AND entity_id=?",
        (source_agent_id,),
    ).fetchone()
    credential_row = conn.execute(
        "SELECT payload FROM kv_store WHERE entity_type='external_agent_credential' AND entity_id=?",
        (source_agent_id,),
    ).fetchone()
    agent = ExternalAgent.model_validate_json(agent_row[0]) if agent_row else None
    credential = (
        AgentCredentialRecord.model_validate_json(credential_row[0])
        if credential_row
        else None
    )
    _require_current_delivery_credential(
        agent=agent,
        credential=credential,
        source_agent_id=source_agent_id,
        workspace_id=workspace_id,
        presented_token_hash=presented_token_hash,
        occurred_at=occurred_at,
    )


def _validate_postgres_delivery_credential(
    conn: psycopg.Connection,
    *,
    source_agent_id: str,
    workspace_id: str,
    presented_token_hash: str,
    occurred_at: datetime,
) -> None:
    agent_row = conn.execute(
        "SELECT payload,agent_id,workspace_id,enabled,auth_type,secret_hash "
        "FROM external_agents WHERE agent_id=%s AND workspace_id=%s FOR UPDATE",
        (source_agent_id, workspace_id),
    ).fetchone()
    credential_row = conn.execute(
        "SELECT payload,credential_id,agent_id,workspace_id,token_expires_at,token_revoked_at,"
        "token_rotated_at,created_at,updated_at FROM external_agent_credentials "
        "WHERE agent_id=%s AND workspace_id=%s FOR UPDATE",
        (source_agent_id, workspace_id),
    ).fetchone()
    agent = ExternalAgent.model_validate(agent_row[0]) if agent_row else None
    credential = (
        AgentCredentialRecord.model_validate(credential_row[0])
        if credential_row
        else None
    )
    if agent is not None and (
        agent.id != agent_row[1]
        or agent.workspace_id != agent_row[2]
        or agent.enabled is not agent_row[3]
        or agent.auth_type.value != agent_row[4]
        or agent.secret_hash != agent_row[5]
    ):
        raise ApprovalDeliveryCredentialRejected("Connector credential record is inconsistent")
    if credential is not None and (
        credential.id != credential_row[1]
        or credential.agent_id != credential_row[2]
        or credential.agent_id != source_agent_id
        or credential_row[3] != workspace_id
        or credential.token_expires_at != credential_row[4]
        or credential.token_revoked_at != credential_row[5]
        or credential.token_rotated_at != credential_row[6]
        or credential.created_at != credential_row[7]
        or credential.updated_at != credential_row[8]
    ):
        raise ApprovalDeliveryCredentialRejected("Connector credential record is inconsistent")
    _require_current_delivery_credential(
        agent=agent,
        credential=credential,
        source_agent_id=source_agent_id,
        workspace_id=workspace_id,
        presented_token_hash=presented_token_hash,
        occurred_at=occurred_at,
    )


def _validate_delivery_failure_report(
    *,
    approval_id: str,
    workspace_id: str,
    source_agent_id: str,
    source_invocation_id: str,
    expected_payload_hash: str,
    reason_code: str,
    audit_event: AuditEvent,
) -> None:
    expected_details = {
        "source_agent_id": source_agent_id,
        "source_invocation_id": source_invocation_id,
        "approved_payload_hash": expected_payload_hash,
        "reason_code": reason_code,
    }
    if (
        reason_code != "CLAIM_RESPONSE_AMBIGUOUS"
        or not approval_id
        or not workspace_id
        or not source_agent_id
        or not source_invocation_id
        or not expected_payload_hash
        or audit_event.event_type != "approval.delivery_failed"
        or audit_event.entity_id != approval_id
        or audit_event.workspace_id != workspace_id
        or audit_event.actor != f"agent:{source_agent_id}"
        or any(
            key not in audit_event.details or audit_event.details[key] != value
            for key, value in expected_details.items()
            if value is not None
        )
    ):
        raise ValueError("Delivery failure report audit identity and scope must match")


@dataclass(frozen=True)
class ApprovalDecisionCommit:
    linked_task: AgentTask | None
    notification: Notification
    linked_task_event: AgentTaskEvent | None = None
    linked_task_audit_event: AuditEvent | None = None
    linked_task_notification: Notification | None = None
    linked_run: AgentRun | None = None


@dataclass(frozen=True)
class ApprovalExpirationCommit:
    approval: ApprovalRequest
    linked_task: AgentTask | None = None
    linked_task_event: AgentTaskEvent | None = None
    linked_task_audit_event: AuditEvent | None = None
    linked_task_notification: Notification | None = None


def _task_expiration_records(
    *, task: AgentTask, approval: ApprovalRequest, actor: str
) -> tuple[AgentTask, AgentTaskEvent, AuditEvent, Notification]:
    if task.approval_request_id != approval.id:
        raise ApprovalDecisionConflict("Linked AgentTask references a different approval.")
    if (
        task.workspace_id != approval.workspace_id
        or task.project_id != approval.project_id
        or task.status != AgentTaskStatus.WAITING_FOR_APPROVAL
    ):
        raise ApprovalDecisionConflict(
            "Linked AgentTask scope or status changed before approval expiration."
        )
    notification = Notification(
        title=f"Agent task approval expired: {task.title}",
        body="The linked approval expired. The task remains blocked pending a new request.",
        entity_type="agent_task",
        entity_id=task.id,
        user_id=task.created_by_user_id,
        workspace_id=task.workspace_id,
        approval_request_id=approval.id,
    )
    task_event = AgentTaskEvent(
        task_id=task.id,
        event_type=AgentTaskEventType.NOTE,
        message="Linked approval expired; task remains blocked.",
        status=task.status,
        actor=actor,
        metadata={
            "approval_request_id": approval.id,
            "approval_status": ApprovalStatus.EXPIRED,
            "task_status": task.status,
            "notification_id": notification.id,
        },
        created_at=approval.updated_at,
    )
    task_audit = AuditEvent(
        event_type="agent_task.approval_expired",
        entity_id=task.id,
        workspace_id=task.workspace_id,
        actor=actor,
        details={
            "approval_request_id": approval.id,
            "approval_status": ApprovalStatus.EXPIRED,
            "task_status": task.status,
            "notification_id": notification.id,
        },
        created_at=approval.updated_at,
    )
    projected_task = task.model_copy(update={"updated_at": approval.updated_at})
    return projected_task, task_event, task_audit, notification


def _task_decision_records(
    *,
    task: AgentTask,
    approval: ApprovalRequest,
    previous_status: ApprovalStatus,
    actor: str,
) -> tuple[AgentTaskEvent, AuditEvent, Notification]:
    if approval.status == ApprovalStatus.APPROVED:
        event_type = AgentTaskEventType.APPROVED
        message = "Linked approval was approved."
        title = f"Agent task approved: {task.title}"
        body = "Worker can now continue after the linked approval decision."
    elif approval.status == ApprovalStatus.DENIED:
        event_type = AgentTaskEventType.DENIED
        message = "Linked approval was denied."
        title = f"Agent task denied: {task.title}"
        body = "Linked approval was denied. Worker must not continue."
    elif approval.status == ApprovalStatus.REVISION_REQUESTED:
        event_type = AgentTaskEventType.NOTE
        message = "Linked approval requested revision."
        title = f"Agent task needs revision: {task.title}"
        body = "Linked approval requested revision before work continues."
    else:
        event_type = AgentTaskEventType.NOTE
        message = f"Linked approval moved from {previous_status} to {approval.status}."
        title = f"Agent task updated: {task.title}"
        body = f"Linked approval moved to {approval.status}."
    notification = Notification(
        title=title,
        body=body,
        entity_type="agent_task",
        entity_id=task.id,
        user_id=task.created_by_user_id,
        workspace_id=task.workspace_id,
        approval_request_id=approval.id,
    )
    task_event = AgentTaskEvent(
        task_id=task.id,
        event_type=event_type,
        message=message,
        status=task.status,
        actor=actor,
        metadata={
            "approval_request_id": approval.id,
            "previous_status": previous_status,
            "new_status": approval.status,
            "notification_id": notification.id,
        },
    )
    task_audit = AuditEvent(
        event_type="agent_task.approval_decision_propagated",
        entity_id=task.id,
        workspace_id=task.workspace_id,
        actor=actor,
        details={
            "approval_request_id": approval.id,
            "previous_status": previous_status,
            "new_status": approval.status,
            "task_status": task.status,
            "notification_id": notification.id,
        },
    )
    return task_event, task_audit, notification


def _validate_approval_creation(
    approval: ApprovalRequest,
    audit_event: AuditEvent,
    notification: Notification,
    *,
    require_workspace: bool,
    notification_entity_type: str = "approval_request",
    notification_entity_id: str | None = None,
) -> None:
    if require_workspace and approval.workspace_id is None:
        raise ValueError("Approval workspace is required for durable creation.")
    if approval.status not in {
        ApprovalStatus.REQUESTED,
        ApprovalStatus.PENDING_REVIEW,
        ApprovalStatus.BLOCKED,
    }:
        raise ValueError("New approvals must begin in a pending or blocked state.")
    if (
        audit_event.event_type not in {"approval.created", "approval.created_by_agent"}
        or audit_event.entity_id != approval.id
        or audit_event.workspace_id != approval.workspace_id
    ):
        raise ValueError("Approval creation audit event must match the approval identity and scope.")
    if (
        notification.entity_type != notification_entity_type
        or notification.entity_id != (notification_entity_id or approval.id)
        or notification.workspace_id != approval.workspace_id
    ):
        raise ValueError("Approval creation notification must match the approval identity and scope.")


def _validate_postgres_work_order_scope(
    conn: psycopg.Connection,
    approval: ApprovalRequest,
) -> None:
    """Validate an optional linked WorkOrder inside the approval transaction."""
    if approval.work_order_id is None:
        return
    row = conn.execute(
        "SELECT work_order_id, project_id, workspace_id, payload "
        "FROM work_orders WHERE work_order_id=%s FOR SHARE",
        (approval.work_order_id,),
    ).fetchone()
    if row is None:
        raise ApprovalDecisionConflict("Approval work order is missing.")
    try:
        work_order = WorkOrder.model_validate(row[3])
    except Exception as exc:
        raise ApprovalDecisionConflict("Approval work order payload is invalid.") from exc
    if (
        work_order.id != row[0]
        or work_order.project_id != row[1]
        or work_order.workspace_id != row[2]
        or row[0] != approval.work_order_id
        or row[1] != approval.project_id
        or row[2] != approval.workspace_id
    ):
        raise ApprovalDecisionConflict(
            "Approval work order ownership does not match its project and workspace."
        )


def _validate_expiration(
    approval_id: str,
    expected_approval: ApprovalRequest,
    expired_approval: ApprovalRequest,
    audit_event: AuditEvent,
) -> None:
    pending = {ApprovalStatus.REQUESTED, ApprovalStatus.PENDING_REVIEW}
    if expected_approval.id != approval_id or expired_approval.id != approval_id:
        raise ValueError("Approval identifiers must match the expiration target")
    if expected_approval.status not in pending or expired_approval.status != ApprovalStatus.EXPIRED:
        raise ValueError("Expiration must transition a pending approval to EXPIRED")
    if (
        audit_event.event_type != "approval.expired"
        or audit_event.entity_id != approval_id
        or audit_event.details.get("previous_status") != expected_approval.status
        or audit_event.details.get("new_status") != ApprovalStatus.EXPIRED
    ):
        raise ValueError("Expiration audit event must match the committed transition")
    if audit_event.workspace_id != expected_approval.workspace_id:
        raise ValueError("Expiration audit workspace must match the approval workspace")
    if audit_event.details.get("expires_at") != expired_approval.expires_at:
        raise ValueError("Expiration audit deadline must match the committed approval")
    expected_expired = expected_approval.model_copy(
        update={
            "status": ApprovalStatus.EXPIRED,
            "updated_at": expired_approval.updated_at,
            "expires_at": expired_approval.expires_at,
        }
    )
    if expired_approval != expected_expired:
        raise ValueError("Expiration must preserve the approval request payload")


def _validate_expiry_backfill(
    approval_id: str,
    expected_approval: ApprovalRequest,
    updated_approval: ApprovalRequest,
    audit_event: AuditEvent,
) -> None:
    if (
        expected_approval.id != approval_id
        or updated_approval.id != approval_id
        or expected_approval.status not in {ApprovalStatus.REQUESTED, ApprovalStatus.PENDING_REVIEW}
        or updated_approval.status != expected_approval.status
        or expected_approval.expires_at is not None
        or updated_approval.expires_at is None
        or updated_approval.expires_at <= expected_approval.created_at
        or updated_approval.updated_at != audit_event.created_at
    ):
        raise ValueError("Expiry backfill must update one pending approval with a single deadline")
    if updated_approval != expected_approval.model_copy(
        update={
            "expires_at": updated_approval.expires_at,
            "updated_at": updated_approval.updated_at,
        }
    ):
        raise ValueError("Expiry backfill must preserve the approval request payload")
    if (
        audit_event.event_type != "approval.expiry_backfilled"
        or audit_event.entity_id != approval_id
        or audit_event.workspace_id != expected_approval.workspace_id
        or audit_event.details.get("previous_expires_at") is not None
        or audit_event.details.get("expires_at") != updated_approval.expires_at
    ):
        raise ValueError("Expiry backfill audit event must match the approval and deadline")


class ApprovalDecisionRepository(Protocol):
    def get(self, approval_id: str) -> ApprovalRequest | None: ...

    def list_delivery_outcomes(
        self, *, workspace_id: str, approval_ids: list[str]
    ) -> dict[str, str]: ...

    def heartbeat_invocation(
        self,
        *,
        approval_id: str,
        workspace_id: str,
        source_agent_id: str,
        source_invocation_id: str,
        presented_token_hash: str,
        occurred_at: datetime,
        ttl_seconds: int,
    ) -> ApprovalInvocationLease: ...

    def get_by_invocation(
        self, *, workspace_id: str | None, source_agent_id: str, source_invocation_id: str
    ) -> ApprovalRequest | None: ...

    def claim_approved_delivery(
        self,
        *,
        approval_id: str,
        workspace_id: str | None,
        source_agent_id: str,
        source_invocation_id: str,
        expected_payload_hash: str,
        presented_token_hash: str,
        audit_event: AuditEvent,
    ) -> bool: ...

    def record_approved_delivery_failure(
        self,
        *,
        approval_id: str,
        workspace_id: str,
        source_agent_id: str,
        source_invocation_id: str,
        expected_payload_hash: str,
        reason_code: str,
        audit_event: AuditEvent,
    ) -> bool: ...

    def list_for_workspace(self, workspace_id: str | None = None) -> list[ApprovalRequest]: ...

    def list_due_pending(
        self,
        *,
        now: datetime,
        legacy_ttl_seconds: int,
        limit: int,
        workspace_id: str | None = None,
    ) -> list[ApprovalRequest]: ...

    def latest_decision_audit(
        self,
        *,
        approval_id: str,
        workspace_id: str | None,
        new_status: ApprovalStatus,
    ) -> AuditEvent | None: ...

    def create(
        self,
        *,
        approval: ApprovalRequest,
        audit_event: AuditEvent,
        notification: Notification,
        invocation_lease_ttl_seconds: int | None = None,
    ) -> None: ...

    def create_for_agent_task(
        self,
        *,
        approval: ApprovalRequest,
        audit_event: AuditEvent,
        task: AgentTask,
        task_event: AgentTaskEvent,
        task_audit_event: AuditEvent,
        notification: Notification,
    ) -> None: ...

    def backfill_expiry(
        self,
        *,
        approval_id: str,
        expected_approval: ApprovalRequest,
        updated_approval: ApprovalRequest,
        audit_event: AuditEvent,
    ) -> tuple[ApprovalRequest, bool]: ...

    def expire_pending(
        self,
        *,
        approval_id: str,
        expected_approval: ApprovalRequest,
        expired_approval: ApprovalRequest,
        audit_event: AuditEvent,
    ) -> ApprovalExpirationCommit: ...

    def commit_decision(
        self,
        *,
        approval_id: str,
        expected_status: ApprovalStatus,
        expected_approval: ApprovalRequest,
        updated_approval: ApprovalRequest,
        audit_event: AuditEvent,
        decision_notification: Notification,
        run_repository: object | None = None,
    ) -> ApprovalDecisionCommit: ...


class SQLiteApprovalDecisionRepository:
    """Atomic decision adapter for the legacy SQLite key-value store.

    This enforces cross-connection compare-and-set for approval state plus its
    audit event. The wider application store remains process-local, so this is
    not a multi-process deployment guarantee.
    """

    def __init__(self, connect: Callable[[], sqlite3.Connection]) -> None:
        self._connect = connect

    def get(self, approval_id: str) -> ApprovalRequest | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT approval.entity_id, approval.payload, project.payload FROM kv_store AS approval "
                "JOIN kv_store AS project ON project.entity_type='project' "
                "AND project.entity_id=json_extract(approval.payload, '$.project_id') "
                "WHERE approval.entity_type = ? AND approval.entity_id = ?",
                ("approval_request", approval_id),
            ).fetchone()
        if row is None:
            return None
        approval = ApprovalRequest.model_validate_json(row[1])
        project = Project.model_validate_json(row[2])
        if (
            approval.id != approval_id
            or row[0] != approval_id
            or approval.project_id != project.id
            or (
                approval.workspace_id is not None
                and approval.workspace_id != project.workspace_id
            )
        ):
            return None
        return approval

    def heartbeat_invocation(
        self,
        *,
        approval_id: str,
        workspace_id: str,
        source_agent_id: str,
        source_invocation_id: str,
        presented_token_hash: str,
        occurred_at: datetime,
        ttl_seconds: int,
    ) -> ApprovalInvocationLease:
        if occurred_at.tzinfo is None or not workspace_id:
            raise ValueError("Invocation heartbeat requires an aware timestamp and workspace")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            _validate_sqlite_delivery_credential(
                conn,
                source_agent_id=source_agent_id,
                workspace_id=workspace_id,
                presented_token_hash=presented_token_hash,
                occurred_at=now_utc(),
            )
            approval_row = conn.execute(
                "SELECT entity_id,payload FROM kv_store "
                "WHERE entity_type='approval_request' AND entity_id=?",
                (approval_id,),
            ).fetchone()
            if approval_row is None:
                raise ApprovalInvocationLeaseRejected("Approval invocation is unavailable")
            try:
                approval = ApprovalRequest.model_validate_json(approval_row[1])
            except Exception as exc:
                raise ApprovalInvocationLeaseRejected("Approval invocation record is invalid") from exc
            if (
                approval_row[0] != approval_id
                or approval.id != approval_id
                or approval.workspace_id != workspace_id
                or approval.source_agent_id != source_agent_id
                or approval.source_invocation_id != source_invocation_id
                or approval.status not in {ApprovalStatus.PENDING_REVIEW, ApprovalStatus.APPROVED}
                or approval.expires_at is None
                or approval.expires_at <= now_utc()
            ):
                raise ApprovalInvocationLeaseRejected("Approval invocation is no longer live")
            claim = conn.execute(
                "SELECT 1 FROM approval_delivery_claims WHERE approval_id=?",
                (approval_id,),
            ).fetchone()
            if claim is not None:
                raise ApprovalInvocationLeaseRejected("Approval invocation was already consumed")
            existing = conn.execute(
                "SELECT approval_id,workspace_id,source_agent_id,source_invocation_id,heartbeat_at,expires_at "
                "FROM approval_invocation_leases WHERE approval_id=?",
                (approval_id,),
            ).fetchone()
            heartbeat_at = now_utc()
            _validate_sqlite_delivery_credential(
                conn,
                source_agent_id=source_agent_id,
                workspace_id=workspace_id,
                presented_token_hash=presented_token_hash,
                occurred_at=heartbeat_at,
            )
            if approval.expires_at is None or approval.expires_at <= heartbeat_at:
                raise ApprovalInvocationLeaseRejected("Approval invocation is no longer live")
            if existing is None:
                raise ApprovalInvocationLeaseRejected("Invocation lease is missing")
            current = _sqlite_lease_from_row(existing)
            if (
                current.approval_id != approval_id
                or existing[1] != workspace_id
                or current.source_agent_id != source_agent_id
                or current.source_invocation_id != source_invocation_id
            ):
                raise ApprovalInvocationLeaseRejected("Invocation lease identity changed")
            if current.expires_at <= heartbeat_at:
                raise ApprovalInvocationLeaseRejected("Invocation lease expired and cannot be renewed")
            expires_at = _invocation_lease_deadline(
                approval=approval, heartbeat_at=heartbeat_at, ttl_seconds=ttl_seconds
            )
            try:
                updated = conn.execute(
                    "UPDATE approval_invocation_leases SET heartbeat_at=?,expires_at=? "
                    "WHERE approval_id=? AND workspace_id=? AND source_agent_id=? "
                    "AND source_invocation_id=? AND expires_at>?",
                    (heartbeat_at.isoformat(), expires_at.isoformat(), approval_id,
                     workspace_id, source_agent_id, source_invocation_id, heartbeat_at.isoformat()),
                )
                if updated.rowcount != 1:
                    raise ApprovalInvocationLeaseRejected("Invocation lease expired and cannot be renewed")
            except sqlite3.IntegrityError as exc:
                raise ApprovalInvocationLeaseRejected("Invocation lease scope conflicts") from exc
            conn.commit()
        return ApprovalInvocationLease(
            approval_id=approval_id,
            source_agent_id=source_agent_id,
            source_invocation_id=source_invocation_id,
            heartbeat_at=heartbeat_at,
            expires_at=expires_at,
        )

    def get_by_invocation(
        self, *, workspace_id: str | None, source_agent_id: str, source_invocation_id: str
    ) -> ApprovalRequest | None:
        if not source_agent_id or not source_invocation_id:
            raise ValueError("Invocation lookup requires workspace, connector, and invocation identity")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT entity_id,payload FROM kv_store "
                "WHERE entity_type='approval_request' "
                "AND json_extract(payload,'$.workspace_id') IS ? "
                "AND json_extract(payload,'$.source_agent_id')=? "
                "AND json_extract(payload,'$.source_invocation_id')=?",
                (workspace_id, source_agent_id, source_invocation_id),
            ).fetchall()
        if len(rows) > 1:
            raise ApprovalDecisionConflict("Connector invocation maps to multiple approvals.")
        if not rows:
            return None
        entity_id, payload = rows[0]
        approval = ApprovalRequest.model_validate_json(payload)
        if (
            approval.id != entity_id
            or approval.workspace_id != workspace_id
            or approval.source_agent_id != source_agent_id
            or approval.source_invocation_id != source_invocation_id
        ):
            raise ApprovalDecisionConflict("Invocation approval identity is inconsistent.")
        return approval

    def claim_approved_delivery(
        self,
        *,
        approval_id: str,
        workspace_id: str | None,
        source_agent_id: str,
        source_invocation_id: str,
        expected_payload_hash: str,
        presented_token_hash: str,
        audit_event: AuditEvent,
    ) -> bool:
        if (
            audit_event.event_type != "approval.delivery_claimed"
            or audit_event.entity_id != approval_id
            or audit_event.workspace_id != workspace_id
        ):
            raise ValueError("Delivery audit identity must match the approval scope")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            _validate_sqlite_delivery_credential(
                conn,
                source_agent_id=source_agent_id,
                workspace_id=workspace_id,
                presented_token_hash=presented_token_hash,
                occurred_at=now_utc(),
            )
            row = conn.execute(
                "SELECT payload FROM kv_store WHERE entity_type='approval_request' AND entity_id=?",
                (approval_id,),
            ).fetchone()
            if row is None:
                raise ApprovalRecordMissing(approval_id)
            approval = ApprovalRequest.model_validate_json(row[0])
            if (
                approval.workspace_id != workspace_id
                or approval.source_agent_id != source_agent_id
                or approval.source_invocation_id != source_invocation_id
                or approval.approved_payload_hash_version != 2
                or approval.approved_payload_hash != expected_payload_hash
                or expected_payload_hash != compute_approval_payload_hash(approval)
            ):
                raise ApprovalDecisionConflict("Approved invocation identity or payload did not match")
            if approval.status != ApprovalStatus.APPROVED:
                return False
            lease_row = conn.execute(
                "SELECT approval_id,workspace_id,source_agent_id,source_invocation_id,heartbeat_at,expires_at "
                "FROM approval_invocation_leases WHERE approval_id=?",
                (approval_id,),
            ).fetchone()
            if lease_row is None:
                raise ApprovalInvocationLeaseRejected("Connector invocation is not live")
            lease = _sqlite_lease_from_row(lease_row)
            claim_time = now_utc()
            _validate_sqlite_delivery_credential(
                conn,
                source_agent_id=source_agent_id,
                workspace_id=workspace_id,
                presented_token_hash=presented_token_hash,
                occurred_at=claim_time,
            )
            claim_audit_event = audit_event.model_copy(update={"created_at": claim_time})
            if approval.expires_at is None or approval.expires_at <= claim_time:
                raise ApprovalInvocationLeaseRejected("Approval expired before delivery claim")
            if (
                lease.approval_id != approval_id
                or lease_row[1] != workspace_id
                or lease.source_agent_id != source_agent_id
                or lease.source_invocation_id != source_invocation_id
                or lease.expires_at <= claim_time
            ):
                raise ApprovalInvocationLeaseRejected("Connector invocation lease is expired or mismatched")
            try:
                conn.execute(
                    "INSERT INTO approval_delivery_claims "
                    "(approval_id,workspace_id,source_agent_id,source_invocation_id,claimed_at) "
                    "VALUES(?,?,?,?,?)",
                    (
                        approval_id,
                        workspace_id or "",
                        source_agent_id,
                        source_invocation_id,
                        claim_time.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError:
                return False
            conn.execute(
                "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
                ("audit_event", claim_audit_event.id, claim_audit_event.model_dump_json()),
            )
            conn.commit()
            return True

    def record_approved_delivery_failure(
        self,
        *,
        approval_id: str,
        workspace_id: str,
        source_agent_id: str,
        source_invocation_id: str,
        expected_payload_hash: str,
        reason_code: str,
        audit_event: AuditEvent,
    ) -> bool:
        _validate_delivery_failure_report(
            approval_id=approval_id,
            workspace_id=workspace_id,
            source_agent_id=source_agent_id,
            source_invocation_id=source_invocation_id,
            expected_payload_hash=expected_payload_hash,
            reason_code=reason_code,
            audit_event=audit_event,
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload FROM kv_store WHERE entity_type='approval_request' AND entity_id=?",
                (approval_id,),
            ).fetchone()
            if row is None:
                raise ApprovalRecordMissing(approval_id)
            approval = ApprovalRequest.model_validate_json(row[0])
            if (
                approval.workspace_id != workspace_id
                or approval.source_agent_id != source_agent_id
                or approval.source_invocation_id != source_invocation_id
                or approval.approved_payload_hash_version != 2
                or approval.approved_payload_hash != expected_payload_hash
                or expected_payload_hash != compute_approval_payload_hash(approval)
            ):
                raise ApprovalDecisionConflict("Delivery failure identity or payload did not match")
            claim = conn.execute(
                "SELECT workspace_id,source_agent_id,source_invocation_id "
                "FROM approval_delivery_claims WHERE approval_id=?",
                (approval_id,),
            ).fetchone()
            if claim != (workspace_id, source_agent_id, source_invocation_id):
                raise ApprovalDecisionConflict("Delivery failure requires the matching durable claim")
            prior = conn.execute(
                "SELECT workspace_id,source_agent_id,source_invocation_id,reason_code "
                "FROM approval_delivery_failures WHERE approval_id=?",
                (approval_id,),
            ).fetchone()
            expected = (workspace_id, source_agent_id, source_invocation_id, reason_code)
            if prior is not None:
                if prior != expected:
                    raise ApprovalDecisionConflict("A different delivery failure is already recorded")
                return False
            conn.execute(
                "INSERT INTO approval_delivery_failures "
                "(approval_id,workspace_id,source_agent_id,source_invocation_id,reason_code,reported_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    approval_id,
                    workspace_id,
                    source_agent_id,
                    source_invocation_id,
                    reason_code,
                    audit_event.created_at.isoformat(),
                ),
            )
            conn.execute(
                "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
                ("audit_event", audit_event.id, audit_event.model_dump_json()),
            )
            conn.commit()
            return True

    def list_delivery_outcomes(
        self, *, workspace_id: str, approval_ids: list[str]
    ) -> dict[str, str]:
        """Batch-read and validate durable delivery evidence for one workspace."""
        approval_ids = _validate_delivery_evidence_query(
            workspace_id=workspace_id, approval_ids=approval_ids
        )
        outcomes = {approval_id: "NOT_CONFIRMED" for approval_id in approval_ids}
        if not approval_ids:
            return outcomes
        values_sql = ",".join("(?)" for _ in approval_ids)
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "WITH requested(approval_id) AS (VALUES "
                    + values_sql
                    + ") SELECT requested.approval_id,claim.approval_id,claim.workspace_id,"
                    "claim.source_agent_id,claim.source_invocation_id,claim.claimed_at,"
                    "failure.approval_id,failure.workspace_id,failure.source_agent_id,"
                    "failure.source_invocation_id,failure.reason_code,failure.reported_at,"
                    "approval.entity_id,"
                    "json_extract(approval.payload,'$.workspace_id'),approval.payload "
                    "FROM requested "
                    "LEFT JOIN approval_delivery_claims AS claim "
                    "ON claim.approval_id=requested.approval_id "
                    "LEFT JOIN approval_delivery_failures AS failure "
                    "ON failure.approval_id=requested.approval_id "
                    "LEFT JOIN kv_store AS approval ON approval.entity_type='approval_request' "
                    "AND approval.entity_id=requested.approval_id",
                    approval_ids,
                ).fetchall()
        except sqlite3.Error as error:
            raise ApprovalDeliveryEvidenceUnavailable(
                "Delivery evidence storage is unavailable"
            ) from error
        if len(rows) != len(approval_ids):
            raise ApprovalDeliveryEvidenceIntegrityError(
                "Delivery evidence batch returned an incomplete result"
            )
        seen: set[str] = set()
        for row in rows:
            requested_id = row[0]
            if requested_id not in outcomes or requested_id in seen:
                raise ApprovalDeliveryEvidenceIntegrityError(
                    "Delivery evidence batch returned an unexpected identity"
                )
            seen.add(requested_id)
            outcomes[requested_id] = _validate_delivery_evidence_row(
                requested_id=requested_id,
                workspace_id=workspace_id,
                claim_id=row[1],
                claim_workspace_id=row[2],
                claim_agent_id=row[3],
                claim_invocation_id=row[4],
                claimed_at=row[5],
                failure_id=row[6],
                failure_workspace_id=row[7],
                failure_agent_id=row[8],
                failure_invocation_id=row[9],
                failure_reason_code=row[10],
                reported_at=row[11],
                approval_id=row[12],
                approval_workspace_id=row[13],
                approval_payload=row[14],
                postgres=False,
            )
        return outcomes

    def list_for_workspace(self, workspace_id: str | None = None) -> list[ApprovalRequest]:
        query = (
            "SELECT approval.entity_id, approval.payload, project.payload FROM kv_store AS approval "
            "LEFT JOIN kv_store AS project ON project.entity_type='project' "
            "AND project.entity_id=json_extract(approval.payload, '$.project_id') "
            "WHERE approval.entity_type = ?"
        )
        with self._connect() as conn:
            rows = conn.execute(query, ("approval_request",)).fetchall()
        requests: list[ApprovalRequest] = []
        for entity_id, approval_payload, project_payload in rows:
            request = ApprovalRequest.model_validate_json(approval_payload)
            if project_payload is not None:
                project = Project.model_validate_json(project_payload)
                if entity_id != request.id or project.id != request.project_id:
                    continue
                if request.workspace_id is not None and request.workspace_id != project.workspace_id:
                    continue
                effective_workspace_id = request.workspace_id or project.workspace_id
            else:
                # Approval scope is derived from its parent project; an
                # orphaned approval cannot be listed, even for unscoped local
                # callers.
                continue
            if workspace_id is not None and effective_workspace_id != workspace_id:
                continue
            requests.append(request)
        return sorted(requests, key=lambda request: (request.created_at, request.id), reverse=True)

    def list_due_pending(
        self,
        *,
        now: datetime,
        legacy_ttl_seconds: int,
        limit: int,
        workspace_id: str | None = None,
    ) -> list[ApprovalRequest]:
        if (
            now.utcoffset() is None
            or legacy_ttl_seconds <= 0
            or not 1 <= limit <= 10_000
        ):
            raise ValueError("Due-approval query bounds are invalid")
        scope_sql = ""
        query_params: list[object] = [
            ApprovalStatus.REQUESTED.value,
            ApprovalStatus.PENDING_REVIEW.value,
            now.isoformat(),
            legacy_ttl_seconds,
            now.isoformat(),
        ]
        if workspace_id is not None:
            scope_sql = (
                " AND COALESCE(json_extract(approval.payload,'$.workspace_id'),"
                "json_extract(project.payload,'$.workspace_id')) = ?"
            )
            query_params.append(workspace_id)
        query_params.extend((legacy_ttl_seconds, limit))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT approval.entity_id,approval.payload,project.payload "
                "FROM kv_store AS approval JOIN kv_store AS project "
                "ON project.entity_type='project' "
                "AND project.entity_id=json_extract(approval.payload,'$.project_id') "
                "WHERE approval.entity_type='approval_request' "
                "AND json_extract(approval.payload,'$.status') IN (?,?) "
                "AND ((json_extract(approval.payload,'$.expires_at') IS NOT NULL "
                "AND datetime(json_extract(approval.payload,'$.expires_at')) <= datetime(?)) "
                "OR (json_extract(approval.payload,'$.expires_at') IS NULL "
                "AND datetime(json_extract(approval.payload,'$.created_at'), '+' || ? || ' seconds') <= datetime(?))) "
                + scope_sql
                + " ORDER BY COALESCE(datetime(json_extract(approval.payload,'$.expires_at')), "
                "datetime(json_extract(approval.payload,'$.created_at'), '+' || ? || ' seconds')), approval.entity_id LIMIT ?",
                query_params,
            ).fetchall()
        due: list[ApprovalRequest] = []
        for entity_id, approval_payload, project_payload in rows:
            approval = ApprovalRequest.model_validate_json(approval_payload)
            project = Project.model_validate_json(project_payload)
            if (
                approval.id != entity_id
                or approval.project_id != project.id
                or approval.workspace_id not in {None, project.workspace_id}
                or (
                    workspace_id is not None
                    and (approval.workspace_id or project.workspace_id) != workspace_id
                )
            ):
                continue
            due.append(approval)
        return due

    def latest_decision_audit(
        self,
        *,
        approval_id: str,
        workspace_id: str | None,
        new_status: ApprovalStatus,
    ) -> AuditEvent | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT audit.entity_id,audit.payload,approval.entity_id,approval.payload,"
                "project.entity_id,project.payload FROM kv_store AS audit "
                "JOIN kv_store AS approval ON approval.entity_type='approval_request' "
                "AND approval.entity_id=json_extract(audit.payload,'$.entity_id') "
                "JOIN kv_store AS project ON project.entity_type='project' "
                "AND project.entity_id=json_extract(approval.payload,'$.project_id') "
                "WHERE audit.entity_type='audit_event' "
                "AND json_extract(audit.payload,'$.event_type')='approval.decision' "
                "AND json_extract(audit.payload,'$.entity_id')=? "
                "AND json_extract(audit.payload,'$.details.new_status')=? "
                "AND COALESCE(json_extract(approval.payload,'$.workspace_id'),"
                "json_extract(project.payload,'$.workspace_id')) IS ? "
                "AND (json_extract(audit.payload,'$.workspace_id') IS ? "
                "OR json_extract(audit.payload,'$.workspace_id') IS NULL) "
                "ORDER BY json_extract(audit.payload,'$.created_at') DESC,audit.entity_id DESC LIMIT 1",
                (approval_id, new_status.value, workspace_id, workspace_id),
            ).fetchone()
        if row is None:
            return None
        try:
            event = AuditEvent.model_validate_json(row[1])
            approval = ApprovalRequest.model_validate_json(row[3])
            project = Project.model_validate_json(row[5])
        except Exception as error:
            raise ApprovalDecisionConflict("Decision audit or approval scope payload is invalid") from error
        if (
            event.id != row[0]
            or approval.id != row[2]
            or project.id != row[4]
            or approval.project_id != project.id
            or (approval.workspace_id is not None and approval.workspace_id != project.workspace_id)
            or event.entity_id != approval_id
            or event.event_type != "approval.decision"
            or event.details.get("new_status") != new_status
            or (approval.workspace_id or project.workspace_id) != workspace_id
            or event.workspace_id not in {None, workspace_id}
        ):
            raise ApprovalDecisionConflict("Decision audit identity or scope is inconsistent")
        return event

    def create(
        self,
        *,
        approval: ApprovalRequest,
        audit_event: AuditEvent,
        notification: Notification,
        invocation_lease_ttl_seconds: int | None = None,
    ) -> None:
        _validate_approval_creation(
            approval, audit_event, notification, require_workspace=False
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            project_row = conn.execute(
                "SELECT payload FROM kv_store WHERE entity_type=? AND entity_id=?",
                ("project", approval.project_id),
            ).fetchone()
            if not project_row:
                raise ApprovalDecisionConflict("Approval project is missing.")
            project = Project.model_validate_json(project_row[0])
            if project.id != approval.project_id or project.workspace_id != approval.workspace_id:
                raise ApprovalDecisionConflict("Approval project ownership does not match its workspace.")
            if approval.work_order_id:
                work_order_row = conn.execute(
                    "SELECT payload FROM kv_store WHERE entity_type=? AND entity_id=?",
                    ("work_order", approval.work_order_id),
                ).fetchone()
                if not work_order_row:
                    raise ApprovalDecisionConflict("Approval work order is missing.")
                work_order = WorkOrder.model_validate_json(work_order_row[0])
                if work_order.project_id != approval.project_id or work_order.workspace_id != approval.workspace_id:
                    raise ApprovalDecisionConflict("Approval work order ownership does not match.")
            if approval.source_agent_id:
                agent_row = conn.execute(
                    "SELECT payload FROM kv_store WHERE entity_type=? AND entity_id=?",
                    ("external_agent", approval.source_agent_id),
                ).fetchone()
                if not agent_row:
                    raise ApprovalDecisionConflict("Approval source agent is missing.")
                agent = ExternalAgent.model_validate_json(agent_row[0])
                if agent.workspace_id != approval.workspace_id:
                    raise ApprovalDecisionConflict("Approval source agent belongs to another workspace.")
            if notification.user_id:
                user_row = conn.execute(
                    "SELECT payload FROM kv_store WHERE entity_type=? AND entity_id=?",
                    ("user", notification.user_id),
                ).fetchone()
                if not user_row or User.model_validate_json(user_row[0]).workspace_id != approval.workspace_id:
                    raise ApprovalDecisionConflict("Approval notification user belongs to another workspace.")
            for entity_type, entity_id in (
                ("approval_request", approval.id),
                ("audit_event", audit_event.id),
                ("notification", notification.id),
            ):
                if conn.execute(
                    "SELECT 1 FROM kv_store WHERE entity_type=? AND entity_id=?",
                    (entity_type, entity_id),
                ).fetchone():
                    raise ApprovalDecisionConflict(f"{entity_type} identifier already exists.")
            conn.executemany(
                "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
                (
                    ("approval_request", approval.id, approval.model_dump_json()),
                    ("audit_event", audit_event.id, audit_event.model_dump_json()),
                    ("notification", notification.id, notification.model_dump_json()),
                ),
            )
            if (
                invocation_lease_ttl_seconds is not None
                and approval.source_agent_id
                and approval.source_invocation_id
                and approval.status == ApprovalStatus.PENDING_REVIEW
            ):
                if not approval.workspace_id:
                    raise ApprovalDecisionConflict(
                        "Connector invocation leases require a workspace-scoped approval"
                    )
                persisted_identity = conn.execute(
                    "SELECT entity_id,json_extract(payload,'$.workspace_id'), "
                    "json_extract(payload,'$.source_agent_id'), "
                    "json_extract(payload,'$.source_invocation_id') "
                    "FROM kv_store WHERE entity_type='approval_request' AND entity_id=?",
                    (approval.id,),
                ).fetchone()
                expected_identity = (
                    approval.id,
                    approval.workspace_id,
                    approval.source_agent_id,
                    approval.source_invocation_id,
                )
                if persisted_identity != expected_identity:
                    raise ApprovalDecisionConflict(
                        "Persisted approval scope does not match its invocation lease"
                    )
                lease_expires_at = _invocation_lease_deadline(
                    approval=approval,
                    heartbeat_at=approval.created_at,
                    ttl_seconds=invocation_lease_ttl_seconds,
                )
                lease_values = (
                    approval.id,
                    approval.workspace_id,
                    approval.source_agent_id,
                    approval.source_invocation_id,
                    approval.created_at.isoformat(),
                    lease_expires_at.isoformat(),
                )
                try:
                    conn.execute(
                        "INSERT INTO approval_invocation_leases "
                        "(approval_id,workspace_id,source_agent_id,source_invocation_id,heartbeat_at,expires_at) "
                        "VALUES(?,?,?,?,?,?)",
                        lease_values,
                    )
                except sqlite3.IntegrityError as exc:
                    raise ApprovalDecisionConflict(
                        "SQLite rejected the scoped invocation lease"
                    ) from exc
            conn.commit()

    def create_for_agent_task(
        self,
        *,
        approval: ApprovalRequest,
        audit_event: AuditEvent,
        task: AgentTask,
        task_event: AgentTaskEvent,
        task_audit_event: AuditEvent,
        notification: Notification,
    ) -> None:
        _validate_approval_creation(
            approval, audit_event, notification, require_workspace=False,
            notification_entity_type="agent_task", notification_entity_id=task.id,
        )
        if (
            task.id != task_event.task_id
            or task.approval_request_id != approval.id
            or task.project_id != approval.project_id
            or task.workspace_id != approval.workspace_id
            or task.status != AgentTaskStatus.WAITING_FOR_APPROVAL
            or task_event.status != AgentTaskStatus.WAITING_FOR_APPROVAL
            or task_event.event_type.value != "APPROVAL_CREATED"
            or task_audit_event.entity_id != task.id
            or task_audit_event.event_type != "agent_task.approval_created"
            or task_audit_event.workspace_id != approval.workspace_id
        ):
            raise ValueError("AgentTask approval records do not match the scoped transition.")

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task_row = conn.execute(
                "SELECT payload FROM kv_store WHERE entity_type=? AND entity_id=?",
                ("agent_task", task.id),
            ).fetchone()
            if task_row is None:
                raise ApprovalDecisionConflict("AgentTask is missing from durable storage.")
            durable_task = AgentTask.model_validate_json(task_row[0])
            if (
                durable_task.approval_request_id is not None
                or durable_task.project_id != approval.project_id
                or durable_task.workspace_id != approval.workspace_id
            ):
                raise ApprovalDecisionConflict("AgentTask changed or has a different owner.")
            project_row = conn.execute(
                "SELECT payload FROM kv_store WHERE entity_type=? AND entity_id=?",
                ("project", approval.project_id),
            ).fetchone()
            if project_row is None:
                raise ApprovalDecisionConflict("Approval project is missing.")
            project = Project.model_validate_json(project_row[0])
            if project.workspace_id != approval.workspace_id:
                raise ApprovalDecisionConflict("Approval project ownership does not match its workspace.")
            for entity_type, entity_id in (
                ("approval_request", approval.id),
                ("audit_event", audit_event.id),
                ("audit_event", task_audit_event.id),
                ("notification", notification.id),
                ("agent_task_event", task_event.id),
            ):
                if conn.execute(
                    "SELECT 1 FROM kv_store WHERE entity_type=? AND entity_id=?",
                    (entity_type, entity_id),
                ).fetchone():
                    raise ApprovalDecisionConflict(f"{entity_type} identifier already exists.")
            conn.executemany(
                "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
                (
                    ("approval_request", approval.id, approval.model_dump_json()),
                    ("audit_event", audit_event.id, audit_event.model_dump_json()),
                    ("audit_event", task_audit_event.id, task_audit_event.model_dump_json()),
                    ("notification", notification.id, notification.model_dump_json()),
                    ("agent_task_event", task_event.id, task_event.model_dump_json()),
                ),
            )
            task_update = conn.execute(
                "UPDATE kv_store SET payload=? WHERE entity_type=? AND entity_id=? AND payload=?",
                (task.model_dump_json(), "agent_task", task.id, task_row[0]),
            )
            if task_update.rowcount != 1:
                raise ApprovalDecisionConflict("AgentTask changed during approval creation.")
            conn.commit()

    def backfill_expiry(
        self,
        *,
        approval_id: str,
        expected_approval: ApprovalRequest,
        updated_approval: ApprovalRequest,
        audit_event: AuditEvent,
    ) -> tuple[ApprovalRequest, bool]:
        _validate_expiry_backfill(
            approval_id, expected_approval, updated_approval, audit_event
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload FROM kv_store WHERE entity_type=? AND entity_id=?",
                ("approval_request", approval_id),
            ).fetchone()
            if row is None:
                raise ApprovalRecordMissing(approval_id)
            durable = ApprovalRequest.model_validate_json(row[0])
            if durable.expires_at is not None:
                return durable, False
            if durable != expected_approval:
                raise ApprovalDecisionConflict("Approval changed before expiry could be backfilled.")
            if conn.execute(
                "SELECT 1 FROM kv_store WHERE entity_type=? AND entity_id=?",
                ("audit_event", audit_event.id),
            ).fetchone():
                raise ApprovalDecisionConflict("Expiry backfill audit identifier already exists.")
            result = conn.execute(
                "UPDATE kv_store SET payload=? "
                "WHERE entity_type=? AND entity_id=? AND payload=?",
                (
                    updated_approval.model_dump_json(),
                    "approval_request",
                    approval_id,
                    row[0],
                ),
            )
            if result.rowcount != 1:
                raise ApprovalDecisionConflict("Approval changed during expiry backfill.")
            conn.execute(
                "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
                ("audit_event", audit_event.id, audit_event.model_dump_json()),
            )
            conn.commit()
        return updated_approval, True

    def expire_pending(
        self,
        *,
        approval_id: str,
        expected_approval: ApprovalRequest,
        expired_approval: ApprovalRequest,
        audit_event: AuditEvent,
    ) -> ApprovalExpirationCommit:
        _validate_expiration(approval_id, expected_approval, expired_approval, audit_event)

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload FROM kv_store WHERE entity_type = ? AND entity_id = ?",
                ("approval_request", approval_id),
            ).fetchone()
            if row is None:
                raise ApprovalRecordMissing(approval_id)
            durable_approval = ApprovalRequest.model_validate_json(row[0])
            if durable_approval != expected_approval:
                raise ApprovalDecisionConflict(
                    "Approval changed before expiration could commit."
                )
            linked_rows = conn.execute(
                "SELECT entity_id,payload FROM kv_store WHERE entity_type='agent_task' "
                "AND json_extract(payload,'$.approval_request_id')=? ORDER BY entity_id",
                (approval_id,),
            ).fetchall()
            if len(linked_rows) > 1:
                raise ApprovalDecisionConflict(
                    "Approval is linked to multiple AgentTasks; refusing ambiguous expiration."
                )
            linked_task = task_event = task_audit_event = task_notification = None
            if linked_rows:
                task_id, task_payload = linked_rows[0]
                durable_task = AgentTask.model_validate_json(task_payload)
                if durable_task.id != task_id:
                    raise ApprovalDecisionConflict("Linked AgentTask storage identity is inconsistent.")
                project_row = conn.execute(
                    "SELECT payload FROM kv_store WHERE entity_type='project' AND entity_id=?",
                    (expired_approval.project_id,),
                ).fetchone()
                if project_row is None:
                    raise ApprovalDecisionConflict("Approval project is missing.")
                project = Project.model_validate_json(project_row[0])
                if (
                    project.id != expected_approval.project_id
                    or project.workspace_id != expected_approval.workspace_id
                ):
                    raise ApprovalDecisionConflict("Approval project ownership does not match its workspace.")
                linked_task, task_event, task_audit_event, task_notification = _task_expiration_records(
                    task=durable_task, approval=expired_approval, actor=audit_event.actor
                )
                task_update = conn.execute(
                    "UPDATE kv_store SET payload=? WHERE entity_type='agent_task' "
                    "AND entity_id=? AND payload=?",
                    (linked_task.model_dump_json(), task_id, task_payload),
                )
                if task_update.rowcount != 1:
                    raise ApprovalDecisionConflict("AgentTask changed during expiration commit.")
            update = conn.execute(
                "UPDATE kv_store SET payload = ? "
                "WHERE entity_type = ? AND entity_id = ? AND payload = ?",
                (
                    expired_approval.model_dump_json(),
                    "approval_request",
                    approval_id,
                    row[0],
                ),
            )
            if update.rowcount != 1:
                raise ApprovalDecisionConflict("Approval changed during expiration commit.")
            inserts = [("audit_event", audit_event.id, audit_event.model_dump_json())]
            if task_event is not None and task_audit_event is not None and task_notification is not None:
                inserts.extend(
                    [
                        ("agent_task_event", task_event.id, task_event.model_dump_json()),
                        ("audit_event", task_audit_event.id, task_audit_event.model_dump_json()),
                        ("notification", task_notification.id, task_notification.model_dump_json()),
                    ]
                )
            for entity_type, entity_id, payload in inserts:
                if conn.execute(
                    "SELECT 1 FROM kv_store WHERE entity_type=? AND entity_id=?",
                    (entity_type, entity_id),
                ).fetchone():
                    raise ApprovalDecisionConflict(f"{entity_type} identifier already exists.")
            conn.executemany(
                "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)", inserts
            )
            conn.commit()
        return ApprovalExpirationCommit(
            approval=expired_approval,
            linked_task=linked_task,
            linked_task_event=task_event,
            linked_task_audit_event=task_audit_event,
            linked_task_notification=task_notification,
        )

    def commit_decision(
        self,
        *,
        approval_id: str,
        expected_status: ApprovalStatus,
        expected_approval: ApprovalRequest,
        updated_approval: ApprovalRequest,
        audit_event: AuditEvent,
        decision_notification: Notification,
        run_repository: object | None = None,
    ) -> ApprovalDecisionCommit:
        if (
            expected_approval.id != approval_id
            or updated_approval.id != approval_id
            or audit_event.entity_id != approval_id
            or decision_notification.entity_id != approval_id
        ):
            raise ValueError("Approval and audit identifiers must match the decision target")
        if decision_notification.entity_type != "approval_request":
            raise ValueError("Approval decision notification must reference the approval request")
        if expected_approval.status != expected_status:
            raise ValueError("Expected approval snapshot status must match expected_status")
        if audit_event.event_type != "approval.decision":
            raise ValueError("Approval decision must use an approval.decision audit event")
        if (
            audit_event.details.get("previous_status") != expected_status
            or audit_event.details.get("new_status") != updated_approval.status
        ):
            raise ValueError("Approval audit statuses must match the committed transition")
        if audit_event.details.get("approved_payload_hash") != updated_approval.approved_payload_hash:
            raise ValueError("Approval audit payload hash must match the committed approval")
        if updated_approval.status == ApprovalStatus.APPROVED and not updated_approval.approved_payload_hash:
            raise ValueError("Approved approval must include a frozen payload hash")
        if (
            updated_approval.approved_payload_hash is not None
            and updated_approval.approved_payload_hash
            != compute_approval_payload_hash(updated_approval)
        ):
            raise ValueError("Approval payload does not match its frozen payload hash")
        if not can_transition(expected_status, updated_approval.status):
            raise ValueError(
                f"Invalid approval transition: {expected_status} -> {updated_approval.status}"
            )

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload FROM kv_store WHERE entity_type = ? AND entity_id = ?",
                ("approval_request", approval_id),
            ).fetchone()
            if row is None:
                raise ApprovalRecordMissing(approval_id)

            durable_approval = ApprovalRequest.model_validate_json(row[0])
            if durable_approval.status != expected_status:
                raise ApprovalDecisionConflict(
                    f"Approval is {durable_approval.status}; expected {expected_status}."
                )
            if durable_approval != expected_approval:
                raise ApprovalDecisionConflict("Approval changed since the decision snapshot was read.")
            decision_at = now_utc()
            if durable_approval.expires_at is not None and durable_approval.expires_at <= decision_at:
                raise ApprovalDecisionConflict("Approval deadline elapsed before the decision committed.")
            if durable_approval.source_invocation_id is not None:
                invocation_lease = conn.execute(
                    "SELECT approval_id,workspace_id,source_agent_id,source_invocation_id,heartbeat_at,expires_at "
                    "FROM approval_invocation_leases WHERE approval_id=?",
                    (approval_id,),
                ).fetchone()
                _validate_live_decision_invocation(
                    approval=durable_approval,
                    lease_row=invocation_lease,
                    now=decision_at,
                    postgres=False,
                )
            if updated_approval.workspace_id != durable_approval.workspace_id:
                raise ApprovalDecisionConflict(
                    "Approval workspace cannot change as part of a decision."
                )
            approval_workspace_id = durable_approval.workspace_id
            project_row = conn.execute(
                "SELECT payload FROM kv_store WHERE entity_type = ? AND entity_id = ?",
                ("project", durable_approval.project_id),
            ).fetchone()
            if project_row is None:
                raise ApprovalDecisionConflict("Approval project is missing.")
            project = Project.model_validate_json(project_row[0])
            if project.id != durable_approval.project_id:
                raise ApprovalDecisionConflict("Approval project identity does not match its record key.")
            project_workspace_id = project.workspace_id
            if project_workspace_id is not None:
                workspace_row = conn.execute(
                    "SELECT payload FROM kv_store WHERE entity_type = ? AND entity_id = ?",
                    ("workspace", project_workspace_id),
                ).fetchone()
                if workspace_row is None:
                    raise ApprovalDecisionConflict("Approval project workspace is missing.")
                workspace = Workspace.model_validate_json(workspace_row[0])
                if workspace.id != project_workspace_id:
                    raise ApprovalDecisionConflict(
                        "Approval project workspace identity does not match its record key."
                    )
            if (
                approval_workspace_id is not None
                and approval_workspace_id != project_workspace_id
            ):
                raise ApprovalDecisionConflict("Approval workspace does not match its project.")
            approval_workspace_id = approval_workspace_id or project_workspace_id
            if decision_notification.workspace_id != approval_workspace_id:
                raise ValueError("Approval notification workspace must match the durable approval scope")

            update = conn.execute(
                "UPDATE kv_store SET payload = ? "
                "WHERE entity_type = ? AND entity_id = ? AND payload = ?",
                (
                    updated_approval.model_dump_json(),
                    "approval_request",
                    approval_id,
                    row[0],
                ),
            )
            if update.rowcount != 1:
                raise ApprovalDecisionConflict("Approval changed during decision commit.")

            conn.execute(
                "INSERT INTO kv_store(entity_type, entity_id, payload) VALUES (?, ?, ?)",
                ("audit_event", audit_event.id, audit_event.model_dump_json()),
            )

            linked_rows = conn.execute(
                "SELECT entity_id, payload FROM kv_store WHERE entity_type = ? ORDER BY entity_id",
                ("agent_task",),
            ).fetchall()
            linked_tasks = [
                (entity_id, AgentTask.model_validate_json(payload), payload)
                for entity_id, payload in linked_rows
                if AgentTask.model_validate_json(payload).approval_request_id == approval_id
            ]
            if len(linked_tasks) > 1:
                raise ApprovalDecisionConflict(
                    "Approval is linked to multiple AgentTasks; refusing ambiguous propagation."
                )

            updated_task: AgentTask | None = None
            linked_task_event: AgentTaskEvent | None = None
            linked_task_audit_event: AuditEvent | None = None
            linked_task_notification: Notification | None = None
            if linked_tasks:
                entity_id, durable_task, original_task_payload = linked_tasks[0]
                if durable_task.id != entity_id or durable_task.project_id != durable_approval.project_id:
                    raise ApprovalDecisionConflict(
                        "Linked AgentTask identity or project does not match the approval."
                    )
                if durable_task.status != AgentTaskStatus.WAITING_FOR_APPROVAL:
                    raise ApprovalDecisionConflict(
                        "Linked AgentTask is no longer waiting for this approval decision."
                    )
                task_workspace_id = durable_task.workspace_id or project_workspace_id
                if task_workspace_id != approval_workspace_id:
                    raise ApprovalDecisionConflict(
                        "Linked AgentTask workspace does not match the approval."
                    )
                task_status = {
                    ApprovalStatus.APPROVED: AgentTaskStatus.APPROVED,
                    ApprovalStatus.DENIED: AgentTaskStatus.DENIED,
                    ApprovalStatus.REVISION_REQUESTED: AgentTaskStatus.PLANNING,
                }.get(updated_approval.status)
                if task_status is not None:
                    updated_task = durable_task.model_copy(
                        update={"status": task_status, "updated_at": updated_approval.updated_at}
                    )
                    task_update = conn.execute(
                        "UPDATE kv_store SET payload = ? "
                        "WHERE entity_type = ? AND entity_id = ? AND payload = ?",
                        (
                            updated_task.model_dump_json(),
                            "agent_task",
                            entity_id,
                            original_task_payload,
                        ),
                    )
                    if task_update.rowcount != 1:
                        raise ApprovalDecisionConflict("Linked AgentTask changed during decision commit.")
                    (
                        linked_task_event,
                        linked_task_audit_event,
                        linked_task_notification,
                    ) = _task_decision_records(
                        task=updated_task,
                        approval=updated_approval,
                        previous_status=expected_status,
                        actor=audit_event.actor,
                    )
                    conn.executemany(
                        "INSERT INTO kv_store(entity_type, entity_id, payload) VALUES (?, ?, ?)",
                        (
                            (
                                "agent_task_event",
                                linked_task_event.id,
                                linked_task_event.model_dump_json(),
                            ),
                            (
                                "audit_event",
                                linked_task_audit_event.id,
                                linked_task_audit_event.model_dump_json(),
                            ),
                            (
                                "notification",
                                linked_task_notification.id,
                                linked_task_notification.model_dump_json(),
                            ),
                        ),
                    )
            conn.execute(
                "INSERT INTO kv_store(entity_type, entity_id, payload) VALUES (?, ?, ?)",
                ("notification", decision_notification.id, decision_notification.model_dump_json()),
            )
            conn.commit()
            return ApprovalDecisionCommit(
                linked_task=updated_task,
                notification=decision_notification,
                linked_task_event=linked_task_event,
                linked_task_audit_event=linked_task_audit_event,
                linked_task_notification=linked_task_notification,
            )


class PostgresApprovalDecisionRepository:
    """Row-locked, multi-process decision adapter for the PostgreSQL schema.

    This adapter owns only the approval-decision transaction slice. It must not
    be used as a complete production store until all API reads and writes use
    PostgreSQL-backed repositories.
    """

    def __init__(self, connect: Callable[[], psycopg.Connection]) -> None:
        self._connect = connect

    @staticmethod
    def _approval_from_row(row: tuple[object, ...] | None, approval_id: str) -> tuple[ApprovalRequest, int]:
        if row is None:
            raise ApprovalRecordMissing(approval_id)
        approval = ApprovalRequest.model_validate(row[0])
        version = int(row[1])
        if approval.id != approval_id:
            raise ApprovalDecisionConflict("Approval identity does not match its database key.")
        return approval, version

    @staticmethod
    def _validate_expiration(
        approval_id: str,
        expected_approval: ApprovalRequest,
        expired_approval: ApprovalRequest,
        audit_event: AuditEvent,
    ) -> None:
        _validate_expiration(approval_id, expected_approval, expired_approval, audit_event)

    def get(self, approval_id: str) -> ApprovalRequest | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT approval.payload,approval.approval_id,approval.workspace_id, "
                "project.project_id,project.workspace_id "
                "FROM approval_requests AS approval "
                "JOIN projects AS project ON project.project_id=approval.project_id "
                "WHERE approval.approval_id = %s",
                (approval_id,),
            ).fetchone()
        if row is None:
            return None
        approval = ApprovalRequest.model_validate(row[0])
        if (
            approval.id != approval_id
            or approval.id != row[1]
            or approval.project_id != row[3]
            or approval.workspace_id != row[2]
            or approval.workspace_id != row[4]
        ):
            raise ApprovalDecisionConflict("Approval identity does not match its database key.")
        return approval

    def heartbeat_invocation(
        self,
        *,
        approval_id: str,
        workspace_id: str,
        source_agent_id: str,
        source_invocation_id: str,
        presented_token_hash: str,
        occurred_at: datetime,
        ttl_seconds: int,
    ) -> ApprovalInvocationLease:
        if occurred_at.tzinfo is None or not workspace_id:
            raise ValueError("Invocation heartbeat requires an aware timestamp and workspace")
        if workspace_id is None:
            raise ApprovalInvocationLeaseRejected("Approval workspace is required")
        with self._connect() as conn:
            _validate_postgres_delivery_credential(
                conn,
                source_agent_id=source_agent_id,
                workspace_id=workspace_id,
                presented_token_hash=presented_token_hash,
                occurred_at=now_utc(),
            )
            row = conn.execute(
                "SELECT payload,approval_id,workspace_id,source_agent_id,source_invocation_id,status,expires_at "
                "FROM approval_requests WHERE approval_id=%s FOR UPDATE",
                (approval_id,),
            ).fetchone()
            if row is None:
                raise ApprovalInvocationLeaseRejected("Approval invocation is unavailable")
            try:
                approval = ApprovalRequest.model_validate(row[0])
            except Exception as exc:
                raise ApprovalInvocationLeaseRejected("Approval invocation record is invalid") from exc
            if (
                approval.id != approval_id
                or approval.id != row[1]
                or approval.workspace_id != workspace_id
                or approval.workspace_id != row[2]
                or approval.source_agent_id != source_agent_id
                or approval.source_agent_id != row[3]
                or approval.source_invocation_id != source_invocation_id
                or approval.source_invocation_id != row[4]
                or approval.status not in {ApprovalStatus.PENDING_REVIEW, ApprovalStatus.APPROVED}
                or approval.expires_at is None
                or approval.expires_at <= now_utc()
                or row[5] != approval.status.value
                or row[6] != approval.expires_at
            ):
                raise ApprovalInvocationLeaseRejected("Approval invocation is no longer live")
            claim = conn.execute(
                "SELECT 1 FROM approval_delivery_claims WHERE approval_id=%s FOR KEY SHARE",
                (approval_id,),
            ).fetchone()
            if claim is not None:
                raise ApprovalInvocationLeaseRejected("Approval invocation was already consumed")
            lease_row = conn.execute(
                "SELECT approval_id,workspace_id,source_agent_id,source_invocation_id,heartbeat_at,expires_at "
                "FROM approval_invocation_leases WHERE approval_id=%s FOR UPDATE",
                (approval_id,),
            ).fetchone()
            heartbeat_at = now_utc()
            _validate_postgres_delivery_credential(
                conn,
                source_agent_id=source_agent_id,
                workspace_id=workspace_id,
                presented_token_hash=presented_token_hash,
                occurred_at=heartbeat_at,
            )
            if approval.expires_at is None or approval.expires_at <= heartbeat_at:
                raise ApprovalInvocationLeaseRejected("Approval invocation is no longer live")
            if lease_row is None:
                raise ApprovalInvocationLeaseRejected("Invocation lease is missing")
            current = _postgres_lease_from_row(lease_row)
            if (
                current.approval_id != approval_id
                or lease_row[1] != workspace_id
                or current.source_agent_id != source_agent_id
                or current.source_invocation_id != source_invocation_id
            ):
                raise ApprovalInvocationLeaseRejected("Invocation lease identity changed")
            if current.expires_at <= heartbeat_at:
                raise ApprovalInvocationLeaseRejected("Invocation lease expired and cannot be renewed")
            expires_at = _invocation_lease_deadline(
                approval=approval, heartbeat_at=heartbeat_at, ttl_seconds=ttl_seconds
            )
            updated = conn.execute(
                "UPDATE approval_invocation_leases SET heartbeat_at=%s,expires_at=%s "
                "WHERE approval_id=%s AND workspace_id=%s AND source_agent_id=%s "
                "AND source_invocation_id=%s AND expires_at>%s "
                "RETURNING approval_id,workspace_id,source_agent_id,source_invocation_id,heartbeat_at,expires_at",
                (
                    heartbeat_at,
                    expires_at,
                    approval_id,
                    workspace_id,
                    source_agent_id,
                    source_invocation_id,
                    heartbeat_at,
                ),
            ).fetchone()
            if updated is None:
                raise ApprovalInvocationLeaseRejected("Invocation lease expired and cannot be renewed")
            return _postgres_lease_from_row(updated)

    def get_by_invocation(
        self, *, workspace_id: str | None, source_agent_id: str, source_invocation_id: str
    ) -> ApprovalRequest | None:
        if not source_agent_id or not source_invocation_id:
            raise ValueError("Invocation lookup requires workspace, connector, and invocation identity")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload,approval_id,workspace_id,source_agent_id,source_invocation_id "
                "FROM approval_requests WHERE workspace_id IS NOT DISTINCT FROM %s AND source_agent_id=%s "
                "AND source_invocation_id=%s",
                (workspace_id, source_agent_id, source_invocation_id),
            ).fetchall()
        if len(rows) > 1:
            raise ApprovalDecisionConflict("Connector invocation maps to multiple approvals.")
        if not rows:
            return None
        row = rows[0]
        approval = ApprovalRequest.model_validate(row[0])
        if (
            approval.id != row[1]
            or approval.workspace_id != row[2]
            or approval.source_agent_id != row[3]
            or approval.source_invocation_id != row[4]
        ):
            raise ApprovalDecisionConflict("Invocation approval identity is inconsistent.")
        return approval

    def claim_approved_delivery(
        self,
        *,
        approval_id: str,
        workspace_id: str | None,
        source_agent_id: str,
        source_invocation_id: str,
        expected_payload_hash: str,
        presented_token_hash: str,
        audit_event: AuditEvent,
    ) -> bool:
        if (
            audit_event.event_type != "approval.delivery_claimed"
            or audit_event.entity_id != approval_id
            or audit_event.workspace_id != workspace_id
        ):
            raise ValueError("Delivery audit identity must match the approval scope")
        if workspace_id is None:
            raise ApprovalDecisionConflict("Approval workspace is required for PostgreSQL delivery.")
        with self._connect() as conn:
            _validate_postgres_delivery_credential(
                conn,
                source_agent_id=source_agent_id,
                workspace_id=workspace_id,
                presented_token_hash=presented_token_hash,
                occurred_at=now_utc(),
            )
            row = conn.execute(
                "SELECT payload,workspace_id,source_agent_id,source_invocation_id "
                "FROM approval_requests WHERE approval_id=%s FOR UPDATE",
                (approval_id,),
            ).fetchone()
            approval, _version = self._approval_from_row(
                None if row is None else (row[0], 0), approval_id
            )
            if (
                row[1] != workspace_id
                or row[2] != source_agent_id
                or row[3] != source_invocation_id
                or approval.workspace_id != workspace_id
                or approval.source_agent_id != source_agent_id
                or approval.source_invocation_id != source_invocation_id
                or approval.approved_payload_hash_version != 2
                or approval.approved_payload_hash != expected_payload_hash
                or expected_payload_hash != compute_approval_payload_hash(approval)
            ):
                raise ApprovalDecisionConflict("Approved invocation identity or payload did not match")
            if approval.status != ApprovalStatus.APPROVED:
                return False
            lease_row = conn.execute(
                "SELECT approval_id,workspace_id,source_agent_id,source_invocation_id,heartbeat_at,expires_at "
                "FROM approval_invocation_leases WHERE approval_id=%s FOR UPDATE",
                (approval_id,),
            ).fetchone()
            if lease_row is None:
                raise ApprovalInvocationLeaseRejected("Connector invocation is not live")
            lease = _postgres_lease_from_row(lease_row)
            claim_time = now_utc()
            _validate_postgres_delivery_credential(
                conn,
                source_agent_id=source_agent_id,
                workspace_id=workspace_id,
                presented_token_hash=presented_token_hash,
                occurred_at=claim_time,
            )
            claim_audit_event = audit_event.model_copy(update={"created_at": claim_time})
            if approval.expires_at is None or approval.expires_at <= claim_time:
                raise ApprovalInvocationLeaseRejected("Approval expired before delivery claim")
            if (
                lease.approval_id != approval_id
                or lease_row[1] != workspace_id
                or lease.source_agent_id != source_agent_id
                or lease.source_invocation_id != source_invocation_id
                or lease.expires_at <= claim_time
            ):
                raise ApprovalInvocationLeaseRejected("Connector invocation lease is expired or mismatched")
            claimed = conn.execute(
                "INSERT INTO approval_delivery_claims "
                "(approval_id,workspace_id,source_agent_id,source_invocation_id,claimed_at) "
                "VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING approval_id",
                (
                    approval_id,
                    workspace_id,
                    source_agent_id,
                    source_invocation_id,
                    claim_time,
                ),
            ).fetchone()
            if claimed is None:
                return False
            self._insert_audit(conn, approval, claim_audit_event)
            return True

    def record_approved_delivery_failure(
        self,
        *,
        approval_id: str,
        workspace_id: str,
        source_agent_id: str,
        source_invocation_id: str,
        expected_payload_hash: str,
        reason_code: str,
        audit_event: AuditEvent,
    ) -> bool:
        _validate_delivery_failure_report(
            approval_id=approval_id,
            workspace_id=workspace_id,
            source_agent_id=source_agent_id,
            source_invocation_id=source_invocation_id,
            expected_payload_hash=expected_payload_hash,
            reason_code=reason_code,
            audit_event=audit_event,
        )
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload,workspace_id,source_agent_id,source_invocation_id "
                "FROM approval_requests WHERE approval_id=%s FOR UPDATE",
                (approval_id,),
            ).fetchone()
            approval, _version = self._approval_from_row(
                None if row is None else (row[0], 0), approval_id
            )
            if (
                row[1] != workspace_id
                or row[2] != source_agent_id
                or row[3] != source_invocation_id
                or approval.workspace_id != workspace_id
                or approval.source_agent_id != source_agent_id
                or approval.source_invocation_id != source_invocation_id
                or approval.approved_payload_hash_version != 2
                or approval.approved_payload_hash != expected_payload_hash
                or expected_payload_hash != compute_approval_payload_hash(approval)
            ):
                raise ApprovalDecisionConflict("Delivery failure identity or payload did not match")
            claim = conn.execute(
                "SELECT workspace_id,source_agent_id,source_invocation_id "
                "FROM approval_delivery_claims WHERE approval_id=%s FOR KEY SHARE",
                (approval_id,),
            ).fetchone()
            if claim != (workspace_id, source_agent_id, source_invocation_id):
                raise ApprovalDecisionConflict("Delivery failure requires the matching durable claim")
            prior = conn.execute(
                "SELECT workspace_id,source_agent_id,source_invocation_id,reason_code "
                "FROM approval_delivery_failures WHERE approval_id=%s FOR UPDATE",
                (approval_id,),
            ).fetchone()
            expected = (workspace_id, source_agent_id, source_invocation_id, reason_code)
            if prior is not None:
                if prior != expected:
                    raise ApprovalDecisionConflict("A different delivery failure is already recorded")
                return False
            conn.execute(
                "INSERT INTO approval_delivery_failures "
                "(approval_id,workspace_id,source_agent_id,source_invocation_id,reason_code,reported_at) "
                "VALUES(%s,%s,%s,%s,%s,%s)",
                (
                    approval_id,
                    workspace_id,
                    source_agent_id,
                    source_invocation_id,
                    reason_code,
                    audit_event.created_at,
                ),
            )
            self._insert_audit(conn, approval, audit_event)
            return True

    def list_delivery_outcomes(
        self, *, workspace_id: str, approval_ids: list[str]
    ) -> dict[str, str]:
        """Batch-read delivery claims/failures from the workspace database."""
        approval_ids = _validate_delivery_evidence_query(
            workspace_id=workspace_id, approval_ids=approval_ids
        )
        outcomes = {approval_id: "NOT_CONFIRMED" for approval_id in approval_ids}
        if not approval_ids:
            return outcomes
        values_sql = ",".join("(%s)" for _ in approval_ids)
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "WITH requested(approval_id) AS (VALUES "
                    + values_sql
                    + ") SELECT requested.approval_id,claim.approval_id,claim.workspace_id,"
                    "claim.source_agent_id,claim.source_invocation_id,claim.claimed_at,"
                    "failure.approval_id,failure.workspace_id,failure.source_agent_id,"
                    "failure.source_invocation_id,failure.reason_code,failure.reported_at,"
                    "approval.approval_id,approval.workspace_id,approval.payload "
                    "FROM requested "
                    "LEFT JOIN approval_delivery_claims AS claim "
                    "ON claim.approval_id=requested.approval_id "
                    "LEFT JOIN approval_delivery_failures AS failure "
                    "ON failure.approval_id=requested.approval_id "
                    "LEFT JOIN approval_requests AS approval "
                    "ON approval.approval_id=requested.approval_id",
                    approval_ids,
                ).fetchall()
        except psycopg.Error as error:
            raise ApprovalDeliveryEvidenceUnavailable(
                "Delivery evidence storage is unavailable"
            ) from error
        if len(rows) != len(approval_ids):
            raise ApprovalDeliveryEvidenceIntegrityError(
                "Delivery evidence batch returned an incomplete result"
            )
        seen: set[str] = set()
        for row in rows:
            requested_id = row[0]
            if requested_id not in outcomes or requested_id in seen:
                raise ApprovalDeliveryEvidenceIntegrityError(
                    "Delivery evidence batch returned an unexpected identity"
                )
            seen.add(requested_id)
            outcomes[requested_id] = _validate_delivery_evidence_row(
                requested_id=requested_id,
                workspace_id=workspace_id,
                claim_id=row[1],
                claim_workspace_id=row[2],
                claim_agent_id=row[3],
                claim_invocation_id=row[4],
                claimed_at=row[5],
                failure_id=row[6],
                failure_workspace_id=row[7],
                failure_agent_id=row[8],
                failure_invocation_id=row[9],
                failure_reason_code=row[10],
                reported_at=row[11],
                approval_id=row[12],
                approval_workspace_id=row[13],
                approval_payload=row[14],
                postgres=True,
            )
        return outcomes

    def list_for_workspace(self, workspace_id: str | None = None) -> list[ApprovalRequest]:
        if workspace_id is None:
            query = (
                "SELECT approval.payload,approval.approval_id,approval.workspace_id, "
                "project.project_id,project.workspace_id "
                "FROM approval_requests AS approval "
                "JOIN projects AS project ON project.project_id=approval.project_id "
                "ORDER BY approval.created_at DESC, approval.approval_id DESC"
            )
            params: tuple[object, ...] = ()
        else:
            query = (
                "SELECT approval.payload,approval.approval_id,approval.workspace_id, "
                "project.project_id,project.workspace_id "
                "FROM approval_requests AS approval "
                "JOIN projects AS project ON project.project_id=approval.project_id "
                "WHERE approval.workspace_id=%s "
                "ORDER BY approval.created_at DESC, approval.approval_id DESC"
            )
            params = (workspace_id,)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        requests = []
        for row in rows:
            request = ApprovalRequest.model_validate(row[0])
            if (
                request.id != row[1]
                or request.project_id != row[3]
                or request.workspace_id != row[2]
                or request.workspace_id != row[4]
            ):
                raise ApprovalDecisionConflict("Approval query found mismatched project/workspace ownership.")
            requests.append(request)
        if any(request.workspace_id != workspace_id for request in requests) and workspace_id is not None:
            raise ApprovalDecisionConflict("Approval query returned a record outside its workspace scope.")
        return requests

    def list_due_pending(
        self,
        *,
        now: datetime,
        legacy_ttl_seconds: int,
        limit: int,
        workspace_id: str | None = None,
    ) -> list[ApprovalRequest]:
        if (
            now.utcoffset() is None
            or legacy_ttl_seconds <= 0
            or not 1 <= limit <= 10_000
        ):
            raise ValueError("Due-approval query bounds are invalid")
        statuses = (ApprovalStatus.REQUESTED.value, ApprovalStatus.PENDING_REVIEW.value)
        scope_sql = " AND workspace_id=%s" if workspace_id is not None else ""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload,approval_id,workspace_id,project_id FROM approval_requests "
                "WHERE status IN (%s,%s) AND (expires_at <= %s OR "
                "(expires_at IS NULL AND created_at + (%s * interval '1 second') <= %s))"
                + scope_sql
                + " ORDER BY COALESCE(expires_at, created_at + (%s * interval '1 second')), approval_id LIMIT %s",
                (
                    statuses[0], statuses[1], now, legacy_ttl_seconds, now,
                    *((workspace_id,) if workspace_id is not None else ()),
                    legacy_ttl_seconds, limit,
                ),
            ).fetchall()
        approvals: list[ApprovalRequest] = []
        for payload, approval_id, row_workspace_id, project_id in rows:
            approval = ApprovalRequest.model_validate(payload)
            if (
                approval.id != approval_id
                or approval.workspace_id != row_workspace_id
                or approval.project_id != project_id
                or (workspace_id is not None and approval.workspace_id != workspace_id)
            ):
                raise ApprovalDecisionConflict(
                    "Due-approval query found mismatched identity or workspace scope."
                )
            approvals.append(approval)
        return approvals

    def latest_decision_audit(
        self,
        *,
        approval_id: str,
        workspace_id: str | None,
        new_status: ApprovalStatus,
    ) -> AuditEvent | None:
        if not workspace_id:
            raise ValueError("PostgreSQL decision-audit reads require a workspace")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT event_id,workspace_id,event_type,entity_id,actor,created_at,details,payload "
                "FROM audit_events WHERE approval_id=%s AND workspace_id=%s "
                "AND event_type='approval.decision' AND details->>'new_status'=%s "
                "ORDER BY created_at DESC,event_id DESC LIMIT 1",
                (approval_id, workspace_id, new_status.value),
            ).fetchone()
        if row is None:
            return None
        try:
            event = AuditEvent.model_validate(row[7])
        except Exception as error:
            raise ApprovalDecisionConflict("Decision audit payload is invalid") from error
        if (
            event.id != row[0]
            or event.workspace_id != row[1]
            or event.event_type != row[2]
            or event.entity_id != row[3]
            or event.actor != row[4]
            or event.created_at != row[5]
            or event.details != row[6]
            or event.entity_id != approval_id
            or event.details.get("new_status") != new_status
        ):
            raise ApprovalDecisionConflict("Decision audit identity or scope is inconsistent")
        return event

    def create(
        self,
        *,
        approval: ApprovalRequest,
        audit_event: AuditEvent,
        notification: Notification,
        invocation_lease_ttl_seconds: int | None = None,
    ) -> None:
        _validate_approval_creation(
            approval, audit_event, notification, require_workspace=True
        )
        try:
            with self._connect() as conn:
                project_row = conn.execute(
                    "SELECT workspace_id FROM projects WHERE project_id = %s FOR KEY SHARE",
                    (approval.project_id,),
                ).fetchone()
                if project_row is None:
                    raise ApprovalDecisionConflict("Approval project is missing.")
                if project_row[0] != approval.workspace_id:
                    raise ApprovalDecisionConflict("Approval project ownership does not match its workspace.")
                _validate_postgres_work_order_scope(conn, approval)
                conn.execute(
                    "INSERT INTO approval_requests "
                    "(approval_id,project_id,workspace_id,work_order_id,source_agent_id,source_invocation_id,status,"
                    "expires_at,created_at,updated_at,version,payload) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s)",
                    (
                        approval.id, approval.project_id, approval.workspace_id,
                        approval.work_order_id, approval.source_agent_id,
                        approval.source_invocation_id, approval.status.value,
                        approval.expires_at, approval.created_at, approval.updated_at,
                        Jsonb(approval.model_dump(mode="json")),
                    ),
                )
                self._insert_audit(conn, approval, audit_event)
                conn.execute(
                    "INSERT INTO notifications "
                    "(notification_id,workspace_id,user_id,approval_id,entity_type,status,attempts,"
                    "next_attempt_at,lease_expires_at,created_at,payload) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        notification.id, notification.workspace_id, notification.user_id,
                        approval.id, notification.entity_type, notification.push_status,
                        notification.push_attempts, notification.push_next_attempt_at,
                        notification.push_lease_expires_at, notification.created_at,
                        Jsonb(notification.model_dump(mode="json")),
                    ),
                )
                if (
                    invocation_lease_ttl_seconds is not None
                    and approval.source_agent_id
                    and approval.source_invocation_id
                    and approval.status == ApprovalStatus.PENDING_REVIEW
                ):
                    if not approval.workspace_id:
                        raise ApprovalDecisionConflict(
                            "Connector invocation leases require a workspace-scoped approval"
                        )
                    lease_expires_at = _invocation_lease_deadline(
                        approval=approval,
                        heartbeat_at=approval.created_at,
                        ttl_seconds=invocation_lease_ttl_seconds,
                    )
                    conn.execute(
                        "INSERT INTO approval_invocation_leases "
                        "(approval_id,workspace_id,source_agent_id,source_invocation_id,heartbeat_at,expires_at) "
                        "VALUES(%s,%s,%s,%s,%s,%s)",
                        (
                            approval.id,
                            approval.workspace_id,
                            approval.source_agent_id,
                            approval.source_invocation_id,
                            approval.created_at,
                            lease_expires_at,
                        ),
                    )
        except psycopg.errors.UniqueViolation as exc:
            raise ApprovalDecisionConflict("Approval creation identifiers already exist.") from exc

    def create_for_agent_task(
        self,
        *,
        approval: ApprovalRequest,
        audit_event: AuditEvent,
        task: AgentTask,
        task_event: AgentTaskEvent,
        task_audit_event: AuditEvent,
        notification: Notification,
    ) -> None:
        _validate_approval_creation(
            approval, audit_event, notification, require_workspace=True,
            notification_entity_type="agent_task", notification_entity_id=task.id,
        )
        if (
            task.id != task_event.task_id
            or task.approval_request_id != approval.id
            or task.project_id != approval.project_id
            or task.workspace_id != approval.workspace_id
            or task.status != AgentTaskStatus.WAITING_FOR_APPROVAL
            or task_event.status != AgentTaskStatus.WAITING_FOR_APPROVAL
            or task_event.event_type.value != "APPROVAL_CREATED"
            or task_audit_event.entity_id != task.id
            or task_audit_event.event_type != "agent_task.approval_created"
            or task_audit_event.workspace_id != approval.workspace_id
        ):
            raise ValueError("AgentTask approval records do not match the scoped transition.")
        try:
            with self._connect() as conn:
                project = conn.execute(
                    "SELECT workspace_id FROM projects WHERE project_id=%s FOR KEY SHARE",
                    (approval.project_id,),
                ).fetchone()
                if project is None or project[0] != approval.workspace_id:
                    raise ApprovalDecisionConflict("Approval project ownership is missing or mismatched.")
                _validate_postgres_work_order_scope(conn, approval)
                durable_task = conn.execute(
                    "SELECT workspace_id, project_id, approval_request_id, payload "
                    "FROM agent_tasks WHERE task_id=%s FOR UPDATE",
                    (task.id,),
                ).fetchone()
                if durable_task is None:
                    raise ApprovalDecisionConflict("AgentTask is missing from durable storage.")
                if (
                    durable_task[0] != approval.workspace_id
                    or durable_task[1] != approval.project_id
                    or durable_task[2] is not None
                ):
                    raise ApprovalDecisionConflict("AgentTask changed or has a different owner.")
                conn.execute(
                    "INSERT INTO approval_requests "
                    "(approval_id,project_id,workspace_id,work_order_id,source_agent_id,source_invocation_id,status,"
                    "expires_at,created_at,updated_at,version,payload) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s)",
                    (
                        approval.id, approval.project_id, approval.workspace_id,
                        approval.work_order_id, approval.source_agent_id,
                        approval.source_invocation_id, approval.status.value,
                        approval.expires_at, approval.created_at, approval.updated_at,
                        Jsonb(approval.model_dump(mode="json")),
                    ),
                )
                self._insert_audit(conn, approval, audit_event)
                task_update = conn.execute(
                    "UPDATE agent_tasks SET approval_request_id=%s,status=%s,updated_at=%s,payload=%s "
                    "WHERE task_id=%s AND approval_request_id IS NULL",
                    (
                        approval.id, task.status.value, task.updated_at,
                        Jsonb(task.model_dump(mode="json")), task.id,
                    ),
                )
                if task_update.rowcount != 1:
                    raise ApprovalDecisionConflict("AgentTask changed during approval creation.")
                conn.execute(
                    "INSERT INTO agent_task_events "
                    "(event_id,task_id,workspace_id,event_type,actor,created_at,payload) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    (
                        task_event.id, task.id, approval.workspace_id,
                        task_event.event_type.value, task_event.actor, task_event.created_at,
                        Jsonb(task_event.model_dump(mode="json")),
                    ),
                )
                conn.execute(
                    "INSERT INTO audit_events "
                    "(event_id,workspace_id,approval_id,entity_type,entity_id,event_type,"
                    "actor,details,created_at,payload) "
                    "VALUES(%s,%s,NULL,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        task_audit_event.id, task_audit_event.workspace_id, "agent_task",
                        task_audit_event.entity_id, task_audit_event.event_type,
                        task_audit_event.actor, Jsonb(task_audit_event.details),
                        task_audit_event.created_at, Jsonb(task_audit_event.model_dump(mode="json")),
                    ),
                )
                conn.execute(
                    "INSERT INTO notifications "
                    "(notification_id,workspace_id,user_id,approval_id,task_id,entity_type,status,attempts,"
                    "next_attempt_at,lease_expires_at,created_at,payload) "
                    "VALUES(%s,%s,%s,NULL,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        notification.id, approval.workspace_id, notification.user_id,
                        task.id, "agent_task", notification.push_status,
                        notification.push_attempts, notification.push_next_attempt_at,
                        notification.push_lease_expires_at, notification.created_at,
                        Jsonb(notification.model_dump(mode="json")),
                    ),
                )
        except psycopg.errors.UniqueViolation as exc:
            raise ApprovalDecisionConflict("AgentTask approval identifiers already exist.") from exc

    def backfill_expiry(
        self,
        *,
        approval_id: str,
        expected_approval: ApprovalRequest,
        updated_approval: ApprovalRequest,
        audit_event: AuditEvent,
    ) -> tuple[ApprovalRequest, bool]:
        _validate_expiry_backfill(
            approval_id, expected_approval, updated_approval, audit_event
        )
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload,version FROM approval_requests "
                "WHERE approval_id=%s FOR UPDATE",
                (approval_id,),
            ).fetchone()
            durable, version = self._approval_from_row(row, approval_id)
            if durable.expires_at is not None:
                return durable, False
            if durable != expected_approval:
                raise ApprovalDecisionConflict("Approval changed before expiry could be backfilled.")
            if conn.execute(
                "SELECT 1 FROM audit_events WHERE event_id=%s", (audit_event.id,)
            ).fetchone():
                raise ApprovalDecisionConflict("Expiry backfill audit identifier already exists.")
            result = conn.execute(
                "UPDATE approval_requests SET expires_at=%s,updated_at=%s,payload=%s,"
                "version=version+1 WHERE approval_id=%s AND version=%s",
                (
                    updated_approval.expires_at,
                    updated_approval.updated_at,
                    Jsonb(updated_approval.model_dump(mode="json")),
                    approval_id,
                    version,
                ),
            )
            if result.rowcount != 1:
                raise ApprovalDecisionConflict("Approval changed during expiry backfill.")
            self._insert_audit(conn, updated_approval, audit_event)
        return updated_approval, True

    def expire_pending(
        self,
        *,
        approval_id: str,
        expected_approval: ApprovalRequest,
        expired_approval: ApprovalRequest,
        audit_event: AuditEvent,
    ) -> ApprovalExpirationCommit:
        self._validate_expiration(
            approval_id, expected_approval, expired_approval, audit_event
        )
        if expected_approval.workspace_id is None:
            raise ApprovalDecisionConflict("Approval workspace is required for PostgreSQL persistence.")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload, version FROM approval_requests "
                "WHERE approval_id = %s FOR UPDATE",
                (approval_id,),
            ).fetchone()
            durable, version = self._approval_from_row(row, approval_id)
            if durable != expected_approval:
                raise ApprovalDecisionConflict("Approval changed before expiration could commit.")
            try:
                apply_postgres_approval_state_transition(conn, expired_approval)
            except MCPPendingRepositoryError as exc:
                raise ApprovalDecisionConflict(
                    "Linked MCP pending request changed before approval expiration committed."
                ) from exc
            linked_rows = conn.execute(
                "SELECT task_id,workspace_id,project_id,approval_request_id,payload "
                "FROM agent_tasks WHERE approval_request_id=%s ORDER BY task_id FOR UPDATE",
                (approval_id,),
            ).fetchall()
            if len(linked_rows) > 1:
                raise ApprovalDecisionConflict(
                    "Approval is linked to multiple AgentTasks; refusing ambiguous expiration."
                )
            linked_task = task_event = task_audit_event = task_notification = None
            if linked_rows:
                task_id, task_workspace_id, task_project_id, task_approval_id, task_payload = linked_rows[0]
                durable_task = AgentTask.model_validate(task_payload)
                if (
                    durable_task.id != task_id
                    or durable_task.workspace_id != task_workspace_id
                    or durable_task.project_id != task_project_id
                    or task_approval_id != approval_id
                ):
                    raise ApprovalDecisionConflict("Linked AgentTask identity is inconsistent.")
                linked_task, task_event, task_audit_event, task_notification = _task_expiration_records(
                    task=durable_task, approval=expired_approval, actor=audit_event.actor
                )
                task_update = conn.execute(
                    "UPDATE agent_tasks SET updated_at=%s,payload=%s "
                    "WHERE task_id=%s AND workspace_id=%s AND project_id=%s "
                    "AND approval_request_id=%s",
                    (
                        linked_task.updated_at,
                        Jsonb(linked_task.model_dump(mode="json")),
                        task_id,
                        task_workspace_id,
                        task_project_id,
                        approval_id,
                    ),
                )
                if task_update.rowcount != 1:
                    raise ApprovalDecisionConflict("AgentTask changed during expiration commit.")
                conn.execute(
                    "INSERT INTO agent_task_events "
                    "(event_id,task_id,workspace_id,event_type,actor,created_at,payload) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    (
                        task_event.id, task_id, task_workspace_id, task_event.event_type.value,
                        task_event.actor, task_event.created_at,
                        Jsonb(task_event.model_dump(mode="json")),
                    ),
                )
                conn.execute(
                    "INSERT INTO audit_events "
                    "(event_id,workspace_id,approval_id,entity_type,entity_id,event_type,"
                    "actor,details,created_at,payload) "
                    "VALUES(%s,%s,NULL,'agent_task',%s,%s,%s,%s,%s,%s)",
                    (
                        task_audit_event.id, task_audit_event.workspace_id,
                        task_audit_event.entity_id, task_audit_event.event_type,
                        task_audit_event.actor, Jsonb(task_audit_event.details),
                        task_audit_event.created_at,
                        Jsonb(task_audit_event.model_dump(mode="json")),
                    ),
                )
                conn.execute(
                    "INSERT INTO notifications "
                    "(notification_id,workspace_id,user_id,approval_id,task_id,entity_type,status,"
                    "attempts,next_attempt_at,lease_expires_at,created_at,payload) "
                    "VALUES(%s,%s,%s,NULL,%s,'agent_task',%s,%s,%s,%s,%s,%s)",
                    (
                        task_notification.id, task_workspace_id, task_notification.user_id,
                        task_id, task_notification.push_status, task_notification.push_attempts,
                        task_notification.push_next_attempt_at,
                        task_notification.push_lease_expires_at, task_notification.created_at,
                        Jsonb(task_notification.model_dump(mode="json")),
                    ),
                )
            result = conn.execute(
                "UPDATE approval_requests SET status = %s, expires_at = %s, updated_at = %s, "
                "payload = %s, version = version + 1 "
                "WHERE approval_id = %s AND version = %s",
                (
                    expired_approval.status.value,
                    expired_approval.expires_at,
                    expired_approval.updated_at,
                    Jsonb(expired_approval.model_dump(mode="json")),
                    approval_id,
                    version,
                ),
            )
            if result.rowcount != 1:
                raise ApprovalDecisionConflict("Approval changed during expiration commit.")
            self._insert_audit(conn, expired_approval, audit_event)
        return ApprovalExpirationCommit(
            approval=expired_approval,
            linked_task=linked_task,
            linked_task_event=task_event,
            linked_task_audit_event=task_audit_event,
            linked_task_notification=task_notification,
        )

    def commit_decision(
        self,
        *,
        approval_id: str,
        expected_status: ApprovalStatus,
        expected_approval: ApprovalRequest,
        updated_approval: ApprovalRequest,
        audit_event: AuditEvent,
        decision_notification: Notification,
        run_repository: object | None = None,
    ) -> ApprovalDecisionCommit:
        self._validate_decision(
            approval_id=approval_id,
            expected_status=expected_status,
            expected_approval=expected_approval,
            updated_approval=updated_approval,
            audit_event=audit_event,
            decision_notification=decision_notification,
        )
        if expected_approval.workspace_id is None:
            raise ApprovalDecisionConflict("Approval workspace is required for PostgreSQL persistence.")

        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload, version FROM approval_requests "
                "WHERE approval_id = %s FOR UPDATE",
                (approval_id,),
            ).fetchone()
            durable, version = self._approval_from_row(row, approval_id)
            if durable.status != expected_status:
                raise ApprovalDecisionConflict(
                    f"Approval is {durable.status}; expected {expected_status}."
                )
            if durable != expected_approval:
                raise ApprovalDecisionConflict("Approval changed since the decision snapshot was read.")
            decision_at = now_utc()
            if durable.expires_at is not None and durable.expires_at <= decision_at:
                raise ApprovalDecisionConflict("Approval deadline elapsed before the decision committed.")
            if durable.source_invocation_id is not None:
                invocation_lease = conn.execute(
                    "SELECT approval_id,workspace_id,source_agent_id,source_invocation_id,heartbeat_at,expires_at "
                    "FROM approval_invocation_leases WHERE approval_id=%s FOR UPDATE",
                    (approval_id,),
                ).fetchone()
                _validate_live_decision_invocation(
                    approval=durable,
                    lease_row=invocation_lease,
                    now=decision_at,
                    postgres=True,
                )
            if durable.workspace_id != updated_approval.workspace_id:
                raise ApprovalDecisionConflict("Approval workspace cannot change during a decision.")

            project_row = conn.execute(
                "SELECT workspace_id FROM projects WHERE project_id = %s FOR KEY SHARE",
                (durable.project_id,),
            ).fetchone()
            if project_row is None:
                raise ApprovalDecisionConflict("Approval project is missing.")
            project_workspace_id = project_row[0]
            if project_workspace_id != durable.workspace_id:
                raise ApprovalDecisionConflict("Approval workspace does not match its project.")
            if decision_notification.workspace_id != durable.workspace_id:
                raise ValueError("Approval notification workspace must match the durable approval scope")

            try:
                apply_postgres_approval_state_transition(conn, updated_approval)
            except MCPPendingRepositoryError as exc:
                raise ApprovalDecisionConflict(
                    "Linked MCP pending request changed before the approval decision committed."
                ) from exc

            linked_rows = conn.execute(
                "SELECT task_id, workspace_id, status, payload FROM agent_tasks "
                "WHERE approval_request_id = %s ORDER BY task_id FOR UPDATE",
                (approval_id,),
            ).fetchall()
            if len(linked_rows) > 1:
                raise ApprovalDecisionConflict(
                    "Approval is linked to multiple AgentTasks; refusing ambiguous propagation."
                )

            updated_task: AgentTask | None = None
            linked_task_event: AgentTaskEvent | None = None
            linked_task_audit_event: AuditEvent | None = None
            linked_task_notification: Notification | None = None
            linked_run: AgentRun | None = None
            if linked_rows:
                task_id, task_workspace_id, task_status_value, task_payload = linked_rows[0]
                durable_task = AgentTask.model_validate(task_payload)
                if (
                    durable_task.id != task_id
                    or durable_task.project_id != durable.project_id
                    or durable_task.workspace_id != task_workspace_id
                    or durable_task.status.value != task_status_value
                    or task_workspace_id != durable.workspace_id
                ):
                    raise ApprovalDecisionConflict(
                        "Linked AgentTask identity or ownership does not match the approval."
                    )
                if durable_task.status != AgentTaskStatus.WAITING_FOR_APPROVAL:
                    raise ApprovalDecisionConflict(
                        "Linked AgentTask is no longer waiting for this approval decision."
                    )
                task_status = {
                    ApprovalStatus.APPROVED: AgentTaskStatus.APPROVED,
                    ApprovalStatus.DENIED: AgentTaskStatus.DENIED,
                    ApprovalStatus.REVISION_REQUESTED: AgentTaskStatus.PLANNING,
                }.get(updated_approval.status)
                if task_status is not None:
                    updated_task = durable_task.model_copy(
                        update={"status": task_status, "updated_at": updated_approval.updated_at}
                    )
                    task_update = conn.execute(
                        "UPDATE agent_tasks SET status = %s, updated_at = %s, payload = %s "
                        "WHERE task_id = %s",
                        (
                            updated_task.status.value,
                            updated_task.updated_at,
                            Jsonb(updated_task.model_dump(mode="json")),
                            task_id,
                        ),
                    )
                    if task_update.rowcount != 1:
                        raise ApprovalDecisionConflict("Linked AgentTask changed during decision commit.")
                    (
                        linked_task_event,
                        linked_task_audit_event,
                        linked_task_notification,
                    ) = _task_decision_records(
                        task=updated_task,
                        approval=updated_approval,
                        previous_status=expected_status,
                        actor=audit_event.actor,
                    )
                    conn.execute(
                        "INSERT INTO agent_task_events "
                        "(event_id,task_id,workspace_id,event_type,actor,created_at,payload) "
                        "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                        (
                            linked_task_event.id, task_id, task_workspace_id,
                            linked_task_event.event_type.value, linked_task_event.actor,
                            linked_task_event.created_at,
                            Jsonb(linked_task_event.model_dump(mode="json")),
                        ),
                    )
                    conn.execute(
                        "INSERT INTO audit_events "
                        "(event_id,workspace_id,approval_id,entity_type,entity_id,event_type,"
                        "actor,details,created_at,payload) "
                        "VALUES(%s,%s,NULL,'agent_task',%s,%s,%s,%s,%s,%s)",
                        (
                            linked_task_audit_event.id, linked_task_audit_event.workspace_id,
                            linked_task_audit_event.entity_id, linked_task_audit_event.event_type,
                            linked_task_audit_event.actor, Jsonb(linked_task_audit_event.details),
                            linked_task_audit_event.created_at,
                            Jsonb(linked_task_audit_event.model_dump(mode="json")),
                        ),
                    )
                    conn.execute(
                        "INSERT INTO notifications "
                        "(notification_id,workspace_id,user_id,approval_id,task_id,entity_type,status,"
                        "attempts,next_attempt_at,lease_expires_at,created_at,payload) "
                        "VALUES(%s,%s,%s,NULL,%s,'agent_task',%s,%s,%s,%s,%s,%s)",
                        (
                            linked_task_notification.id, task_workspace_id,
                            linked_task_notification.user_id, task_id,
                            linked_task_notification.push_status,
                            linked_task_notification.push_attempts,
                            linked_task_notification.push_next_attempt_at,
                            linked_task_notification.push_lease_expires_at,
                            linked_task_notification.created_at,
                            Jsonb(linked_task_notification.model_dump(mode="json")),
                        ),
                    )

            approval_update = conn.execute(
                "UPDATE approval_requests SET status = %s, updated_at = %s, payload = %s, "
                "version = version + 1 WHERE approval_id = %s AND status = %s AND version = %s",
                (
                    updated_approval.status.value,
                    updated_approval.updated_at,
                    Jsonb(updated_approval.model_dump(mode="json")),
                    approval_id,
                    expected_status.value,
                    version,
                ),
            )
            if approval_update.rowcount != 1:
                raise ApprovalDecisionConflict("Approval changed during decision commit.")
            self._insert_audit(conn, updated_approval, audit_event)
            conn.execute(
                "INSERT INTO notifications "
                "(notification_id, workspace_id, user_id, approval_id, entity_type, status, attempts, "
                "next_attempt_at, lease_expires_at, created_at, payload) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    decision_notification.id,
                    decision_notification.workspace_id,
                    decision_notification.user_id,
                    approval_id,
                    decision_notification.entity_type,
                    decision_notification.push_status,
                    decision_notification.push_attempts,
                    decision_notification.push_next_attempt_at,
                    decision_notification.push_lease_expires_at,
                    decision_notification.created_at,
                    Jsonb(decision_notification.model_dump(mode="json")),
                ),
            )
            if updated_approval.status == ApprovalStatus.APPROVED and updated_task is not None:
                from .agent_run_creation import build_agent_run_creation_records
                from .agent_run_models import AgentRunCreate
                from .agent_run_repository import (
                    AgentRunRepositoryConflict,
                    PostgresAgentRunRepository,
                )

                if not isinstance(run_repository, PostgresAgentRunRepository):
                    raise ApprovalDecisionConflict(
                        "PostgreSQL approval of a linked AgentTask requires its durable run repository."
                    )
                if run_repository._connect is not self._connect:
                    raise ApprovalDecisionConflict(
                        "Approval and AgentRun repositories must share the same PostgreSQL connection factory."
                    )
                records = build_agent_run_creation_records(
                    task=updated_task,
                    approval=updated_approval,
                    payload=AgentRunCreate(
                        task_id=updated_task.id,
                        metadata={
                            "scheduled_from": "approval_decision",
                            "approval_decision_actor": audit_event.actor,
                        },
                    ),
                    actor=audit_event.actor,
                )
                try:
                    run_repository.create_in_transaction(
                        conn,
                        run=records.run,
                        steps=records.steps,
                        events=records.events,
                        audit=records.audit,
                        scheduled_audit=records.scheduled_audit,
                    )
                except (AgentRunRepositoryConflict, psycopg.errors.UniqueViolation) as exc:
                    raise ApprovalDecisionConflict(
                        "Linked AgentRun could not be scheduled with the approval decision."
                    ) from exc
                linked_run = records.run

        return ApprovalDecisionCommit(
            linked_task=updated_task,
            notification=decision_notification,
            linked_task_event=linked_task_event,
            linked_task_audit_event=linked_task_audit_event,
            linked_task_notification=linked_task_notification,
            linked_run=linked_run,
        )

    @staticmethod
    def _insert_audit(
        conn: psycopg.Connection,
        approval: ApprovalRequest,
        audit_event: AuditEvent,
    ) -> None:
        if audit_event.workspace_id != approval.workspace_id:
            raise ValueError("Approval audit workspace must match the durable approval scope")
        conn.execute(
            "INSERT INTO audit_events "
            "(event_id, workspace_id, approval_id, entity_type, entity_id, event_type, "
            "actor, details, created_at, payload) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                audit_event.id,
                audit_event.workspace_id,
                approval.id,
                "approval_request",
                audit_event.entity_id,
                audit_event.event_type,
                audit_event.actor,
                Jsonb(audit_event.model_dump(mode="json")["details"]),
                audit_event.created_at,
                Jsonb(audit_event.model_dump(mode="json")),
            ),
        )

    @staticmethod
    def _validate_decision(
        *,
        approval_id: str,
        expected_status: ApprovalStatus,
        expected_approval: ApprovalRequest,
        updated_approval: ApprovalRequest,
        audit_event: AuditEvent,
        decision_notification: Notification,
    ) -> None:
        if (
            expected_approval.id != approval_id
            or updated_approval.id != approval_id
            or audit_event.entity_id != approval_id
            or decision_notification.entity_id != approval_id
        ):
            raise ValueError("Approval and audit identifiers must match the decision target")
        if decision_notification.entity_type != "approval_request":
            raise ValueError("Approval decision notification must reference the approval request")
        if expected_approval.status != expected_status:
            raise ValueError("Expected approval snapshot status must match expected_status")
        if audit_event.event_type != "approval.decision":
            raise ValueError("Approval decision must use an approval.decision audit event")
        if (
            audit_event.details.get("previous_status") != expected_status
            or audit_event.details.get("new_status") != updated_approval.status
        ):
            raise ValueError("Approval audit statuses must match the committed transition")
        if audit_event.details.get("approved_payload_hash") != updated_approval.approved_payload_hash:
            raise ValueError("Approval audit payload hash must match the committed approval")
        if updated_approval.status == ApprovalStatus.APPROVED and not updated_approval.approved_payload_hash:
            raise ValueError("Approved approval must include a frozen payload hash")
        if (
            updated_approval.approved_payload_hash is not None
            and updated_approval.approved_payload_hash
            != compute_approval_payload_hash(updated_approval)
        ):
            raise ValueError("Approval payload does not match its frozen payload hash")
        if not can_transition(expected_status, updated_approval.status):
            raise ValueError(
                f"Invalid approval transition: {expected_status} -> {updated_approval.status}"
            )
