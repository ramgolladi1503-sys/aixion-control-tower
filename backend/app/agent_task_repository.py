from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Protocol

import psycopg
from psycopg.types.json import Jsonb

from .agent_task_models import AgentTask, AgentTaskEvent, AgentTaskEventType, AgentTaskStatus
from .approval_integrity import compute_approval_payload_hash
from .models import (
    AgentProvider, ApprovalRequest, ApprovalStatus, AuditEvent, Notification, Project,
    new_id,
)

RETRY_REQUESTED_OPERATION = "agent_task_retry_requested"

_CANCELLABLE_TASK_STATUSES = {
    AgentTaskStatus.RECEIVED,
    AgentTaskStatus.PLANNING,
    AgentTaskStatus.WAITING_FOR_APPROVAL,
    AgentTaskStatus.APPROVED,
    AgentTaskStatus.EXECUTING,
    AgentTaskStatus.TESTING,
    AgentTaskStatus.FAILED,
}


def _validate_cancel_commit(
    expected: AgentTask,
    updated: AgentTask,
    event: AgentTaskEvent,
    audit: AuditEvent,
    expected_approval: ApprovalRequest | None,
    cancelled_approval: ApprovalRequest | None,
    approval_audit: AuditEvent | None,
    approval_notification: Notification | None,
) -> None:
    approval_fields = (cancelled_approval, approval_audit, approval_notification)
    if expected.status == AgentTaskStatus.WAITING_FOR_APPROVAL:
        if (
            not expected.approval_request_id
            or expected_approval is None
            or expected_approval.id != expected.approval_request_id
            or expected_approval.project_id != expected.project_id
            or expected_approval.workspace_id != expected.workspace_id
            or expected_approval.status not in {
                ApprovalStatus.REQUESTED,
                ApprovalStatus.PENDING_REVIEW,
                ApprovalStatus.BLOCKED,
                ApprovalStatus.DENIED,
                ApprovalStatus.EXPIRED,
                ApprovalStatus.CANCELLED,
            }
        ):
            raise ValueError("Waiting AgentTask cancellation requires its matching pending or terminal approval.")
        if expected_approval.status in {
            ApprovalStatus.REQUESTED,
            ApprovalStatus.PENDING_REVIEW,
            ApprovalStatus.BLOCKED,
        }:
            if any(value is None for value in approval_fields):
                raise ValueError("Pending approval cancellation requires approval, audit, and notification records.")
            if (
                cancelled_approval.id != expected_approval.id
                or cancelled_approval.project_id != expected_approval.project_id
                or cancelled_approval.workspace_id != expected_approval.workspace_id
                or cancelled_approval.status != ApprovalStatus.CANCELLED
                or cancelled_approval != expected_approval.model_copy(
                    update={"status": ApprovalStatus.CANCELLED, "updated_at": cancelled_approval.updated_at}
                )
                or approval_audit.event_type != "approval.cancelled"
                or approval_audit.entity_id != expected_approval.id
                or approval_audit.workspace_id != expected_approval.workspace_id
                or approval_audit.actor != event.actor
                or approval_audit.details.get("source_task_id") != expected.id
                or approval_audit.details.get("previous_status") != expected_approval.status
                or approval_audit.details.get("new_status") != ApprovalStatus.CANCELLED
                or approval_audit.details.get("approved_payload_hash") != expected_approval.approved_payload_hash
                or approval_notification.entity_type != "approval_request"
                or approval_notification.entity_id != expected_approval.id
                or approval_notification.approval_request_id != expected_approval.id
                or approval_notification.workspace_id != expected_approval.workspace_id
            ):
                raise ValueError("Linked approval cancellation records do not match the waiting AgentTask.")
        elif any(value is not None for value in approval_fields):
            raise ValueError("Terminal approval cancellation must not create another approval transition.")
    elif expected_approval is not None or any(value is not None for value in approval_fields):
        raise ValueError("Only a waiting AgentTask may cancel a linked approval.")
    approval_changed = cancelled_approval is not None
    if (
        expected.id != updated.id
        or expected.project_id != updated.project_id
        or expected.workspace_id != updated.workspace_id
        or expected.approval_request_id != updated.approval_request_id
        or expected.id != event.task_id
        or expected.id != audit.entity_id
        or event.actor != audit.actor
        or expected.status not in _CANCELLABLE_TASK_STATUSES
        or updated.status != AgentTaskStatus.CANCELLED
        or event.event_type.value != "CANCELLED"
        or event.status != AgentTaskStatus.CANCELLED
        or event.metadata.get("operation_type") != "agent_task_cancel_requested"
        or event.metadata.get("previous_status") != expected.status
        or event.metadata.get("new_status") != AgentTaskStatus.CANCELLED
        or event.metadata.get("worker_lease_cleared") is not True
        or event.metadata.get("approval_decision_changed") is not approval_changed
        or event.metadata.get("worker_cleanup_performed") is not False
        or updated.worker_lease_owner is not None
        or updated.worker_lease_expires_at is not None
        or updated.worker_lease_token is not None
        or audit.event_type != "agent_task.cancelled"
        or audit.workspace_id != updated.workspace_id
        or audit.details.get("event_id") != event.id
        or audit.details.get("approval_request_id") != expected.approval_request_id
        or audit.details.get("task_status") != AgentTaskStatus.CANCELLED
        or audit.details.get("previous_status") != expected.status
        or audit.details.get("worker_lease_cleared") is not True
        or audit.details.get("approval_decision_changed") is not approval_changed
        or audit.details.get("worker_cleanup_performed") is not False
    ):
        raise ValueError("AgentTask cancellation records do not match an allowed transition.")
    if any((expected.worker_lease_owner, expected.worker_lease_token, expected.worker_lease_expires_at)):
        raise ValueError("AgentTask cancellation is blocked while a worker lease is recorded.")


def _validate_retry_commit(
    expected: AgentTask,
    updated: AgentTask,
    event: AgentTaskEvent,
    audit: AuditEvent,
    max_retries: int,
) -> None:
    if not 0 <= max_retries <= 10:
        raise ValueError("AgentTask retry limit must be between 0 and 10.")
    previous_retry_count = event.metadata.get("previous_retry_count")
    event_max_retries = event.metadata.get("max_retries")
    if (
        expected.id != updated.id
        or expected.project_id != updated.project_id
        or expected.workspace_id != updated.workspace_id
        or expected.approval_request_id != updated.approval_request_id
        or expected.id != event.task_id
        or expected.id != audit.entity_id
        or expected.status != AgentTaskStatus.FAILED
        or not expected.approval_request_id
        or updated.status != AgentTaskStatus.APPROVED
        or event.event_type.value != "NOTE"
        or event.status != AgentTaskStatus.APPROVED
        or event.metadata.get("operation_type") != RETRY_REQUESTED_OPERATION
        or not isinstance(previous_retry_count, int)
        or isinstance(previous_retry_count, bool)
        or previous_retry_count < 0
        or event.metadata.get("retry_count") != previous_retry_count + 1
        or not isinstance(event_max_retries, int)
        or isinstance(event_max_retries, bool)
        or event_max_retries != max_retries
        or event.metadata.get("worker_lease_cleared") is not True
        or updated.worker_lease_owner is not None
        or updated.worker_lease_expires_at is not None
        or updated.worker_lease_token is not None
        or audit.event_type != "agent_task.retry_requested"
        or audit.workspace_id != updated.workspace_id
        or audit.details.get("event_id") != event.id
    ):
        raise ValueError("AgentTask retry records do not match an allowed transition.")


def _validate_event_commit(
    expected: AgentTask,
    updated: AgentTask,
    event: AgentTaskEvent,
    audit: AuditEvent,
    notification: Notification | None,
) -> None:
    expected_status = event.status or expected.status
    approval_decision_task_states = {
        AgentTaskEventType.APPROVED: (AgentTaskStatus.APPROVED, ApprovalStatus.APPROVED),
        AgentTaskEventType.DENIED: (AgentTaskStatus.DENIED, ApprovalStatus.DENIED),
    }
    terminal_approval_event = (
        event.event_type in approval_decision_task_states
        or event.status in {AgentTaskStatus.APPROVED, AgentTaskStatus.DENIED}
        or (
            expected.status != updated.status
            and updated.status in {AgentTaskStatus.APPROVED, AgentTaskStatus.DENIED}
        )
    )
    if terminal_approval_event:
        decision = approval_decision_task_states.get(event.event_type)
        recovery_event_only = (
            decision is not None
            and event.actor == "approval-recovery"
            and audit.actor == event.actor
            and event.metadata.get("reconciled") is True
            and audit.details.get("reconciled") is True
            and expected.status == decision[0]
            and updated.status == expected.status
        )
        if (
            audit.event_type != "agent_task.approval_decision_propagated"
            or decision is None
            or not expected.approval_request_id
            or (
                expected.status != AgentTaskStatus.WAITING_FOR_APPROVAL
                and not recovery_event_only
            )
            or event.status != decision[0]
            or updated.status != decision[0]
            or event.metadata.get("approval_request_id") != expected.approval_request_id
            or event.metadata.get("new_status") != decision[1]
            or audit.details.get("approval_request_id") != expected.approval_request_id
            or audit.details.get("new_status") != decision[1]
            or audit.details.get("task_status") != decision[0]
            or audit.actor != event.actor
        ):
            raise ValueError(
                "AgentTask approval decisions must come from the linked approval transition."
            )
    if (
        expected.id != updated.id
        or expected.project_id != updated.project_id
        or expected.workspace_id != updated.workspace_id
        or expected.approval_request_id != updated.approval_request_id
        or expected.id != event.task_id
        or expected.id != audit.entity_id
        or event.status != (updated.status if event.status is not None else None)
        or updated.status != expected_status
        or audit.event_type not in {
            "agent_task.event_recorded",
            "agent_task.event_recorded_by_agent",
            "connector.webhook_task_event_appended",
            "agent_task.approval_decision_propagated",
        }
        or audit.workspace_id != updated.workspace_id
        or audit.details.get("event_id") != event.id
        or audit.details.get("status") != event.status
        or audit.details.get("message") != event.message
        or audit.details.get("notification_id") != (notification.id if notification else None)
        or (notification is None and event.metadata.get("notification_id") is not None)
        or (notification is not None and (
            notification.entity_type != "agent_task"
            or notification.entity_id != event.task_id
            or event.metadata.get("notification_id") != notification.id
            or notification.workspace_id != audit.workspace_id
        ))
    ):
        raise ValueError("AgentTask event records do not match the requested transition.")


class AgentTaskRepository(Protocol):
    def create(self, *, task: AgentTask, event: AgentTaskEvent, audit: AuditEvent) -> None: ...

    def create_in_transaction(
        self, conn: psycopg.Connection, *, task: AgentTask,
        event: AgentTaskEvent, audit: AuditEvent,
    ) -> None: ...

    def get(self, task_id: str) -> AgentTask | None: ...

    def list_for_approval(self, approval_request_id: str) -> list[AgentTask]: ...

    def list(
        self,
        *,
        workspace_id: str | None = None,
        provider: AgentProvider | None = None,
        status: AgentTaskStatus | None = None,
        project_id: str | None = None,
        limit: int = 100,
    ) -> list[AgentTask]: ...

    def list_events(self, task_id: str, *, workspace_id: str | None = None) -> list[AgentTaskEvent]: ...

    def commit_cancel(
        self, *, expected: AgentTask, updated: AgentTask,
        event: AgentTaskEvent, audit: AuditEvent,
        expected_approval: ApprovalRequest | None = None,
        cancelled_approval: ApprovalRequest | None = None,
        approval_audit: AuditEvent | None = None,
        approval_notification: Notification | None = None,
    ) -> None: ...

    def commit_retry(
        self, *, expected: AgentTask, updated: AgentTask, event: AgentTaskEvent,
        audit: AuditEvent, max_retries: int,
    ) -> None: ...

    def count_retries(self, task_id: str) -> int: ...

    def commit_event(
        self, *, expected: AgentTask, updated: AgentTask, event: AgentTaskEvent,
        audit: AuditEvent, notification: Notification | None = None,
    ) -> None: ...

    def commit_event_in_transaction(
        self, conn: psycopg.Connection, *, expected: AgentTask,
        updated: AgentTask, event: AgentTaskEvent, audit: AuditEvent,
        notification: Notification | None = None,
    ) -> None: ...

    def claim(
        self, *, task_id: str | None, worker_id: str, lease_seconds: int,
    ) -> tuple[AgentTask | None, str, AuditEvent | None]: ...

    def renew_validation_lease(
        self, *, task: AgentTask, worker_id: str, lease_token: str, lease_seconds: int,
    ) -> AgentTask: ...

    def commit_worker_outcome(
        self, *, expected: AgentTask, updated: AgentTask,
        event: AgentTaskEvent | None, audit: AuditEvent,
    ) -> None: ...

    def recover_abandoned_validation_tasks(self, *, task_id: str | None = None) -> list[AgentTask]: ...


def _validate_worker_outcome_commit(
    expected: AgentTask,
    updated: AgentTask,
    event: AgentTaskEvent | None,
    audit: AuditEvent,
) -> None:
    validation_execution_failed = (
        event is not None
        and updated.status == AgentTaskStatus.FAILED
        and event.event_type == AgentTaskEventType.TESTS_FAILED
        and event.status == AgentTaskStatus.FAILED
        and expected.approval_request_id is not None
        and event.metadata.get("approval_request_id") == expected.approval_request_id
        and event.metadata.get("runner_type") == "validation_command_runner"
        and event.metadata.get("commands_executed") is True
        and audit.event_type == "agent_worker.validation_run_completed"
        and audit.details.get("approval_request_id") == expected.approval_request_id
        and audit.details.get("passed") is False
    )
    validation_runner_outcome_unknown = (
        event is not None
        and updated.status == AgentTaskStatus.FAILED
        and event.event_type == AgentTaskEventType.FAILED
        and event.status == AgentTaskStatus.FAILED
        and expected.approval_request_id is not None
        and event.metadata.get("approval_request_id") == expected.approval_request_id
        and event.metadata.get("runner_type") == "validation_command_runner"
        and "commands_executed" not in event.metadata
        and isinstance(event.metadata.get("results"), list)
        and event.metadata.get("command_count") == len(event.metadata.get("results", []))
        and event.metadata.get("execution_outcome") == "UNKNOWN"
        and isinstance(event.metadata.get("failure_class"), str)
        and bool(event.metadata.get("failure_class"))
        and audit.event_type == "agent_worker.validation_runner_failed"
        and audit.details.get("approval_request_id") == expected.approval_request_id
        and audit.details.get("commands_completed") == event.metadata.get("command_count")
        and audit.details.get("execution_outcome") == "UNKNOWN"
        and audit.details.get("failure_class") == event.metadata.get("failure_class")
    )
    if (
        not expected.workspace_id
        or not expected.project_id
        or expected.id != updated.id
        or expected.project_id != updated.project_id
        or expected.workspace_id != updated.workspace_id
        or expected.approval_request_id != updated.approval_request_id
        or (
            expected.status != updated.status
            and not (
                expected.status == AgentTaskStatus.APPROVED
                and event is not None
                and (
                    (
                        updated.status == AgentTaskStatus.READY_FOR_PR
                        and event.event_type == AgentTaskEventType.PR_CREATED
                    )
                    or validation_execution_failed
                    or validation_runner_outcome_unknown
                )
            )
        )
        or not expected.worker_lease_owner
        or not expected.worker_lease_token
        or expected.worker_lease_expires_at is None
        or expected.worker_lease_expires_at <= datetime.now(timezone.utc)
        or updated.worker_lease_owner is not None
        or updated.worker_lease_expires_at is not None
        or updated.worker_lease_token is not None
        or audit.entity_id != updated.id
        or audit.workspace_id != updated.workspace_id
        or not audit.event_type.startswith("agent_worker.")
        or (event is not None and (
            event.task_id != updated.id
            or event.status != updated.status
            or event.actor != audit.actor
            or audit.details.get("event_id") != event.id
        ))
    ):
        raise ValueError("Worker outcome records do not match a leased task and workspace.")


def _worker_eligibility_failure(
    task: AgentTask,
    approval: ApprovalRequest | None,
    project: Project | None,
    *,
    now: datetime,
) -> str | None:
    if task.status in {
        AgentTaskStatus.DENIED, AgentTaskStatus.FAILED, AgentTaskStatus.CANCELLED,
        AgentTaskStatus.DONE,
    }:
        return f"Agent task is terminal: {task.status}"
    if task.status != AgentTaskStatus.APPROVED:
        return f"Agent task is not approved: {task.status}"
    if not task.approval_request_id:
        return "Agent task is approved but missing approval_request_id"
    if approval is None:
        return "Linked approval request not found."
    if approval.status != ApprovalStatus.APPROVED:
        return f"Linked approval is not approved: {approval.status}"
    if not task.project_id:
        return "AgentTask project is missing"
    if approval.project_id != task.project_id:
        return "Linked approval project does not match the AgentTask project"
    if project is None:
        return "AgentTask project record is missing"
    if project.workspace_id is None:
        return "AgentTask project workspace is missing"
    task_workspace_id = task.workspace_id or project.workspace_id
    if task_workspace_id != project.workspace_id:
        return "AgentTask workspace does not match its project workspace"
    approval_workspace_id = approval.workspace_id or project.workspace_id
    if approval_workspace_id != project.workspace_id:
        return "Linked approval workspace does not match its project workspace"
    if task_workspace_id != approval_workspace_id:
        return "Linked approval workspace does not match the AgentTask workspace"
    if task.worker_lease_owner:
        if task.worker_lease_expires_at is None or task.worker_lease_expires_at > now:
            return f"Agent task already leased by {task.worker_lease_owner} until {task.worker_lease_expires_at}"
    return None


def _with_worker_lease(task: AgentTask, worker_id: str, lease_seconds: int, now: datetime) -> AgentTask:
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise ValueError("A worker id is required.")
    if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds < 1:
        raise ValueError("Worker lease duration must be a positive integer number of seconds.")
    return task.model_copy(update={
        "worker_lease_owner": worker_id,
        "worker_lease_token": new_id("agent_task_lease"),
        "worker_lease_expires_at": now + timedelta(seconds=lease_seconds),
        "updated_at": now,
    })


def _claim_refusal_audit(task: AgentTask, project: Project | None, worker_id: str, reason: str) -> AuditEvent:
    return AuditEvent(
        event_type="agent_worker.task_claim_refused",
        actor=worker_id,
        entity_id=task.id,
        workspace_id=task.workspace_id or (project.workspace_id if project else None),
        details={
            "task_id": task.id,
            "worker_id": worker_id,
            "approval_request_id": task.approval_request_id,
            "reason": reason,
        },
    )


def _claim_success_audit(task: AgentTask, project: Project | None, worker_id: str) -> AuditEvent:
    return AuditEvent(
        event_type="agent_worker.task_claimed",
        actor=worker_id,
        entity_id=task.id,
        workspace_id=task.workspace_id or (project.workspace_id if project else None),
        details={
            "task_id": task.id,
            "worker_id": worker_id,
            "approval_request_id": task.approval_request_id,
            "lease_expires_at": (
                task.worker_lease_expires_at.isoformat()
                if task.worker_lease_expires_at is not None else None
            ),
        },
    )


class AgentTaskRepositoryConflict(RuntimeError):
    """Raised when durable AgentTask records do not match the requested transition."""


class SQLiteAgentTaskRepository:
    """Typed operations over the local SQLite store; no process-map reads."""

    def __init__(self, connect: Callable[[], sqlite3.Connection]) -> None:
        self._connect = connect

    def create(self, *, task: AgentTask, event: AgentTaskEvent, audit: AuditEvent) -> None:
        if (
            task.id != event.task_id
            or event.event_type.value != "TASK_CREATED"
            or audit.entity_id != task.id
            or audit.event_type not in {
                "agent_task.created",
                "agent_task.created_by_agent",
                "connector.webhook_task_created",
            }
            or audit.workspace_id != task.workspace_id
        ):
            raise ValueError("AgentTask creation records do not share an entity identity.")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if task.project_id:
                    project_row = conn.execute(
                        "SELECT payload FROM kv_store WHERE entity_type='project' AND entity_id=?",
                        (task.project_id,),
                    ).fetchone()
                    if project_row is None:
                        raise AgentTaskRepositoryConflict("AgentTask project is missing.")
                    project = Project.model_validate_json(project_row[0])
                    if task.workspace_id != project.workspace_id:
                        raise AgentTaskRepositoryConflict(
                            "AgentTask project ownership does not match its workspace."
                        )
                conn.executemany(
                    "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
                    [
                        ("agent_task", task.id, task.model_dump_json()),
                        ("agent_task_event", event.id, event.model_dump_json()),
                        ("audit_event", audit.id, audit.model_dump_json()),
                    ],
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise AgentTaskRepositoryConflict("AgentTask creation identifiers already exist.") from exc

    def get(self, task_id: str) -> AgentTask | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT entity_id,payload FROM kv_store WHERE entity_type=? AND entity_id=?",
                ("agent_task", task_id),
            ).fetchone()
        if row is None:
            return None
        task = AgentTask.model_validate_json(row[1])
        if task.id != row[0] or task.id != task_id:
            raise AgentTaskRepositoryConflict("AgentTask identity does not match its durable key.")
        return task

    def list_for_approval(self, approval_request_id: str) -> list[AgentTask]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT entity_id,payload FROM kv_store WHERE entity_type=? "
                "AND json_extract(payload,'$.approval_request_id')=? "
                "ORDER BY entity_id",
                ("agent_task", approval_request_id),
            ).fetchall()
            projects = {
                row[0]: row[1]
                for row in conn.execute(
                    "SELECT entity_id,payload FROM kv_store WHERE entity_type='project'"
                ).fetchall()
            }
        tasks: list[AgentTask] = []
        for entity_id, payload in rows:
            task = AgentTask.model_validate_json(payload)
            if task.id != entity_id or task.approval_request_id != approval_request_id:
                raise AgentTaskRepositoryConflict(
                    "AgentTask approval relationship does not match its durable row."
                )
            if task.project_id:
                project_payload = projects.get(task.project_id)
                if project_payload is None:
                    continue
                project = Project.model_validate_json(project_payload)
                if task.workspace_id is not None and task.workspace_id != project.workspace_id:
                    continue
            tasks.append(task)
        return tasks

    def list(
        self,
        *,
        workspace_id: str | None = None,
        provider: AgentProvider | None = None,
        status: AgentTaskStatus | None = None,
        project_id: str | None = None,
        limit: int = 100,
    ) -> list[AgentTask]:
        if not 1 <= limit <= 500:
            raise ValueError("AgentTask list limit must be between 1 and 500.")
        clauses = ["entity_type=?"]
        params: list[object] = ["agent_task"]
        if workspace_id is not None:
            clauses.append("json_extract(payload,'$.workspace_id')=?")
            params.append(workspace_id)
        if provider is not None:
            clauses.append("json_extract(payload,'$.provider')=?")
            params.append(provider.value)
        if status is not None:
            clauses.append("json_extract(payload,'$.status')=?")
            params.append(status.value)
        if project_id is not None:
            clauses.append("json_extract(payload,'$.project_id')=?")
            params.append(project_id)
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT entity_id,payload FROM kv_store WHERE "
                + " AND ".join(clauses)
                + " ORDER BY json_extract(payload,'$.updated_at') DESC,entity_id DESC LIMIT ?",
                params,
            ).fetchall()
            projects = {
                row[0]: row[1]
                for row in conn.execute(
                    "SELECT entity_id,payload FROM kv_store WHERE entity_type='project'"
                ).fetchall()
            }
        tasks: list[AgentTask] = []
        for entity_id, payload in rows:
            task = AgentTask.model_validate_json(payload)
            if task.id != entity_id:
                raise AgentTaskRepositoryConflict("AgentTask identity does not match its durable key.")
            if task.project_id:
                project_payload = projects.get(task.project_id)
                if project_payload is None:
                    continue
                from .models import Project

                project = Project.model_validate_json(project_payload)
                if task.workspace_id is not None and task.workspace_id != project.workspace_id:
                    continue
                resolved_workspace = task.workspace_id or project.workspace_id
            else:
                resolved_workspace = task.workspace_id
            if workspace_id is not None and resolved_workspace != workspace_id:
                continue
            tasks.append(task)
        return tasks

    def list_events(self, task_id: str, *, workspace_id: str | None = None) -> list[AgentTaskEvent]:
        with self._connect() as conn:
            if workspace_id is None:
                rows = conn.execute(
                    "SELECT entity_id,payload FROM kv_store WHERE entity_type=? "
                    "AND json_extract(payload,'$.task_id')=? ORDER BY "
                    "json_extract(payload,'$.created_at'),entity_id",
                    ("agent_task_event", task_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT event.entity_id,event.payload FROM kv_store AS event "
                    "JOIN kv_store AS task ON task.entity_type='agent_task' "
                    "AND task.entity_id=? "
                    "WHERE event.entity_type='agent_task_event' "
                    "AND json_extract(event.payload,'$.task_id')=? "
                    "AND json_extract(task.payload,'$.workspace_id')=? "
                    "ORDER BY json_extract(event.payload,'$.created_at'),event.entity_id",
                    (task_id, task_id, workspace_id),
                ).fetchall()
        events = [AgentTaskEvent.model_validate_json(row[1]) for row in rows]
        if any(event.id != row[0] or event.task_id != task_id for event, row in zip(events, rows, strict=True)):
            raise AgentTaskRepositoryConflict("AgentTask event identity does not match its durable key.")
        return events

    def commit_cancel(
        self, *, expected: AgentTask, updated: AgentTask,
        event: AgentTaskEvent, audit: AuditEvent,
        expected_approval: ApprovalRequest | None = None,
        cancelled_approval: ApprovalRequest | None = None,
        approval_audit: AuditEvent | None = None,
        approval_notification: Notification | None = None,
    ) -> None:
        _validate_cancel_commit(
            expected, updated, event, audit, expected_approval,
            cancelled_approval, approval_audit, approval_notification,
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT entity_id,payload FROM kv_store WHERE entity_type='agent_task' AND entity_id=?",
                    (expected.id,),
                ).fetchone()
                if row is None or row[0] != expected.id:
                    raise AgentTaskRepositoryConflict("AgentTask is missing from durable storage.")
                durable = AgentTask.model_validate_json(row[1])
                if durable != expected:
                    raise AgentTaskRepositoryConflict("AgentTask changed before cancellation committed.")
                active_run = conn.execute(
                    "SELECT entity_id FROM kv_store WHERE entity_type='agent_run' "
                    "AND json_extract(payload,'$.task_id')=? "
                    "AND json_extract(payload,'$.status') NOT IN ('FAILED','CANCELLED','BLOCKED') LIMIT 1",
                    (expected.id,),
                ).fetchone()
                if active_run is not None:
                    raise AgentTaskRepositoryConflict(
                        "An AgentRun is linked to this task; cancel or reconcile the run before cancelling the task."
                    )
                approval_row = None
                if expected_approval is not None:
                    approval_row = conn.execute(
                        "SELECT payload FROM kv_store WHERE entity_type='approval_request' AND entity_id=?",
                        (expected_approval.id,),
                    ).fetchone()
                    if approval_row is None:
                        raise AgentTaskRepositoryConflict("Linked approval is missing from durable storage.")
                    durable_approval = ApprovalRequest.model_validate_json(approval_row[0])
                    if durable_approval != expected_approval:
                        raise AgentTaskRepositoryConflict("Linked approval changed before task cancellation committed.")
                changed = conn.execute(
                    "UPDATE kv_store SET payload=? WHERE entity_type='agent_task' AND entity_id=? AND payload=?",
                    (updated.model_dump_json(), expected.id, row[1]),
                )
                if changed.rowcount != 1:
                    raise AgentTaskRepositoryConflict("AgentTask changed during cancellation.")
                if cancelled_approval is not None:
                    approval_update = conn.execute(
                        "UPDATE kv_store SET payload=? WHERE entity_type='approval_request' "
                        "AND entity_id=? AND payload=?",
                        (
                            cancelled_approval.model_dump_json(),
                            expected_approval.id,
                            approval_row[0],
                        ),
                    )
                    if approval_update.rowcount != 1:
                        raise AgentTaskRepositoryConflict("Linked approval changed during task cancellation.")
                conn.executemany(
                    "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
                    [
                        ("agent_task_event", event.id, event.model_dump_json()),
                        ("audit_event", audit.id, audit.model_dump_json()),
                        *(
                            [("audit_event", approval_audit.id, approval_audit.model_dump_json())]
                            if approval_audit is not None else []
                        ),
                        *(
                            [("notification", approval_notification.id, approval_notification.model_dump_json())]
                            if approval_notification is not None else []
                        ),
                    ],
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise AgentTaskRepositoryConflict("AgentTask cancellation record already exists.") from exc

    def count_retries(self, task_id: str) -> int:
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM kv_store WHERE entity_type='agent_task_event' "
                "AND json_extract(payload,'$.task_id')=? "
                "AND json_extract(payload,'$.metadata.operation_type')=?",
                (task_id, RETRY_REQUESTED_OPERATION),
            ).fetchone()[0]

    def commit_retry(
        self, *, expected: AgentTask, updated: AgentTask, event: AgentTaskEvent,
        audit: AuditEvent, max_retries: int,
    ) -> None:
        _validate_retry_commit(expected, updated, event, audit, max_retries)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT entity_id,payload FROM kv_store WHERE entity_type='agent_task' AND entity_id=?",
                    (expected.id,),
                ).fetchone()
                if row is None or row[0] != expected.id:
                    raise AgentTaskRepositoryConflict("AgentTask is missing from durable storage.")
                durable = AgentTask.model_validate_json(row[1])
                if durable != expected:
                    raise AgentTaskRepositoryConflict("AgentTask changed before retry committed.")
                approval_row = conn.execute(
                    "SELECT entity_id,payload FROM kv_store "
                    "WHERE entity_type='approval_request' AND entity_id=?",
                    (expected.approval_request_id,),
                ).fetchone()
                if approval_row is None:
                    raise AgentTaskRepositoryConflict("Linked approval request was not found.")
                durable_approval = ApprovalRequest.model_validate_json(approval_row[1])
                if (
                    durable_approval.id != approval_row[0]
                    or durable_approval.id != expected.approval_request_id
                    or durable_approval.status != ApprovalStatus.APPROVED
                    or durable_approval.project_id != expected.project_id
                    or durable_approval.workspace_id != expected.workspace_id
                    or not durable_approval.approved_payload_hash
                    or durable_approval.approved_payload_hash
                    != compute_approval_payload_hash(durable_approval)
                ):
                    raise AgentTaskRepositoryConflict(
                        "Linked approval must remain approved and match the AgentTask scope before retry."
                    )
                if expected.project_id:
                    project_row = conn.execute(
                        "SELECT entity_id,payload FROM kv_store "
                        "WHERE entity_type='project' AND entity_id=?",
                        (expected.project_id,),
                    ).fetchone()
                    if project_row is None:
                        raise AgentTaskRepositoryConflict("AgentTask project is missing.")
                    project = Project.model_validate_json(project_row[1])
                    if (
                        project.id != project_row[0]
                        or project.workspace_id != expected.workspace_id
                        or durable_approval.workspace_id != project.workspace_id
                    ):
                        raise AgentTaskRepositoryConflict(
                            "Linked approval, AgentTask, and project scope do not match."
                        )
                retry_count = conn.execute(
                    "SELECT COUNT(*) FROM kv_store WHERE entity_type='agent_task_event' "
                    "AND json_extract(payload,'$.task_id')=? "
                    "AND json_extract(payload,'$.metadata.operation_type')=?",
                    (expected.id, RETRY_REQUESTED_OPERATION),
                ).fetchone()[0]
                if retry_count >= max_retries:
                    raise AgentTaskRepositoryConflict(
                        f"Retry limit reached: {retry_count} >= {max_retries}."
                    )
                if event.metadata.get("previous_retry_count") != retry_count:
                    raise AgentTaskRepositoryConflict("Retry count changed before retry committed.")
                changed = conn.execute(
                    "UPDATE kv_store SET payload=? WHERE entity_type='agent_task' AND entity_id=? AND payload=?",
                    (updated.model_dump_json(), expected.id, row[1]),
                )
                if changed.rowcount != 1:
                    raise AgentTaskRepositoryConflict("AgentTask changed during retry.")
                conn.executemany(
                    "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
                    [
                        ("agent_task_event", event.id, event.model_dump_json()),
                        ("audit_event", audit.id, audit.model_dump_json()),
                    ],
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise AgentTaskRepositoryConflict("AgentTask retry record already exists.") from exc

    def commit_event(
        self, *, expected: AgentTask, updated: AgentTask, event: AgentTaskEvent,
        audit: AuditEvent, notification: Notification | None = None,
    ) -> None:
        _validate_event_commit(expected, updated, event, audit, notification)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT entity_id,payload FROM kv_store WHERE entity_type='agent_task' AND entity_id=?",
                    (expected.id,),
                ).fetchone()
                if row is None or row[0] != expected.id:
                    raise AgentTaskRepositoryConflict("AgentTask is missing from durable storage.")
                durable = AgentTask.model_validate_json(row[1])
                if durable != expected:
                    raise AgentTaskRepositoryConflict("AgentTask changed before event committed.")
                if expected.project_id:
                    project_row = conn.execute(
                        "SELECT payload FROM kv_store WHERE entity_type='project' AND entity_id=?",
                        (expected.project_id,),
                    ).fetchone()
                    if project_row is None:
                        raise AgentTaskRepositoryConflict("AgentTask project is missing.")
                    project = Project.model_validate_json(project_row[0])
                    if expected.workspace_id is not None and expected.workspace_id != project.workspace_id:
                        raise AgentTaskRepositoryConflict(
                            "AgentTask project ownership does not match its workspace."
                        )
                changed = conn.execute(
                    "UPDATE kv_store SET payload=? WHERE entity_type='agent_task' AND entity_id=? AND payload=?",
                    (updated.model_dump_json(), expected.id, row[1]),
                )
                if changed.rowcount != 1:
                    raise AgentTaskRepositoryConflict("AgentTask changed during event commit.")
                records = [
                    ("agent_task_event", event.id, event.model_dump_json()),
                    ("audit_event", audit.id, audit.model_dump_json()),
                ]
                if notification is not None:
                    records.append(("notification", notification.id, notification.model_dump_json()))
                conn.executemany(
                    "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
                    records,
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise AgentTaskRepositoryConflict("AgentTask event record already exists.") from exc

    def commit_worker_outcome(
        self, *, expected: AgentTask, updated: AgentTask,
        event: AgentTaskEvent | None, audit: AuditEvent,
    ) -> None:
        _validate_worker_outcome_commit(expected, updated, event, audit)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT entity_id,payload FROM kv_store WHERE entity_type='agent_task' AND entity_id=?",
                    (expected.id,),
                ).fetchone()
                if row is None or row[0] != expected.id:
                    raise AgentTaskRepositoryConflict("AgentTask is missing during worker outcome commit.")
                durable = AgentTask.model_validate_json(row[1])
                if durable != expected:
                    raise AgentTaskRepositoryConflict("AgentTask changed before worker outcome commit.")
                changed = conn.execute(
                    "UPDATE kv_store SET payload=? WHERE entity_type='agent_task' AND entity_id=? AND payload=?",
                    (updated.model_dump_json(), expected.id, row[1]),
                )
                if changed.rowcount != 1:
                    raise AgentTaskRepositoryConflict("AgentTask changed during worker outcome commit.")
                records = []
                if event is not None:
                    records.append(("agent_task_event", event.id, event.model_dump_json()))
                records.append(("audit_event", audit.id, audit.model_dump_json()))
                conn.executemany(
                    "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
                    records,
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise AgentTaskRepositoryConflict("Worker outcome record already exists.") from exc

    def claim(
        self, *, task_id: str | None, worker_id: str, lease_seconds: int,
    ) -> tuple[AgentTask | None, str, AuditEvent | None]:
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("A worker id is required.")
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds < 1:
            raise ValueError("Worker lease duration must be a positive integer number of seconds.")
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if task_id is None:
                rows = conn.execute(
                    "SELECT entity_id,payload FROM kv_store WHERE entity_type='agent_task'"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT entity_id,payload FROM kv_store WHERE entity_type='agent_task' AND entity_id=?",
                    (task_id,),
                ).fetchall()
            candidates: list[tuple[AgentTask, Project | None, str | None]] = []
            for entity_id, payload in rows:
                task = AgentTask.model_validate_json(payload)
                if task.id != entity_id:
                    raise AgentTaskRepositoryConflict("AgentTask identity does not match its durable key.")
                approval = None
                if task.approval_request_id:
                    approval_row = conn.execute(
                        "SELECT entity_id,payload FROM kv_store WHERE entity_type='approval_request' AND entity_id=?",
                        (task.approval_request_id,),
                    ).fetchone()
                    if approval_row is not None:
                        approval = ApprovalRequest.model_validate_json(approval_row[1])
                        if approval.id != approval_row[0]:
                            raise AgentTaskRepositoryConflict("Approval identity does not match its durable key.")
                project = None
                if task.project_id:
                    project_row = conn.execute(
                        "SELECT entity_id,payload FROM kv_store WHERE entity_type='project' AND entity_id=?",
                        (task.project_id,),
                    ).fetchone()
                    if project_row is not None:
                        project = Project.model_validate_json(project_row[1])
                        if project.id != project_row[0]:
                            raise AgentTaskRepositoryConflict("Project identity does not match its durable key.")
                failure = _worker_eligibility_failure(task, approval, project, now=now)
                candidates.append((task, project, failure))

            eligible = [candidate for candidate in candidates if candidate[2] is None]
            if task_id is None:
                if not eligible:
                    conn.commit()
                    return None, "No approved, linked, lease-available agent task found.", None
                task, project, _ = min(eligible, key=lambda candidate: candidate[0].updated_at)
            elif not candidates:
                conn.commit()
                return None, f"Agent task not found or unavailable: {task_id}", None
            else:
                task, project, failure = candidates[0]
                if failure:
                    refusal = _claim_refusal_audit(task, project, worker_id, failure)
                    conn.execute(
                        "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES('audit_event',?,?)",
                        (refusal.id, refusal.model_dump_json()),
                    )
                    conn.commit()
                    return task, failure, refusal

            updated = _with_worker_lease(task, worker_id, lease_seconds, now)
            changed = conn.execute(
                "UPDATE kv_store SET payload=? WHERE entity_type='agent_task' AND entity_id=? AND payload=?",
                (updated.model_dump_json(), task.id, task.model_dump_json()),
            )
            if changed.rowcount != 1:
                raise AgentTaskRepositoryConflict("AgentTask changed during worker claim.")
            claim_audit = _claim_success_audit(updated, project, worker_id)
            conn.execute(
                "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES('audit_event',?,?)",
                (claim_audit.id, claim_audit.model_dump_json()),
            )
            conn.commit()
            return updated, "", claim_audit


class PostgresAgentTaskRepository:
    """Workspace-scoped AgentTask operations over the normalized PostgreSQL schema."""

    def __init__(self, connect: Callable[[], psycopg.Connection]) -> None:
        self._connect = connect

    def create(self, *, task: AgentTask, event: AgentTaskEvent, audit: AuditEvent) -> None:
        with self._connect() as conn:
            self.create_in_transaction(conn, task=task, event=event, audit=audit)

    def create_in_transaction(
        self,
        conn: psycopg.Connection,
        *,
        task: AgentTask,
        event: AgentTaskEvent,
        audit: AuditEvent,
    ) -> None:
        """Write a task, its initial event, and audit using the caller's transaction."""
        if (
            not task.workspace_id
            or not task.project_id
            or task.id != event.task_id
            or audit.entity_id != task.id
            or audit.workspace_id != task.workspace_id
            or event.event_type.value != "TASK_CREATED"
            or audit.event_type not in {
                "agent_task.created",
                "agent_task.created_by_agent",
                "connector.webhook_task_created",
            }
        ):
            raise ValueError("PostgreSQL AgentTask creation requires matching workspace-scoped records.")
        project = conn.execute(
            "SELECT workspace_id FROM projects WHERE project_id=%s FOR KEY SHARE",
            (task.project_id,),
        ).fetchone()
        if project is None or project[0] != task.workspace_id:
            raise AgentTaskRepositoryConflict("AgentTask project is missing or outside its workspace.")
        try:
            conn.execute(
                "INSERT INTO agent_tasks(task_id,project_id,workspace_id,external_agent_id,"
                "approval_request_id,status,updated_at,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (task.id, task.project_id, task.workspace_id, task.external_agent_id,
                 task.approval_request_id, task.status.value, task.updated_at,
                 Jsonb(task.model_dump(mode="json"))),
            )
            conn.execute(
                "INSERT INTO agent_task_events(event_id,task_id,workspace_id,event_type,actor,created_at,payload) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (event.id, task.id, task.workspace_id, event.event_type.value, event.actor,
                 event.created_at, Jsonb(event.model_dump(mode="json"))),
            )
            conn.execute(
                "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,event_type,"
                "actor,details,created_at,payload) VALUES(%s,%s,NULL,'agent_task',%s,%s,%s,%s,%s,%s)",
                (audit.id, task.workspace_id, task.id, audit.event_type, audit.actor,
                 Jsonb(audit.details), audit.created_at, Jsonb(audit.model_dump(mode="json"))),
            )
        except psycopg.errors.UniqueViolation as exc:
            raise AgentTaskRepositoryConflict("AgentTask creation identifiers already exist.") from exc

    def get(self, task_id: str) -> AgentTask | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT task_id,workspace_id,project_id,payload FROM agent_tasks WHERE task_id=%s",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        task = AgentTask.model_validate(row[3])
        if task.id != row[0] or task.workspace_id != row[1] or task.project_id != row[2]:
            raise AgentTaskRepositoryConflict("AgentTask identity or ownership does not match its database key.")
        return task

    def list_for_approval(self, approval_request_id: str) -> list[AgentTask]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT task_id,workspace_id,project_id,payload FROM agent_tasks "
                "WHERE payload->>'approval_request_id'=%s ORDER BY task_id",
                (approval_request_id,),
            ).fetchall()
        tasks = [AgentTask.model_validate(row[3]) for row in rows]
        for task, row in zip(tasks, rows, strict=True):
            if (
                task.id != row[0]
                or task.workspace_id != row[1]
                or task.project_id != row[2]
                or task.approval_request_id != approval_request_id
            ):
                raise AgentTaskRepositoryConflict(
                    "AgentTask approval relationship does not match its database row."
                )
        return tasks

    def list(
        self,
        *,
        workspace_id: str | None = None,
        provider: AgentProvider | None = None,
        status: AgentTaskStatus | None = None,
        project_id: str | None = None,
        limit: int = 100,
    ) -> list[AgentTask]:
        if not 1 <= limit <= 500:
            raise ValueError("AgentTask list limit must be between 1 and 500.")
        clauses: list[str] = []
        params: list[object] = []
        if workspace_id is not None:
            clauses.append("workspace_id=%s")
            params.append(workspace_id)
        if provider is not None:
            clauses.append("payload->>'provider'=%s")
            params.append(provider.value)
        if status is not None:
            clauses.append("status=%s")
            params.append(status.value)
        if project_id is not None:
            clauses.append("project_id=%s")
            params.append(project_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT task_id,workspace_id,project_id,payload FROM agent_tasks"
                + where
                + " ORDER BY updated_at DESC,task_id DESC LIMIT %s",
                params,
            ).fetchall()
        tasks = [AgentTask.model_validate(row[3]) for row in rows]
        for task, row in zip(tasks, rows, strict=True):
            if task.id != row[0] or task.workspace_id != row[1] or task.project_id != row[2]:
                raise AgentTaskRepositoryConflict("AgentTask list returned mismatched identity or ownership.")
        return tasks

    def list_events(self, task_id: str, *, workspace_id: str | None = None) -> list[AgentTaskEvent]:
        clauses = ["task_id=%s"]
        params: list[object] = [task_id]
        if workspace_id is not None:
            clauses.append("workspace_id=%s")
            params.append(workspace_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT event_id,task_id,payload FROM agent_task_events WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at,event_id",
                params,
            ).fetchall()
        events = [AgentTaskEvent.model_validate(row[2]) for row in rows]
        if any(event.id != row[0] or event.task_id != row[1] for event, row in zip(events, rows, strict=True)):
            raise AgentTaskRepositoryConflict("AgentTask event identity does not match its database key.")
        return events

    def commit_cancel(
        self, *, expected: AgentTask, updated: AgentTask,
        event: AgentTaskEvent, audit: AuditEvent,
        expected_approval: ApprovalRequest | None = None,
        cancelled_approval: ApprovalRequest | None = None,
        approval_audit: AuditEvent | None = None,
        approval_notification: Notification | None = None,
    ) -> None:
        _validate_cancel_commit(
            expected, updated, event, audit, expected_approval,
            cancelled_approval, approval_audit, approval_notification,
        )
        if not expected.workspace_id or not expected.project_id:
            raise ValueError("PostgreSQL AgentTask cancellation requires persisted workspace ownership.")
        try:
            with self._connect() as conn:
                approval_row = None
                if expected_approval is not None:
                    approval_row = conn.execute(
                        "SELECT workspace_id,project_id,status,payload,version FROM approval_requests "
                        "WHERE approval_id=%s FOR UPDATE",
                        (expected_approval.id,),
                    ).fetchone()
                    if (
                        approval_row is None
                        or approval_row[0] != expected_approval.workspace_id
                        or approval_row[1] != expected_approval.project_id
                        or approval_row[2] != expected_approval.status.value
                    ):
                        raise AgentTaskRepositoryConflict("Linked approval is missing or outside its scope.")
                    durable_approval = ApprovalRequest.model_validate(approval_row[3])
                    if durable_approval != expected_approval:
                        raise AgentTaskRepositoryConflict("Linked approval changed before task cancellation committed.")
                row = conn.execute(
                    "SELECT workspace_id,project_id,payload FROM agent_tasks "
                    "WHERE task_id=%s FOR UPDATE",
                    (expected.id,),
                ).fetchone()
                if row is None or row[0] != expected.workspace_id or row[1] != expected.project_id:
                    raise AgentTaskRepositoryConflict("AgentTask is missing or outside its workspace.")
                durable = AgentTask.model_validate(row[2])
                if durable != expected:
                    raise AgentTaskRepositoryConflict("AgentTask changed before cancellation committed.")
                linked_run = conn.execute(
                    "SELECT run_id FROM agent_runs WHERE task_id=%s AND workspace_id=%s "
                    "AND status NOT IN ('FAILED','CANCELLED','BLOCKED') LIMIT 1",
                    (expected.id, expected.workspace_id),
                ).fetchone()
                if linked_run is not None:
                    raise AgentTaskRepositoryConflict(
                        "An AgentRun is linked to this task; cancel or reconcile the run before cancelling the task."
                    )
                updated_row = conn.execute(
                    "UPDATE agent_tasks SET status=%s,updated_at=%s,payload=%s "
                    "WHERE task_id=%s AND workspace_id=%s AND payload=%s",
                    (updated.status.value, updated.updated_at, Jsonb(updated.model_dump(mode="json")),
                     expected.id, expected.workspace_id, Jsonb(expected.model_dump(mode="json"))),
                )
                if updated_row.rowcount != 1:
                    raise AgentTaskRepositoryConflict("AgentTask changed during cancellation.")
                if cancelled_approval is not None:
                    approval_update = conn.execute(
                        "UPDATE approval_requests SET status=%s,updated_at=%s,payload=%s,version=version+1 "
                        "WHERE approval_id=%s AND status=%s AND version=%s",
                        (
                            cancelled_approval.status.value,
                            cancelled_approval.updated_at,
                            Jsonb(cancelled_approval.model_dump(mode="json")),
                            expected_approval.id,
                            expected_approval.status.value,
                            approval_row[4],
                        ),
                    )
                    if approval_update.rowcount != 1:
                        raise AgentTaskRepositoryConflict("Linked approval changed during task cancellation.")
                    conn.execute(
                        "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,event_type,"
                        "actor,details,created_at,payload) VALUES(%s,%s,%s,'approval_request',%s,%s,%s,%s,%s,%s)",
                        (
                            approval_audit.id,
                            approval_audit.workspace_id,
                            expected_approval.id,
                            approval_audit.entity_id,
                            approval_audit.event_type,
                            approval_audit.actor,
                            Jsonb(approval_audit.details),
                            approval_audit.created_at,
                            Jsonb(approval_audit.model_dump(mode="json")),
                        ),
                    )
                    conn.execute(
                        "INSERT INTO notifications(notification_id,workspace_id,user_id,approval_id,task_id,"
                        "entity_type,status,attempts,next_attempt_at,lease_expires_at,created_at,payload) "
                        "VALUES(%s,%s,%s,%s,NULL,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            approval_notification.id,
                            approval_notification.workspace_id,
                            approval_notification.user_id,
                            approval_notification.approval_request_id,
                            approval_notification.entity_type,
                            approval_notification.push_status,
                            approval_notification.push_attempts,
                            approval_notification.push_next_attempt_at,
                            approval_notification.push_lease_expires_at,
                            approval_notification.created_at,
                            Jsonb(approval_notification.model_dump(mode="json")),
                        ),
                    )
                conn.execute(
                    "INSERT INTO agent_task_events(event_id,task_id,workspace_id,event_type,actor,created_at,payload) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    (event.id, updated.id, updated.workspace_id, event.event_type.value,
                     event.actor, event.created_at, Jsonb(event.model_dump(mode="json"))),
                )
                conn.execute(
                    "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,event_type,"
                    "actor,details,created_at,payload) VALUES(%s,%s,NULL,'agent_task',%s,%s,%s,%s,%s,%s)",
                    (audit.id, audit.workspace_id, audit.entity_id, audit.event_type, audit.actor,
                     Jsonb(audit.details), audit.created_at, Jsonb(audit.model_dump(mode="json"))),
                )
        except psycopg.errors.UniqueViolation as exc:
            raise AgentTaskRepositoryConflict("AgentTask cancellation record already exists.") from exc

    def count_retries(self, task_id: str) -> int:
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM agent_task_events WHERE task_id=%s "
                "AND payload->'metadata'->>'operation_type'=%s",
                (task_id, RETRY_REQUESTED_OPERATION),
            ).fetchone()[0]

    def commit_retry(
        self, *, expected: AgentTask, updated: AgentTask, event: AgentTaskEvent,
        audit: AuditEvent, max_retries: int,
    ) -> None:
        _validate_retry_commit(expected, updated, event, audit, max_retries)
        if not expected.workspace_id or not expected.project_id:
            raise ValueError("PostgreSQL AgentTask retry requires persisted workspace ownership.")
        try:
            with self._connect() as conn:
                # Match approval decision and cancellation lock order:
                # approval first, then its linked task.
                approval_row = conn.execute(
                    "SELECT approval_id,project_id,workspace_id,status,payload "
                    "FROM approval_requests WHERE approval_id=%s FOR UPDATE",
                    (expected.approval_request_id,),
                ).fetchone()
                if approval_row is None:
                    raise AgentTaskRepositoryConflict("Linked approval request was not found.")
                durable_approval = ApprovalRequest.model_validate(approval_row[4])
                if (
                    approval_row[0] != expected.approval_request_id
                    or durable_approval.id != approval_row[0]
                    or approval_row[1] != expected.project_id
                    or durable_approval.project_id != expected.project_id
                    or approval_row[2] != expected.workspace_id
                    or durable_approval.workspace_id != expected.workspace_id
                    or approval_row[3] != ApprovalStatus.APPROVED.value
                    or durable_approval.status != ApprovalStatus.APPROVED
                    or not durable_approval.approved_payload_hash
                    or durable_approval.approved_payload_hash
                    != compute_approval_payload_hash(durable_approval)
                ):
                    raise AgentTaskRepositoryConflict(
                        "Linked approval must remain approved and match the AgentTask scope before retry."
                    )
                project_row = conn.execute(
                    "SELECT workspace_id FROM projects WHERE project_id=%s FOR KEY SHARE",
                    (expected.project_id,),
                ).fetchone()
                if project_row is None or project_row[0] != expected.workspace_id:
                    raise AgentTaskRepositoryConflict(
                        "Linked approval, AgentTask, and project scope do not match."
                    )
                row = conn.execute(
                    "SELECT workspace_id,project_id,payload FROM agent_tasks "
                    "WHERE task_id=%s FOR UPDATE",
                    (expected.id,),
                ).fetchone()
                if row is None or row[0] != expected.workspace_id or row[1] != expected.project_id:
                    raise AgentTaskRepositoryConflict("AgentTask is missing or outside its workspace.")
                durable = AgentTask.model_validate(row[2])
                if durable != expected:
                    raise AgentTaskRepositoryConflict("AgentTask changed before retry committed.")
                retry_count = conn.execute(
                    "SELECT COUNT(*) FROM agent_task_events WHERE task_id=%s "
                    "AND payload->'metadata'->>'operation_type'=%s",
                    (expected.id, RETRY_REQUESTED_OPERATION),
                ).fetchone()[0]
                if retry_count >= max_retries:
                    raise AgentTaskRepositoryConflict(
                        f"Retry limit reached: {retry_count} >= {max_retries}."
                    )
                if event.metadata.get("previous_retry_count") != retry_count:
                    raise AgentTaskRepositoryConflict("Retry count changed before retry committed.")
                changed = conn.execute(
                    "UPDATE agent_tasks SET status=%s,updated_at=%s,payload=%s "
                    "WHERE task_id=%s AND workspace_id=%s AND payload=%s",
                    (updated.status.value, updated.updated_at,
                     Jsonb(updated.model_dump(mode="json")), expected.id,
                     expected.workspace_id, Jsonb(expected.model_dump(mode="json"))),
                )
                if changed.rowcount != 1:
                    raise AgentTaskRepositoryConflict("AgentTask changed during retry.")
                conn.execute(
                    "INSERT INTO agent_task_events(event_id,task_id,workspace_id,event_type,actor,created_at,payload) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    (event.id, updated.id, updated.workspace_id, event.event_type.value,
                     event.actor, event.created_at, Jsonb(event.model_dump(mode="json"))),
                )
                conn.execute(
                    "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,event_type,"
                    "actor,details,created_at,payload) VALUES(%s,%s,NULL,'agent_task',%s,%s,%s,%s,%s,%s)",
                    (audit.id, audit.workspace_id, audit.entity_id, audit.event_type, audit.actor,
                     Jsonb(audit.details), audit.created_at, Jsonb(audit.model_dump(mode="json"))),
                )
        except psycopg.errors.UniqueViolation as exc:
            raise AgentTaskRepositoryConflict("AgentTask retry record already exists.") from exc

    def commit_event(
        self, *, expected: AgentTask, updated: AgentTask, event: AgentTaskEvent,
        audit: AuditEvent, notification: Notification | None = None,
    ) -> None:
        with self._connect() as conn:
            self.commit_event_in_transaction(
                conn,
                expected=expected,
                updated=updated,
                event=event,
                audit=audit,
                notification=notification,
            )

    def commit_event_in_transaction(
        self,
        conn: psycopg.Connection,
        *,
        expected: AgentTask,
        updated: AgentTask,
        event: AgentTaskEvent,
        audit: AuditEvent,
        notification: Notification | None = None,
    ) -> None:
        """Append an event and task audit using the caller's transaction."""
        _validate_event_commit(expected, updated, event, audit, notification)
        if not expected.workspace_id or not expected.project_id:
            raise ValueError("PostgreSQL AgentTask event requires persisted workspace ownership.")
        row = conn.execute(
            "SELECT workspace_id,project_id,payload,clock_timestamp() FROM agent_tasks "
            "WHERE task_id=%s FOR UPDATE",
            (expected.id,),
        ).fetchone()
        if row is None or row[0] != expected.workspace_id or row[1] != expected.project_id:
            raise AgentTaskRepositoryConflict("AgentTask is missing or outside its workspace.")
        durable = AgentTask.model_validate(row[2])
        if durable != expected:
            raise AgentTaskRepositoryConflict("AgentTask changed before event committed.")
        if (
            event.event_type == AgentTaskEventType.TESTS_STARTED
            and event.metadata.get("runner_type") == "validation_command_runner"
            and (
                durable.worker_lease_owner != event.metadata.get("worker_id")
                or durable.worker_lease_token != event.metadata.get("lease_token")
                or durable.worker_lease_expires_at is None
                or durable.worker_lease_expires_at <= row[3]
            )
        ):
            raise AgentTaskRepositoryConflict("Validation lease expired before its start event committed.")
        try:
            changed = conn.execute(
                "UPDATE agent_tasks SET status=%s,updated_at=%s,payload=%s "
                "WHERE task_id=%s AND workspace_id=%s AND payload=%s",
                (updated.status.value, updated.updated_at,
                 Jsonb(updated.model_dump(mode="json")), expected.id,
                 expected.workspace_id, Jsonb(expected.model_dump(mode="json"))),
            )
            if changed.rowcount != 1:
                raise AgentTaskRepositoryConflict("AgentTask changed during event commit.")
            conn.execute(
                "INSERT INTO agent_task_events(event_id,task_id,workspace_id,event_type,actor,created_at,payload) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (event.id, updated.id, updated.workspace_id, event.event_type.value,
                 event.actor, event.created_at, Jsonb(event.model_dump(mode="json"))),
            )
            conn.execute(
                "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,event_type,"
                "actor,details,created_at,payload) VALUES(%s,%s,NULL,'agent_task',%s,%s,%s,%s,%s,%s)",
                (audit.id, audit.workspace_id, audit.entity_id, audit.event_type, audit.actor,
                 Jsonb(audit.details), audit.created_at, Jsonb(audit.model_dump(mode="json"))),
            )
            if notification is not None:
                conn.execute(
                    "INSERT INTO notifications(notification_id,workspace_id,user_id,approval_id,task_id,"
                    "entity_type,status,attempts,next_attempt_at,lease_expires_at,created_at,payload) "
                    "VALUES(%s,%s,%s,NULL,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (notification.id, notification.workspace_id, notification.user_id,
                     updated.id, notification.entity_type, notification.push_status,
                     notification.push_attempts, notification.push_next_attempt_at,
                     notification.push_lease_expires_at, notification.created_at,
                     Jsonb(notification.model_dump(mode="json"))),
                )
        except psycopg.errors.UniqueViolation as exc:
            raise AgentTaskRepositoryConflict("AgentTask event record already exists.") from exc

    def commit_worker_outcome(
        self, *, expected: AgentTask, updated: AgentTask,
        event: AgentTaskEvent | None, audit: AuditEvent,
    ) -> None:
        _validate_worker_outcome_commit(expected, updated, event, audit)
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT workspace_id,project_id,payload,clock_timestamp() FROM agent_tasks "
                    "WHERE task_id=%s FOR UPDATE",
                    (expected.id,),
                ).fetchone()
                if row is None or row[0] != expected.workspace_id or row[1] != expected.project_id:
                    raise AgentTaskRepositoryConflict("AgentTask is missing or outside its workspace.")
                durable = AgentTask.model_validate(row[2])
                if durable != expected:
                    raise AgentTaskRepositoryConflict("AgentTask changed before worker outcome commit.")
                if (
                    durable.worker_lease_expires_at is None
                    or durable.worker_lease_expires_at <= row[3]
                ):
                    raise AgentTaskRepositoryConflict("Worker lease expired before outcome commit.")
                changed = conn.execute(
                    "UPDATE agent_tasks SET status=%s,updated_at=%s,payload=%s "
                    "WHERE task_id=%s AND workspace_id=%s AND payload=%s",
                    (updated.status.value, updated.updated_at,
                     Jsonb(updated.model_dump(mode="json")), expected.id,
                     expected.workspace_id, Jsonb(expected.model_dump(mode="json"))),
                )
                if changed.rowcount != 1:
                    raise AgentTaskRepositoryConflict("AgentTask changed during worker outcome commit.")
                if event is not None:
                    conn.execute(
                        "INSERT INTO agent_task_events(event_id,task_id,workspace_id,event_type,actor,created_at,payload) "
                        "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                        (event.id, updated.id, updated.workspace_id, event.event_type.value,
                         event.actor, event.created_at, Jsonb(event.model_dump(mode="json"))),
                    )
                conn.execute(
                    "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,event_type,"
                    "actor,details,created_at,payload) VALUES(%s,%s,NULL,'agent_task',%s,%s,%s,%s,%s,%s)",
                    (audit.id, audit.workspace_id, audit.entity_id, audit.event_type, audit.actor,
                     Jsonb(audit.details), audit.created_at, Jsonb(audit.model_dump(mode="json"))),
                )
        except psycopg.errors.UniqueViolation as exc:
            raise AgentTaskRepositoryConflict("Worker outcome record already exists.") from exc

    def recover_abandoned_validation_tasks(
        self, *, task_id: str | None = None,
    ) -> list[AgentTask]:
        """Fail closed for expired validation leases after a durable TESTS_STARTED event.

        This records an unknown outcome and fences the old lease. It never retries
        or executes a validation command.
        """
        recovered: list[AgentTask] = []
        with self._connect() as conn:
            if task_id is None:
                rows = conn.execute(
                    "SELECT task_id,workspace_id,project_id,payload FROM agent_tasks "
                    "WHERE status IN ('APPROVED','TESTING') "
                    "AND (payload->>'worker_lease_expires_at')::timestamptz <= clock_timestamp() "
                    "ORDER BY updated_at,task_id FOR UPDATE SKIP LOCKED"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT task_id,workspace_id,project_id,payload FROM agent_tasks "
                    "WHERE task_id=%s AND status IN ('APPROVED','TESTING') "
                    "AND (payload->>'worker_lease_expires_at')::timestamptz <= clock_timestamp() "
                    "FOR UPDATE",
                    (task_id,),
                ).fetchall()
            for row in rows:
                task = AgentTask.model_validate(row[3])
                if task.id != row[0] or task.workspace_id != row[1] or task.project_id != row[2]:
                    raise AgentTaskRepositoryConflict(
                        "AgentTask identity or ownership does not match its database key."
                    )
                if not task.workspace_id or not task.project_id or not task.approval_request_id:
                    continue
                started_row = conn.execute(
                    "SELECT event_id,payload FROM agent_task_events WHERE task_id=%s AND workspace_id=%s "
                    "AND event_type='TESTS_STARTED' "
                    "AND payload->'metadata'->>'runner_type'='validation_command_runner' "
                    "AND payload->'metadata'->>'lease_token'=%s "
                    "ORDER BY created_at DESC,event_id DESC LIMIT 1",
                    (task.id, task.workspace_id, task.worker_lease_token),
                ).fetchone()
                if started_row is None:
                    continue
                started = AgentTaskEvent.model_validate(started_row[1])
                if (
                    started.id != started_row[0]
                    or started.task_id != task.id
                    or started.event_type != AgentTaskEventType.TESTS_STARTED
                    or started.metadata.get("runner_type") != "validation_command_runner"
                    or started.metadata.get("lease_token") != task.worker_lease_token
                    or started.metadata.get("approval_request_id") != task.approval_request_id
                ):
                    raise AgentTaskRepositoryConflict(
                        "Validation recovery start event does not match the expired task lease."
                    )
                now = datetime.now(timezone.utc)
                failed = AgentTaskEvent(
                    task_id=task.id,
                    event_type=AgentTaskEventType.FAILED,
                    status=AgentTaskStatus.FAILED,
                    actor="agent-worker-validation-recovery",
                    message="Validation worker lease expired after tests started; execution outcome is unknown.",
                    metadata={
                        "worker_id": started.metadata.get("worker_id"),
                        "lease_token": task.worker_lease_token,
                        "approval_request_id": task.approval_request_id,
                        "planned_command_count": started.metadata.get("command_count"),
                        "completed_command_count": None,
                        "results": None,
                        "results_available": False,
                        "validation_policy": started.metadata.get("validation_policy", {}),
                        "runner_type": "validation_command_runner",
                        "failure_class": "WorkerProcessLost",
                        "execution_outcome": "UNKNOWN",
                    },
                    created_at=now,
                )
                updated = task.model_copy(deep=True)
                updated.status = AgentTaskStatus.FAILED
                updated.worker_lease_owner = None
                updated.worker_lease_expires_at = None
                updated.worker_lease_token = None
                updated.updated_at = now
                audit = AuditEvent(
                    event_type="agent_worker.validation_worker_lost",
                    entity_id=task.id,
                    workspace_id=task.workspace_id,
                    actor="agent-worker-validation-recovery",
                    details={
                        "event_id": failed.id,
                        "approval_request_id": task.approval_request_id,
                        "commands_completed": None,
                        "completed_command_count_known": False,
                        "failure_class": "WorkerProcessLost",
                        "execution_outcome": "UNKNOWN",
                        "worker_lease_cleared": True,
                    },
                    created_at=now,
                )
                changed = conn.execute(
                    "UPDATE agent_tasks SET status=%s,updated_at=%s,payload=%s "
                    "WHERE task_id=%s AND workspace_id=%s AND payload=%s",
                    (updated.status.value, updated.updated_at,
                     Jsonb(updated.model_dump(mode="json")), task.id,
                     task.workspace_id, Jsonb(task.model_dump(mode="json"))),
                )
                if changed.rowcount != 1:
                    raise AgentTaskRepositoryConflict("AgentTask changed during validation recovery.")
                conn.execute(
                    "INSERT INTO agent_task_events(event_id,task_id,workspace_id,event_type,actor,created_at,payload) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    (failed.id, task.id, task.workspace_id, failed.event_type.value,
                     failed.actor, failed.created_at, Jsonb(failed.model_dump(mode="json"))),
                )
                conn.execute(
                    "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,event_type,"
                    "actor,details,created_at,payload) VALUES(%s,%s,NULL,'agent_task',%s,%s,%s,%s,%s,%s)",
                    (audit.id, audit.workspace_id, audit.entity_id, audit.event_type,
                     audit.actor, Jsonb(audit.details), audit.created_at,
                     Jsonb(audit.model_dump(mode="json"))),
                )
                recovered.append(updated)
        return recovered

    def claim(
        self, *, task_id: str | None, worker_id: str, lease_seconds: int,
    ) -> tuple[AgentTask | None, str, AuditEvent | None]:
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("A worker id is required.")
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds < 1:
            raise ValueError("Worker lease duration must be a positive integer number of seconds.")
        now = datetime.now(timezone.utc)
        try:
            with self._connect() as conn:
                if task_id is None:
                    rows = conn.execute(
                        "SELECT task.task_id,task.workspace_id,task.project_id,task.payload "
                        "FROM agent_tasks AS task "
                        "JOIN approval_requests AS approval ON approval.approval_id=task.approval_request_id "
                        "AND approval.workspace_id=task.workspace_id AND approval.project_id=task.project_id "
                        "JOIN projects AS project ON project.project_id=task.project_id "
                        "AND project.workspace_id=task.workspace_id "
                        "WHERE task.status='APPROVED' AND approval.status='APPROVED' "
                        "AND (task.payload->>'worker_lease_owner' IS NULL OR "
                        "((task.payload->>'worker_lease_expires_at')::timestamptz <= %s)) "
                        "ORDER BY task.updated_at,task.task_id LIMIT 1 FOR UPDATE OF task SKIP LOCKED",
                        (now,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT task_id,workspace_id,project_id,payload FROM agent_tasks "
                        "WHERE task_id=%s FOR UPDATE",
                        (task_id,),
                    ).fetchall()
                if not rows:
                    if task_id is None:
                        return None, "No approved, linked, lease-available agent task found.", None
                    return None, f"Agent task not found or unavailable: {task_id}", None

                candidates: list[tuple[AgentTask, Project | None, str | None]] = []
                for row in rows:
                    task = AgentTask.model_validate(row[3])
                    if task.id != row[0] or task.workspace_id != row[1] or task.project_id != row[2]:
                        raise AgentTaskRepositoryConflict("AgentTask identity or ownership does not match its database key.")
                    approval = None
                    if task.approval_request_id:
                        approval_row = conn.execute(
                            "SELECT payload FROM approval_requests WHERE approval_id=%s "
                            "AND workspace_id=%s FOR SHARE",
                            (task.approval_request_id, task.workspace_id),
                        ).fetchone()
                        if approval_row is not None:
                            approval = ApprovalRequest.model_validate(approval_row[0])
                    project = None
                    if task.project_id:
                        project_row = conn.execute(
                            "SELECT project_id,workspace_id,payload FROM projects WHERE project_id=%s",
                            (task.project_id,),
                        ).fetchone()
                        if project_row is not None:
                            project = Project.model_validate(project_row[2])
                            if project.id != project_row[0] or project.workspace_id != project_row[1]:
                                raise AgentTaskRepositoryConflict("Project identity or ownership does not match its database key.")
                    failure = _worker_eligibility_failure(task, approval, project, now=now)
                    candidates.append((task, project, failure))

                eligible = [candidate for candidate in candidates if candidate[2] is None]
                refusal: AuditEvent | None = None
                if task_id is None:
                    if not eligible:
                        return None, "No approved, linked, lease-available agent task found.", None
                    task, project, _ = eligible[0]
                else:
                    task, project, failure = candidates[0]
                    if failure:
                        refusal = _claim_refusal_audit(task, project, worker_id, failure)
                        if not refusal.workspace_id:
                            raise AgentTaskRepositoryConflict("Cannot audit a worker claim refusal without workspace ownership.")
                        conn.execute(
                            "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,event_type,"
                            "actor,details,created_at,payload) VALUES(%s,%s,NULL,'agent_task',%s,%s,%s,%s,%s,%s)",
                            (refusal.id, refusal.workspace_id, task.id, refusal.event_type, refusal.actor,
                             Jsonb(refusal.details), refusal.created_at, Jsonb(refusal.model_dump(mode="json"))),
                        )
                        return task, failure, refusal

                updated = _with_worker_lease(task, worker_id, lease_seconds, now)
                changed = conn.execute(
                    "UPDATE agent_tasks SET updated_at=%s,payload=%s WHERE task_id=%s "
                    "AND workspace_id=%s AND payload=%s",
                    (updated.updated_at, Jsonb(updated.model_dump(mode="json")), task.id,
                     task.workspace_id, Jsonb(task.model_dump(mode="json"))),
                )
                if changed.rowcount != 1:
                    raise AgentTaskRepositoryConflict("AgentTask changed during worker claim.")
                claim_audit = _claim_success_audit(updated, project, worker_id)
                if not claim_audit.workspace_id:
                    raise AgentTaskRepositoryConflict("Cannot audit a worker claim without workspace ownership.")
                conn.execute(
                    "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,event_type,"
                    "actor,details,created_at,payload) VALUES(%s,%s,NULL,'agent_task',%s,%s,%s,%s,%s,%s)",
                    (claim_audit.id, claim_audit.workspace_id, updated.id, claim_audit.event_type,
                     claim_audit.actor, Jsonb(claim_audit.details), claim_audit.created_at,
                     Jsonb(claim_audit.model_dump(mode="json"))),
                )
                return updated, "", claim_audit
        except psycopg.errors.UniqueViolation as exc:
            raise AgentTaskRepositoryConflict("Worker claim refusal audit already exists.") from exc

    def renew_validation_lease(
        self, *, task: AgentTask, worker_id: str, lease_token: str, lease_seconds: int,
    ) -> AgentTask:
        if not task.workspace_id or not task.project_id:
            raise ValueError("Validation lease renewal requires persisted workspace ownership.")
        if not worker_id.strip() or not lease_token:
            raise ValueError("Validation lease renewal requires worker and lease identities.")
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds < 1:
            raise ValueError("Worker lease duration must be a positive integer number of seconds.")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT workspace_id,project_id,payload,clock_timestamp() FROM agent_tasks "
                "WHERE task_id=%s FOR UPDATE",
                (task.id,),
            ).fetchone()
            if row is None or row[0] != task.workspace_id or row[1] != task.project_id:
                raise AgentTaskRepositoryConflict("AgentTask is missing or outside its workspace.")
            durable = AgentTask.model_validate(row[2])
            db_now = row[3]
            if (
                durable != task
                or durable.status not in {AgentTaskStatus.APPROVED, AgentTaskStatus.TESTING}
                or durable.worker_lease_owner != worker_id
                or durable.worker_lease_token != lease_token
                or durable.worker_lease_expires_at is None
                or durable.worker_lease_expires_at <= db_now
            ):
                raise AgentTaskRepositoryConflict(
                    "Validation lease is stale, expired, or owned by another worker."
                )
            updated = durable.model_copy(deep=True)
            updated.worker_lease_expires_at = db_now + timedelta(seconds=lease_seconds)
            updated.updated_at = db_now
            changed = conn.execute(
                "UPDATE agent_tasks SET updated_at=%s,payload=%s WHERE task_id=%s "
                "AND workspace_id=%s AND payload=%s",
                (updated.updated_at, Jsonb(updated.model_dump(mode="json")), updated.id,
                 updated.workspace_id, Jsonb(durable.model_dump(mode="json"))),
            )
            if changed.rowcount != 1:
                raise AgentTaskRepositoryConflict("AgentTask changed during validation lease renewal.")
            audit = AuditEvent(
                event_type="agent_worker.validation_lease_renewed",
                entity_id=updated.id,
                workspace_id=updated.workspace_id,
                actor=worker_id,
                details={
                    "worker_id": worker_id,
                    "lease_expires_at": updated.worker_lease_expires_at.isoformat(),
                    "lease_token_validated": True,
                },
                created_at=db_now,
            )
            conn.execute(
                "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,event_type,"
                "actor,details,created_at,payload) VALUES(%s,%s,NULL,'agent_task',%s,%s,%s,%s,%s,%s)",
                (audit.id, audit.workspace_id, audit.entity_id, audit.event_type, audit.actor,
                 Jsonb(audit.details), audit.created_at, Jsonb(audit.model_dump(mode="json"))),
            )
            return updated
