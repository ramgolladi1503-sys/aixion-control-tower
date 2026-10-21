from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, TypeVar

from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from .trust_action_authorization_models import (
    ActionAuthorization,
    ActionAuthorizationDecision,
)
from .trust_crypto import sha256_hex, verify_payload_signature
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
)
from .postgres_trust_event_repository import PostgresTrustEventRepository
from .trust_policy import evaluate_proposed_action as evaluate_action_policy

T = TypeVar("T")


class PostgresTrustRepositoryError(RuntimeError):
    """A PostgreSQL trust record is missing, malformed, or out of scope."""


class PostgresTrustRepositoryConflict(RuntimeError):
    """A requested trust operation conflicts with durable policy or scope."""


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
    ) -> T | None:
        PostgresTrustRepository._require_scope(workspace_id)
        selected = [id_column, "workspace_id", *(column for column, _ in columns), "payload"]
        row = conn.execute(
            sql.SQL("SELECT {} FROM {} WHERE {}=%s AND workspace_id=%s{}").format(
                sql.SQL(",").join(sql.Identifier(name) for name in selected),
                sql.Identifier(table),
                sql.Identifier(id_column),
                sql.SQL(" FOR UPDATE") if for_update else sql.SQL(""),
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

    def get_capability_lease(
        self,
        conn: Connection,
        lease_id: str,
        workspace_id: str,
        *,
        for_update: bool = False,
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
        self, conn: Connection, grant_id: str, workspace_id: str
    ) -> CredentialGrant | None:
        return self._get(
            conn,
            table="credential_grants",
            id_column="grant_id",
            record_id=grant_id,
            workspace_id=workspace_id,
            model=CredentialGrant,
            columns=(("lease_id", "lease_id"), ("grant_type", "grant_type"), ("audience", "audience"), ("subject", "subject"), ("expires_at", "expires_at"), ("revoked", "revoked"), ("token_hash", "token_hash")),
        )

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
