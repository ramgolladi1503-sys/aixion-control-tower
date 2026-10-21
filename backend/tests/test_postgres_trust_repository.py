from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace

import psycopg
import pytest
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app.application_repositories import ApplicationRepositories
from app.models import (
    ApprovalRequest,
    ApprovalStatus,
    AuthUser,
    Project,
    RiskAssessment,
    RiskLevel,
    User,
    UserRole,
    Workspace,
    now_utc,
)
from app.postgres_database import run_postgres_migrations
from app.postgres_trust_repository import (
    PostgresTrustRepository,
    PostgresTrustRepositoryConflict,
)
from app.postgres_trust_event_repository import (
    PostgresTrustEventRepository,
    PostgresTrustEventRepositoryError,
)
from app.store import store
from app.trust_routes import consume_manual_action
from app.trust_action_authorization_models import (
    ActionAuthorization,
    ActionAuthorizationDecision,
)
from app.trust_models import (
    ActionConsumption,
    CapabilityActionType,
    CapabilityLease,
    CapabilityLeaseMode,
    CapabilityScope,
    CredentialGrant,
    CredentialGrantType,
    CapabilityLeaseStatus,
    PolicyDecision,
    PolicyDecisionType,
    ProposedAction,
    ReviewerAttestation,
    ReviewerAttestationDecision,
    TrustEventType,
)


@pytest.fixture
def trust_repository() -> tuple[PostgresTrustRepository, Callable[[], Connection], str, str, str, str]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL integration tests")
    schema = f"ct_trust_repo_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    workspace_id = f"workspace_{uuid.uuid4().hex}"
    project_id = f"project_{uuid.uuid4().hex}"
    user_id = f"user_{uuid.uuid4().hex}"
    approval_id = f"approval_{uuid.uuid4().hex}"
    workspace = Workspace(id=workspace_id, name="Trust test", owner_user_id=user_id)
    user = User(
        id=user_id,
        email=f"{user_id}@example.test",
        role=UserRole.OWNER,
        password_hash="test-password-hash",
        password_salt="test-password-salt",
        workspace_id=workspace_id,
    )
    project = Project(id=project_id, name="Trust test project", workspace_id=workspace_id)
    now = now_utc()
    approval = ApprovalRequest(
        id=approval_id,
        project_id=project_id,
        title="Trust approval",
        summary="Approved action context",
        target_branch="feature/trust-test",
        files=[],
        risk=RiskAssessment(level=RiskLevel.MEDIUM),
        status=ApprovalStatus.APPROVED,
        workspace_id=workspace_id,
        created_at=now,
        updated_at=now,
    )
    try:
        run_postgres_migrations(connect)
        with connect() as conn:
            conn.execute(
                "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
                (workspace_id, Jsonb(workspace.model_dump(mode="json"))),
            )
            conn.execute(
                "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,"
                "email_verified,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    user_id, workspace_id, user.email, user.email.lower(), user.role.value,
                    user.disabled, user.email_verified, Jsonb(user.model_dump(mode="json")),
                ),
            )
            conn.execute(
                "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
                (project_id, workspace_id, Jsonb(project.model_dump(mode="json"))),
            )
            conn.execute(
                "INSERT INTO approval_requests(approval_id,project_id,workspace_id,work_order_id,"
                "source_agent_id,status,expires_at,created_at,updated_at,version,source_invocation_id,payload) "
                "VALUES(%s,%s,%s,NULL,NULL,%s,NULL,%s,%s,0,NULL,%s)",
                (
                    approval_id, project_id, workspace_id, approval.status.value,
                    approval.created_at, approval.updated_at,
                    Jsonb(approval.model_dump(mode="json")),
                ),
            )
        yield PostgresTrustRepository(), connect, workspace_id, user_id, project_id, approval_id
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_postgres_trust_repository_round_trips_workspace_records(trust_repository):
    repository, connect, workspace_id, user_id, project_id, approval_id = trust_repository
    now = now_utc()
    lease = CapabilityLease(
        id=f"lease_{uuid.uuid4().hex}",
        approval_request_id=approval_id,
        workspace_id=workspace_id,
        approved_payload_hash="sha256:" + ("a" * 64),
        mode=CapabilityLeaseMode.BOUNDED,
        scope=CapabilityScope(
            repository="owner/repo",
            branch="feature/trust-test",
            allowed_actions=[CapabilityActionType.RUN_COMMAND],
        ),
        issued_by_user_id=user_id,
        expires_at=now + timedelta(minutes=5),
        receipt_nonce="nonce",
        receipt_signature="signature",
    )
    attestation = ReviewerAttestation(
        approval_request_id=approval_id,
        workspace_id=workspace_id,
        approval_payload_hash="sha256:" + ("b" * 64),
        reviewer_user_id=user_id,
        reviewer_role=UserRole.OWNER,
        decision=ReviewerAttestationDecision.APPROVE,
        signature="attestation-signature",
    )
    action = ProposedAction(
        id=f"action_{uuid.uuid4().hex}",
        lease_id=lease.id,
        project_id=project_id,
        workspace_id=workspace_id,
        action_type=CapabilityActionType.RUN_COMMAND,
        command="git status --short",
    )
    decision = PolicyDecision(
        proposed_action_id=action.id,
        lease_id=lease.id,
        decision=PolicyDecisionType.REQUIRE_APPROVAL,
        risk_level=RiskLevel.MEDIUM,
    )
    authorization = ActionAuthorization(
        action_id=action.id,
        workspace_id=workspace_id,
        action_payload_hash="sha256:" + ("c" * 64),
        policy_decision_id=decision.id,
        reviewer_user_id=user_id,
        reviewer_role=UserRole.OWNER,
        decision=ActionAuthorizationDecision.ALLOW,
        reason="Approved exact action",
        signature="authorization-signature",
    )
    consumption = ActionConsumption(
        actual_runtime_seconds=3,
        actual_cost_usd=0.01,
        evidence={"result": "completed"},
    )
    grant = CredentialGrant(
        lease_id=lease.id,
        workspace_id=workspace_id,
        grant_type=CredentialGrantType.INTERNAL_CAPABILITY_TOKEN,
        audience="agent-runner",
        subject="agent-1",
        expires_at=now + timedelta(minutes=2),
        token_hash="d" * 64,
    )

    with connect() as conn:
        with conn.transaction():
            repository.insert_capability_lease(conn, lease)
            repository.insert_reviewer_attestation(conn, attestation)
            repository.insert_proposed_action(conn, action)
            repository.insert_policy_decision(conn, decision, workspace_id=workspace_id)
            repository.insert_action_authorization(conn, authorization)
            repository.insert_action_consumption(
                conn,
                action_id=action.id,
                lease_id=lease.id,
                workspace_id=workspace_id,
                consumed_at=now,
                consumption=consumption,
            )
            repository.insert_credential_grant(conn, grant)

            assert repository.get_capability_lease(conn, lease.id, workspace_id) == lease
            assert repository.get_reviewer_attestation(conn, attestation.id, workspace_id) == attestation
            assert repository.get_proposed_action(conn, action.id, workspace_id) == action
            assert repository.get_policy_decision(conn, decision.id, workspace_id) == decision
            assert repository.get_action_authorization(conn, authorization.id, workspace_id) == authorization
            assert repository.get_action_consumption(conn, action.id, workspace_id) == consumption
            assert repository.get_credential_grant(conn, grant.id, workspace_id) == grant
        assert repository.get_capability_lease(conn, lease.id, "wrong-workspace") is None


def test_postgres_trust_repository_writes_roll_back_with_callers_transaction(trust_repository):
    repository, connect, workspace_id, user_id, project_id, approval_id = trust_repository
    lease = CapabilityLease(
        id=f"lease_{uuid.uuid4().hex}",
        approval_request_id=approval_id,
        workspace_id=workspace_id,
        approved_payload_hash="sha256:" + ("e" * 64),
        mode=CapabilityLeaseMode.BOUNDED,
        scope=CapabilityScope(
            repository="owner/repo",
            branch="feature/trust-test",
            allowed_actions=[CapabilityActionType.RUN_COMMAND],
        ),
        issued_by_user_id=user_id,
        expires_at=now_utc() + timedelta(minutes=5),
        receipt_nonce="nonce-rollback",
        receipt_signature="signature",
    )
    action = ProposedAction(
        lease_id=lease.id,
        project_id=project_id,
        workspace_id=workspace_id,
        action_type=CapabilityActionType.RUN_COMMAND,
        command="git status",
    )

    with pytest.raises(RuntimeError, match="rollback trust unit"):
        with connect() as conn:
            with conn.transaction():
                repository.insert_capability_lease(conn, lease)
                repository.insert_proposed_action(conn, action)
                raise RuntimeError("rollback trust unit")

    with connect() as conn:
        assert repository.get_capability_lease(conn, lease.id, workspace_id) is None
        assert repository.get_proposed_action(conn, action.id, workspace_id) is None


def _lease_with_runtime_budget(
    *, workspace_id: str, user_id: str, approval_id: str, runtime_budget: int
) -> CapabilityLease:
    return CapabilityLease(
        id=f"lease_{uuid.uuid4().hex}",
        approval_request_id=approval_id,
        workspace_id=workspace_id,
        approved_payload_hash="sha256:" + ("f" * 64),
        mode=CapabilityLeaseMode.BOUNDED,
        scope=CapabilityScope(
            repository="owner/repo",
            branch="feature/trust-test",
            allowed_actions=[CapabilityActionType.RUN_COMMAND],
            max_runtime_seconds=runtime_budget,
        ),
        issued_by_user_id=user_id,
        expires_at=now_utc() + timedelta(minutes=5),
        receipt_nonce=uuid.uuid4().hex,
        receipt_signature="signature",
    )


def test_postgres_lease_revocation_updates_grants_and_appends_one_event(trust_repository):
    repository, connect, workspace_id, user_id, _project_id, approval_id = trust_repository
    events = PostgresTrustEventRepository(connect)
    lease = _lease_with_runtime_budget(
        workspace_id=workspace_id, user_id=user_id, approval_id=approval_id, runtime_budget=5
    )
    grants = [
        CredentialGrant(
            lease_id=lease.id,
            workspace_id=workspace_id,
            grant_type=CredentialGrantType.INTERNAL_CAPABILITY_TOKEN,
            audience=f"runner-{index}",
            subject="agent-1",
            expires_at=now_utc() + timedelta(minutes=5),
            token_hash=uuid.uuid4().hex,
        )
        for index in range(2)
    ]
    with connect() as conn:
        with conn.transaction():
            repository.insert_capability_lease(conn, lease)
            for grant in grants:
                repository.insert_credential_grant(conn, grant)

    with connect() as conn:
        with conn.transaction():
            revoked = repository.revoke_capability_lease(
                conn,
                lease_id=lease.id,
                workspace_id=workspace_id,
                actor_user_id=user_id,
                actor="owner@example.test",
                reason="Operator requested revoke",
                now=now_utc(),
                trust_event_repository=events,
            )
    assert revoked.status == CapabilityLeaseStatus.REVOKED
    assert revoked.revoked_by_user_id == user_id
    assert revoked.revocation_reason == "Operator requested revoke"
    with connect() as conn:
        durable = repository.get_capability_lease(conn, lease.id, workspace_id)
        assert durable == revoked
        rows = conn.execute(
            "SELECT grant_id,revoked,payload FROM credential_grants "
            "WHERE lease_id=%s AND workspace_id=%s ORDER BY grant_id",
            (lease.id, workspace_id),
        ).fetchall()
        assert len(rows) == 2
        assert all(row[1] and row[2]["revoked"] for row in rows)
        assert conn.execute(
            "SELECT count(*) FROM trust_events WHERE entity_id=%s AND event_type=%s",
            (lease.id, TrustEventType.LEASE_REVOKED.value),
        ).fetchone()[0] == 1
    assert events.verify().valid


def test_postgres_lease_revocation_is_idempotent_and_hides_other_workspace(
    trust_repository,
):
    repository, connect, workspace_id, user_id, _project_id, approval_id = trust_repository
    events = PostgresTrustEventRepository(connect)
    lease = _lease_with_runtime_budget(
        workspace_id=workspace_id, user_id=user_id, approval_id=approval_id, runtime_budget=5
    )
    with connect() as conn:
        with conn.transaction():
            repository.insert_capability_lease(conn, lease)

    def revoke(scope: str):
        with connect() as conn:
            with conn.transaction():
                return repository.revoke_capability_lease(
                    conn,
                    lease_id=lease.id,
                    workspace_id=scope,
                    actor_user_id=user_id,
                    actor="owner@example.test",
                    reason="Operator requested revoke",
                    now=now_utc(),
                    trust_event_repository=events,
                )

    with pytest.raises(ValueError, match="not found"):
        revoke("different-workspace")
    first = revoke(workspace_id)
    second = revoke(workspace_id)
    assert first.status == second.status == CapabilityLeaseStatus.REVOKED
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM trust_events WHERE entity_id=%s AND event_type=%s",
            (lease.id, TrustEventType.LEASE_REVOKED.value),
        ).fetchone()[0] == 1


def test_postgres_lease_revocation_rolls_back_on_trust_event_failure(trust_repository):
    repository, connect, workspace_id, user_id, _project_id, approval_id = trust_repository
    events = PostgresTrustEventRepository(connect)
    lease = _lease_with_runtime_budget(
        workspace_id=workspace_id, user_id=user_id, approval_id=approval_id, runtime_budget=5
    )
    with connect() as conn:
        with conn.transaction():
            repository.insert_capability_lease(conn, lease)
    with connect() as conn:
        conn.execute(
            "UPDATE trust_event_heads SET event_hash=%s WHERE singleton=TRUE", ("0" * 64,)
        )

    with pytest.raises(PostgresTrustEventRepositoryError, match="empty ledger"):
        with connect() as conn:
            with conn.transaction():
                repository.revoke_capability_lease(
                    conn,
                    lease_id=lease.id,
                    workspace_id=workspace_id,
                    actor_user_id=user_id,
                    actor="owner@example.test",
                    reason="Operator requested revoke",
                    now=now_utc(),
                    trust_event_repository=events,
                )
    with connect() as conn:
        persisted = repository.get_capability_lease(conn, lease.id, workspace_id)
        assert persisted is not None and persisted.status == CapabilityLeaseStatus.ACTIVE
        assert conn.execute("SELECT count(*) FROM trust_events").fetchone()[0] == 0


def _action_with_allow_decision(
    repository: PostgresTrustRepository,
    conn: Connection,
    *,
    lease: CapabilityLease,
    workspace_id: str,
    project_id: str,
) -> ProposedAction:
    action = ProposedAction(
        lease_id=lease.id,
        project_id=project_id,
        workspace_id=workspace_id,
        action_type=CapabilityActionType.RUN_COMMAND,
        command="git status",
    )
    repository.insert_proposed_action(conn, action)
    repository.insert_policy_decision(
        conn,
        PolicyDecision(
            proposed_action_id=action.id,
            lease_id=lease.id,
            decision=PolicyDecisionType.ALLOW,
            risk_level=RiskLevel.LOW,
        ),
        workspace_id=workspace_id,
    )
    return action


def test_postgres_manual_consumption_route_uses_durable_trust_state(
    trust_repository,
    monkeypatch: pytest.MonkeyPatch,
):
    repository, connect, workspace_id, user_id, project_id, approval_id = trust_repository
    lease = _lease_with_runtime_budget(
        workspace_id=workspace_id, user_id=user_id, approval_id=approval_id, runtime_budget=5
    )
    with connect() as conn:
        with conn.transaction():
            repository.insert_capability_lease(conn, lease)
            action = _action_with_allow_decision(
                repository,
                conn,
                lease=lease,
                workspace_id=workspace_id,
                project_id=project_id,
            )

    repositories = ApplicationRepositories.from_connector(connect)
    state = SimpleNamespace(
        postgres_runtime=SimpleNamespace(connect=connect),
        postgres_repositories=repositories,
        approval_decision_repository=repositories.approval_decisions,
        workspace_membership_repository=repositories.workspace_membership,
        trust_repository=repositories.trust,
        trust_event_repository=repositories.trust_events,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    user = AuthUser(
        id=user_id,
        email=f"{user_id}@example.test",
        display_name="Trust route maintainer",
        role=UserRole.MAINTAINER,
        workspace_id=workspace_id,
        email_verified=True,
    )
    monkeypatch.setattr(
        "app.trust_routes.workspace_id_for_request",
        lambda _user, _request: workspace_id,
    )
    consumption = ActionConsumption(actual_runtime_seconds=1, evidence={"result": "ok"})

    first = consume_manual_action(action.id, consumption, user, request)
    retry = consume_manual_action(action.id, consumption, user, request)

    assert first == retry == consumption
    assert action.id not in store.proposed_actions
    assert action.id not in store.action_consumptions
    with connect() as conn:
        assert repository.get_action_consumption(conn, action.id, workspace_id) == consumption
        assert conn.execute(
            "SELECT count(*) FROM trust_events WHERE workspace_id=%s "
            "AND event_type=%s AND entity_id=%s",
            (workspace_id, TrustEventType.ACTION_CONSUMED.value, action.id),
        ).fetchone()[0] == 1


def test_postgres_consumption_serializes_shared_lease_budget_and_is_idempotent(
    trust_repository,
):
    repository, connect, workspace_id, user_id, project_id, approval_id = trust_repository
    lease = _lease_with_runtime_budget(
        workspace_id=workspace_id,
        user_id=user_id,
        approval_id=approval_id,
        runtime_budget=1,
    )
    with connect() as conn:
        with conn.transaction():
            repository.insert_capability_lease(conn, lease)
            actions = [
                _action_with_allow_decision(
                    repository,
                    conn,
                    lease=lease,
                    workspace_id=workspace_id,
                    project_id=project_id,
                )
                for _ in range(2)
            ]

    def consume(action: ProposedAction):
        consumption = ActionConsumption(actual_runtime_seconds=1)
        try:
            with connect() as conn:
                with conn.transaction():
                    repository.consume_effective_action(
                        conn,
                        action_id=action.id,
                        workspace_id=workspace_id,
                        consumption=consumption,
                        actor="runner",
                        now=now_utc(),
                    )
            return action.id, True, consumption
        except PostgresTrustRepositoryConflict:
            return action.id, False, consumption

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(consume, actions))

    successful = [outcome for outcome in outcomes if outcome[1]]
    rejected = [outcome for outcome in outcomes if not outcome[1]]
    assert len(successful) == 1
    assert len(rejected) == 1
    successful_action_id, _, successful_consumption = successful[0]

    with connect() as conn:
        durable_lease = repository.get_capability_lease(conn, lease.id, workspace_id)
        assert durable_lease is not None
        assert durable_lease.consumed_runtime_seconds == 1
        assert repository.get_action_consumption(
            conn, successful_action_id, workspace_id
        ) == successful_consumption
        assert repository.get_action_consumption(
            conn, rejected[0][0], workspace_id
        ) is None
        assert conn.execute("SELECT count(*) FROM action_consumptions").fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM trust_events WHERE event_type='ACTION_CONSUMED'"
        ).fetchone() == (1,)
        # Repeating the exact same request is an idempotent read of the durable result.
        with conn.transaction():
            assert repository.consume_effective_action(
                conn,
                action_id=successful_action_id,
                workspace_id=workspace_id,
                consumption=successful_consumption,
                actor="runner",
                now=now_utc(),
            ) == successful_consumption
    assert PostgresTrustEventRepository(connect).verify().valid is True


def test_postgres_consumption_rolls_back_budget_and_record_if_event_append_fails(
    trust_repository,
):
    repository, connect, workspace_id, user_id, project_id, approval_id = trust_repository
    lease = _lease_with_runtime_budget(
        workspace_id=workspace_id,
        user_id=user_id,
        approval_id=approval_id,
        runtime_budget=5,
    )
    with connect() as conn:
        with conn.transaction():
            repository.insert_capability_lease(conn, lease)
            action = _action_with_allow_decision(
                repository,
                conn,
                lease=lease,
                workspace_id=workspace_id,
                project_id=project_id,
            )
    with connect() as conn:
        conn.execute(
            "UPDATE trust_event_heads SET event_hash=%s WHERE singleton=TRUE",
            ("0" * 64,),
        )

    with pytest.raises(PostgresTrustEventRepositoryError, match="empty ledger"):
        with connect() as conn:
            with conn.transaction():
                repository.consume_effective_action(
                    conn,
                    action_id=action.id,
                    workspace_id=workspace_id,
                    consumption=ActionConsumption(actual_runtime_seconds=3),
                    actor="runner",
                    now=now_utc(),
                )

    with connect() as conn:
        durable_lease = repository.get_capability_lease(conn, lease.id, workspace_id)
        assert durable_lease is not None
        assert durable_lease.consumed_runtime_seconds == 0
        assert repository.get_action_consumption(conn, action.id, workspace_id) is None
        assert conn.execute("SELECT count(*) FROM action_consumptions").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM trust_events").fetchone() == (0,)
