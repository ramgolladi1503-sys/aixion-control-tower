from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace

import psycopg
import pytest
from fastapi import HTTPException
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app.agent_run_models import (
    AgentRun,
    AgentRunStatus,
    AgentRunStep,
    AgentRunStepStatus,
    AgentRunStepType,
)
from app.agent_task_models import AgentTask, AgentTaskStatus
from app.application_repositories import ApplicationRepositories
from app.approval_integrity import compute_approval_payload_hash
from app.models import (
    AgentAction,
    AgentProvider,
    ApprovalRequest,
    ApprovalStatus,
    AuthUser,
    ExternalAgent,
    FileChange,
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
    PostgresTrustRepositoryError,
)
from app.postgres_trust_event_repository import (
    PostgresTrustEventRepository,
    PostgresTrustEventRepositoryError,
)
from app.store import store
from app.trust_crypto import sign_payload
from app.trust_policy import lease_receipt_payload
from app.trust_routes import (
    consume_manual_action,
    consume_external_agent_action,
    create_lease,
    decide_exact_action,
    evaluate_manual_action,
    get_lease,
    get_exact_action_decision,
    list_leases,
    list_flight_recorder_events,
    list_exceptions,
    list_scorecards,
    verify_flight_recorder_chain,
)
from app.trust_action_authorization_models import (
    ActionAuthorization,
    ActionAuthorizationCreate,
    ActionAuthorizationDecision,
)
from app.trust_action_authorization import (
    ActionAuthorizationConflict,
    record_agent_run_action_authorization_postgres,
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
    CapabilityLeaseCreate,
    PolicyDecision,
    PolicyDecisionType,
    ProposedAction,
    ReviewerAttestation,
    ReviewerAttestationDecision,
    TrustEventType,
)


def _postgres_lease_issue_context(trust_repository, monkeypatch):
    repository, connect, workspace_id, user_id, project_id, approval_id = trust_repository
    with connect() as conn:
        row = conn.execute(
            "SELECT payload FROM approval_requests WHERE approval_id=%s",
            (approval_id,),
        ).fetchone()
        approval = ApprovalRequest.model_validate(row[0]).model_copy(
            update={
                "created_by_user_id": f"creator_{uuid.uuid4().hex}",
                "approved_by_user_id": user_id,
                "files": [
                    FileChange(
                        path="src/approved.py",
                        change_type="modify",
                        diff="+approved = True",
                    )
                ],
                "test_plan": ["python -m pytest tests/test_approved.py"],
            }
        )
        approval.approved_payload_hash = compute_approval_payload_hash(approval)
        task = AgentTask(
            provider=AgentProvider.CODEX,
            project_id=project_id,
            workspace_id=workspace_id,
            title="Approved task",
            goal="Change one approved file",
            repository="owner/repo",
            branch_preference="feature/trust-test",
            approval_request_id=approval_id,
            status=AgentTaskStatus.APPROVED,
        )
        conn.execute(
            "UPDATE approval_requests SET payload=%s WHERE approval_id=%s",
            (Jsonb(approval.model_dump(mode="json")), approval_id),
        )
        conn.execute(
            "INSERT INTO agent_tasks(task_id,project_id,workspace_id,external_agent_id,"
            "approval_request_id,status,updated_at,payload) VALUES(%s,%s,%s,NULL,%s,%s,%s,%s)",
            (
                task.id,
                project_id,
                workspace_id,
                approval_id,
                task.status.value,
                task.updated_at,
                Jsonb(task.model_dump(mode="json")),
            ),
        )
    repositories = ApplicationRepositories.from_connector(connect)
    state = SimpleNamespace(
        postgres_runtime=SimpleNamespace(connect=connect),
        postgres_repositories=repositories,
        approval_decision_repository=repositories.approval_decisions,
        workspace_membership_repository=repositories.workspace_membership,
        project_repository=repositories.projects,
        agent_task_repository=repositories.agent_tasks,
        external_agent_auth_repository=repositories.external_agent_auth,
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
    payload = CapabilityLeaseCreate(
        approval_request_id=approval_id,
        provider=AgentProvider.CODEX,
        mode=CapabilityLeaseMode.BOUNDED,
        scope=CapabilityScope(
            repository="owner/repo",
            branch="feature/trust-test",
            allowed_actions=[CapabilityActionType.MODIFY_FILES],
            allowed_path_prefixes=["src/approved.py"],
            allowed_commands=["python -m pytest tests/test_approved.py"],
            max_runtime_seconds=120,
            max_cost_usd=1.0,
        ),
    )
    return repository, connect, workspace_id, approval_id, request, user, payload


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
            snapshot = repository.metrics_snapshot(
                conn,
                workspace_id=workspace_id,
                now=now,
                trust_event_repository=PostgresTrustEventRepository(connect),
            )
            assert snapshot.lease_status_counts == {"ACTIVE": 1}
            assert snapshot.policy_decision_counts == {"REQUIRE_APPROVAL": 1}
            assert snapshot.provider_decision_counts == {("OTHER", "REQUIRE_APPROVAL"): 1}
            assert snapshot.authorization_count == 1
            assert snapshot.consumption_count == 1
            assert snapshot.active_credential_count == 1
            assert snapshot.revoked_credential_count == 0
            assert snapshot.trust_event_count == 0
            assert snapshot.verification.valid is True
            assert snapshot.push_status_counts == {}
            assert snapshot.push_attempt_count == 0
            assert snapshot.push_queue_depth == 0
            other_workspace_snapshot = repository.metrics_snapshot(
                conn,
                workspace_id=f"other-{workspace_id}",
                now=now,
                trust_event_repository=PostgresTrustEventRepository(connect),
            )
            assert other_workspace_snapshot.lease_status_counts == {}
            assert other_workspace_snapshot.policy_decision_counts == {}
            assert other_workspace_snapshot.provider_decision_counts == {}
            assert other_workspace_snapshot.authorization_count == 0
            assert other_workspace_snapshot.consumption_count == 0
            assert other_workspace_snapshot.active_credential_count == 0
            assert other_workspace_snapshot.revoked_credential_count == 0
            assert other_workspace_snapshot.trust_event_count == 0
            assert other_workspace_snapshot.push_status_counts == {}
            assert other_workspace_snapshot.push_attempt_count == 0
            assert other_workspace_snapshot.push_queue_depth == 0
            assert other_workspace_snapshot.verification.valid is True
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
    lease = CapabilityLease(
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
    lease.receipt_signature = sign_payload(lease_receipt_payload(lease))
    return lease


def _run_action_awaiting_review(
    repository: PostgresTrustRepository,
    conn: Connection,
    *,
    lease: CapabilityLease,
    workspace_id: str,
    project_id: str,
    metadata_overrides: dict[str, str] | None = None,
    mismatch_action_task: bool = False,
    mismatch_action_project: bool = False,
    mismatch_action_lease: bool = False,
    mismatch_step_task: bool = False,
) -> ProposedAction:
    now = now_utc()
    task = AgentTask(
        id=f"task_{uuid.uuid4().hex}",
        provider=AgentProvider.CODEX,
        project_id=project_id,
        title="Canonical AgentRun trust fixture",
        goal="Exercise exact-action authorization without executing work.",
        workspace_id=workspace_id,
        approval_request_id=lease.approval_request_id,
        status=AgentTaskStatus.APPROVED,
        created_at=now,
        updated_at=now,
    )
    other_task = AgentTask(
        id=f"task_{uuid.uuid4().hex}",
        provider=AgentProvider.CODEX,
        project_id=project_id,
        title="Foreign task for mismatch regression",
        goal="Keep a valid same-workspace foreign task row.",
        workspace_id=workspace_id,
        status=AgentTaskStatus.APPROVED,
        created_at=now,
        updated_at=now,
    )
    run_id = f"run_{uuid.uuid4().hex}"
    current_step_id = f"step_{uuid.uuid4().hex}"
    prior_step = AgentRunStep(
        id=f"step_{uuid.uuid4().hex}",
        run_id=run_id,
        task_id=task.id,
        sequence=0,
        step_type=AgentRunStepType.VALIDATE_SCOPE,
        status=AgentRunStepStatus.SUCCEEDED,
        idempotency_key=f"{run_id}:VALIDATE_SCOPE:1",
    )
    step = AgentRunStep(
        id=current_step_id,
        run_id=run_id,
        task_id=other_task.id if mismatch_step_task else task.id,
        sequence=1,
        step_type=AgentRunStepType.CREATE_BRANCH,
        status=AgentRunStepStatus.NEEDS_HUMAN,
        idempotency_key=f"{run_id}:CREATE_BRANCH:1",
    )
    run = AgentRun(
        id=run_id,
        task_id=task.id,
        workspace_id=workspace_id,
        approval_request_id=lease.approval_request_id,
        capability_lease_id=lease.id,
        project_id=project_id,
        repository="owner/repo",
        status=AgentRunStatus.NEEDS_HUMAN,
        current_step_id=step.id,
        current_step_index=1,
        created_at=now,
        updated_at=now,
    )
    for durable_task in (task, other_task):
        conn.execute(
            "INSERT INTO agent_tasks(task_id,project_id,workspace_id,external_agent_id,"
            "approval_request_id,status,updated_at,payload) VALUES(%s,%s,%s,NULL,%s,%s,%s,%s)",
            (
                durable_task.id,
                project_id,
                workspace_id,
                durable_task.approval_request_id,
                durable_task.status.value,
                durable_task.updated_at,
                Jsonb(durable_task.model_dump(mode="json")),
            ),
        )
    conn.execute(
        "INSERT INTO agent_runs(run_id,task_id,project_id,workspace_id,approval_id,status,"
        "current_step_id,lease_owner,lease_token,lease_expires_at,updated_at,payload) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s,NULL,NULL,NULL,%s,%s)",
        (
            run.id,
            run.task_id,
            run.project_id,
            workspace_id,
            run.approval_request_id,
            run.status.value,
            run.current_step_id,
            run.updated_at,
            Jsonb(run.model_dump(mode="json")),
        ),
    )
    for durable_step in (prior_step, step):
        conn.execute(
            "INSERT INTO agent_run_steps(step_id,run_id,task_id,workspace_id,sequence,"
            "status,updated_at,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                durable_step.id,
                durable_step.run_id,
                durable_step.task_id,
                workspace_id,
                durable_step.sequence,
                durable_step.status.value,
                durable_step.updated_at,
                Jsonb(durable_step.model_dump(mode="json")),
            ),
        )
    metadata = {
        "step_id": step.id,
        "step_type": step.step_type.value,
        "idempotency_key": step.idempotency_key,
    }
    metadata.update(metadata_overrides or {})
    action = ProposedAction(
        id=f"action_{uuid.uuid4().hex}",
        lease_id=None if mismatch_action_lease else lease.id,
        provider=AgentProvider.CODEX,
        run_id=run.id,
        task_id=None if mismatch_action_task else task.id,
        project_id=None if mismatch_action_project else project_id,
        workspace_id=workspace_id,
        action_type=CapabilityActionType.RUN_COMMAND,
        repository="owner/repo",
        branch="feature/trust-test",
        command="pytest tests/safe.py",
        metadata=metadata,
    )
    repository.insert_proposed_action(conn, action)
    repository.insert_policy_decision(
        conn,
        PolicyDecision(
            proposed_action_id=action.id,
            lease_id=action.lease_id,
            decision=PolicyDecisionType.REQUIRE_APPROVAL,
            risk_level=RiskLevel.HIGH,
        ),
        workspace_id=workspace_id,
    )
    return action


def _record_run_action_review(
    repository: PostgresTrustRepository,
    connect: Callable[[], Connection],
    *,
    action: ProposedAction,
    workspace_id: str,
    user_id: str,
    decision: ActionAuthorizationDecision = ActionAuthorizationDecision.ALLOW,
    reason: str = "Reviewed the exact run action.",
) -> ActionAuthorization:
    return record_agent_run_action_authorization_postgres(
        action.id,
        ActionAuthorizationCreate(decision=decision, reason=reason),
        expected_run_id=action.run_id or "",
        expected_step_id=str(action.metadata["step_id"]),
        user=AuthUser(
            id=user_id,
            email=f"{user_id}@example.test",
            display_name="Durable run reviewer",
            role=UserRole.OWNER,
            workspace_id=workspace_id,
            email_verified=True,
        ),
        workspace_id=workspace_id,
        connect=connect,
        trust_repository=repository,
        trust_event_repository=PostgresTrustEventRepository(connect),
        now=now_utc(),
    )


def test_postgres_agent_run_action_authorization_is_exact_and_idempotent(
    trust_repository,
):
    repository, connect, workspace_id, user_id, project_id, approval_id = trust_repository
    lease = _lease_with_runtime_budget(
        workspace_id=workspace_id, user_id=user_id, approval_id=approval_id, runtime_budget=5
    )
    with connect() as conn:
        with conn.transaction():
            repository.insert_capability_lease(conn, lease)
            action = _run_action_awaiting_review(
                repository,
                conn,
                lease=lease,
                workspace_id=workspace_id,
                project_id=project_id,
            )

    first = _record_run_action_review(
        repository, connect, action=action, workspace_id=workspace_id, user_id=user_id
    )
    retry = _record_run_action_review(
        repository, connect, action=action, workspace_id=workspace_id, user_id=user_id
    )
    assert first == retry
    assert first.action_payload_hash

    with pytest.raises(ActionAuthorizationConflict, match="does not match"):
        _record_run_action_review(
            repository,
            connect,
            action=action,
            workspace_id=workspace_id,
            user_id=f"user_{uuid.uuid4().hex}",
        )
    with pytest.raises(ActionAuthorizationConflict, match="does not match"):
        _record_run_action_review(
            repository,
            connect,
            action=action,
            workspace_id=workspace_id,
            user_id=user_id,
            reason="A different decision payload.",
        )

    with pytest.raises(PostgresTrustRepositoryConflict, match="not visible"):
        record_agent_run_action_authorization_postgres(
            action.id,
            ActionAuthorizationCreate(
                decision=ActionAuthorizationDecision.ALLOW,
                reason="Reviewed the exact run action.",
            ),
            expected_run_id=f"run_{uuid.uuid4().hex}",
            expected_step_id=str(action.metadata["step_id"]),
            user=AuthUser(
                id=user_id,
                email=f"{user_id}@example.test",
                display_name="Durable run reviewer",
                role=UserRole.OWNER,
                workspace_id=workspace_id,
                email_verified=True,
            ),
            workspace_id=workspace_id,
            connect=connect,
            trust_repository=repository,
            trust_event_repository=PostgresTrustEventRepository(connect),
            now=now_utc(),
        )
    with pytest.raises(PostgresTrustRepositoryConflict, match="current step"):
        record_agent_run_action_authorization_postgres(
            action.id,
            ActionAuthorizationCreate(
                decision=ActionAuthorizationDecision.ALLOW,
                reason="Reviewed the exact run action.",
            ),
            expected_run_id=action.run_id or "",
            expected_step_id=f"step_{uuid.uuid4().hex}",
            user=AuthUser(
                id=user_id,
                email=f"{user_id}@example.test",
                display_name="Durable run reviewer",
                role=UserRole.OWNER,
                workspace_id=workspace_id,
                email_verified=True,
            ),
            workspace_id=workspace_id,
            connect=connect,
            trust_repository=repository,
            trust_event_repository=PostgresTrustEventRepository(connect),
            now=now_utc(),
        )
    with pytest.raises(PostgresTrustRepositoryConflict, match="not visible"):
        record_agent_run_action_authorization_postgres(
            action.id,
            ActionAuthorizationCreate(
                decision=ActionAuthorizationDecision.ALLOW,
                reason="Reviewed the exact run action.",
            ),
            expected_run_id=action.run_id or "",
            expected_step_id=str(action.metadata["step_id"]),
            user=AuthUser(
                id=user_id,
                email=f"{user_id}@example.test",
                display_name="Durable run reviewer",
                role=UserRole.OWNER,
                workspace_id="workspace_other",
                email_verified=True,
            ),
            workspace_id="workspace_other",
            connect=connect,
            trust_repository=repository,
            trust_event_repository=PostgresTrustEventRepository(connect),
            now=now_utc(),
        )

    with connect() as conn:
        assert repository.get_action_authorization_for_action(
            conn, action.id, workspace_id
        ) == first
        assert conn.execute(
            "SELECT count(*) FROM trust_events WHERE workspace_id=%s "
            "AND event_type=%s AND entity_id=%s",
            (workspace_id, TrustEventType.ACTION_ALLOWED.value, action.id),
        ).fetchone()[0] == 1
        assert repository.get_latest_policy_decision_for_action(
            conn, action.id, workspace_id
        ).decision == PolicyDecisionType.ALLOW


@pytest.mark.parametrize(
    "binding_mismatch",
    [
        "step_id",
        "step_type",
        "idempotency_key",
        "task_id",
        "project_id",
        "lease_id",
        "step_task_id",
        "missing_step",
    ],
)
def test_postgres_agent_run_action_authorization_rejects_noncanonical_binding(
    trust_repository,
    binding_mismatch: str,
):
    repository, connect, workspace_id, user_id, project_id, approval_id = trust_repository
    lease = _lease_with_runtime_budget(
        workspace_id=workspace_id, user_id=user_id, approval_id=approval_id, runtime_budget=5
    )
    with connect() as conn:
        with conn.transaction():
            repository.insert_capability_lease(conn, lease)
            action = _run_action_awaiting_review(
                repository,
                conn,
                lease=lease,
                workspace_id=workspace_id,
                project_id=project_id,
                metadata_overrides=(
                    {binding_mismatch: "not-the-durable-value"}
                    if binding_mismatch in {"step_id", "step_type", "idempotency_key"}
                    else None
                ),
                mismatch_action_task=binding_mismatch == "task_id",
                mismatch_action_project=binding_mismatch == "project_id",
                mismatch_action_lease=binding_mismatch == "lease_id",
                mismatch_step_task=binding_mismatch == "step_task_id",
            )
            if binding_mismatch == "missing_step":
                conn.execute(
                    "DELETE FROM agent_run_steps WHERE step_id=%s AND workspace_id=%s",
                    (action.metadata["step_id"], workspace_id),
                )

    expected_error = (
        PostgresTrustRepositoryError
        if binding_mismatch == "step_task_id"
        else PostgresTrustRepositoryConflict
    )
    with pytest.raises(expected_error):
        _record_run_action_review(
            repository, connect, action=action, workspace_id=workspace_id, user_id=user_id
        )

    with connect() as conn:
        assert repository.get_action_authorization_for_action(
            conn, action.id, workspace_id
        ) is None
        assert repository.get_latest_policy_decision_for_action(
            conn, action.id, workspace_id
        ).decision == PolicyDecisionType.REQUIRE_APPROVAL


def test_postgres_agent_run_action_consumption_is_run_bound_and_replay_safe(
    trust_repository,
):
    repository, connect, workspace_id, user_id, project_id, approval_id = trust_repository
    lease = _lease_with_runtime_budget(
        workspace_id=workspace_id, user_id=user_id, approval_id=approval_id, runtime_budget=5
    )
    with connect() as conn:
        with conn.transaction():
            repository.insert_capability_lease(conn, lease)
            action = _run_action_awaiting_review(
                repository,
                conn,
                lease=lease,
                workspace_id=workspace_id,
                project_id=project_id,
            )
    _record_run_action_review(
        repository, connect, action=action, workspace_id=workspace_id, user_id=user_id
    )
    consumption = ActionConsumption(
        actual_runtime_seconds=1,
        evidence={"run_id": action.run_id, "step_id": action.metadata["step_id"]},
    )

    def consume(run_id: str, step_id: str, value: ActionConsumption = consumption):
        with connect() as conn:
            with conn.transaction():
                return repository.consume_agent_run_action(
                    conn,
                    action_id=action.id,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    step_id=step_id,
                    consumption=value,
                    actor=f"run:{action.run_id}",
                    now=now_utc(),
                )

    first = consume(action.run_id or "", str(action.metadata["step_id"]))
    retry = consume(action.run_id or "", str(action.metadata["step_id"]))
    assert first == retry == consumption
    with pytest.raises(PostgresTrustRepositoryConflict, match="not visible"):
        consume(f"run_{uuid.uuid4().hex}", str(action.metadata["step_id"]))
    with pytest.raises(PostgresTrustRepositoryConflict, match="current step"):
        consume(action.run_id or "", f"step_{uuid.uuid4().hex}")
    with pytest.raises(PostgresTrustRepositoryConflict, match="immutable"):
        consume(
            action.run_id or "",
            str(action.metadata["step_id"]),
            ActionConsumption(actual_runtime_seconds=2),
        )

    with connect() as conn:
        assert repository.get_action_consumption(conn, action.id, workspace_id) == consumption
        persisted_lease = repository.get_capability_lease(conn, lease.id, workspace_id)
        assert persisted_lease is not None and persisted_lease.consumed_runtime_seconds == 1
        assert conn.execute(
            "SELECT count(*) FROM trust_events WHERE workspace_id=%s "
            "AND event_type=%s AND entity_id=%s",
            (workspace_id, TrustEventType.ACTION_CONSUMED.value, action.id),
        ).fetchone()[0] == 1


def test_postgres_agent_run_action_authorization_rolls_back_with_ledger_failure(
    trust_repository,
):
    repository, connect, workspace_id, user_id, project_id, approval_id = trust_repository
    lease = _lease_with_runtime_budget(
        workspace_id=workspace_id, user_id=user_id, approval_id=approval_id, runtime_budget=5
    )
    with connect() as conn:
        with conn.transaction():
            repository.insert_capability_lease(conn, lease)
            action = _run_action_awaiting_review(
                repository,
                conn,
                lease=lease,
                workspace_id=workspace_id,
                project_id=project_id,
            )
        conn.execute(
            "UPDATE trust_event_heads SET event_hash=%s WHERE singleton=TRUE",
            ("0" * 64,),
        )

    with pytest.raises(PostgresTrustEventRepositoryError, match="ledger"):
        _record_run_action_review(
            repository, connect, action=action, workspace_id=workspace_id, user_id=user_id
        )

    with connect() as conn:
        assert repository.get_action_authorization_for_action(
            conn, action.id, workspace_id
        ) is None
        assert repository.get_latest_policy_decision_for_action(
            conn, action.id, workspace_id
        ).decision == PolicyDecisionType.REQUIRE_APPROVAL


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


def test_postgres_credential_revocation_is_workspace_scoped_idempotent_and_atomic(
    trust_repository,
):
    repository, connect, workspace_id, user_id, _project_id, approval_id = trust_repository
    events = PostgresTrustEventRepository(connect)
    lease = _lease_with_runtime_budget(
        workspace_id=workspace_id, user_id=user_id, approval_id=approval_id, runtime_budget=5
    )
    grant = CredentialGrant(
        lease_id=lease.id,
        workspace_id=workspace_id,
        grant_type=CredentialGrantType.INTERNAL_CAPABILITY_TOKEN,
        audience="worker",
        subject="agent:test",
        scope=["run:execute"],
        expires_at=now_utc() + timedelta(minutes=5),
        token_hash=uuid.uuid4().hex,
    )
    with connect() as conn:
        with conn.transaction():
            repository.insert_capability_lease(conn, lease)
            repository.insert_credential_grant(conn, grant)

    def revoke(scope: str):
        with connect() as conn:
            with conn.transaction():
                return repository.revoke_credential_grant(
                    conn,
                    grant_id=grant.id,
                    workspace_id=scope,
                    actor_user_id=user_id,
                    actor="owner@example.test",
                    reason="Operator revoked credential",
                    now=now_utc(),
                    trust_event_repository=events,
                )

    with pytest.raises(ValueError, match="not found"):
        revoke("different-workspace")
    barrier = Barrier(2)

    def concurrent_revoke():
        barrier.wait(timeout=5)
        return revoke(workspace_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _index: concurrent_revoke(), range(2)))
    assert first.revoked and first.revoked_at is not None
    assert second.revoked and second.revoked_at is not None
    assert first.metadata["revoked_by_user_id"] == user_id
    assert second.metadata["revoked_by_user_id"] == user_id
    with connect() as conn:
        persisted = repository.get_credential_grant(conn, grant.id, workspace_id)
        assert persisted == first
        assert conn.execute(
            "SELECT count(*) FROM trust_events WHERE entity_id=%s AND event_type=%s",
            (grant.id, TrustEventType.CREDENTIAL_GRANT_REVOKED.value),
        ).fetchone()[0] == 1
    assert events.verify().valid


def test_postgres_credential_revocation_rolls_back_if_event_append_fails(trust_repository):
    repository, connect, workspace_id, user_id, _project_id, approval_id = trust_repository
    events = PostgresTrustEventRepository(connect)
    lease = _lease_with_runtime_budget(
        workspace_id=workspace_id, user_id=user_id, approval_id=approval_id, runtime_budget=5
    )
    grant = CredentialGrant(
        lease_id=lease.id,
        workspace_id=workspace_id,
        grant_type=CredentialGrantType.INTERNAL_CAPABILITY_TOKEN,
        audience="worker",
        subject="agent:test",
        scope=["run:execute"],
        expires_at=now_utc() + timedelta(minutes=5),
        token_hash=uuid.uuid4().hex,
    )
    with connect() as conn:
        with conn.transaction():
            repository.insert_capability_lease(conn, lease)
            repository.insert_credential_grant(conn, grant)
        conn.execute(
            "UPDATE trust_event_heads SET event_hash=%s WHERE singleton=TRUE", ("0" * 64,)
        )

    with pytest.raises(PostgresTrustEventRepositoryError, match="empty ledger"):
        with connect() as conn:
            with conn.transaction():
                repository.revoke_credential_grant(
                    conn,
                    grant_id=grant.id,
                    workspace_id=workspace_id,
                    actor_user_id=user_id,
                    actor="owner@example.test",
                    reason="Operator revoked credential",
                    now=now_utc(),
                    trust_event_repository=events,
                )
    with connect() as conn:
        persisted = repository.get_credential_grant(conn, grant.id, workspace_id)
        assert persisted is not None and not persisted.revoked
        assert conn.execute("SELECT count(*) FROM trust_events").fetchone()[0] == 0


def test_postgres_credential_issuance_requires_active_scoped_lease_and_is_durable(
    trust_repository,
):
    repository, connect, workspace_id, user_id, _project_id, approval_id = trust_repository
    events = PostgresTrustEventRepository(connect)
    lease = _lease_with_runtime_budget(
        workspace_id=workspace_id, user_id=user_id, approval_id=approval_id, runtime_budget=5
    )
    grant = CredentialGrant(
        lease_id=lease.id,
        workspace_id=workspace_id,
        grant_type=CredentialGrantType.INTERNAL_CAPABILITY_TOKEN,
        audience="worker",
        subject="agent:test",
        scope=["run:execute"],
        expires_at=now_utc() + timedelta(minutes=4),
        token_hash=uuid.uuid4().hex + ("0" * 32),
        metadata={"issued_by_user_id": user_id},
    )
    with connect() as conn:
        with conn.transaction():
            repository.insert_capability_lease(conn, lease)
            issued = repository.issue_credential_grant(
                conn,
                record=grant,
                actor_user_id=user_id,
                actor="owner@example.test",
                now=now_utc(),
                trust_event_repository=events,
            )
    assert issued == grant
    with connect() as conn:
        assert repository.get_credential_grant_by_token_hash(
            conn, grant.token_hash
        ) == (grant, lease)
        assert repository.get_credential_grant_by_token_hash(conn, "not-a-hash") is None
        assert conn.execute(
            "SELECT count(*) FROM trust_events WHERE entity_id=%s AND event_type=%s",
            (grant.id, TrustEventType.CREDENTIAL_GRANT_ISSUED.value),
        ).fetchone()[0] == 1
    assert events.verify().valid


def test_postgres_credential_issuance_rejects_revoked_lease_and_rolls_back_on_event_error(
    trust_repository,
):
    repository, connect, workspace_id, user_id, _project_id, approval_id = trust_repository
    events = PostgresTrustEventRepository(connect)
    lease = _lease_with_runtime_budget(
        workspace_id=workspace_id, user_id=user_id, approval_id=approval_id, runtime_budget=5
    )
    grant = CredentialGrant(
        lease_id=lease.id,
        workspace_id=workspace_id,
        grant_type=CredentialGrantType.INTERNAL_CAPABILITY_TOKEN,
        audience="worker",
        subject="agent:test",
        scope=["run:execute"],
        expires_at=now_utc() + timedelta(minutes=4),
        token_hash=uuid.uuid4().hex + ("0" * 32),
        metadata={"issued_by_user_id": user_id},
    )
    with connect() as conn:
        with conn.transaction():
            repository.insert_capability_lease(conn, lease)
    with connect() as conn:
        original_event_hash = conn.execute(
            "SELECT event_hash FROM trust_event_heads WHERE singleton=TRUE"
        ).fetchone()[0]
        conn.execute(
            "UPDATE trust_event_heads SET event_hash=%s WHERE singleton=TRUE", ("0" * 64,)
        )
    with pytest.raises(PostgresTrustEventRepositoryError, match="empty ledger"):
        with connect() as conn:
            with conn.transaction():
                repository.issue_credential_grant(
                    conn,
                    record=grant,
                    actor_user_id=user_id,
                    actor="owner@example.test",
                    now=now_utc(),
                    trust_event_repository=events,
                )
    with connect() as conn:
        assert repository.get_credential_grant(conn, grant.id, workspace_id) is None
        assert conn.execute("SELECT count(*) FROM trust_events").fetchone()[0] == 0

    # The same operation must reject authorization after a durable lease revoke.
    with connect() as conn:
        conn.execute(
            "UPDATE trust_event_heads SET event_hash=%s WHERE singleton=TRUE",
            (original_event_hash,),
        )
    with connect() as conn:
        with conn.transaction():
            repository.revoke_capability_lease(
                conn,
                lease_id=lease.id,
                workspace_id=workspace_id,
                actor_user_id=user_id,
                actor="owner@example.test",
                reason="Operator revoked the lease",
                now=now_utc(),
                trust_event_repository=PostgresTrustEventRepository(connect),
            )
    with pytest.raises(PostgresTrustRepositoryConflict, match="active, unexpired"):
        with connect() as conn:
            with conn.transaction():
                repository.issue_credential_grant(
                    conn,
                    record=grant.model_copy(update={"id": f"grant_{uuid.uuid4().hex}"}),
                    actor_user_id=user_id,
                    actor="owner@example.test",
                    now=now_utc(),
                    trust_event_repository=PostgresTrustEventRepository(connect),
                )


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


@pytest.mark.parametrize(
    ("authorization_decision", "effective_decision"),
    [
        (ActionAuthorizationDecision.ALLOW, PolicyDecisionType.ALLOW),
        (ActionAuthorizationDecision.BLOCK, PolicyDecisionType.BLOCK),
    ],
)
def test_postgres_manual_action_evaluation_and_reviewer_decision_are_durable(
    trust_repository,
    monkeypatch: pytest.MonkeyPatch,
    authorization_decision: ActionAuthorizationDecision,
    effective_decision: PolicyDecisionType,
):
    repository, connect, workspace_id, user_id, project_id, _approval_id = trust_repository
    repositories = ApplicationRepositories.from_connector(connect)
    state = SimpleNamespace(
        postgres_runtime=SimpleNamespace(connect=connect),
        postgres_repositories=repositories,
        approval_decision_repository=repositories.approval_decisions,
        workspace_membership_repository=repositories.workspace_membership,
        trust_repository=repositories.trust,
        trust_event_repository=repositories.trust_events,
        project_repository=repositories.projects,
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
    action = ProposedAction(
        provider=AgentProvider.CODEX,
        project_id=project_id,
        action_type=CapabilityActionType.READ_REPOSITORY,
        repository="owner/repo",
        branch="feature/trust-test",
    )

    result = evaluate_manual_action(action, user, request)

    assert result.decision.decision == PolicyDecisionType.REQUIRE_APPROVAL
    assert action.id not in store.proposed_actions
    decision_payload = ActionAuthorizationCreate(
        decision=authorization_decision,
        reason="Reviewed exact action on the approval console.",
    )
    authorization = decide_exact_action(action.id, decision_payload, user, request)
    assert isinstance(authorization, ActionAuthorization)
    assert authorization.action_payload_hash
    assert get_exact_action_decision(action.id, user, request) == authorization
    assert decide_exact_action(action.id, decision_payload, user, request) == authorization
    with pytest.raises(HTTPException) as conflicting_retry:
        decide_exact_action(
            action.id,
            ActionAuthorizationCreate(
                decision=authorization_decision,
                reason="A different reason cannot replace the immutable decision.",
            ),
            user,
            request,
        )
    assert conflicting_retry.value.status_code == 409
    with connect() as conn:
        assert repository.get_proposed_action(conn, action.id, workspace_id) == result.action
        stored_effective = repository.get_latest_policy_decision_for_action(
            conn, action.id, workspace_id
        )
        assert stored_effective is not None
        assert stored_effective.decision == effective_decision
        assert repository.get_action_authorization_for_action(
            conn, action.id, workspace_id
        ) == authorization
        assert conn.execute(
            "SELECT count(*) FROM trust_events WHERE workspace_id=%s AND entity_id=%s",
            (workspace_id, action.id),
        ).fetchone()[0] == 3

    tampered = authorization.model_copy(update={"signature": "invalid-signature"})
    with connect() as conn:
        conn.execute(
            "UPDATE action_authorizations SET payload=%s WHERE authorization_id=%s",
            (Jsonb(tampered.model_dump(mode="json")), authorization.id),
        )
    with pytest.raises(HTTPException) as invalid_signature:
        get_exact_action_decision(action.id, user, request)
    assert invalid_signature.value.status_code == 409


def test_postgres_manual_action_decision_rolls_back_when_ledger_is_inconsistent(
    trust_repository,
    monkeypatch: pytest.MonkeyPatch,
):
    repository, connect, workspace_id, user_id, project_id, _approval_id = trust_repository
    repositories = ApplicationRepositories.from_connector(connect)
    state = SimpleNamespace(
        postgres_runtime=SimpleNamespace(connect=connect),
        postgres_repositories=repositories,
        approval_decision_repository=repositories.approval_decisions,
        workspace_membership_repository=repositories.workspace_membership,
        trust_repository=repositories.trust,
        trust_event_repository=repositories.trust_events,
        project_repository=repositories.projects,
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
    action = ProposedAction(
        provider=AgentProvider.CODEX,
        project_id=project_id,
        action_type=CapabilityActionType.READ_REPOSITORY,
        repository="owner/repo",
        branch="feature/trust-test",
    )
    evaluated = evaluate_manual_action(action, user, request)
    with connect() as conn:
        conn.execute(
            "UPDATE trust_event_heads SET event_hash=%s WHERE singleton=TRUE",
            ("0" * 64,),
        )

    with pytest.raises(PostgresTrustEventRepositoryError, match="ledger"):
        decide_exact_action(
            action.id,
            ActionAuthorizationCreate(
                decision=ActionAuthorizationDecision.ALLOW,
                reason="Reviewed exact action.",
            ),
            user,
            request,
        )

    with connect() as conn:
        assert repository.get_action_authorization_for_action(
            conn, action.id, workspace_id
        ) is None
        latest = repository.get_latest_policy_decision_for_action(
            conn, action.id, workspace_id
        )
        assert latest == evaluated.decision
        assert conn.execute(
            "SELECT count(*) FROM trust_events WHERE workspace_id=%s AND entity_id=%s",
            (workspace_id, action.id),
        ).fetchone()[0] == 2


def test_postgres_manual_action_evaluation_rolls_back_when_ledger_is_inconsistent(
    trust_repository,
    monkeypatch: pytest.MonkeyPatch,
):
    repository, connect, workspace_id, user_id, project_id, _approval_id = trust_repository
    repositories = ApplicationRepositories.from_connector(connect)
    state = SimpleNamespace(
        postgres_runtime=SimpleNamespace(connect=connect),
        postgres_repositories=repositories,
        approval_decision_repository=repositories.approval_decisions,
        workspace_membership_repository=repositories.workspace_membership,
        trust_repository=repositories.trust,
        trust_event_repository=repositories.trust_events,
        project_repository=repositories.projects,
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
    action = ProposedAction(
        provider=AgentProvider.CODEX,
        project_id=project_id,
        action_type=CapabilityActionType.READ_REPOSITORY,
        repository="owner/repo",
        branch="feature/trust-test",
    )
    with connect() as conn:
        conn.execute(
            "UPDATE trust_event_heads SET event_hash=%s WHERE singleton=TRUE",
            ("0" * 64,),
        )

    with pytest.raises(PostgresTrustEventRepositoryError, match="empty ledger"):
        evaluate_manual_action(action, user, request)

    with connect() as conn:
        assert repository.get_proposed_action(conn, action.id, workspace_id) is None
        assert conn.execute("SELECT count(*) FROM policy_decisions").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM trust_events").fetchone()[0] == 0


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


def test_postgres_lease_issue_route_is_atomic_idempotent_and_not_projected(
    trust_repository,
    monkeypatch: pytest.MonkeyPatch,
):
    repository, connect, workspace_id, approval_id, request, user, payload = (
        _postgres_lease_issue_context(trust_repository, monkeypatch)
    )
    first = create_lease(payload, user, request)
    retry = create_lease(payload, user, request)

    assert first.id == retry.id
    listed = list_leases(status_filter=None, user=user, request=request)
    assert [item.id for item in listed] == [first.id]
    assert get_lease(first.id, user=user, request=request).id == first.id
    assert "workspace_id" not in first.model_dump()
    assert first.id not in store.capability_leases
    assert PostgresTrustEventRepository(connect).verify().valid is True
    with connect() as conn:
        durable = repository.get_capability_lease(conn, first.id, workspace_id)
        assert durable is not None
        assert durable.scope.metadata["request_fingerprint"]
        assert conn.execute(
            "SELECT count(*) FROM trust_events WHERE workspace_id=%s AND "
            "event_type=%s AND entity_id=%s",
            (workspace_id, TrustEventType.LEASE_ISSUED.value, first.id),
        ).fetchone()[0] == 1


def test_postgres_external_agent_action_consumption_is_durable_and_workspace_bound(
    trust_repository,
    monkeypatch: pytest.MonkeyPatch,
):
    repository, connect, workspace_id, _approval_id, request, user, payload = (
        _postgres_lease_issue_context(trust_repository, monkeypatch)
    )
    agent_id = f"agent_{uuid.uuid4().hex}"
    with connect() as conn:
        project_id = conn.execute(
            "SELECT project_id FROM projects WHERE workspace_id=%s",
            (workspace_id,),
        ).fetchone()[0]
    agent = ExternalAgent(
        id=agent_id,
        provider=AgentProvider.CODEX,
        display_name="Durable action consumer",
        allowed_project_ids=[project_id],
        allowed_repositories=["owner/repo"],
        allowed_actions=[AgentAction.EXECUTE_GITHUB],
        workspace_id=workspace_id,
        created_by_user_id=user.id,
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO external_agents(agent_id,workspace_id,created_by_user_id,"
            "provider,auth_type,enabled,secret_hash,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                agent.id,
                workspace_id,
                user.id,
                agent.provider.value,
                agent.auth_type.value,
                agent.enabled,
                agent.secret_hash,
                Jsonb(agent.model_dump(mode="json")),
            ),
        )
    lease = create_lease(
        payload.model_copy(update={"agent_id": agent.id}), user, request
    )
    action = ProposedAction(
        lease_id=lease.id,
        provider=agent.provider,
        agent_id=agent.id,
        project_id=project_id,
        workspace_id=workspace_id,
        action_type=CapabilityActionType.MODIFY_FILES,
        repository="owner/repo",
        branch="feature/trust-test",
        paths=["src/approved.py"],
        estimated_runtime_seconds=10,
    )
    repositories = request.app.state.postgres_repositories
    with connect() as conn:
        with conn.transaction():
            decision, _ = repositories.trust.evaluate_proposed_action(
                conn,
                action=action,
                actor=f"agent:{agent.id}",
                now=now_utc(),
                trust_event_repository=repositories.trust_events,
                expected_agent_id=agent.id,
            )
    assert decision.decision == PolicyDecisionType.ALLOW
    consumption = ActionConsumption(actual_runtime_seconds=10)

    wrong_agent = agent.model_copy(update={"id": f"agent_{uuid.uuid4().hex}"})
    with pytest.raises(HTTPException) as denied:
        consume_external_agent_action(
            action.id, consumption, wrong_agent, request
        )
    assert denied.value.status_code == 403

    bound_external_auth = request.app.state.external_agent_auth_repository
    request.app.state.external_agent_auth_repository = object()
    with pytest.raises(HTTPException) as partial_binding:
        consume_external_agent_action(action.id, consumption, agent, request)
    assert partial_binding.value.status_code == 409
    request.app.state.external_agent_auth_repository = bound_external_auth

    first = consume_external_agent_action(action.id, consumption, agent, request)
    retry = consume_external_agent_action(action.id, consumption, agent, request)
    assert first == retry == consumption
    assert action.id not in store.proposed_actions
    assert action.id not in store.action_consumptions
    with connect() as conn:
        persisted_lease = repositories.trust.get_capability_lease(
            conn, lease.id, workspace_id
        )
        assert persisted_lease is not None
        assert persisted_lease.consumed_runtime_seconds == 10
        assert repositories.trust.get_action_consumption(
            conn, action.id, workspace_id
        ) == consumption
        assert conn.execute(
            "SELECT count(*) FROM action_consumptions WHERE action_id=%s",
            (action.id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM trust_events WHERE event_type=%s AND entity_id=%s",
            (TrustEventType.ACTION_CONSUMED.value, action.id),
        ).fetchone()[0] == 1
    assert PostgresTrustEventRepository(connect).verify().valid is True


def test_postgres_flight_recorder_routes_read_the_durable_workspace_ledger(
    trust_repository,
    monkeypatch: pytest.MonkeyPatch,
):
    _repository, connect, workspace_id, _approval_id, request, user, _payload = (
        _postgres_lease_issue_context(trust_repository, monkeypatch)
    )
    events = request.app.state.postgres_repositories.trust_events
    workspace_event = events.append(
        TrustEventType.ACTION_RECEIVED,
        entity_type="proposed_action",
        entity_id="action_current_workspace",
        actor="agent:current",
        workspace_id=workspace_id,
    )
    events.append(
        TrustEventType.ACTION_RECEIVED,
        entity_type="proposed_action",
        entity_id="action_unscoped_system_event",
        actor="system",
    )

    listed = list_flight_recorder_events(
        limit=500, user=user, request=request
    )
    assert [event.id for event in listed] == [workspace_event.id]
    assert verify_flight_recorder_chain(user=user, request=request).valid is True

    bound_event_repository = request.app.state.trust_event_repository
    request.app.state.trust_event_repository = object()
    with pytest.raises(HTTPException) as partial_binding:
        list_flight_recorder_events(limit=500, user=user, request=request)
    assert partial_binding.value.status_code == 503
    request.app.state.trust_event_repository = bound_event_repository


def test_postgres_runtime_disables_sqlite_backed_trust_reports(
    trust_repository,
    monkeypatch: pytest.MonkeyPatch,
):
    _repository, _connect, _workspace_id, _approval_id, request, user, _payload = (
        _postgres_lease_issue_context(trust_repository, monkeypatch)
    )
    for endpoint in (list_scorecards, list_exceptions):
        with pytest.raises(HTTPException) as unavailable:
            endpoint(user=user, request=request)
        assert unavailable.value.status_code == 503


def test_postgres_concurrent_identical_lease_issues_resolve_once(
    trust_repository,
    monkeypatch: pytest.MonkeyPatch,
):
    repository, connect, workspace_id, _approval_id, request, user, payload = (
        _postgres_lease_issue_context(trust_repository, monkeypatch)
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(lambda _index: create_lease(payload, user, request), range(2))
        )
    assert results[0].id == results[1].id
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM capability_leases WHERE workspace_id=%s AND status='ACTIVE'",
            (workspace_id,),
        ).fetchone()[0] == 1


def test_postgres_lease_issue_rejects_conflicting_active_scope(
    trust_repository,
    monkeypatch: pytest.MonkeyPatch,
):
    _repository, connect, workspace_id, _approval_id, request, user, payload = (
        _postgres_lease_issue_context(trust_repository, monkeypatch)
    )
    create_lease(payload, user, request)
    conflicting = payload.model_copy(
        update={
            "scope": payload.scope.model_copy(update={"max_cost_usd": 0.5})
        }
    )
    with pytest.raises(HTTPException) as error:
        create_lease(conflicting, user, request)
    assert error.value.status_code == 409
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM capability_leases WHERE workspace_id=%s",
            (workspace_id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM trust_events WHERE event_type=%s",
            (TrustEventType.LEASE_ISSUED.value,),
        ).fetchone()[0] == 1


def test_postgres_lease_issue_partial_repository_binding_fails_closed(
    trust_repository,
    monkeypatch: pytest.MonkeyPatch,
):
    _repository, _connect, _workspace_id, _approval_id, request, user, payload = (
        _postgres_lease_issue_context(trust_repository, monkeypatch)
    )
    request.app.state.project_repository = object()
    with pytest.raises(HTTPException) as error:
        create_lease(payload, user, request)
    assert error.value.status_code == 409


def test_postgres_lease_issue_denied_by_exact_hash_attestation(
    trust_repository,
    monkeypatch: pytest.MonkeyPatch,
):
    repository, connect, workspace_id, approval_id, request, user, payload = (
        _postgres_lease_issue_context(trust_repository, monkeypatch)
    )
    with connect() as conn:
        row = conn.execute(
            "SELECT payload FROM approval_requests WHERE approval_id=%s",
            (approval_id,),
        ).fetchone()
        approval = ApprovalRequest.model_validate(row[0])
        denial = ReviewerAttestation(
            approval_request_id=approval_id,
            workspace_id=workspace_id,
            approval_payload_hash=approval.approved_payload_hash,
            reviewer_user_id=user.id,
            reviewer_role=user.role,
            decision=ReviewerAttestationDecision.DENY,
            reason="The requested action is not acceptable.",
            signature="pending",
        )
        material = {
            "approval_request_id": denial.approval_request_id,
            "approval_payload_hash": denial.approval_payload_hash,
            "reviewer_user_id": denial.reviewer_user_id,
            "reviewer_role": denial.reviewer_role,
            "decision": denial.decision,
            "reason": denial.reason,
        }
        denial.signature = sign_payload(material)
        repository.insert_reviewer_attestation(conn, denial)

    with pytest.raises(HTTPException) as error:
        create_lease(payload, user, request)
    assert error.value.status_code == 409
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM capability_leases WHERE workspace_id=%s",
            (workspace_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM trust_events WHERE event_type=%s",
            (TrustEventType.LEASE_ISSUED.value,),
        ).fetchone()[0] == 0


def test_postgres_expired_active_lease_is_expired_before_replacement(
    trust_repository,
    monkeypatch: pytest.MonkeyPatch,
):
    repository, connect, workspace_id, _approval_id, request, user, payload = (
        _postgres_lease_issue_context(trust_repository, monkeypatch)
    )
    first = create_lease(payload, user, request)
    with connect() as conn:
        row = conn.execute(
            "SELECT payload FROM capability_leases WHERE lease_id=%s",
            (first.id,),
        ).fetchone()
        expired_at = now_utc() - timedelta(seconds=1)
        expired = CapabilityLease.model_validate(row[0]).model_copy(
            update={"expires_at": expired_at}
        )
        conn.execute(
            "UPDATE capability_leases SET expires_at=%s,payload=%s WHERE lease_id=%s",
            (expired_at, Jsonb(expired.model_dump(mode="json")), first.id),
        )

    replacement = create_lease(payload, user, request)
    assert replacement.id != first.id
    expired = get_lease(first.id, user=user, request=request)
    assert expired.status == CapabilityLeaseStatus.EXPIRED
    assert [
        item.id
        for item in list_leases(status_filter=None, user=user, request=request)
    ] == [
        replacement.id,
        first.id,
    ]
    with connect() as conn:
        prior = repository.get_capability_lease(conn, first.id, workspace_id)
        assert prior is not None and prior.status == CapabilityLeaseStatus.EXPIRED
        assert conn.execute(
            "SELECT count(*) FROM capability_leases WHERE workspace_id=%s AND status='ACTIVE'",
            (workspace_id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM trust_events WHERE event_type=%s",
            (TrustEventType.LEASE_EXPIRED.value,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM trust_events WHERE event_type=%s",
            (TrustEventType.LEASE_ISSUED.value,),
        ).fetchone()[0] == 2
    assert PostgresTrustEventRepository(connect).verify().valid is True


def test_postgres_lease_issue_rejects_scope_outside_approved_payload(
    trust_repository,
    monkeypatch: pytest.MonkeyPatch,
):
    _repository, connect, workspace_id, _approval_id, request, user, payload = (
        _postgres_lease_issue_context(trust_repository, monkeypatch)
    )
    invalid = payload.model_copy(
        update={
            "scope": payload.scope.model_copy(
                update={"allowed_path_prefixes": ["src/not-approved.py"]}
            )
        }
    )
    with pytest.raises(HTTPException) as error:
        create_lease(invalid, user, request)
    assert error.value.status_code == 409
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM capability_leases WHERE workspace_id=%s",
            (workspace_id,),
        ).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM trust_events").fetchone()[0] == 0


def test_postgres_lease_issue_rolls_back_when_trust_event_append_fails(
    trust_repository,
    monkeypatch: pytest.MonkeyPatch,
):
    _repository, connect, workspace_id, _approval_id, request, user, payload = (
        _postgres_lease_issue_context(trust_repository, monkeypatch)
    )

    def fail_event(*_args, **_kwargs):
        raise RuntimeError("injected trust ledger failure")

    monkeypatch.setattr(
        PostgresTrustEventRepository, "append_in_transaction", fail_event
    )
    with pytest.raises(RuntimeError, match="injected trust ledger failure"):
        create_lease(payload, user, request)
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM capability_leases WHERE workspace_id=%s",
            (workspace_id,),
        ).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM trust_events").fetchone()[0] == 0
