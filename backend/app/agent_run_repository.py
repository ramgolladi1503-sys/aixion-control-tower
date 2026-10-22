from __future__ import annotations

import sqlite3
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Protocol

import psycopg
from psycopg import Connection
from psycopg.types.json import Jsonb

from .agent_run_models import (
    AgentRun,
    AgentRunEvent,
    AgentRunEventType,
    AgentRunStatus,
    AgentRunStep,
    AgentRunStepStatus,
)
from .agent_run_state_machine import can_transition_run, can_transition_step
from .agent_task_models import AgentTask, AgentTaskStatus
from .approval_integrity import compute_approval_payload_hash
from .models import ApprovalRequest, ApprovalStatus, AuditEvent, Project
from .postgres_trust_repository import PostgresTrustRepository
from .trust_crypto import verify_payload_signature
from .trust_models import CapabilityLeaseStatus
from .trust_policy import lease_receipt_payload


class AgentRunRepositoryIntegrityError(RuntimeError):
    """A durable AgentRun row does not match its indexed identity or scope."""


class AgentRunRepositoryConflict(RuntimeError):
    """A durable AgentRun creation conflicts with existing or inconsistent state."""


class AgentRunRepositoryNotFound(AgentRunRepositoryConflict):
    """A workspace-scoped durable AgentRun or lease is not visible."""


class AgentRunRepository(Protocol):
    def get(self, run_id: str) -> AgentRun | None: ...

    def list_for_workspace(
        self,
        *,
        workspace_id: str | None,
        status: str | None = None,
        task_id: str | None = None,
        limit: int = 100,
    ) -> list[AgentRun]: ...

    def list_for_task(self, task_id: str) -> list[AgentRun]: ...

    def has_run_scheduled_audit(
        self, *, task_id: str, run_id: str, workspace_id: str
    ) -> bool: ...

    def record_run_scheduled_audit(self, event: AuditEvent) -> bool: ...

    def get_active_for_task(self, task_id: str) -> AgentRun | None: ...

    def list_steps_for_run(self, run_id: str) -> list[AgentRunStep]: ...

    def list_steps_for_runs(self, runs: list[AgentRun]) -> list[AgentRunStep]: ...

    def list_events_for_run(self, run_id: str) -> list[AgentRunEvent]: ...

    def list_expired_leases(
        self,
        *,
        now: datetime,
        workspace_id: str | None = None,
        limit: int = 100,
    ) -> list[tuple[AgentRun, AgentRunStep | None]]: ...

    def acquire_lease(
        self,
        *,
        run_id: str,
        worker_id: str,
        lease_seconds: int,
        now: datetime,
    ) -> tuple[AgentRun, AgentRunEvent | None, AuditEvent | None]: ...

    def commit_step_completion(
        self,
        *,
        expected_run: AgentRun,
        updated_run: AgentRun,
        expected_steps: list[AgentRunStep],
        updated_steps: list[AgentRunStep],
        events: list[AgentRunEvent],
        audit: AuditEvent,
        now: datetime,
    ) -> None: ...

    def recover_expired_lease(
        self,
        *,
        expected_run: AgentRun,
        updated_run: AgentRun,
        expected_step: AgentRunStep | None,
        updated_step: AgentRunStep | None,
        event: AgentRunEvent,
        audit: AuditEvent,
        now: datetime,
    ) -> None: ...

    def update_lease(
        self,
        *,
        run_id: str,
        worker_id: str,
        lease_token: str,
        lease_seconds: int | None,
        now: datetime,
    ) -> tuple[AgentRun, AgentRunStep | None, AgentRunEvent, AuditEvent]: ...

    def commit_control_transition(
        self,
        *,
        expected_run: AgentRun,
        updated_run: AgentRun,
        expected_steps: list[AgentRunStep],
        updated_steps: list[AgentRunStep],
        events: list[AgentRunEvent],
        audits: list[AuditEvent],
    ) -> None: ...

    def attach_capability_lease(
        self, *, run_id: str, workspace_id: str, lease_id: str, actor: str,
        now: datetime,
    ) -> AgentRun: ...

    def create(
        self,
        *,
        run: AgentRun,
        steps: list[AgentRunStep],
        events: list[AgentRunEvent],
        audit: AuditEvent,
        scheduled_audit: AuditEvent | None = None,
    ) -> None: ...


def _validate_run_creation(
    *,
    run: AgentRun,
    steps: list[AgentRunStep],
    events: list[AgentRunEvent],
    audit: AuditEvent,
    scheduled_audit: AuditEvent | None = None,
) -> None:
    ordered_steps = sorted(steps, key=lambda item: item.sequence)
    if (
        not steps
        or len({step.id for step in steps}) != len(steps)
        or [step.sequence for step in ordered_steps] != list(range(len(steps)))
        or ordered_steps[0].id != run.current_step_id
        or any(step.run_id != run.id or step.task_id != run.task_id for step in steps)
        or any(event.run_id != run.id or event.task_id != run.task_id for event in events)
        or len({event.id for event in events}) != len(events)
        or audit.event_type != "agent_run.created"
        or audit.entity_id != run.id
        or audit.workspace_id != run.workspace_id
    ):
        raise ValueError("AgentRun creation records do not share a valid identity and scope")
    if scheduled_audit is not None and (
        scheduled_audit.event_type != "agent_task.run_scheduled"
        or scheduled_audit.entity_id != run.task_id
        or scheduled_audit.workspace_id != run.workspace_id
        or scheduled_audit.details.get("agent_run_id") != run.id
        or scheduled_audit.details.get("approval_request_id") != run.approval_request_id
    ):
        raise ValueError("Run-scheduled audit does not match the AgentRun identity and scope")


def _validate_created_task_scope(
    *,
    run: AgentRun,
    task_payload: object,
    project_payload: object | None,
    approval_payload: object | None,
) -> None:
    try:
        task = AgentTask.model_validate_json(task_payload) if isinstance(
            task_payload, (str, bytes)
        ) else AgentTask.model_validate(task_payload)
        project = (
            Project.model_validate_json(project_payload)
            if isinstance(project_payload, (str, bytes))
            else Project.model_validate(project_payload)
        ) if project_payload is not None else None
        approval = (
            ApprovalRequest.model_validate_json(approval_payload)
            if isinstance(approval_payload, (str, bytes))
            else ApprovalRequest.model_validate(approval_payload)
        ) if approval_payload is not None else None
    except Exception as error:  # pragma: no cover - guard keeps validation failure explicit
        raise AgentRunRepositoryIntegrityError("Run parent payload is invalid") from error
    if (
        task.id != run.task_id
        or task.status != AgentTaskStatus.APPROVED
        or project is None
        or project.id != task.project_id
        or (task.workspace_id is not None and task.workspace_id != project.workspace_id)
        or run.project_id != project.id
        or (run.workspace_id is not None and run.workspace_id != project.workspace_id)
        or task.approval_request_id != run.approval_request_id
        or approval is None
        or approval.id != run.approval_request_id
        or approval.status != ApprovalStatus.APPROVED
        or approval.project_id != project.id
        or (approval.workspace_id is not None and approval.workspace_id != project.workspace_id)
    ):
        raise AgentRunRepositoryConflict("Approved task, approval, project, and run scopes must match")


def _validate_run(
    *,
    payload: object,
    row_id: str,
    workspace_id: str | None,
    task_id: str | None,
    project_id: str | None,
) -> AgentRun:
    try:
        run = (
            AgentRun.model_validate_json(payload)
            if isinstance(payload, (str, bytes))
            else AgentRun.model_validate(payload)
        )
    except Exception as error:
        raise AgentRunRepositoryIntegrityError("Stored AgentRun payload is invalid") from error
    if (
        run.id != row_id
        or (workspace_id is not None and run.workspace_id != workspace_id)
        or (task_id is not None and run.task_id != task_id)
        or (project_id is not None and run.project_id != project_id)
    ):
        raise AgentRunRepositoryIntegrityError("Stored AgentRun identity or scope is inconsistent")
    return run


def _lease_result(
    run: AgentRun,
    *,
    worker_id: str,
    lease_seconds: int,
    now: datetime,
) -> tuple[AgentRun, AgentRunEvent | None, AuditEvent | None]:
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise ValueError("A worker id is required")
    if (
        not isinstance(lease_seconds, int)
        or isinstance(lease_seconds, bool)
        or not 1 <= lease_seconds <= 3600
    ):
        raise ValueError("Run lease duration must be between 1 and 3600 seconds")
    if run.status.value in {"BLOCKED", "SUCCEEDED", "FAILED", "CANCELLED"}:
        raise AgentRunRepositoryConflict(f"Terminal run cannot acquire a lease: {run.status}")
    if run.status.value == "PAUSED" or run.pause_requested or run.cancel_requested:
        raise AgentRunRepositoryConflict("Paused or cancellation-requested run cannot acquire a lease")
    if run.lease_owner and run.lease_expires_at and run.lease_expires_at > now:
        raise AgentRunRepositoryConflict(
            f"Run is already leased by {run.lease_owner} until {run.lease_expires_at.isoformat()}"
        )
    token = secrets.token_urlsafe(24)
    updated = run.model_copy(
        update={
            "lease_owner": worker_id,
            "lease_token": token,
            "heartbeat_at": now,
            "lease_expires_at": now + timedelta(seconds=lease_seconds),
            "updated_at": now,
        }
    )
    event = AgentRunEvent(
        run_id=run.id,
        task_id=run.task_id,
        correlation_id=run.correlation_id,
        event_type=AgentRunEventType.RUN_LEASE_ACQUIRED,
        message="Worker acquired the durable run lease.",
        actor=worker_id,
        metadata={"lease_seconds": lease_seconds},
    )
    audit = AuditEvent(
        event_type="agent_run.lease_acquired",
        entity_id=run.id,
        workspace_id=run.workspace_id,
        actor=worker_id,
        details={"event_id": event.id, "lease_seconds": lease_seconds},
    )
    return updated, event, audit


def _validate_step_completion(
    *,
    expected_run: AgentRun,
    updated_run: AgentRun,
    expected_steps: list[AgentRunStep],
    updated_steps: list[AgentRunStep],
    events: list[AgentRunEvent],
    audit: AuditEvent,
    now: datetime,
) -> None:
    expected_by_id = {step.id: step for step in expected_steps}
    updated_by_id = {step.id: step for step in updated_steps}
    completion_step = expected_by_id.get(str(audit.details.get("step_id", "")))
    if expected_run.lease_expires_at is None or expected_run.lease_expires_at <= now:
        raise AgentRunRepositoryConflict("AgentRun lease expired before step completion")
    if (
        expected_run.id != updated_run.id
        or expected_run.task_id != updated_run.task_id
        or expected_run.workspace_id != updated_run.workspace_id
        or not expected_run.lease_token
        or not expected_run.lease_owner
        or completion_step is None
        or completion_step.id != expected_run.current_step_id
        or completion_step.status != AgentRunStepStatus.RUNNING
        or completion_step.lease_owner != expected_run.lease_owner
        or completion_step.lease_token != expected_run.lease_token
        or completion_step.lease_expires_at != expected_run.lease_expires_at
        or updated_run.lease_token is not None
        or updated_run.lease_owner is not None
        or updated_run.lease_expires_at is not None
        or not expected_by_id
        or set(expected_by_id) != set(updated_by_id)
        or any(
            expected.run_id != expected_run.id
            or expected.task_id != expected_run.task_id
            or updated_by_id[step_id].run_id != expected_run.id
            or updated_by_id[step_id].task_id != expected_run.task_id
            for step_id, expected in expected_by_id.items()
        )
        or any(event.run_id != expected_run.id or event.task_id != expected_run.task_id for event in events)
        or len({event.id for event in events}) != len(events)
        or audit.event_type != "agent_run.step_completed"
        or audit.entity_id != expected_run.id
        or audit.workspace_id != expected_run.workspace_id
        or audit.details.get("step_id") not in expected_by_id
    ):
        raise ValueError("AgentRun step completion records do not share a valid identity and scope")


def _validate_lease_recovery(
    *,
    expected_run: AgentRun,
    updated_run: AgentRun,
    expected_step: AgentRunStep | None,
    updated_step: AgentRunStep | None,
    event: AgentRunEvent,
    audit: AuditEvent,
    now: datetime,
) -> None:
    if (
        expected_run.id != updated_run.id
        or expected_run.task_id != updated_run.task_id
        or expected_run.workspace_id != updated_run.workspace_id
        or expected_run.lease_expires_at is None
        or expected_run.lease_expires_at > now
        or updated_run.lease_owner is not None
        or updated_run.lease_token is not None
        or updated_run.lease_expires_at is not None
        or updated_run.heartbeat_at is not None
        or (expected_step is None) != (updated_step is None)
        or expected_run.current_step_id != (expected_step.id if expected_step else None)
        or (expected_step is not None and updated_step is not None and (
            expected_step.id != updated_step.id
            or expected_step.run_id != expected_run.id
            or expected_step.task_id != expected_run.task_id
            or updated_step.run_id != expected_run.id
            or updated_step.task_id != expected_run.task_id
        ))
        or event.run_id != expected_run.id
        or event.task_id != expected_run.task_id
        or event.event_type != AgentRunEventType.STALE_LEASE_RECOVERED
        or event.step_id != (expected_step.id if expected_step else None)
        or audit.event_type != "agent_run.stale_lease_recovered"
        or audit.entity_id != expected_run.id
        or audit.workspace_id != expected_run.workspace_id
    ):
        raise ValueError("Expired AgentRun lease recovery records do not share a valid identity and scope")


def _lease_update_result(
    run: AgentRun,
    step: AgentRunStep | None,
    *,
    worker_id: str,
    lease_token: str,
    lease_seconds: int | None,
    now: datetime,
) -> tuple[AgentRun, AgentRunStep | None, AgentRunEvent, AuditEvent]:
    if not isinstance(worker_id, str) or not worker_id.strip() or not isinstance(lease_token, str) or not lease_token:
        raise ValueError("Worker id and lease token are required")
    if (
        run.lease_owner != worker_id
        or run.lease_token != lease_token
        or run.lease_expires_at is None
        or run.lease_expires_at <= now
    ):
        raise AgentRunRepositoryConflict("Lease update rejected because owner, token, or expiry does not match")
    if lease_seconds is not None and run.status.value in {"BLOCKED", "SUCCEEDED", "FAILED", "CANCELLED"}:
        raise AgentRunRepositoryConflict("Terminal runs cannot renew a lease")
    if lease_seconds is not None and (
        not isinstance(lease_seconds, int)
        or isinstance(lease_seconds, bool)
        or not 1 <= lease_seconds <= 3600
    ):
        raise ValueError("Run lease duration must be between 1 and 3600 seconds")

    if lease_seconds is None:
        updated_run = run.model_copy(update={
            "lease_owner": None,
            "lease_token": None,
            "lease_expires_at": None,
            "heartbeat_at": None,
            "updated_at": now,
        })
        event_type = AgentRunEventType.RUN_LEASE_RELEASED
        event_message = "Worker released the Mission Control lease."
        audit_type = "agent_run.lease_released"
        updated_step = None
        if step and step.lease_owner == worker_id and step.lease_token == lease_token:
            updated_step = step.model_copy(update={
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at": None,
                "updated_at": now,
            })
        event_step_id = None
        details = {"worker_id": worker_id}
    else:
        updated_run = run.model_copy(update={
            "heartbeat_at": now,
            "lease_expires_at": now + timedelta(seconds=lease_seconds),
            "updated_at": now,
        })
        event_type = AgentRunEventType.RUN_HEARTBEAT
        event_message = "Worker renewed the Mission Control lease."
        audit_type = "agent_run.lease_heartbeat"
        updated_step = None
        if step and step.lease_owner == worker_id and step.lease_token == lease_token:
            updated_step = step.model_copy(update={
                "heartbeat_at": now,
                "lease_expires_at": updated_run.lease_expires_at,
                "updated_at": now,
            })
        event_step_id = step.id if step else None
        details = {"worker_id": worker_id, "lease_seconds": lease_seconds}

    event = AgentRunEvent(
        run_id=run.id,
        task_id=run.task_id,
        step_id=event_step_id,
        correlation_id=run.correlation_id,
        event_type=event_type,
        message=event_message,
        actor=worker_id,
        metadata=details,
    )
    audit = AuditEvent(
        event_type=audit_type,
        entity_id=run.id,
        workspace_id=run.workspace_id,
        actor=worker_id,
        details={"event_id": event.id, **details},
    )
    return updated_run, updated_step, event, audit


def _validate_control_transition(
    *,
    expected_run: AgentRun,
    updated_run: AgentRun,
    expected_steps: list[AgentRunStep],
    updated_steps: list[AgentRunStep],
    events: list[AgentRunEvent],
    audits: list[AuditEvent],
) -> None:
    expected_by_id = {step.id: step for step in expected_steps}
    updated_by_id = {step.id: step for step in updated_steps}
    if (
        expected_run.id != updated_run.id
        or expected_run.task_id != updated_run.task_id
        or expected_run.workspace_id != updated_run.workspace_id
        or len(expected_by_id) != len(expected_steps)
        or set(expected_by_id) != set(updated_by_id)
        or len(updated_by_id) != len(updated_steps)
        or not can_transition_run(expected_run.status, updated_run.status)
        or any(
            step.run_id != expected_run.id
            or step.task_id != expected_run.task_id
            or updated_by_id[step_id].run_id != expected_run.id
            or updated_by_id[step_id].task_id != expected_run.task_id
            or not (
                can_transition_step(step.status, updated_by_id[step_id].status)
                or (
                    step.status == "FAILED"
                    and updated_by_id[step_id].status == "READY"
                )
            )
            for step_id, step in expected_by_id.items()
        )
        or not events
        or any(event.run_id != expected_run.id or event.task_id != expected_run.task_id for event in events)
        or len({event.id for event in events}) != len(events)
        or not audits
        or any(
            audit.entity_id != expected_run.id
            or audit.workspace_id != expected_run.workspace_id
            for audit in audits
        )
        or len({audit.id for audit in audits}) != len(audits)
    ):
        raise ValueError("AgentRun control transition records do not share a valid identity and scope")


class SQLiteAgentRunRepository:
    """Read AgentRun records from SQLite without depending on process maps."""

    def __init__(self, connect: Callable[[], sqlite3.Connection]) -> None:
        self._connect = connect

    def get(self, run_id: str) -> AgentRun | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT entity_id,payload FROM kv_store "
                "WHERE entity_type=? AND entity_id=?",
                ("agent_run", run_id),
            ).fetchone()
        if row is None:
            return None
        return _validate_run(
            payload=row[1],
            row_id=row[0],
            workspace_id=None,
            task_id=None,
            project_id=None,
        )

    def list_for_workspace(
        self,
        *,
        workspace_id: str | None,
        status: str | None = None,
        task_id: str | None = None,
        limit: int = 100,
    ) -> list[AgentRun]:
        if not 1 <= limit <= 500:
            raise ValueError("AgentRun query limit must be between 1 and 500")
        clauses = ["entity_type='agent_run'"]
        params: list[object] = []
        if workspace_id is not None:
            clauses.append("json_extract(payload,'$.workspace_id')=?")
            params.append(workspace_id)
        if status is not None:
            clauses.append("json_extract(payload,'$.status')=?")
            params.append(status)
        if task_id is not None:
            clauses.append("json_extract(payload,'$.task_id')=?")
            params.append(task_id)
        query = (
            "SELECT entity_id,payload FROM kv_store WHERE "
            + " AND ".join(clauses)
            + " ORDER BY json_extract(payload,'$.updated_at') DESC,entity_id LIMIT ?"
        )
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            _validate_run(
                payload=row[1], row_id=row[0], workspace_id=workspace_id,
                task_id=task_id, project_id=None,
            )
            for row in rows
        ]

    def list_for_task(self, task_id: str) -> list[AgentRun]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT entity_id,payload FROM kv_store WHERE entity_type='agent_run' "
                "AND json_extract(payload,'$.task_id')=? "
                "ORDER BY json_extract(payload,'$.created_at'),entity_id",
                (task_id,),
            ).fetchall()
        return [
            _validate_run(
                payload=row[1],
                row_id=row[0],
                workspace_id=None,
                task_id=task_id,
                project_id=None,
            )
            for row in rows
        ]

    def has_run_scheduled_audit(
        self, *, task_id: str, run_id: str, workspace_id: str
    ) -> bool:
        if not task_id or not run_id or not workspace_id:
            raise ValueError("Task, run, and workspace identities are required")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT entity_id,payload FROM kv_store WHERE entity_type='audit_event' "
                "AND json_extract(payload,'$.event_type')='agent_task.run_scheduled' "
                "AND json_extract(payload,'$.entity_id')=? "
                "AND json_extract(payload,'$.workspace_id')=? "
                "AND json_extract(payload,'$.details.agent_run_id')=?",
                (task_id, workspace_id, run_id),
            ).fetchall()
        for row_id, payload in rows:
            try:
                event = AuditEvent.model_validate_json(payload)
            except Exception as error:
                raise AgentRunRepositoryIntegrityError(
                    "Stored run-scheduled audit payload is invalid"
                ) from error
            if (
                event.id != row_id
                or event.event_type != "agent_task.run_scheduled"
                or event.entity_id != task_id
                or event.workspace_id != workspace_id
                or event.details.get("agent_run_id") != run_id
            ):
                raise AgentRunRepositoryIntegrityError(
                    "Run-scheduled audit payload does not match its indexed identity or scope"
                )
        return bool(rows)

    def record_run_scheduled_audit(self, event: AuditEvent) -> bool:
        run_id = event.details.get("agent_run_id")
        approval_id = event.details.get("approval_request_id")
        if (
            event.event_type != "agent_task.run_scheduled"
            or not event.entity_id
            or not event.workspace_id
            or not isinstance(run_id, str)
            or not isinstance(approval_id, str)
        ):
            raise ValueError("Run-scheduled audit requires task, workspace, run, and approval scope")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run_row = conn.execute(
                "SELECT payload FROM kv_store WHERE entity_type='agent_run' AND entity_id=?",
                (run_id,),
            ).fetchone()
            if run_row is None:
                raise AgentRunRepositoryConflict("Scheduled AgentRun is missing")
            run = AgentRun.model_validate_json(run_row[0])
            if (
                run.task_id != event.entity_id
                or run.workspace_id != event.workspace_id
                or run.approval_request_id != approval_id
            ):
                raise AgentRunRepositoryConflict("Run-scheduled audit scope does not match AgentRun")
            existing = conn.execute(
                "SELECT 1 FROM kv_store WHERE entity_type='audit_event' "
                "AND json_extract(payload,'$.event_type')='agent_task.run_scheduled' "
                "AND json_extract(payload,'$.entity_id')=? "
                "AND json_extract(payload,'$.workspace_id')=? "
                "AND json_extract(payload,'$.details.agent_run_id')=?",
                (event.entity_id, event.workspace_id, run_id),
            ).fetchone()
            if existing is not None:
                return False
            conn.execute(
                "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES('audit_event',?,?)",
                (event.id, event.model_dump_json()),
            )
            return True

    def get_active_for_task(self, task_id: str) -> AgentRun | None:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT entity_id,payload FROM kv_store WHERE entity_type='agent_run' "
                "AND json_extract(payload,'$.task_id')=? "
                "AND json_extract(payload,'$.status') NOT IN ('FAILED','CANCELLED','BLOCKED') "
                "ORDER BY json_extract(payload,'$.created_at') DESC,entity_id DESC LIMIT 2",
                (task_id,),
            ).fetchall()
        if len(rows) > 1:
            raise AgentRunRepositoryIntegrityError("Multiple active runs exist for one task")
        if not rows:
            return None
        return _validate_run(
            payload=rows[0][1],
            row_id=rows[0][0],
            workspace_id=None,
            task_id=task_id,
            project_id=None,
        )

    def list_steps_for_run(self, run_id: str) -> list[AgentRunStep]:
        with self._connect() as conn:
            run_row = conn.execute(
                "SELECT entity_id,payload FROM kv_store "
                "WHERE entity_type='agent_run' AND entity_id=?",
                (run_id,),
            ).fetchone()
            if run_row is None:
                raise AgentRunRepositoryConflict("AgentRun is missing from durable storage")
            run = _validate_run(
                payload=run_row[1], row_id=run_row[0], workspace_id=None,
                task_id=None, project_id=None,
            )
            rows = conn.execute(
                "SELECT entity_id,payload FROM kv_store WHERE entity_type='agent_run_step' "
                "AND json_extract(payload,'$.run_id')=? ORDER BY json_extract(payload,'$.sequence'),entity_id",
                (run_id,),
            ).fetchall()
        steps: list[AgentRunStep] = []
        for row in rows:
            try:
                step = AgentRunStep.model_validate_json(row[1])
            except Exception as error:
                raise AgentRunRepositoryIntegrityError("Stored AgentRun step payload is invalid") from error
            if row[0] != step.id or step.run_id != run.id or step.task_id != run.task_id:
                raise AgentRunRepositoryIntegrityError("Stored AgentRun step identity or scope is inconsistent")
            steps.append(step)
        return steps

    def list_steps_for_runs(self, runs: list[AgentRun]) -> list[AgentRunStep]:
        """Fetch steps for an explicit run set without an N+1 query pattern."""
        if not runs:
            return []
        runs_by_id = {run.id: run for run in runs}
        if len(runs_by_id) != len(runs):
            raise ValueError("AgentRun summary contains duplicate run identities")
        unique_run_ids = sorted(runs_by_id)
        placeholders = ",".join("?" for _ in unique_run_ids)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT entity_id,payload FROM kv_store WHERE entity_type='agent_run_step' "
                f"AND json_extract(payload,'$.run_id') IN ({placeholders}) "
                "ORDER BY json_extract(payload,'$.run_id'),"
                "json_extract(payload,'$.sequence'),entity_id",
                unique_run_ids,
            ).fetchall()
        steps: list[AgentRunStep] = []
        for row_id, payload in rows:
            try:
                step = AgentRunStep.model_validate_json(payload)
            except Exception as error:
                raise AgentRunRepositoryIntegrityError("Stored AgentRun step payload is invalid") from error
            run = runs_by_id.get(step.run_id)
            if (
                run is None
                or row_id != step.id
                or step.task_id != run.task_id
            ):
                raise AgentRunRepositoryIntegrityError(
                    "Stored AgentRun step identity or scope is inconsistent"
                )
            steps.append(step)
        return steps

    def list_events_for_run(self, run_id: str) -> list[AgentRunEvent]:
        run = self.get(run_id)
        if run is None:
            raise AgentRunRepositoryConflict("AgentRun is missing from durable storage")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT entity_id,payload FROM kv_store WHERE entity_type='agent_run_event' "
                "AND json_extract(payload,'$.run_id')=? "
                "ORDER BY json_extract(payload,'$.created_at'),entity_id",
                (run_id,),
            ).fetchall()
        events: list[AgentRunEvent] = []
        for row_id, payload in rows:
            try:
                event = AgentRunEvent.model_validate_json(payload)
            except Exception as error:
                raise AgentRunRepositoryIntegrityError("Stored AgentRun event payload is invalid") from error
            if row_id != event.id or event.run_id != run.id or event.task_id != run.task_id:
                raise AgentRunRepositoryIntegrityError("Stored AgentRun event identity or scope is inconsistent")
            events.append(event)
        return events

    def list_expired_leases(
        self,
        *,
        now: datetime,
        workspace_id: str | None = None,
        limit: int = 100,
    ) -> list[tuple[AgentRun, AgentRunStep | None]]:
        if not 1 <= limit <= 1000:
            raise ValueError("Expired lease query limit must be between 1 and 1000")
        query = (
            "SELECT entity_id,payload FROM kv_store WHERE entity_type='agent_run' "
            "AND json_extract(payload,'$.lease_expires_at') IS NOT NULL "
            "AND julianday(json_extract(payload,'$.lease_expires_at')) <= julianday(?) "
        )
        params: list[object] = [now.isoformat()]
        if workspace_id is not None:
            query += "AND json_extract(payload,'$.workspace_id')=? "
            params.append(workspace_id)
        query += (
            "ORDER BY julianday(json_extract(payload,'$.lease_expires_at')),entity_id LIMIT ?"
        )
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
            candidates: list[tuple[AgentRun, AgentRunStep | None]] = []
            for row in rows:
                run = _validate_run(
                    payload=row[1], row_id=row[0], workspace_id=None,
                    task_id=None, project_id=None,
                )
                step = None
                if run.current_step_id:
                    step_row = conn.execute(
                        "SELECT entity_id,payload FROM kv_store "
                        "WHERE entity_type='agent_run_step' AND entity_id=?",
                        (run.current_step_id,),
                    ).fetchone()
                    if step_row is None:
                        raise AgentRunRepositoryIntegrityError(
                            "Expired AgentRun references a missing current step"
                        )
                    try:
                        step = AgentRunStep.model_validate_json(step_row[1])
                    except Exception as error:
                        raise AgentRunRepositoryIntegrityError(
                            "Stored expired AgentRun step payload is invalid"
                        ) from error
                    if (
                        step_row[0] != run.current_step_id
                        or step.run_id != run.id
                        or step.task_id != run.task_id
                    ):
                        raise AgentRunRepositoryIntegrityError(
                            "Expired AgentRun current step identity or scope is inconsistent"
                        )
                candidates.append((run, step))
        return candidates

    def acquire_lease(
        self,
        *,
        run_id: str,
        worker_id: str,
        lease_seconds: int,
        now: datetime,
    ) -> tuple[AgentRun, AgentRunEvent | None, AuditEvent | None]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT entity_id,payload FROM kv_store "
                "WHERE entity_type='agent_run' AND entity_id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise AgentRunRepositoryConflict("AgentRun is missing from durable storage")
            current = _validate_run(
                payload=row[1],
                row_id=row[0],
                workspace_id=None,
                task_id=None,
                project_id=None,
            )
            updated, event, audit = _lease_result(
                current,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
                now=now,
            )
            if event is None or audit is None:
                return updated, None, None
            changed = conn.execute(
                "UPDATE kv_store SET payload=? WHERE entity_type='agent_run' "
                "AND entity_id=? AND payload=?",
                (updated.model_dump_json(), run_id, row[1]),
            )
            if changed.rowcount != 1:
                raise AgentRunRepositoryConflict("AgentRun changed while acquiring its lease")
            conn.executemany(
                "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
                [
                    ("agent_run_event", event.id, event.model_dump_json()),
                    ("audit_event", audit.id, audit.model_dump_json()),
                ],
            )
            conn.commit()
            return updated, event, audit

    def recover_expired_lease(
        self,
        *,
        expected_run: AgentRun,
        updated_run: AgentRun,
        expected_step: AgentRunStep | None,
        updated_step: AgentRunStep | None,
        event: AgentRunEvent,
        audit: AuditEvent,
        now: datetime,
    ) -> None:
        _validate_lease_recovery(
            expected_run=expected_run, updated_run=updated_run,
            expected_step=expected_step, updated_step=updated_step,
            event=event, audit=audit, now=now,
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                run_row = conn.execute(
                    "SELECT entity_id,payload FROM kv_store "
                    "WHERE entity_type='agent_run' AND entity_id=?",
                    (expected_run.id,),
                ).fetchone()
                if run_row is None:
                    raise AgentRunRepositoryConflict("Expired AgentRun is missing")
                durable_run = _validate_run(
                    payload=run_row[1], row_id=run_row[0], workspace_id=None,
                    task_id=None, project_id=None,
                )
                if durable_run != expected_run or durable_run.lease_expires_at is None or durable_run.lease_expires_at > now:
                    raise AgentRunRepositoryConflict("Expired AgentRun changed before recovery")

                step_row = None
                if expected_step is not None:
                    step_row = conn.execute(
                        "SELECT entity_id,payload FROM kv_store "
                        "WHERE entity_type='agent_run_step' AND entity_id=?",
                        (expected_step.id,),
                    ).fetchone()
                    if step_row is None or step_row[0] != expected_step.id:
                        raise AgentRunRepositoryConflict("Expired AgentRun step is missing")
                    durable_step = AgentRunStep.model_validate_json(step_row[1])
                    if durable_step != expected_step:
                        raise AgentRunRepositoryConflict("Expired AgentRun step changed before recovery")

                changed = conn.execute(
                    "UPDATE kv_store SET payload=? WHERE entity_type='agent_run' "
                    "AND entity_id=? AND payload=?",
                    (updated_run.model_dump_json(), expected_run.id, run_row[1]),
                )
                if changed.rowcount != 1:
                    raise AgentRunRepositoryConflict("Expired AgentRun changed during recovery")
                if updated_step is not None and step_row is not None:
                    changed = conn.execute(
                        "UPDATE kv_store SET payload=? WHERE entity_type='agent_run_step' "
                        "AND entity_id=? AND payload=?",
                        (updated_step.model_dump_json(), expected_step.id, step_row[1]),
                    )
                    if changed.rowcount != 1:
                        raise AgentRunRepositoryConflict("Expired AgentRun step changed during recovery")
                conn.executemany(
                    "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
                    [
                        ("agent_run_event", event.id, event.model_dump_json()),
                        ("audit_event", audit.id, audit.model_dump_json()),
                    ],
                )
                conn.commit()
            except sqlite3.IntegrityError as error:
                conn.rollback()
                raise AgentRunRepositoryConflict("Expired AgentRun recovery record already exists") from error

    def update_lease(
        self,
        *,
        run_id: str,
        worker_id: str,
        lease_token: str,
        lease_seconds: int | None,
        now: datetime,
    ) -> tuple[AgentRun, AgentRunStep | None, AgentRunEvent, AuditEvent]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                run_row = conn.execute(
                    "SELECT entity_id,payload FROM kv_store "
                    "WHERE entity_type='agent_run' AND entity_id=?",
                    (run_id,),
                ).fetchone()
                if run_row is None:
                    raise AgentRunRepositoryConflict("AgentRun is missing from durable storage")
                run = _validate_run(
                    payload=run_row[1], row_id=run_row[0], workspace_id=None,
                    task_id=None, project_id=None,
                )
                step = None
                step_row = None
                if run.current_step_id:
                    step_row = conn.execute(
                        "SELECT entity_id,payload FROM kv_store "
                        "WHERE entity_type='agent_run_step' AND entity_id=?",
                        (run.current_step_id,),
                    ).fetchone()
                    if step_row is None:
                        raise AgentRunRepositoryIntegrityError(
                            "AgentRun references a missing current step during lease update"
                        )
                    step = AgentRunStep.model_validate_json(step_row[1])
                    if step.id != run.current_step_id or step.run_id != run.id or step.task_id != run.task_id:
                        raise AgentRunRepositoryIntegrityError(
                            "AgentRun current step identity or scope is inconsistent during lease update"
                        )
                updated_run, updated_step, event, audit = _lease_update_result(
                    run, step, worker_id=worker_id, lease_token=lease_token,
                    lease_seconds=lease_seconds, now=now,
                )
                changed = conn.execute(
                    "UPDATE kv_store SET payload=? WHERE entity_type='agent_run' "
                    "AND entity_id=? AND payload=?",
                    (updated_run.model_dump_json(), run.id, run_row[1]),
                )
                if changed.rowcount != 1:
                    raise AgentRunRepositoryConflict("AgentRun changed during lease update")
                if updated_step is not None and step_row is not None:
                    changed = conn.execute(
                        "UPDATE kv_store SET payload=? WHERE entity_type='agent_run_step' "
                        "AND entity_id=? AND payload=?",
                        (updated_step.model_dump_json(), updated_step.id, step_row[1]),
                    )
                    if changed.rowcount != 1:
                        raise AgentRunRepositoryConflict("AgentRun step changed during lease update")
                conn.executemany(
                    "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
                    [
                        ("agent_run_event", event.id, event.model_dump_json()),
                        ("audit_event", audit.id, audit.model_dump_json()),
                    ],
                )
                conn.commit()
                return updated_run, updated_step, event, audit
            except sqlite3.IntegrityError as error:
                conn.rollback()
                raise AgentRunRepositoryConflict("AgentRun lease event or audit already exists") from error

    def commit_control_transition(
        self,
        *,
        expected_run: AgentRun,
        updated_run: AgentRun,
        expected_steps: list[AgentRunStep],
        updated_steps: list[AgentRunStep],
        events: list[AgentRunEvent],
        audits: list[AuditEvent],
    ) -> None:
        _validate_control_transition(
            expected_run=expected_run, updated_run=updated_run,
            expected_steps=expected_steps, updated_steps=updated_steps,
            events=events, audits=audits,
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                run_row = conn.execute(
                    "SELECT entity_id,payload FROM kv_store "
                    "WHERE entity_type='agent_run' AND entity_id=?",
                    (expected_run.id,),
                ).fetchone()
                if run_row is None:
                    raise AgentRunRepositoryConflict("AgentRun is missing during control transition")
                durable_run = _validate_run(
                    payload=run_row[1], row_id=run_row[0], workspace_id=None,
                    task_id=None, project_id=None,
                )
                if durable_run != expected_run:
                    raise AgentRunRepositoryConflict("AgentRun changed before control transition")

                durable_step_ids = {
                    row[0]
                    for row in conn.execute(
                        "SELECT entity_id FROM kv_store WHERE entity_type='agent_run_step' "
                        "AND json_extract(payload,'$.run_id')=?",
                        (expected_run.id,),
                    ).fetchall()
                }
                if durable_step_ids != set(expected_step.id for expected_step in expected_steps):
                    raise AgentRunRepositoryConflict("AgentRun step set changed before control transition")

                durable_steps: dict[str, tuple[str, str]] = {}
                for expected_step in expected_steps:
                    row = conn.execute(
                        "SELECT entity_id,payload FROM kv_store "
                        "WHERE entity_type='agent_run_step' AND entity_id=?",
                        (expected_step.id,),
                    ).fetchone()
                    if row is None or row[0] != expected_step.id:
                        raise AgentRunRepositoryConflict("AgentRun step is missing during control transition")
                    stored_step = AgentRunStep.model_validate_json(row[1])
                    if stored_step != expected_step:
                        raise AgentRunRepositoryConflict("AgentRun step changed before control transition")
                    durable_steps[expected_step.id] = (row[0], row[1])

                changed = conn.execute(
                    "UPDATE kv_store SET payload=? WHERE entity_type='agent_run' "
                    "AND entity_id=? AND payload=?",
                    (updated_run.model_dump_json(), expected_run.id, run_row[1]),
                )
                if changed.rowcount != 1:
                    raise AgentRunRepositoryConflict("AgentRun changed during control transition")
                updated_by_id = {step.id: step for step in updated_steps}
                for step_id, (row_id, old_payload) in durable_steps.items():
                    changed = conn.execute(
                        "UPDATE kv_store SET payload=? WHERE entity_type='agent_run_step' "
                        "AND entity_id=? AND payload=?",
                        (updated_by_id[step_id].model_dump_json(), row_id, old_payload),
                    )
                    if changed.rowcount != 1:
                        raise AgentRunRepositoryConflict("AgentRun step changed during control transition")
                conn.executemany(
                    "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
                    [
                        *[("agent_run_event", event.id, event.model_dump_json()) for event in events],
                        *[("audit_event", audit.id, audit.model_dump_json()) for audit in audits],
                    ],
                )
                conn.commit()
            except sqlite3.IntegrityError as error:
                conn.rollback()
                raise AgentRunRepositoryConflict("AgentRun control transition record already exists") from error

    def commit_step_completion(
        self,
        *,
        expected_run: AgentRun,
        updated_run: AgentRun,
        expected_steps: list[AgentRunStep],
        updated_steps: list[AgentRunStep],
        events: list[AgentRunEvent],
        audit: AuditEvent,
        now: datetime,
    ) -> None:
        _validate_step_completion(
            expected_run=expected_run,
            updated_run=updated_run,
            expected_steps=expected_steps,
            updated_steps=updated_steps,
            events=events,
            audit=audit,
            now=now,
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                checked_at = max(now, datetime.now(timezone.utc))
                _validate_step_completion(
                    expected_run=expected_run,
                    updated_run=updated_run,
                    expected_steps=expected_steps,
                    updated_steps=updated_steps,
                    events=events,
                    audit=audit,
                    now=checked_at,
                )
                run_row = conn.execute(
                    "SELECT entity_id,payload FROM kv_store "
                    "WHERE entity_type='agent_run' AND entity_id=?",
                    (expected_run.id,),
                ).fetchone()
                if run_row is None or run_row[0] != expected_run.id:
                    raise AgentRunRepositoryConflict("AgentRun is missing during step completion")
                durable_run = _validate_run(
                    payload=run_row[1],
                    row_id=run_row[0],
                    workspace_id=None,
                    task_id=None,
                    project_id=None,
                )
                if durable_run != expected_run:
                    raise AgentRunRepositoryConflict("AgentRun changed before step completion")

                step_rows: dict[str, tuple[str, str]] = {}
                for expected_step in expected_steps:
                    row = conn.execute(
                        "SELECT entity_id,payload FROM kv_store "
                        "WHERE entity_type='agent_run_step' AND entity_id=?",
                        (expected_step.id,),
                    ).fetchone()
                    if row is None or row[0] != expected_step.id:
                        raise AgentRunRepositoryConflict("AgentRun step is missing during completion")
                    durable_step = AgentRunStep.model_validate_json(row[1])
                    if durable_step != expected_step:
                        raise AgentRunRepositoryConflict("AgentRun step changed before completion")
                    step_rows[expected_step.id] = (row[0], row[1])

                changed = conn.execute(
                    "UPDATE kv_store SET payload=? WHERE entity_type='agent_run' "
                    "AND entity_id=? AND payload=?",
                    (updated_run.model_dump_json(), expected_run.id, run_row[1]),
                )
                if changed.rowcount != 1:
                    raise AgentRunRepositoryConflict("AgentRun changed during step completion")
                for updated_step in updated_steps:
                    row_id, old_payload = step_rows[updated_step.id]
                    changed = conn.execute(
                        "UPDATE kv_store SET payload=? WHERE entity_type='agent_run_step' "
                        "AND entity_id=? AND payload=?",
                        (updated_step.model_dump_json(), row_id, old_payload),
                    )
                    if changed.rowcount != 1:
                        raise AgentRunRepositoryConflict("AgentRun step changed during completion")
                conn.executemany(
                    "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
                    [
                        *[("agent_run_event", event.id, event.model_dump_json()) for event in events],
                        ("audit_event", audit.id, audit.model_dump_json()),
                    ],
                )
                conn.commit()
            except sqlite3.IntegrityError as error:
                conn.rollback()
                raise AgentRunRepositoryConflict("AgentRun completion record already exists") from error

    def create(
        self,
        *,
        run: AgentRun,
        steps: list[AgentRunStep],
        events: list[AgentRunEvent],
        audit: AuditEvent,
        scheduled_audit: AuditEvent | None = None,
    ) -> None:
        _validate_run_creation(
            run=run,
            steps=steps,
            events=events,
            audit=audit,
            scheduled_audit=scheduled_audit,
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT entity_id FROM kv_store WHERE entity_type='agent_run' "
                "AND json_extract(payload,'$.task_id')=? "
                "AND json_extract(payload,'$.status') NOT IN ('FAILED','CANCELLED','BLOCKED') LIMIT 1",
                (run.task_id,),
            ).fetchone()
            if existing is not None:
                raise AgentRunRepositoryConflict("An active run already exists for this task")
            task_row = conn.execute(
                "SELECT payload FROM kv_store WHERE entity_type='agent_task' AND entity_id=?",
                (run.task_id,),
            ).fetchone()
            project_row = conn.execute(
                "SELECT payload FROM kv_store WHERE entity_type='project' AND entity_id=?",
                (run.project_id,),
            ).fetchone() if run.project_id else None
            approval_row = conn.execute(
                "SELECT payload FROM kv_store WHERE entity_type='approval_request' AND entity_id=?",
                (run.approval_request_id,),
            ).fetchone() if run.approval_request_id else None
            if task_row is None:
                raise AgentRunRepositoryConflict("Approved AgentTask is missing from durable storage")
            _validate_created_task_scope(
                run=run,
                task_payload=task_row[0],
                project_payload=project_row[0] if project_row else None,
                approval_payload=approval_row[0] if approval_row else None,
            )
            conn.executemany(
                "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
                [
                    ("agent_run", run.id, run.model_dump_json()),
                    *[("agent_run_step", step.id, step.model_dump_json()) for step in steps],
                    *[("agent_run_event", event.id, event.model_dump_json()) for event in events],
                    ("audit_event", audit.id, audit.model_dump_json()),
                    *(
                        [("audit_event", scheduled_audit.id, scheduled_audit.model_dump_json())]
                        if scheduled_audit is not None
                        else []
                    ),
                ],
            )
            conn.commit()


class PostgresAgentRunRepository:
    """Read workspace-scoped AgentRun records from normalized PostgreSQL."""

    def __init__(self, connect: Callable[[], Connection]) -> None:
        self._connect = connect

    def get(self, run_id: str) -> AgentRun | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT run_id,workspace_id,task_id,project_id,payload "
                "FROM agent_runs WHERE run_id=%s",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return _validate_run(
            payload=row[4],
            row_id=row[0],
            workspace_id=row[1],
            task_id=row[2],
            project_id=row[3],
        )

    def get_control_snapshot_in_transaction(
        self,
        conn: Connection,
        *,
        run_id: str,
        workspace_id: str,
    ) -> tuple[AgentRun, list[AgentRunStep]]:
        """Lock and validate a run with its complete step set in the caller's transaction."""
        if not workspace_id:
            raise AgentRunRepositoryConflict(
                "PostgreSQL control transition requires workspace scope"
            )
        run_row = conn.execute(
            "SELECT run_id,workspace_id,task_id,project_id,status,updated_at,payload "
            "FROM agent_runs WHERE run_id=%s AND workspace_id=%s FOR UPDATE",
            (run_id, workspace_id),
        ).fetchone()
        if run_row is None:
            raise AgentRunRepositoryNotFound("AgentRun is not visible in this workspace")
        run = _validate_run(
            payload=run_row[6],
            row_id=run_row[0],
            workspace_id=run_row[1],
            task_id=run_row[2],
            project_id=run_row[3],
        )
        if run.status.value != run_row[4] or run.updated_at != run_row[5]:
            raise AgentRunRepositoryIntegrityError(
                "Stored AgentRun payload does not match its indexed control state"
            )
        rows = conn.execute(
            "SELECT step_id,run_id,task_id,workspace_id,sequence,status,updated_at,payload "
            "FROM agent_run_steps WHERE run_id=%s AND workspace_id=%s "
            "ORDER BY sequence,step_id FOR UPDATE",
            (run_id, workspace_id),
        ).fetchall()
        steps: list[AgentRunStep] = []
        for row in rows:
            try:
                step = AgentRunStep.model_validate(row[7])
            except Exception as error:
                raise AgentRunRepositoryIntegrityError(
                    "Stored AgentRun step payload is invalid"
                ) from error
            if (
                step.id != row[0]
                or step.run_id != run.id
                or step.task_id != run.task_id
                or row[1] != run.id
                or row[2] != run.task_id
                or row[3] != workspace_id
                or step.sequence != row[4]
                or step.status.value != row[5]
                or step.updated_at != row[6]
            ):
                raise AgentRunRepositoryIntegrityError(
                    "Stored AgentRun step identity or scope is inconsistent"
                )
            steps.append(step)
        return run, steps

    def list_for_workspace(
        self,
        *,
        workspace_id: str | None,
        status: str | None = None,
        task_id: str | None = None,
        limit: int = 100,
    ) -> list[AgentRun]:
        if not 1 <= limit <= 500:
            raise ValueError("AgentRun query limit must be between 1 and 500")
        clauses: list[str] = []
        params: list[object] = []
        if workspace_id is not None:
            clauses.append("workspace_id=%s")
            params.append(workspace_id)
        if status is not None:
            clauses.append("status=%s")
            params.append(status)
        if task_id is not None:
            clauses.append("task_id=%s")
            params.append(task_id)
        query = (
            "SELECT run_id,workspace_id,task_id,project_id,payload FROM agent_runs"
            + (" WHERE " + " AND ".join(clauses) if clauses else "")
            + " ORDER BY updated_at DESC,run_id LIMIT %s"
        )
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            _validate_run(
                payload=row[4], row_id=row[0], workspace_id=row[1],
                task_id=task_id, project_id=row[3],
            )
            for row in rows
        ]

    def list_for_task(self, task_id: str) -> list[AgentRun]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT run_id,workspace_id,task_id,project_id,payload FROM agent_runs "
                "WHERE task_id=%s ORDER BY payload->>'created_at',run_id",
                (task_id,),
            ).fetchall()
        return [
            _validate_run(
                payload=row[4],
                row_id=row[0],
                workspace_id=row[1],
                task_id=row[2],
                project_id=row[3],
            )
            for row in rows
        ]

    def has_run_scheduled_audit(
        self, *, task_id: str, run_id: str, workspace_id: str
    ) -> bool:
        if not task_id or not run_id or not workspace_id:
            raise ValueError("Task, run, and workspace identities are required")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT event_id,workspace_id,event_type,entity_id,actor,created_at,details,payload "
                "FROM audit_events WHERE workspace_id=%s AND entity_type='agent_task' "
                "AND entity_id=%s AND event_type='agent_task.run_scheduled' "
                "AND details->>'agent_run_id'=%s",
                (workspace_id, task_id, run_id),
            ).fetchall()
        for row in rows:
            try:
                event = AuditEvent.model_validate(row[7])
            except Exception as error:
                raise AgentRunRepositoryIntegrityError(
                    "Stored run-scheduled audit payload is invalid"
                ) from error
            if (
                event.id != row[0]
                or event.workspace_id != row[1]
                or event.event_type != row[2]
                or event.entity_id != row[3]
                or event.actor != row[4]
                or event.created_at != row[5]
                or event.details != row[6]
                or event.workspace_id != workspace_id
                or event.entity_id != task_id
                or event.event_type != "agent_task.run_scheduled"
                or event.details.get("agent_run_id") != run_id
            ):
                raise AgentRunRepositoryIntegrityError(
                    "Run-scheduled audit payload does not match its indexed identity or scope"
                )
        return bool(rows)

    def record_run_scheduled_audit(self, event: AuditEvent) -> bool:
        run_id = event.details.get("agent_run_id")
        approval_id = event.details.get("approval_request_id")
        if (
            event.event_type != "agent_task.run_scheduled"
            or not event.entity_id
            or not event.workspace_id
            or not isinstance(run_id, str)
            or not isinstance(approval_id, str)
        ):
            raise ValueError("Run-scheduled audit requires task, workspace, run, and approval scope")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT task_id,workspace_id,approval_id FROM agent_runs "
                "WHERE run_id=%s FOR UPDATE",
                (run_id,),
            ).fetchone()
            if row is None:
                raise AgentRunRepositoryConflict("Scheduled AgentRun is missing")
            if row != (event.entity_id, event.workspace_id, approval_id):
                raise AgentRunRepositoryConflict("Run-scheduled audit scope does not match AgentRun")
            existing = conn.execute(
                "SELECT 1 FROM audit_events WHERE workspace_id=%s AND entity_type='agent_task' "
                "AND entity_id=%s AND event_type='agent_task.run_scheduled' "
                "AND details->>'agent_run_id'=%s",
                (event.workspace_id, event.entity_id, run_id),
            ).fetchone()
            if existing is not None:
                return False
            conn.execute(
                "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,"
                "event_type,actor,details,created_at,payload) "
                "VALUES(%s,%s,NULL,'agent_task',%s,%s,%s,%s,%s,%s)",
                (
                    event.id,
                    event.workspace_id,
                    event.entity_id,
                    event.event_type,
                    event.actor,
                    Jsonb(event.details),
                    event.created_at,
                    Jsonb(event.model_dump(mode="json")),
                ),
            )
            return True

    def get_active_for_task(self, task_id: str) -> AgentRun | None:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT run_id,workspace_id,task_id,project_id,payload FROM agent_runs "
                "WHERE task_id=%s AND status NOT IN ('FAILED','CANCELLED','BLOCKED') "
                "ORDER BY updated_at DESC,run_id DESC LIMIT 2",
                (task_id,),
            ).fetchall()
        if len(rows) > 1:
            raise AgentRunRepositoryIntegrityError("Multiple active runs exist for one task")
        if not rows:
            return None
        return _validate_run(
            payload=rows[0][4],
            row_id=rows[0][0],
            workspace_id=rows[0][1],
            task_id=task_id,
            project_id=rows[0][3],
        )

    def list_steps_for_run(self, run_id: str) -> list[AgentRunStep]:
        with self._connect() as conn:
            run_row = conn.execute(
                "SELECT run_id,workspace_id,task_id,project_id,payload "
                "FROM agent_runs WHERE run_id=%s",
                (run_id,),
            ).fetchone()
            if run_row is None:
                raise AgentRunRepositoryConflict("AgentRun is missing from durable storage")
            run = _validate_run(
                payload=run_row[4], row_id=run_row[0], workspace_id=run_row[1],
                task_id=run_row[2], project_id=run_row[3],
            )
            rows = conn.execute(
                "SELECT step_id,run_id,task_id,workspace_id,payload FROM agent_run_steps "
                "WHERE run_id=%s ORDER BY sequence,step_id",
                (run_id,),
            ).fetchall()
        steps: list[AgentRunStep] = []
        for row in rows:
            try:
                step = AgentRunStep.model_validate(row[4])
            except Exception as error:
                raise AgentRunRepositoryIntegrityError("Stored AgentRun step payload is invalid") from error
            if (
                row[0] != step.id or row[1] != run.id or row[2] != run.task_id
                or row[3] != run.workspace_id or step.run_id != run.id
                or step.task_id != run.task_id
            ):
                raise AgentRunRepositoryIntegrityError("Stored AgentRun step identity or scope is inconsistent")
            steps.append(step)
        return steps

    def list_steps_for_runs(self, runs: list[AgentRun]) -> list[AgentRunStep]:
        """Fetch steps for an explicit run set without an N+1 query pattern."""
        if not runs:
            return []
        runs_by_id = {run.id: run for run in runs}
        if len(runs_by_id) != len(runs):
            raise ValueError("AgentRun summary contains duplicate run identities")
        unique_run_ids = sorted(runs_by_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT step_id,run_id,task_id,workspace_id,payload FROM agent_run_steps "
                "WHERE run_id = ANY(%s) ORDER BY run_id,sequence,step_id",
                (unique_run_ids,),
            ).fetchall()
        steps: list[AgentRunStep] = []
        for row in rows:
            try:
                step = AgentRunStep.model_validate(row[4])
            except Exception as error:
                raise AgentRunRepositoryIntegrityError("Stored AgentRun step payload is invalid") from error
            run = runs_by_id.get(row[1])
            if (
                run is None
                or row[0] != step.id
                or row[2] != step.task_id
                or row[3] != run.workspace_id
                or step.run_id != run.id
                or step.task_id != run.task_id
            ):
                raise AgentRunRepositoryIntegrityError(
                    "Stored AgentRun step identity or scope is inconsistent"
                )
            steps.append(step)
        return steps

    def list_events_for_run(self, run_id: str) -> list[AgentRunEvent]:
        with self._connect() as conn:
            run_row = conn.execute(
                "SELECT run_id,workspace_id,task_id,payload FROM agent_runs WHERE run_id=%s",
                (run_id,),
            ).fetchone()
            if run_row is None:
                raise AgentRunRepositoryConflict("AgentRun is missing from durable storage")
            run = _validate_run(
                payload=run_row[3], row_id=run_row[0], workspace_id=run_row[1],
                task_id=run_row[2], project_id=None,
            )
            rows = conn.execute(
                "SELECT event_id,run_id,task_id,workspace_id,payload FROM agent_run_events "
                "WHERE run_id=%s ORDER BY created_at,event_id",
                (run_id,),
            ).fetchall()
        events: list[AgentRunEvent] = []
        for row in rows:
            try:
                event = AgentRunEvent.model_validate(row[4])
            except Exception as error:
                raise AgentRunRepositoryIntegrityError("Stored AgentRun event payload is invalid") from error
            if (
                row[0] != event.id or row[1] != run.id or row[2] != run.task_id
                or row[3] != run.workspace_id or event.run_id != run.id
                or event.task_id != run.task_id
            ):
                raise AgentRunRepositoryIntegrityError("Stored AgentRun event identity or scope is inconsistent")
            events.append(event)
        return events

    def list_expired_leases(
        self,
        *,
        now: datetime,
        workspace_id: str | None = None,
        limit: int = 100,
    ) -> list[tuple[AgentRun, AgentRunStep | None]]:
        if not 1 <= limit <= 1000:
            raise ValueError("Expired lease query limit must be between 1 and 1000")
        query = (
            "SELECT run_id,workspace_id,task_id,project_id,current_step_id,payload "
            "FROM agent_runs WHERE lease_expires_at IS NOT NULL AND lease_expires_at <= %s "
        )
        params: list[object] = [now]
        if workspace_id is not None:
            query += "AND workspace_id=%s "
            params.append(workspace_id)
        query += "ORDER BY lease_expires_at,run_id LIMIT %s"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
            candidates: list[tuple[AgentRun, AgentRunStep | None]] = []
            for row in rows:
                run = _validate_run(
                    payload=row[5], row_id=row[0], workspace_id=row[1],
                    task_id=row[2], project_id=row[3],
                )
                step = None
                if row[4]:
                    step_row = conn.execute(
                        "SELECT step_id,run_id,task_id,workspace_id,payload "
                        "FROM agent_run_steps WHERE step_id=%s",
                        (row[4],),
                    ).fetchone()
                    if step_row is None:
                        raise AgentRunRepositoryIntegrityError(
                            "Expired AgentRun references a missing current step"
                        )
                    try:
                        step = AgentRunStep.model_validate(step_row[4])
                    except Exception as error:
                        raise AgentRunRepositoryIntegrityError(
                            "Stored expired AgentRun step payload is invalid"
                        ) from error
                    if (
                        step_row[0] != run.current_step_id
                        or step_row[1] != run.id
                        or step_row[2] != run.task_id
                        or step_row[3] != run.workspace_id
                        or step.run_id != run.id
                        or step.task_id != run.task_id
                    ):
                        raise AgentRunRepositoryIntegrityError(
                            "Expired AgentRun current step identity or scope is inconsistent"
                        )
                candidates.append((run, step))
        return candidates

    def acquire_lease(
        self,
        *,
        run_id: str,
        worker_id: str,
        lease_seconds: int,
        now: datetime,
    ) -> tuple[AgentRun, AgentRunEvent | None, AuditEvent | None]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT run_id,workspace_id,task_id,project_id,payload "
                "FROM agent_runs WHERE run_id=%s FOR UPDATE",
                (run_id,),
            ).fetchone()
            if row is None:
                raise AgentRunRepositoryConflict("AgentRun is missing from durable storage")
            current = _validate_run(
                payload=row[4],
                row_id=row[0],
                workspace_id=row[1],
                task_id=row[2],
                project_id=row[3],
            )
            updated, event, audit = _lease_result(
                current,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
                now=now,
            )
            if event is None or audit is None:
                return updated, None, None
            changed = conn.execute(
                "UPDATE agent_runs SET lease_owner=%s,lease_token=%s,lease_expires_at=%s,"
                "updated_at=%s,payload=%s WHERE run_id=%s AND workspace_id=%s",
                (
                    updated.lease_owner,
                    updated.lease_token,
                    updated.lease_expires_at,
                    updated.updated_at,
                    Jsonb(updated.model_dump(mode="json")),
                    run_id,
                    updated.workspace_id,
                ),
            )
            if changed.rowcount != 1:
                raise AgentRunRepositoryConflict("AgentRun changed while acquiring its lease")
            conn.execute(
                "INSERT INTO agent_run_events(event_id,run_id,task_id,workspace_id,created_at,"
                "event_type,payload) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (
                    event.id, event.run_id, event.task_id, updated.workspace_id,
                    event.created_at, event.event_type.value,
                    Jsonb(event.model_dump(mode="json")),
                ),
            )
            conn.execute(
                "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,"
                "event_type,actor,details,created_at,payload) "
                "VALUES(%s,%s,NULL,'agent_run',%s,%s,%s,%s,%s,%s)",
                (
                    audit.id, updated.workspace_id, updated.id, audit.event_type,
                    audit.actor, Jsonb(audit.details), audit.created_at,
                    Jsonb(audit.model_dump(mode="json")),
                ),
            )
            return updated, event, audit

    def recover_expired_lease(
        self,
        *,
        expected_run: AgentRun,
        updated_run: AgentRun,
        expected_step: AgentRunStep | None,
        updated_step: AgentRunStep | None,
        event: AgentRunEvent,
        audit: AuditEvent,
        now: datetime,
    ) -> None:
        _validate_lease_recovery(
            expected_run=expected_run, updated_run=updated_run,
            expected_step=expected_step, updated_step=updated_step,
            event=event, audit=audit, now=now,
        )
        if not expected_run.workspace_id:
            raise AgentRunRepositoryConflict("PostgreSQL lease recovery requires workspace scope")
        with self._connect() as conn:
            run_row = conn.execute(
                "SELECT run_id,workspace_id,task_id,project_id,current_step_id,payload "
                "FROM agent_runs WHERE run_id=%s FOR UPDATE",
                (expected_run.id,),
            ).fetchone()
            if run_row is None:
                raise AgentRunRepositoryConflict("Expired AgentRun is missing")
            durable_run = _validate_run(
                payload=run_row[5], row_id=run_row[0], workspace_id=run_row[1],
                task_id=run_row[2], project_id=run_row[3],
            )
            if (
                durable_run != expected_run
                or durable_run.lease_expires_at is None
                or durable_run.lease_expires_at > now
            ):
                raise AgentRunRepositoryConflict("Expired AgentRun changed before recovery")

            step_row = None
            if expected_step is not None:
                step_row = conn.execute(
                    "SELECT step_id,run_id,task_id,workspace_id,payload "
                    "FROM agent_run_steps WHERE step_id=%s FOR UPDATE",
                    (expected_step.id,),
                ).fetchone()
                if step_row is None:
                    raise AgentRunRepositoryConflict("Expired AgentRun step is missing")
                try:
                    durable_step = AgentRunStep.model_validate(step_row[4])
                except Exception as error:
                    raise AgentRunRepositoryIntegrityError(
                        "Stored expired AgentRun step payload is invalid"
                    ) from error
                if (
                    step_row[0] != expected_step.id
                    or step_row[1] != expected_run.id
                    or step_row[2] != expected_run.task_id
                    or step_row[3] != expected_run.workspace_id
                    or durable_step != expected_step
                ):
                    raise AgentRunRepositoryConflict("Expired AgentRun step changed before recovery")

            changed = conn.execute(
                "UPDATE agent_runs SET status=%s,current_step_id=%s,lease_owner=NULL,"
                "lease_token=NULL,lease_expires_at=NULL,updated_at=%s,payload=%s "
                "WHERE run_id=%s AND workspace_id=%s",
                (
                    updated_run.status.value, updated_run.current_step_id,
                    updated_run.updated_at, Jsonb(updated_run.model_dump(mode="json")),
                    updated_run.id, updated_run.workspace_id,
                ),
            )
            if changed.rowcount != 1:
                raise AgentRunRepositoryConflict("Expired AgentRun changed during recovery")
            if updated_step is not None:
                changed = conn.execute(
                    "UPDATE agent_run_steps SET status=%s,updated_at=%s,payload=%s "
                    "WHERE step_id=%s AND run_id=%s AND workspace_id=%s",
                    (
                        updated_step.status.value, updated_step.updated_at,
                        Jsonb(updated_step.model_dump(mode="json")), updated_step.id,
                        updated_step.run_id, updated_run.workspace_id,
                    ),
                )
                if changed.rowcount != 1:
                    raise AgentRunRepositoryConflict("Expired AgentRun step changed during recovery")
            conn.execute(
                "INSERT INTO agent_run_events(event_id,run_id,task_id,workspace_id,created_at,event_type,payload) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (
                    event.id, event.run_id, event.task_id, updated_run.workspace_id,
                    event.created_at, event.event_type.value, Jsonb(event.model_dump(mode="json")),
                ),
            )
            conn.execute(
                "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,"
                "event_type,actor,details,created_at,payload) "
                "VALUES(%s,%s,NULL,'agent_run',%s,%s,%s,%s,%s,%s)",
                (
                    audit.id, updated_run.workspace_id, updated_run.id, audit.event_type,
                    audit.actor, Jsonb(audit.details), audit.created_at,
                    Jsonb(audit.model_dump(mode="json")),
                ),
            )

    def update_lease(
        self,
        *,
        run_id: str,
        worker_id: str,
        lease_token: str,
        lease_seconds: int | None,
        now: datetime,
    ) -> tuple[AgentRun, AgentRunStep | None, AgentRunEvent, AuditEvent]:
        with self._connect() as conn:
            run_row = conn.execute(
                "SELECT run_id,workspace_id,task_id,project_id,current_step_id,payload "
                "FROM agent_runs WHERE run_id=%s FOR UPDATE",
                (run_id,),
            ).fetchone()
            if run_row is None:
                raise AgentRunRepositoryConflict("AgentRun is missing from durable storage")
            run = _validate_run(
                payload=run_row[5], row_id=run_row[0], workspace_id=run_row[1],
                task_id=run_row[2], project_id=run_row[3],
            )
            step = None
            step_row = None
            if run_row[4]:
                step_row = conn.execute(
                    "SELECT step_id,run_id,task_id,workspace_id,payload "
                    "FROM agent_run_steps WHERE step_id=%s FOR UPDATE",
                    (run_row[4],),
                ).fetchone()
                if step_row is None:
                    raise AgentRunRepositoryIntegrityError(
                        "AgentRun references a missing current step during lease update"
                    )
                try:
                    step = AgentRunStep.model_validate(step_row[4])
                except Exception as error:
                    raise AgentRunRepositoryIntegrityError(
                        "Stored AgentRun step payload is invalid"
                    ) from error
                if (
                    step_row[0] != run.current_step_id
                    or step_row[1] != run.id
                    or step_row[2] != run.task_id
                    or step_row[3] != run.workspace_id
                    or step.run_id != run.id
                    or step.task_id != run.task_id
                ):
                    raise AgentRunRepositoryIntegrityError(
                        "AgentRun current step identity or scope is inconsistent during lease update"
                    )
            updated_run, updated_step, event, audit = _lease_update_result(
                run, step, worker_id=worker_id, lease_token=lease_token,
                lease_seconds=lease_seconds, now=now,
            )
            changed = conn.execute(
                "UPDATE agent_runs SET lease_owner=%s,lease_token=%s,lease_expires_at=%s,"
                "updated_at=%s,payload=%s WHERE run_id=%s AND workspace_id=%s",
                (
                    updated_run.lease_owner, updated_run.lease_token,
                    updated_run.lease_expires_at, updated_run.updated_at,
                    Jsonb(updated_run.model_dump(mode="json")), updated_run.id,
                    updated_run.workspace_id,
                ),
            )
            if changed.rowcount != 1:
                raise AgentRunRepositoryConflict("AgentRun changed during lease update")
            if updated_step is not None and step_row is not None:
                changed = conn.execute(
                    "UPDATE agent_run_steps SET status=%s,updated_at=%s,payload=%s "
                    "WHERE step_id=%s AND run_id=%s AND workspace_id=%s",
                    (
                        updated_step.status.value, updated_step.updated_at,
                        Jsonb(updated_step.model_dump(mode="json")), updated_step.id,
                        updated_step.run_id, updated_run.workspace_id,
                    ),
                )
                if changed.rowcount != 1:
                    raise AgentRunRepositoryConflict("AgentRun step changed during lease update")
            conn.execute(
                "INSERT INTO agent_run_events(event_id,run_id,task_id,workspace_id,created_at,event_type,payload) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (
                    event.id, event.run_id, event.task_id, updated_run.workspace_id,
                    event.created_at, event.event_type.value, Jsonb(event.model_dump(mode="json")),
                ),
            )
            conn.execute(
                "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,"
                "event_type,actor,details,created_at,payload) "
                "VALUES(%s,%s,NULL,'agent_run',%s,%s,%s,%s,%s,%s)",
                (
                    audit.id, updated_run.workspace_id, updated_run.id, audit.event_type,
                    audit.actor, Jsonb(audit.details), audit.created_at,
                    Jsonb(audit.model_dump(mode="json")),
                ),
            )
            return updated_run, updated_step, event, audit

    def commit_control_transition(
        self,
        *,
        expected_run: AgentRun,
        updated_run: AgentRun,
        expected_steps: list[AgentRunStep],
        updated_steps: list[AgentRunStep],
        events: list[AgentRunEvent],
        audits: list[AuditEvent],
    ) -> None:
        with self._connect() as conn:
            with conn.transaction():
                self.commit_control_transition_in_transaction(
                    conn,
                    expected_run=expected_run,
                    updated_run=updated_run,
                    expected_steps=expected_steps,
                    updated_steps=updated_steps,
                    events=events,
                    audits=audits,
                )

    def commit_control_transition_in_transaction(
        self,
        conn: Connection,
        *,
        expected_run: AgentRun,
        updated_run: AgentRun,
        expected_steps: list[AgentRunStep],
        updated_steps: list[AgentRunStep],
        events: list[AgentRunEvent],
        audits: list[AuditEvent],
    ) -> None:
        """Persist a run transition without committing the caller's transaction."""
        _validate_control_transition(
            expected_run=expected_run, updated_run=updated_run,
            expected_steps=expected_steps, updated_steps=updated_steps,
            events=events, audits=audits,
        )
        if not expected_run.workspace_id:
            raise AgentRunRepositoryConflict("PostgreSQL control transition requires workspace scope")
        durable_run, durable_steps = self.get_control_snapshot_in_transaction(
            conn,
            run_id=expected_run.id,
            workspace_id=expected_run.workspace_id,
        )
        if durable_run != expected_run:
            raise AgentRunRepositoryConflict("AgentRun changed before control transition")
        expected_by_id = {step.id: step for step in expected_steps}
        durable_by_id = {step.id: step for step in durable_steps}
        if durable_by_id != expected_by_id:
            raise AgentRunRepositoryConflict("AgentRun step set changed before control transition")

        changed = conn.execute(
            "UPDATE agent_runs SET status=%s,current_step_id=%s,updated_at=%s,payload=%s "
            "WHERE run_id=%s AND workspace_id=%s",
            (
                updated_run.status.value, updated_run.current_step_id,
                updated_run.updated_at, Jsonb(updated_run.model_dump(mode="json")),
                updated_run.id, updated_run.workspace_id,
            ),
        )
        if changed.rowcount != 1:
            raise AgentRunRepositoryConflict("AgentRun changed during control transition")
        updated_by_id = {step.id: step for step in updated_steps}
        for step in durable_steps:
            updated_step = updated_by_id[step.id]
            changed = conn.execute(
                "UPDATE agent_run_steps SET status=%s,updated_at=%s,payload=%s "
                "WHERE step_id=%s AND run_id=%s AND workspace_id=%s",
                (
                    updated_step.status.value, updated_step.updated_at,
                    Jsonb(updated_step.model_dump(mode="json")), updated_step.id,
                    updated_step.run_id, updated_run.workspace_id,
                ),
            )
            if changed.rowcount != 1:
                raise AgentRunRepositoryConflict("AgentRun step changed during control transition")
        for event in events:
            conn.execute(
                "INSERT INTO agent_run_events(event_id,run_id,task_id,workspace_id,created_at,event_type,payload) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (
                    event.id, event.run_id, event.task_id, updated_run.workspace_id,
                    event.created_at, event.event_type.value,
                    Jsonb(event.model_dump(mode="json")),
                ),
            )
        for audit in audits:
            conn.execute(
                "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,"
                "event_type,actor,details,created_at,payload) "
                "VALUES(%s,%s,NULL,'agent_run',%s,%s,%s,%s,%s,%s)",
                (
                    audit.id, updated_run.workspace_id, updated_run.id, audit.event_type,
                    audit.actor, Jsonb(audit.details), audit.created_at,
                    Jsonb(audit.model_dump(mode="json")),
                ),
            )

    def attach_capability_lease(
        self,
        *,
        run_id: str,
        workspace_id: str,
        lease_id: str,
        actor: str,
        now: datetime,
    ) -> AgentRun:
        """Attach a capability lease and its audit trail in one scoped transaction."""
        if not workspace_id:
            raise AgentRunRepositoryConflict(
                "PostgreSQL capability binding requires workspace scope"
            )
        trust_repository = PostgresTrustRepository()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT run_id,workspace_id,task_id,project_id,payload "
                "FROM agent_runs WHERE run_id=%s AND workspace_id=%s FOR UPDATE",
                (run_id, workspace_id),
            ).fetchone()
            if row is None:
                raise AgentRunRepositoryNotFound("AgentRun not found")
            run = _validate_run(
                payload=row[4], row_id=row[0], workspace_id=row[1],
                task_id=row[2], project_id=row[3],
            )
            if run.capability_lease_id and run.capability_lease_id != lease_id:
                raise AgentRunRepositoryConflict(
                    "AgentRun capability lease cannot be replaced"
                )

            lease = trust_repository.get_capability_lease(
                conn, lease_id, workspace_id, for_update=True
            )
            if lease is None:
                raise AgentRunRepositoryNotFound("Capability lease not found")
            if lease.status != CapabilityLeaseStatus.ACTIVE or lease.expires_at <= now:
                raise AgentRunRepositoryConflict("Capability lease is not active")
            try:
                receipt_is_valid = verify_payload_signature(
                    lease_receipt_payload(lease), lease.receipt_signature
                )
            except RuntimeError as error:
                raise AgentRunRepositoryConflict(
                    "Capability lease receipt cannot be verified"
                ) from error
            if not receipt_is_valid:
                raise AgentRunRepositoryConflict(
                    "Capability lease receipt signature is invalid"
                )
            if run.capability_lease_id == lease_id:
                approval_row = conn.execute(
                    "SELECT approval_id,project_id,workspace_id,payload FROM approval_requests "
                    "WHERE approval_id=%s AND workspace_id=%s FOR SHARE",
                    (run.approval_request_id, workspace_id),
                ).fetchone()
                if approval_row is None:
                    raise AgentRunRepositoryConflict(
                        "Capability lease approval is missing from this workspace"
                    )
                try:
                    approval = ApprovalRequest.model_validate(approval_row[3])
                except Exception as error:
                    raise AgentRunRepositoryIntegrityError(
                        "Capability lease approval payload is invalid"
                    ) from error
                if (
                    approval.id != approval_row[0]
                    or approval.project_id != approval_row[1]
                    or approval.workspace_id != approval_row[2]
                    or approval.id != run.approval_request_id
                    or approval.project_id != run.project_id
                    or approval.workspace_id != workspace_id
                    or approval.status != ApprovalStatus.APPROVED
                    or not approval.approved_payload_hash
                    or approval.approved_payload_hash
                    != compute_approval_payload_hash(approval)
                    or lease.approval_request_id != approval.id
                    or lease.approved_payload_hash != approval.approved_payload_hash
                    or lease.scope.repository != run.repository
                ):
                    raise AgentRunRepositoryConflict(
                        "Attached capability lease no longer matches the approved run"
                    )
                return run
            existing_binding = conn.execute(
                "SELECT run_id FROM agent_runs WHERE workspace_id=%s "
                "AND payload->>'capability_lease_id'=%s AND run_id<>%s FOR UPDATE",
                (workspace_id, lease_id, run_id),
            ).fetchone()
            if existing_binding is not None:
                raise AgentRunRepositoryConflict(
                    "Capability lease is already bound to another AgentRun"
                )

            task_row = conn.execute(
                "SELECT task_id,project_id,workspace_id,payload FROM agent_tasks "
                "WHERE task_id=%s AND workspace_id=%s FOR SHARE",
                (run.task_id, workspace_id),
            ).fetchone()
            approval_row = conn.execute(
                "SELECT approval_id,project_id,workspace_id,payload FROM approval_requests "
                "WHERE approval_id=%s AND workspace_id=%s FOR SHARE",
                (run.approval_request_id, workspace_id),
            ).fetchone()
            if task_row is None or approval_row is None:
                raise AgentRunRepositoryConflict(
                    "Capability lease parents are missing from this workspace"
                )
            try:
                task = AgentTask.model_validate(task_row[3])
                approval = ApprovalRequest.model_validate(approval_row[3])
            except Exception as error:
                raise AgentRunRepositoryIntegrityError(
                    "Capability lease parent payload is invalid"
                ) from error
            if (
                task.id != task_row[0] or task.project_id != task_row[1]
                or task.workspace_id != task_row[2]
                or approval.id != approval_row[0]
                or approval.project_id != approval_row[1]
                or approval.workspace_id != approval_row[2]
                or task.id != run.task_id or task.project_id != run.project_id
                or task.workspace_id != workspace_id
                or task.status != AgentTaskStatus.APPROVED
                or task.approval_request_id != run.approval_request_id
                or approval.id != run.approval_request_id
                or approval.project_id != run.project_id
                or approval.status != ApprovalStatus.APPROVED
                or not approval.approved_payload_hash
                or approval.approved_payload_hash
                != compute_approval_payload_hash(approval)
                or lease.workspace_id != workspace_id
                or lease.approval_request_id != approval.id
                or lease.approved_payload_hash != approval.approved_payload_hash
                or lease.scope.repository != run.repository
                or (task.repository and lease.scope.repository != task.repository)
                or lease.scope.branch != task.branch_preference
                or run.max_attempts_per_step > lease.scope.max_retries + 1
                or lease.provider != task.provider
                or (lease.agent_id and lease.agent_id != task.external_agent_id)
            ):
                raise AgentRunRepositoryConflict(
                    "Capability lease does not match the approved run scope"
                )

            step_rows = conn.execute(
                "SELECT step_id,run_id,task_id,workspace_id,payload "
                "FROM agent_run_steps WHERE run_id=%s AND workspace_id=%s "
                "ORDER BY sequence,step_id FOR UPDATE",
                (run.id, workspace_id),
            ).fetchall()
            steps: list[AgentRunStep] = []
            for step_row in step_rows:
                try:
                    step = AgentRunStep.model_validate(step_row[4])
                except Exception as error:
                    raise AgentRunRepositoryIntegrityError(
                        "Stored AgentRun step payload is invalid"
                    ) from error
                if (
                    step.id != step_row[0] or step.run_id != run.id
                    or step.task_id != run.task_id or step_row[1] != run.id
                    or step_row[2] != run.task_id or step_row[3] != workspace_id
                ):
                    raise AgentRunRepositoryIntegrityError(
                        "Stored AgentRun step identity or scope is inconsistent"
                    )
                steps.append(step)
            current_step = next(
                (step for step in steps if step.id == run.current_step_id), None
            )
            if (
                run.status in {
                    AgentRunStatus.EVALUATING,
                    AgentRunStatus.BLOCKED,
                    AgentRunStatus.SUCCEEDED,
                    AgentRunStatus.FAILED,
                    AgentRunStatus.CANCELLED,
                }
                or
                current_step is None
                or current_step.status.value == "RUNNING"
                or run.lease_owner is not None
                or run.lease_token is not None
                or any(
                    step.attempt_count > 0
                    and step.step_type.value in {
                        "CREATE_BRANCH", "APPLY_CHANGES", "RUN_VALIDATION",
                        "CREATE_PULL_REQUEST",
                    }
                    for step in steps
                )
            ):
                raise AgentRunRepositoryConflict(
                    "Capability lease must be attached before an active or side-effect attempt"
                )

            updated = run.model_copy(update={"capability_lease_id": lease.id, "updated_at": now})
            event = AgentRunEvent(
                run_id=run.id,
                task_id=run.task_id,
                correlation_id=run.correlation_id,
                event_type=AgentRunEventType.CAPABILITY_LEASE_ATTACHED,
                actor=actor,
                created_at=now,
                message="Signed capability lease attached to the durable run.",
                metadata={
                    "capability_lease_id": lease.id,
                    "mode": lease.mode,
                    "expires_at": lease.expires_at,
                    "approved_payload_hash": lease.approved_payload_hash,
                },
            )
            audit = AuditEvent(
                event_type="agent_run.capability_lease_attached",
                entity_id=run.id,
                workspace_id=workspace_id,
                actor=actor,
                details={
                    "event_id": event.id,
                    "capability_lease_id": lease.id,
                    "approved_payload_hash": lease.approved_payload_hash,
                },
                created_at=now,
            )
            changed = conn.execute(
                "UPDATE agent_runs SET updated_at=%s,payload=%s "
                "WHERE run_id=%s AND workspace_id=%s AND payload->>'capability_lease_id' IS NULL",
                (now, Jsonb(updated.model_dump(mode="json")), run.id, workspace_id),
            )
            if changed.rowcount != 1:
                raise AgentRunRepositoryConflict(
                    "AgentRun changed during capability binding"
                )
            conn.execute(
                "INSERT INTO agent_run_events(event_id,run_id,task_id,workspace_id,"
                "created_at,event_type,payload) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (event.id, run.id, run.task_id, workspace_id, event.created_at,
                 event.event_type.value, Jsonb(event.model_dump(mode="json"))),
            )
            conn.execute(
                "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,"
                "entity_id,event_type,actor,details,created_at,payload) "
                "VALUES(%s,%s,NULL,'agent_run',%s,%s,%s,%s,%s,%s)",
                (audit.id, workspace_id, run.id, audit.event_type, actor,
                 Jsonb(audit.details), now, Jsonb(audit.model_dump(mode="json"))),
            )
            return updated

    def commit_step_completion(
        self,
        *,
        expected_run: AgentRun,
        updated_run: AgentRun,
        expected_steps: list[AgentRunStep],
        updated_steps: list[AgentRunStep],
        events: list[AgentRunEvent],
        audit: AuditEvent,
        now: datetime,
    ) -> None:
        _validate_step_completion(
            expected_run=expected_run,
            updated_run=updated_run,
            expected_steps=expected_steps,
            updated_steps=updated_steps,
            events=events,
            audit=audit,
            now=now,
        )
        if not expected_run.workspace_id:
            raise AgentRunRepositoryConflict("PostgreSQL step completion requires workspace scope")
        with self._connect() as conn:
            run_row = conn.execute(
                "SELECT run_id,workspace_id,task_id,project_id,payload FROM agent_runs "
                "WHERE run_id=%s FOR UPDATE",
                (expected_run.id,),
            ).fetchone()
            if run_row is None:
                raise AgentRunRepositoryConflict("AgentRun is missing during step completion")
            durable_run = _validate_run(
                payload=run_row[4],
                row_id=run_row[0],
                workspace_id=run_row[1],
                task_id=run_row[2],
                project_id=run_row[3],
            )
            if durable_run != expected_run:
                raise AgentRunRepositoryConflict("AgentRun changed before step completion")

            checked_at = conn.execute("SELECT clock_timestamp()").fetchone()[0]
            if expected_run.lease_expires_at is None or expected_run.lease_expires_at <= checked_at:
                raise AgentRunRepositoryConflict("AgentRun lease expired before step completion")

            step_rows: dict[str, tuple[AgentRunStep, str]] = {}
            for expected_step in expected_steps:
                row = conn.execute(
                    "SELECT step_id,run_id,task_id,workspace_id,payload FROM agent_run_steps "
                    "WHERE step_id=%s FOR UPDATE",
                    (expected_step.id,),
                ).fetchone()
                if row is None:
                    raise AgentRunRepositoryConflict("AgentRun step is missing during completion")
                try:
                    durable_step = AgentRunStep.model_validate(row[4])
                except Exception as error:
                    raise AgentRunRepositoryIntegrityError("Stored AgentRun step payload is invalid") from error
                if (
                    row[0] != expected_step.id
                    or row[1] != expected_run.id
                    or row[2] != expected_run.task_id
                    or row[3] != expected_run.workspace_id
                    or durable_step != expected_step
                ):
                    raise AgentRunRepositoryConflict("AgentRun step changed before completion")
                step_rows[expected_step.id] = (expected_step, row[4])

            changed = conn.execute(
                "UPDATE agent_runs SET status=%s,current_step_id=%s,lease_owner=%s,lease_token=%s,"
                "lease_expires_at=%s,updated_at=%s,payload=%s WHERE run_id=%s AND workspace_id=%s "
                "AND lease_owner=%s AND lease_token=%s AND lease_expires_at>clock_timestamp()",
                (
                    updated_run.status.value,
                    updated_run.current_step_id,
                    updated_run.lease_owner,
                    updated_run.lease_token,
                    updated_run.lease_expires_at,
                    updated_run.updated_at,
                    Jsonb(updated_run.model_dump(mode="json")),
                    updated_run.id,
                    updated_run.workspace_id,
                    expected_run.lease_owner,
                    expected_run.lease_token,
                ),
            )
            if changed.rowcount != 1:
                raise AgentRunRepositoryConflict("AgentRun changed during step completion")
            for updated_step in updated_steps:
                changed = conn.execute(
                    "UPDATE agent_run_steps SET status=%s,updated_at=%s,payload=%s "
                    "WHERE step_id=%s AND run_id=%s AND workspace_id=%s",
                    (
                        updated_step.status.value,
                        updated_step.updated_at,
                        Jsonb(updated_step.model_dump(mode="json")),
                        updated_step.id,
                        updated_step.run_id,
                        updated_run.workspace_id,
                    ),
                )
                if changed.rowcount != 1:
                    raise AgentRunRepositoryConflict("AgentRun step changed during completion")
            for event in events:
                conn.execute(
                    "INSERT INTO agent_run_events(event_id,run_id,task_id,workspace_id,created_at,"
                    "event_type,payload) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    (
                        event.id, event.run_id, event.task_id, updated_run.workspace_id,
                        event.created_at, event.event_type.value,
                        Jsonb(event.model_dump(mode="json")),
                    ),
                )
            conn.execute(
                "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,"
                "event_type,actor,details,created_at,payload) "
                "VALUES(%s,%s,NULL,'agent_run',%s,%s,%s,%s,%s,%s)",
                (
                    audit.id, updated_run.workspace_id, updated_run.id, audit.event_type,
                    audit.actor, Jsonb(audit.details), audit.created_at,
                    Jsonb(audit.model_dump(mode="json")),
                ),
            )

    def create(
        self,
        *,
        run: AgentRun,
        steps: list[AgentRunStep],
        events: list[AgentRunEvent],
        audit: AuditEvent,
        scheduled_audit: AuditEvent | None = None,
    ) -> None:
        _validate_run_creation(
            run=run,
            steps=steps,
            events=events,
            audit=audit,
            scheduled_audit=scheduled_audit,
        )
        if not run.workspace_id or not run.project_id or not run.approval_request_id:
            raise AgentRunRepositoryConflict(
                "PostgreSQL AgentRun creation requires project, workspace, and approval scope"
            )
        try:
            with self._connect() as conn:
                parent = conn.execute(
                    "SELECT task.payload,project.payload,approval.payload "
                    "FROM agent_tasks AS task "
                    "JOIN projects AS project ON project.project_id=task.project_id "
                    "AND project.workspace_id=task.workspace_id "
                    "JOIN approval_requests AS approval ON approval.approval_id=%s "
                    "AND approval.project_id=project.project_id "
                    "AND approval.workspace_id=project.workspace_id "
                    "WHERE task.task_id=%s AND task.workspace_id=%s "
                    "AND task.project_id=%s FOR UPDATE OF task FOR KEY SHARE OF project,approval",
                    (run.approval_request_id, run.task_id, run.workspace_id, run.project_id),
                ).fetchone()
                if parent is None:
                    raise AgentRunRepositoryConflict("Approved task/project/approval scope is missing")
                _validate_created_task_scope(
                    run=run,
                    task_payload=parent[0],
                    project_payload=parent[1],
                    approval_payload=parent[2],
                )
                conn.execute(
                    "INSERT INTO agent_runs(run_id,task_id,project_id,workspace_id,approval_id,status,"
                    "current_step_id,lease_owner,lease_token,lease_expires_at,updated_at,payload) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        run.id, run.task_id, run.project_id, run.workspace_id,
                        run.approval_request_id, run.status.value, run.current_step_id,
                        run.lease_owner, run.lease_token, run.lease_expires_at,
                        run.updated_at, Jsonb(run.model_dump(mode="json")),
                    ),
                )
                for step in sorted(steps, key=lambda item: item.sequence):
                    conn.execute(
                        "INSERT INTO agent_run_steps(step_id,run_id,task_id,workspace_id,sequence,"
                        "status,updated_at,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            step.id, run.id, step.task_id, run.workspace_id, step.sequence,
                            step.status.value, step.updated_at, Jsonb(step.model_dump(mode="json")),
                        ),
                    )
                for event in events:
                    conn.execute(
                        "INSERT INTO agent_run_events(event_id,run_id,task_id,workspace_id,created_at,"
                        "event_type,payload) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                        (
                            event.id, run.id, event.task_id, run.workspace_id,
                            event.created_at, event.event_type.value,
                            Jsonb(event.model_dump(mode="json")),
                        ),
                    )
                conn.execute(
                    "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,"
                    "event_type,actor,details,created_at,payload) "
                    "VALUES(%s,%s,NULL,'agent_run',%s,%s,%s,%s,%s,%s)",
                    (
                        audit.id, run.workspace_id, run.id, audit.event_type, audit.actor,
                        Jsonb(audit.details), audit.created_at, Jsonb(audit.model_dump(mode="json")),
                    ),
                )
                if scheduled_audit is not None:
                    conn.execute(
                        "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,"
                        "event_type,actor,details,created_at,payload) "
                        "VALUES(%s,%s,NULL,'agent_task',%s,%s,%s,%s,%s,%s)",
                        (
                            scheduled_audit.id,
                            scheduled_audit.workspace_id,
                            scheduled_audit.entity_id,
                            scheduled_audit.event_type,
                            scheduled_audit.actor,
                            Jsonb(scheduled_audit.details),
                            scheduled_audit.created_at,
                            Jsonb(scheduled_audit.model_dump(mode="json")),
                        ),
                    )
        except psycopg.errors.UniqueViolation as error:
            raise AgentRunRepositoryConflict("AgentRun creation conflicts with existing durable state") from error
