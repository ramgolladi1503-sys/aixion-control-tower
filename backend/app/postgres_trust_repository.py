from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, TypeVar

from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from .agent_run_models import AgentRun, AgentRunStep
from .agent_task_models import AgentTask
from .trust_action_authorization_models import (
    ActionAuthorization,
    ActionAuthorizationDecision,
)
from .trust_crypto import sha256_hex, verify_payload_signature
from .models import AgentProvider, RiskLevel
from .trust_models import (
    ActionConsumption,
    CapabilityLease,
    CapabilityLeaseStatus,
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
        if action.run_id or action.task_id:
            raise PostgresTrustRepositoryConflict(
                "Run-linked action evaluation requires the durable AgentRun execution transaction"
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

        lease = self.get_capability_lease(
            conn, action.lease_id, workspace_id, for_update=True
        )
        if lease is None:
            raise PostgresTrustRepositoryConflict("Capability lease is missing")
        if lease.status != CapabilityLeaseStatus.ACTIVE or lease.expires_at <= now:
            raise PostgresTrustRepositoryConflict("Capability lease is not active")

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
