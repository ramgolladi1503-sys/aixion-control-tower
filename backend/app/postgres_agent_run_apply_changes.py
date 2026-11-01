from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Callable

from pydantic import BaseModel
from psycopg import Connection
from psycopg.types.json import Jsonb

from .agent_run_models import AgentRun, AgentRunEvent, AgentRunEventType, AgentRunStep, AgentRunStepStatus, AgentRunStatus
from .postgres_agent_run_branch import AgentRunBranchIntent, BranchIntentStatus
from .agent_task_models import AgentTask, AgentTaskStatus
from .approval_integrity import compute_approval_payload_hash
from .models import ApprovalRequest, ApprovalStatus, AuditEvent
from .postgres_trust_event_repository import PostgresTrustEventRepository
from .postgres_trust_repository import PostgresTrustRepository, PostgresTrustRepositoryConflict
from .trust_models import ActionConsumption, CapabilityActionType, CapabilityLeaseStatus, ProposedAction
from .trust_crypto import sha256_hex
from .trust_crypto import verify_payload_signature
from .trust_policy import lease_receipt_payload


class ApplyChangeIntentStatus(StrEnum):
    INTENT = "INTENT"
    POST_CLAIMED = "POST_CLAIMED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    REMOTE_VERIFIED = "REMOTE_VERIFIED"
    COMPLETED = "COMPLETED"


class AgentRunApplyChangeIntent(BaseModel):
    id: str
    workspace_id: str
    run_id: str
    task_id: str
    approval_id: str
    step_id: str
    action_id: str
    lease_id: str
    idempotency_key: str
    repository: str
    branch: str
    path: str
    change_type: str
    before_sha: str | None = None
    desired_content_sha256: str
    lease_token_hash: str
    run_revision: datetime
    step_revision: datetime
    status: ApplyChangeIntentStatus = ApplyChangeIntentStatus.INTENT
    commit_sha: str | None = None
    reason: str = ""
    created_at: datetime
    updated_at: datetime


_COLUMNS = (
    "intent_id,workspace_id,run_id,task_id,approval_id,step_id,action_id,lease_id,"
    "idempotency_key,repository,branch,path,change_type,before_sha,desired_content_sha256,"
    "lease_token_hash,run_revision,step_revision,status,commit_sha,reason,created_at,updated_at,payload"
)


def _payload(record: AgentRunApplyChangeIntent) -> dict[str, Any]:
    return record.model_dump(mode="json")


def _decode(row: tuple[Any, ...]) -> AgentRunApplyChangeIntent:
    record = AgentRunApplyChangeIntent.model_validate(row[23])
    indexed = (
        record.id, record.workspace_id, record.run_id, record.task_id, record.approval_id,
        record.step_id, record.action_id, record.lease_id, record.idempotency_key,
        record.repository, record.branch, record.path, record.change_type, record.before_sha,
        record.desired_content_sha256, record.lease_token_hash, record.run_revision,
        record.step_revision, record.status.value, record.commit_sha, record.reason,
        record.created_at, record.updated_at,
    )
    if indexed != tuple(row[:23]):
        raise ValueError("Apply-change intent payload differs from indexed state")
    if record.change_type not in {"create", "update"}:
        raise ValueError("Stored apply-change intent uses an unsupported operation")
    if len(record.desired_content_sha256) != 64 or len(record.lease_token_hash) != 64:
        raise ValueError("Stored apply-change intent digest is invalid")
    return record


def _lease_allows_path(lease: Any, path: str) -> bool:
    return any(
        path == prefix.rstrip("/") or path.startswith(prefix.rstrip("/") + "/")
        for prefix in lease.scope.allowed_path_prefixes
    )


def _lease_matches_task(lease: Any, task: AgentTask) -> bool:
    return lease.provider == task.provider and lease.agent_id == task.external_agent_id


def _consumption_matches_intent(
    consumption: ActionConsumption | None,
    intent: AgentRunApplyChangeIntent,
) -> bool:
    if consumption is None or not 1 <= consumption.actual_runtime_seconds <= 3600:
        return False
    authorized_attempt_count = consumption.evidence.get("authorized_attempt_count")
    expected_retry_consumed = consumption.evidence.get("authorized_retry_consumed")
    if (
        type(authorized_attempt_count) is not int or authorized_attempt_count < 1
        or not isinstance(expected_retry_consumed, bool)
        or expected_retry_consumed != (authorized_attempt_count > 1)
        or consumption.retry_consumed != expected_retry_consumed
    ):
        return False
    expected_evidence = {
        "workspace_id": intent.workspace_id,
        "run_id": intent.run_id,
        "task_id": intent.task_id,
        "approval_id": intent.approval_id,
        "step_id": intent.step_id,
        "action_id": intent.action_id,
        "lease_id": intent.lease_id,
        "intent_id": intent.id,
        "path": intent.path,
        "content_sha256": intent.desired_content_sha256,
        "runtime_seconds_reserved": consumption.actual_runtime_seconds,
        "authorized_attempt_count": authorized_attempt_count,
        "authorized_retry_consumed": expected_retry_consumed,
    }
    return (
        consumption.evidence == expected_evidence
        and consumption.actual_cost_usd == 0
        and consumption.pull_requests_created == 0
        and consumption.output_reference is None
    )


class PostgresAgentRunApplyChangeIntentRepository:
    """Workspace-scoped durable fence for one approved file mutation per step."""

    def __init__(
        self, connect: Callable[[], Connection], *,
        trust_repository: PostgresTrustRepository | None = None,
        trust_event_repository: PostgresTrustEventRepository | None = None,
    ) -> None:
        self._connect = connect
        self._trust = trust_repository or PostgresTrustRepository()
        self._trust_events = trust_event_repository or PostgresTrustEventRepository(connect)

    def get_for_step(
        self, *, workspace_id: str, run_id: str, step_id: str, idempotency_key: str
    ) -> AgentRunApplyChangeIntent | None:
        if not workspace_id:
            raise ValueError("Apply-change intent reads require workspace scope")
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM agent_run_apply_change_intents "
                "WHERE workspace_id=%s AND run_id=%s AND step_id=%s AND idempotency_key=%s",
                (workspace_id, run_id, step_id, idempotency_key),
            ).fetchone()
        return _decode(row) if row else None

    def record_authorized_intent(
        self, *, record: AgentRunApplyChangeIntent, action: ProposedAction,
        expected_run: AgentRun, expected_step: AgentRunStep, timeout_seconds: int,
        actor: str, now: datetime,
    ) -> AgentRunApplyChangeIntent:
        """Persist exact action, policy decision, runtime reservation, and intent atomically."""
        if (
            record.status != ApplyChangeIntentStatus.INTENT
            or action.action_type != CapabilityActionType.MODIFY_FILES
            or action.paths != [record.path]
            or action.metadata.get("content_sha256") != record.desired_content_sha256
            or action.metadata.get("change_type") != record.change_type
            or action.metadata.get("before_sha") != record.before_sha
            or action.metadata.get("idempotency_key") != record.idempotency_key
            or action.metadata.get("step_id") != record.step_id
            or expected_run.id != record.run_id or expected_step.id != record.step_id
            or not 1 <= timeout_seconds <= 3600
        ):
            raise PostgresTrustRepositoryConflict("Apply-change exact action scope is incomplete")
        with self._connect() as conn:
            with conn.transaction():
                decision, lease = self._trust.evaluate_agent_run_action(
                    conn, action=action, workspace_id=record.workspace_id,
                    run_id=record.run_id, step_id=record.step_id, actor=actor,
                    now=now, trust_event_repository=self._trust_events,
                )
                if decision.decision.value != "ALLOW" or decision.lease_id != lease.id:
                    raise PostgresTrustRepositoryConflict("Apply-change action was not allowed")
                self._trust.consume_agent_run_action(
                    conn, action_id=action.id, workspace_id=record.workspace_id,
                    run_id=record.run_id, step_id=record.step_id,
                    consumption=ActionConsumption(
                        actual_runtime_seconds=timeout_seconds,
                        retry_consumed=expected_step.attempt_count > 1,
                        evidence={"workspace_id": record.workspace_id, "run_id": record.run_id,
                                  "task_id": record.task_id, "approval_id": record.approval_id,
                                  "step_id": record.step_id, "action_id": record.action_id,
                                  "lease_id": record.lease_id, "intent_id": record.id, "path": record.path,
                                  "content_sha256": record.desired_content_sha256,
                                  "runtime_seconds_reserved": timeout_seconds,
                                  "authorized_attempt_count": expected_step.attempt_count,
                                  "authorized_retry_consumed": expected_step.attempt_count > 1},
                    ), actor=actor, now=now,
                )
                conn.execute(
                    f"INSERT INTO agent_run_apply_change_intents({_COLUMNS.replace(',payload', '')},payload) "
                    "VALUES(" + ",".join(["%s"] * 23) + ",%s)",
                    (record.id, record.workspace_id, record.run_id, record.task_id,
                     record.approval_id, record.step_id, record.action_id, record.lease_id,
                     record.idempotency_key, record.repository, record.branch, record.path,
                     record.change_type, record.before_sha, record.desired_content_sha256,
                     record.lease_token_hash, record.run_revision, record.step_revision,
                     record.status.value, record.commit_sha, record.reason, record.created_at,
                     record.updated_at, Jsonb(_payload(record))),
                )
                event = AgentRunEvent(
                    run_id=record.run_id, task_id=record.task_id, step_id=record.step_id,
                    correlation_id=expected_run.correlation_id,
                    event_type=AgentRunEventType.OPERATOR_ACTION, actor=actor,
                    attempt_number=expected_step.attempt_count,
                    message="Durable exact-file intent recorded before GitHub mutation.",
                    metadata={"intent_id": record.id, "action_id": action.id,
                              "path": record.path, "change_type": record.change_type,
                              "content_sha256": record.desired_content_sha256,
                              "capability_consumed": True},
                )
                audit = AuditEvent(
                    event_type="agent_run.apply_change_intent_recorded",
                    entity_id=record.run_id, workspace_id=record.workspace_id, actor=actor,
                    details={"intent_id": record.id, "action_id": action.id,
                             "path": record.path, "change_type": record.change_type,
                             "content_sha256": record.desired_content_sha256}, created_at=now,
                )
                conn.execute(
                    "INSERT INTO agent_run_events(event_id,run_id,task_id,workspace_id,created_at,event_type,payload) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    (event.id, event.run_id, event.task_id, record.workspace_id,
                     event.created_at, event.event_type.value, Jsonb(event.model_dump(mode="json"))),
                )
                conn.execute(
                    "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,event_type,actor,details,created_at,payload) "
                    "VALUES(%s,%s,NULL,'agent_run',%s,%s,%s,%s,%s,%s)",
                    (audit.id, record.workspace_id, record.run_id, audit.event_type,
                     audit.actor, Jsonb(audit.details), now, Jsonb(audit.model_dump(mode="json"))),
                )
        return record

    def claim_write_once(
        self, *, intent: AgentRunApplyChangeIntent, expected_run: AgentRun,
        expected_step: AgentRunStep, worker_lease_token: str, now: datetime,
    ) -> AgentRunApplyChangeIntent:
        """Commit the unique remote-write fence. Claimed intents can only be reconciled by GET."""
        if not intent.workspace_id or not worker_lease_token:
            raise ValueError("Apply-change claim requires workspace and lease identity")
        with self._connect() as conn:
            with conn.transaction():
                # Global lock order matches governed actions: approval -> task -> run -> step ->
                # intent/action/decision -> capability lease. Revocation serializes before POST.
                approval_row = conn.execute(
                    "SELECT approval_id,project_id,workspace_id,status,payload FROM approval_requests "
                    "WHERE approval_id=%s AND workspace_id=%s FOR SHARE",
                    (intent.approval_id, intent.workspace_id),
                ).fetchone()
                task_row = conn.execute(
                    "SELECT task_id,project_id,workspace_id,approval_request_id,status,payload "
                    "FROM agent_tasks WHERE task_id=%s AND workspace_id=%s FOR SHARE",
                    (intent.task_id, intent.workspace_id),
                ).fetchone()
                if approval_row is None or task_row is None:
                    raise PostgresTrustRepositoryConflict("Apply-change approval or task authority is missing")
                run_row = conn.execute(
                    "SELECT run_id,workspace_id,task_id,project_id,status,current_step_id,updated_at,payload "
                    "FROM agent_runs WHERE run_id=%s AND workspace_id=%s FOR UPDATE",
                    (intent.run_id, intent.workspace_id),
                ).fetchone()
                step_row = conn.execute(
                    "SELECT step_id,run_id,task_id,workspace_id,status,updated_at,payload "
                    "FROM agent_run_steps WHERE step_id=%s AND run_id=%s AND workspace_id=%s FOR UPDATE",
                    (intent.step_id, intent.run_id, intent.workspace_id),
                ).fetchone()
                row = conn.execute(
                    f"SELECT {_COLUMNS} FROM agent_run_apply_change_intents "
                    "WHERE intent_id=%s AND workspace_id=%s FOR UPDATE",
                    (intent.id, intent.workspace_id),
                ).fetchone()
                if run_row is None or step_row is None or row is None:
                    raise PostgresTrustRepositoryConflict("Apply-change durable run, step, or intent is missing")
                current = _decode(row)
                try:
                    approval = ApprovalRequest.model_validate(approval_row[4])
                    task = AgentTask.model_validate(task_row[5])
                    run = AgentRun.model_validate(run_row[7])
                    step = AgentRunStep.model_validate(step_row[6])
                except Exception as error:
                    raise PostgresTrustRepositoryConflict("Apply-change authority payload is invalid") from error
                db_now = conn.execute("SELECT clock_timestamp()").fetchone()[0]
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
                    or step.task_id != step_row[2] or step_row[3] != current.workspace_id
                    or step.status.value != step_row[4] or step.updated_at != step_row[5]
                ):
                    raise PostgresTrustRepositoryConflict("Apply-change indexed authority differs from payload")
                action = self._trust.get_proposed_action(conn, current.action_id, current.workspace_id, for_update=True)
                decision = self._trust.get_latest_policy_decision_for_action(conn, current.action_id, current.workspace_id, for_update=True)
                branch_rows = conn.execute(
                    "SELECT status,repository,target_ref,source_sha,payload FROM agent_run_branch_intents "
                    "WHERE workspace_id=%s AND run_id=%s FOR SHARE",
                    (current.workspace_id, current.run_id),
                ).fetchall()
                branch_binding = False
                if len(branch_rows) == 1:
                    try:
                        branch_record = AgentRunBranchIntent.model_validate(branch_rows[0][4])
                        branch_binding = (
                            branch_record.status == BranchIntentStatus.COMPLETED
                            and branch_record.repository == branch_rows[0][1]
                            and branch_record.target_ref == branch_rows[0][2]
                            and branch_record.source_sha == branch_rows[0][3]
                            and branch_record.repository == current.repository
                            and branch_record.target_ref == current.branch
                            and action is not None
                            and action.metadata.get("base_commit_sha") == branch_record.source_sha
                        )
                    except Exception:
                        branch_binding = False
                lease = self._trust.get_capability_lease(conn, current.lease_id, current.workspace_id, for_update=True)
                consumption = self._trust.get_action_consumption(conn, current.action_id, current.workspace_id)
                approved_files = [item for item in approval.files if item.path == current.path]
                import hashlib
                exact_content = bool(
                    len(approval.files) == 1 and len(approved_files) == 1
                    and approved_files[0].change_type.strip().lower() == current.change_type
                    and approved_files[0].new_content is not None
                    and hashlib.sha256(approved_files[0].new_content.encode("utf-8")).hexdigest()
                    == current.desired_content_sha256
                )
                if (
                    current != intent or current.status != ApplyChangeIntentStatus.INTENT
                    or run != expected_run or step != expected_step
                    or run.status != AgentRunStatus.RUNNING or run.current_step_id != step.id
                    or run.pause_requested or run.cancel_requested
                    or run.lease_owner is None or run.lease_token != worker_lease_token
                    or run.lease_expires_at is None or run.lease_expires_at <= db_now
                    or step.status != AgentRunStepStatus.RUNNING or step.step_type.value != "APPLY_CHANGES"
                    or step.lease_token != worker_lease_token
                    or current.run_revision != run.updated_at or current.step_revision != step.updated_at
                    or current.lease_token_hash != sha256_hex({"lease_token": worker_lease_token})
                    or run.task_id != current.task_id or run.workspace_id != current.workspace_id
                    or run.project_id != task.project_id or run.approval_request_id != current.approval_id
                    or run.capability_lease_id != current.lease_id
                    or task.id != current.task_id or task.workspace_id != current.workspace_id
                    or task.project_id != run.project_id or task.approval_request_id != current.approval_id
                    or task.status != AgentTaskStatus.APPROVED
                    or approval.id != current.approval_id or approval.project_id != run.project_id
                    or approval.workspace_id != current.workspace_id or approval.status != ApprovalStatus.APPROVED
                    or approval.approved_payload_hash is None
                    or approval.approved_payload_hash != compute_approval_payload_hash(approval)
                    or not exact_content or action is None or decision is None or lease is None
                    or action.id != current.action_id or action.action_type != CapabilityActionType.MODIFY_FILES
                    or action.workspace_id != current.workspace_id or action.run_id != current.run_id
                    or action.task_id != current.task_id or action.lease_id != current.lease_id
                    or action.repository != current.repository or action.branch != current.branch
                    or action.paths != [current.path]
                    or action.metadata.get("path") != current.path
                    or action.metadata.get("change_type") != current.change_type
                    or action.metadata.get("content_sha256") != current.desired_content_sha256
                    or action.metadata.get("before_sha") != current.before_sha
                    or not branch_binding
                    or action.metadata.get("step_id") != current.step_id
                    or action.metadata.get("step_type") != "APPLY_CHANGES"
                    or action.metadata.get("idempotency_key") != current.idempotency_key
                    or action.metadata.get("approval_id") != current.approval_id
                    or action.metadata.get("lease_id") != current.lease_id
                    or action.provider != task.provider or action.agent_id != task.external_agent_id
                    or action.project_id != task.project_id
                    or decision.proposed_action_id != action.id or decision.lease_id != lease.id
                    or decision.decision.value != "ALLOW"
                    or lease.status != CapabilityLeaseStatus.ACTIVE or lease.expires_at <= db_now
                    or lease.approval_request_id != approval.id
                    or lease.approved_payload_hash != approval.approved_payload_hash
                    or not _lease_matches_task(lease, task)
                    or lease.scope.repository != current.repository or lease.scope.branch != current.branch
                    or CapabilityActionType.MODIFY_FILES not in lease.scope.allowed_actions
                    or not _lease_allows_path(lease, current.path)
                    or not verify_payload_signature(lease_receipt_payload(lease), lease.receipt_signature)
                    or not _consumption_matches_intent(consumption, current)
                ):
                    raise PostgresTrustRepositoryConflict("Apply-change authority changed before one-shot write")
                claimed = current.model_copy(update={
                    "status": ApplyChangeIntentStatus.POST_CLAIMED,
                    "reason": "One-shot GitHub write claimed; all recovery attempts are GET-only.",
                    "updated_at": now,
                })
                changed = conn.execute(
                    "UPDATE agent_run_apply_change_intents SET status=%s,reason=%s,updated_at=%s,payload=%s "
                    "WHERE intent_id=%s AND workspace_id=%s AND status='INTENT' "
                    "AND lease_token_hash=%s AND run_revision=%s AND step_revision=%s",
                    (claimed.status.value, claimed.reason, now, Jsonb(_payload(claimed)),
                     claimed.id, claimed.workspace_id, claimed.lease_token_hash,
                     claimed.run_revision, claimed.step_revision),
                )
                if changed.rowcount != 1:
                    raise ValueError("Apply-change write claim lost its compare-and-swap")
                return claimed

    def record_observation(
        self, *, intent: AgentRunApplyChangeIntent, expected_status: ApplyChangeIntentStatus,
        status: ApplyChangeIntentStatus, commit_sha: str | None, reason: str, now: datetime,
    ) -> AgentRunApplyChangeIntent:
        if expected_status not in {ApplyChangeIntentStatus.POST_CLAIMED, ApplyChangeIntentStatus.RECONCILIATION_REQUIRED}:
            raise ValueError("Apply-change observation requires a claimed intent")
        if status not in {ApplyChangeIntentStatus.RECONCILIATION_REQUIRED, ApplyChangeIntentStatus.REMOTE_VERIFIED}:
            raise ValueError("Apply-change observation status is invalid")
        with self._connect() as conn:
            with conn.transaction():
                row = conn.execute(
                    f"SELECT {_COLUMNS} FROM agent_run_apply_change_intents "
                    "WHERE intent_id=%s AND workspace_id=%s FOR UPDATE",
                    (intent.id, intent.workspace_id),
                ).fetchone()
                if row is None:
                    raise ValueError("Apply-change intent is missing")
                current = _decode(row)
                if current != intent or current.status != expected_status:
                    raise ValueError("Apply-change observation lost its compare-and-swap")
                updated = current.model_copy(update={
                    "status": status, "commit_sha": commit_sha,
                    "reason": reason, "updated_at": now,
                })
                changed = conn.execute(
                    "UPDATE agent_run_apply_change_intents SET status=%s,commit_sha=%s,reason=%s,updated_at=%s,payload=%s "
                    "WHERE intent_id=%s AND workspace_id=%s AND status=%s",
                    (updated.status.value, updated.commit_sha, updated.reason, now,
                     Jsonb(_payload(updated)), updated.id, updated.workspace_id, expected_status.value),
                )
                if changed.rowcount != 1:
                    raise ValueError("Apply-change observation lost its database fence")
                return updated

    def rebind_verified_completion(
        self, *, intent: AgentRunApplyChangeIntent, expected_run: AgentRun,
        expected_step: AgentRunStep, worker_lease_token: str, now: datetime,
    ) -> AgentRunApplyChangeIntent:
        """Rebind an already observed remote result to a fresh worker lease; never authorizes a write."""
        with self._connect() as conn:
            with conn.transaction():
                # Same authority lock order as the one-shot claim. The immutable action/result
                # must still match; this transition only refreshes the run/step lease fence.
                approval_row = conn.execute(
                    "SELECT approval_id,project_id,workspace_id,status,payload FROM approval_requests "
                    "WHERE approval_id=%s AND workspace_id=%s FOR SHARE",
                    (intent.approval_id, intent.workspace_id),
                ).fetchone()
                task_row = conn.execute(
                    "SELECT task_id,project_id,workspace_id,approval_request_id,status,payload FROM agent_tasks "
                    "WHERE task_id=%s AND workspace_id=%s FOR SHARE",
                    (intent.task_id, intent.workspace_id),
                ).fetchone()
                run_row = conn.execute(
                    "SELECT run_id,workspace_id,task_id,project_id,status,current_step_id,updated_at,payload "
                    "FROM agent_runs WHERE run_id=%s AND workspace_id=%s FOR UPDATE",
                    (intent.run_id, intent.workspace_id),
                ).fetchone()
                step_row = conn.execute(
                    "SELECT step_id,run_id,task_id,workspace_id,status,updated_at,payload "
                    "FROM agent_run_steps WHERE step_id=%s AND run_id=%s AND workspace_id=%s FOR UPDATE",
                    (intent.step_id, intent.run_id, intent.workspace_id),
                ).fetchone()
                intent_row = conn.execute(
                    f"SELECT {_COLUMNS} FROM agent_run_apply_change_intents "
                    "WHERE intent_id=%s AND workspace_id=%s FOR UPDATE",
                    (intent.id, intent.workspace_id),
                ).fetchone()
                if any(row is None for row in (approval_row, task_row, run_row, step_row, intent_row)):
                    raise PostgresTrustRepositoryConflict("Verified apply intent rebind authority is missing")
                current = _decode(intent_row)
                run = AgentRun.model_validate(run_row[7])
                step = AgentRunStep.model_validate(step_row[6])
                approval = ApprovalRequest.model_validate(approval_row[4])
                task = AgentTask.model_validate(task_row[5])
                action = self._trust.get_proposed_action(conn, current.action_id, current.workspace_id, for_update=True)
                decision = self._trust.get_latest_policy_decision_for_action(conn, current.action_id, current.workspace_id, for_update=True)
                lease = self._trust.get_capability_lease(conn, current.lease_id, current.workspace_id, for_update=True)
                db_now = conn.execute("SELECT clock_timestamp()").fetchone()[0]
                consumption = self._trust.get_action_consumption(conn, current.action_id, current.workspace_id)
                branch_rows = conn.execute(
                    "SELECT status,repository,target_ref,source_sha,payload FROM agent_run_branch_intents "
                    "WHERE workspace_id=%s AND run_id=%s FOR SHARE",
                    (current.workspace_id, current.run_id),
                ).fetchall()
                branch_binding = False
                if len(branch_rows) == 1:
                    try:
                        branch_record = AgentRunBranchIntent.model_validate(branch_rows[0][4])
                        branch_binding = (
                            branch_record.status == BranchIntentStatus.COMPLETED
                            and branch_record.repository == branch_rows[0][1]
                            and branch_record.target_ref == branch_rows[0][2]
                            and branch_record.source_sha == branch_rows[0][3]
                            and branch_record.repository == current.repository
                            and branch_record.target_ref == current.branch
                            and action is not None
                            and action.metadata.get("base_commit_sha") == branch_record.source_sha
                        )
                    except Exception:
                        branch_binding = False
                approved_files = [item for item in approval.files if item.path == current.path]
                import hashlib
                approved_content_matches = bool(
                    len(approval.files) == 1 and len(approved_files) == 1
                    and approved_files[0].change_type.strip().lower() == current.change_type
                    and approved_files[0].new_content is not None
                    and hashlib.sha256(approved_files[0].new_content.encode("utf-8")).hexdigest()
                    == current.desired_content_sha256
                )
                if (
                    current != intent or current.status != ApplyChangeIntentStatus.REMOTE_VERIFIED
                    or run != expected_run or step != expected_step
                    or run.id != run_row[0] or run.workspace_id != run_row[1]
                    or run.task_id != run_row[2] or run.project_id != run_row[3]
                    or run.status.value != run_row[4] or run.current_step_id != run_row[5]
                    or run.updated_at != run_row[6]
                    or step.id != step_row[0] or step.run_id != step_row[1]
                    or step.task_id != step_row[2] or step_row[3] != current.workspace_id
                    or step.status.value != step_row[4] or step.updated_at != step_row[5]
                    or run.status != AgentRunStatus.RUNNING or run.current_step_id != step.id
                    or run.pause_requested or run.cancel_requested
                    or run.lease_owner is None or run.lease_token != worker_lease_token
                    or run.lease_expires_at is None or run.lease_expires_at <= db_now
                    or step.status != AgentRunStepStatus.RUNNING or step.step_type.value != "APPLY_CHANGES"
                    or step.lease_token != worker_lease_token
                    or approval.id != approval_row[0] or approval.project_id != approval_row[1]
                    or approval.workspace_id != approval_row[2] or approval.status.value != approval_row[3]
                    or task.id != task_row[0] or task.project_id != task_row[1]
                    or task.workspace_id != task_row[2] or task.approval_request_id != task_row[3]
                    or task.status.value != task_row[4]
                    or approval.id != current.approval_id or task.id != current.task_id
                    or approval.workspace_id != current.workspace_id or task.workspace_id != current.workspace_id
                    or approval.project_id != task.project_id or task.project_id != run.project_id
                    or approval.status != ApprovalStatus.APPROVED or task.status != AgentTaskStatus.APPROVED
                    or approval.approved_payload_hash is None
                    or approval.approved_payload_hash != compute_approval_payload_hash(approval)
                    or action is None or action.id != current.action_id
                    or action.action_type != CapabilityActionType.MODIFY_FILES
                    or action.workspace_id != current.workspace_id or action.run_id != current.run_id
                    or action.task_id != current.task_id or action.lease_id != current.lease_id
                    or action.repository != current.repository or action.branch != current.branch
                    or action.paths != [current.path]
                    or action.metadata.get("path") != current.path
                    or action.metadata.get("content_sha256") != current.desired_content_sha256
                    or action.metadata.get("change_type") != current.change_type
                    or action.metadata.get("before_sha") != current.before_sha
                    or action.metadata.get("repository") != current.repository
                    or action.metadata.get("target_ref") != current.branch
                    or action.metadata.get("step_id") != current.step_id
                    or action.metadata.get("step_type") != "APPLY_CHANGES"
                    or action.metadata.get("idempotency_key") != current.idempotency_key
                    or action.metadata.get("task_id") != current.task_id
                    or action.metadata.get("approval_id") != current.approval_id
                    or action.metadata.get("lease_id") != current.lease_id
                    or action.metadata.get("base_commit_sha") != (branch_rows[0][3] if branch_binding else None)
                    or action.provider != task.provider or action.agent_id != task.external_agent_id
                    or action.project_id != task.project_id
                    or not branch_binding or not approved_content_matches
                    or decision is None or decision.proposed_action_id != action.id
                    or decision.lease_id != current.lease_id or decision.decision.value != "ALLOW"
                    or lease is None or lease.approval_request_id != approval.id
                    or lease.approved_payload_hash != approval.approved_payload_hash
                    or lease.status != CapabilityLeaseStatus.ACTIVE or lease.expires_at <= db_now
                    or not _lease_matches_task(lease, task)
                    or lease.scope.repository != current.repository or lease.scope.branch != current.branch
                    or CapabilityActionType.MODIFY_FILES not in lease.scope.allowed_actions
                    or not _lease_allows_path(lease, current.path)
                    or not verify_payload_signature(lease_receipt_payload(lease), lease.receipt_signature)
                    or not _consumption_matches_intent(consumption, current)
                    or not current.commit_sha
                ):
                    raise PostgresTrustRepositoryConflict("Verified apply result cannot be safely rebound")
                updated = current.model_copy(update={
                    "lease_token_hash": sha256_hex({"lease_token": worker_lease_token}),
                    "run_revision": run.updated_at,
                    "step_revision": step.updated_at,
                    "reason": "Verified remote result rebound to current run lease; no provider write permitted.",
                    "updated_at": now,
                })
                changed = conn.execute(
                    "UPDATE agent_run_apply_change_intents SET lease_token_hash=%s,run_revision=%s,"
                    "step_revision=%s,reason=%s,updated_at=%s,payload=%s "
                    "WHERE intent_id=%s AND workspace_id=%s AND status='REMOTE_VERIFIED' "
                    "AND lease_token_hash=%s AND run_revision=%s AND step_revision=%s",
                    (updated.lease_token_hash, updated.run_revision, updated.step_revision,
                     updated.reason, now, Jsonb(_payload(updated)), current.id,
                     current.workspace_id, current.lease_token_hash, current.run_revision,
                     current.step_revision),
                )
                if changed.rowcount != 1:
                    raise PostgresTrustRepositoryConflict("Verified apply result rebind lost its CAS")
                return updated
