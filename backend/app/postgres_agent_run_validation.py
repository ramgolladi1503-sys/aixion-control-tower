from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import re
from typing import Any, Callable

from pydantic import BaseModel
from psycopg import Connection
from psycopg.types.json import Jsonb

from .agent_run_models import (
    AgentRun, AgentRunEvent, AgentRunEventType, AgentRunStatus, AgentRunStep,
    AgentRunStepStatus, AgentRunStepType,
)
from .agent_task_models import AgentTask, AgentTaskStatus
from .approval_integrity import compute_approval_payload_hash
from .models import ApprovalRequest, ApprovalStatus, AuditEvent
from .postgres_trust_event_repository import PostgresTrustEventRepository
from .postgres_trust_repository import PostgresTrustRepository, PostgresTrustRepositoryConflict
from .trust_crypto import sha256_hex, verify_payload_signature
from .trust_models import (
    ActionConsumption, CapabilityActionType, CapabilityLeaseStatus,
    PolicyDecisionType, ProposedAction,
)
from .trust_policy import lease_receipt_payload
from .agent_worker_validation_plan import validate_validation_command, validate_validation_plan


class RunValidationIntentStatus(StrEnum):
    INTENT = "INTENT"
    EXECUTION_CLAIMED = "EXECUTION_CLAIMED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    COMPLETED = "COMPLETED"


class RunValidationCommandStatus(StrEnum):
    CLAIMED = "CLAIMED"
    RESULT_RECORDED = "RESULT_RECORDED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class AgentRunValidationIntent(BaseModel):
    id: str
    workspace_id: str
    run_id: str
    task_id: str
    approval_id: str
    step_id: str
    lease_id: str
    idempotency_key: str
    approval_hash: str
    test_plan_hash: str
    lease_token_hash: str
    bundle_revision: str
    bundle_git_tree_sha: str
    run_revision: datetime
    step_revision: datetime
    status: RunValidationIntentStatus = RunValidationIntentStatus.INTENT
    reason: str = ""
    created_at: datetime
    updated_at: datetime


class AgentRunValidationCommandClaim(BaseModel):
    intent_id: str
    workspace_id: str
    action_id: str
    command_index: int
    command_hash: str
    status: RunValidationCommandStatus
    exit_code: int | None = None
    timed_out: bool | None = None
    stdout_sha256: str | None = None
    stdout_bytes: int | None = None
    stderr_sha256: str | None = None
    stderr_bytes: int | None = None
    reason: str = ""
    claimed_at: datetime
    updated_at: datetime


_PARENT_COLUMNS = (
    "intent_id,workspace_id,run_id,task_id,approval_id,step_id,lease_id,idempotency_key,"
    "approval_hash,test_plan_hash,lease_token_hash,bundle_revision,bundle_git_tree_sha,"
    "run_revision,step_revision,status,reason,"
    "created_at,updated_at,payload"
)
_COMMAND_COLUMNS = (
    "intent_id,workspace_id,action_id,command_index,command_hash,status,exit_code,timed_out,"
    "stdout_sha256,stdout_bytes,stderr_sha256,stderr_bytes,reason,claimed_at,updated_at,payload"
)
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_APPROVAL_DIGEST = re.compile(r"^sha256:(?:v2:)?[a-f0-9]{64}$")
_GIT_SHA = re.compile(r"^[a-f0-9]{40}$")
_OUTPUT_LIMIT = 1024 * 1024


def _parent_payload(record: AgentRunValidationIntent) -> dict[str, Any]:
    return record.model_dump(mode="json")


def _command_payload(record: AgentRunValidationCommandClaim) -> dict[str, Any]:
    return record.model_dump(mode="json")


def _validation_action_consumption(
    *,
    intent: AgentRunValidationIntent,
    action: ProposedAction,
    command_index: int,
    command_hash: str,
    runtime_seconds: int,
    attempt_count: int,
) -> ActionConsumption:
    if (
        type(command_index) is not int or command_index < 0
        or not _DIGEST.fullmatch(command_hash)
        or type(runtime_seconds) is not int or not 1 <= runtime_seconds <= 3600
        or type(attempt_count) is not int or attempt_count < 1
        or action.id == "" or action.id is None
    ):
        raise ValueError("Validation action consumption evidence is invalid")
    retry_consumed = attempt_count > 1
    return ActionConsumption(
        actual_runtime_seconds=runtime_seconds,
        retry_consumed=retry_consumed,
        evidence={
            "workspace_id": intent.workspace_id,
            "run_id": intent.run_id,
            "task_id": intent.task_id,
            "approval_id": intent.approval_id,
            "step_id": intent.step_id,
            "action_id": action.id,
            "lease_id": intent.lease_id,
            "intent_id": intent.id,
            "command_index": command_index,
            "command_sha256": command_hash,
            "runtime_seconds_reserved": runtime_seconds,
            "authorized_attempt_count": attempt_count,
            "authorized_retry_consumed": retry_consumed,
        },
    )


def _decode_parent(row: tuple[Any, ...]) -> AgentRunValidationIntent:
    record = AgentRunValidationIntent.model_validate(row[19])
    indexed = (
        record.id, record.workspace_id, record.run_id, record.task_id, record.approval_id,
        record.step_id, record.lease_id, record.idempotency_key, record.approval_hash,
        record.test_plan_hash, record.lease_token_hash, record.bundle_revision,
        record.bundle_git_tree_sha, record.run_revision,
        record.step_revision, record.status.value, record.reason, record.created_at,
        record.updated_at,
    )
    if indexed != tuple(row[:19]):
        raise ValueError("Stored validation intent differs from indexed state")
    if (
        not _APPROVAL_DIGEST.fullmatch(record.approval_hash)
        or not _DIGEST.fullmatch(record.test_plan_hash)
        or not _DIGEST.fullmatch(record.lease_token_hash)
        or not _GIT_SHA.fullmatch(record.bundle_revision)
        or not _GIT_SHA.fullmatch(record.bundle_git_tree_sha)
    ):
        raise ValueError("Stored validation intent contains an invalid authority digest")
    return record


def _decode_command(row: tuple[Any, ...]) -> AgentRunValidationCommandClaim:
    record = AgentRunValidationCommandClaim.model_validate(row[15])
    indexed = (
        record.intent_id, record.workspace_id, record.action_id, record.command_index,
        record.command_hash, record.status.value, record.exit_code, record.timed_out,
        record.stdout_sha256, record.stdout_bytes, record.stderr_sha256, record.stderr_bytes,
        record.reason, record.claimed_at, record.updated_at,
    )
    if indexed != tuple(row[:15]):
        raise ValueError("Stored validation command claim differs from indexed state")
    if record.command_index < 0 or not _DIGEST.fullmatch(record.command_hash):
        raise ValueError("Stored validation command claim has invalid identity")
    if record.status == RunValidationCommandStatus.RESULT_RECORDED:
        if (
            type(record.exit_code) is not int or not -255 <= record.exit_code <= 255
            or type(record.timed_out) is not bool
            or any(not isinstance(value, str) or not _DIGEST.fullmatch(value)
                   for value in (record.stdout_sha256, record.stderr_sha256))
            or any(type(value) is not int or not 0 <= value <= _OUTPUT_LIMIT
                   for value in (record.stdout_bytes, record.stderr_bytes))
        ):
            raise ValueError("Stored validation command result is invalid")
    elif any(value is not None for value in (
        record.exit_code, record.timed_out, record.stdout_sha256, record.stdout_bytes,
        record.stderr_sha256, record.stderr_bytes,
    )) and record.status == RunValidationCommandStatus.CLAIMED:
        raise ValueError("Unfinished validation command claim contains a result")
    return record


class PostgresAgentRunValidationIntentRepository:
    """Durable one-shot authority and result storage for approved test-plan commands."""

    def __init__(
        self,
        connect: Callable[[], Connection],
        *,
        trust_repository: PostgresTrustRepository | None = None,
        trust_event_repository: PostgresTrustEventRepository | None = None,
    ) -> None:
        self._connect = connect
        self._trust = trust_repository or PostgresTrustRepository()
        self._trust_events = trust_event_repository or PostgresTrustEventRepository(connect)

    def _lock_validation_authority(
        self,
        conn: Connection,
        *,
        workspace_id: str,
        run_id: str,
        task_id: str,
        approval_id: str,
        step_id: str,
        require_running: bool = True,
    ) -> tuple[ApprovalRequest, AgentTask, AgentRun, AgentRunStep]:
        """Lock approval -> task -> run -> step and validate indexed payload authority."""
        approval_row = conn.execute(
            "SELECT approval_id,project_id,workspace_id,status,payload "
            "FROM approval_requests WHERE approval_id=%s AND workspace_id=%s FOR SHARE",
            (approval_id, workspace_id),
        ).fetchone()
        task_row = conn.execute(
            "SELECT task_id,project_id,workspace_id,approval_request_id,status,payload "
            "FROM agent_tasks WHERE task_id=%s AND workspace_id=%s FOR SHARE",
            (task_id, workspace_id),
        ).fetchone()
        run_row = conn.execute(
            "SELECT run_id,workspace_id,task_id,project_id,status,current_step_id,updated_at,payload "
            "FROM agent_runs WHERE run_id=%s AND workspace_id=%s FOR UPDATE",
            (run_id, workspace_id),
        ).fetchone()
        step_row = conn.execute(
            "SELECT step_id,run_id,task_id,workspace_id,status,updated_at,payload "
            "FROM agent_run_steps WHERE step_id=%s AND workspace_id=%s FOR UPDATE",
            (step_id, workspace_id),
        ).fetchone()
        if any(row is None for row in (approval_row, task_row, run_row, step_row)):
            raise PostgresTrustRepositoryConflict("Validation authority is not visible in this workspace")
        try:
            approval = ApprovalRequest.model_validate(approval_row[4])
            task = AgentTask.model_validate(task_row[5])
            run = AgentRun.model_validate(run_row[7])
            step = AgentRunStep.model_validate(step_row[6])
        except Exception as error:
            raise PostgresTrustRepositoryConflict("Stored validation authority payload is invalid") from error
        if (
            approval.id != approval_row[0] or approval.project_id != approval_row[1]
            or approval.workspace_id != approval_row[2] or approval.status.value != approval_row[3]
            or task.id != task_row[0] or task.project_id != task_row[1]
            or task.workspace_id != task_row[2] or task.approval_request_id != task_row[3]
            or task.status.value != task_row[4]
            or run.id != run_row[0] or run.workspace_id != run_row[1]
            or run.task_id != run_row[2] or run.project_id != run_row[3]
            or run.status.value != run_row[4] or run.current_step_id != run_row[5]
            or run.updated_at != run_row[6]
            or step.id != step_row[0] or step.run_id != step_row[1]
            or step.task_id != step_row[2] or step.status.value != step_row[4]
            or step.updated_at != step_row[5] or step.run_id != run.id
            or step.task_id != task.id or task.id != run.task_id
            or task.project_id != approval.project_id
            or task.approval_request_id != approval.id
            or run.approval_request_id != approval.id
            or approval.workspace_id != workspace_id
            or task.workspace_id != workspace_id or run.workspace_id != workspace_id
            or step_row[3] != workspace_id
            or (require_running and approval.status != ApprovalStatus.APPROVED)
            or (require_running and task.status != AgentTaskStatus.APPROVED)
            or (require_running and run.status != AgentRunStatus.RUNNING)
            or (require_running and run.current_step_id != step.id)
            or (require_running and step.step_type != AgentRunStepType.RUN_VALIDATION)
            or (require_running and step.status != AgentRunStepStatus.RUNNING)
            or not run.capability_lease_id or not run.repository
            or (require_running and approval.approved_payload_hash != compute_approval_payload_hash(approval))
            or (require_running and validate_validation_plan(approval) is not None)
        ):
            raise PostgresTrustRepositoryConflict("Durable validation authority is inconsistent or no longer approved")
        return approval, task, run, step

    def get_for_step(
        self, *, workspace_id: str, run_id: str, step_id: str, idempotency_key: str,
    ) -> AgentRunValidationIntent | None:
        if not workspace_id:
            raise ValueError("Validation intent reads require workspace scope")
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_PARENT_COLUMNS} FROM agent_run_validation_intents "
                "WHERE workspace_id=%s AND run_id=%s AND step_id=%s AND idempotency_key=%s",
                (workspace_id, run_id, step_id, idempotency_key),
            ).fetchone()
        return _decode_parent(row) if row else None

    def list_command_claims(
        self, *, intent_id: str, workspace_id: str,
    ) -> list[AgentRunValidationCommandClaim]:
        if not intent_id or not workspace_id:
            raise ValueError("Validation claim reads require intent and workspace scope")
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_COMMAND_COLUMNS} FROM agent_run_validation_command_claims "
                "WHERE intent_id=%s AND workspace_id=%s ORDER BY command_index",
                (intent_id, workspace_id),
            ).fetchall()
        return [_decode_command(row) for row in rows]

    def record_intent(
        self,
        *,
        record: AgentRunValidationIntent,
        expected_run: AgentRun,
        expected_step: AgentRunStep,
        approval: ApprovalRequest,
        worker_id: str,
        lease_token: str,
        actor: str,
    ) -> AgentRunValidationIntent:
        """Persist the immutable exact approval/plan binding before command claims."""
        if (
            record.status != RunValidationIntentStatus.INTENT
            or expected_run.status.value != "RUNNING"
            or expected_step.status != AgentRunStepStatus.RUNNING
            or expected_step.step_type != AgentRunStepType.RUN_VALIDATION
            or approval.status.value != "APPROVED"
            or not expected_run.workspace_id or not expected_run.lease_token
            or expected_run.lease_owner != worker_id or expected_run.lease_token != lease_token
            or expected_step.lease_owner != worker_id or expected_step.lease_token != lease_token
            or expected_run.approval_request_id != approval.id
            or expected_step.run_id != expected_run.id
            or expected_step.task_id != expected_run.task_id
            or not approval.test_plan or validate_validation_plan(approval) is not None
            or record.workspace_id != expected_run.workspace_id
            or record.run_id != expected_run.id or record.task_id != expected_run.task_id
            or record.approval_id != approval.id or record.step_id != expected_step.id
            or record.lease_id != expected_run.capability_lease_id
            or not record.lease_id
            or record.idempotency_key != expected_step.idempotency_key
            or approval.workspace_id != record.workspace_id
            or record.approval_hash != compute_approval_payload_hash(approval)
            or record.approval_hash != approval.approved_payload_hash
            or record.test_plan_hash != sha256_hex(approval.test_plan)
            or record.lease_token_hash != sha256_hex({"lease_token": lease_token})
            or not _GIT_SHA.fullmatch(record.bundle_revision)
            or not _GIT_SHA.fullmatch(record.bundle_git_tree_sha)
            or record.run_revision != expected_run.updated_at
            or record.step_revision != expected_step.updated_at
            or not actor
        ):
            raise PostgresTrustRepositoryConflict("Validation intent does not bind the running approved step")
        with self._connect() as conn:
            with conn.transaction():
                approval_db, task_db, run_db, step_db = self._lock_validation_authority(
                    conn, workspace_id=record.workspace_id, run_id=record.run_id,
                    task_id=record.task_id, approval_id=record.approval_id,
                    step_id=record.step_id,
                )
                checked_at = conn.execute("SELECT clock_timestamp()").fetchone()[0]
                lease = self._trust.get_capability_lease(
                    conn, record.lease_id, record.workspace_id, for_share=True,
                )
                if (
                    approval_db != approval or task_db.id != expected_run.task_id
                    or run_db != expected_run or step_db != expected_step
                    or approval_db.approved_payload_hash != record.approval_hash
                    or run_db.lease_owner != worker_id or run_db.lease_token != lease_token
                    or step_db.lease_owner != worker_id or step_db.lease_token != lease_token
                    or run_db.lease_expires_at is None or run_db.lease_expires_at <= checked_at
                    or step_db.lease_expires_at is None or step_db.lease_expires_at <= checked_at
                    or lease is None or lease.id != record.lease_id
                    or lease.workspace_id != record.workspace_id
                    or lease.approval_request_id != approval_db.id
                    or lease.approved_payload_hash != record.approval_hash
                    or lease.status != CapabilityLeaseStatus.ACTIVE
                    or lease.expires_at <= checked_at
                    or lease.provider != task_db.provider
                    or lease.agent_id != task_db.external_agent_id
                    or lease.scope.repository != run_db.repository
                    or CapabilityActionType.RUN_COMMAND not in lease.scope.allowed_actions
                    or any(command not in lease.scope.allowed_commands for command in approval_db.test_plan)
                    or not verify_payload_signature(lease_receipt_payload(lease), lease.receipt_signature)
                ):
                    raise PostgresTrustRepositoryConflict(
                        "Durable validation approval, task, run, step, or lease changed before intent persistence"
                    )
                existing = conn.execute(
                    f"SELECT {_PARENT_COLUMNS} FROM agent_run_validation_intents "
                    "WHERE workspace_id=%s AND run_id=%s AND step_id=%s AND idempotency_key=%s FOR UPDATE",
                    (record.workspace_id, record.run_id, record.step_id, record.idempotency_key),
                ).fetchone()
                if existing:
                    saved = _decode_parent(existing)
                    retry_record = record.model_copy(update={
                        "created_at": saved.created_at,
                        "updated_at": saved.updated_at,
                    })
                    if saved != retry_record:
                        raise PostgresTrustRepositoryConflict("Validation intent identity is already bound")
                    return saved
                created_at = conn.execute("SELECT clock_timestamp()").fetchone()[0]
                persisted = record.model_copy(update={
                    "created_at": created_at,
                    "updated_at": created_at,
                })
                conn.execute(
                    f"INSERT INTO agent_run_validation_intents({_PARENT_COLUMNS.replace(',payload', '')},payload) "
                    "VALUES(" + ",".join(["%s"] * 19) + ",%s)",
                    (
                        record.id, record.workspace_id, record.run_id, record.task_id,
                        record.approval_id, record.step_id, record.lease_id,
                        record.idempotency_key, record.approval_hash, record.test_plan_hash,
                        record.lease_token_hash, record.bundle_revision,
                        record.bundle_git_tree_sha, record.run_revision, record.step_revision,
                        persisted.status.value, persisted.reason,
                        persisted.created_at, persisted.updated_at,
                        Jsonb(_parent_payload(persisted)),
                    ),
                )
                event = AgentRunEvent(
                    run_id=record.run_id, task_id=record.task_id, step_id=record.step_id,
                    correlation_id=expected_run.correlation_id,
                    event_type=AgentRunEventType.OPERATOR_ACTION,
                    actor=actor,
                    attempt_number=expected_step.attempt_count,
                    message="Durable approved validation plan intent recorded.",
                    metadata={"validation_intent_id": record.id,
                              "approval_hash": record.approval_hash,
                              "test_plan_hash": record.test_plan_hash,
                              "lease_token_hash": record.lease_token_hash,
                              "bundle_revision": record.bundle_revision,
                              "bundle_git_tree_sha": record.bundle_git_tree_sha},
                    created_at=created_at,
                )
                conn.execute(
                    "INSERT INTO agent_run_events(event_id,run_id,task_id,workspace_id,created_at,event_type,payload) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    (event.id, event.run_id, event.task_id, record.workspace_id,
                     event.created_at, event.event_type.value,
                     Jsonb(event.model_dump(mode="json"))),
                )
                audit = AuditEvent(
                    event_type="agent_run.validation_intent_recorded",
                    entity_id=record.run_id, workspace_id=record.workspace_id,
                    actor=actor,
                    details={"validation_intent_id": record.id,
                             "approval_hash": record.approval_hash,
                             "test_plan_hash": record.test_plan_hash,
                             "lease_token_hash": record.lease_token_hash,
                             "bundle_revision": record.bundle_revision,
                             "bundle_git_tree_sha": record.bundle_git_tree_sha},
                    created_at=created_at,
                )
                conn.execute(
                    "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,event_type,actor,details,created_at,payload) "
                    "VALUES(%s,%s,%s,'agent_run',%s,%s,%s,%s,%s,%s)",
                    (audit.id, record.workspace_id, None, record.run_id,
                     audit.event_type, actor, Jsonb(audit.details), audit.created_at,
                     Jsonb(audit.model_dump(mode="json"))),
                )
        return persisted

    def claim_next_command(
        self,
        *,
        intent: AgentRunValidationIntent,
        expected_run: AgentRun,
        expected_step: AgentRunStep,
        approval: ApprovalRequest,
        action: ProposedAction,
        worker_id: str,
        lease_token: str,
        actor: str,
        timeout_seconds: int,
    ) -> AgentRunValidationCommandClaim:
        """Atomically authorize and consume exactly the next approved command."""
        if (
            type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3600
            or action.estimated_runtime_seconds != timeout_seconds
            or expected_run.workspace_id != intent.workspace_id
            or expected_run.status.value != "RUNNING"
            or expected_run.id != intent.run_id or expected_step.id != intent.step_id
            or expected_step.run_id != intent.run_id or expected_step.task_id != intent.task_id
            or expected_step.step_type != AgentRunStepType.RUN_VALIDATION
            or approval.id != intent.approval_id or not worker_id or not lease_token or not actor
            or approval.status.value != "APPROVED"
            or expected_run.approval_request_id != approval.id
            or approval.workspace_id != intent.workspace_id
        ):
            raise PostgresTrustRepositoryConflict("Validation command claim scope is incomplete")
        if validate_validation_plan(approval) is not None:
            raise PostgresTrustRepositoryConflict("Approved validation plan is not allowlisted")
        with self._connect() as conn:
            with conn.transaction():
                approval_db, task_db, run_db, step_db = self._lock_validation_authority(
                    conn, workspace_id=intent.workspace_id, run_id=intent.run_id,
                    task_id=intent.task_id, approval_id=intent.approval_id,
                    step_id=intent.step_id,
                )
                if (
                    approval_db != approval or task_db.id != intent.task_id
                    or run_db != expected_run or step_db != expected_step
                    or run_db.updated_at != intent.run_revision
                    or step_db.updated_at != intent.step_revision
                    or run_db.lease_owner != worker_id or run_db.lease_token != lease_token
                    or step_db.lease_owner != worker_id or step_db.lease_token != lease_token
                ):
                    raise PostgresTrustRepositoryConflict(
                        "RUN_VALIDATION durable authority changed before command claim"
                    )
                parent_row = conn.execute(
                    f"SELECT {_PARENT_COLUMNS} FROM agent_run_validation_intents "
                    "WHERE intent_id=%s AND workspace_id=%s FOR UPDATE",
                    (intent.id, intent.workspace_id),
                ).fetchone()
                if parent_row is None:
                    raise PostgresTrustRepositoryConflict("Validation intent is not visible in this workspace")
                saved = _decode_parent(parent_row)
                immutable_identity = (
                    saved.id, saved.workspace_id, saved.run_id, saved.task_id,
                    saved.approval_id, saved.step_id, saved.lease_id,
                    saved.idempotency_key, saved.approval_hash, saved.test_plan_hash,
                    saved.lease_token_hash, saved.bundle_revision, saved.bundle_git_tree_sha,
                    saved.run_revision, saved.step_revision,
                    saved.created_at,
                )
                expected_identity = (
                    intent.id, intent.workspace_id, intent.run_id, intent.task_id,
                    intent.approval_id, intent.step_id, intent.lease_id,
                    intent.idempotency_key, intent.approval_hash, intent.test_plan_hash,
                    intent.lease_token_hash, intent.bundle_revision, intent.bundle_git_tree_sha,
                    intent.run_revision, intent.step_revision,
                    intent.created_at,
                )
                if immutable_identity != expected_identity or saved.status not in {
                    RunValidationIntentStatus.INTENT,
                    RunValidationIntentStatus.EXECUTION_CLAIMED,
                }:
                    raise PostgresTrustRepositoryConflict("Validation intent is stale or permanently fenced")
                if (
                    saved.approval_hash != compute_approval_payload_hash(approval)
                    or approval.approved_payload_hash != saved.approval_hash
                    or saved.test_plan_hash != sha256_hex(approval.test_plan)
                    or saved.lease_token_hash != sha256_hex({"lease_token": lease_token})
                    or expected_run.updated_at != saved.run_revision
                    or expected_step.updated_at != saved.step_revision
                    or expected_run.lease_owner != worker_id
                    or expected_run.lease_token != lease_token
                    or expected_step.status != AgentRunStepStatus.RUNNING
                ):
                    raise PostgresTrustRepositoryConflict("Validation authority changed since intent creation")
                claims_rows = conn.execute(
                    f"SELECT {_COMMAND_COLUMNS} FROM agent_run_validation_command_claims "
                    "WHERE intent_id=%s AND workspace_id=%s ORDER BY command_index FOR UPDATE",
                    (intent.id, intent.workspace_id),
                ).fetchall()
                claims = [_decode_command(row) for row in claims_rows]
                command_index = len(claims)
                if (
                    [claim.command_index for claim in claims] != list(range(command_index))
                    or any(claim.status != RunValidationCommandStatus.RESULT_RECORDED
                           or claim.exit_code != 0 or claim.timed_out is True for claim in claims)
                    or command_index >= len(approval.test_plan)
                ):
                    raise PostgresTrustRepositoryConflict("A command is claimed, failed, ambiguous, or the plan is exhausted")
                command = approval.test_plan[command_index]
                failure = validate_validation_command(command)
                command_hash = sha256_hex({"command": command})
                required_metadata = {
                    "command_index": command_index,
                    "command_source": "approval.test_plan",
                    "command_sha256": command_hash,
                    "step_id": expected_step.id,
                    "step_type": AgentRunStepType.RUN_VALIDATION.value,
                    "idempotency_key": expected_step.idempotency_key,
                    "approval_request_id": approval.id,
                    "approved_payload_hash": saved.approval_hash,
                    "bundle_revision": saved.bundle_revision,
                    "bundle_git_tree_sha": saved.bundle_git_tree_sha,
                }
                if (
                    failure is not None
                    or action.action_type != CapabilityActionType.RUN_COMMAND
                    or action.command != command
                    or action.workspace_id != intent.workspace_id
                    or action.run_id != intent.run_id or action.task_id != intent.task_id
                    or action.lease_id != intent.lease_id
                    or any(action.metadata.get(key) != value for key, value in required_metadata.items())
                    or type(action.metadata.get("command_index")) is not int
                ):
                    raise PostgresTrustRepositoryConflict("Proposed action differs from exact approved command")
                self._trust.insert_proposed_action(conn, action)
                decision, _lease = self._trust.evaluate_agent_run_validation_command(
                    conn, action=action, workspace_id=intent.workspace_id,
                    run_id=intent.run_id, step_id=intent.step_id, worker_id=worker_id,
                    lease_token=lease_token, actor=actor,
                    trust_event_repository=self._trust_events,
                )
                current_run_revision = conn.execute(
                    "SELECT updated_at FROM agent_runs WHERE run_id=%s AND workspace_id=%s",
                    (intent.run_id, intent.workspace_id),
                ).fetchone()
                current_step_revision = conn.execute(
                    "SELECT updated_at FROM agent_run_steps WHERE step_id=%s AND workspace_id=%s",
                    (intent.step_id, intent.workspace_id),
                ).fetchone()
                if (
                    current_run_revision is None or current_step_revision is None
                    or current_run_revision[0] != saved.run_revision
                    or current_step_revision[0] != saved.step_revision
                ):
                    raise PostgresTrustRepositoryConflict(
                        "RUN_VALIDATION run or step revision changed since intent creation"
                    )
                if decision.decision != PolicyDecisionType.ALLOW:
                    denied_reason = "Validation command policy decision requires external resolution"
                else:
                    denied_reason = ""
                    consumption = _validation_action_consumption(
                        intent=intent, action=action, command_index=command_index,
                        command_hash=command_hash, runtime_seconds=timeout_seconds,
                        attempt_count=expected_step.attempt_count,
                    )
                    consumed_at = conn.execute("SELECT clock_timestamp()").fetchone()[0]
                    self._trust.consume_effective_action(
                        conn, action_id=action.id, workspace_id=intent.workspace_id,
                        consumption=consumption, actor=actor, now=consumed_at,
                    )
                    now = conn.execute("SELECT clock_timestamp()").fetchone()[0]
                    claim = AgentRunValidationCommandClaim(
                        intent_id=intent.id, workspace_id=intent.workspace_id,
                        action_id=action.id, command_index=command_index,
                        command_hash=command_hash, status=RunValidationCommandStatus.CLAIMED,
                        claimed_at=now, updated_at=now,
                    )
                    conn.execute(
                        f"INSERT INTO agent_run_validation_command_claims({_COMMAND_COLUMNS.replace(',payload', '')},payload) "
                        "VALUES(" + ",".join(["%s"] * 15) + ",%s)",
                        (claim.intent_id, claim.workspace_id, claim.action_id,
                         claim.command_index, claim.command_hash, claim.status.value,
                         claim.exit_code, claim.timed_out, claim.stdout_sha256,
                         claim.stdout_bytes, claim.stderr_sha256, claim.stderr_bytes,
                         claim.reason, claim.claimed_at, claim.updated_at,
                         Jsonb(_command_payload(claim))),
                    )
                    if saved.status == RunValidationIntentStatus.INTENT:
                        updated = saved.model_copy(update={
                            "status": RunValidationIntentStatus.EXECUTION_CLAIMED,
                            "updated_at": now,
                        })
                        updated_payload = _parent_payload(updated)
                        updated_cursor = conn.execute(
                        "UPDATE agent_run_validation_intents SET status=%s,updated_at=%s,payload=%s "
                            "WHERE intent_id=%s AND workspace_id=%s AND status='INTENT'",
                            (updated.status.value, now, Jsonb(updated_payload), intent.id, intent.workspace_id),
                        )
                        if updated_cursor.rowcount != 1:
                            raise PostgresTrustRepositoryConflict(
                                "RUN_VALIDATION intent status lost its one-shot transition"
                            )
        if denied_reason:
            raise PostgresTrustRepositoryConflict(denied_reason)
        return claim

    def record_result(
        self,
        *,
        claim: AgentRunValidationCommandClaim,
        worker_id: str,
        lease_token: str,
        exit_code: int,
        timed_out: bool,
        stdout_sha256: str,
        stdout_bytes: int,
        stderr_sha256: str,
        stderr_bytes: int,
    ) -> AgentRunValidationCommandClaim:
        if (
            claim.status != RunValidationCommandStatus.CLAIMED
            or not worker_id or not lease_token
            or type(exit_code) is not int or not -255 <= exit_code <= 255
            or type(timed_out) is not bool
            or any(not isinstance(value, str) or not _DIGEST.fullmatch(value)
                   for value in (stdout_sha256, stderr_sha256))
            or any(type(value) is not int or not 0 <= value <= _OUTPUT_LIMIT
                   for value in (stdout_bytes, stderr_bytes))
        ):
            raise ValueError("Validation result is invalid or claim is not in CLAIMED state")
        result = claim.model_copy(update={
            "status": RunValidationCommandStatus.RESULT_RECORDED,
            "exit_code": exit_code, "timed_out": timed_out,
            "stdout_sha256": stdout_sha256, "stdout_bytes": stdout_bytes,
            "stderr_sha256": stderr_sha256, "stderr_bytes": stderr_bytes,
        })
        with self._connect() as conn:
            with conn.transaction():
                identity = conn.execute(
                    "SELECT run_id,task_id,approval_id,step_id FROM agent_run_validation_intents "
                    "WHERE intent_id=%s AND workspace_id=%s",
                    (claim.intent_id, claim.workspace_id),
                ).fetchone()
                if identity is None:
                    raise PostgresTrustRepositoryConflict("Validation intent is not visible")
                approval, _task, run, step = self._lock_validation_authority(
                    conn, workspace_id=claim.workspace_id, run_id=identity[0],
                    task_id=identity[1], approval_id=identity[2], step_id=identity[3],
                )
                parent_row = conn.execute(
                    f"SELECT {_PARENT_COLUMNS} FROM agent_run_validation_intents "
                    "WHERE intent_id=%s AND workspace_id=%s FOR UPDATE",
                    (claim.intent_id, claim.workspace_id),
                ).fetchone()
                if parent_row is None:
                    raise PostgresTrustRepositoryConflict("Validation intent is not visible")
                parent = _decode_parent(parent_row)
                if (
                    parent.status != RunValidationIntentStatus.EXECUTION_CLAIMED
                    or parent.run_id != identity[0] or parent.task_id != identity[1]
                    or parent.approval_id != identity[2] or parent.step_id != identity[3]
                    or parent.workspace_id != claim.workspace_id
                ):
                    raise PostgresTrustRepositoryConflict("Validation result has no current executable intent")
                claim_row = conn.execute(
                    f"SELECT {_COMMAND_COLUMNS} FROM agent_run_validation_command_claims "
                    "WHERE intent_id=%s AND workspace_id=%s AND command_index=%s FOR UPDATE",
                    (claim.intent_id, claim.workspace_id, claim.command_index),
                ).fetchone()
                if claim_row is None or _decode_command(claim_row) != claim:
                    raise PostgresTrustRepositoryConflict("Validation result claim identity changed")
                # Authority rows are already locked before intent and claim rows;
                # this second binding locks the exact action and lease afterward.
                action = self._trust.lock_agent_run_validation_command_binding(
                    conn, action_id=claim.action_id, workspace_id=claim.workspace_id,
                    run_id=parent.run_id, step_id=parent.step_id,
                    worker_id=worker_id, lease_token=lease_token,
                )
                if (
                    run.updated_at != parent.run_revision
                    or step.updated_at != parent.step_revision
                    or sha256_hex({"lease_token": lease_token}) != parent.lease_token_hash
                    or action.metadata.get("command_index") != claim.command_index
                    or action.command != approval.test_plan[claim.command_index]
                    or sha256_hex({"command": action.command}) != claim.command_hash
                    or action.metadata.get("bundle_revision") != parent.bundle_revision
                    or action.metadata.get("bundle_git_tree_sha") != parent.bundle_git_tree_sha
                ):
                    raise PostgresTrustRepositoryConflict("Validation command authority changed before result persistence")
                now = conn.execute("SELECT clock_timestamp()").fetchone()[0]
                result = result.model_copy(update={"updated_at": now})
                cursor = conn.execute(
                    "UPDATE agent_run_validation_command_claims SET status=%s,exit_code=%s,timed_out=%s,"
                    "stdout_sha256=%s,stdout_bytes=%s,stderr_sha256=%s,stderr_bytes=%s,updated_at=%s,payload=%s "
                    "WHERE intent_id=%s AND workspace_id=%s AND command_index=%s "
                    "AND action_id=%s AND command_hash=%s AND status='CLAIMED'",
                    (result.status.value, result.exit_code, result.timed_out,
                     result.stdout_sha256, result.stdout_bytes, result.stderr_sha256,
                     result.stderr_bytes, result.updated_at, Jsonb(_command_payload(result)),
                     claim.intent_id, claim.workspace_id, claim.command_index,
                     claim.action_id, claim.command_hash),
                )
                if cursor.rowcount != 1:
                    raise PostgresTrustRepositoryConflict("Validation command result lost its one-shot claim CAS")
        return result

    def mark_reconciliation_required(
        self,
        *,
        intent_id: str,
        workspace_id: str,
        command_index: int,
        reason: str,
    ) -> tuple[AgentRunValidationIntent, AgentRunValidationCommandClaim]:
        reason = reason.strip()[:1000]
        if not intent_id or not workspace_id or type(command_index) is not int or command_index < 0 or not reason:
            raise ValueError("Validation reconciliation requires exact identity and reason")
        with self._connect() as conn:
            with conn.transaction():
                identity = conn.execute(
                    "SELECT run_id,task_id,approval_id,step_id FROM agent_run_validation_intents "
                    "WHERE intent_id=%s AND workspace_id=%s",
                    (intent_id, workspace_id),
                ).fetchone()
                if identity is None:
                    raise PostgresTrustRepositoryConflict("Validation intent is not visible")
                self._lock_validation_authority(
                    conn, workspace_id=workspace_id, run_id=identity[0],
                    task_id=identity[1], approval_id=identity[2],
                    step_id=identity[3], require_running=False,
                )
                parent_row = conn.execute(
                    f"SELECT {_PARENT_COLUMNS} FROM agent_run_validation_intents "
                    "WHERE intent_id=%s AND workspace_id=%s FOR UPDATE",
                    (intent_id, workspace_id),
                ).fetchone()
                if parent_row is None:
                    raise PostgresTrustRepositoryConflict("Validation intent is not visible")
                parent = _decode_parent(parent_row)
                claim_row = conn.execute(
                    f"SELECT {_COMMAND_COLUMNS} FROM agent_run_validation_command_claims "
                    "WHERE intent_id=%s AND workspace_id=%s AND command_index=%s FOR UPDATE",
                    (intent_id, workspace_id, command_index),
                ).fetchone()
                if claim_row is None:
                    raise PostgresTrustRepositoryConflict("No durable command claim exists to reconcile")
                claim = _decode_command(claim_row)
                if claim.status not in {
                    RunValidationCommandStatus.CLAIMED,
                    RunValidationCommandStatus.RECONCILIATION_REQUIRED,
                }:
                    raise PostgresTrustRepositoryConflict("Recorded command result cannot be made ambiguous")
                now = conn.execute("SELECT clock_timestamp()").fetchone()[0]
                updated_claim = claim.model_copy(update={
                    "status": RunValidationCommandStatus.RECONCILIATION_REQUIRED,
                    "reason": reason, "updated_at": now,
                })
                updated_parent = parent.model_copy(update={
                    "status": RunValidationIntentStatus.RECONCILIATION_REQUIRED,
                    "reason": reason, "updated_at": now,
                })
                claim_cursor = conn.execute(
                    "UPDATE agent_run_validation_command_claims SET status=%s,reason=%s,updated_at=%s,payload=%s "
                    "WHERE intent_id=%s AND workspace_id=%s AND command_index=%s "
                    "AND status IN ('CLAIMED','RECONCILIATION_REQUIRED')",
                    (updated_claim.status.value, reason, now, Jsonb(_command_payload(updated_claim)),
                     intent_id, workspace_id, command_index),
                )
                if claim_cursor.rowcount != 1:
                    raise PostgresTrustRepositoryConflict("Validation reconciliation lost its command CAS")
                parent_cursor = conn.execute(
                    "UPDATE agent_run_validation_intents SET status=%s,reason=%s,updated_at=%s,payload=%s "
                    "WHERE intent_id=%s AND workspace_id=%s AND status IN ('EXECUTION_CLAIMED','RECONCILIATION_REQUIRED')",
                    (updated_parent.status.value, reason, now, Jsonb(_parent_payload(updated_parent)),
                     intent_id, workspace_id),
                )
                if parent_cursor.rowcount != 1:
                    raise PostgresTrustRepositoryConflict("Validation reconciliation lost its intent CAS")
        return updated_parent, updated_claim
