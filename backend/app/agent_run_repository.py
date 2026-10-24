from __future__ import annotations

import sqlite3
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

import psycopg
from psycopg import Connection
from psycopg.types.json import Jsonb

from .agent_run_models import (
    DEFAULT_AGENT_RUN_STEPS,
    AgentRun,
    AgentRunEvent,
    AgentRunEventType,
    AgentRunStatus,
    AgentRunStep,
    AgentRunStepStatus,
    AgentRunStepType,
)
from .agent_run_state_machine import can_transition_run, can_transition_step
from .agent_task_models import AgentTask, AgentTaskEvent, AgentTaskEventType, AgentTaskStatus
from .postgres_agent_run_branch import AgentRunBranchIntent, BranchIntentStatus, _payload as branch_intent_payload
from .postgres_agent_run_apply_changes import AgentRunApplyChangeIntent, ApplyChangeIntentStatus
from .postgres_agent_run_pull_request import AgentRunPullRequestIntent, PullRequestIntentStatus
from .approval_integrity import compute_approval_payload_hash
from .models import ApprovalRequest, ApprovalStatus, AuditEvent, Project
from .postgres_trust_repository import PostgresTrustRepository
from .trust_crypto import sha256_hex, verify_payload_signature
from .trust_models import ActionConsumption, CapabilityActionType, CapabilityLeaseStatus
from .trust_policy import lease_receipt_payload


class AgentRunRepositoryIntegrityError(RuntimeError):
    """A durable AgentRun row does not match its indexed identity or scope."""


class AgentRunRepositoryConflict(RuntimeError):
    """A durable AgentRun creation conflicts with existing or inconsistent state."""


class AgentRunRepositoryNotFound(AgentRunRepositoryConflict):
    """A workspace-scoped durable AgentRun or lease is not visible."""


@dataclass(frozen=True)
class PostgresAgentRunMetricsSnapshot:
    """Workspace-scoped AgentRun aggregates read from a caller snapshot."""

    run_status_counts: dict[str, int]
    step_status_counts: dict[tuple[str, str], int]
    queue_depth: int
    retry_attempts: int
    duplicate_deliveries: int
    stale_lease_recoveries: int
    step_duration_seconds: dict[str, tuple[float, int]]
    expired_leases: int


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
        branch_intent_id: str | None = None,
        branch_task_event: AgentTaskEvent | None = None,
        branch_task_audit: AuditEvent | None = None,
        apply_change_intent_id: str | None = None,
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
        or not can_transition_run(expected_run.status, updated_run.status)
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
            or not can_transition_step(
                expected.status, updated_by_id[step_id].status
            )
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
        branch_intent_id: str | None = None,
        branch_task_event: AgentTaskEvent | None = None,
        branch_task_audit: AuditEvent | None = None,
        apply_change_intent_id: str | None = None,
    ) -> None:
        if any(item is not None for item in (
            branch_intent_id, branch_task_event, branch_task_audit, apply_change_intent_id
        )):
            raise AgentRunRepositoryConflict(
                "SQLite does not support PostgreSQL governed intent completion"
            )
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

    def metrics_snapshot(
        self,
        conn: Connection,
        *,
        workspace_id: str,
        now: datetime,
    ) -> PostgresAgentRunMetricsSnapshot:
        """Read bounded AgentRun aggregates using the caller's DB snapshot.

        The migration constraints bind the indexed identity/status columns to
        the JSON payload. Queries use those indexed columns and workspace
        predicates, avoiding unbounded row materialization and process maps.
        """
        if not workspace_id or not workspace_id.strip():
            raise ValueError("AgentRun metrics require a workspace")
        if now.utcoffset() is None:
            raise ValueError("AgentRun metrics time must be timezone-aware")

        run_status_counts: dict[str, int] = {}
        expired_leases = 0
        for state, count, expired_count in conn.execute(
            "SELECT status,COUNT(*)::bigint,"
            "COUNT(*) FILTER (WHERE lease_expires_at IS NOT NULL "
            "AND lease_expires_at<=%s)::bigint "
            "FROM agent_runs WHERE workspace_id=%s GROUP BY status",
            (now, workspace_id),
        ).fetchall():
            if state not in {item.value for item in AgentRunStatus}:
                raise AgentRunRepositoryIntegrityError(
                    "AgentRun metrics contain an unsupported state"
                )
            run_status_counts[state] = int(count)
            expired_leases += int(expired_count)

        step_status_counts: dict[tuple[str, str], int] = {}
        step_duration_ms: dict[str, tuple[int, int]] = {}
        queue_depth = 0
        retry_attempts = 0
        for step_type, state, count, retries, duration_ms, duration_count, queued in conn.execute(
            "SELECT step.payload->>'step_type',step.status,COUNT(*)::bigint,"
            "COALESCE(SUM(GREATEST(COALESCE((step.payload->>'attempt_count')::integer,1)-1,0)),0)::bigint,"
            "COALESCE(SUM((step.payload->>'duration_ms')::integer) FILTER "
            "(WHERE step.payload ? 'duration_ms' "
            "AND step.payload->>'duration_ms' IS NOT NULL),0)::bigint,"
            "COUNT(*) FILTER (WHERE step.payload ? 'duration_ms' "
            "AND step.payload->>'duration_ms' IS NOT NULL)::bigint,"
            "COUNT(*) FILTER (WHERE step.status IN ('PENDING','READY','RETRY_WAIT'))::bigint "
            "FROM agent_run_steps AS step JOIN agent_runs AS run "
            "ON run.run_id=step.run_id AND run.workspace_id=step.workspace_id "
            "WHERE run.workspace_id=%s "
            "GROUP BY step.payload->>'step_type',step.status",
            (workspace_id,),
        ).fetchall():
            if (
                step_type not in {item.value for item in AgentRunStepType}
                or state not in {item.value for item in AgentRunStepStatus}
            ):
                raise AgentRunRepositoryIntegrityError(
                    "AgentRun step metrics contain an unsupported type or state"
                )
            key = (step_type, state)
            step_status_counts[key] = int(count)
            retry_attempts += int(retries)
            queue_depth += int(queued)
            if duration_count:
                prior_sum, prior_count = step_duration_ms.get(step_type, (0, 0))
                step_duration_ms[step_type] = (
                    prior_sum + int(duration_ms),
                    prior_count + int(duration_count),
                )

        duplicate_deliveries = 0
        stale_lease_recoveries = 0
        for event_type, count in conn.execute(
            "SELECT event.event_type,COUNT(*)::bigint FROM agent_run_events AS event "
            "JOIN agent_runs AS run ON run.run_id=event.run_id "
            "AND run.workspace_id=event.workspace_id "
            "WHERE run.workspace_id=%s AND event.event_type=ANY(%s) "
            "GROUP BY event.event_type",
            (
                workspace_id,
                [
                    AgentRunEventType.DUPLICATE_DELIVERY_IGNORED.value,
                    AgentRunEventType.STALE_LEASE_RECOVERED.value,
                ],
            ),
        ).fetchall():
            if event_type == AgentRunEventType.DUPLICATE_DELIVERY_IGNORED.value:
                duplicate_deliveries = int(count)
            elif event_type == AgentRunEventType.STALE_LEASE_RECOVERED.value:
                stale_lease_recoveries = int(count)

        return PostgresAgentRunMetricsSnapshot(
            run_status_counts=run_status_counts,
            step_status_counts=step_status_counts,
            queue_depth=queue_depth,
            retry_attempts=retry_attempts,
            duplicate_deliveries=duplicate_deliveries,
            stale_lease_recoveries=stale_lease_recoveries,
            step_duration_seconds={
                step_type: (duration_sum / 1000.0, sample_count)
                for step_type, (duration_sum, sample_count) in step_duration_ms.items()
            },
            expired_leases=expired_leases,
        )

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
        branch_intent_id: str | None = None,
        branch_task_event: AgentTaskEvent | None = None,
        branch_task_audit: AuditEvent | None = None,
        apply_change_intent_id: str | None = None,
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
        if (branch_intent_id is None) != (branch_task_event is None) or (
            branch_intent_id is None
        ) != (branch_task_audit is None):
            raise ValueError("Branch intent completion requires its task event and audit together")
        if branch_intent_id is not None and apply_change_intent_id is not None:
            raise ValueError("A completion cannot bind both branch and file mutation intents")
        if not expected_run.workspace_id:
            raise AgentRunRepositoryConflict("PostgreSQL step completion requires workspace scope")
        with self._connect() as conn:
            if branch_intent_id is not None:
                # Follow governed-step lock order. This completion reports an
                # already verified remote side effect; it must not make another
                # provider request. A later revocation cannot erase that fact.
                if not expected_run.approval_request_id:
                    raise AgentRunRepositoryConflict("Branch completion lacks approval scope")
                approval_row = conn.execute(
                    "SELECT approval_id,project_id,workspace_id,status,payload "
                    "FROM approval_requests WHERE approval_id=%s AND workspace_id=%s FOR SHARE",
                    (expected_run.approval_request_id, expected_run.workspace_id),
                ).fetchone()
                task_row = conn.execute(
                    "SELECT task_id,project_id,workspace_id,approval_request_id,status,payload "
                    "FROM agent_tasks WHERE task_id=%s AND workspace_id=%s FOR SHARE",
                    (expected_run.task_id, expected_run.workspace_id),
                ).fetchone()
                if approval_row is None or task_row is None:
                    raise AgentRunRepositoryConflict("Branch completion authority scope is missing")
                try:
                    approval_scope = ApprovalRequest.model_validate(approval_row[4])
                    task_scope = AgentTask.model_validate(task_row[5])
                except Exception as error:
                    raise AgentRunRepositoryIntegrityError("Branch completion authority payload is invalid") from error
                if (
                    approval_scope.id != approval_row[0]
                    or approval_scope.project_id != approval_row[1]
                    or approval_scope.workspace_id != approval_row[2]
                    or approval_scope.status.value != approval_row[3]
                    or task_scope.id != task_row[0]
                    or task_scope.project_id != task_row[1]
                    or task_scope.workspace_id != task_row[2]
                    or task_scope.approval_request_id != task_row[3]
                    or task_scope.status.value != task_row[4]
                    or task_scope.id != expected_run.task_id
                    or task_scope.project_id != expected_run.project_id
                    or task_scope.workspace_id != expected_run.workspace_id
                    or task_scope.approval_request_id != expected_run.approval_request_id
                    or approval_scope.id != expected_run.approval_request_id
                    or approval_scope.project_id != expected_run.project_id
                ):
                    raise AgentRunRepositoryIntegrityError("Branch completion authority scope is inconsistent")
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

            branch_intent = None
            if branch_intent_id is not None:
                intent_row = conn.execute(
                    "SELECT intent_id,workspace_id,run_id,task_id,approval_id,step_id,action_id,lease_id,"
                    "idempotency_key,repository,source_branch,target_ref,source_sha,lease_token_hash,"
                    "initial_lease_token_hash,run_revision,step_revision,status,branch_url,reason,created_at,updated_at,payload "
                    "FROM agent_run_branch_intents WHERE intent_id=%s AND workspace_id=%s FOR UPDATE",
                    (branch_intent_id, expected_run.workspace_id),
                ).fetchone()
                if intent_row is None:
                    raise AgentRunRepositoryConflict("Branch intent is missing during completion")
                try:
                    branch_intent = AgentRunBranchIntent.model_validate(intent_row[22])
                except Exception as error:
                    raise AgentRunRepositoryIntegrityError("Stored branch intent is invalid") from error
                indexed = (
                    branch_intent.id, branch_intent.workspace_id, branch_intent.run_id,
                    branch_intent.task_id, branch_intent.approval_id, branch_intent.step_id,
                    branch_intent.action_id, branch_intent.lease_id, branch_intent.idempotency_key,
                    branch_intent.repository, branch_intent.source_branch, branch_intent.target_ref,
                    branch_intent.source_sha, branch_intent.lease_token_hash,
                    branch_intent.initial_lease_token_hash, branch_intent.run_revision,
                    branch_intent.step_revision, branch_intent.status.value,
                    branch_intent.branch_url, branch_intent.reason,
                    branch_intent.created_at, branch_intent.updated_at,
                )
                if indexed != tuple(intent_row[:22]):
                    raise AgentRunRepositoryIntegrityError("Branch intent indexed state is inconsistent")
                branch_steps = [item for item in expected_steps if item.id == branch_intent.step_id]
                if (
                    branch_intent.status != BranchIntentStatus.REMOTE_VERIFIED
                    or branch_intent.workspace_id != expected_run.workspace_id
                    or branch_intent.run_id != expected_run.id
                    or branch_intent.task_id != expected_run.task_id
                    or branch_intent.approval_id != expected_run.approval_request_id
                    or branch_intent.lease_id != expected_run.capability_lease_id
                    or len(branch_steps) != 1
                    or branch_steps[0].step_type != AgentRunStepType.CREATE_BRANCH
                    or branch_steps[0].idempotency_key != branch_intent.idempotency_key
                    or branch_intent.run_revision != expected_run.updated_at
                    or branch_intent.step_revision != branch_steps[0].updated_at
                    or branch_intent.lease_token_hash
                    != sha256_hex({"lease_token": expected_run.lease_token})
                    or not expected_run.lease_owner
                    or branch_task_event is None
                    or branch_task_audit is None
                    or branch_task_event.task_id != branch_intent.task_id
                    or branch_task_event.actor != expected_run.lease_owner
                    or branch_task_event.event_type != AgentTaskEventType.NOTE
                    or branch_task_event.status is not None
                    or branch_task_event.metadata.get("branch_intent_id") != branch_intent.id
                    or branch_task_audit.workspace_id != branch_intent.workspace_id
                    or branch_task_audit.entity_id != branch_intent.task_id
                    or branch_task_audit.actor != expected_run.lease_owner
                    or branch_task_audit.event_type != "agent_task.branch_created"
                    or branch_task_audit.details.get("branch_intent_id") != branch_intent.id
                ):
                    raise AgentRunRepositoryConflict("Branch intent completion CAS or task audit binding is invalid")
            elif any(item is not None for item in (branch_task_event, branch_task_audit)):
                raise ValueError("Branch completion records require a branch intent")
            apply_intent = None
            if apply_change_intent_id is not None:
                apply_row = conn.execute(
                    "SELECT intent_id,workspace_id,run_id,task_id,approval_id,step_id,action_id,lease_id,"
                    "idempotency_key,repository,branch,path,change_type,before_sha,desired_content_sha256,"
                    "lease_token_hash,run_revision,step_revision,status,commit_sha,reason,created_at,updated_at,payload "
                    "FROM agent_run_apply_change_intents WHERE intent_id=%s AND workspace_id=%s FOR UPDATE",
                    (apply_change_intent_id, expected_run.workspace_id),
                ).fetchone()
                if apply_row is None:
                    raise AgentRunRepositoryConflict("Apply-change intent is missing during completion")
                try:
                    apply_intent = AgentRunApplyChangeIntent.model_validate(apply_row[23])
                except Exception as error:
                    raise AgentRunRepositoryIntegrityError("Stored apply-change intent is invalid") from error
                apply_indexed = (
                    apply_intent.id, apply_intent.workspace_id, apply_intent.run_id,
                    apply_intent.task_id, apply_intent.approval_id, apply_intent.step_id,
                    apply_intent.action_id, apply_intent.lease_id, apply_intent.idempotency_key,
                    apply_intent.repository, apply_intent.branch, apply_intent.path,
                    apply_intent.change_type, apply_intent.before_sha,
                    apply_intent.desired_content_sha256, apply_intent.lease_token_hash,
                    apply_intent.run_revision, apply_intent.step_revision,
                    apply_intent.status.value, apply_intent.commit_sha, apply_intent.reason,
                    apply_intent.created_at, apply_intent.updated_at,
                )
                apply_step = next((item for item in expected_steps if item.id == apply_intent.step_id), None)
                if (
                    apply_indexed != tuple(apply_row[:23])
                    or apply_intent.status != ApplyChangeIntentStatus.REMOTE_VERIFIED
                    or apply_intent.workspace_id != expected_run.workspace_id
                    or apply_intent.run_id != expected_run.id
                    or apply_intent.task_id != expected_run.task_id
                    or apply_intent.approval_id != expected_run.approval_request_id
                    or apply_intent.lease_id != expected_run.capability_lease_id
                    or apply_step is None
                    or apply_step.step_type != AgentRunStepType.APPLY_CHANGES
                    or apply_step.idempotency_key != apply_intent.idempotency_key
                    or apply_intent.run_revision != expected_run.updated_at
                    or apply_intent.step_revision != apply_step.updated_at
                    or apply_intent.lease_token_hash != sha256_hex({"lease_token": expected_run.lease_token})
                    or not apply_intent.commit_sha
                ):
                    raise AgentRunRepositoryConflict("Apply-change intent completion binding is invalid")
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
            if branch_intent is not None:
                completed_intent = branch_intent.model_copy(update={
                    "status": BranchIntentStatus.COMPLETED,
                    "reason": "Verified remote branch completion committed with AgentRun outcome.",
                    "updated_at": now,
                })
                changed = conn.execute(
                    "UPDATE agent_run_branch_intents SET status=%s,reason=%s,updated_at=%s,payload=%s "
                    "WHERE intent_id=%s AND workspace_id=%s AND status='REMOTE_VERIFIED' "
                    "AND run_revision=%s AND step_revision=%s AND lease_token_hash=%s",
                    (completed_intent.status.value, completed_intent.reason, now,
                     Jsonb(branch_intent_payload(completed_intent)), branch_intent.id,
                     branch_intent.workspace_id, branch_intent.run_revision,
                     branch_intent.step_revision, branch_intent.lease_token_hash),
                )
                if changed.rowcount != 1:
                    raise AgentRunRepositoryConflict("Branch intent completion lost its CAS")
                conn.execute(
                    "INSERT INTO agent_task_events(event_id,task_id,workspace_id,event_type,actor,created_at,payload) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    (branch_task_event.id, branch_task_event.task_id, branch_intent.workspace_id,
                     branch_task_event.event_type.value, branch_task_event.actor,
                     branch_task_event.created_at, Jsonb(branch_task_event.model_dump(mode="json"))),
                )
                conn.execute(
                    "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,event_type,actor,details,created_at,payload) "
                    "VALUES(%s,%s,NULL,'agent_task',%s,%s,%s,%s,%s,%s)",
                    (branch_task_audit.id, branch_intent.workspace_id, branch_intent.task_id,
                     branch_task_audit.event_type, branch_task_audit.actor,
                     Jsonb(branch_task_audit.details), branch_task_audit.created_at,
                     Jsonb(branch_task_audit.model_dump(mode="json"))),
                )
            if apply_intent is not None:
                completed_intent = apply_intent.model_copy(update={
                    "status": ApplyChangeIntentStatus.COMPLETED,
                    "reason": "Verified file mutation committed with AgentRun outcome.",
                    "updated_at": now,
                })
                changed = conn.execute(
                    "UPDATE agent_run_apply_change_intents SET status=%s,reason=%s,updated_at=%s,payload=%s "
                    "WHERE intent_id=%s AND workspace_id=%s AND status='REMOTE_VERIFIED' "
                    "AND run_revision=%s AND step_revision=%s AND lease_token_hash=%s AND commit_sha=%s",
                    (completed_intent.status.value, completed_intent.reason, now,
                     Jsonb(completed_intent.model_dump(mode="json")), apply_intent.id,
                     apply_intent.workspace_id, apply_intent.run_revision,
                     apply_intent.step_revision, apply_intent.lease_token_hash,
                     apply_intent.commit_sha),
                )
                if changed.rowcount != 1:
                    raise AgentRunRepositoryConflict("Apply-change intent completion lost its CAS")
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

    def commit_scope_validation(
        self,
        *,
        expected_run: AgentRun,
        expected_steps: list[AgentRunStep],
        worker_id: str,
        lease_token: str,
        build_records: Callable[
            [AgentTask, ApprovalRequest, AgentRun, list[AgentRunStep]],
            tuple[AgentRun, list[AgentRunStep], list[AgentRunEvent], AuditEvent],
        ],
    ) -> tuple[AgentRun, list[AgentRunStep], list[AgentRunEvent], AuditEvent]:
        """Compatibility wrapper for the atomic VALIDATE_SCOPE transaction."""
        def adapt_builder(task, approval, _task_events, run, steps):
            return build_records(task, approval, run, steps)

        return self.commit_authoritative_task_step(
            expected_run=expected_run,
            expected_steps=expected_steps,
            worker_id=worker_id,
            lease_token=lease_token,
            step_type=AgentRunStepType.VALIDATE_SCOPE,
            build_records=adapt_builder,
        )

    def commit_authoritative_task_step(
        self,
        *,
        expected_run: AgentRun,
        expected_steps: list[AgentRunStep],
        worker_id: str,
        lease_token: str,
        step_type: AgentRunStepType,
        validation_intent_id: str | None = None,
        validation_task_event: AgentTaskEvent | None = None,
        pull_request_intent_id: str | None = None,
        pull_request_task_event: AgentTaskEvent | None = None,
        pull_request_task_audit: AuditEvent | None = None,
        build_records: Callable[
            [AgentTask, ApprovalRequest, list[AgentTaskEvent], AgentRun, list[AgentRunStep]],
            tuple[AgentRun, list[AgentRunStep], list[AgentRunEvent], AuditEvent],
        ],
    ) -> tuple[AgentRun, list[AgentRunStep], list[AgentRunEvent], AuditEvent]:
        """Read task authority/evidence and atomically complete a governed step.

        Lock order is approval -> task -> run -> steps -> validation intent ->
        validation command claims, matching approval/task
        transition writers. VERIFY_RESULT also locks its event rows after the
        task lock; the task row lock blocks repository event writers from
        inserting phantom evidence while the result is computed. The callback
        must be pure and must not perform I/O or side effects. RUN_VALIDATION
        requires a durable validation intent and an exact TESTS_PASSED or
        TESTS_FAILED task event. The intent's pinned Git commit and tree must
        match the action metadata, step result evidence, task event, AgentRun
        result events, and completion audit. A failed task event is permitted
        only for a contiguous command prefix ending at its first deterministic
        failure. Event metadata contains only indexed command result digests;
        it never carries command text or stdout/stderr contents.
        """
        if (
            step_type not in {
                AgentRunStepType.VALIDATE_SCOPE,
                AgentRunStepType.VERIFY_RESULT,
                AgentRunStepType.RUN_VALIDATION,
                AgentRunStepType.CREATE_PULL_REQUEST,
            }
            or not expected_run.workspace_id
            or not expected_run.approval_request_id
            or not expected_run.lease_token
            or expected_run.lease_owner != worker_id
            or expected_run.lease_token != lease_token
        ):
            raise AgentRunRepositoryConflict("Governed step lease identity is invalid")
        if (
            (step_type == AgentRunStepType.RUN_VALIDATION)
            != (validation_intent_id is not None and validation_task_event is not None)
            or (validation_intent_id is None) != (validation_task_event is None)
        ):
            raise ValueError("RUN_VALIDATION completion requires its intent and task event")
        if (
            (step_type == AgentRunStepType.CREATE_PULL_REQUEST)
            != (pull_request_intent_id is not None and pull_request_task_event is not None and pull_request_task_audit is not None)
            or (pull_request_intent_id is None)
            != (pull_request_task_event is None and pull_request_task_audit is None)
        ):
            raise ValueError("CREATE_PULL_REQUEST completion requires intent, task event, and task audit")
        if step_type == AgentRunStepType.CREATE_PULL_REQUEST and (
            validation_intent_id is not None or validation_task_event is not None
        ):
            raise ValueError("CREATE_PULL_REQUEST completion cannot carry validation evidence")

        with self._connect() as conn:
            with conn.transaction():
                approval_row = conn.execute(
                    "SELECT payload,approval_id,workspace_id,project_id,status,version "
                    "FROM approval_requests WHERE approval_id=%s AND workspace_id=%s FOR SHARE",
                    (expected_run.approval_request_id, expected_run.workspace_id),
                ).fetchone()
                if approval_row is None:
                    raise AgentRunRepositoryNotFound(
                        "Governed step approval is not visible in this workspace"
                    )
                try:
                    approval = ApprovalRequest.model_validate(approval_row[0])
                except Exception as error:
                    raise AgentRunRepositoryIntegrityError(
                        "Stored governed-step approval payload is invalid"
                    ) from error
                if (
                    approval.id != approval_row[1]
                    or approval.workspace_id != approval_row[2]
                    or approval.project_id != approval_row[3]
                    or approval.status.value != approval_row[4]
                    or approval.id != expected_run.approval_request_id
                    or approval.workspace_id != expected_run.workspace_id
                    or approval.project_id != expected_run.project_id
                    or int(approval_row[5]) < 0
                ):
                    raise AgentRunRepositoryIntegrityError(
                        "Governed-step approval identity or revision is inconsistent"
                    )

                task_row = conn.execute(
                    "SELECT task_id,project_id,workspace_id,approval_request_id,status,updated_at,payload "
                    "FROM agent_tasks WHERE task_id=%s AND workspace_id=%s FOR UPDATE",
                    (expected_run.task_id, expected_run.workspace_id),
                ).fetchone()
                if task_row is None:
                    raise AgentRunRepositoryNotFound(
                        "Governed-step task is not visible in this workspace"
                    )
                try:
                    task = AgentTask.model_validate(task_row[6])
                except Exception as error:
                    raise AgentRunRepositoryIntegrityError(
                        "Stored governed-step task payload is invalid"
                    ) from error
                if (
                    task.id != task_row[0]
                    or task.project_id != task_row[1]
                    or task.workspace_id != task_row[2]
                    or task.approval_request_id != task_row[3]
                    or task.status.value != task_row[4]
                    or task.updated_at != task_row[5]
                    or task.id != expected_run.task_id
                    or task.project_id != expected_run.project_id
                    or task.workspace_id != expected_run.workspace_id
                    or task.approval_request_id != approval.id
                ):
                    raise AgentRunRepositoryIntegrityError(
                        "Governed-step task identity, revision, or approval link is inconsistent"
                    )
                if (
                    step_type == AgentRunStepType.VALIDATE_SCOPE
                    and task.status != AgentTaskStatus.APPROVED
                ):
                    raise AgentRunRepositoryConflict(
                        "Scope validation task is no longer approved"
                    )
                durable_run, durable_steps = self.get_control_snapshot_in_transaction(
                    conn,
                    run_id=expected_run.id,
                    workspace_id=expected_run.workspace_id,
                )
                if durable_run != expected_run:
                    raise AgentRunRepositoryConflict(
                        "AgentRun revision changed before governed-step completion"
                    )
                if {step.id: step for step in durable_steps} != {
                    step.id: step for step in expected_steps
                }:
                    raise AgentRunRepositoryConflict(
                        "AgentRun step revisions changed before governed-step completion"
                    )
                validation_intent: dict[str, object] | None = None
                validation_claims: list[dict[str, object]] = []
                validation_step: AgentRunStep | None = None
                pull_request_intent: AgentRunPullRequestIntent | None = None
                pull_request_step: AgentRunStep | None = None
                if step_type == AgentRunStepType.RUN_VALIDATION:
                    from .agent_worker_validation_plan import validate_validation_plan

                    validation_step = next(
                        (item for item in durable_steps if item.id == durable_run.current_step_id),
                        None,
                    )
                    if (
                        validation_step is None
                        or validation_step.step_type != AgentRunStepType.RUN_VALIDATION
                        or validation_step.status != AgentRunStepStatus.RUNNING
                        or durable_run.status != AgentRunStatus.RUNNING
                        or validation_step.lease_owner != worker_id
                        or validation_step.lease_token != lease_token
                        or validation_step.lease_expires_at != durable_run.lease_expires_at
                        or approval.status != ApprovalStatus.APPROVED
                        or task.status != AgentTaskStatus.APPROVED
                    ):
                        raise AgentRunRepositoryConflict(
                            "RUN_VALIDATION authority or current step is no longer valid"
                        )
                    if any((
                        task.worker_lease_owner,
                        task.worker_lease_token,
                        task.worker_lease_expires_at,
                    )) and (
                        task.worker_lease_owner != worker_id
                        or task.worker_lease_token != lease_token
                        or task.worker_lease_expires_at is None
                    ):
                        raise AgentRunRepositoryConflict(
                            "AgentTask worker lease belongs to another or incomplete execution"
                        )
                    intent_row = conn.execute(
                        "SELECT intent_id,workspace_id,run_id,task_id,approval_id,step_id,lease_id,"
                        "idempotency_key,approval_hash,test_plan_hash,lease_token_hash,bundle_revision,"
                        "bundle_git_tree_sha,run_revision,step_revision,"
                        "status,reason,created_at,updated_at,payload FROM agent_run_validation_intents "
                        "WHERE intent_id=%s AND workspace_id=%s FOR UPDATE",
                        (validation_intent_id, expected_run.workspace_id),
                    ).fetchone()
                    if intent_row is None:
                        raise AgentRunRepositoryConflict("Validation intent is missing during completion")
                    try:
                        validation_intent = dict(intent_row[19])
                        def _payload_datetime(value: object) -> datetime | None:
                            if isinstance(value, datetime):
                                return value
                            if isinstance(value, str):
                                return datetime.fromisoformat(value.replace("Z", "+00:00"))
                            return None

                        indexed_intent = (
                            validation_intent.get("id"), validation_intent.get("workspace_id"),
                            validation_intent.get("run_id"), validation_intent.get("task_id"),
                            validation_intent.get("approval_id"), validation_intent.get("step_id"),
                            validation_intent.get("lease_id"),
                            validation_intent.get("idempotency_key"), validation_intent.get("approval_hash"),
                            validation_intent.get("test_plan_hash"), validation_intent.get("lease_token_hash"),
                            validation_intent.get("bundle_revision"),
                            validation_intent.get("bundle_git_tree_sha"),
                            _payload_datetime(validation_intent.get("run_revision")),
                            _payload_datetime(validation_intent.get("step_revision")),
                            validation_intent.get("status"), validation_intent.get("reason", ""),
                            _payload_datetime(validation_intent.get("created_at")),
                            _payload_datetime(validation_intent.get("updated_at")),
                        )
                    except Exception as error:
                        raise AgentRunRepositoryIntegrityError(
                            "Stored validation intent payload is invalid"
                        ) from error
                    if indexed_intent != tuple(intent_row[:19]):
                        raise AgentRunRepositoryIntegrityError(
                            "Validation intent indexed state is inconsistent"
                        )
                    approved_plan = list(approval.test_plan)
                    approval_hash = compute_approval_payload_hash(approval)
                    test_plan_hash = sha256_hex(approved_plan)
                    lease_token_hash = sha256_hex({"lease_token": lease_token})
                    bundle_revision = validation_intent.get("bundle_revision")
                    bundle_git_tree_sha = validation_intent.get("bundle_git_tree_sha")
                    plan_error = validate_validation_plan(approval)
                    if (
                        validation_intent.get("status") not in {"EXECUTION_CLAIMED", "RECONCILIATION_REQUIRED"}
                        or validation_intent.get("workspace_id") != durable_run.workspace_id
                        or validation_intent.get("run_id") != durable_run.id
                        or validation_intent.get("task_id") != durable_run.task_id
                        or validation_intent.get("approval_id") != approval.id
                        or validation_intent.get("step_id") != validation_step.id
                        or validation_intent.get("lease_id") != durable_run.capability_lease_id
                        or _payload_datetime(validation_intent.get("run_revision")) != durable_run.updated_at
                        or _payload_datetime(validation_intent.get("step_revision")) != validation_step.updated_at
                        or validation_intent.get("approval_hash") != approval_hash
                        or approval.approved_payload_hash != approval_hash
                        or validation_intent.get("test_plan_hash") != test_plan_hash
                        or validation_intent.get("lease_token_hash") != lease_token_hash
                        or not isinstance(bundle_revision, str)
                        or len(bundle_revision) != 40
                        or any(char not in "0123456789abcdef" for char in bundle_revision)
                        or not isinstance(bundle_git_tree_sha, str)
                        or len(bundle_git_tree_sha) != 40
                        or any(char not in "0123456789abcdef" for char in bundle_git_tree_sha)
                        or plan_error is not None
                    ):
                        raise AgentRunRepositoryConflict(
                            "Validation intent no longer matches approved plan, run, step, or worker lease"
                        )
                    claim_rows = conn.execute(
                        "SELECT intent_id,workspace_id,action_id,command_index,command_hash,status,exit_code,timed_out,"
                        "stdout_sha256,stdout_bytes,stderr_sha256,stderr_bytes,reason,claimed_at,updated_at,payload "
                        "FROM agent_run_validation_command_claims WHERE intent_id=%s AND workspace_id=%s "
                        "ORDER BY command_index FOR UPDATE",
                        (validation_intent_id, durable_run.workspace_id),
                    ).fetchall()
                    if not claim_rows or len(claim_rows) > len(approved_plan):
                        raise AgentRunRepositoryConflict(
                            "Validation command claim set is empty or exceeds the approved plan"
                        )
                    for index, claim_row in enumerate(claim_rows):
                        try:
                            claim = dict(claim_row[15])
                            payload_claimed_at = _payload_datetime(claim.get("claimed_at"))
                            payload_updated_at = _payload_datetime(claim.get("updated_at"))
                        except Exception as error:
                            raise AgentRunRepositoryIntegrityError(
                                "Stored validation command claim payload is invalid"
                            ) from error
                        indexed_claim = (
                            claim.get("intent_id"), claim.get("workspace_id"), claim.get("action_id"),
                            claim.get("command_index"), claim.get("command_hash"), claim.get("status"),
                            claim.get("exit_code"), claim.get("timed_out"), claim.get("stdout_sha256"),
                            claim.get("stdout_bytes"), claim.get("stderr_sha256"), claim.get("stderr_bytes"),
                            claim.get("reason", ""), payload_claimed_at, payload_updated_at,
                        )
                        if indexed_claim != tuple(claim_row[:15]):
                            raise AgentRunRepositoryIntegrityError(
                                "Validation command claim indexed state is inconsistent"
                            )
                        command_hash = sha256_hex({"command": approved_plan[index]})
                        if (
                            claim.get("intent_id") != validation_intent_id
                            or claim.get("workspace_id") != durable_run.workspace_id
                            or not isinstance(claim.get("action_id"), str)
                            or not claim.get("action_id")
                            or type(claim.get("command_index")) is not int
                            or claim.get("command_index") != index
                            or claim.get("command_hash") != command_hash
                            or claim.get("status") != "RESULT_RECORDED"
                            or type(claim.get("exit_code")) is not int
                            or type(claim.get("timed_out")) is not bool
                            or any(
                                not isinstance(claim.get(field), str)
                                or len(claim[field]) != 64
                                or any(char not in "0123456789abcdef" for char in claim[field])
                                for field in ("stdout_sha256", "stderr_sha256")
                            )
                            or any(
                                type(claim.get(field)) is not int or not 0 <= claim[field] <= 1048576
                                for field in ("stdout_bytes", "stderr_bytes")
                            )
                            or any(field in claim for field in ("command", "stdout", "stderr"))
                        ):
                            raise AgentRunRepositoryConflict(
                                "Validation command claim is missing, ambiguous, or not an exact-plan result"
                            )
                        trust = PostgresTrustRepository()
                        action = trust.get_proposed_action(
                            conn, str(claim["action_id"]), durable_run.workspace_id, for_update=True
                        )
                        required_action_metadata = {
                            "step_id": validation_step.id,
                            "step_type": AgentRunStepType.RUN_VALIDATION.value,
                            "idempotency_key": validation_step.idempotency_key,
                            "approval_request_id": approval.id,
                            "approved_payload_hash": approval.approved_payload_hash,
                            "command_source": "approval.test_plan",
                            "command_index": index,
                            "command_sha256": command_hash,
                            "bundle_revision": bundle_revision,
                            "bundle_git_tree_sha": bundle_git_tree_sha,
                        }
                        if (
                            action is None
                            or action.id != claim["action_id"]
                            or action.action_type != CapabilityActionType.RUN_COMMAND
                            or action.command != approved_plan[index]
                            or action.workspace_id != durable_run.workspace_id
                            or action.run_id != durable_run.id
                            or action.task_id != durable_run.task_id
                            or action.project_id != durable_run.project_id
                            or action.lease_id != durable_run.capability_lease_id
                            or action.provider != task.provider
                            or action.agent_id != task.external_agent_id
                            or action.repository != durable_run.repository
                            or action.retry_number != max(0, validation_step.attempt_count - 1)
                            or not 1 <= action.estimated_runtime_seconds <= 3600
                            or any(
                                action.metadata.get(field) != value
                                for field, value in required_action_metadata.items()
                            )
                        ):
                            raise AgentRunRepositoryConflict(
                                "Validation claim is not bound to its exact approved RUN_COMMAND action"
                            )
                        consumption_row = conn.execute(
                            "SELECT action_id,workspace_id,lease_id,consumed_at,payload "
                            "FROM action_consumptions WHERE action_id=%s AND workspace_id=%s FOR SHARE",
                            (action.id, durable_run.workspace_id),
                        ).fetchone()
                        if consumption_row is None:
                            raise AgentRunRepositoryConflict(
                                "Validation RUN_COMMAND lacks its durable action consumption"
                            )
                        try:
                            consumption_envelope = dict(consumption_row[4])
                            consumption = ActionConsumption.model_validate(
                                consumption_envelope["consumption"]
                            )
                        except Exception as error:
                            raise AgentRunRepositoryIntegrityError(
                                "Stored validation action consumption is invalid"
                            ) from error
                        if (
                            consumption_envelope.get("action_id") != consumption_row[0]
                            or consumption_envelope.get("workspace_id") != consumption_row[1]
                            or consumption_envelope.get("lease_id") != consumption_row[2]
                            or _payload_datetime(consumption_envelope.get("consumed_at")) != consumption_row[3]
                            or consumption_row[0] != action.id
                            or consumption_row[1] != durable_run.workspace_id
                            or consumption_row[2] != durable_run.capability_lease_id
                            or consumption_row[3] > claim_row[13]
                            or consumption.evidence != {
                                "workspace_id": durable_run.workspace_id,
                                "run_id": durable_run.id,
                                "task_id": durable_run.task_id,
                                "approval_id": approval.id,
                                "step_id": validation_step.id,
                                "action_id": action.id,
                                "lease_id": durable_run.capability_lease_id,
                                "intent_id": validation_intent_id,
                                "command_index": index,
                                "command_sha256": command_hash,
                                "runtime_seconds_reserved": action.estimated_runtime_seconds,
                                "authorized_attempt_count": validation_step.attempt_count,
                                "authorized_retry_consumed": validation_step.attempt_count > 1,
                            }
                            or type(consumption.evidence.get("command_index")) is not int
                            or type(consumption.evidence.get("authorized_attempt_count")) is not int
                            or type(consumption.evidence.get("authorized_retry_consumed")) is not bool
                            or type(consumption.evidence.get("runtime_seconds_reserved")) is not int
                            or not 1 <= consumption.evidence["runtime_seconds_reserved"] <= 3600
                            or action.estimated_runtime_seconds != consumption.evidence["runtime_seconds_reserved"]
                            or consumption.actual_runtime_seconds != consumption.evidence["runtime_seconds_reserved"]
                            or consumption.actual_cost_usd != 0
                            or consumption.pull_requests_created != 0
                            or consumption.retry_consumed != (validation_step.attempt_count > 1)
                            or consumption.output_reference is not None
                        ):
                            raise AgentRunRepositoryConflict(
                                "Validation action consumption lacks exact command provenance"
                            )
                        validation_claims.append(claim)
                    failure_positions = [
                        index for index, claim in enumerate(validation_claims)
                        if claim["exit_code"] != 0 or claim["timed_out"] is True
                    ]
                    validation_passed = (
                        len(validation_claims) == len(approved_plan)
                        and not failure_positions
                    )
                    validation_failed = (
                        bool(failure_positions)
                        and failure_positions == [len(validation_claims) - 1]
                        and all(
                            claim["exit_code"] == 0 and claim["timed_out"] is False
                            for claim in validation_claims[:-1]
                        )
                    )
                    if not validation_passed and not validation_failed:
                        raise AgentRunRepositoryConflict(
                            "Validation evidence is incomplete, noncontiguous, or contradictory"
                        )
                if step_type == AgentRunStepType.CREATE_PULL_REQUEST:
                    if (
                        task.status != AgentTaskStatus.APPROVED
                        or approval.status != ApprovalStatus.APPROVED
                        or durable_run.status != AgentRunStatus.RUNNING
                        or durable_run.pause_requested or durable_run.cancel_requested
                        or not pull_request_intent_id
                        or pull_request_task_event is None
                        or pull_request_task_audit is None
                    ):
                        raise AgentRunRepositoryConflict(
                            "CREATE_PULL_REQUEST authority is no longer active"
                        )
                    pull_request_step = next(
                        (item for item in durable_steps if item.id == durable_run.current_step_id),
                        None,
                    )
                    if (
                        pull_request_step is None
                        or pull_request_step.step_type != AgentRunStepType.CREATE_PULL_REQUEST
                        or pull_request_step.status != AgentRunStepStatus.RUNNING
                        or pull_request_step.lease_owner != worker_id
                        or pull_request_step.lease_token != lease_token
                        or pull_request_step.lease_expires_at != durable_run.lease_expires_at
                    ):
                        raise AgentRunRepositoryConflict(
                            "CREATE_PULL_REQUEST is not the current worker-leased step"
                        )
                    intent_row = conn.execute(
                        "SELECT intent_id,workspace_id,run_id,task_id,approval_id,step_id,action_id,lease_id,"
                        "idempotency_key,repository,head_branch,base_branch,expected_head_sha,expected_base_sha,"
                        "title_sha256,body_sha256,intent_marker,"
                        "lease_token_hash,run_revision,step_revision,status,pull_number,pull_url,observed_repository,"
                        "observed_head_branch,observed_base_branch,marker_matches,observed_head_sha,observed_base_sha,"
                        "response_sha256,last_get_status,get_match_count,get_response_sha256,reason,created_at,updated_at,payload "
                        "FROM agent_run_pull_request_intents WHERE intent_id=%s AND workspace_id=%s FOR UPDATE",
                        (pull_request_intent_id, durable_run.workspace_id),
                    ).fetchone()
                    if intent_row is None:
                        raise AgentRunRepositoryConflict("Verified pull request intent is missing")
                    try:
                        pull_request_intent = AgentRunPullRequestIntent.model_validate(intent_row[36])
                    except Exception as error:
                        raise AgentRunRepositoryIntegrityError(
                            "Stored pull request intent payload is invalid"
                        ) from error
                    indexed_intent = (
                        pull_request_intent.id, pull_request_intent.workspace_id,
                        pull_request_intent.run_id, pull_request_intent.task_id,
                        pull_request_intent.approval_id, pull_request_intent.step_id,
                        pull_request_intent.action_id, pull_request_intent.lease_id,
                        pull_request_intent.idempotency_key, pull_request_intent.repository,
                        pull_request_intent.head_branch, pull_request_intent.base_branch,
                        pull_request_intent.expected_head_sha, pull_request_intent.expected_base_sha,
                        pull_request_intent.title_sha256, pull_request_intent.body_sha256,
                        pull_request_intent.intent_marker, pull_request_intent.lease_token_hash,
                        pull_request_intent.run_revision, pull_request_intent.step_revision,
                        pull_request_intent.status.value, pull_request_intent.pull_number,
                        pull_request_intent.pull_url, pull_request_intent.observed_repository,
                        pull_request_intent.observed_head_branch,
                        pull_request_intent.observed_base_branch, pull_request_intent.marker_matches,
                        pull_request_intent.observed_head_sha, pull_request_intent.observed_base_sha,
                        pull_request_intent.response_sha256, pull_request_intent.last_get_status,
                        pull_request_intent.get_match_count, pull_request_intent.get_response_sha256,
                        pull_request_intent.reason,
                        pull_request_intent.created_at, pull_request_intent.updated_at,
                    )
                    if indexed_intent != tuple(intent_row[:36]):
                        raise AgentRunRepositoryIntegrityError(
                            "Pull request intent payload differs from indexed identity"
                        )
                    if (
                        pull_request_intent.id != pull_request_intent_id
                        or pull_request_intent.status != PullRequestIntentStatus.REMOTE_VERIFIED
                        or pull_request_intent.workspace_id != durable_run.workspace_id
                        or pull_request_intent.run_id != durable_run.id
                        or pull_request_intent.task_id != task.id
                        or pull_request_intent.approval_id != approval.id
                        or pull_request_intent.step_id != pull_request_step.id
                        or pull_request_intent.lease_id != durable_run.capability_lease_id
                        or pull_request_intent.idempotency_key != pull_request_step.idempotency_key
                        or pull_request_intent.run_revision != durable_run.updated_at
                        or pull_request_intent.step_revision != pull_request_step.updated_at
                        or pull_request_intent.lease_token_hash != sha256_hex({"lease_token": lease_token})
                        or pull_request_intent.repository != task.repository
                        or pull_request_intent.head_branch != task.branch_preference
                        or pull_request_intent.observed_repository != pull_request_intent.repository
                        or pull_request_intent.observed_head_branch != pull_request_intent.head_branch
                        or pull_request_intent.observed_base_branch != pull_request_intent.base_branch
                        or pull_request_intent.marker_matches is not True
                        or not pull_request_intent.pull_number or not pull_request_intent.pull_url
                        or not pull_request_intent.observed_head_sha
                        or not pull_request_intent.observed_base_sha
                        or not pull_request_intent.response_sha256
                    ):
                        raise AgentRunRepositoryConflict(
                            "Pull request intent no longer matches the durable run, approval, or lease"
                        )
                    if pull_request_task_event.event_type != AgentTaskEventType.PR_CREATED:
                        raise AgentRunRepositoryConflict("Pull request completion event has the wrong type")
                    expected_pr_metadata = {
                        "run_id": durable_run.id, "step_id": pull_request_step.id,
                        "pull_request_intent_id": pull_request_intent.id,
                        "repository": pull_request_intent.repository,
                        "head_branch": pull_request_intent.head_branch,
                        "base_branch": pull_request_intent.base_branch,
                        "pull_number": pull_request_intent.pull_number,
                        "pull_url": pull_request_intent.pull_url,
                        "head_sha": pull_request_intent.observed_head_sha,
                        "base_sha": pull_request_intent.observed_base_sha,
                        "title_sha256": pull_request_intent.title_sha256,
                        "body_sha256": pull_request_intent.body_sha256,
                        "intent_marker": pull_request_intent.intent_marker,
                        "response_sha256": pull_request_intent.response_sha256,
                    }
                    if (
                        pull_request_task_event.task_id != task.id
                        or pull_request_task_event.status != AgentTaskStatus.READY_FOR_PR
                        or pull_request_task_event.actor != worker_id
                        or pull_request_task_event.metadata != expected_pr_metadata
                        or pull_request_task_audit.event_type != "agent_task.pull_request_created"
                        or pull_request_task_audit.entity_id != task.id
                        or pull_request_task_audit.workspace_id != task.workspace_id
                        or pull_request_task_audit.actor != worker_id
                        or pull_request_task_audit.details.get("pull_request_intent_id") != pull_request_intent.id
                    ):
                        raise AgentRunRepositoryConflict(
                            "Pull request completion event or audit does not exactly bind verified metadata"
                        )
                    branch_rows = conn.execute(
                        "SELECT payload FROM agent_run_branch_intents WHERE workspace_id=%s AND run_id=%s AND task_id=%s AND status='COMPLETED'",
                        (durable_run.workspace_id, durable_run.id, task.id),
                    ).fetchall()
                    apply_rows = conn.execute(
                        "SELECT payload FROM agent_run_apply_change_intents WHERE workspace_id=%s AND run_id=%s AND task_id=%s AND status='COMPLETED'",
                        (durable_run.workspace_id, durable_run.id, task.id),
                    ).fetchall()
                    validation_rows = conn.execute(
                        "SELECT payload FROM agent_run_validation_intents WHERE workspace_id=%s AND run_id=%s AND task_id=%s AND status='COMPLETED'",
                        (durable_run.workspace_id, durable_run.id, task.id),
                    ).fetchall()
                    if len(branch_rows) != 1 or len(apply_rows) != 1 or len(validation_rows) != 1:
                        raise AgentRunRepositoryConflict(
                            "Pull request requires exactly one completed durable branch, apply, and validation intent"
                        )
                    try:
                        from .postgres_agent_run_validation import AgentRunValidationIntent

                        branch_intent = AgentRunBranchIntent.model_validate(branch_rows[0][0])
                        apply_intent = AgentRunApplyChangeIntent.model_validate(apply_rows[0][0])
                        validation_intent = AgentRunValidationIntent.model_validate(validation_rows[0][0])
                    except Exception as error:
                        raise AgentRunRepositoryIntegrityError(
                            "Pull request predecessor intent payload is invalid"
                        ) from error
                    if (
                        branch_intent.repository != pull_request_intent.repository
                        or branch_intent.target_ref != pull_request_intent.head_branch
                        or apply_intent.repository != pull_request_intent.repository
                        or apply_intent.branch != pull_request_intent.head_branch
                        or not apply_intent.commit_sha
                        or validation_intent.bundle_revision != apply_intent.commit_sha
                        or validation_intent.workspace_id != pull_request_intent.workspace_id
                        or validation_intent.run_id != pull_request_intent.run_id
                        or validation_intent.task_id != pull_request_intent.task_id
                        or validation_intent.approval_id != pull_request_intent.approval_id
                        or pull_request_intent.expected_head_sha != validation_intent.bundle_revision
                        or pull_request_intent.observed_head_sha != validation_intent.bundle_revision
                        or pull_request_intent.observed_head_sha != pull_request_intent.expected_head_sha
                        or pull_request_intent.observed_base_sha != pull_request_intent.expected_base_sha
                    ):
                        raise AgentRunRepositoryConflict(
                            "Pull request does not match durable validated branch/apply authority"
                        )
                    if (
                        [item.step_type for item in durable_steps] != list(DEFAULT_AGENT_RUN_STEPS)
                        or [item.sequence for item in durable_steps] != list(range(len(DEFAULT_AGENT_RUN_STEPS)))
                        or any(
                            item.sequence < pull_request_step.sequence
                            and item.status != AgentRunStepStatus.SUCCEEDED
                            for item in durable_steps
                        )
                        or branch_intent.step_id != next(item.id for item in durable_steps if item.step_type == AgentRunStepType.CREATE_BRANCH)
                        or apply_intent.step_id != next(item.id for item in durable_steps if item.step_type == AgentRunStepType.APPLY_CHANGES)
                        or validation_intent.step_id != next(item.id for item in durable_steps if item.step_type == AgentRunStepType.RUN_VALIDATION)
                    ):
                        raise AgentRunRepositoryConflict(
                            "Pull request requires the canonical run plan with every predecessor succeeded"
                        )
                    trust = PostgresTrustRepository()
                    action = trust.get_proposed_action(
                        conn, pull_request_intent.action_id, pull_request_intent.workspace_id, for_update=True
                    )
                    policy = trust.get_latest_policy_decision_for_action(
                        conn, pull_request_intent.action_id, pull_request_intent.workspace_id, for_update=True
                    )
                    lease = trust.get_capability_lease(
                        conn, pull_request_intent.lease_id, pull_request_intent.workspace_id, for_update=True
                    )
                    if (
                        action is None or policy is None or lease is None
                        or policy.decision.value != "ALLOW"
                        or action.action_type != CapabilityActionType.CREATE_PULL_REQUEST
                        or action.workspace_id != pull_request_intent.workspace_id
                        or action.run_id != pull_request_intent.run_id
                        or action.task_id != pull_request_intent.task_id
                        or action.lease_id != pull_request_intent.lease_id
                        or action.repository != pull_request_intent.repository
                        or action.branch != pull_request_intent.head_branch
                        or action.retry_number != max(0, pull_request_step.attempt_count - 1)
                        or any(action.metadata.get(key) != value for key, value in {
                            "repository": pull_request_intent.repository,
                            "head_branch": pull_request_intent.head_branch,
                            "base_branch": pull_request_intent.base_branch,
                            "title_sha256": pull_request_intent.title_sha256,
                            "body_sha256": pull_request_intent.body_sha256,
                            "intent_marker": pull_request_intent.intent_marker,
                            "step_type": AgentRunStepType.CREATE_PULL_REQUEST.value,
                            "idempotency_key": pull_request_intent.idempotency_key,
                            "step_id": pull_request_intent.step_id,
                            "task_id": pull_request_intent.task_id,
                            "approval_id": pull_request_intent.approval_id,
                            "lease_id": pull_request_intent.lease_id,
                            "validated_head_sha": validation_intent.bundle_revision,
                            "validated_base_sha": pull_request_intent.expected_base_sha,
                        }.items())
                        or lease.status != CapabilityLeaseStatus.ACTIVE
                        or lease.approval_request_id != approval.id
                        or lease.workspace_id != task.workspace_id
                        or lease.scope.repository != pull_request_intent.repository
                        or lease.scope.branch != pull_request_intent.head_branch
                        or CapabilityActionType.CREATE_PULL_REQUEST not in lease.scope.allowed_actions
                    ):
                        raise AgentRunRepositoryConflict(
                            "Pull request trust policy, action, or capability lease is not valid"
                        )
                    consumption_row = conn.execute(
                        "SELECT action_id,workspace_id,lease_id,consumed_at,payload FROM action_consumptions "
                        "WHERE action_id=%s AND workspace_id=%s FOR SHARE",
                        (action.id, pull_request_intent.workspace_id),
                    ).fetchone()
                    if consumption_row is None:
                        raise AgentRunRepositoryConflict("Pull request action has no durable capability consumption")
                    try:
                        consumption_envelope = dict(consumption_row[4])
                        consumption = ActionConsumption.model_validate(consumption_envelope["consumption"])
                    except Exception as error:
                        raise AgentRunRepositoryIntegrityError("Pull request capability consumption is invalid") from error
                    expected_consumption = {
                        "workspace_id": pull_request_intent.workspace_id,
                        "run_id": pull_request_intent.run_id,
                        "task_id": pull_request_intent.task_id,
                        "approval_id": pull_request_intent.approval_id,
                        "step_id": pull_request_intent.step_id,
                        "action_id": pull_request_intent.action_id,
                        "lease_id": pull_request_intent.lease_id,
                        "intent_id": pull_request_intent.id,
                        "repository": pull_request_intent.repository,
                        "head_branch": pull_request_intent.head_branch,
                        "base_branch": pull_request_intent.base_branch,
                        "intent_marker": pull_request_intent.intent_marker,
                        "runtime_seconds_reserved": action.estimated_runtime_seconds,
                        "authorized_attempt_count": pull_request_step.attempt_count,
                        "authorized_retry_consumed": pull_request_step.attempt_count > 1,
                    }
                    if (
                        consumption_row[0] != action.id
                        or consumption_row[1] != pull_request_intent.workspace_id
                        or consumption_row[2] != pull_request_intent.lease_id
                        or consumption_envelope.get("action_id") != action.id
                        or consumption_envelope.get("workspace_id") != pull_request_intent.workspace_id
                        or consumption_envelope.get("lease_id") != pull_request_intent.lease_id
                        or consumption.evidence != expected_consumption
                        or consumption.pull_requests_created != 1
                        or consumption.actual_runtime_seconds != action.estimated_runtime_seconds
                        or consumption.retry_consumed != (pull_request_step.attempt_count > 1)
                        or consumption.actual_cost_usd != 0
                        or consumption.output_reference is not None
                    ):
                        raise AgentRunRepositoryConflict("Pull request capability consumption is inconsistent")
                checked_at = conn.execute("SELECT clock_timestamp()").fetchone()[0]
                if (
                    durable_run.lease_owner != worker_id
                    or durable_run.lease_token != lease_token
                    or durable_run.lease_expires_at is None
                    or durable_run.lease_expires_at <= checked_at
                ):
                    raise AgentRunRepositoryConflict(
                        "AgentRun lease is stale before governed-step completion"
                    )
                current_step = next(
                    (item for item in durable_steps if item.id == durable_run.current_step_id),
                    None,
                )
                expected_current_status = (
                    AgentRunStepStatus.RUNNING
                    if step_type in {AgentRunStepType.RUN_VALIDATION, AgentRunStepType.CREATE_PULL_REQUEST}
                    else AgentRunStepStatus.READY
                )
                if (
                    current_step is None
                    or current_step.step_type != step_type
                    or current_step.status != expected_current_status
                ):
                    raise AgentRunRepositoryConflict(
                        f"{step_type.value} step is no longer ready for completion"
                    )
                if step_type == AgentRunStepType.VERIFY_RESULT and (
                    [item.step_type for item in durable_steps] != list(DEFAULT_AGENT_RUN_STEPS)
                    or [item.sequence for item in durable_steps]
                    != list(range(len(DEFAULT_AGENT_RUN_STEPS)))
                    or any(
                        item.sequence < current_step.sequence
                        and item.status != AgentRunStepStatus.SUCCEEDED
                        for item in durable_steps
                    )
                ):
                    raise AgentRunRepositoryConflict(
                        "VERIFY_RESULT requires the canonical plan with every prior step succeeded"
                    )

                task_events: list[AgentTaskEvent] = []
                if step_type == AgentRunStepType.VERIFY_RESULT:
                    event_rows = conn.execute(
                        "SELECT event_id,task_id,workspace_id,event_type,actor,created_at,payload "
                        "FROM agent_task_events WHERE task_id=%s AND workspace_id=%s "
                        "AND event_type IN (%s,%s,%s) ORDER BY created_at,event_id FOR SHARE",
                        (
                            task.id,
                            task.workspace_id,
                            AgentTaskEventType.PR_CREATED.value,
                            AgentTaskEventType.TESTS_PASSED.value,
                            AgentTaskEventType.TESTS_FAILED.value,
                        ),
                    ).fetchall()
                    for event_row in event_rows:
                        try:
                            task_event = AgentTaskEvent.model_validate(event_row[6])
                        except Exception as error:
                            raise AgentRunRepositoryIntegrityError(
                                "Stored task evidence event payload is invalid"
                            ) from error
                        if (
                            task_event.id != event_row[0]
                            or task_event.task_id != event_row[1]
                            or event_row[2] != task.workspace_id
                            or task_event.event_type.value != event_row[3]
                            or task_event.actor != event_row[4]
                            or task_event.created_at != event_row[5]
                        ):
                            raise AgentRunRepositoryIntegrityError(
                                "Stored task evidence event identity or scope is inconsistent"
                            )
                        task_events.append(task_event)

                updated_run, updated_steps, events, audit = build_records(
                    task, approval, task_events, durable_run, durable_steps
                )
                if step_type == AgentRunStepType.RUN_VALIDATION:
                    task_event = validation_task_event
                    expected_results = [
                        {
                            "command_index": claim["command_index"],
                            "command_hash": claim["command_hash"],
                            "exit_code": claim["exit_code"],
                            "timed_out": claim["timed_out"],
                            "stdout_sha256": claim["stdout_sha256"],
                            "stdout_bytes": claim["stdout_bytes"],
                            "stderr_sha256": claim["stderr_sha256"],
                            "stderr_bytes": claim["stderr_bytes"],
                        }
                        for claim in validation_claims
                    ]
                    metadata = task_event.metadata if task_event is not None else {}
                    expected_metadata = {
                        "run_id": durable_run.id,
                        "step_id": validation_step.id,
                        "intent_id": validation_intent_id,
                        "attempt_count": validation_step.attempt_count,
                        "approval_hash": validation_intent["approval_hash"],
                        "test_plan_hash": validation_intent["test_plan_hash"],
                        "lease_token_hash": validation_intent["lease_token_hash"],
                        "bundle_revision": bundle_revision,
                        "bundle_git_tree_sha": bundle_git_tree_sha,
                        "command_results": expected_results,
                    }
                    if (
                        task_event is None
                        or task_event.task_id != task.id
                        or task_event.event_type != (
                            AgentTaskEventType.TESTS_PASSED
                            if validation_passed else AgentTaskEventType.TESTS_FAILED
                        )
                        or task_event.status != (
                            AgentTaskStatus.APPROVED if validation_passed else AgentTaskStatus.FAILED
                        )
                        or task_event.actor != worker_id
                        or type(metadata.get("attempt_count")) is not int
                        or metadata != expected_metadata
                        or task_event.created_at > checked_at
                    ):
                        raise AgentRunRepositoryConflict(
                            "RUN_VALIDATION task event does not exactly bind durable command results"
                        )
                    if (
                        audit.details.get("validation_intent_id") != validation_intent_id
                        or audit.details.get("approval_hash") != validation_intent["approval_hash"]
                        or audit.details.get("test_plan_hash") != validation_intent["test_plan_hash"]
                        or audit.details.get("lease_token_hash") != validation_intent["lease_token_hash"]
                        or audit.details.get("bundle_revision") != bundle_revision
                        or audit.details.get("bundle_git_tree_sha") != bundle_git_tree_sha
                    ):
                        raise AgentRunRepositoryConflict(
                            "RUN_VALIDATION audit does not bind the durable validation authority"
                        )
                    result_step = next(
                        (item for item in updated_steps if item.id == validation_step.id), None
                    )
                    step_evidence = result_step.evidence if result_step is not None else {}
                    if (
                        step_evidence.get("bundle_revision") != bundle_revision
                        or step_evidence.get("bundle_git_tree_sha") != bundle_git_tree_sha
                    ):
                        raise AgentRunRepositoryConflict(
                            "RUN_VALIDATION step result does not bind the inspected bundle"
                        )
                    run_result_events = [item for item in events if item.step_id == validation_step.id]
                    if not run_result_events or any(
                        item.metadata.get("bundle_revision") != bundle_revision
                        or item.metadata.get("bundle_git_tree_sha") != bundle_git_tree_sha
                        for item in run_result_events
                    ):
                        raise AgentRunRepositoryConflict(
                            "RUN_VALIDATION AgentRun event does not bind the inspected bundle"
                        )
                if step_type == AgentRunStepType.CREATE_PULL_REQUEST:
                    result_step = next(
                        (item for item in updated_steps if item.id == pull_request_step.id), None
                    )
                    if (
                        result_step is None
                        or result_step.status != AgentRunStepStatus.SUCCEEDED
                        or result_step.evidence.get("pull_request_intent_id") != pull_request_intent.id
                        or result_step.evidence.get("repository") != pull_request_intent.repository
                        or result_step.evidence.get("head_branch") != pull_request_intent.head_branch
                        or result_step.evidence.get("base_branch") != pull_request_intent.base_branch
                        or result_step.evidence.get("pull_number") != pull_request_intent.pull_number
                        or result_step.evidence.get("pull_url") != pull_request_intent.pull_url
                        or result_step.evidence.get("head_sha") != pull_request_intent.observed_head_sha
                        or result_step.evidence.get("base_sha") != pull_request_intent.observed_base_sha
                        or result_step.evidence.get("title_sha256") != pull_request_intent.title_sha256
                        or result_step.evidence.get("body_sha256") != pull_request_intent.body_sha256
                        or result_step.evidence.get("intent_marker") != pull_request_intent.intent_marker
                        or result_step.evidence.get("response_sha256") != pull_request_intent.response_sha256
                        or pull_request_task_event.created_at > checked_at
                        or pull_request_task_audit.created_at > checked_at
                    ):
                        raise AgentRunRepositoryConflict(
                            "CREATE_PULL_REQUEST step result does not exactly bind verified intent"
                        )
                # Validate the generated completion using the existing shared
                # state/identity contract, represented as its in-transaction
                # RUNNING checkpoint. No intermediate checkpoint is persisted.
                already_running_validation = step_type in {
                    AgentRunStepType.RUN_VALIDATION,
                    AgentRunStepType.CREATE_PULL_REQUEST,
                }
                running_run = durable_run.model_copy(deep=True)
                running_steps = [item.model_copy(deep=True) for item in durable_steps]
                if not already_running_validation:
                    if running_run.status in {AgentRunStatus.SCHEDULED, AgentRunStatus.RETRY_WAIT}:
                        running_run.status = AgentRunStatus.RUNNING
                        running_run.started_at = running_run.started_at or checked_at
                    running_step = next(item for item in running_steps if item.id == current_step.id)
                    running_step.status = AgentRunStepStatus.RUNNING
                    running_step.lease_owner = worker_id
                    running_step.lease_token = lease_token
                    running_step.lease_expires_at = durable_run.lease_expires_at
                    running_step.attempt_count += 1
                completion_expected = running_run.model_copy(deep=True)
                completion_steps = [item.model_copy(deep=True) for item in running_steps]
                # The common validator checks step completion invariants. Its
                # timestamp must be the database clock sampled under the lock.
                _validate_step_completion(
                    expected_run=completion_expected,
                    updated_run=updated_run,
                    expected_steps=completion_steps,
                    updated_steps=updated_steps,
                    events=events,
                    audit=audit,
                    now=checked_at,
                )

                changed = conn.execute(
                    "UPDATE agent_runs SET status=%s,current_step_id=%s,lease_owner=%s,"
                    "lease_token=%s,lease_expires_at=%s,updated_at=%s,payload=%s "
                    "WHERE run_id=%s AND workspace_id=%s AND updated_at=%s "
                    "AND lease_owner=%s AND lease_token=%s AND lease_expires_at>clock_timestamp()",
                    (
                        updated_run.status.value,
                        updated_run.current_step_id,
                        updated_run.lease_owner,
                        updated_run.lease_token,
                        updated_run.lease_expires_at,
                        updated_run.updated_at,
                        Jsonb(updated_run.model_dump(mode="json")),
                        durable_run.id,
                        durable_run.workspace_id,
                        durable_run.updated_at,
                        worker_id,
                        lease_token,
                    ),
                )
                if changed.rowcount != 1:
                    raise AgentRunRepositoryConflict(
                        "AgentRun changed during governed-step completion"
                    )
                updated_by_id = {item.id: item for item in updated_steps}
                for durable_step in durable_steps:
                    updated_step = updated_by_id.get(durable_step.id)
                    if updated_step is None:
                        raise AgentRunRepositoryConflict(
                            "Scope validation changed the durable AgentRun step set"
                        )
                    changed = conn.execute(
                        "UPDATE agent_run_steps SET status=%s,updated_at=%s,payload=%s "
                        "WHERE step_id=%s AND run_id=%s AND workspace_id=%s AND updated_at=%s",
                        (
                            updated_step.status.value,
                            updated_step.updated_at,
                            Jsonb(updated_step.model_dump(mode="json")),
                            durable_step.id,
                            durable_run.id,
                            durable_run.workspace_id,
                            durable_step.updated_at,
                        ),
                    )
                    if changed.rowcount != 1:
                        raise AgentRunRepositoryConflict(
                            "AgentRun step changed during governed-step completion"
                        )
                for event in events:
                    conn.execute(
                        "INSERT INTO agent_run_events(event_id,run_id,task_id,workspace_id,"
                        "created_at,event_type,payload) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                        (
                            event.id,
                            event.run_id,
                            event.task_id,
                            durable_run.workspace_id,
                            event.created_at,
                            event.event_type.value,
                            Jsonb(event.model_dump(mode="json")),
                        ),
                    )
                if step_type == AgentRunStepType.CREATE_PULL_REQUEST:
                    completed_intent = pull_request_intent.model_copy(update={
                        "status": PullRequestIntentStatus.COMPLETED,
                        "reason": "Verified remote pull request, AgentRun completion, task event, and audits committed atomically.",
                        "updated_at": checked_at,
                    })
                    changed = conn.execute(
                        "UPDATE agent_run_pull_request_intents SET status='COMPLETED',reason=%s,updated_at=%s,payload=%s "
                        "WHERE intent_id=%s AND workspace_id=%s AND status='REMOTE_VERIFIED' "
                        "AND run_revision=%s AND step_revision=%s AND lease_token_hash=%s",
                        (
                            completed_intent.reason, checked_at,
                            Jsonb(completed_intent.model_dump(mode="json")),
                            pull_request_intent.id, durable_run.workspace_id,
                            pull_request_intent.run_revision, pull_request_intent.step_revision,
                            pull_request_intent.lease_token_hash,
                        ),
                    )
                    if changed.rowcount != 1:
                        raise AgentRunRepositoryConflict("Pull request intent completion lost its CAS")
                    conn.execute(
                        "INSERT INTO agent_task_events(event_id,task_id,workspace_id,event_type,actor,created_at,payload) "
                        "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                        (
                            pull_request_task_event.id, pull_request_task_event.task_id,
                            task.workspace_id, pull_request_task_event.event_type.value,
                            pull_request_task_event.actor, pull_request_task_event.created_at,
                            Jsonb(pull_request_task_event.model_dump(mode="json")),
                        ),
                    )
                    updated_task = task.model_copy(deep=True)
                    updated_task.status = AgentTaskStatus.READY_FOR_PR
                    updated_task.worker_lease_owner = None
                    updated_task.worker_lease_token = None
                    updated_task.worker_lease_expires_at = None
                    updated_task.updated_at = checked_at
                    changed = conn.execute(
                        "UPDATE agent_tasks SET status=%s,updated_at=%s,payload=%s "
                        "WHERE task_id=%s AND workspace_id=%s AND status=%s AND updated_at=%s",
                        (
                            updated_task.status.value, updated_task.updated_at,
                            Jsonb(updated_task.model_dump(mode="json")), task.id,
                            task.workspace_id, task.status.value, task_row[5],
                        ),
                    )
                    if changed.rowcount != 1:
                        raise AgentRunRepositoryConflict(
                            "AgentTask changed during pull request completion"
                        )
                    updated_task_audit = pull_request_task_audit.model_copy(
                        update={"created_at": checked_at}
                    )
                    conn.execute(
                        "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,"
                        "event_type,actor,details,created_at,payload) "
                        "VALUES(%s,%s,NULL,'agent_task',%s,%s,%s,%s,%s,%s)",
                        (
                            updated_task_audit.id, task.workspace_id, task.id,
                            updated_task_audit.event_type, updated_task_audit.actor,
                            Jsonb(updated_task_audit.details), checked_at,
                            Jsonb(updated_task_audit.model_dump(mode="json")),
                        ),
                    )
                if step_type == AgentRunStepType.RUN_VALIDATION:
                    completed_intent = dict(validation_intent)
                    intent_reason = (
                        "All approved validation commands completed successfully."
                        if validation_passed
                        else "Approved validation command failed or timed out; later commands were not executed."
                    )
                    completed_intent.update(
                        status="COMPLETED",
                        reason=intent_reason,
                        updated_at=checked_at.isoformat(),
                    )
                    changed = conn.execute(
                        "UPDATE agent_run_validation_intents SET status='COMPLETED',reason=%s,updated_at=%s,payload=%s "
                        "WHERE intent_id=%s AND workspace_id=%s AND status=%s AND run_revision=%s "
                        "AND step_revision=%s AND lease_token_hash=%s",
                        (
                            completed_intent["reason"], checked_at, Jsonb(completed_intent),
                            validation_intent_id, durable_run.workspace_id,
                            validation_intent["status"], validation_intent["run_revision"],
                            validation_intent["step_revision"], validation_intent["lease_token_hash"],
                        ),
                    )
                    if changed.rowcount != 1:
                        raise AgentRunRepositoryConflict("Validation intent completion lost its CAS")
                    conn.execute(
                        "INSERT INTO agent_task_events(event_id,task_id,workspace_id,event_type,actor,created_at,payload) "
                        "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                        (
                            task_event.id, task_event.task_id, task.workspace_id, task_event.event_type.value,
                            task_event.actor, task_event.created_at, Jsonb(task_event.model_dump(mode="json")),
                        ),
                    )
                    validation_audit = AuditEvent(
                        event_type="agent_worker.validation_run_completed",
                        entity_id=task.id,
                        workspace_id=task.workspace_id,
                        actor=worker_id,
                        created_at=checked_at,
                        details={
                            "event_id": task_event.id,
                            "worker_id": worker_id,
                            "approval_request_id": approval.id,
                            "passed": validation_passed,
                            "command_count": len(validation_claims),
                            "validation_intent_id": validation_intent_id,
                            "approval_hash": validation_intent["approval_hash"],
                            "test_plan_hash": validation_intent["test_plan_hash"],
                            "lease_token_hash": validation_intent["lease_token_hash"],
                            "bundle_revision": bundle_revision,
                            "bundle_git_tree_sha": bundle_git_tree_sha,
                        },
                    )
                    updated_task = task.model_copy(deep=True)
                    if not validation_passed:
                        updated_task.status = AgentTaskStatus.FAILED
                    if task.worker_lease_owner is not None:
                        updated_task.worker_lease_owner = None
                        updated_task.worker_lease_token = None
                        updated_task.worker_lease_expires_at = None
                    if updated_task != task:
                        updated_task.updated_at = checked_at
                        changed = conn.execute(
                            "UPDATE agent_tasks SET status=%s,updated_at=%s,payload=%s "
                            "WHERE task_id=%s AND workspace_id=%s AND status=%s AND updated_at=%s",
                            (
                                updated_task.status.value,
                                updated_task.updated_at,
                                Jsonb(updated_task.model_dump(mode="json")),
                                task.id,
                                task.workspace_id,
                                task.status.value,
                                task_row[5],
                            ),
                        )
                        if changed.rowcount != 1:
                            raise AgentRunRepositoryConflict(
                                "AgentTask changed during validation completion"
                            )
                    conn.execute(
                        "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,"
                        "event_type,actor,details,created_at,payload) "
                        "VALUES(%s,%s,NULL,'agent_task',%s,%s,%s,%s,%s,%s)",
                        (
                            validation_audit.id,
                            task.workspace_id,
                            task.id,
                            validation_audit.event_type,
                            validation_audit.actor,
                            Jsonb(validation_audit.details),
                            validation_audit.created_at,
                            Jsonb(validation_audit.model_dump(mode="json")),
                        ),
                    )
                conn.execute(
                    "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,"
                    "entity_id,event_type,actor,details,created_at,payload) "
                    "VALUES(%s,%s,NULL,'agent_run',%s,%s,%s,%s,%s,%s)",
                    (
                        audit.id,
                        durable_run.workspace_id,
                        durable_run.id,
                        audit.event_type,
                        audit.actor,
                        Jsonb(audit.details),
                        audit.created_at,
                        Jsonb(audit.model_dump(mode="json")),
                    ),
                )
                return updated_run, updated_steps, events, audit

    def commit_evidence_seal(
        self,
        *,
        expected_run: AgentRun,
        expected_steps: list[AgentRunStep],
        worker_id: str,
        lease_token: str,
        build_records: Callable[
            [AgentRun, list[AgentRunStep], list[AgentRunEvent]],
            tuple[AgentRun, list[AgentRunStep], list[AgentRunEvent], AuditEvent],
        ],
    ) -> tuple[AgentRun, list[AgentRunStep], list[AgentRunEvent], AuditEvent]:
        """Build and commit SEAL_EVIDENCE from one locked durable snapshot.

        The run row lock prevents new event rows from passing their FK check
        until sealing commits. Existing event rows are locked against updates
        and deletes. The callback is pure: it receives only validated durable
        models and performs no I/O.
        """
        if (
            not expected_run.workspace_id
            or not expected_run.lease_token
            or expected_run.lease_owner != worker_id
            or expected_run.lease_token != lease_token
        ):
            raise AgentRunRepositoryConflict("Evidence seal lease identity is invalid")

        with self._connect() as conn:
            with conn.transaction():
                durable_run, durable_steps = self.get_control_snapshot_in_transaction(
                    conn,
                    run_id=expected_run.id,
                    workspace_id=expected_run.workspace_id,
                )
                if durable_run != expected_run:
                    raise AgentRunRepositoryConflict(
                        "AgentRun revision changed before evidence sealing"
                    )
                if {item.id: item for item in durable_steps} != {
                    item.id: item for item in expected_steps
                }:
                    raise AgentRunRepositoryConflict(
                        "AgentRun step revisions changed before evidence sealing"
                    )
                checked_at = conn.execute("SELECT clock_timestamp()").fetchone()[0]
                if (
                    durable_run.lease_owner != worker_id
                    or durable_run.lease_token != lease_token
                    or durable_run.lease_expires_at is None
                    or durable_run.lease_expires_at <= checked_at
                ):
                    raise AgentRunRepositoryConflict(
                        "AgentRun lease is stale before evidence sealing"
                    )
                if (
                    [item.step_type for item in durable_steps]
                    != list(DEFAULT_AGENT_RUN_STEPS)
                    or [item.sequence for item in durable_steps]
                    != list(range(len(DEFAULT_AGENT_RUN_STEPS)))
                    or not durable_steps
                    or durable_run.current_step_id != durable_steps[-1].id
                    or durable_steps[-1].step_type != AgentRunStepType.SEAL_EVIDENCE
                    or durable_steps[-1].status != AgentRunStepStatus.RUNNING
                    or any(
                        item.status != AgentRunStepStatus.SUCCEEDED
                        for item in durable_steps[:-1]
                    )
                ):
                    raise AgentRunRepositoryConflict(
                        "SEAL_EVIDENCE requires the canonical plan and all prior steps succeeded"
                    )
                if durable_run.status != AgentRunStatus.RUNNING:
                    raise AgentRunRepositoryConflict(
                        "SEAL_EVIDENCE requires a running AgentRun"
                    )

                event_rows = conn.execute(
                    "SELECT event_id,run_id,task_id,workspace_id,created_at,event_type,payload "
                    "FROM agent_run_events WHERE run_id=%s AND workspace_id=%s "
                    "ORDER BY created_at,event_id FOR SHARE",
                    (durable_run.id, durable_run.workspace_id),
                ).fetchall()
                events_snapshot: list[AgentRunEvent] = []
                for row in event_rows:
                    try:
                        event = AgentRunEvent.model_validate(row[6])
                    except Exception as error:
                        raise AgentRunRepositoryIntegrityError(
                            "Stored AgentRun evidence event payload is invalid"
                        ) from error
                    if (
                        event.id != row[0]
                        or event.run_id != row[1]
                        or event.task_id != row[2]
                        or row[3] != durable_run.workspace_id
                        or event.created_at != row[4]
                        or event.event_type.value != row[5]
                        or event.correlation_id != durable_run.correlation_id
                        or (event.step_id is not None and event.step_id not in {
                            item.id for item in durable_steps
                        })
                    ):
                        raise AgentRunRepositoryIntegrityError(
                            "Stored AgentRun evidence event identity or scope is inconsistent"
                        )
                    events_snapshot.append(event)

                updated_run, updated_steps, events, audit = build_records(
                    durable_run, durable_steps, events_snapshot
                )
                running_run = durable_run.model_copy(deep=True)
                running_steps = [item.model_copy(deep=True) for item in durable_steps]
                running_step = running_steps[-1]
                running_step.lease_owner = worker_id
                running_step.lease_token = lease_token
                running_step.lease_expires_at = durable_run.lease_expires_at
                _validate_step_completion(
                    expected_run=running_run,
                    updated_run=updated_run,
                    expected_steps=running_steps,
                    updated_steps=updated_steps,
                    events=events,
                    audit=audit,
                    now=checked_at,
                )
                if (
                    not updated_run.final_evidence_hash
                    or updated_run.final_evidence_hash
                    != updated_steps[-1].evidence.get("final_evidence_hash")
                ):
                    raise AgentRunRepositoryIntegrityError(
                        "SEAL_EVIDENCE completion does not bind the final manifest"
                    )

                changed = conn.execute(
                    "UPDATE agent_runs SET status=%s,current_step_id=%s,lease_owner=NULL,"
                    "lease_token=NULL,lease_expires_at=NULL,updated_at=%s,payload=%s "
                    "WHERE run_id=%s AND workspace_id=%s AND updated_at=%s "
                    "AND lease_owner=%s AND lease_token=%s "
                    "AND lease_expires_at>clock_timestamp()",
                    (
                        updated_run.status.value,
                        updated_run.current_step_id,
                        updated_run.updated_at,
                        Jsonb(updated_run.model_dump(mode="json")),
                        durable_run.id,
                        durable_run.workspace_id,
                        durable_run.updated_at,
                        worker_id,
                        lease_token,
                    ),
                )
                if changed.rowcount != 1:
                    raise AgentRunRepositoryConflict(
                        "AgentRun changed during evidence sealing"
                    )
                updated_by_id = {item.id: item for item in updated_steps}
                for previous in durable_steps:
                    updated = updated_by_id.get(previous.id)
                    if updated is None:
                        raise AgentRunRepositoryConflict(
                            "SEAL_EVIDENCE changed the durable step set"
                        )
                    changed = conn.execute(
                        "UPDATE agent_run_steps SET status=%s,updated_at=%s,payload=%s "
                        "WHERE step_id=%s AND run_id=%s AND workspace_id=%s "
                        "AND updated_at=%s",
                        (
                            updated.status.value,
                            updated.updated_at,
                            Jsonb(updated.model_dump(mode="json")),
                            previous.id,
                            durable_run.id,
                            durable_run.workspace_id,
                            previous.updated_at,
                        ),
                    )
                    if changed.rowcount != 1:
                        raise AgentRunRepositoryConflict(
                            "AgentRun step changed during evidence sealing"
                        )
                for event in events:
                    conn.execute(
                        "INSERT INTO agent_run_events(event_id,run_id,task_id,workspace_id,"
                        "created_at,event_type,payload) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                        (
                            event.id,
                            event.run_id,
                            event.task_id,
                            durable_run.workspace_id,
                            event.created_at,
                            event.event_type.value,
                            Jsonb(event.model_dump(mode="json")),
                        ),
                    )
                conn.execute(
                    "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,"
                    "entity_id,event_type,actor,details,created_at,payload) "
                    "VALUES(%s,%s,NULL,'agent_run',%s,%s,%s,%s,%s,%s)",
                    (
                        audit.id,
                        durable_run.workspace_id,
                        durable_run.id,
                        audit.event_type,
                        audit.actor,
                        Jsonb(audit.details),
                        audit.created_at,
                        Jsonb(audit.model_dump(mode="json")),
                    ),
                )
                return updated_run, updated_steps, events, audit

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
                self.create_in_transaction(
                    conn,
                    run=run,
                    steps=steps,
                    events=events,
                    audit=audit,
                    scheduled_audit=scheduled_audit,
                )
        except psycopg.errors.UniqueViolation as error:
            raise AgentRunRepositoryConflict("AgentRun creation conflicts with existing durable state") from error

    def create_in_transaction(
        self,
        conn: Connection,
        *,
        run: AgentRun,
        steps: list[AgentRunStep],
        events: list[AgentRunEvent],
        audit: AuditEvent,
        scheduled_audit: AuditEvent | None = None,
    ) -> None:
        """Create a scheduled run and its evidence inside a caller-owned transaction."""
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
