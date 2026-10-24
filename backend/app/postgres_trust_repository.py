from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import os
import re
import unicodedata
from typing import Any, TypeVar

from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from .agent_run_models import (
    AgentRun,
    AgentRunEvent,
    AgentRunStatus,
    AgentRunStep,
    AgentRunStepStatus,
    AgentRunStepType,
)
from .agent_task_models import AgentTask, AgentTaskStatus
from .approval_integrity import compute_approval_payload_hash
from .trust_action_authorization_models import (
    ActionAuthorization,
    ActionAuthorizationDecision,
)
from .trust_crypto import sha256_hex, verify_payload_signature
from .models import AgentProvider, ApprovalRequest, ApprovalStatus, RiskLevel
from .trust_models import (
    ActionConsumption,
    AgentReliabilityScorecard,
    CapabilityLease,
    CapabilityLeaseStatus,
    CapabilityActionType,
    CredentialGrant,
    PolicyDecision,
    PolicyDecisionType,
    ProposedAction,
    ReviewerAttestation,
    TrustEventType,
    FlightRecorderVerification,
    ExceptionQueueItem,
)
from .postgres_trust_event_repository import PostgresTrustEventRepository
from .trust_policy import (
    evaluate_proposed_action as evaluate_action_policy,
    lease_receipt_payload,
)

T = TypeVar("T")


class PostgresTrustRepositoryError(RuntimeError):
    """A PostgreSQL trust record is missing, malformed, or out of scope."""


class PostgresTrustRepositoryConflict(RuntimeError):
    """A requested trust operation conflicts with durable policy or scope."""


@dataclass(frozen=True)
class PostgresTrustMetricsSnapshot:
    """Workspace-scoped trust aggregates from one PostgreSQL snapshot."""

    lease_status_counts: dict[str, int]
    policy_decision_counts: dict[str, int]
    provider_decision_counts: dict[tuple[str, str], int]
    authorization_count: int
    consumption_count: int
    active_credential_count: int
    revoked_credential_count: int
    trust_event_count: int
    push_status_counts: dict[str, int]
    push_attempt_count: int
    push_queue_depth: int
    verification: FlightRecorderVerification


def _model_payload(record: Any) -> dict[str, Any]:
    return record.model_dump(mode="json")


class PostgresTrustRepository:
    """Transaction-scoped persistence primitives for workspace trust records.

    Methods accept the caller's connection and never commit independently, so
    callers can compose a trust mutation and its trust event in one transaction.
    This repository does not select itself for application routes.
    """

    @staticmethod
    def _require_scope(workspace_id: str) -> None:
        if not workspace_id:
            raise ValueError("PostgreSQL trust operations require a workspace")

    @staticmethod
    def task_actions_enabled() -> bool:
        """Task-only effects remain disabled unless explicitly enabled per process."""
        return os.getenv("AIXION_POSTGRES_TASK_ACTIONS_ENABLED", "false").strip().lower() in {
            "1", "true", "yes", "on"
        }

    def _lock_approved_task_authority(
        self, conn: Connection, *, task_id: str, workspace_id: str
    ) -> tuple[AgentTask, ApprovalRequest]:
        """Lock task -> project -> approval in the same order as AgentRun creation."""
        identity = conn.execute(
            "SELECT project_id,approval_request_id FROM agent_tasks "
            "WHERE task_id=%s AND workspace_id=%s",
            (task_id, workspace_id),
        ).fetchone()
        if identity is None or not identity[1]:
            raise PostgresTrustRepositoryConflict(
                "Approved task action is not visible in this workspace"
            )
        task_row = conn.execute(
            "SELECT task_id,project_id,workspace_id,approval_request_id,status,payload "
            "FROM agent_tasks WHERE task_id=%s AND workspace_id=%s FOR UPDATE",
            (task_id, workspace_id),
        ).fetchone()
        if task_row is None:
            raise PostgresTrustRepositoryConflict("AgentTask is not visible in this workspace")
        try:
            task = AgentTask.model_validate(task_row[5])
        except Exception as error:
            raise PostgresTrustRepositoryError("Stored AgentTask payload is invalid") from error
        project_row = conn.execute(
            "SELECT project_id,workspace_id FROM projects "
            "WHERE project_id=%s AND workspace_id=%s FOR KEY SHARE",
            (identity[0], workspace_id),
        ).fetchone()
        if project_row is None:
            raise PostgresTrustRepositoryConflict("Task project is not visible in this workspace")
        approval_row = conn.execute(
            "SELECT approval_id,project_id,workspace_id,status,payload "
            "FROM approval_requests WHERE approval_id=%s AND workspace_id=%s FOR UPDATE",
            (identity[1], workspace_id),
        ).fetchone()
        if approval_row is None:
            raise PostgresTrustRepositoryConflict("Task approval is not visible in this workspace")
        try:
            approval = ApprovalRequest.model_validate(approval_row[4])
        except Exception as error:
            raise PostgresTrustRepositoryError("Stored task approval payload is invalid") from error
        if (
            task.id != task_row[0]
            or task.project_id != task_row[1]
            or task.workspace_id != task_row[2]
            or task.approval_request_id != task_row[3]
            or task.status.value != task_row[4]
            or task.id != task_id
            or task.project_id != identity[0]
            or task.project_id != project_row[0]
            or task.workspace_id != workspace_id
            or task.approval_request_id != identity[1]
            or task.status != AgentTaskStatus.APPROVED
            or approval.id != approval_row[0]
            or approval.project_id != approval_row[1]
            or approval.workspace_id != approval_row[2]
            or approval.status != ApprovalStatus.APPROVED
            or approval.project_id != project_row[0]
            or approval.workspace_id != project_row[1]
            or approval.id != task.approval_request_id
            or not approval.approved_payload_hash
            or approval.approved_payload_hash != compute_approval_payload_hash(approval)
        ):
            raise PostgresTrustRepositoryConflict(
                "AgentTask is not approved or its project/approval binding is inconsistent"
            )
        if conn.execute(
            "SELECT 1 FROM agent_runs WHERE task_id=%s AND workspace_id=%s LIMIT 1",
            (task_id, workspace_id),
        ).fetchone() is not None:
            raise PostgresTrustRepositoryConflict(
                "Task has an AgentRun; use the durable run-linked action transaction"
            )
        return task, approval

    def lock_task_action_binding(
        self,
        conn: Connection,
        *,
        action_id: str,
        workspace_id: str,
        task_id: str,
    ) -> tuple[ProposedAction, AgentTask, ApprovalRequest]:
        """Lock and verify a task-only action against its approved authority.

        Lock order is task -> project -> approval -> proposed action, matching
        AgentRun creation. A run must not have
        been created for the task; once a run exists, its run/step transaction
        owns action lifecycle and the task-only route must reject it.
        """
        self._require_scope(workspace_id)
        if not self.task_actions_enabled():
            raise PostgresTrustRepositoryConflict(
                "PostgreSQL task-only actions are disabled"
            )
        if not task_id or conn.autocommit:
            raise ValueError("Task action binding requires a task and transaction")
        task, approval = self._lock_approved_task_authority(
            conn, task_id=task_id, workspace_id=workspace_id
        )
        action = self.get_proposed_action(
            conn, action_id, workspace_id, for_update=True
        )
        if action is None:
            raise PostgresTrustRepositoryConflict("Proposed task action is not visible")
        if (
            action.run_id is not None
            or action.task_id != task.id
            or action.workspace_id != workspace_id
            or action.project_id != task.project_id
            or action.provider != task.provider
            or action.agent_id != task.external_agent_id
            or action.repository != task.repository
            or action.branch != task.branch_preference
            or action.metadata.get("approval_request_id") != approval.id
            or action.metadata.get("approved_payload_hash") != approval.approved_payload_hash
        ):
            raise PostgresTrustRepositoryConflict(
                "Proposed action does not match its exact approved task payload and scope"
            )
        return action, task, approval

    def lock_task_action_lease(
        self,
        conn: Connection,
        *,
        action: ProposedAction,
        task: AgentTask,
        approval: ApprovalRequest,
    ) -> CapabilityLease:
        """Lock and validate the signed active lease after action/policy locks."""
        if not self.task_actions_enabled():
            raise PostgresTrustRepositoryConflict("PostgreSQL task-only actions are disabled")
        if conn.autocommit or not action.lease_id:
            raise PostgresTrustRepositoryConflict("Task action requires a durable capability lease")
        lease = self.get_capability_lease(
            conn, action.lease_id, approval.workspace_id or "", for_update=True
        )
        checked_at = conn.execute("SELECT clock_timestamp()").fetchone()[0]
        if (
            lease is None
            or lease.status != CapabilityLeaseStatus.ACTIVE
            or lease.expires_at <= checked_at
            or lease.approval_request_id != approval.id
            or lease.approved_payload_hash != approval.approved_payload_hash
            or lease.provider != task.provider
            or lease.agent_id != task.external_agent_id
            or lease.scope.repository != task.repository
            or lease.scope.branch != task.branch_preference
            or action.action_type not in lease.scope.allowed_actions
            or action.repository != lease.scope.repository
            or action.branch != lease.scope.branch
            or not verify_payload_signature(lease_receipt_payload(lease), lease.receipt_signature)
        ):
            raise PostgresTrustRepositoryConflict(
                "Task action is outside its active signed capability lease"
            )
        return lease

    def lock_agent_run_action_binding(
        self,
        conn: Connection,
        *,
        action_id: str,
        workspace_id: str,
        run_id: str,
        step_id: str,
    ) -> ProposedAction:
        """Lock and validate run → step → action identity in one transaction.

        Lock order is run, step, action, policy/authorization, then capability
        lease. This method validates identity only; it does not authorize or
        execute a side effect. Callers still need authenticated membership at
        the route boundary and must keep the AgentRun execution route disabled
        until all state and worker effects share a reviewed transaction.
        """
        self._require_scope(workspace_id)
        if not run_id or not step_id or conn.autocommit:
            raise ValueError("AgentRun action binding requires a transaction and exact identity")

        run_row = conn.execute(
            "SELECT run_id,workspace_id,task_id,project_id,status,current_step_id,updated_at,payload "
            "FROM agent_runs WHERE run_id=%s AND workspace_id=%s FOR UPDATE",
            (run_id, workspace_id),
        ).fetchone()
        if run_row is None:
            raise PostgresTrustRepositoryConflict(
                "AgentRun is not visible in this workspace"
            )
        try:
            run = AgentRun.model_validate(run_row[7])
        except Exception as error:
            raise PostgresTrustRepositoryError("Stored AgentRun payload is invalid") from error
        if (
            run.id != run_row[0]
            or run.workspace_id != run_row[1]
            or run.task_id != run_row[2]
            or run.project_id != run_row[3]
            or run.status.value != run_row[4]
            or run.current_step_id != run_row[5]
            or run.updated_at != run_row[6]
        ):
            raise PostgresTrustRepositoryError(
                "Stored AgentRun identity or indexed state is inconsistent"
            )
        if run.current_step_id != step_id:
            raise PostgresTrustRepositoryConflict(
                "AgentRun current step does not match the requested action step"
            )

        step_row = conn.execute(
            "SELECT step_id,run_id,task_id,workspace_id,sequence,status,updated_at,payload "
            "FROM agent_run_steps WHERE step_id=%s AND workspace_id=%s FOR UPDATE",
            (step_id, workspace_id),
        ).fetchone()
        if step_row is None:
            raise PostgresTrustRepositoryConflict(
                "AgentRun step is not visible in this workspace"
            )
        try:
            step = AgentRunStep.model_validate(step_row[7])
        except Exception as error:
            raise PostgresTrustRepositoryError("Stored AgentRun step payload is invalid") from error
        if (
            step.id != step_row[0]
            or step.run_id != step_row[1]
            or step.task_id != step_row[2]
            or step.sequence != step_row[4]
            or step.status.value != step_row[5]
            or step.updated_at != step_row[6]
            or step_row[1] != run.id
            or step_row[2] != run.task_id
            or step_row[3] != workspace_id
        ):
            raise PostgresTrustRepositoryError(
                "Stored AgentRun step identity or scope is inconsistent"
            )

        action = self.get_proposed_action(
            conn, action_id, workspace_id, for_update=True
        )
        if action is None:
            raise PostgresTrustRepositoryConflict("Proposed action not found")
        metadata = action.metadata
        if (
            action.workspace_id != workspace_id
            or action.run_id != run.id
            or action.task_id != run.task_id
            or action.project_id != run.project_id
            or action.lease_id != run.capability_lease_id
            or metadata.get("step_id") != step.id
            or metadata.get("step_type") != step.step_type.value
            or metadata.get("idempotency_key") != step.idempotency_key
        ):
            raise PostgresTrustRepositoryConflict(
                "Proposed action does not match the durable AgentRun step identity"
            )
        return action

    def lock_agent_run_validation_command_binding(
        self,
        conn: Connection,
        *,
        action_id: str,
        workspace_id: str,
        run_id: str,
        step_id: str,
        worker_id: str,
        lease_token: str,
        lock_capability_lease: bool = True,
    ) -> ProposedAction:
        """Validate one RUN_COMMAND against the durable approved test plan.

        This method is a trust-binding primitive only. It does not authorize,
        consume, or execute an action. Callers must perform those operations in
        a separately reviewed durable transaction.
        """
        self._require_scope(workspace_id)
        if (
            not run_id
            or not step_id
            or not worker_id
            or not lease_token
            or conn.autocommit
        ):
            raise ValueError("Validation command binding requires a transaction and full lease identity")

        # Read the parent IDs without locks, then acquire the authority rows
        # in approval -> task -> run -> step -> action -> capability-lease order.
        identity = conn.execute(
            "SELECT task_id,project_id,approval_id FROM agent_runs "
            "WHERE run_id=%s AND workspace_id=%s",
            (run_id, workspace_id),
        ).fetchone()
        if identity is None or not identity[0] or not identity[2]:
            raise PostgresTrustRepositoryConflict("AgentRun approval scope is not visible in this workspace")

        approval_row = conn.execute(
            "SELECT approval_id,project_id,workspace_id,status,payload FROM approval_requests "
            "WHERE approval_id=%s AND workspace_id=%s FOR UPDATE",
            (identity[2], workspace_id),
        ).fetchone()
        if approval_row is None:
            raise PostgresTrustRepositoryConflict("Approved validation plan is not visible in this workspace")
        try:
            approval = ApprovalRequest.model_validate(approval_row[4])
        except Exception as error:
            raise PostgresTrustRepositoryError("Stored approval payload is invalid") from error
        if (
            approval.id != approval_row[0]
            or approval.project_id != approval_row[1]
            or approval.workspace_id != approval_row[2]
            or approval.status != ApprovalStatus.APPROVED
            or approval.id != identity[2]
            or approval.project_id != identity[1]
            or approval.workspace_id != workspace_id
            or not approval.approved_payload_hash
            or approval.approved_payload_hash != compute_approval_payload_hash(approval)
        ):
            raise PostgresTrustRepositoryConflict("Approval is not current or its reviewed payload changed")
        # Reuse the existing validation-command policy; do not fork its prefix,
        # shell-control, count, length, or duplicate-command rules here.
        from .agent_worker_validation_plan import validate_validation_plan

        validation_error = validate_validation_plan(approval)
        if validation_error:
            raise PostgresTrustRepositoryConflict(
                "Approved validation plan is not executable under current policy"
            )

        task_row = conn.execute(
            "SELECT task_id,project_id,workspace_id,approval_request_id,status,payload "
            "FROM agent_tasks WHERE task_id=%s AND workspace_id=%s FOR UPDATE",
            (identity[0], workspace_id),
        ).fetchone()
        if task_row is None:
            raise PostgresTrustRepositoryConflict("AgentTask is not visible in this workspace")
        try:
            task = AgentTask.model_validate(task_row[5])
        except Exception as error:
            raise PostgresTrustRepositoryError("Stored AgentTask payload is invalid") from error
        if (
            task.id != task_row[0]
            or task.project_id != task_row[1]
            or task.workspace_id != task_row[2]
            or task.approval_request_id != task_row[3]
            or task.status.value != task_row[4]
            or task.id != identity[0]
            or task.project_id != identity[1]
            or task.workspace_id != workspace_id
            or task.approval_request_id != approval.id
            or task.status != AgentTaskStatus.APPROVED
        ):
            raise PostgresTrustRepositoryConflict("AgentTask approval or workspace binding is inconsistent")

        run_row = conn.execute(
            "SELECT run_id,task_id,project_id,workspace_id,approval_id,status,"
            "current_step_id,lease_owner,lease_token,lease_expires_at,updated_at,payload "
            "FROM agent_runs WHERE run_id=%s AND workspace_id=%s FOR UPDATE",
            (run_id, workspace_id),
        ).fetchone()
        if run_row is None:
            raise PostgresTrustRepositoryConflict("AgentRun is not visible in this workspace")
        try:
            run = AgentRun.model_validate(run_row[11])
        except Exception as error:
            raise PostgresTrustRepositoryError("Stored AgentRun payload is invalid") from error
        if (
            run.id != run_row[0]
            or run.task_id != run_row[1]
            or run.project_id != run_row[2]
            or run.workspace_id != run_row[3]
            or run.approval_request_id != run_row[4]
            or run.status.value != run_row[5]
            or run.current_step_id != run_row[6]
            or run.lease_owner != run_row[7]
            or run.lease_token != run_row[8]
            or run.lease_expires_at != run_row[9]
            or run.updated_at != run_row[10]
            or run.task_id != identity[0]
            or run.project_id != identity[1]
            or run.approval_request_id != identity[2]
        ):
            raise PostgresTrustRepositoryError("Stored AgentRun identity or lease is inconsistent")
        if (
            run.status != AgentRunStatus.RUNNING
            or run.current_step_id != step_id
            or run.lease_owner != worker_id
            or run.lease_token != lease_token
            or run.lease_expires_at is None
            or not run.approval_request_id
            or not run.capability_lease_id
        ):
            raise PostgresTrustRepositoryConflict("AgentRun execution lease or approval scope is not current")

        step_row = conn.execute(
            "SELECT step_id,run_id,task_id,workspace_id,sequence,status,updated_at,payload "
            "FROM agent_run_steps WHERE step_id=%s AND workspace_id=%s FOR UPDATE",
            (step_id, workspace_id),
        ).fetchone()
        if step_row is None:
            raise PostgresTrustRepositoryConflict("AgentRun step is not visible in this workspace")
        try:
            step = AgentRunStep.model_validate(step_row[7])
        except Exception as error:
            raise PostgresTrustRepositoryError("Stored AgentRun step payload is invalid") from error
        if (
            step.id != step_row[0]
            or step.run_id != step_row[1]
            or step.task_id != step_row[2]
            or step.sequence != step_row[4]
            or step.status.value != step_row[5]
            or step.updated_at != step_row[6]
            or step.run_id != run.id
            or step.task_id != run.task_id
            or step_row[3] != workspace_id
        ):
            raise PostgresTrustRepositoryError("Stored AgentRun step identity or scope is inconsistent")
        if (
            step.step_type != AgentRunStepType.RUN_VALIDATION
            or step.status != AgentRunStepStatus.RUNNING
            or step.lease_owner != worker_id
            or step.lease_token != lease_token
            or step.lease_expires_at is None
        ):
            raise PostgresTrustRepositoryConflict("AgentRun step lease or type is not eligible for validation")

        action = self.get_proposed_action(conn, action_id, workspace_id, for_update=True)
        if action is None:
            raise PostgresTrustRepositoryConflict("Proposed validation command not found")
        command_index = action.metadata.get("command_index")
        if isinstance(command_index, bool) or not isinstance(command_index, int):
            raise PostgresTrustRepositoryConflict("Validation command index is invalid")
        if not 0 <= command_index < len(approval.test_plan):
            raise PostgresTrustRepositoryConflict("Validation command index is outside the approved plan")
        command = approval.test_plan[command_index]
        required_metadata = {
            "step_id": step.id,
            "step_type": AgentRunStepType.RUN_VALIDATION.value,
            "idempotency_key": step.idempotency_key,
            "approval_request_id": approval.id,
            "approved_payload_hash": approval.approved_payload_hash,
            "command_source": "approval.test_plan",
            "command_sha256": sha256_hex({"command": command}),
        }
        if (
            action.action_type != CapabilityActionType.RUN_COMMAND
            or action.command != command
            or action.lease_id != run.capability_lease_id
            or action.workspace_id != workspace_id
            or action.project_id != run.project_id
            or action.task_id != run.task_id
            or action.run_id != run.id
            or action.provider != task.provider
            or action.agent_id != task.external_agent_id
            or action.repository != run.repository
            or any(action.metadata.get(key) != value for key, value in required_metadata.items())
        ):
            raise PostgresTrustRepositoryConflict("Proposed command does not match the approved validation provenance")

        lease = self.get_capability_lease(
            conn,
            run.capability_lease_id,
            workspace_id,
            for_update=lock_capability_lease,
        )
        if lease is None:
            raise PostgresTrustRepositoryConflict("Capability lease is not visible in this workspace")
        if (
            lease.id != run.capability_lease_id
            or lease.approval_request_id != approval.id
            or lease.workspace_id != workspace_id
            or lease.approved_payload_hash != approval.approved_payload_hash
            or lease.provider != task.provider
            or lease.agent_id != task.external_agent_id
            or lease.status != CapabilityLeaseStatus.ACTIVE
            or CapabilityActionType.RUN_COMMAND not in lease.scope.allowed_actions
            or command not in lease.scope.allowed_commands
            or lease.scope.repository != run.repository
            or action.branch != lease.scope.branch
            or not verify_payload_signature(lease_receipt_payload(lease), lease.receipt_signature)
        ):
            raise PostgresTrustRepositoryConflict("Capability lease does not authorize this exact validation command")
        checked_at = conn.execute("SELECT clock_timestamp()").fetchone()[0]
        if (
            run.lease_expires_at <= checked_at
            or step.lease_expires_at <= checked_at
            or lease.expires_at <= checked_at
        ):
            raise PostgresTrustRepositoryConflict("AgentRun, step, or capability lease expired")
        return action

    def evaluate_agent_run_validation_command(
        self,
        conn: Connection,
        *,
        action: ProposedAction,
        workspace_id: str,
        run_id: str,
        step_id: str,
        worker_id: str,
        lease_token: str,
        actor: str,
        trust_event_repository: PostgresTrustEventRepository,
    ) -> tuple[PolicyDecision, CapabilityLease]:
        """Evaluate a persisted exact RUN_VALIDATION command atomically.

        The caller owns the transaction. This records policy and audit evidence
        only; it never approves a reviewer request, consumes a capability, or
        executes the command. The action must already exist and retries are
        accepted only when its payload, lease, and original trust events match.
        """
        self._require_scope(workspace_id)
        if conn.autocommit or not actor:
            raise ValueError("Validation command evaluation requires an actor and transaction")

        # The binding helper takes locks through the action. Defer its lease
        # lock so the established action -> policy -> lease ordering holds.
        bound_action = self.lock_agent_run_validation_command_binding(
            conn,
            action_id=action.id,
            workspace_id=workspace_id,
            run_id=run_id,
            step_id=step_id,
            worker_id=worker_id,
            lease_token=lease_token,
            lock_capability_lease=False,
        )
        if bound_action.model_dump(mode="json") != action.model_dump(mode="json"):
            raise PostgresTrustRepositoryConflict(
                "Persisted validation command differs from the supplied action payload"
            )

        existing_decision = self.get_latest_policy_decision_for_action(
            conn, action.id, workspace_id, for_update=True
        )
        lease_id = action.lease_id
        if not lease_id:
            raise PostgresTrustRepositoryConflict("Validation command has no capability lease")
        lease = self.get_capability_lease(
            conn, lease_id, workspace_id, for_update=True
        )
        if lease is None or lease.status != CapabilityLeaseStatus.ACTIVE:
            raise PostgresTrustRepositoryConflict("Validation command capability lease is inactive")

        # Recheck the complete signed scope after locking the lease. The first
        # binding pass validated identity and acquired authority/action locks,
        # but intentionally read the lease unlocked to preserve lock order.
        # Another transaction may have changed its mutable state meanwhile.
        if (
            lease.id != action.lease_id
            or lease.workspace_id != workspace_id
            or lease.approval_request_id != action.metadata.get("approval_request_id")
            or lease.approved_payload_hash != action.metadata.get("approved_payload_hash")
            or lease.provider != action.provider
            or lease.agent_id != action.agent_id
            or lease.status != CapabilityLeaseStatus.ACTIVE
            or CapabilityActionType.RUN_COMMAND not in lease.scope.allowed_actions
            or action.command not in lease.scope.allowed_commands
            or lease.scope.repository != action.repository
            or lease.scope.branch != action.branch
            or not verify_payload_signature(lease_receipt_payload(lease), lease.receipt_signature)
        ):
            raise PostgresTrustRepositoryConflict("Locked capability lease no longer matches the command")

        run_row = conn.execute(
            "SELECT payload FROM agent_runs WHERE run_id=%s AND workspace_id=%s",
            (run_id, workspace_id),
        ).fetchone()
        step_row = conn.execute(
            "SELECT payload FROM agent_run_steps WHERE step_id=%s AND workspace_id=%s",
            (step_id, workspace_id),
        ).fetchone()
        if run_row is None or step_row is None:
            raise PostgresTrustRepositoryConflict("Validation run or step disappeared during evaluation")
        try:
            run = AgentRun.model_validate(run_row[0])
            step = AgentRunStep.model_validate(step_row[0])
        except Exception as error:
            raise PostgresTrustRepositoryError("Stored validation run or step payload is invalid") from error
        checked_at = conn.execute("SELECT clock_timestamp()").fetchone()[0]
        if (
            run.lease_owner != worker_id
            or run.lease_token != lease_token
            or run.current_step_id != step_id
            or run.lease_expires_at is None
            or run.lease_expires_at <= checked_at
            or step.lease_owner != worker_id
            or step.lease_token != lease_token
            or step.status != AgentRunStepStatus.RUNNING
            or step.lease_expires_at is None
            or step.lease_expires_at <= checked_at
            or lease.expires_at <= checked_at
        ):
            raise PostgresTrustRepositoryConflict("Validation worker, step, or capability lease expired")

        evaluated = evaluate_action_policy(action, lease, now=checked_at)
        action_payload_hash = sha256_hex(action.model_dump(mode="json"))
        if existing_decision is not None:
            if (
                existing_decision.lease_id != lease.id
                or existing_decision.evaluated_policy_version != "trust-policy-v1"
                or existing_decision.decision != evaluated.decision
                or existing_decision.risk_level != evaluated.risk_level
                or existing_decision.reasons != evaluated.reasons
                or existing_decision.obligations != evaluated.obligations
            ):
                raise PostgresTrustRepositoryConflict(
                    "Existing action decision is not an idempotent validation evaluation"
                )
            events = trust_event_repository.list_workspace_events(
                conn,
                workspace_id=workspace_id,
                entity_type="proposed_action",
                entity_id=action.id,
            )
            received = [event for event in events if event.event_type == TrustEventType.ACTION_RECEIVED]
            outcomes = [
                event for event in events
                if event.event_type in {
                    TrustEventType.ACTION_ALLOWED,
                    TrustEventType.ACTION_REQUIRES_APPROVAL,
                    TrustEventType.ACTION_BLOCKED,
                }
            ]
            expected_outcome = {
                PolicyDecisionType.ALLOW: TrustEventType.ACTION_ALLOWED,
                PolicyDecisionType.REQUIRE_APPROVAL: TrustEventType.ACTION_REQUIRES_APPROVAL,
                PolicyDecisionType.BLOCK: TrustEventType.ACTION_BLOCKED,
            }[existing_decision.decision]
            if (
                len(received) != 1
                or len(outcomes) != 1
                or received[0].payload.get("action_payload_sha256") != action_payload_hash
                or received[0].payload.get("lease_id") != lease.id
                or received[0].payload.get("run_id") != run_id
                or received[0].payload.get("step_id") != step_id
                or outcomes[0].event_type != expected_outcome
                or outcomes[0].payload.get("decision_id") != existing_decision.id
                or outcomes[0].payload.get("decision") != existing_decision.decision
            ):
                raise PostgresTrustRepositoryConflict(
                    "Existing validation decision has no matching immutable receipt"
                )
            return existing_decision, lease

        decision = evaluated
        self.insert_policy_decision(conn, decision, workspace_id=workspace_id)
        trust_event_repository.append_in_transaction(
            conn,
            TrustEventType.ACTION_RECEIVED,
            entity_type="proposed_action",
            entity_id=action.id,
            actor=actor,
            workspace_id=workspace_id,
            correlation_id=run_id,
            payload={
                "provider": action.provider,
                "agent_id": action.agent_id,
                "action_type": action.action_type,
                "lease_id": lease.id,
                "run_id": run_id,
                "step_id": step_id,
                "idempotency_key": action.metadata["idempotency_key"],
                "command_source": action.metadata["command_source"],
                "command_index": action.metadata["command_index"],
                "command_sha256": action.metadata["command_sha256"],
                "action_payload_sha256": action_payload_hash,
            },
        )
        event_type = {
            PolicyDecisionType.ALLOW: TrustEventType.ACTION_ALLOWED,
            PolicyDecisionType.REQUIRE_APPROVAL: TrustEventType.ACTION_REQUIRES_APPROVAL,
            PolicyDecisionType.BLOCK: TrustEventType.ACTION_BLOCKED,
        }[decision.decision]
        trust_event_repository.append_in_transaction(
            conn,
            event_type,
            entity_type="proposed_action",
            entity_id=action.id,
            actor=actor,
            workspace_id=workspace_id,
            correlation_id=run_id,
            payload={
                "decision_id": decision.id,
                "decision": decision.decision,
                "risk_level": decision.risk_level,
                "reasons": decision.reasons,
                "obligations": decision.obligations,
            },
        )
        return decision, lease

    def evaluate_agent_run_action(
        self,
        conn: Connection,
        *,
        action: ProposedAction,
        workspace_id: str,
        run_id: str,
        step_id: str,
        actor: str,
        now: datetime,
        trust_event_repository: PostgresTrustEventRepository,
        expected_base_branch: str | None = None,
    ) -> tuple[PolicyDecision, CapabilityLease]:
        """Create/evaluate one exact run action while locking its authority.

        This is deliberately narrower than the standalone action evaluator:
        it accepts only CREATE_BRANCH, one exact MODIFY_FILES action, or one
        exact CREATE_PULL_REQUEST action for the current leased run step.
        Callers must use the returned persisted decision and must not perform a
        remote side effect in this transaction.
        """
        self._require_scope(workspace_id)
        if conn.autocommit or not actor or not run_id or not step_id:
            raise ValueError("Run action evaluation requires a transaction and full identity")
        action_type = action.action_type.value
        apply_change = action_type == "MODIFY_FILES"
        create_pull_request = action_type == "CREATE_PULL_REQUEST"
        expected_step_type = (
            "APPLY_CHANGES" if apply_change else
            "CREATE_PULL_REQUEST" if create_pull_request else "CREATE_BRANCH"
        )
        if (
            action.workspace_id != workspace_id
            or action.run_id != run_id
            or not action.task_id
            or action_type not in {"CREATE_BRANCH", "MODIFY_FILES", "CREATE_PULL_REQUEST"}
            or action.lease_id is None
            or action.repository is None
            or not action.branch
            or action.metadata.get("step_id") != step_id
            or action.metadata.get("step_type") != expected_step_type
            or not action.metadata.get("idempotency_key")
            or (
                apply_change
                and (
                    len(action.paths) != 1
                    or action.paths[0] != action.metadata.get("path")
                    or action.paths[0].startswith("/")
                    or "\\" in action.paths[0]
                    or any(part in {"", ".", ".."} for part in action.paths[0].split("/"))
                    or any(unicodedata.category(char) in {"Cc", "Cf"} for char in action.paths[0])
                    or action.metadata.get("change_type") not in {"create", "update"}
                    or not re.fullmatch(r"[0-9a-f]{64}", str(action.metadata.get("content_sha256", "")))
                    or not re.fullmatch(r"[0-9a-f]{40}", str(action.metadata.get("base_commit_sha", "")))
                    or (action.metadata.get("change_type") == "create" and action.metadata.get("before_sha") is not None)
                    or (action.metadata.get("change_type") == "update" and not re.fullmatch(r"[0-9a-f]{40}", str(action.metadata.get("before_sha", ""))))
                )
            )
            or (
                not apply_change and not create_pull_request
                and not re.fullmatch(r"[0-9a-f]{40}", str(action.metadata.get("source_sha", "")))
            )
            or (
                create_pull_request
                and (
                    action.metadata.get("step_type") != "CREATE_PULL_REQUEST"
                    or action.metadata.get("head_branch") != action.branch
                    or not action.metadata.get("base_branch")
                    or expected_base_branch is None
                    or action.metadata.get("base_branch") != expected_base_branch
                    or action.metadata.get("repository") != action.repository
                    or not re.fullmatch(r"[0-9a-f]{40}", str(action.metadata.get("validated_head_sha", "")))
                    or not re.fullmatch(r"[0-9a-f]{40}", str(action.metadata.get("validated_base_sha", "")))
                    or not re.fullmatch(r"[0-9a-f]{64}", str(action.metadata.get("title_sha256", "")))
                    or not re.fullmatch(r"[0-9a-f]{64}", str(action.metadata.get("body_sha256", "")))
                    or not re.fullmatch(r"aixion-intent:[a-f0-9-]{36}", str(action.metadata.get("intent_marker", "")))
                )
            )
        ):
            raise PostgresTrustRepositoryConflict("Run branch action scope is incomplete")

        # Evaluation takes approval/task SHARE locks before locking the run,
        # step, action, and policy decision, then locks the capability lease.
        # Reviewer recording has a different prefix (run -> step -> action /
        # policy, then task/approval SHARE -> lease). These parent locks are
        # SHARE-compatible, and that reviewer path only re-reads its already
        # locked run/step rows during the final transition; do not treat this
        # as one global order or move locks without a demonstrated cycle.
        approval_id = str(action.metadata.get("approval_id", ""))
        approval_row = conn.execute(
            "SELECT approval_id,project_id,workspace_id,status,payload "
            "FROM approval_requests WHERE approval_id=%s AND workspace_id=%s FOR SHARE",
            (approval_id, workspace_id),
        ).fetchone()
        task_row = conn.execute(
            "SELECT task_id,project_id,workspace_id,approval_request_id,status,payload "
            "FROM agent_tasks WHERE task_id=%s AND workspace_id=%s FOR SHARE",
            (action.task_id, workspace_id),
        ).fetchone()
        if approval_row is None or task_row is None:
            raise PostgresTrustRepositoryConflict("Run branch authority is missing")
        try:
            approval = ApprovalRequest.model_validate(approval_row[4])
            task = AgentTask.model_validate(task_row[5])
        except Exception as error:
            raise PostgresTrustRepositoryError("Stored run branch authority is invalid") from error
        if (
            approval.id != approval_row[0]
            or approval.project_id != approval_row[1]
            or approval.workspace_id != approval_row[2]
            or approval.status.value != approval_row[3]
            or approval.status.value != "APPROVED"
            or approval.approved_payload_hash is None
            or approval.approved_payload_hash != compute_approval_payload_hash(approval)
            or task.id != task_row[0]
            or task.project_id != task_row[1]
            or task.workspace_id != task_row[2]
            or task.approval_request_id != task_row[3]
            or task.status.value != task_row[4]
            or task.status.value != "APPROVED"
            or task.approval_request_id != approval.id
            or (create_pull_request and approval.target_branch != action.branch)
        ):
            raise PostgresTrustRepositoryConflict("Run branch authority payload or status is invalid")

        run_row = conn.execute(
            "SELECT run_id,workspace_id,task_id,project_id,status,current_step_id,payload "
            "FROM agent_runs WHERE run_id=%s AND workspace_id=%s FOR UPDATE",
            (run_id, workspace_id),
        ).fetchone()
        if run_row is None:
            raise PostgresTrustRepositoryConflict("AgentRun is not visible in this workspace")
        try:
            run = AgentRun.model_validate(run_row[6])
        except Exception as error:
            raise PostgresTrustRepositoryError("Stored AgentRun payload is invalid") from error
        checked_at = conn.execute("SELECT clock_timestamp()").fetchone()[0]
        if (
            run.id != run_row[0]
            or run.workspace_id != workspace_id
            or run.task_id != run_row[2]
            or run.project_id != run_row[3]
            or run.status.value != run_row[4]
            or run.current_step_id != run_row[5]
            or run.current_step_id != step_id
            or run.task_id != action.task_id
            or run.project_id != action.project_id
            or run.approval_request_id != approval.id
            or run.capability_lease_id != action.lease_id
            or run.status != AgentRunStatus.RUNNING
            or run.lease_owner is None
            or actor != run.lease_owner
            or run.lease_token is None
            or run.lease_expires_at is None
            or run.lease_expires_at <= checked_at
        ):
            raise PostgresTrustRepositoryConflict("AgentRun lease or action binding is invalid")

        step_row = conn.execute(
            "SELECT step_id,run_id,task_id,workspace_id,status,payload FROM agent_run_steps "
            "WHERE step_id=%s AND run_id=%s AND workspace_id=%s FOR UPDATE",
            (step_id, run_id, workspace_id),
        ).fetchone()
        if step_row is None:
            raise PostgresTrustRepositoryConflict("AgentRun step is not visible in this workspace")
        try:
            step = AgentRunStep.model_validate(step_row[5])
        except Exception as error:
            raise PostgresTrustRepositoryError("Stored AgentRun step payload is invalid") from error
        if (
            step.id != step_row[0]
            or step.run_id != run_id
            or step.task_id != run.task_id
            or step.status.value != step_row[4]
            or step.step_type.value != expected_step_type
            or step.idempotency_key != action.metadata.get("idempotency_key")
            or action.metadata.get("task_id") != run.task_id
            or action.metadata.get("approval_id") != run.approval_request_id
            or action.metadata.get("approved_payload_hash") != approval.approved_payload_hash
            or action.metadata.get("lease_id") != run.capability_lease_id
            or action.metadata.get("repository") != action.repository
            or (
                not create_pull_request
                and action.metadata.get("target_ref") != action.branch
            )
            or (
                create_pull_request
                and action.metadata.get("head_branch") != action.branch
            )
            or step.status not in {AgentRunStepStatus.READY, AgentRunStepStatus.RUNNING}
        ):
            raise PostgresTrustRepositoryConflict("Run branch step identity or state is invalid")

        # This evaluator already holds approval/task SHARE, then run/step;
        # action and policy rows follow, with the capability lease last. The
        # reviewer route locks run/step/action/policy first, then obtains only
        # SHARE locks for task/approval and updates the lease. Those authority
        # SHARE locks are compatible, and the reviewer transition reuses its
        # already-held run/step locks. These are per-path orders, not a single
        # global order. Consumption follows the action/policy-before-lease
        # order after its run binding has been validated.
        existing = self.get_proposed_action(conn, action.id, workspace_id, for_update=True)
        if existing is not None and existing.model_dump(mode="json") != action.model_dump(mode="json"):
            raise PostgresTrustRepositoryConflict("Run branch action id is bound to other content")
        existing_decision = (
            self.get_latest_policy_decision_for_action(
                conn, action.id, workspace_id, for_update=True
            )
            if existing is not None else None
        )
        if existing is not None and existing_decision is None:
            raise PostgresTrustRepositoryError("Persisted run action has no policy decision")

        if (
            task.id != task_row[0]
            or task.project_id != task_row[1]
            or task.workspace_id != workspace_id
            or task.approval_request_id != approval_row[0]
            or task.status.value != task_row[4]
            or task.status.value != "APPROVED"
            or task.repository != action.repository
            or task.branch_preference != action.branch
            or task.project_id != run.project_id
            or approval.id != approval_row[0]
            or approval.project_id != approval_row[1]
            or approval.workspace_id != workspace_id
            or approval.status.value != approval_row[3]
            or approval.status.value != "APPROVED"
            or approval.approved_payload_hash is None
            or approval.approved_payload_hash != compute_approval_payload_hash(approval)
        ):
            raise PostgresTrustRepositoryConflict("Run branch approval scope is invalid")

        lease = self.get_capability_lease(
            conn, action.lease_id, workspace_id, for_update=True
        )
        if lease is None or lease.status != CapabilityLeaseStatus.ACTIVE or lease.expires_at <= checked_at:
            raise PostgresTrustRepositoryConflict("Run branch capability lease is inactive")
        if (
            not verify_payload_signature(lease_receipt_payload(lease), lease.receipt_signature)
            or lease.workspace_id != workspace_id
            or lease.approval_request_id != approval.id
            or lease.approved_payload_hash != approval.approved_payload_hash
            or lease.scope.repository != action.repository
            or lease.scope.branch != action.branch
            or action.action_type not in lease.scope.allowed_actions
            or action.provider != task.provider
            or action.agent_id != task.external_agent_id
            or action.project_id != task.project_id
            or action.task_id != task.id
            or (task.branch_preference and action.branch != task.branch_preference)
        ):
            raise PostgresTrustRepositoryConflict("Run branch action exceeds approved capability scope")

        if existing is not None:
            if existing_decision.lease_id != action.lease_id:
                raise PostgresTrustRepositoryConflict("Persisted policy decision is bound to a different lease")
            return existing_decision, lease

        decision = evaluate_action_policy(action, lease, now=now)
        self.insert_proposed_action(conn, action)
        self.insert_policy_decision(conn, decision, workspace_id=workspace_id)
        event_payload = {
            "provider": action.provider,
            "agent_id": action.agent_id,
            "lease_id": action.lease_id,
            "action_type": action.action_type,
            "run_id": run.id,
            "step_id": step.id,
            "idempotency_key": step.idempotency_key,
        }
        if apply_change:
            allowed_prefixes = lease.scope.allowed_path_prefixes
            path = action.paths[0]
            if (
                not allowed_prefixes
                or not any(path == prefix.rstrip("/") or path.startswith(prefix.rstrip("/") + "/") for prefix in allowed_prefixes)
                or path.startswith("/")
                or ".." in path.split("/")
            ):
                raise PostgresTrustRepositoryConflict("Run file action exceeds approved path prefixes")
            file_changes = [item for item in approval.files if item.path == path]
            if len(approval.files) != 1 or len(file_changes) != 1:
                raise PostgresTrustRepositoryConflict("Run file action requires exactly one approved file")
            approved_file = file_changes[0]
            import hashlib
            if (
                approved_file.change_type.strip().lower() != action.metadata.get("change_type")
                or approved_file.new_content is None
                or hashlib.sha256(approved_file.new_content.encode("utf-8")).hexdigest() != action.metadata.get("content_sha256")
            ):
                raise PostgresTrustRepositoryConflict("Run file action differs from exact approved content")
            if action.metadata.get("before_sha") is not None and action.metadata.get("before_sha") == action.metadata.get("content_sha256"):
                raise PostgresTrustRepositoryConflict("Run file action has an invalid before/after digest binding")
            event_payload.update({
                "path": path,
                "change_type": action.metadata["change_type"],
                "content_sha256": action.metadata["content_sha256"],
                "before_sha": action.metadata.get("before_sha"),
            })
        elif create_pull_request:
            event_payload.update({
                "repository": action.repository,
                "head_branch": action.metadata["head_branch"],
                "base_branch": action.metadata["base_branch"],
                "validated_head_sha": action.metadata["validated_head_sha"],
                "validated_base_sha": action.metadata["validated_base_sha"],
                "intent_marker": action.metadata["intent_marker"],
            })
        else:
            event_payload.update({
                "source_sha": action.metadata["source_sha"],
                "target_ref": action.branch,
            })
        trust_event_repository.append_in_transaction(
            conn,
            TrustEventType.ACTION_RECEIVED,
            entity_type="proposed_action",
            entity_id=action.id,
            actor=actor,
            workspace_id=workspace_id,
            correlation_id=run.id,
            payload=event_payload,
        )
        event_type = {
            PolicyDecisionType.ALLOW: TrustEventType.ACTION_ALLOWED,
            PolicyDecisionType.REQUIRE_APPROVAL: TrustEventType.ACTION_REQUIRES_APPROVAL,
            PolicyDecisionType.BLOCK: TrustEventType.ACTION_BLOCKED,
        }[decision.decision]
        trust_event_repository.append_in_transaction(
            conn,
            event_type,
            entity_type="proposed_action",
            entity_id=action.id,
            actor=actor,
            workspace_id=workspace_id,
            correlation_id=run.id,
            payload={
                "decision_id": decision.id,
                "decision": decision.decision,
                "risk_level": decision.risk_level,
                "reasons": decision.reasons,
                "obligations": decision.obligations,
            },
        )
        return decision, lease

    def metrics_snapshot(
        self,
        conn: Connection,
        *,
        workspace_id: str,
        now: datetime,
        trust_event_repository: PostgresTrustEventRepository,
    ) -> PostgresTrustMetricsSnapshot:
        """Read trust and notification aggregates in the caller's DB snapshot."""
        self._require_scope(workspace_id)
        if now.utcoffset() is None:
            raise ValueError("Trust metric snapshot time must be timezone-aware")

        lease_status_counts = Counter()
        for status, count in conn.execute(
            "SELECT CASE WHEN status='ACTIVE' AND expires_at<=%s THEN 'EXPIRED' "
            "ELSE status END,COUNT(*) FROM capability_leases "
            "WHERE workspace_id=%s GROUP BY 1",
            (now, workspace_id),
        ).fetchall():
            if status not in {item.value for item in CapabilityLeaseStatus}:
                raise PostgresTrustRepositoryError(
                    "Capability lease metrics contain an unsupported state"
                )
            lease_status_counts[status] = int(count)

        policy_decision_counts = Counter()
        provider_decision_counts = Counter()
        for decision, provider, count in conn.execute(
            "SELECT decision,action.payload->>'provider',COUNT(*) "
            "FROM policy_decisions AS policy "
            "JOIN proposed_actions AS action "
            "ON action.action_id=policy.action_id "
            "AND action.workspace_id=policy.workspace_id "
            "WHERE policy.workspace_id=%s "
            "GROUP BY decision,action.payload->>'provider'",
            (workspace_id,),
        ).fetchall():
            if decision not in {item.value for item in PolicyDecisionType}:
                raise PostgresTrustRepositoryError(
                    "Policy decision metrics contain an unsupported decision"
                )
            if provider not in {item.value for item in AgentProvider}:
                raise PostgresTrustRepositoryError(
                    "Policy decision metrics contain an unsupported provider"
                )
            policy_decision_counts[decision] += int(count)
            provider_decision_counts[(provider, decision)] = int(count)

        authorization_count = conn.execute(
            "SELECT COUNT(*) FROM action_authorizations WHERE workspace_id=%s",
            (workspace_id,),
        ).fetchone()[0]
        consumption_count = conn.execute(
            "SELECT COUNT(*) FROM action_consumptions WHERE workspace_id=%s",
            (workspace_id,),
        ).fetchone()[0]
        active_credentials, revoked_credentials = conn.execute(
            "SELECT COUNT(*) FILTER (WHERE NOT revoked AND expires_at>%s),"
            "COUNT(*) FILTER (WHERE revoked) FROM credential_grants "
            "WHERE workspace_id=%s",
            (now, workspace_id),
        ).fetchone()
        trust_event_count = conn.execute(
            "SELECT COUNT(*) FROM trust_events WHERE workspace_id=%s",
            (workspace_id,),
        ).fetchone()[0]

        push_status_counts = Counter()
        push_attempt_count = 0
        push_queue_depth = 0
        for status, count, attempts, queued in conn.execute(
            "SELECT status,COUNT(*),COALESCE(SUM(attempts),0),"
            "COUNT(*) FILTER (WHERE status IN ('PENDING','RETRY_WAIT','SENDING')) "
            "FROM notifications WHERE workspace_id=%s GROUP BY status",
            (workspace_id,),
        ).fetchall():
            push_status_counts[status] = int(count)
            push_attempt_count += int(attempts)
            push_queue_depth += int(queued)

        verification = trust_event_repository.verify_in_transaction(conn)
        return PostgresTrustMetricsSnapshot(
            lease_status_counts=dict(lease_status_counts),
            policy_decision_counts=dict(policy_decision_counts),
            provider_decision_counts=dict(provider_decision_counts),
            authorization_count=int(authorization_count),
            consumption_count=int(consumption_count),
            active_credential_count=int(active_credentials),
            revoked_credential_count=int(revoked_credentials),
            trust_event_count=int(trust_event_count),
            push_status_counts=dict(push_status_counts),
            push_attempt_count=push_attempt_count,
            push_queue_depth=push_queue_depth,
            verification=verification,
        )

    def list_exception_queue(
        self,
        conn: Connection,
        *,
        workspace_id: str,
    ) -> list[ExceptionQueueItem]:
        """Read exception rows from one workspace-scoped PostgreSQL snapshot.

        Callers provide a read-only transaction. Both run exceptions and
        non-ALLOW action decisions are joined and checked against their stored
        payloads so a stale or cross-workspace JSON record cannot be surfaced.
        """
        self._require_scope(workspace_id)
        if conn.autocommit:
            raise ValueError("Exception queue reads require a transaction")

        items: list[ExceptionQueueItem] = []
        run_rows = conn.execute(
            "SELECT run.run_id,run.workspace_id,run.task_id,run.project_id,"
            "run.status,run.updated_at,run.payload,task.task_id,task.workspace_id,"
            "task.project_id,task.payload FROM agent_runs AS run "
            "JOIN agent_tasks AS task ON task.task_id=run.task_id "
            "AND task.workspace_id=run.workspace_id "
            "WHERE run.workspace_id=%s AND run.status IN ('NEEDS_HUMAN','BLOCKED','FAILED')",
            (workspace_id,),
        ).fetchall()
        for row in run_rows:
            try:
                run = AgentRun.model_validate(row[6])
                task = AgentTask.model_validate(row[10])
            except Exception as error:
                raise PostgresTrustRepositoryError(
                    "Stored exception run or task payload is invalid"
                ) from error
            if (
                run.id != row[0]
                or run.workspace_id != row[1]
                or run.task_id != row[2]
                or run.project_id != row[3]
                or run.status.value != row[4]
                or run.updated_at != row[5]
                or task.id != row[7]
                or task.workspace_id != row[8]
                or task.project_id != row[9]
                or task.id != run.task_id
                or task.workspace_id != workspace_id
                or task.project_id != run.project_id
            ):
                raise PostgresTrustRepositoryError(
                    "Stored exception run or task scope is inconsistent"
                )
            items.append(
                ExceptionQueueItem(
                    id=f"run:{run.id}",
                    category=run.status.value,
                    severity=(
                        RiskLevel.CRITICAL
                        if run.status.value == "BLOCKED"
                        else RiskLevel.HIGH
                    ),
                    title=f"Agent run {run.status.value.replace('_', ' ').lower()}",
                    summary=run.last_error or run.objective or "Operator review is required.",
                    run_id=run.id,
                    task_id=run.task_id,
                    created_at=run.updated_at,
                )
            )

        action_rows = conn.execute(
            "SELECT action.action_id,action.workspace_id,action.lease_id,"
            "action.project_id,action.task_id,action.run_id,action.created_at,"
            "action.payload,decision.decision_id,decision.lease_id,decision.decision,"
            "decision.evaluated_at,decision.payload FROM proposed_actions AS action "
            "JOIN LATERAL (SELECT policy.decision_id,policy.lease_id,policy.decision,"
            "policy.evaluated_at,policy.payload FROM policy_decisions AS policy "
            "WHERE policy.action_id=action.action_id "
            "AND policy.workspace_id=action.workspace_id "
            "ORDER BY policy.evaluated_at DESC,policy.decision_id DESC LIMIT 1) AS decision ON TRUE "
            "WHERE action.workspace_id=%s AND decision.decision<>'ALLOW'",
            (workspace_id,),
        ).fetchall()
        for row in action_rows:
            try:
                action = ProposedAction.model_validate(row[7])
                decision = PolicyDecision.model_validate(row[12])
            except Exception as error:
                raise PostgresTrustRepositoryError(
                    "Stored exception action or decision payload is invalid"
                ) from error
            mismatches = [
                label
                for label, matches in (
                    ("action_id", action.id == row[0]),
                    ("action_workspace", action.workspace_id == row[1]),
                    ("action_lease", action.lease_id == row[2]),
                    ("action_project", action.project_id == row[3]),
                    ("action_task", action.task_id == row[4]),
                    ("action_run", action.run_id == row[5]),
                    ("action_created_at", action.created_at == row[6]),
                    ("decision_id", decision.id == row[8]),
                    ("decision_action", decision.proposed_action_id == action.id),
                    ("decision_lease", decision.lease_id == row[9]),
                    ("decision_type", decision.decision.value == row[10]),
                    ("decision_time", decision.evaluated_at == row[11]),
                )
                if not matches
            ]
            if action.workspace_id != workspace_id:
                mismatches.append("action_workspace_scope")
            if mismatches:
                raise PostgresTrustRepositoryError(
                    "Stored exception action or decision scope is inconsistent: "
                    + ",".join(mismatches)
                )
            items.append(
                ExceptionQueueItem(
                    id=f"action:{action.id}",
                    category=decision.decision.value,
                    severity=decision.risk_level,
                    title=(
                        "Agent action blocked"
                        if decision.decision == PolicyDecisionType.BLOCK
                        else "Agent action needs exact approval"
                    ),
                    summary=" ".join(decision.reasons),
                    run_id=action.run_id,
                    task_id=action.task_id,
                    action_id=action.id,
                    lease_id=action.lease_id,
                    created_at=decision.evaluated_at,
                )
            )
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    def list_reliability_scorecards(
        self,
        conn: Connection,
        *,
        workspace_id: str,
    ) -> list[AgentReliabilityScorecard]:
        """Build reliability scorecards entirely from one durable workspace snapshot."""
        self._require_scope(workspace_id)
        if conn.autocommit:
            raise ValueError("Scorecard reads require a transaction")

        actions: list[ProposedAction] = []
        action_rows = conn.execute(
            "SELECT action_id,workspace_id,lease_id,project_id,task_id,run_id,created_at,payload "
            "FROM proposed_actions WHERE workspace_id=%s",
            (workspace_id,),
        ).fetchall()
        for row in action_rows:
            try:
                action = ProposedAction.model_validate(row[7])
            except Exception as error:
                raise PostgresTrustRepositoryError(
                    "Stored scorecard action payload is invalid"
                ) from error
            if (
                action.id != row[0]
                or action.workspace_id != row[1]
                or action.lease_id != row[2]
                or action.project_id != row[3]
                or action.task_id != row[4]
                or action.run_id != row[5]
                or action.created_at != row[6]
                or row[1] != workspace_id
            ):
                raise PostgresTrustRepositoryError(
                    "Stored scorecard action scope is inconsistent"
                )
            actions.append(action)

        tasks: list[AgentTask] = []
        task_rows = conn.execute(
            "SELECT task_id,project_id,workspace_id,external_agent_id,status,updated_at,payload "
            "FROM agent_tasks WHERE workspace_id=%s",
            (workspace_id,),
        ).fetchall()
        for row in task_rows:
            try:
                task = AgentTask.model_validate(row[6])
            except Exception as error:
                raise PostgresTrustRepositoryError(
                    "Stored scorecard task payload is invalid"
                ) from error
            if (
                task.id != row[0]
                or task.project_id != row[1]
                or task.workspace_id != row[2]
                or task.external_agent_id != row[3]
                or task.status.value != row[4]
                or task.updated_at != row[5]
                or row[2] != workspace_id
            ):
                raise PostgresTrustRepositoryError(
                    "Stored scorecard task scope is inconsistent"
                )
            tasks.append(task)

        task_by_id = {task.id: task for task in tasks}
        decisions_by_action: dict[str, PolicyDecision] = {}
        decision_rows = conn.execute(
            "SELECT decision_id,action_id,workspace_id,lease_id,decision,evaluated_at,payload "
            "FROM policy_decisions WHERE workspace_id=%s",
            (workspace_id,),
        ).fetchall()
        for row in decision_rows:
            try:
                decision = PolicyDecision.model_validate(row[6])
            except Exception as error:
                raise PostgresTrustRepositoryError(
                    "Stored scorecard policy decision payload is invalid"
                ) from error
            if (
                decision.id != row[0]
                or decision.proposed_action_id != row[1]
                or decision.lease_id != row[3]
                or decision.decision.value != row[4]
                or decision.evaluated_at != row[5]
                or row[2] != workspace_id
            ):
                raise PostgresTrustRepositoryError(
                    "Stored scorecard policy decision scope is inconsistent"
                )
            action = next((item for item in actions if item.id == row[1]), None)
            if action is None or action.lease_id != decision.lease_id:
                raise PostgresTrustRepositoryError(
                    "Stored scorecard decision is detached from its workspace action"
                )
            previous = decisions_by_action.get(decision.proposed_action_id)
            if previous is None or (decision.evaluated_at, decision.id) > (
                previous.evaluated_at,
                previous.id,
            ):
                decisions_by_action[decision.proposed_action_id] = decision

        runs: list[AgentRun] = []
        run_rows = conn.execute(
            "SELECT run_id,workspace_id,task_id,project_id,status,updated_at,payload "
            "FROM agent_runs WHERE workspace_id=%s",
            (workspace_id,),
        ).fetchall()
        for row in run_rows:
            try:
                run = AgentRun.model_validate(row[6])
            except Exception as error:
                raise PostgresTrustRepositoryError(
                    "Stored scorecard run payload is invalid"
                ) from error
            task = task_by_id.get(row[2])
            if (
                run.id != row[0]
                or run.workspace_id != row[1]
                or run.task_id != row[2]
                or run.project_id != row[3]
                or run.status.value != row[4]
                or run.updated_at != row[5]
                or row[1] != workspace_id
                or task is None
                or task.project_id != run.project_id
            ):
                raise PostgresTrustRepositoryError(
                    "Stored scorecard run/task scope is inconsistent"
                )
            runs.append(run)

        run_by_id = {run.id: run for run in runs}
        steps_by_run: dict[str, list[AgentRunStep]] = {}
        step_rows = conn.execute(
            "SELECT step_id,run_id,task_id,workspace_id,status,sequence,payload "
            "FROM agent_run_steps WHERE workspace_id=%s",
            (workspace_id,),
        ).fetchall()
        for row in step_rows:
            try:
                step = AgentRunStep.model_validate(row[6])
            except Exception as error:
                raise PostgresTrustRepositoryError(
                    "Stored scorecard run-step payload is invalid"
                ) from error
            run = run_by_id.get(row[1])
            if (
                step.id != row[0]
                or step.run_id != row[1]
                or step.task_id != row[2]
                or step.status.value != row[4]
                or step.sequence != row[5]
                or run is None
                or run.task_id != step.task_id
                or row[3] != workspace_id
            ):
                raise PostgresTrustRepositoryError(
                    "Stored scorecard run-step scope is inconsistent"
                )
            steps_by_run.setdefault(step.run_id, []).append(step)

        recovered_run_ids: set[str] = set()
        event_rows = conn.execute(
            "SELECT event_id,run_id,task_id,workspace_id,created_at,event_type,payload "
            "FROM agent_run_events WHERE workspace_id=%s",
            (workspace_id,),
        ).fetchall()
        for row in event_rows:
            try:
                event = AgentRunEvent.model_validate(row[6])
            except Exception as error:
                raise PostgresTrustRepositoryError(
                    "Stored scorecard run-event payload is invalid"
                ) from error
            run = run_by_id.get(row[1])
            if (
                event.id != row[0]
                or event.run_id != row[1]
                or event.task_id != row[2]
                or event.created_at != row[4]
                or event.event_type.value != row[5]
                or row[3] != workspace_id
                or run is None
                or run.task_id != event.task_id
            ):
                raise PostgresTrustRepositoryError(
                    "Stored scorecard run-event scope is inconsistent"
                )
            if event.event_type.value == "STALE_LEASE_RECOVERED":
                recovered_run_ids.add(event.run_id)

        consumptions_by_action: dict[str, ActionConsumption] = {}
        consumption_rows = conn.execute(
            "SELECT action_id,workspace_id,lease_id,consumed_at,payload "
            "FROM action_consumptions WHERE workspace_id=%s",
            (workspace_id,),
        ).fetchall()
        action_by_id = {action.id: action for action in actions}
        for row in consumption_rows:
            payload = row[4]
            if (
                payload.get("action_id") != row[0]
                or payload.get("workspace_id") != row[1]
                or payload.get("lease_id") != row[2]
                or datetime.fromisoformat(payload.get("consumed_at", "")) != row[3]
                or row[1] != workspace_id
            ):
                raise PostgresTrustRepositoryError(
                    "Stored scorecard consumption scope is inconsistent"
                )
            action = action_by_id.get(row[0])
            if action is None or action.lease_id != row[2]:
                raise PostgresTrustRepositoryError(
                    "Stored scorecard consumption is detached from its workspace action"
                )
            try:
                consumptions_by_action[row[0]] = ActionConsumption.model_validate(
                    payload["consumption"]
                )
            except Exception as error:
                raise PostgresTrustRepositoryError(
                    "Stored scorecard consumption payload is invalid"
                ) from error

        keys = {(action.provider, action.agent_id) for action in actions}
        keys.update((task.provider, task.external_agent_id) for task in tasks)
        results: list[AgentReliabilityScorecard] = []
        for provider, agent_id in sorted(
            keys, key=lambda item: (item[0].value, item[1] or "")
        ):
            scoped_actions = [
                action
                for action in actions
                if action.provider == provider and action.agent_id == agent_id
            ]
            action_ids = {action.id for action in scoped_actions}
            decisions = [
                decisions_by_action[action_id]
                for action_id in action_ids
                if action_id in decisions_by_action
            ]
            scoped_tasks = [
                task
                for task in tasks
                if task.provider == provider and task.external_agent_id == agent_id
            ]
            task_ids = {task.id for task in scoped_tasks}
            scoped_runs = [run for run in runs if run.task_id in task_ids]
            run_ids = {run.id for run in scoped_runs}
            allowed = sum(
                decision.decision == PolicyDecisionType.ALLOW
                for decision in decisions
            )
            blocked = sum(
                decision.decision == PolicyDecisionType.BLOCK
                for decision in decisions
            )
            escalated = sum(
                decision.decision == PolicyDecisionType.REQUIRE_APPROVAL
                for decision in decisions
            )
            first_attempt_successes = sum(
                run.status == AgentRunStatus.SUCCEEDED
                and bool(steps_by_run.get(run.id))
                and all(step.attempt_count <= 1 for step in steps_by_run[run.id])
                for run in scoped_runs
            )
            recovered_runs = recovered_run_ids & run_ids
            recovered_successes = sum(
                run.id in recovered_runs and run.status.value == "SUCCEEDED"
                for run in scoped_runs
            )
            interventions = sum(
                run.status.value == "NEEDS_HUMAN" for run in scoped_runs
            )
            completed_with_evidence = sum(
                bool(run.final_evidence_hash) for run in scoped_runs
            )
            durations = [
                (run.completed_at - run.started_at).total_seconds()
                for run in scoped_runs
                if run.started_at and run.completed_at
            ]
            total_cost = sum(
                consumptions_by_action[action_id].actual_cost_usd
                for action_id in action_ids
                if action_id in consumptions_by_action
            )
            evaluated = len(decisions)
            results.append(
                AgentReliabilityScorecard(
                    provider=provider,
                    agent_id=agent_id,
                    evaluated_actions=evaluated,
                    allowed_actions=allowed,
                    blocked_actions=blocked,
                    approval_escalations=escalated,
                    scope_adherence_rate=allowed / evaluated if evaluated else 0.0,
                    first_attempt_success_rate=(
                        first_attempt_successes / len(scoped_runs)
                        if scoped_runs
                        else 0.0
                    ),
                    recovery_success_rate=(
                        recovered_successes / len(recovered_runs)
                        if recovered_runs
                        else 0.0
                    ),
                    human_intervention_rate=(
                        interventions / len(scoped_runs) if scoped_runs else 0.0
                    ),
                    policy_block_rate=blocked / evaluated if evaluated else 0.0,
                    evidence_completion_rate=(
                        completed_with_evidence / len(scoped_runs)
                        if scoped_runs
                        else 0.0
                    ),
                    average_runtime_seconds=(
                        sum(durations) / len(durations) if durations else 0.0
                    ),
                    total_cost_usd=total_cost,
                    confidence=min(1.0, (evaluated + len(scoped_runs)) / 30.0),
                )
            )
        return results

    @staticmethod
    def _insert(
        conn: Connection,
        *,
        table: str,
        columns: Sequence[str],
        values: Sequence[Any],
        record: Any,
    ) -> None:
        names = [*columns, "payload"]
        statement = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(table),
            sql.SQL(",").join(sql.Identifier(name) for name in names),
            sql.SQL(",").join(sql.Placeholder() for _ in names),
        )
        conn.execute(statement, (*values, Jsonb(_model_payload(record))))

    @staticmethod
    def _get(
        conn: Connection,
        *,
        table: str,
        id_column: str,
        record_id: str,
        workspace_id: str,
        model: type[T],
        columns: Sequence[tuple[str, str]],
        for_update: bool = False,
        for_share: bool = False,
    ) -> T | None:
        PostgresTrustRepository._require_scope(workspace_id)
        selected = [id_column, "workspace_id", *(column for column, _ in columns), "payload"]
        row = conn.execute(
            sql.SQL("SELECT {} FROM {} WHERE {}=%s AND workspace_id=%s{}").format(
                sql.SQL(",").join(sql.Identifier(name) for name in selected),
                sql.Identifier(table),
                sql.Identifier(id_column),
                sql.SQL(" FOR UPDATE") if for_update else (
                    sql.SQL(" FOR SHARE") if for_share else sql.SQL("")
                ),
            ),
            (record_id, workspace_id),
        ).fetchone()
        if row is None:
            return None
        try:
            record = model.model_validate(row[-1])
        except Exception as error:
            raise PostgresTrustRepositoryError("Stored trust payload is invalid") from error
        expected_id = getattr(record, "id", None)
        if expected_id != row[0]:
            raise PostgresTrustRepositoryError("Stored trust record identity is inconsistent")
        payload_workspace = getattr(record, "workspace_id", workspace_id)
        if payload_workspace != row[1] or payload_workspace != workspace_id:
            raise PostgresTrustRepositoryError("Stored trust record workspace is inconsistent")
        for index, (_, attribute) in enumerate(columns, start=2):
            expected = getattr(record, attribute)
            actual = row[index]
            if isinstance(expected, float) and isinstance(actual, Decimal):
                actual = float(actual)
            if expected != actual:
                raise PostgresTrustRepositoryError(
                    "Stored trust payload does not match its indexed columns"
                )
        return record

    def insert_capability_lease(self, conn: Connection, record: CapabilityLease) -> None:
        workspace_id = record.workspace_id
        if not workspace_id:
            raise ValueError("Capability lease workspace is required")
        self._insert(
            conn,
            table="capability_leases",
            columns=("lease_id", "approval_id", "workspace_id", "issued_by_user_id", "status", "expires_at", "consumed_pull_requests", "consumed_retries", "consumed_runtime_seconds", "consumed_cost_usd", "created_at", "updated_at"),
            values=(record.id, record.approval_request_id, workspace_id, record.issued_by_user_id, record.status.value, record.expires_at, record.consumed_pull_requests, record.consumed_retries, record.consumed_runtime_seconds, Decimal(str(record.consumed_cost_usd)), record.created_at, record.updated_at),
            record=record,
        )

    def active_capability_lease_for_approval(
        self,
        conn: Connection,
        *,
        approval_id: str,
        workspace_id: str,
        for_update: bool = False,
    ) -> CapabilityLease | None:
        """Read and optionally lock the single active lease for an approval."""
        self._require_scope(workspace_id)
        rows = conn.execute(
            "SELECT lease_id,approval_id,workspace_id,issued_by_user_id,status,expires_at,"
            "consumed_pull_requests,consumed_retries,consumed_runtime_seconds,"
            "consumed_cost_usd,created_at,updated_at,payload FROM capability_leases "
            "WHERE approval_id=%s AND workspace_id=%s AND status=%s "
            "ORDER BY lease_id LIMIT 2" + (" FOR UPDATE" if for_update else ""),
            (approval_id, workspace_id, CapabilityLeaseStatus.ACTIVE.value),
        ).fetchall()
        if len(rows) > 1:
            raise PostgresTrustRepositoryError(
                "Approval has multiple active capability leases"
            )
        if not rows:
            return None
        lease = self._capability_lease_from_row(rows[0])
        if lease.approval_request_id != approval_id or lease.workspace_id != workspace_id:
            raise PostgresTrustRepositoryError(
                "Capability lease is outside the requested approval scope"
            )
        return lease

    @staticmethod
    def _capability_lease_from_row(row: tuple[Any, ...]) -> CapabilityLease:
        try:
            lease = CapabilityLease.model_validate(row[12])
        except Exception as error:
            raise PostgresTrustRepositoryError(
                "Stored capability lease payload is invalid"
            ) from error
        indexed = (
            lease.id,
            lease.approval_request_id,
            lease.workspace_id,
            lease.issued_by_user_id,
            lease.status.value,
            lease.expires_at,
            lease.consumed_pull_requests,
            lease.consumed_retries,
            lease.consumed_runtime_seconds,
            Decimal(str(lease.consumed_cost_usd)),
            lease.created_at,
            lease.updated_at,
        )
        actual = tuple(row[:12])
        if indexed != actual:
            raise PostgresTrustRepositoryError(
                "Capability lease payload does not match its indexed columns"
            )
        return lease

    def list_capability_leases(
        self,
        conn: Connection,
        *,
        workspace_id: str,
        now: datetime,
        trust_event_repository: PostgresTrustEventRepository,
        status: CapabilityLeaseStatus | None = None,
        approval_id: str | None = None,
        agent_id: str | None = None,
    ) -> list[CapabilityLease]:
        """Refresh elapsed leases and list verified records in one transaction."""
        self._require_scope(workspace_id)
        filters = ["workspace_id=%s"]
        values: list[Any] = [workspace_id]
        if approval_id is not None:
            filters.append("approval_id=%s")
            values.append(approval_id)
        if agent_id is not None:
            filters.append("payload->>'agent_id'=%s")
            values.append(agent_id)
        where = " AND ".join(filters)
        columns = (
            "lease_id,approval_id,workspace_id,issued_by_user_id,status,expires_at,"
            "consumed_pull_requests,consumed_retries,consumed_runtime_seconds,"
            "consumed_cost_usd,created_at,updated_at,payload"
        )
        expired_rows = conn.execute(
            f"SELECT {columns} FROM capability_leases WHERE {where} "
            "AND status=%s AND expires_at<=%s ORDER BY lease_id FOR UPDATE",
            (*values, CapabilityLeaseStatus.ACTIVE.value, now),
        ).fetchall()
        for row in expired_rows:
            lease = self._capability_lease_from_row(row)
            self.expire_active_capability_lease(
                conn,
                lease=lease,
                now=now,
                trust_event_repository=trust_event_repository,
            )

        if status is not None:
            filters.append("status=%s")
            values.append(status.value)
        rows = conn.execute(
            f"SELECT {columns} FROM capability_leases WHERE {' AND '.join(filters)} "
            "ORDER BY created_at DESC,lease_id",
            values,
        ).fetchall()
        return [self._capability_lease_from_row(row) for row in rows]

    def expire_active_capability_lease(
        self,
        conn: Connection,
        *,
        lease: CapabilityLease,
        now: datetime,
        trust_event_repository: PostgresTrustEventRepository,
    ) -> CapabilityLease:
        """Expire a locked active lease and append its event in the same transaction."""
        if (
            not lease.workspace_id
            or lease.status != CapabilityLeaseStatus.ACTIVE
            or lease.expires_at > now
        ):
            raise PostgresTrustRepositoryConflict(
                "Only an elapsed active capability lease can be expired."
            )
        updated = lease.model_copy(
            update={"status": CapabilityLeaseStatus.EXPIRED, "updated_at": now}
        )
        changed = conn.execute(
            "UPDATE capability_leases SET status=%s,updated_at=%s,payload=%s "
            "WHERE lease_id=%s AND workspace_id=%s AND status=%s AND expires_at<=%s",
            (
                updated.status.value,
                updated.updated_at,
                Jsonb(_model_payload(updated)),
                lease.id,
                lease.workspace_id,
                CapabilityLeaseStatus.ACTIVE.value,
                now,
            ),
        )
        if changed.rowcount != 1:
            raise PostgresTrustRepositoryConflict(
                "Capability lease changed while it was being expired."
            )
        trust_event_repository.append_in_transaction(
            conn,
            TrustEventType.LEASE_EXPIRED,
            entity_type="capability_lease",
            entity_id=lease.id,
            actor="system",
            workspace_id=lease.workspace_id,
            correlation_id=lease.scope.metadata.get("correlation_id"),
            payload={"expires_at": lease.expires_at},
        )
        return updated

    def get_capability_lease(
        self,
        conn: Connection,
        lease_id: str,
        workspace_id: str,
        *,
        for_update: bool = False,
        for_share: bool = False,
    ) -> CapabilityLease | None:
        return self._get(
            conn,
            table="capability_leases",
            id_column="lease_id",
            record_id=lease_id,
            workspace_id=workspace_id,
            model=CapabilityLease,
            columns=(("approval_id", "approval_request_id"), ("issued_by_user_id", "issued_by_user_id"), ("status", "status"), ("expires_at", "expires_at"), ("consumed_pull_requests", "consumed_pull_requests"), ("consumed_retries", "consumed_retries"), ("consumed_runtime_seconds", "consumed_runtime_seconds"), ("consumed_cost_usd", "consumed_cost_usd"), ("created_at", "created_at"), ("updated_at", "updated_at")),
            for_update=for_update,
            for_share=for_share,
        )

    def revoke_capability_lease(
        self,
        conn: Connection,
        *,
        lease_id: str,
        workspace_id: str,
        actor_user_id: str,
        actor: str,
        reason: str,
        now: datetime,
        trust_event_repository: PostgresTrustEventRepository,
    ) -> CapabilityLease:
        """Revoke a lease and its child grants atomically with the trust event."""
        self._require_scope(workspace_id)
        if not actor_user_id or not actor or len(reason.strip()) < 3:
            raise ValueError("Lease revocation requires actor and reason")
        lease = self.get_capability_lease(
            conn, lease_id, workspace_id, for_update=True
        )
        if lease is None:
            raise ValueError("Capability lease not found")
        if lease.status in {CapabilityLeaseStatus.REVOKED, CapabilityLeaseStatus.EXPIRED}:
            return lease
        if lease.status != CapabilityLeaseStatus.ACTIVE:
            raise PostgresTrustRepositoryConflict(
                f"Lease cannot be revoked from {lease.status}."
            )

        if lease.expires_at <= now:
            updated = lease.model_copy(
                update={"status": CapabilityLeaseStatus.EXPIRED, "updated_at": now}
            )
            event_type = TrustEventType.LEASE_EXPIRED
            event_payload = {"expires_at": lease.expires_at}
        else:
            updated = lease.model_copy(
                update={
                    "status": CapabilityLeaseStatus.REVOKED,
                    "revoked_at": now,
                    "revoked_by_user_id": actor_user_id,
                    "revocation_reason": reason.strip(),
                    "updated_at": now,
                }
            )
            event_type = TrustEventType.LEASE_REVOKED
            event_payload = {"reason": reason.strip()}

        if event_type == TrustEventType.LEASE_REVOKED:
            grant_rows = conn.execute(
                "SELECT grant_id,lease_id,workspace_id,revoked,payload "
                "FROM credential_grants WHERE lease_id=%s AND workspace_id=%s "
                "ORDER BY grant_id FOR UPDATE",
                (lease_id, workspace_id),
            ).fetchall()
            for row in grant_rows:
                try:
                    grant = CredentialGrant.model_validate(row[4])
                except Exception as error:
                    raise PostgresTrustRepositoryError(
                        "Stored credential grant payload is invalid"
                    ) from error
                if (
                    grant.id != row[0]
                    or grant.lease_id != row[1]
                    or grant.workspace_id != row[2]
                    or grant.revoked != row[3]
                ):
                    raise PostgresTrustRepositoryError(
                        "Stored credential grant scope or state is inconsistent"
                    )
                if grant.revoked:
                    continue
                revoked_grant = grant.model_copy(
                    update={"revoked": True, "revoked_at": now}
                )
                conn.execute(
                    "UPDATE credential_grants SET revoked=TRUE,payload=%s "
                    "WHERE grant_id=%s AND lease_id=%s AND workspace_id=%s AND revoked=FALSE",
                    (
                        Jsonb(_model_payload(revoked_grant)),
                        grant.id,
                        lease_id,
                        workspace_id,
                    ),
                )

        changed = conn.execute(
            "UPDATE capability_leases SET status=%s,updated_at=%s,payload=%s "
            "WHERE lease_id=%s AND workspace_id=%s AND status=%s",
            (
                updated.status.value,
                updated.updated_at,
                Jsonb(_model_payload(updated)),
                lease_id,
                workspace_id,
                CapabilityLeaseStatus.ACTIVE.value,
            ),
        )
        if changed.rowcount != 1:
            raise PostgresTrustRepositoryConflict(
                "Capability lease changed during revocation"
            )
        trust_event_repository.append_in_transaction(
            conn,
            event_type,
            entity_type="capability_lease",
            entity_id=lease.id,
            actor=actor,
            workspace_id=workspace_id,
            payload=event_payload,
        )
        return updated

    def insert_reviewer_attestation(self, conn: Connection, record: ReviewerAttestation) -> None:
        if not record.workspace_id:
            raise ValueError("Reviewer attestation workspace is required")
        self._insert(
            conn,
            table="reviewer_attestations",
            columns=("attestation_id", "approval_id", "workspace_id", "reviewer_user_id", "approval_payload_hash", "decision", "created_at"),
            values=(record.id, record.approval_request_id, record.workspace_id, record.reviewer_user_id, record.approval_payload_hash, record.decision.value, record.created_at),
            record=record,
        )

    def get_reviewer_attestation(
        self, conn: Connection, attestation_id: str, workspace_id: str
    ) -> ReviewerAttestation | None:
        return self._get(
            conn,
            table="reviewer_attestations",
            id_column="attestation_id",
            record_id=attestation_id,
            workspace_id=workspace_id,
            model=ReviewerAttestation,
            columns=(("approval_id", "approval_request_id"), ("reviewer_user_id", "reviewer_user_id"), ("approval_payload_hash", "approval_payload_hash"), ("decision", "decision"), ("created_at", "created_at")),
        )

    def list_reviewer_attestations(
        self,
        conn: Connection,
        *,
        workspace_id: str,
        approval_request_id: str | None = None,
        reviewer_user_id: str | None = None,
        approval_payload_hash: str | None = None,
    ) -> list[ReviewerAttestation]:
        self._require_scope(workspace_id)
        filters = ["workspace_id=%s"]
        values: list[Any] = [workspace_id]
        for column, value in (
            ("approval_id", approval_request_id),
            ("reviewer_user_id", reviewer_user_id),
            ("approval_payload_hash", approval_payload_hash),
        ):
            if value is not None:
                filters.append(f"{column}=%s")
                values.append(value)
        rows = conn.execute(
            "SELECT attestation_id,approval_id,workspace_id,reviewer_user_id,"
            "approval_payload_hash,decision,created_at,payload FROM reviewer_attestations "
            f"WHERE {' AND '.join(filters)} ORDER BY created_at DESC,attestation_id",
            values,
        ).fetchall()
        records: list[ReviewerAttestation] = []
        for row in rows:
            try:
                record = ReviewerAttestation.model_validate(row[7])
            except Exception as error:
                raise PostgresTrustRepositoryError(
                    "Stored reviewer attestation payload is invalid"
                ) from error
            if (
                record.id != row[0]
                or record.approval_request_id != row[1]
                or record.workspace_id != row[2]
                or record.reviewer_user_id != row[3]
                or record.approval_payload_hash != row[4]
                or record.decision.value != row[5]
                or record.created_at != row[6]
            ):
                raise PostgresTrustRepositoryError(
                    "Stored reviewer attestation does not match its indexed columns"
                )
            records.append(record)
        return records

    def insert_proposed_action(self, conn: Connection, record: ProposedAction) -> None:
        if not record.workspace_id:
            raise ValueError("Proposed action workspace is required")
        self._insert(
            conn,
            table="proposed_actions",
            columns=("action_id", "workspace_id", "lease_id", "project_id", "task_id", "run_id", "created_at"),
            values=(record.id, record.workspace_id, record.lease_id, record.project_id, record.task_id, record.run_id, record.created_at),
            record=record,
        )

    def get_proposed_action(
        self,
        conn: Connection,
        action_id: str,
        workspace_id: str,
        *,
        for_update: bool = False,
    ) -> ProposedAction | None:
        return self._get(
            conn,
            table="proposed_actions",
            id_column="action_id",
            record_id=action_id,
            workspace_id=workspace_id,
            model=ProposedAction,
            columns=(("lease_id", "lease_id"), ("project_id", "project_id"), ("task_id", "task_id"), ("run_id", "run_id"), ("created_at", "created_at")),
            for_update=for_update,
        )

    def evaluate_proposed_action(
        self,
        conn: Connection,
        *,
        action: ProposedAction,
        actor: str,
        now: datetime,
        trust_event_repository: PostgresTrustEventRepository,
        expected_agent_id: str | None = None,
    ) -> tuple[PolicyDecision, CapabilityLease | None]:
        """Persist one standalone action evaluation and its ledger events atomically."""
        workspace_id = action.workspace_id
        if not workspace_id:
            raise ValueError("PostgreSQL action evaluation requires workspace scope")
        if not actor or conn.autocommit:
            raise ValueError("Action evaluation requires an actor and transaction")
        if action.run_id:
            raise PostgresTrustRepositoryConflict(
                "Run-linked action evaluation requires the durable AgentRun execution transaction"
            )

        if action.task_id:
            if not self.task_actions_enabled():
                raise PostgresTrustRepositoryConflict(
                    "PostgreSQL task-only actions are disabled"
                )
            task, approval = self._lock_approved_task_authority(
                conn, task_id=action.task_id, workspace_id=workspace_id
            )
            if (
                action.workspace_id != workspace_id
                or action.project_id != task.project_id
                or action.provider != task.provider
                or action.agent_id != task.external_agent_id
                or action.repository != task.repository
                or action.branch != task.branch_preference
                or action.metadata.get("approval_request_id") != approval.id
                or action.metadata.get("approved_payload_hash") != approval.approved_payload_hash
            ):
                raise PostgresTrustRepositoryConflict(
                    "Proposed action does not match its exact approved task payload and scope"
                )

        lease: CapabilityLease | None = None
        if action.lease_id:
            lease = self.get_capability_lease(
                conn, action.lease_id, workspace_id, for_update=True
            )
            if lease is None:
                raise ValueError("Capability lease not found")
            if (
                expected_agent_id is not None
                and lease.agent_id
                and lease.agent_id != expected_agent_id
            ):
                raise PostgresTrustRepositoryConflict(
                    "Capability lease belongs to another external agent"
                )
            if action.task_id:
                task = AgentTask.model_validate(
                    conn.execute(
                        "SELECT payload FROM agent_tasks WHERE task_id=%s AND workspace_id=%s",
                        (action.task_id, workspace_id),
                    ).fetchone()[0]
                )
                approval = ApprovalRequest.model_validate(
                    conn.execute(
                        "SELECT payload FROM approval_requests WHERE approval_id=%s AND workspace_id=%s",
                        (task.approval_request_id, workspace_id),
                    ).fetchone()[0]
                )
                if (
                    lease.status != CapabilityLeaseStatus.ACTIVE
                    or lease.approval_request_id != approval.id
                    or lease.approved_payload_hash != approval.approved_payload_hash
                    or lease.provider != task.provider
                    or lease.agent_id != task.external_agent_id
                    or lease.scope.repository != task.repository
                    or lease.scope.branch != task.branch_preference
                    or not verify_payload_signature(lease_receipt_payload(lease), lease.receipt_signature)
                ):
                    raise PostgresTrustRepositoryConflict(
                        "Capability lease does not match the approved task scope"
                    )
        elif action.task_id:
            raise PostgresTrustRepositoryConflict("Task action requires an active capability lease")

        decision = evaluate_action_policy(action, lease, now=now)
        self.insert_proposed_action(conn, action)
        self.insert_policy_decision(conn, decision, workspace_id=workspace_id)
        correlation_id = action.run_id or action.task_id
        trust_event_repository.append_in_transaction(
            conn,
            TrustEventType.ACTION_RECEIVED,
            entity_type="proposed_action",
            entity_id=action.id,
            actor=actor,
            workspace_id=workspace_id,
            correlation_id=correlation_id,
            payload={
                "provider": action.provider,
                "agent_id": action.agent_id,
                "action_type": action.action_type,
                "lease_id": action.lease_id,
            },
        )
        event_type = {
            PolicyDecisionType.ALLOW: TrustEventType.ACTION_ALLOWED,
            PolicyDecisionType.REQUIRE_APPROVAL: TrustEventType.ACTION_REQUIRES_APPROVAL,
            PolicyDecisionType.BLOCK: TrustEventType.ACTION_BLOCKED,
        }[decision.decision]
        trust_event_repository.append_in_transaction(
            conn,
            event_type,
            entity_type="proposed_action",
            entity_id=action.id,
            actor=actor,
            workspace_id=workspace_id,
            correlation_id=correlation_id,
            payload={
                "decision_id": decision.id,
                "decision": decision.decision,
                "risk_level": decision.risk_level,
                "reasons": decision.reasons,
                "obligations": decision.obligations,
            },
        )
        return decision, lease

    def insert_policy_decision(
        self, conn: Connection, record: PolicyDecision, *, workspace_id: str
    ) -> None:
        self._require_scope(workspace_id)
        self._insert(
            conn,
            table="policy_decisions",
            columns=("decision_id", "action_id", "workspace_id", "lease_id", "decision", "evaluated_at"),
            values=(record.id, record.proposed_action_id, workspace_id, record.lease_id, record.decision.value, record.evaluated_at),
            record=record,
        )

    def get_policy_decision(
        self, conn: Connection, decision_id: str, workspace_id: str
    ) -> PolicyDecision | None:
        return self._get(
            conn,
            table="policy_decisions",
            id_column="decision_id",
            record_id=decision_id,
            workspace_id=workspace_id,
            model=PolicyDecision,
            columns=(("action_id", "proposed_action_id"), ("lease_id", "lease_id"), ("decision", "decision"), ("evaluated_at", "evaluated_at")),
        )

    def get_latest_policy_decision_for_action(
        self,
        conn: Connection,
        action_id: str,
        workspace_id: str,
        *,
        for_update: bool = False,
    ) -> PolicyDecision | None:
        self._require_scope(workspace_id)
        row = conn.execute(
            "SELECT decision_id,action_id,workspace_id,lease_id,decision,evaluated_at,payload "
            "FROM policy_decisions WHERE action_id=%s AND workspace_id=%s "
            "ORDER BY evaluated_at DESC,decision_id DESC LIMIT 1" +
            (" FOR UPDATE" if for_update else ""),
            (action_id, workspace_id),
        ).fetchone()
        if row is None:
            return None
        try:
            decision = PolicyDecision.model_validate(row[6])
        except Exception as error:
            raise PostgresTrustRepositoryError("Stored policy decision payload is invalid") from error
        if (
            decision.id != row[0]
            or decision.proposed_action_id != row[1]
            or decision.lease_id != row[3]
            or decision.decision.value != row[4]
            or decision.evaluated_at != row[5]
            or row[2] != workspace_id
        ):
            raise PostgresTrustRepositoryError("Stored policy decision scope is inconsistent")
        return decision

    def insert_action_authorization(self, conn: Connection, record: ActionAuthorization) -> None:
        if not record.workspace_id:
            raise ValueError("Action authorization workspace is required")
        self._insert(
            conn,
            table="action_authorizations",
            columns=("authorization_id", "action_id", "workspace_id", "policy_decision_id", "reviewer_user_id", "decision", "action_payload_hash", "created_at"),
            values=(record.id, record.action_id, record.workspace_id, record.policy_decision_id, record.reviewer_user_id, record.decision.value, record.action_payload_hash, record.created_at),
            record=record,
        )

    def get_action_authorization(
        self, conn: Connection, authorization_id: str, workspace_id: str
    ) -> ActionAuthorization | None:
        return self._get(
            conn,
            table="action_authorizations",
            id_column="authorization_id",
            record_id=authorization_id,
            workspace_id=workspace_id,
            model=ActionAuthorization,
            columns=(("action_id", "action_id"), ("policy_decision_id", "policy_decision_id"), ("reviewer_user_id", "reviewer_user_id"), ("decision", "decision"), ("action_payload_hash", "action_payload_hash"), ("created_at", "created_at")),
        )

    def get_action_authorization_for_action(
        self,
        conn: Connection,
        action_id: str,
        workspace_id: str,
        *,
        for_update: bool = False,
    ) -> ActionAuthorization | None:
        self._require_scope(workspace_id)
        row = conn.execute(
            "SELECT authorization_id,action_id,workspace_id,policy_decision_id,"
            "reviewer_user_id,decision,action_payload_hash,created_at,payload "
            "FROM action_authorizations WHERE action_id=%s AND workspace_id=%s" +
            (" FOR UPDATE" if for_update else ""),
            (action_id, workspace_id),
        ).fetchone()
        if row is None:
            return None
        try:
            authorization = ActionAuthorization.model_validate(row[8])
        except Exception as error:
            raise PostgresTrustRepositoryError("Stored action authorization payload is invalid") from error
        if (
            authorization.id != row[0]
            or authorization.action_id != row[1]
            or authorization.workspace_id != row[2]
            or authorization.policy_decision_id != row[3]
            or authorization.reviewer_user_id != row[4]
            or authorization.decision.value != row[5]
            or authorization.action_payload_hash != row[6]
            or authorization.created_at != row[7]
            or row[2] != workspace_id
        ):
            raise PostgresTrustRepositoryError("Stored action authorization scope is inconsistent")
        return authorization

    def insert_action_consumption(
        self,
        conn: Connection,
        *,
        action_id: str,
        lease_id: str,
        workspace_id: str,
        consumed_at: datetime,
        consumption: ActionConsumption,
    ) -> None:
        self._require_scope(workspace_id)
        payload = {
            "action_id": action_id,
            "lease_id": lease_id,
            "workspace_id": workspace_id,
            "consumed_at": consumed_at.isoformat(),
            "consumption": _model_payload(consumption),
        }
        conn.execute(
            "INSERT INTO action_consumptions(action_id,workspace_id,lease_id,consumed_at,payload) "
            "VALUES(%s,%s,%s,%s,%s)",
            (action_id, workspace_id, lease_id, consumed_at, Jsonb(payload)),
        )

    def get_action_consumption(
        self, conn: Connection, action_id: str, workspace_id: str
    ) -> ActionConsumption | None:
        self._require_scope(workspace_id)
        row = conn.execute(
            "SELECT action_id,workspace_id,lease_id,consumed_at,payload "
            "FROM action_consumptions WHERE action_id=%s AND workspace_id=%s",
            (action_id, workspace_id),
        ).fetchone()
        if row is None:
            return None
        if (
            row[4].get("action_id") != row[0]
            or row[4].get("workspace_id") != row[1]
            or row[4].get("lease_id") != row[2]
            or datetime.fromisoformat(row[4].get("consumed_at", "")) != row[3]
        ):
            raise PostgresTrustRepositoryError("Stored action consumption scope is inconsistent")
        try:
            return ActionConsumption.model_validate(row[4]["consumption"])
        except Exception as error:
            raise PostgresTrustRepositoryError("Stored action consumption payload is invalid") from error

    def insert_credential_grant(self, conn: Connection, record: CredentialGrant) -> None:
        if not record.workspace_id:
            raise ValueError("Credential grant workspace is required")
        self._insert(
            conn,
            table="credential_grants",
            columns=("grant_id", "lease_id", "workspace_id", "grant_type", "audience", "subject", "expires_at", "revoked", "token_hash"),
            values=(record.id, record.lease_id, record.workspace_id, record.grant_type.value, record.audience, record.subject, record.expires_at, record.revoked, record.token_hash),
            record=record,
        )

    def get_credential_grant(
        self,
        conn: Connection,
        grant_id: str,
        workspace_id: str,
        *,
        for_update: bool = False,
        for_share: bool = False,
    ) -> CredentialGrant | None:
        return self._get(
            conn,
            table="credential_grants",
            id_column="grant_id",
            record_id=grant_id,
            workspace_id=workspace_id,
            model=CredentialGrant,
            columns=(("lease_id", "lease_id"), ("grant_type", "grant_type"), ("audience", "audience"), ("subject", "subject"), ("expires_at", "expires_at"), ("revoked", "revoked"), ("token_hash", "token_hash")),
            for_update=for_update,
            for_share=for_share,
        )

    def get_credential_grant_by_token_hash(
        self, conn: Connection, token_hash: str
    ) -> tuple[CredentialGrant, CapabilityLease | None] | None:
        """Resolve a bearer hash without exposing or indexing the raw token."""
        if len(token_hash) != 64:
            return None
        row = conn.execute(
            "SELECT grant_id,workspace_id,lease_id FROM credential_grants WHERE token_hash=%s",
            (token_hash,),
        ).fetchone()
        if row is None:
            return None
        grant_id, workspace_id, lease_id = row
        # Match the lease-then-grant order used by lease revocation. Shared locks
        # keep the returned grant/lease state stable through caller evaluation.
        lease = self.get_capability_lease(
            conn, lease_id, workspace_id, for_share=True
        )
        grant = self.get_credential_grant(
            conn, grant_id, workspace_id, for_share=True
        )
        if grant is None:
            raise PostgresTrustRepositoryError(
                "Credential grant disappeared during token lookup"
            )
        if lease is None or grant.lease_id != lease.id:
            raise PostgresTrustRepositoryError(
                "Credential grant lease scope is inconsistent"
            )
        return grant, lease

    def issue_credential_grant(
        self,
        conn: Connection,
        *,
        record: CredentialGrant,
        actor_user_id: str,
        actor: str,
        now: datetime,
        trust_event_repository: PostgresTrustEventRepository,
    ) -> CredentialGrant:
        """Insert a grant only under an active lease and append its event atomically."""
        workspace_id = record.workspace_id
        self._require_scope(workspace_id)
        if not actor_user_id or not actor or conn.autocommit:
            raise ValueError("Credential issuance requires actor and transaction")
        if record.metadata.get("issued_by_user_id") != actor_user_id:
            raise ValueError("Credential grant issuer does not match authenticated actor")
        lease = self.get_capability_lease(
            conn, record.lease_id, workspace_id, for_update=True
        )
        if lease is None:
            raise ValueError("Capability lease not found")
        if lease.status != CapabilityLeaseStatus.ACTIVE or lease.expires_at <= now:
            raise PostgresTrustRepositoryConflict(
                "Credentials require an active, unexpired capability lease"
            )
        if not verify_payload_signature(
            lease_receipt_payload(lease), lease.receipt_signature
        ):
            raise PostgresTrustRepositoryConflict(
                "Credential issuance requires a valid capability lease receipt"
            )
        if record.expires_at <= now or record.expires_at > lease.expires_at:
            raise PostgresTrustRepositoryConflict(
                "Credential grant must expire before its capability lease"
            )
        self.insert_credential_grant(conn, record)
        trust_event_repository.append_in_transaction(
            conn,
            TrustEventType.CREDENTIAL_GRANT_ISSUED,
            entity_type="credential_grant",
            entity_id=record.id,
            actor=actor,
            workspace_id=workspace_id,
            correlation_id=record.lease_id,
            payload={
                "lease_id": record.lease_id,
                "grant_type": record.grant_type,
                "audience": record.audience,
                "subject": record.subject,
                "scope": record.scope,
                "expires_at": record.expires_at.isoformat(),
            },
        )
        return record

    def revoke_credential_grant(
        self,
        conn: Connection,
        *,
        grant_id: str,
        workspace_id: str,
        actor_user_id: str,
        actor: str,
        reason: str,
        now: datetime,
        trust_event_repository: PostgresTrustEventRepository,
    ) -> CredentialGrant:
        """Revoke one credential grant and append its event in one transaction."""
        self._require_scope(workspace_id)
        if not actor_user_id or not actor or len(reason.strip()) < 3:
            raise ValueError("Credential revocation requires actor and reason")
        if conn.autocommit:
            raise ValueError("Credential revocation requires a transaction")
        grant = self.get_credential_grant(
            conn, grant_id, workspace_id, for_update=True
        )
        if grant is None:
            raise ValueError("Credential grant not found")
        if grant.revoked:
            return grant

        updated = grant.model_copy(
            update={
                "revoked": True,
                "revoked_at": now,
                "metadata": {
                    **grant.metadata,
                    "revoked_by_user_id": actor_user_id,
                    "revocation_reason": reason.strip(),
                },
            }
        )
        changed = conn.execute(
            "UPDATE credential_grants SET revoked=TRUE,payload=%s "
            "WHERE grant_id=%s AND workspace_id=%s AND revoked=FALSE",
            (Jsonb(_model_payload(updated)), grant_id, workspace_id),
        )
        if changed.rowcount != 1:
            raise PostgresTrustRepositoryConflict(
                "Credential grant changed during revocation"
            )
        trust_event_repository.append_in_transaction(
            conn,
            TrustEventType.CREDENTIAL_GRANT_REVOKED,
            entity_type="credential_grant",
            entity_id=updated.id,
            actor=actor,
            workspace_id=workspace_id,
            correlation_id=updated.lease_id,
            payload={"lease_id": updated.lease_id, "reason": reason.strip()},
        )
        return updated

    def consume_effective_action(
        self,
        conn: Connection,
        *,
        action_id: str,
        workspace_id: str,
        consumption: ActionConsumption,
        actor: str,
        now: datetime,
    ) -> ActionConsumption:
        """Consume one allowed action, its lease budget, and event atomically.

        The caller owns the transaction. Locks are acquired in action-then-lease
        order; different actions sharing a lease serialize on the lease row.
        """
        self._require_scope(workspace_id)
        if not actor:
            raise ValueError("Action consumption requires an actor")
        if conn.autocommit:
            raise ValueError("Trust consumption requires a transaction-enabled connection")

        action = self.get_proposed_action(conn, action_id, workspace_id)
        if action is None:
            raise PostgresTrustRepositoryConflict("Proposed action not found")
        if action.task_id and not action.run_id:
            if not self.task_actions_enabled():
                raise PostgresTrustRepositoryConflict(
                    "PostgreSQL task-only actions are disabled"
                )
            action, _task, _approval = self.lock_task_action_binding(
                conn,
                action_id=action_id,
                workspace_id=workspace_id,
                task_id=action.task_id,
            )
        else:
            action = self.get_proposed_action(
                conn, action_id, workspace_id, for_update=True
            )
            if action is None:
                raise PostgresTrustRepositoryConflict("Proposed action not found")
        if not action.lease_id:
            raise PostgresTrustRepositoryConflict("Allowed action has no capability lease")

        decision_row = conn.execute(
            "SELECT decision_id,action_id,workspace_id,lease_id,decision,evaluated_at,payload "
            "FROM policy_decisions WHERE action_id=%s AND workspace_id=%s "
            "ORDER BY evaluated_at DESC,decision_id DESC LIMIT 1 FOR SHARE",
            (action.id, workspace_id),
        ).fetchone()
        if decision_row is None:
            raise PostgresTrustRepositoryConflict("Action has no durable policy decision")
        try:
            decision = PolicyDecision.model_validate(decision_row[6])
        except Exception as error:
            raise PostgresTrustRepositoryError("Stored policy decision payload is invalid") from error
        if (
            decision.id != decision_row[0]
            or decision.proposed_action_id != decision_row[1]
            or decision.lease_id != decision_row[3]
            or decision.decision.value != decision_row[4]
            or decision.evaluated_at != decision_row[5]
            or decision.proposed_action_id != action.id
            or decision.lease_id != action.lease_id
        ):
            raise PostgresTrustRepositoryError("Policy decision identity or scope is inconsistent")
        if decision.decision != PolicyDecisionType.ALLOW:
            raise PostgresTrustRepositoryConflict("Only an effective ALLOW decision can be consumed")

        if "+human-decision" in decision.evaluated_policy_version:
            authorization_row = conn.execute(
                "SELECT authorization_id,action_id,workspace_id,policy_decision_id,"
                "reviewer_user_id,decision,action_payload_hash,created_at,payload "
                "FROM action_authorizations WHERE action_id=%s AND workspace_id=%s FOR SHARE",
                (action.id, workspace_id),
            ).fetchone()
            if authorization_row is None:
                raise PostgresTrustRepositoryConflict(
                    "Human-authorized ALLOW is missing its signed exact-action decision"
                )
            try:
                authorization = ActionAuthorization.model_validate(authorization_row[8])
            except Exception as error:
                raise PostgresTrustRepositoryError(
                    "Stored action authorization payload is invalid"
                ) from error
            signature_payload = {
                "action_id": authorization.action_id,
                "action_payload_hash": authorization.action_payload_hash,
                "policy_decision_id": authorization.policy_decision_id,
                "reviewer_user_id": authorization.reviewer_user_id,
                "reviewer_role": authorization.reviewer_role,
                "decision": authorization.decision,
                "reason": authorization.reason,
            }
            if (
                authorization.id != authorization_row[0]
                or authorization.action_id != action.id
                or authorization.workspace_id != workspace_id
                or authorization.policy_decision_id != authorization_row[3]
                or authorization.reviewer_user_id != authorization_row[4]
                or authorization.decision.value != authorization_row[5]
                or authorization.action_payload_hash != authorization_row[6]
                or authorization.created_at != authorization_row[7]
                or authorization.decision != ActionAuthorizationDecision.ALLOW
                or authorization.action_payload_hash != sha256_hex(action.model_dump(mode="json"))
                or not verify_payload_signature(signature_payload, authorization.signature)
            ):
                raise PostgresTrustRepositoryConflict(
                    "Human authorization signature, scope, decision, or action hash is invalid"
                )
            prior_decision_row = conn.execute(
                "SELECT action_id,workspace_id,decision,payload FROM policy_decisions "
                "WHERE decision_id=%s AND action_id=%s AND workspace_id=%s FOR SHARE",
                (authorization.policy_decision_id, action.id, workspace_id),
            ).fetchone()
            if prior_decision_row is None:
                raise PostgresTrustRepositoryConflict("Human authorization references no policy decision")
            try:
                prior_decision = PolicyDecision.model_validate(prior_decision_row[3])
            except Exception as error:
                raise PostgresTrustRepositoryError("Prior policy decision payload is invalid") from error
            if prior_decision.decision != PolicyDecisionType.REQUIRE_APPROVAL:
                raise PostgresTrustRepositoryConflict(
                    "Human authorization does not reference a REQUIRE_APPROVAL decision"
                )

        task_action_lease: CapabilityLease | None = None
        if action.task_id and not action.run_id:
            task_row = conn.execute(
                "SELECT payload FROM agent_tasks WHERE task_id=%s AND workspace_id=%s",
                (action.task_id, workspace_id),
            ).fetchone()
            if task_row is None:
                raise PostgresTrustRepositoryConflict("AgentTask is not visible in this workspace")
            task = AgentTask.model_validate(task_row[0])
            approval_row = conn.execute(
                "SELECT payload FROM approval_requests WHERE approval_id=%s AND workspace_id=%s",
                (task.approval_request_id, workspace_id),
            ).fetchone()
            if approval_row is None:
                raise PostgresTrustRepositoryConflict("Task approval is not visible in this workspace")
            approval = ApprovalRequest.model_validate(approval_row[0])
            task_action_lease = self.lock_task_action_lease(
                conn, action=action, task=task, approval=approval
            )

        existing = self.get_action_consumption(conn, action.id, workspace_id)
        if existing is not None:
            if existing != consumption:
                raise PostgresTrustRepositoryConflict("Action consumption is immutable once recorded")
            event = conn.execute(
                "SELECT event_id FROM trust_events WHERE workspace_id=%s "
                "AND event_type=%s AND entity_type='proposed_action' AND entity_id=%s",
                (workspace_id, TrustEventType.ACTION_CONSUMED.value, action.id),
            ).fetchall()
            if len(event) != 1:
                raise PostgresTrustRepositoryError(
                    "Consumed action does not have exactly one durable trust event"
                )
            return existing

        lease = task_action_lease or self.get_capability_lease(
            conn, action.lease_id, workspace_id, for_update=True
        )
        if lease is None:
            raise PostgresTrustRepositoryConflict("Capability lease is missing")
        if lease.status != CapabilityLeaseStatus.ACTIVE or lease.expires_at <= now:
            raise PostgresTrustRepositoryConflict("Capability lease is not active")
        if action.task_id and not action.run_id:
            task_row = conn.execute(
                "SELECT task_id,project_id,workspace_id,approval_request_id,status,payload "
                "FROM agent_tasks WHERE task_id=%s AND workspace_id=%s FOR SHARE",
                (action.task_id, workspace_id),
            ).fetchone()
            if task_row is None:
                raise PostgresTrustRepositoryConflict("AgentTask is not visible in this workspace")
            task = AgentTask.model_validate(task_row[5])
            approval_row = conn.execute(
                "SELECT approval_id,project_id,workspace_id,status,payload FROM approval_requests "
                "WHERE approval_id=%s AND workspace_id=%s FOR SHARE",
                (task.approval_request_id, workspace_id),
            ).fetchone()
            if approval_row is None:
                raise PostgresTrustRepositoryConflict("Task approval is not visible in this workspace")
            approval = ApprovalRequest.model_validate(approval_row[4])
            if (
                task.status != AgentTaskStatus.APPROVED
                or task.workspace_id != workspace_id
                or task.project_id != action.project_id
                or task.approval_request_id != approval.id
                or approval.status != ApprovalStatus.APPROVED
                or approval.workspace_id != workspace_id
                or approval.project_id != task.project_id
                or not approval.approved_payload_hash
                or approval.approved_payload_hash != compute_approval_payload_hash(approval)
                or action.metadata.get("approval_request_id") != approval.id
                or action.metadata.get("approved_payload_hash") != approval.approved_payload_hash
                or lease.approval_request_id != approval.id
                or lease.approved_payload_hash != approval.approved_payload_hash
                or lease.provider != task.provider
                or lease.agent_id != task.external_agent_id
                or lease.scope.repository != task.repository
                or lease.scope.branch != task.branch_preference
                or not verify_payload_signature(lease_receipt_payload(lease), lease.receipt_signature)
            ):
                raise PostgresTrustRepositoryConflict(
                    "Task action, approval, or signed capability lease changed before consumption"
                )

        projected_runtime = lease.consumed_runtime_seconds + consumption.actual_runtime_seconds
        projected_cost = lease.consumed_cost_usd + consumption.actual_cost_usd
        projected_prs = lease.consumed_pull_requests + consumption.pull_requests_created
        projected_retries = lease.consumed_retries + int(consumption.retry_consumed)
        if projected_runtime > lease.scope.max_runtime_seconds:
            raise PostgresTrustRepositoryConflict("Actual runtime exceeds capability budget")
        if projected_cost > lease.scope.max_cost_usd:
            raise PostgresTrustRepositoryConflict("Actual cost exceeds capability budget")
        if projected_prs > lease.scope.max_pull_requests:
            raise PostgresTrustRepositoryConflict("Pull-request count exceeds capability budget")
        if projected_retries > lease.scope.max_retries:
            raise PostgresTrustRepositoryConflict("Retry count exceeds capability budget")

        updated_lease = lease.model_copy(
            update={
                "consumed_runtime_seconds": projected_runtime,
                "consumed_cost_usd": projected_cost,
                "consumed_pull_requests": projected_prs,
                "consumed_retries": projected_retries,
                "updated_at": now,
            }
        )
        changed = conn.execute(
            "UPDATE capability_leases SET consumed_pull_requests=%s,consumed_retries=%s,"
            "consumed_runtime_seconds=%s,consumed_cost_usd=%s,updated_at=%s,payload=%s "
            "WHERE lease_id=%s AND workspace_id=%s AND status='ACTIVE'",
            (
                updated_lease.consumed_pull_requests,
                updated_lease.consumed_retries,
                updated_lease.consumed_runtime_seconds,
                Decimal(str(updated_lease.consumed_cost_usd)),
                updated_lease.updated_at,
                Jsonb(_model_payload(updated_lease)),
                lease.id,
                workspace_id,
            ),
        )
        if changed.rowcount != 1:
            raise PostgresTrustRepositoryConflict("Capability lease changed during consumption")
        self.insert_action_consumption(
            conn,
            action_id=action.id,
            lease_id=lease.id,
            workspace_id=workspace_id,
            consumed_at=now,
            consumption=consumption,
        )
        from .postgres_trust_event_repository import PostgresTrustEventRepository

        PostgresTrustEventRepository(lambda: conn).append_in_transaction(
            conn,
            TrustEventType.ACTION_CONSUMED,
            entity_type="proposed_action",
            entity_id=action.id,
            actor=actor,
            workspace_id=workspace_id,
            correlation_id=action.run_id or action.task_id,
            payload={
                "lease_id": lease.id,
                "effective_policy_decision_id": decision.id,
                "actual_runtime_seconds": consumption.actual_runtime_seconds,
                "actual_cost_usd": consumption.actual_cost_usd,
                "pull_requests_created": consumption.pull_requests_created,
                "retry_consumed": consumption.retry_consumed,
                "output_reference": consumption.output_reference,
                "evidence_hash": sha256_hex(consumption.evidence),
            },
        )
        return consumption

    def consume_task_action(
        self,
        conn: Connection,
        *,
        action_id: str,
        workspace_id: str,
        task_id: str,
        consumption: ActionConsumption,
        actor: str,
        now: datetime,
        expected_agent_id: str | None = None,
    ) -> ActionConsumption:
        """Consume an approved task action once, under the task binding lock."""
        if not self.task_actions_enabled():
            raise PostgresTrustRepositoryConflict("PostgreSQL task-only actions are disabled")
        action, _task, _approval = self.lock_task_action_binding(
            conn,
            action_id=action_id,
            workspace_id=workspace_id,
            task_id=task_id,
        )
        if expected_agent_id is not None and action.agent_id != expected_agent_id:
            raise PostgresTrustRepositoryConflict("Agent does not own this task action")
        return self.consume_effective_action(
            conn,
            action_id=action_id,
            workspace_id=workspace_id,
            consumption=consumption,
            actor=actor,
            now=now,
        )

    def consume_agent_run_action(
        self,
        conn: Connection,
        *,
        action_id: str,
        workspace_id: str,
        run_id: str,
        step_id: str,
        consumption: ActionConsumption,
        actor: str,
        now: datetime,
    ) -> ActionConsumption:
        """Consume an action only when its durable run and step binding matches.

        This primitive deliberately does not advance an AgentRun or authorize a
        worker side effect. The authenticated route must verify membership.
        Callers must join it to run transitions and worker effects in a
        separately reviewed transaction before enabling execution. Locks use
        run → step → action → policy/authorization → capability lease order.
        """
        self._require_scope(workspace_id)
        if not run_id or not step_id:
            raise ValueError("AgentRun action consumption requires run and step identity")
        self.lock_agent_run_action_binding(
            conn,
            action_id=action_id,
            workspace_id=workspace_id,
            run_id=run_id,
            step_id=step_id,
        )
        return self.consume_effective_action(
            conn,
            action_id=action_id,
            workspace_id=workspace_id,
            consumption=consumption,
            actor=actor,
            now=now,
        )
