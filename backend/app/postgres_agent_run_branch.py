from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Callable

from pydantic import BaseModel
from psycopg import Connection
from psycopg.types.json import Jsonb

from .agent_run_models import (
    AgentRun,
    AgentRunEvent,
    AgentRunEventType,
    AgentRunStatus,
    AgentRunStep,
    AgentRunStepStatus,
    VerificationDecision,
)
from .agent_task_models import AgentTask, AgentTaskStatus
from .approval_integrity import compute_approval_payload_hash
from .models import ApprovalRequest, ApprovalStatus, AuditEvent
from .postgres_trust_event_repository import PostgresTrustEventRepository
from .postgres_trust_repository import (
    PostgresTrustRepository,
    PostgresTrustRepositoryConflict,
    PostgresTrustRepositoryError,
)
from .trust_models import ActionConsumption, CapabilityActionType, ProposedAction
from .trust_models import CapabilityLeaseStatus
from .trust_crypto import sha256_hex
from .trust_crypto import verify_payload_signature
from .trust_policy import lease_receipt_payload


def postgres_branch_trust_enabled() -> bool:
    """Default-off gate for PostgreSQL CREATE_BRANCH human authorization."""
    import os

    return os.getenv("AIXION_POSTGRES_CREATE_BRANCH_TRUST_ENABLED", "false").strip().lower() in {
        "1", "true", "yes",
    }


class BranchIntentStatus(StrEnum):
    INTENT = "INTENT"
    POST_CLAIMED = "POST_CLAIMED"
    REMOTE_VERIFIED = "REMOTE_VERIFIED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    COMPLETED = "COMPLETED"


class AgentRunBranchIntent(BaseModel):
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
    source_branch: str
    target_ref: str
    source_sha: str
    lease_token_hash: str
    initial_lease_token_hash: str
    run_revision: datetime
    step_revision: datetime
    status: BranchIntentStatus = BranchIntentStatus.INTENT
    branch_url: str | None = None
    reason: str = ""
    created_at: datetime
    updated_at: datetime


_COLUMNS = (
    "intent_id,workspace_id,run_id,task_id,approval_id,step_id,action_id,lease_id,"
    "idempotency_key,repository,source_branch,target_ref,source_sha,lease_token_hash,initial_lease_token_hash,"
    "run_revision,step_revision,status,branch_url,reason,created_at,updated_at,payload"
)


def _payload(record: AgentRunBranchIntent) -> dict[str, Any]:
    return record.model_dump(mode="json")


def _decode(row: tuple[Any, ...]) -> AgentRunBranchIntent:
    try:
        record = AgentRunBranchIntent.model_validate(row[22])
    except Exception as error:
        raise PostgresTrustRepositoryError("Stored branch intent payload is invalid") from error
    indexed = (
        record.id,
        record.workspace_id,
        record.run_id,
        record.task_id,
        record.approval_id,
        record.step_id,
        record.action_id,
        record.lease_id,
        record.idempotency_key,
        record.repository,
        record.source_branch,
        record.target_ref,
        record.source_sha,
        record.lease_token_hash,
        record.initial_lease_token_hash,
        record.run_revision,
        record.step_revision,
        record.status.value,
        record.branch_url,
        record.reason,
        record.created_at,
        record.updated_at,
    )
    if indexed != tuple(row[:22]):
        raise PostgresTrustRepositoryError("Branch intent payload differs from indexed state")
    if not re.fullmatch(r"[0-9a-f]{40}", record.source_sha):
        raise PostgresTrustRepositoryError("Stored branch intent source SHA is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", record.lease_token_hash):
        raise PostgresTrustRepositoryError("Stored branch intent lease digest is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", record.initial_lease_token_hash):
        raise PostgresTrustRepositoryError("Stored branch intent initial lease digest is invalid")
    return record


def _validate_branch_authority(
    *,
    task: AgentTask,
    approval: ApprovalRequest,
    lease: Any,
    action: ProposedAction,
    decision: Any,
    intent: AgentRunBranchIntent,
    now: datetime,
    post_claim_reconciliation: bool = False,
) -> None:
    """Revalidate immutable branch authority before claim, observation, or reporting."""
    if (
        task.id != intent.task_id
        or task.workspace_id != intent.workspace_id
        or task.project_id != approval.project_id
        or task.approval_request_id != intent.approval_id
        or task.status != AgentTaskStatus.APPROVED
        or task.repository != intent.repository
        or task.branch_preference != intent.target_ref
        or approval.id != intent.approval_id
        or approval.workspace_id != intent.workspace_id
        or approval.status != ApprovalStatus.APPROVED
        or approval.project_id != task.project_id
        or approval.target_branch != intent.target_ref
        or approval.approved_payload_hash is None
        or approval.approved_payload_hash != compute_approval_payload_hash(approval)
        or (
            not post_claim_reconciliation
            and (lease.status != CapabilityLeaseStatus.ACTIVE or lease.expires_at <= now)
        )
        or not verify_payload_signature(
            lease_receipt_payload(lease), lease.receipt_signature
        )
        or lease.approval_request_id != intent.approval_id
        or lease.approved_payload_hash != approval.approved_payload_hash
        or lease.provider != task.provider
        or lease.agent_id != task.external_agent_id
        or lease.scope.repository != intent.repository
        or lease.scope.branch != intent.target_ref
        or CapabilityActionType.CREATE_BRANCH not in lease.scope.allowed_actions
        or action.action_type != CapabilityActionType.CREATE_BRANCH
        or action.workspace_id != intent.workspace_id
        or action.run_id != intent.run_id
        or action.task_id != intent.task_id
        or action.lease_id != intent.lease_id
        or action.repository != intent.repository
        or action.branch != intent.target_ref
        or action.provider != task.provider
        or action.agent_id != task.external_agent_id
        or action.project_id != task.project_id
        or action.metadata.get("source_sha") != intent.source_sha
        or action.metadata.get("source_branch") != intent.source_branch
        or action.metadata.get("target_ref") != intent.target_ref
        or action.metadata.get("idempotency_key") != intent.idempotency_key
        or action.metadata.get("step_id") != intent.step_id
        or action.metadata.get("task_id") != intent.task_id
        or action.metadata.get("approval_id") != intent.approval_id
        or action.metadata.get("approved_payload_hash") != approval.approved_payload_hash
        or action.metadata.get("lease_id") != intent.lease_id
        or action.metadata.get("repository") != intent.repository
        or decision.proposed_action_id != action.id
        or decision.lease_id != action.lease_id
        or (not post_claim_reconciliation and decision.decision.value != "ALLOW")
    ):
        raise PostgresTrustRepositoryConflict(
            "Branch authorization, task scope, or capability lease changed"
        )


class PostgresAgentRunBranchIntentRepository:
    """Durable intent and reconciliation operations for CREATE_BRANCH only."""

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

    def get_for_step(
        self, *, workspace_id: str, run_id: str, step_id: str, idempotency_key: str
    ) -> AgentRunBranchIntent | None:
        if not workspace_id:
            raise ValueError("Branch intent reads require workspace scope")
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM agent_run_branch_intents "
                "WHERE workspace_id=%s AND run_id=%s AND step_id=%s AND idempotency_key=%s",
                (workspace_id, run_id, step_id, idempotency_key),
            ).fetchone()
        return _decode(row) if row else None

    def get_unconsumed_action_for_step(
        self, *, workspace_id: str, run_id: str, step_id: str, idempotency_key: str
    ) -> ProposedAction | None:
        """Return the sole durable, unconsumed CREATE_BRANCH action for this step.

        The action payload pins the source SHA before a reviewer sees it. A retry
        must reuse that exact payload instead of silently deriving a different
        action from a changed source ref.
        """
        if not workspace_id or not run_id or not step_id or not idempotency_key:
            raise ValueError("Branch action lookup requires complete run and step scope")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT action.action_id FROM proposed_actions AS action "
                "WHERE action.workspace_id=%s AND action.run_id=%s "
                "AND action.payload->>'action_type'='CREATE_BRANCH' "
                "AND action.payload->'metadata'->>'step_id'=%s "
                "AND action.payload->'metadata'->>'step_type'='CREATE_BRANCH' "
                "AND action.payload->'metadata'->>'idempotency_key'=%s "
                "AND NOT EXISTS (SELECT 1 FROM action_consumptions AS consumed "
                "WHERE consumed.workspace_id=action.workspace_id AND consumed.action_id=action.action_id) "
                "ORDER BY action.created_at,action.action_id LIMIT 2",
                (workspace_id, run_id, step_id, idempotency_key),
            ).fetchall()
            if len(rows) > 1:
                raise PostgresTrustRepositoryConflict(
                    "Multiple unconsumed branch actions exist for this durable step"
                )
            if not rows:
                return None
            action = self._trust.get_proposed_action(conn, rows[0][0], workspace_id)
            if action is None:
                raise PostgresTrustRepositoryError("Indexed branch action disappeared")
            if (
                action.run_id != run_id
                or action.workspace_id != workspace_id
                or action.action_type != CapabilityActionType.CREATE_BRANCH
                or action.metadata.get("step_id") != step_id
                or action.metadata.get("step_type") != "CREATE_BRANCH"
                or action.metadata.get("idempotency_key") != idempotency_key
            ):
                raise PostgresTrustRepositoryError("Stored branch action scope is inconsistent")
            decision = self._trust.get_latest_policy_decision_for_action(
                conn, action.id, workspace_id
            )
            if decision is None or decision.proposed_action_id != action.id:
                raise PostgresTrustRepositoryError("Stored branch action has no exact policy decision")
            if decision.decision.value == "BLOCK":
                raise PostgresTrustRepositoryConflict("Stored branch action was denied")
            return action

    def evaluate_action(
        self,
        *,
        action: ProposedAction,
        workspace_id: str,
        run_id: str,
        step_id: str,
        actor: str,
        now: datetime,
        expected_run: AgentRun,
        expected_step: AgentRunStep,
        persist_human_gate: bool = False,
    ):
        with self._connect() as conn:
            with conn.transaction():
                decision, lease = self._trust.evaluate_agent_run_action(
                    conn,
                    action=action,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    step_id=step_id,
                    actor=actor,
                    now=now,
                    trust_event_repository=self._trust_events,
                )
                if decision.decision.value == "REQUIRE_APPROVAL" and persist_human_gate:
                    self._persist_human_gate_in_transaction(
                        conn,
                        action=action,
                        decision=decision,
                        expected_run=expected_run,
                        expected_step=expected_step,
                        actor=actor,
                        now=now,
                    )
                return decision, lease

    def _persist_human_gate_in_transaction(
        self,
        conn: Connection,
        *,
        action: ProposedAction,
        decision: Any,
        expected_run: AgentRun,
        expected_step: AgentRunStep,
        actor: str,
        now: datetime,
    ) -> None:
        """Atomically park the exact run step with its persisted policy decision."""
        from .agent_run_repository import PostgresAgentRunRepository
        from .agent_run_state_machine import transition_run, transition_step

        repository = PostgresAgentRunRepository(self._connect)
        durable_run, durable_steps = repository.get_control_snapshot_in_transaction(
            conn,
            run_id=expected_run.id,
            workspace_id=expected_run.workspace_id or "",
        )
        original_steps = [item.model_copy(deep=True) for item in durable_steps]
        durable_by_id = {item.id: item for item in durable_steps}
        durable_step = durable_by_id.get(expected_step.id)
        if (
            durable_run != expected_run
            or durable_step != expected_step
            or expected_run.current_step_id != expected_step.id
            or expected_step.status != AgentRunStepStatus.RUNNING
            or expected_run.status != AgentRunStatus.RUNNING
            or expected_run.lease_owner != actor
            or not expected_run.lease_token
            or action.run_id != expected_run.id
            or action.task_id != expected_run.task_id
            or action.workspace_id != expected_run.workspace_id
            or action.metadata.get("step_id") != expected_step.id
            or decision.proposed_action_id != action.id
            or decision.decision.value != "REQUIRE_APPROVAL"
        ):
            raise PostgresTrustRepositoryConflict(
                "CREATE_BRANCH human gate no longer matches the active run step"
            )

        reason = "Exact CREATE_BRANCH action is waiting for a reviewer decision."
        _, _ = transition_run(durable_run, AgentRunStatus.NEEDS_HUMAN, reason=reason)
        durable_run.lease_owner = None
        durable_run.lease_token = None
        durable_run.lease_expires_at = None
        durable_run.heartbeat_at = None
        _, _ = transition_step(
            durable_step, AgentRunStepStatus.NEEDS_HUMAN, reason=reason
        )
        durable_step.lease_owner = None
        durable_step.lease_token = None
        durable_step.lease_expires_at = None
        durable_step.heartbeat_at = None
        durable_step.decision = VerificationDecision.NEEDS_HUMAN
        durable_step.reason = reason
        durable_step.evidence = {
            **durable_step.evidence,
            "pending_action": {
                "action_id": action.id,
                "action_type": action.action_type.value,
                "payload_sha256": sha256_hex(action.model_dump(mode="json")),
                "policy_decision_id": decision.id,
                "provider": action.provider.value,
                "agent_id": action.agent_id,
                "repository": action.repository,
                "branch": action.branch,
                "source_sha": action.metadata.get("source_sha"),
                "source_branch": action.metadata.get("source_branch"),
                "target_ref": action.metadata.get("target_ref"),
                "approval_id": action.metadata.get("approval_id"),
                "approved_payload_hash": action.metadata.get("approved_payload_hash"),
                "lease_id": action.lease_id,
                "step_id": expected_step.id,
                "idempotency_key": expected_step.idempotency_key,
            },
        }

        event = AgentRunEvent(
            run_id=durable_run.id,
            task_id=durable_run.task_id,
            step_id=durable_step.id,
            correlation_id=durable_run.correlation_id,
            event_type=AgentRunEventType.STEP_NEEDS_HUMAN,
            previous_status=AgentRunStepStatus.RUNNING.value,
            new_status=AgentRunStepStatus.NEEDS_HUMAN.value,
            message="CREATE_BRANCH is paused for an exact reviewer decision.",
            reason=reason,
            actor=actor,
            attempt_number=durable_step.attempt_count,
            metadata={
                "action_id": action.id,
                "action_payload_sha256": sha256_hex(action.model_dump(mode="json")),
                "policy_decision_id": decision.id,
                "run_previous_status": AgentRunStatus.RUNNING.value,
                "run_new_status": AgentRunStatus.NEEDS_HUMAN.value,
                "step_idempotency_key": durable_step.idempotency_key,
            },
            created_at=now,
        )
        audit = AuditEvent(
            event_type="agent_run.action_requires_approval",
            entity_id=durable_run.id,
            workspace_id=durable_run.workspace_id,
            actor=actor,
            details={
                "action_id": action.id,
                "action_payload_sha256": sha256_hex(action.model_dump(mode="json")),
                "policy_decision_id": decision.id,
                "step_id": durable_step.id,
                "idempotency_key": durable_step.idempotency_key,
            },
            created_at=now,
        )
        updated_steps = [
            durable_step if item.id == durable_step.id else item
            for item in durable_steps
        ]
        repository.commit_control_transition_in_transaction(
            conn,
            expected_run=expected_run,
            updated_run=durable_run,
            expected_steps=original_steps,
            updated_steps=updated_steps,
            events=[event],
            audits=[audit],
        )

    def begin_intent(
        self,
        *,
        record: AgentRunBranchIntent,
        expected_run: AgentRun,
        expected_step: AgentRunStep,
        timeout_seconds: int,
        actor: str,
        now: datetime,
    ) -> tuple[AgentRunBranchIntent, AgentRun | None, list[AgentRunStep], list[AgentRunEvent], AuditEvent | None]:
        """Reserve the full bounded runtime budget before any remote side effect.

        The reservation is charged as consumed runtime and is deliberately not
        refunded after a successful or failed request; this avoids retrying
        ambiguous remote work against unreserved capability budget.
        """
        if (
            not record.workspace_id
            or expected_run.workspace_id != record.workspace_id
            or expected_run.id != record.run_id
            or expected_step.run_id != record.run_id
            or expected_step.id != record.step_id
            or expected_step.step_type.value != "CREATE_BRANCH"
            or expected_step.idempotency_key != record.idempotency_key
            or not re.fullmatch(r"[0-9a-f]{40}", record.source_sha)
            or not re.fullmatch(r"[0-9a-f]{64}", record.lease_token_hash)
            or not re.fullmatch(r"[0-9a-f]{64}", record.initial_lease_token_hash)
            or not record.repository
            or not record.source_branch
            or not record.target_ref
            or not expected_run.lease_token
            or sha256_hex({"lease_token": expected_run.lease_token}) != record.lease_token_hash
            or expected_run.lease_owner is None
            or expected_run.lease_expires_at is None
            or not 1 <= timeout_seconds <= 3600
        ):
            raise PostgresTrustRepositoryConflict("Branch intent expected run or step is invalid")

        with self._connect() as conn:
            with conn.transaction():
                approval_row = conn.execute(
                    "SELECT approval_id,project_id,workspace_id,status,payload "
                    "FROM approval_requests WHERE approval_id=%s AND workspace_id=%s FOR SHARE",
                    (record.approval_id, record.workspace_id),
                ).fetchone()
                task_row = conn.execute(
                    "SELECT task_id,project_id,workspace_id,approval_request_id,status,payload "
                    "FROM agent_tasks WHERE task_id=%s AND workspace_id=%s FOR SHARE",
                    (record.task_id, record.workspace_id),
                ).fetchone()
                if approval_row is None or task_row is None:
                    raise PostgresTrustRepositoryConflict("Branch intent task or approval is missing")
                try:
                    approval = ApprovalRequest.model_validate(approval_row[4])
                    task = AgentTask.model_validate(task_row[5])
                except Exception as error:
                    raise PostgresTrustRepositoryError("Stored branch task authority is invalid") from error
                if (
                    approval.id != approval_row[0]
                    or approval.project_id != approval_row[1]
                    or approval.workspace_id != approval_row[2]
                    or approval.status.value != approval_row[3]
                    or task.id != task_row[0]
                    or task.project_id != task_row[1]
                    or task.workspace_id != task_row[2]
                    or task.approval_request_id != task_row[3]
                    or task.status.value != task_row[4]
                ):
                    raise PostgresTrustRepositoryError("Branch task authority index/payload mismatch")
                run_row = conn.execute(
                    "SELECT run_id,workspace_id,task_id,project_id,status,current_step_id,updated_at,payload "
                    "FROM agent_runs WHERE run_id=%s AND workspace_id=%s FOR UPDATE",
                    (record.run_id, record.workspace_id),
                ).fetchone()
                if run_row is None:
                    raise PostgresTrustRepositoryConflict("AgentRun is not visible in this workspace")
                try:
                    durable_run = AgentRun.model_validate(run_row[7])
                except Exception as error:
                    raise PostgresTrustRepositoryError("Stored AgentRun payload is invalid") from error
                if (
                    durable_run != expected_run
                    or durable_run.id != run_row[0]
                    or durable_run.workspace_id != run_row[1]
                    or durable_run.task_id != run_row[2]
                    or durable_run.project_id != run_row[3]
                    or durable_run.status.value != run_row[4]
                    or durable_run.current_step_id != run_row[5]
                    or durable_run.updated_at != run_row[6]
                    or durable_run.current_step_id != record.step_id
                    or not durable_run.lease_token
                    or sha256_hex({"lease_token": durable_run.lease_token}) != record.lease_token_hash
                    or record.initial_lease_token_hash != record.lease_token_hash
                    or record.run_revision != durable_run.updated_at
                    or durable_run.lease_owner != actor
                    or durable_run.status.value != "RUNNING"
                    or durable_run.lease_expires_at <= conn.execute(
                        "SELECT clock_timestamp()"
                    ).fetchone()[0]
                    or durable_run.capability_lease_id != record.lease_id
                ):
                    raise PostgresTrustRepositoryConflict("Run lease or revision changed before branch intent")

                step_row = conn.execute(
                    "SELECT step_id,run_id,task_id,workspace_id,sequence,status,updated_at,payload "
                    "FROM agent_run_steps WHERE step_id=%s AND workspace_id=%s FOR UPDATE",
                    (record.step_id, record.workspace_id),
                ).fetchone()
                if step_row is None:
                    raise PostgresTrustRepositoryConflict("AgentRun step is not visible in this workspace")
                try:
                    durable_step = AgentRunStep.model_validate(step_row[7])
                except Exception as error:
                    raise PostgresTrustRepositoryError("Stored AgentRun step payload is invalid") from error
                if (
                    durable_step != expected_step
                    or durable_step.id != step_row[0]
                    or durable_step.run_id != step_row[1]
                    or durable_step.task_id != step_row[2]
                    or step_row[1] != record.run_id
                    or step_row[2] != record.task_id
                    or step_row[3] != record.workspace_id
                    or durable_step.status.value != step_row[5]
                    or durable_step.updated_at != step_row[6]
                    or record.step_revision != durable_step.updated_at
                    or durable_step.status != AgentRunStepStatus.RUNNING
                    or not durable_step.lease_token
                    or sha256_hex({"lease_token": durable_step.lease_token})
                    != record.lease_token_hash
                ):
                    raise PostgresTrustRepositoryConflict("Run step lease or revision changed before intent")

                existing_row = conn.execute(
                    f"SELECT {_COLUMNS} FROM agent_run_branch_intents "
                    "WHERE workspace_id=%s AND run_id=%s AND step_id=%s AND idempotency_key=%s FOR UPDATE",
                    (record.workspace_id, record.run_id, record.step_id, record.idempotency_key),
                ).fetchone()
                if existing_row is not None:
                    existing = _decode(existing_row)
                    if any(
                        getattr(existing, name) != getattr(record, name)
                        for name in (
                            "workspace_id", "run_id", "task_id", "approval_id", "step_id",
                            "action_id", "lease_id", "idempotency_key", "repository",
                            "source_branch", "target_ref", "source_sha",
                        )
                    ):
                        raise PostgresTrustRepositoryConflict(
                            "Idempotent branch intent is bound to different approved content"
                        )
                    return existing, None, [], [], None

                action = self._trust.get_proposed_action(
                    conn, record.action_id, record.workspace_id, for_update=True
                )
                if action is None:
                    raise PostgresTrustRepositoryConflict("Branch intent has no persisted action")
                decision = self._trust.get_latest_policy_decision_for_action(
                    conn, record.action_id, record.workspace_id, for_update=True
                )
                if decision is None or decision.decision.value != "ALLOW":
                    raise PostgresTrustRepositoryConflict("Branch intent requires a persisted ALLOW decision")
                if (
                    action.action_type.value != "CREATE_BRANCH"
                    or action.workspace_id != record.workspace_id
                    or action.run_id != record.run_id
                    or action.task_id != record.task_id
                    or action.lease_id != record.lease_id
                    or action.repository != record.repository
                    or action.branch != record.target_ref
                    or action.metadata.get("source_sha") != record.source_sha
                    or action.metadata.get("source_branch") != record.source_branch
                    or action.metadata.get("target_ref") != record.target_ref
                    or action.metadata.get("idempotency_key") != record.idempotency_key
                    or action.metadata.get("step_id") != record.step_id
                    or action.metadata.get("task_id") != record.task_id
                    or action.metadata.get("approval_id") != record.approval_id
                    or action.metadata.get("approved_payload_hash") != approval.approved_payload_hash
                    or action.metadata.get("lease_id") != record.lease_id
                    or action.metadata.get("repository") != record.repository
                ):
                    raise PostgresTrustRepositoryConflict("Branch intent differs from persisted approved action")
                if (
                    task.id != task_row[0] or task.workspace_id != task_row[2]
                    or task.project_id != task_row[1]
                    or task.project_id != durable_run.project_id
                    or task.approval_request_id != task_row[3]
                    or task.approval_request_id != record.approval_id
                    or task.status != AgentTaskStatus.APPROVED or task_row[4] != "APPROVED"
                    or approval.id != approval_row[0] or approval.project_id != approval_row[1]
                    or approval.project_id != durable_run.project_id
                    or approval.workspace_id != approval_row[2]
                    or approval.workspace_id != record.workspace_id
                    or approval.status != ApprovalStatus.APPROVED or approval_row[3] != "APPROVED"
                    or approval.approved_payload_hash is None
                    or approval.approved_payload_hash != compute_approval_payload_hash(approval)
                    or task.repository != record.repository
                    or action.provider != task.provider
                    or action.agent_id != task.external_agent_id
                    or action.project_id != task.project_id
                    or approval.project_id != task.project_id
                ):
                    raise PostgresTrustRepositoryConflict("Branch intent authority was revoked or changed")
                durable_lease = self._trust.get_capability_lease(
                    conn, record.lease_id, record.workspace_id, for_update=True
                )
                if durable_lease is None:
                    raise PostgresTrustRepositoryConflict("Branch intent capability lease is missing")
                _validate_branch_authority(
                    task=task, approval=approval, lease=durable_lease, action=action,
                    decision=decision, intent=record,
                    now=conn.execute("SELECT clock_timestamp()").fetchone()[0],
                )
                consumption = ActionConsumption(
                    actual_runtime_seconds=timeout_seconds,
                    retry_consumed=durable_step.attempt_count > 1,
                    evidence={
                        "reservation": True,
                        "run_id": record.run_id,
                        "step_id": record.step_id,
                        "idempotency_key": record.idempotency_key,
                        "source_sha": record.source_sha,
                        "target_ref": record.target_ref,
                        "runtime_seconds_reserved": timeout_seconds,
                    },
                )
                self._trust.consume_agent_run_action(
                    conn,
                    action_id=record.action_id,
                    workspace_id=record.workspace_id,
                    run_id=record.run_id,
                    step_id=record.step_id,
                    consumption=consumption,
                    actor=actor,
                    now=now,
                )
                conn.execute(
                    f"INSERT INTO agent_run_branch_intents({_COLUMNS.replace(',payload', '')},payload) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        record.id, record.workspace_id, record.run_id, record.task_id,
                        record.approval_id, record.step_id, record.action_id, record.lease_id,
                        record.idempotency_key, record.repository, record.source_branch,
                        record.target_ref, record.source_sha, record.lease_token_hash,
                        record.initial_lease_token_hash,
                        record.run_revision, record.step_revision, record.status.value,
                        record.branch_url, record.reason, record.created_at, record.updated_at,
                        Jsonb(_payload(record)),
                    ),
                )
                event = AgentRunEvent(
                    run_id=record.run_id,
                    task_id=record.task_id,
                    step_id=record.step_id,
                    correlation_id=durable_run.correlation_id,
                    event_type=AgentRunEventType.OPERATOR_ACTION,
                    actor=actor,
                    attempt_number=durable_step.attempt_count,
                    message="Durable branch intent recorded before GitHub mutation.",
                    metadata={
                        "intent_id": record.id,
                        "action_id": record.action_id,
                        "idempotency_key": record.idempotency_key,
                        "repository": record.repository,
                        "source_branch": record.source_branch,
                        "target_ref": record.target_ref,
                        "source_sha": record.source_sha,
                        "capability_consumed": True,
                    },
                )
                audit = AuditEvent(
                    event_type="agent_run.branch_intent_recorded",
                    entity_id=record.run_id,
                    workspace_id=record.workspace_id,
                    actor=actor,
                    details={"intent_id": record.id, "action_id": record.action_id,
                             "step_id": record.step_id, "source_sha": record.source_sha,
                             "target_ref": record.target_ref},
                    created_at=now,
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
                    (audit.id, record.workspace_id, record.run_id, audit.event_type, actor,
                     Jsonb(audit.details), audit.created_at, Jsonb(audit.model_dump(mode="json"))),
                )
                return record, durable_run, [durable_step], [event], audit

    def rebind_intent_lease(
        self,
        *,
        intent: AgentRunBranchIntent,
        expected_run: AgentRun,
        expected_step: AgentRunStep,
        worker_lease_token: str,
        actor: str,
        now: datetime,
    ) -> AgentRunBranchIntent:
        """Rebind an unclaimed INTENT after a worker lease is reacquired.

        This only updates durable lease/revision fences. It never calls a
        provider and cannot rebind an intent after its one-shot POST claim.
        """
        return self._rebind_worker_lease(
            intent=intent,
            expected_run=expected_run,
            expected_step=expected_step,
            worker_lease_token=worker_lease_token,
            actor=actor,
            now=now,
            allowed_statuses={BranchIntentStatus.INTENT},
            require_live_authority=True,
            event_message="Branch intent rebound to the current worker lease before POST.",
        )

    def rebind_for_reconciliation(
        self,
        *,
        intent: AgentRunBranchIntent,
        expected_run: AgentRun,
        expected_step: AgentRunStep,
        worker_lease_token: str,
        actor: str,
        now: datetime,
    ) -> AgentRunBranchIntent:
        """Acquire GET-only recovery ownership after an irreversible POST claim.

        The stored status remains POST_CLAIMED, REMOTE_VERIFIED, or
        RECONCILIATION_REQUIRED.
        Neither this method nor its result can authorize another provider POST.
        """
        return self._rebind_worker_lease(
            intent=intent,
            expected_run=expected_run,
            expected_step=expected_step,
            worker_lease_token=worker_lease_token,
            actor=actor,
            now=now,
            allowed_statuses={
                BranchIntentStatus.POST_CLAIMED,
                BranchIntentStatus.REMOTE_VERIFIED,
                BranchIntentStatus.RECONCILIATION_REQUIRED,
            },
            require_live_authority=False,
            event_message="GET-only branch reconciliation rebound to the current worker lease.",
        )

    def _rebind_worker_lease(
        self,
        *,
        intent: AgentRunBranchIntent,
        expected_run: AgentRun,
        expected_step: AgentRunStep,
        worker_lease_token: str,
        actor: str,
        now: datetime,
        allowed_statuses: set[BranchIntentStatus],
        require_live_authority: bool,
        event_message: str,
    ) -> AgentRunBranchIntent:
        if not worker_lease_token or not actor:
            raise ValueError("Branch intent recovery requires a worker lease and actor")
        candidate_hash = sha256_hex({"lease_token": worker_lease_token})
        if (
            expected_run.workspace_id != intent.workspace_id
            or expected_run.id != intent.run_id
            or expected_step.run_id != intent.run_id
            or expected_step.id != intent.step_id
            or expected_step.task_id != intent.task_id
            or not intent.workspace_id
        ):
            raise PostgresTrustRepositoryConflict("Branch recovery scope is invalid")

        with self._connect() as conn:
            with conn.transaction():
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
                    f"SELECT {_COLUMNS} FROM agent_run_branch_intents "
                    "WHERE intent_id=%s AND workspace_id=%s FOR UPDATE",
                    (intent.id, intent.workspace_id),
                ).fetchone()
                if None in (approval_row, task_row, run_row, step_row, intent_row):
                    raise PostgresTrustRepositoryConflict("Branch recovery scope is missing")
                try:
                    approval = ApprovalRequest.model_validate(approval_row[4])
                    task = AgentTask.model_validate(task_row[5])
                    run = AgentRun.model_validate(run_row[7])
                    step = AgentRunStep.model_validate(step_row[6])
                except Exception as error:
                    raise PostgresTrustRepositoryError("Stored branch recovery scope is invalid") from error
                current = _decode(intent_row)
                db_now = conn.execute("SELECT clock_timestamp()").fetchone()[0]
                if (
                    current.id != intent.id
                    or current.workspace_id != intent.workspace_id
                    or current.run_id != intent.run_id
                    or current.task_id != intent.task_id
                    or current.approval_id != intent.approval_id
                    or current.step_id != intent.step_id
                    or current.action_id != intent.action_id
                    or current.lease_id != intent.lease_id
                    or current.idempotency_key != intent.idempotency_key
                    or current.repository != intent.repository
                    or current.source_branch != intent.source_branch
                    or current.target_ref != intent.target_ref
                    or current.source_sha != intent.source_sha
                    or current.initial_lease_token_hash != intent.initial_lease_token_hash
                    or current.status not in allowed_statuses
                    or run != expected_run
                    or step != expected_step
                    or run.id != run_row[0]
                    or run.workspace_id != run_row[1]
                    or run.task_id != run_row[2]
                    or run.project_id != run_row[3]
                    or run.status.value != run_row[4]
                    or run.current_step_id != run_row[5]
                    or run.updated_at != run_row[6]
                    or step.id != step_row[0]
                    or step.run_id != step_row[1]
                    or step.task_id != step_row[2]
                    or step.status.value != step_row[4]
                    or step.updated_at != step_row[5]
                    or run.current_step_id != step.id
                    or run.status.value != "RUNNING"
                    or run.lease_owner != actor
                    or run.lease_token != worker_lease_token
                    or run.lease_expires_at is None
                    or run.lease_expires_at <= db_now
                    or run.capability_lease_id != current.lease_id
                    or run.approval_request_id != current.approval_id
                    or step.status != AgentRunStepStatus.RUNNING
                    or step.lease_owner != actor
                    or step.lease_token != worker_lease_token
                    or step.lease_expires_at is None
                    or step.lease_expires_at <= db_now
                    or run.pause_requested
                    or run.cancel_requested
                    or task.id != task_row[0]
                    or task.project_id != task_row[1]
                    or task.workspace_id != task_row[2]
                    or task.approval_request_id != task_row[3]
                    or task.status.value != task_row[4]
                    or approval.id != approval_row[0]
                    or approval.project_id != approval_row[1]
                    or approval.workspace_id != approval_row[2]
                    or approval.status.value != approval_row[3]
                    or task.workspace_id != current.workspace_id
                    or task.id != current.task_id
                    or task.approval_request_id != current.approval_id
                    or task.project_id != run.project_id
                    or task.repository != current.repository
                    or task.branch_preference != current.target_ref
                    or approval.id != current.approval_id
                    or approval.workspace_id != current.workspace_id
                    or approval.project_id != run.project_id
                    or approval.target_branch != current.target_ref
                ):
                    raise PostgresTrustRepositoryConflict(
                        "Branch intent recovery lost workspace, authority, or worker-lease CAS"
                    )

                action = self._trust.get_proposed_action(
                    conn, current.action_id, current.workspace_id, for_update=True
                )
                decision = self._trust.get_latest_policy_decision_for_action(
                    conn, current.action_id, current.workspace_id, for_update=True
                )
                capability_lease = self._trust.get_capability_lease(
                    conn, current.lease_id, current.workspace_id, for_update=True
                )
                if action is None or decision is None or capability_lease is None:
                    raise PostgresTrustRepositoryConflict("Branch recovery action authority is missing")
                if (
                    decision.proposed_action_id != current.action_id
                    or decision.lease_id != current.lease_id
                    or action.workspace_id != current.workspace_id
                    or action.run_id != current.run_id
                    or action.task_id != current.task_id
                ):
                    raise PostgresTrustRepositoryConflict("Branch recovery decision/action scope changed")
                if require_live_authority:
                    _validate_branch_authority(
                        task=task, approval=approval, lease=capability_lease,
                        action=action, decision=decision, intent=current, now=db_now,
                    )
                elif (
                    action.action_type != CapabilityActionType.CREATE_BRANCH
                    or action.workspace_id != current.workspace_id
                    or action.run_id != current.run_id
                    or action.task_id != current.task_id
                    or action.lease_id != current.lease_id
                    or action.repository != current.repository
                    or action.branch != current.target_ref
                    or action.metadata.get("source_sha") != current.source_sha
                    or action.metadata.get("source_branch") != current.source_branch
                    or action.metadata.get("target_ref") != current.target_ref
                    or action.metadata.get("idempotency_key") != current.idempotency_key
                    or action.metadata.get("step_id") != current.step_id
                    or action.metadata.get("task_id") != current.task_id
                    or action.metadata.get("approval_id") != current.approval_id
                    or action.metadata.get("lease_id") != current.lease_id
                    or action.metadata.get("repository") != current.repository
                    or action.provider != task.provider
                    or action.agent_id != task.external_agent_id
                    or action.project_id != task.project_id
                    or task.status != AgentTaskStatus.APPROVED
                    or task.project_id != run.project_id
                    or task.repository != current.repository
                    or approval.status != ApprovalStatus.APPROVED
                    or approval.project_id != task.project_id
                    or approval.target_branch != current.target_ref
                    or approval.approved_payload_hash is None
                    or approval.approved_payload_hash != compute_approval_payload_hash(approval)
                    or capability_lease.approval_request_id != current.approval_id
                    or capability_lease.workspace_id != current.workspace_id
                    or capability_lease.approved_payload_hash != approval.approved_payload_hash
                    or capability_lease.provider != task.provider
                    or capability_lease.agent_id != task.external_agent_id
                    or capability_lease.scope.repository != current.repository
                    or capability_lease.scope.branch != current.target_ref
                    or CapabilityActionType.CREATE_BRANCH not in capability_lease.scope.allowed_actions
                    or not verify_payload_signature(
                        lease_receipt_payload(capability_lease),
                        capability_lease.receipt_signature,
                    )
                ):
                    raise PostgresTrustRepositoryConflict("Branch reconciliation action changed")

                # Idempotent retry by the worker that already owns this exact
                # recovery lease. It still cannot transition into INTENT.
                if (
                    current.lease_token_hash == candidate_hash
                    and current.run_revision == run.updated_at
                    and current.step_revision == step.updated_at
                ):
                    return current

                if current == intent:
                    expected_hash = intent.lease_token_hash
                    expected_run_revision = intent.run_revision
                    expected_step_revision = intent.step_revision
                else:
                    # A stale snapshot is accepted only when immutable intent
                    # identity matched above; row locking and the latest
                    # durable run/step lease fence the new owner.
                    expected_hash = current.lease_token_hash
                    expected_run_revision = current.run_revision
                    expected_step_revision = current.step_revision
                updated = current.model_copy(update={
                    "lease_token_hash": candidate_hash,
                    "run_revision": run.updated_at,
                    "step_revision": step.updated_at,
                    "updated_at": now,
                })
                changed = conn.execute(
                    "UPDATE agent_run_branch_intents SET lease_token_hash=%s,run_revision=%s,"
                    "step_revision=%s,updated_at=%s,payload=%s WHERE intent_id=%s "
                    "AND workspace_id=%s AND run_id=%s AND step_id=%s AND approval_id=%s "
                    "AND action_id=%s AND idempotency_key=%s AND source_sha=%s "
                    "AND status=%s AND lease_token_hash=%s AND run_revision=%s AND step_revision=%s",
                    (
                        updated.lease_token_hash, updated.run_revision, updated.step_revision,
                        updated.updated_at, Jsonb(_payload(updated)), updated.id,
                        updated.workspace_id, updated.run_id, updated.step_id,
                        updated.approval_id, updated.action_id, updated.idempotency_key,
                        updated.source_sha, current.status.value, expected_hash,
                        expected_run_revision, expected_step_revision,
                    ),
                )
                if changed.rowcount != 1:
                    raise PostgresTrustRepositoryConflict("Branch recovery lease rebind lost its CAS")
                event = AgentRunEvent(
                    run_id=updated.run_id,
                    task_id=updated.task_id,
                    step_id=updated.step_id,
                    correlation_id=run.correlation_id,
                    event_type=AgentRunEventType.OPERATOR_ACTION,
                    actor=actor,
                    attempt_number=step.attempt_count,
                    message=event_message,
                    metadata={
                        "intent_id": updated.id,
                        "action_id": updated.action_id,
                        "status": updated.status.value,
                        "idempotency_key": updated.idempotency_key,
                    },
                    created_at=now,
                )
                audit = AuditEvent(
                    event_type="agent_run.branch_intent_rebound",
                    entity_id=updated.run_id,
                    workspace_id=updated.workspace_id,
                    actor=actor,
                    details={
                        "intent_id": updated.id,
                        "action_id": updated.action_id,
                        "step_id": updated.step_id,
                        "status": updated.status.value,
                    },
                    created_at=now,
                )
                conn.execute(
                    "INSERT INTO agent_run_events(event_id,run_id,task_id,workspace_id,created_at,event_type,payload) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    (event.id, event.run_id, event.task_id, updated.workspace_id,
                     event.created_at, event.event_type.value, Jsonb(event.model_dump(mode="json"))),
                )
                conn.execute(
                    "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,event_type,actor,details,created_at,payload) "
                    "VALUES(%s,%s,NULL,'agent_run',%s,%s,%s,%s,%s,%s)",
                    (audit.id, updated.workspace_id, updated.run_id, audit.event_type, actor,
                     Jsonb(audit.details), audit.created_at, Jsonb(audit.model_dump(mode="json"))),
                )
                return updated

    def record_remote_observation(
        self,
        *,
        intent: AgentRunBranchIntent,
        expected_run: AgentRun,
        expected_step: AgentRunStep,
        worker_lease_token: str,
        verified_sha: str | None,
        branch_url: str | None,
        reason: str,
        now: datetime,
    ) -> AgentRunBranchIntent:
        """Persist a GET result; this method never repeats a remote create."""
        if verified_sha is not None and not re.fullmatch(r"[0-9a-f]{40}", verified_sha):
            raise ValueError("Observed branch SHA is malformed")
        if not worker_lease_token:
            raise ValueError("Branch observation requires the active worker lease token")
        with self._connect() as conn:
            with conn.transaction():
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
                if approval_row is None or task_row is None:
                    raise PostgresTrustRepositoryConflict("Branch reconciliation authority is missing")
                run_row = conn.execute(
                    "SELECT run_id,workspace_id,task_id,project_id,status,current_step_id,updated_at,payload FROM agent_runs "
                    "WHERE run_id=%s AND workspace_id=%s FOR UPDATE",
                    (intent.run_id, intent.workspace_id),
                ).fetchone()
                step_row = conn.execute(
                    "SELECT step_id,run_id,task_id,workspace_id,status,updated_at,payload FROM agent_run_steps "
                    "WHERE step_id=%s AND run_id=%s AND workspace_id=%s FOR UPDATE",
                    (intent.step_id, intent.run_id, intent.workspace_id),
                ).fetchone()
                intent_row = conn.execute(
                    f"SELECT {_COLUMNS} FROM agent_run_branch_intents "
                    "WHERE intent_id=%s AND workspace_id=%s FOR UPDATE",
                    (intent.id, intent.workspace_id),
                ).fetchone()
                if run_row is None or step_row is None or intent_row is None:
                    raise PostgresTrustRepositoryConflict("Branch reconciliation scope is missing")
                try:
                    durable_run = AgentRun.model_validate(run_row[7])
                    durable_step = AgentRunStep.model_validate(step_row[6])
                except Exception as error:
                    raise PostgresTrustRepositoryError("Stored reconciliation scope is invalid") from error
                current = _decode(intent_row)
                checked_at = conn.execute("SELECT clock_timestamp()").fetchone()[0]
                if (
                    current != intent
                    or current.id != intent.id
                    or current.workspace_id != intent.workspace_id
                    or current.run_id != intent.run_id
                    or current.step_id != intent.step_id
                    or current.idempotency_key != intent.idempotency_key
                    or current.source_sha != intent.source_sha
                    or durable_run.id != run_row[0]
                    or durable_run.workspace_id != run_row[1]
                    or durable_run.task_id != run_row[2]
                    or durable_run.project_id != run_row[3]
                    or durable_run.status.value != run_row[4]
                    or durable_run.current_step_id != run_row[5]
                    or durable_run.updated_at != run_row[6]
                    or durable_step.id != step_row[0]
                    or durable_step.run_id != step_row[1]
                    or durable_step.task_id != step_row[2]
                    or durable_step.status.value != step_row[4]
                    or durable_step.updated_at != step_row[5]
                    or durable_run != expected_run
                    or durable_step != expected_step
                    or not durable_run.lease_token
                    or sha256_hex({"lease_token": durable_run.lease_token})
                    != sha256_hex({"lease_token": worker_lease_token})
                    or durable_run.lease_owner is None
                    or durable_run.lease_expires_at is None
                    or durable_run.lease_expires_at <= checked_at
                    or durable_step.lease_token != durable_run.lease_token
                    or durable_step.status != AgentRunStepStatus.RUNNING
                    or durable_run.current_step_id != durable_step.id
                    or durable_run.pause_requested
                    or durable_run.cancel_requested
                ):
                    raise PostgresTrustRepositoryConflict(
                        "Branch remote observation has a stale run, step, or lease revision"
                    )
                action = self._trust.get_proposed_action(
                    conn, current.action_id, current.workspace_id, for_update=True
                )
                decision = self._trust.get_latest_policy_decision_for_action(
                    conn, current.action_id, current.workspace_id, for_update=True
                )
                lease = self._trust.get_capability_lease(
                    conn, current.lease_id, current.workspace_id, for_update=True
                )
                if action is None or decision is None or lease is None:
                    raise PostgresTrustRepositoryConflict("Branch reconciliation authority is missing")
                task = AgentTask.model_validate(task_row[5])
                approval = ApprovalRequest.model_validate(approval_row[4])
                if (
                    task.id != task_row[0]
                    or task.project_id != task_row[1]
                    or task.workspace_id != task_row[2]
                    or task.approval_request_id != task_row[3]
                    or task.status.value != task_row[4]
                    or approval.id != approval_row[0]
                    or approval.project_id != approval_row[1]
                    or approval.workspace_id != approval_row[2]
                    or approval.status.value != approval_row[3]
                ):
                    raise PostgresTrustRepositoryError("Branch authority indexed state differs from payload")
                _validate_branch_authority(
                    task=task, approval=approval, lease=lease, action=action,
                    decision=decision, intent=current, now=checked_at,
                    post_claim_reconciliation=current.status in {
                        BranchIntentStatus.POST_CLAIMED,
                        BranchIntentStatus.REMOTE_VERIFIED,
                        BranchIntentStatus.RECONCILIATION_REQUIRED,
                    },
                )
                if (
                    task.id != current.task_id or task.workspace_id != current.workspace_id
                    or task.project_id != task_row[1]
                    or task.project_id != durable_run.project_id
                    or task.approval_request_id != task_row[3]
                    or task.approval_request_id != current.approval_id
                    or task.status != AgentTaskStatus.APPROVED or task_row[4] != "APPROVED"
                    or approval.id != current.approval_id or approval.project_id != approval_row[1]
                    or approval.project_id != durable_run.project_id
                    or approval.workspace_id != approval_row[2]
                    or approval.workspace_id != current.workspace_id
                    or approval.status != ApprovalStatus.APPROVED or approval_row[3] != "APPROVED"
                    or approval.approved_payload_hash is None
                    or approval.approved_payload_hash != compute_approval_payload_hash(approval)
                    or (
                        current.status not in {
                            BranchIntentStatus.POST_CLAIMED,
                            BranchIntentStatus.REMOTE_VERIFIED,
                            BranchIntentStatus.RECONCILIATION_REQUIRED,
                        }
                        and (lease.status != CapabilityLeaseStatus.ACTIVE or lease.expires_at <= checked_at)
                    )
                    or lease.approval_request_id != current.approval_id
                    or lease.approved_payload_hash != approval.approved_payload_hash
                    or action.action_type.value != "CREATE_BRANCH"
                    or action.workspace_id != current.workspace_id
                    or action.run_id != current.run_id
                    or action.task_id != current.task_id
                    or action.lease_id != current.lease_id
                    or action.repository != current.repository
                    or action.branch != current.target_ref
                    or action.metadata.get("source_sha") != current.source_sha
                    or action.metadata.get("source_branch") != current.source_branch
                    or action.metadata.get("target_ref") != current.target_ref
                    or action.metadata.get("idempotency_key") != current.idempotency_key
                    or action.metadata.get("step_id") != current.step_id
                    or action.metadata.get("task_id") != current.task_id
                    or action.metadata.get("approval_id") != current.approval_id
                    or action.metadata.get("lease_id") != current.lease_id
                    or action.metadata.get("repository") != current.repository
                    or action.provider != task.provider
                    or action.agent_id != task.external_agent_id
                    or action.project_id != task.project_id
                    or decision.decision.value != "ALLOW"
                ):
                    raise PostgresTrustRepositoryConflict("Branch reconciliation authority was revoked or changed")
                status = (
                    BranchIntentStatus.REMOTE_VERIFIED
                    if verified_sha == current.source_sha
                    else BranchIntentStatus.RECONCILIATION_REQUIRED
                )
                if current.status == BranchIntentStatus.COMPLETED:
                    raise PostgresTrustRepositoryConflict("Completed branch intent is immutable")
                detail = reason if status == BranchIntentStatus.RECONCILIATION_REQUIRED else ""
                updated = current.model_copy(
                    update={
                        "status": status,
                        "branch_url": branch_url or current.branch_url,
                        "reason": detail,
                        "lease_token_hash": sha256_hex({"lease_token": worker_lease_token}),
                        "run_revision": durable_run.updated_at,
                        "step_revision": durable_step.updated_at,
                        "updated_at": now,
                    }
                )
                changed = conn.execute(
                    "UPDATE agent_run_branch_intents SET lease_token_hash=%s,run_revision=%s,"
                    "step_revision=%s,status=%s,branch_url=%s,reason=%s,updated_at=%s,payload=%s "
                    "WHERE intent_id=%s AND workspace_id=%s AND run_id=%s AND step_id=%s "
                    "AND idempotency_key=%s AND source_sha=%s AND lease_token_hash=%s "
                    "AND run_revision=%s AND step_revision=%s AND status<>'COMPLETED'",
                    (
                        updated.lease_token_hash, updated.run_revision, updated.step_revision,
                        updated.status.value, updated.branch_url, updated.reason,
                        updated.updated_at, Jsonb(_payload(updated)), updated.id,
                        updated.workspace_id, updated.run_id, updated.step_id,
                        updated.idempotency_key, updated.source_sha, current.lease_token_hash,
                        current.run_revision, current.step_revision,
                    ),
                )
                if changed.rowcount != 1:
                    raise PostgresTrustRepositoryConflict(
                        "Branch intent changed during remote reconciliation"
                    )
                return updated

    def claim_create_once(
        self,
        *,
        intent: AgentRunBranchIntent,
        expected_run: AgentRun,
        expected_step: AgentRunStep,
        worker_lease_token: str,
        now: datetime,
    ) -> AgentRunBranchIntent:
        """Durably fence the only permitted POST before any network call.

        Once POST_CLAIMED is committed, every executor instance must reconcile
        through GET only. A crash after this transaction is intentionally
        ambiguous and cannot authorize a second POST.
        """
        with self._connect() as conn:
            with conn.transaction():
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
                if approval_row is None or task_row is None:
                    raise PostgresTrustRepositoryConflict("Branch create authority is missing")
                run_row = conn.execute(
                    "SELECT run_id,workspace_id,task_id,project_id,status,current_step_id,updated_at,payload FROM agent_runs "
                    "WHERE run_id=%s AND workspace_id=%s FOR UPDATE",
                    (intent.run_id, intent.workspace_id),
                ).fetchone()
                step_row = conn.execute(
                    "SELECT step_id,run_id,task_id,workspace_id,status,updated_at,payload FROM agent_run_steps "
                    "WHERE step_id=%s AND run_id=%s AND workspace_id=%s FOR UPDATE",
                    (intent.step_id, intent.run_id, intent.workspace_id),
                ).fetchone()
                intent_row = conn.execute(
                    f"SELECT {_COLUMNS} FROM agent_run_branch_intents "
                    "WHERE intent_id=%s AND workspace_id=%s FOR UPDATE",
                    (intent.id, intent.workspace_id),
                ).fetchone()
                if run_row is None or step_row is None or intent_row is None:
                    raise PostgresTrustRepositoryConflict("Branch create claim scope is missing")
                run = AgentRun.model_validate(run_row[7])
                step = AgentRunStep.model_validate(step_row[6])
                current = _decode(intent_row)
                db_now = conn.execute("SELECT clock_timestamp()").fetchone()[0]
                if (
                    current.status != BranchIntentStatus.INTENT
                    or current != intent
                    or run.id != run_row[0] or run.workspace_id != run_row[1]
                    or run.task_id != run_row[2] or run.project_id != run_row[3]
                    or run.status.value != run_row[4] or run.current_step_id != run_row[5]
                    or run.updated_at != run_row[6]
                    or step.id != step_row[0] or step.run_id != step_row[1]
                    or step.task_id != step_row[2]
                    or step.status.value != step_row[4] or step.updated_at != step_row[5]
                    or current.run_revision != run.updated_at
                    or current.step_revision != step.updated_at
                    or run != expected_run
                    or step != expected_step
                    or run.lease_owner is None
                    or run.lease_token != worker_lease_token
                    or sha256_hex({"lease_token": worker_lease_token}) != current.lease_token_hash
                    or not run.lease_expires_at or run.lease_expires_at <= db_now
                    or step.status != AgentRunStepStatus.RUNNING
                    or step.lease_token != worker_lease_token
                    or run.current_step_id != step.id
                    or run.pause_requested or run.cancel_requested
                ):
                    raise PostgresTrustRepositoryConflict("Branch create claim is stale or already consumed")
                action = self._trust.get_proposed_action(
                    conn, current.action_id, current.workspace_id, for_update=True
                )
                decision = self._trust.get_latest_policy_decision_for_action(
                    conn, current.action_id, current.workspace_id, for_update=True
                )
                if action is None or decision is None or decision.decision.value != "ALLOW":
                    raise PostgresTrustRepositoryConflict("Branch create authority is unavailable")
                task = AgentTask.model_validate(task_row[5])
                approval = ApprovalRequest.model_validate(approval_row[4])
                lease = self._trust.get_capability_lease(
                    conn, current.lease_id, current.workspace_id, for_update=True
                )
                if (
                    task.id != task_row[0]
                    or task.project_id != task_row[1]
                    or task.workspace_id != task_row[2]
                    or task.approval_request_id != task_row[3]
                    or task.status.value != task_row[4]
                    or approval.id != approval_row[0]
                    or approval.project_id != approval_row[1]
                    or approval.workspace_id != approval_row[2]
                    or approval.status.value != approval_row[3]
                ):
                    raise PostgresTrustRepositoryError("Branch create authority indexed state differs from payload")
                if lease is not None:
                    _validate_branch_authority(
                        task=task, approval=approval, lease=lease, action=action,
                        decision=decision, intent=current, now=db_now,
                    )
                if (
                    task.id != current.task_id or task.workspace_id != current.workspace_id
                    or task.project_id != task_row[1] or task.project_id != run.project_id
                    or task.approval_request_id != task_row[3]
                    or task.approval_request_id != current.approval_id
                    or task.status != AgentTaskStatus.APPROVED or task_row[4] != "APPROVED"
                    or approval.id != current.approval_id or approval.project_id != approval_row[1]
                    or approval.project_id != run.project_id
                    or approval.workspace_id != approval_row[2]
                    or approval.workspace_id != current.workspace_id
                    or approval.status != ApprovalStatus.APPROVED or approval_row[3] != "APPROVED"
                    or approval.approved_payload_hash is None
                    or approval.approved_payload_hash != compute_approval_payload_hash(approval)
                    or lease is None or lease.status.value != "ACTIVE" or lease.expires_at <= db_now
                    or lease.approval_request_id != current.approval_id
                    or lease.approved_payload_hash != approval.approved_payload_hash
                    or action.action_type.value != "CREATE_BRANCH"
                    or action.run_id != current.run_id or action.task_id != current.task_id
                    or action.workspace_id != current.workspace_id or action.lease_id != current.lease_id
                    or action.repository != current.repository or action.branch != current.target_ref
                    or action.metadata.get("source_sha") != current.source_sha
                    or action.metadata.get("source_branch") != current.source_branch
                    or action.metadata.get("target_ref") != current.target_ref
                    or action.metadata.get("idempotency_key") != current.idempotency_key
                    or action.metadata.get("step_id") != current.step_id
                    or action.metadata.get("task_id") != current.task_id
                    or action.metadata.get("approval_id") != current.approval_id
                    or action.metadata.get("lease_id") != current.lease_id
                    or action.metadata.get("repository") != current.repository
                    or action.provider != task.provider
                    or action.agent_id != task.external_agent_id
                    or action.project_id != task.project_id
                ):
                    raise PostgresTrustRepositoryConflict("Branch create authorization was revoked or changed")
                claimed = current.model_copy(update={
                    "status": BranchIntentStatus.POST_CLAIMED,
                    "reason": "One-shot remote POST claim committed; subsequent attempts are GET-only.",
                    "updated_at": now,
                })
                changed = conn.execute(
                    "UPDATE agent_run_branch_intents SET status=%s,reason=%s,updated_at=%s,payload=%s "
                    "WHERE intent_id=%s AND workspace_id=%s AND status='INTENT' "
                    "AND lease_token_hash=%s AND run_revision=%s AND step_revision=%s",
                    (claimed.status.value, claimed.reason, claimed.updated_at,
                     Jsonb(_payload(claimed)), claimed.id, claimed.workspace_id,
                     current.lease_token_hash, current.run_revision, current.step_revision),
                )
                if changed.rowcount != 1:
                    raise PostgresTrustRepositoryConflict("Branch create claim lost its fence")
                return claimed
