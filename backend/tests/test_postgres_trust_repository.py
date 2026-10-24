from __future__ import annotations

import multiprocessing
import os
import queue as queue_module
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from time import sleep
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from fastapi import HTTPException
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app.agent_run_models import (
    AgentRun,
    AgentRunCreate,
    AgentRunStatus,
    AgentRunStep,
    AgentRunStepStatus,
    AgentRunStepType,
)
from app.agent_run_creation import build_agent_run_creation_records
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
from app.trust_crypto import sha256_hex, sign_payload
from app.trust_policy import lease_receipt_payload
from app.trust_routes import (
    consume_manual_action,
    consume_external_agent_action,
    create_lease,
    create_attestation,
    decide_exact_action,
    evaluate_manual_action,
    get_lease,
    get_exact_action_decision,
    list_leases,
    list_flight_recorder_events,
    list_attestations,
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
    record_action_authorization_postgres,
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
    ReviewerAttestationCreate,
    ReviewerAttestationDecision,
    TrustEventType,
)


def _consume_task_action_in_process(
    dsn: str,
    schema: str,
    barrier: Any,
    results: Any,
    action_id: str,
    workspace_id: str,
    task_id: str,
    consumption_payload: dict[str, Any],
) -> None:
    try:
        barrier.wait(timeout=15)
        with psycopg.connect(dsn) as conn:
            conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            with conn.transaction():
                consumed = PostgresTrustRepository().consume_task_action(
                    conn,
                    action_id=action_id,
                    workspace_id=workspace_id,
                    task_id=task_id,
                    consumption=ActionConsumption.model_validate(consumption_payload),
                    actor="agent:task-only-race",
                    now=now_utc(),
                )
        results.put(("ok", consumed.model_dump(mode="json")))
    except BaseException as error:  # pragma: no cover - surfaced by the parent test
        results.put(("error", f"{type(error).__name__}: {error}"))


def _consume_agent_run_action_in_process(
    dsn: str,
    schema: str,
    barrier: Any,
    results: Any,
    action_id: str,
    workspace_id: str,
    run_id: str,
    step_id: str,
    consumption_payload: dict[str, Any],
) -> None:
    """Race one durable action consumption from an independent process."""
    try:
        barrier.wait(timeout=20)
        with psycopg.connect(dsn) as conn:
            conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            with conn.transaction():
                consumed = PostgresTrustRepository().consume_agent_run_action(
                    conn,
                    action_id=action_id,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    step_id=step_id,
                    consumption=ActionConsumption.model_validate(consumption_payload),
                    actor=f"run:{run_id}",
                    now=now_utc(),
                )
        results.put(("ok", consumed.model_dump(mode="json")))
    except BaseException as error:  # pragma: no cover - surfaced in the parent test
        results.put(("error", f"{type(error).__name__}: {error}"))


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
        postgres_runtime=SimpleNamespace(connect=connect, repositories=repositories),
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
        display_name="Task action reviewer",
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

            assert repository.count_active_capability_leases(
                conn, workspace_id=workspace_id, now=now
            ) == 1
            assert repository.count_active_capability_leases(
                conn, workspace_id=f"other-{workspace_id}", now=now
            ) == 0
            with pytest.raises(ValueError, match="timezone-aware"):
                repository.count_active_capability_leases(
                    conn, workspace_id=workspace_id, now=now.replace(tzinfo=None)
                )

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


def _run_validation_binding_fixture(
    repository: PostgresTrustRepository,
    conn: Connection,
    *,
    workspace_id: str,
    user_id: str,
    project_id: str,
    approval_id: str,
    lease_mode: CapabilityLeaseMode = CapabilityLeaseMode.BOUNDED,
) -> tuple[str, str, str, str, str]:
    """Persist exact validation-command trust state without executing it."""
    command = "pytest -q tests/test_approved.py"
    now = now_utc()
    row = conn.execute(
        "SELECT payload FROM approval_requests WHERE approval_id=%s AND workspace_id=%s FOR UPDATE",
        (approval_id, workspace_id),
    ).fetchone()
    assert row is not None
    approval = ApprovalRequest.model_validate(row[0]).model_copy(
        update={"test_plan": [command]}
    )
    approval.approved_payload_hash = compute_approval_payload_hash(approval)
    conn.execute(
        "UPDATE approval_requests SET payload=%s WHERE approval_id=%s AND workspace_id=%s",
        (Jsonb(approval.model_dump(mode="json")), approval_id, workspace_id),
    )

    lease = _lease_with_runtime_budget(
        workspace_id=workspace_id,
        user_id=user_id,
        approval_id=approval_id,
        runtime_budget=60,
    ).model_copy(
        update={
            "approved_payload_hash": approval.approved_payload_hash,
            "provider": AgentProvider.CODEX,
            "mode": lease_mode,
            "scope": CapabilityScope(
                repository="owner/repo",
                branch="feature/trust-test",
                allowed_actions=[CapabilityActionType.RUN_COMMAND],
                allowed_commands=[command],
                max_runtime_seconds=60,
            ),
        }
    )
    lease.receipt_signature = sign_payload(lease_receipt_payload(lease))
    repository.insert_capability_lease(conn, lease)

    task = AgentTask(
        id=f"task_{uuid.uuid4().hex}",
        provider=AgentProvider.CODEX,
        project_id=project_id,
        title="RUN_VALIDATION binding fixture",
        goal="Verify exact approved command provenance.",
        workspace_id=workspace_id,
        approval_request_id=approval_id,
        status=AgentTaskStatus.APPROVED,
        created_at=now,
        updated_at=now,
    )
    run_id = f"run_{uuid.uuid4().hex}"
    step_id = f"step_{uuid.uuid4().hex}"
    worker_id = f"worker_{uuid.uuid4().hex}"
    lease_token = uuid.uuid4().hex
    expires = now + timedelta(minutes=2)
    run = AgentRun(
        id=run_id,
        task_id=task.id,
        workspace_id=workspace_id,
        approval_request_id=approval_id,
        capability_lease_id=lease.id,
        project_id=project_id,
        repository="owner/repo",
        status=AgentRunStatus.RUNNING,
        current_step_id=step_id,
        lease_owner=worker_id,
        lease_token=lease_token,
        lease_expires_at=expires,
        created_at=now,
        updated_at=now,
    )
    step = AgentRunStep(
        id=step_id,
        run_id=run_id,
        task_id=task.id,
        sequence=3,
        step_type=AgentRunStepType.RUN_VALIDATION,
        status=AgentRunStepStatus.RUNNING,
        idempotency_key=f"{run_id}:RUN_VALIDATION:1",
        lease_owner=worker_id,
        lease_token=lease_token,
        lease_expires_at=expires,
        created_at=now,
        updated_at=now,
    )
    conn.execute(
        "INSERT INTO agent_tasks(task_id,project_id,workspace_id,external_agent_id,"
        "approval_request_id,status,updated_at,payload) VALUES(%s,%s,%s,NULL,%s,%s,%s,%s)",
        (task.id, project_id, workspace_id, approval_id, task.status.value, task.updated_at,
         Jsonb(task.model_dump(mode="json"))),
    )
    conn.execute(
        "INSERT INTO agent_runs(run_id,task_id,project_id,workspace_id,approval_id,status,"
        "current_step_id,lease_owner,lease_token,lease_expires_at,updated_at,payload) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (run.id, task.id, project_id, workspace_id, approval_id, run.status.value, step.id,
         worker_id, lease_token, expires, run.updated_at, Jsonb(run.model_dump(mode="json"))),
    )
    conn.execute(
        "INSERT INTO agent_run_steps(step_id,run_id,task_id,workspace_id,sequence,status,"
        "updated_at,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
        (step.id, run.id, task.id, workspace_id, step.sequence, step.status.value,
         step.updated_at, Jsonb(step.model_dump(mode="json"))),
    )
    action = ProposedAction(
        id=f"action_{uuid.uuid4().hex}",
        lease_id=lease.id,
        provider=task.provider,
        run_id=run.id,
        task_id=task.id,
        project_id=project_id,
        workspace_id=workspace_id,
        action_type=CapabilityActionType.RUN_COMMAND,
        repository="owner/repo",
        branch="feature/trust-test",
        command=command,
        metadata={
            "step_id": step.id,
            "step_type": AgentRunStepType.RUN_VALIDATION.value,
            "idempotency_key": step.idempotency_key,
            "approval_request_id": approval.id,
            "approved_payload_hash": approval.approved_payload_hash,
            "command_source": "approval.test_plan",
            "command_index": 0,
            "command_sha256": sha256_hex({"command": command}),
        },
    )
    repository.insert_proposed_action(conn, action)
    return action.id, run.id, step.id, worker_id, lease_token


def test_postgres_agent_run_validation_command_binding_accepts_exact_plan(
    trust_repository,
):
    repository, connect, workspace_id, user_id, project_id, approval_id = trust_repository
    with connect() as conn:
        with conn.transaction():
            action_id, run_id, step_id, worker_id, lease_token = _run_validation_binding_fixture(
                repository, conn, workspace_id=workspace_id, user_id=user_id,
                project_id=project_id, approval_id=approval_id,
            )
    with connect() as conn:
        with conn.transaction():
            action = repository.lock_agent_run_validation_command_binding(
                conn, action_id=action_id, workspace_id=workspace_id, run_id=run_id,
                step_id=step_id, worker_id=worker_id, lease_token=lease_token,
            )
            assert action.command == "pytest -q tests/test_approved.py"


def test_postgres_evaluate_agent_run_validation_command_is_idempotent_for_exact_action(
    trust_repository,
):
    repository, connect, workspace_id, user_id, project_id, approval_id = trust_repository
    event_repository = PostgresTrustEventRepository(connect)
    with connect() as conn:
        with conn.transaction():
            action_id, run_id, step_id, worker_id, lease_token = _run_validation_binding_fixture(
                repository, conn, workspace_id=workspace_id, user_id=user_id,
                project_id=project_id, approval_id=approval_id,
            )
    with connect() as conn:
        with conn.transaction():
            action = repository.get_proposed_action(conn, action_id, workspace_id)
    assert action is not None

    with connect() as conn:
        with conn.transaction():
            decision, lease = repository.evaluate_agent_run_validation_command(
                conn,
                action=action,
                workspace_id=workspace_id,
                run_id=run_id,
                step_id=step_id,
                worker_id=worker_id,
                lease_token=lease_token,
                actor="validation-worker",
                trust_event_repository=event_repository,
            )
            assert decision.decision == PolicyDecisionType.ALLOW
            assert lease.id == action.lease_id
    with connect() as conn:
        with conn.transaction():
            repeated, lease = repository.evaluate_agent_run_validation_command(
                conn,
                action=action,
                workspace_id=workspace_id,
                run_id=run_id,
                step_id=step_id,
                worker_id=worker_id,
                lease_token=lease_token,
                actor="validation-worker",
                trust_event_repository=event_repository,
            )
            assert repeated.id == decision.id
            assert lease.id == action.lease_id
            events = event_repository.list_workspace_events(
                conn, workspace_id=workspace_id, entity_type="proposed_action", entity_id=action_id
            )
            assert [event.event_type for event in events] == [
                TrustEventType.ACTION_RECEIVED,
                TrustEventType.ACTION_ALLOWED,
            ]
            assert events[0].payload["action_payload_sha256"] == sha256_hex(
                action.model_dump(mode="json")
            )
            assert repository.get_action_consumption(conn, action_id, workspace_id) is None


def test_postgres_evaluate_agent_run_validation_command_rejects_changed_payload(
    trust_repository,
):
    repository, connect, workspace_id, user_id, project_id, approval_id = trust_repository
    event_repository = PostgresTrustEventRepository(connect)
    with connect() as conn:
        with conn.transaction():
            action_id, run_id, step_id, worker_id, lease_token = _run_validation_binding_fixture(
                repository, conn, workspace_id=workspace_id, user_id=user_id,
                project_id=project_id, approval_id=approval_id,
            )
    with connect() as conn:
        with conn.transaction():
            action = repository.get_proposed_action(conn, action_id, workspace_id)
    assert action is not None
    altered = action.model_copy(update={"command": "pytest -q tests/other.py"})
    with connect() as conn:
        with conn.transaction():
            with pytest.raises(PostgresTrustRepositoryConflict, match="differs from the supplied"):
                repository.evaluate_agent_run_validation_command(
                    conn,
                    action=altered,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    step_id=step_id,
                    worker_id=worker_id,
                    lease_token=lease_token,
                    actor="validation-worker",
                    trust_event_repository=event_repository,
                )
            assert repository.get_latest_policy_decision_for_action(
                conn, action_id, workspace_id
            ) is None
            assert event_repository.list_workspace_events(
                conn, workspace_id=workspace_id, entity_type="proposed_action", entity_id=action_id
            ) == []


def test_postgres_evaluate_agent_run_validation_command_never_auto_approves_or_consumes(
    trust_repository,
):
    repository, connect, workspace_id, user_id, project_id, approval_id = trust_repository
    event_repository = PostgresTrustEventRepository(connect)
    with connect() as conn:
        with conn.transaction():
            action_id, run_id, step_id, worker_id, lease_token = _run_validation_binding_fixture(
                repository,
                conn,
                workspace_id=workspace_id,
                user_id=user_id,
                project_id=project_id,
                approval_id=approval_id,
                lease_mode=CapabilityLeaseMode.STRICT,
            )
    with connect() as conn:
        with conn.transaction():
            action = repository.get_proposed_action(conn, action_id, workspace_id)
    assert action is not None
    with connect() as conn:
        with conn.transaction():
            decision, _lease = repository.evaluate_agent_run_validation_command(
                conn,
                action=action,
                workspace_id=workspace_id,
                run_id=run_id,
                step_id=step_id,
                worker_id=worker_id,
                lease_token=lease_token,
                actor="validation-worker",
                trust_event_repository=event_repository,
            )
            assert decision.decision == PolicyDecisionType.REQUIRE_APPROVAL
            with pytest.raises(PostgresTrustRepositoryConflict, match="Only an effective ALLOW"):
                repository.consume_agent_run_action(
                    conn,
                    action_id=action_id,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    step_id=step_id,
                    consumption=ActionConsumption(
                        actual_runtime_seconds=1,
                        evidence={"validation": "must not be accepted"},
                    ),
                    actor="validation-worker",
                    now=now_utc(),
                )
            assert repository.get_action_consumption(conn, action_id, workspace_id) is None


def test_postgres_agent_run_validation_command_binding_locks_authorities_in_order(
    trust_repository,
):
    repository, connect, workspace_id, user_id, project_id, approval_id = trust_repository

    class RecordingConnection:
        def __init__(self, connection):
            self.connection = connection
            self.statements: list[str] = []
            self.queries: list[str] = []

        @property
        def autocommit(self):
            return self.connection.autocommit

        def execute(self, query, params=None):
            statement = query.as_string(self.connection) if hasattr(query, "as_string") else str(query)
            self.queries.append(" ".join(statement.upper().split()))
            if "FOR UPDATE" in statement.upper():
                self.statements.append(" ".join(statement.upper().split()))
            return self.connection.execute(query, params)

    with connect() as conn:
        with conn.transaction():
            action_id, run_id, step_id, worker_id, lease_token = _run_validation_binding_fixture(
                repository, conn, workspace_id=workspace_id, user_id=user_id,
                project_id=project_id, approval_id=approval_id,
            )
    with connect() as conn:
        with conn.transaction():
            recorded = RecordingConnection(conn)
            repository.lock_agent_run_validation_command_binding(
                recorded, action_id=action_id, workspace_id=workspace_id, run_id=run_id,
                step_id=step_id, worker_id=worker_id, lease_token=lease_token,
            )
            locked_tables = [
                next(
                    table for table in (
                        "APPROVAL_REQUESTS", "AGENT_TASKS", "AGENT_RUNS",
                        "AGENT_RUN_STEPS", "PROPOSED_ACTIONS", "CAPABILITY_LEASES",
                    )
                    if table in statement
                )
                for statement in recorded.statements
            ]
            assert locked_tables == [
                "APPROVAL_REQUESTS", "AGENT_TASKS", "AGENT_RUNS",
                "AGENT_RUN_STEPS", "PROPOSED_ACTIONS", "CAPABILITY_LEASES",
            ]
            assert any("SELECT CLOCK_TIMESTAMP()" in query for query in recorded.queries)


@pytest.mark.parametrize(
    "mutation",
    ["command", "index", "command_hash", "command_source", "approval_hash", "step_type", "action_agent"],
)
def test_postgres_agent_run_validation_command_binding_rejects_metadata_drift(
    trust_repository, mutation,
):
    repository, connect, workspace_id, user_id, project_id, approval_id = trust_repository
    with connect() as conn:
        with conn.transaction():
            action_id, run_id, step_id, worker_id, lease_token = _run_validation_binding_fixture(
                repository, conn, workspace_id=workspace_id, user_id=user_id,
                project_id=project_id, approval_id=approval_id,
            )
            action = repository.get_proposed_action(conn, action_id, workspace_id)
            assert action is not None
            if mutation == "command":
                action.command = "pytest -q tests/unapproved.py"
            elif mutation == "index":
                action.metadata["command_index"] = True
            elif mutation == "command_hash":
                action.metadata["command_sha256"] = "0" * 64
            elif mutation == "command_source":
                action.metadata["command_source"] = "untrusted.request"
            elif mutation == "approval_hash":
                action.metadata["approved_payload_hash"] = "sha256:" + "0" * 64
            elif mutation == "action_agent":
                action.agent_id = "unbound-external-agent"
            else:
                action.metadata["step_type"] = AgentRunStepType.CREATE_BRANCH.value
            conn.execute(
                "UPDATE proposed_actions SET payload=%s WHERE action_id=%s AND workspace_id=%s",
                (Jsonb(action.model_dump(mode="json")), action_id, workspace_id),
            )
    with connect() as conn:
        with conn.transaction(), pytest.raises(PostgresTrustRepositoryConflict):
            repository.lock_agent_run_validation_command_binding(
                conn, action_id=action_id, workspace_id=workspace_id, run_id=run_id,
                step_id=step_id, worker_id=worker_id, lease_token=lease_token,
            )


@pytest.mark.parametrize(
    "mismatch", ["worker", "token", "expired_run", "expired_step", "expired_lease"]
)
def test_postgres_agent_run_validation_command_binding_requires_current_lease(
    trust_repository, mismatch,
):
    repository, connect, workspace_id, user_id, project_id, approval_id = trust_repository
    with connect() as conn:
        with conn.transaction():
            action_id, run_id, step_id, worker_id, lease_token = _run_validation_binding_fixture(
                repository, conn, workspace_id=workspace_id, user_id=user_id,
                project_id=project_id, approval_id=approval_id,
            )
            if mismatch == "worker":
                worker_id = "other-worker"
            elif mismatch == "token":
                lease_token = "other-token"
            elif mismatch == "expired_run":
                expired_at = conn.execute(
                    "SELECT clock_timestamp() - interval '1 second'"
                ).fetchone()[0]
                run_row = conn.execute(
                    "SELECT payload FROM agent_runs WHERE run_id=%s", (run_id,),
                ).fetchone()
                run_payload = dict(run_row[0])
                run_payload["lease_expires_at"] = expired_at.isoformat()
                conn.execute(
                    "UPDATE agent_runs SET lease_expires_at=%s,payload=%s WHERE run_id=%s",
                    (expired_at, Jsonb(run_payload), run_id),
                )
            elif mismatch == "expired_step":
                expired_at = conn.execute(
                    "SELECT clock_timestamp() - interval '1 second'"
                ).fetchone()[0]
                step_row = conn.execute(
                    "SELECT payload FROM agent_run_steps WHERE step_id=%s", (step_id,),
                ).fetchone()
                step_payload = dict(step_row[0])
                step_payload["lease_expires_at"] = expired_at.isoformat()
                conn.execute(
                    "UPDATE agent_run_steps SET payload=%s WHERE step_id=%s",
                    (Jsonb(step_payload), step_id),
                )
            else:
                lease_row = conn.execute(
                    "SELECT payload FROM capability_leases WHERE lease_id=%s AND workspace_id=%s",
                    (conn.execute(
                        "SELECT payload->>'capability_lease_id' FROM agent_runs WHERE run_id=%s",
                        (run_id,),
                    ).fetchone()[0], workspace_id),
                ).fetchone()
                lease = CapabilityLease.model_validate(lease_row[0])
                lease.expires_at = conn.execute(
                    "SELECT clock_timestamp() - interval '1 second'"
                ).fetchone()[0]
                lease.receipt_signature = sign_payload(lease_receipt_payload(lease))
                conn.execute(
                    "UPDATE capability_leases SET expires_at=%s,payload=%s WHERE lease_id=%s AND workspace_id=%s",
                    (lease.expires_at, Jsonb(lease.model_dump(mode="json")), lease.id, workspace_id),
                )
    with connect() as conn:
        with conn.transaction(), pytest.raises(PostgresTrustRepositoryConflict):
            repository.lock_agent_run_validation_command_binding(
                conn, action_id=action_id, workspace_id=workspace_id, run_id=run_id,
                step_id=step_id, worker_id=worker_id, lease_token=lease_token,
            )


@pytest.mark.parametrize("mismatch", ["provider", "agent"])
def test_postgres_agent_run_validation_command_binding_requires_lease_provider_agent_match(
    trust_repository, mismatch,
):
    repository, connect, workspace_id, user_id, project_id, approval_id = trust_repository
    with connect() as conn:
        with conn.transaction():
            action_id, run_id, step_id, worker_id, lease_token = _run_validation_binding_fixture(
                repository, conn, workspace_id=workspace_id, user_id=user_id,
                project_id=project_id, approval_id=approval_id,
            )
            lease_id = conn.execute(
                "SELECT payload->>'capability_lease_id' FROM agent_runs WHERE run_id=%s",
                (run_id,),
            ).fetchone()[0]
            row = conn.execute(
                "SELECT payload FROM capability_leases WHERE lease_id=%s AND workspace_id=%s",
                (lease_id, workspace_id),
            ).fetchone()
            lease = CapabilityLease.model_validate(row[0])
            if mismatch == "provider":
                lease.provider = AgentProvider.ANTIGRAVITY
            else:
                lease.agent_id = "unbound-external-agent"
            lease.receipt_signature = sign_payload(lease_receipt_payload(lease))
            conn.execute(
                "UPDATE capability_leases SET payload=%s WHERE lease_id=%s AND workspace_id=%s",
                (Jsonb(lease.model_dump(mode="json")), lease.id, workspace_id),
            )
    with connect() as conn:
        with conn.transaction(), pytest.raises(PostgresTrustRepositoryConflict):
            repository.lock_agent_run_validation_command_binding(
                conn, action_id=action_id, workspace_id=workspace_id, run_id=run_id,
                step_id=step_id, worker_id=worker_id, lease_token=lease_token,
            )


@pytest.mark.parametrize("invalid_plan", ["dangerous", "unallowlisted", "duplicate", "too_many"])
def test_postgres_agent_run_validation_command_binding_reuses_validation_plan_policy(
    trust_repository, invalid_plan,
):
    repository, connect, workspace_id, user_id, project_id, approval_id = trust_repository
    with connect() as conn:
        with conn.transaction():
            action_id, run_id, step_id, worker_id, lease_token = _run_validation_binding_fixture(
                repository, conn, workspace_id=workspace_id, user_id=user_id,
                project_id=project_id, approval_id=approval_id,
            )
            row = conn.execute(
                "SELECT payload FROM approval_requests WHERE approval_id=%s AND workspace_id=%s",
                (approval_id, workspace_id),
            ).fetchone()
            approval = ApprovalRequest.model_validate(row[0])
            command = "pytest -q tests/test_approved.py"
            if invalid_plan == "dangerous":
                approval.test_plan = ["pytest tests/safe.py; rm -rf /"]
            elif invalid_plan == "unallowlisted":
                approval.test_plan = ["python run_unapproved.py"]
            elif invalid_plan == "duplicate":
                approval.test_plan = [command, command]
            else:
                approval.test_plan = [f"pytest -q tests/test_{index}.py" for index in range(13)]
            approval.approved_payload_hash = compute_approval_payload_hash(approval)
            conn.execute(
                "UPDATE approval_requests SET payload=%s WHERE approval_id=%s AND workspace_id=%s",
                (Jsonb(approval.model_dump(mode="json")), approval_id, workspace_id),
            )
    with connect() as conn:
        with conn.transaction(), pytest.raises(PostgresTrustRepositoryConflict):
            repository.lock_agent_run_validation_command_binding(
                conn, action_id=action_id, workspace_id=workspace_id, run_id=run_id,
                step_id=step_id, worker_id=worker_id, lease_token=lease_token,
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


def test_postgres_agent_run_action_consumption_is_idempotent_across_processes(
    trust_repository,
):
    repository, connect, workspace_id, user_id, project_id, approval_id = trust_repository
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    assert dsn, "the trust_repository fixture requires the isolated PostgreSQL test DSN"
    with connect() as conn:
        schema = conn.execute("SELECT current_schema()").fetchone()[0]

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
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(
            target=_consume_agent_run_action_in_process,
            args=(
                dsn,
                schema,
                barrier,
                results,
                action.id,
                workspace_id,
                action.run_id or "",
                str(action.metadata["step_id"]),
                consumption.model_dump(mode="json"),
            ),
        )
        for _ in range(2)
    ]
    try:
        for process in processes:
            process.start()
        outcomes = []
        for _ in processes:
            try:
                outcomes.append(results.get(timeout=30))
            except queue_module.Empty:
                pytest.fail("Timed out waiting for concurrent PostgreSQL action consumers")
        for process in processes:
            process.join(timeout=10)
            if process.is_alive():
                pytest.fail("PostgreSQL action-consumption worker did not exit before timeout")
        assert [process.exitcode for process in processes] == [0, 0]
        assert all(outcome[0] == "ok" for outcome in outcomes), outcomes
        assert all(outcome[1] == consumption.model_dump(mode="json") for outcome in outcomes)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
        results.close()
        results.join_thread()

    with connect() as conn:
        assert repository.get_action_consumption(conn, action.id, workspace_id) == consumption
        persisted_lease = repository.get_capability_lease(conn, lease.id, workspace_id)
        assert persisted_lease is not None
        assert persisted_lease.consumed_runtime_seconds == 1
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
        postgres_runtime=SimpleNamespace(connect=connect, repositories=repositories),
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


def _postgres_decision_route_context(
    connect: Callable[[], Connection],
    monkeypatch: pytest.MonkeyPatch,
    *,
    workspace_id: str,
    user_id: str,
):
    repositories = ApplicationRepositories.from_connector(connect)
    state = SimpleNamespace(
        postgres_runtime=SimpleNamespace(connect=connect, repositories=repositories),
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
        display_name="PostgreSQL run-action reviewer",
        role=UserRole.REVIEWER,
        workspace_id=workspace_id,
        email_verified=True,
    )
    membership_resolutions: list[str] = []

    def resolve_membership(_user: AuthUser, _request) -> str:
        membership_resolutions.append(_user.id)
        return workspace_id

    monkeypatch.setattr("app.trust_routes.workspace_id_for_request", resolve_membership)
    return request, user, repositories, membership_resolutions


@pytest.mark.parametrize(
    ("decision", "effective_decision"),
    [
        (ActionAuthorizationDecision.ALLOW, PolicyDecisionType.ALLOW),
        (ActionAuthorizationDecision.BLOCK, PolicyDecisionType.BLOCK),
    ],
)
def test_postgres_run_action_decision_route_uses_exact_durable_binding(
    trust_repository,
    monkeypatch: pytest.MonkeyPatch,
    decision: ActionAuthorizationDecision,
    effective_decision: PolicyDecisionType,
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

    request, user, _repositories, membership_resolutions = _postgres_decision_route_context(
        connect, monkeypatch, workspace_id=workspace_id, user_id=user_id
    )
    decision_payload = ActionAuthorizationCreate(
        decision=decision, reason="Reviewed the exact durable AgentRun action."
    )
    authorization = decide_exact_action(action.id, decision_payload, user, request)

    assert authorization.action_id == action.id
    assert authorization.workspace_id == workspace_id
    assert get_exact_action_decision(action.id, user, request) == authorization
    assert decide_exact_action(action.id, decision_payload, user, request) == authorization
    assert membership_resolutions == [user_id, user_id, user_id]
    with connect() as conn:
        assert repository.get_action_authorization_for_action(
            conn, action.id, workspace_id
        ) == authorization
        assert repository.get_latest_policy_decision_for_action(
            conn, action.id, workspace_id
        ).decision == effective_decision
        assert conn.execute(
            "SELECT count(*) FROM trust_events WHERE workspace_id=%s "
            "AND entity_id=%s AND event_type IN (%s,%s)",
            (
                workspace_id,
                action.id,
                TrustEventType.ACTION_ALLOWED.value,
                TrustEventType.ACTION_BLOCKED.value,
            ),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT status,current_step_id FROM agent_runs WHERE run_id=%s AND workspace_id=%s",
            (action.run_id, workspace_id),
        ).fetchone() == (AgentRunStatus.NEEDS_HUMAN.value, action.metadata["step_id"])
        assert repository.get_action_consumption(conn, action.id, workspace_id) is None
    assert action.id not in store.action_authorizations


def test_postgres_run_action_decision_route_hides_foreign_workspace_action(
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
            action = _run_action_awaiting_review(
                repository,
                conn,
                lease=lease,
                workspace_id=workspace_id,
                project_id=project_id,
            )

    foreign_workspace_id = f"ws_{uuid.uuid4().hex}"
    request, user, _repositories, membership_resolutions = _postgres_decision_route_context(
        connect, monkeypatch, workspace_id=foreign_workspace_id, user_id=f"user_{uuid.uuid4().hex}"
    )
    with pytest.raises(HTTPException) as not_found:
        decide_exact_action(
            action.id,
            ActionAuthorizationCreate(
                decision=ActionAuthorizationDecision.ALLOW,
                reason="Attempt a cross-workspace decision.",
            ),
            user,
            request,
        )
    assert not_found.value.status_code == 404
    assert membership_resolutions == [user.id]
    with connect() as conn:
        assert repository.get_action_authorization_for_action(conn, action.id, workspace_id) is None
        assert repository.get_latest_policy_decision_for_action(
            conn, action.id, workspace_id
        ).decision == PolicyDecisionType.REQUIRE_APPROVAL


def test_postgres_run_action_decision_route_requires_workspace_membership(
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
            action = _run_action_awaiting_review(
                repository,
                conn,
                lease=lease,
                workspace_id=workspace_id,
                project_id=project_id,
            )

    request, user, _repositories, _membership_resolutions = _postgres_decision_route_context(
        connect, monkeypatch, workspace_id=workspace_id, user_id=user_id
    )
    membership_calls: list[str] = []

    def no_membership(_user: AuthUser, _request):
        membership_calls.append(_user.id)
        return None

    monkeypatch.setattr("app.trust_routes.workspace_id_for_request", no_membership)
    with pytest.raises(HTTPException) as forbidden:
        decide_exact_action(
            action.id,
            ActionAuthorizationCreate(
                decision=ActionAuthorizationDecision.ALLOW,
                reason="Attempt to approve without workspace membership.",
            ),
            user,
            request,
        )
    assert forbidden.value.status_code == 403
    assert membership_calls == [user_id]
    with connect() as conn:
        assert repository.get_action_authorization_for_action(conn, action.id, workspace_id) is None
        assert repository.get_latest_policy_decision_for_action(
            conn, action.id, workspace_id
        ).decision == PolicyDecisionType.REQUIRE_APPROVAL


def test_postgres_run_action_decision_route_rejects_stale_current_step(
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
            action = _run_action_awaiting_review(
                repository,
                conn,
                lease=lease,
                workspace_id=workspace_id,
                project_id=project_id,
            )
    with connect() as conn:
        row = conn.execute(
            "SELECT payload FROM agent_runs WHERE run_id=%s AND workspace_id=%s",
            (action.run_id, workspace_id),
        ).fetchone()
        run = AgentRun.model_validate(row[0]).model_copy(
            update={"current_step_id": f"step_{uuid.uuid4().hex}"}
        )
        conn.execute(
            "UPDATE agent_runs SET current_step_id=%s,payload=%s "
            "WHERE run_id=%s AND workspace_id=%s",
            (run.current_step_id, Jsonb(run.model_dump(mode="json")), action.run_id, workspace_id),
        )

    request, user, _repositories, _membership_resolutions = _postgres_decision_route_context(
        connect, monkeypatch, workspace_id=workspace_id, user_id=user_id
    )
    with pytest.raises(HTTPException) as stale:
        decide_exact_action(
            action.id,
            ActionAuthorizationCreate(
                decision=ActionAuthorizationDecision.ALLOW,
                reason="Attempt to approve a stale run step.",
            ),
            user,
            request,
        )
    assert stale.value.status_code == 409
    with connect() as conn:
        assert repository.get_action_authorization_for_action(conn, action.id, workspace_id) is None
        assert repository.get_latest_policy_decision_for_action(
            conn, action.id, workspace_id
        ).decision == PolicyDecisionType.REQUIRE_APPROVAL


def test_postgres_run_action_decision_route_rejects_incomplete_binding_without_fallback(
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
            action = _run_action_awaiting_review(
                repository,
                conn,
                lease=lease,
                workspace_id=workspace_id,
                project_id=project_id,
            )
        malformed = action.model_copy(update={"metadata": {"step_type": "CREATE_BRANCH"}})
        conn.execute(
            "UPDATE proposed_actions SET payload=%s WHERE action_id=%s AND workspace_id=%s",
            (Jsonb(malformed.model_dump(mode="json")), action.id, workspace_id),
        )

    request, user, _repositories, _membership_resolutions = _postgres_decision_route_context(
        connect, monkeypatch, workspace_id=workspace_id, user_id=user_id
    )
    with pytest.raises(HTTPException) as incomplete:
        decide_exact_action(
            action.id,
            ActionAuthorizationCreate(
                decision=ActionAuthorizationDecision.ALLOW,
                reason="Attempt to approve an incomplete run action.",
            ),
            user,
            request,
        )
    assert incomplete.value.status_code == 409
    assert "exact durable AgentRun binding" in incomplete.value.detail
    assert action.id not in store.proposed_actions
    with connect() as conn:
        assert repository.get_action_authorization_for_action(conn, action.id, workspace_id) is None
        assert repository.get_latest_policy_decision_for_action(
            conn, action.id, workspace_id
        ).decision == PolicyDecisionType.REQUIRE_APPROVAL


def test_postgres_run_action_decision_route_rolls_back_on_ledger_failure(
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

    request, user, _repositories, _membership_resolutions = _postgres_decision_route_context(
        connect, monkeypatch, workspace_id=workspace_id, user_id=user_id
    )
    with pytest.raises(PostgresTrustEventRepositoryError, match="ledger"):
        decide_exact_action(
            action.id,
            ActionAuthorizationCreate(
                decision=ActionAuthorizationDecision.ALLOW,
                reason="Attempt to approve while the audit ledger is invalid.",
            ),
            user,
            request,
        )
    with connect() as conn:
        assert repository.get_action_authorization_for_action(conn, action.id, workspace_id) is None
        assert repository.get_latest_policy_decision_for_action(
            conn, action.id, workspace_id
        ).decision == PolicyDecisionType.REQUIRE_APPROVAL


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
        postgres_runtime=SimpleNamespace(connect=connect, repositories=repositories),
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
        postgres_runtime=SimpleNamespace(connect=connect, repositories=repositories),
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
        postgres_runtime=SimpleNamespace(connect=connect, repositories=repositories),
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


def test_postgres_attestation_list_reads_the_durable_workspace_repository(
    trust_repository,
    monkeypatch: pytest.MonkeyPatch,
):
    _repository, connect, workspace_id, approval_id, request, user, _payload = (
        _postgres_lease_issue_context(trust_repository, monkeypatch)
    )
    repositories = request.app.state.postgres_repositories
    with connect() as conn:
        row = conn.execute(
            "SELECT payload->>'approved_payload_hash' FROM approval_requests WHERE approval_id=%s",
            (approval_id,),
        ).fetchone()
        attestation = ReviewerAttestation(
            approval_request_id=approval_id,
            workspace_id=workspace_id,
            approval_payload_hash=row[0],
            reviewer_user_id=user.id,
            reviewer_role=user.role,
            decision=ReviewerAttestationDecision.APPROVE,
            reason="Durable route fixture",
            signature="test-signature",
        )
        with conn.transaction():
            repositories.trust.insert_reviewer_attestation(conn, attestation)

    listed = list_attestations(
        approval_request_id=approval_id,
        user=user,
        request=request,
    )

    assert [item.id for item in listed] == [attestation.id]


def test_postgres_attestation_routes_refuse_partial_binding_without_sqlite_fallback(
    trust_repository,
    monkeypatch: pytest.MonkeyPatch,
):
    _repository, _connect, _workspace_id, approval_id, request, user, _payload = (
        _postgres_lease_issue_context(trust_repository, monkeypatch)
    )
    request.app.state.trust_event_repository = object()
    before = dict(store.reviewer_attestations)

    with pytest.raises(HTTPException) as list_error:
        list_attestations(
            approval_request_id=approval_id,
            user=user,
            request=request,
        )
    with pytest.raises(HTTPException) as create_error:
        create_attestation(
            ReviewerAttestationCreate(
                approval_request_id=approval_id,
                decision=ReviewerAttestationDecision.APPROVE,
            ),
            user=user,
            request=request,
        )

    assert list_error.value.status_code == 503
    assert create_error.value.status_code == 503
    assert store.reviewer_attestations == before


def test_postgres_attestation_routes_refuse_runtime_bundle_mismatch(
    trust_repository,
    monkeypatch: pytest.MonkeyPatch,
):
    _repository, _connect, _workspace_id, approval_id, request, user, _payload = (
        _postgres_lease_issue_context(trust_repository, monkeypatch)
    )
    connection_attempts: list[bool] = []

    def mismatched_connect():
        connection_attempts.append(True)
        raise AssertionError("A mismatched runtime must not be used")

    request.app.state.postgres_runtime = SimpleNamespace(
        connect=mismatched_connect,
        repositories=object(),
    )
    before = dict(store.reviewer_attestations)

    with pytest.raises(HTTPException) as list_error:
        list_attestations(
            approval_request_id=approval_id,
            user=user,
            request=request,
        )
    with pytest.raises(HTTPException) as create_error:
        create_attestation(
            ReviewerAttestationCreate(
                approval_request_id=approval_id,
                decision=ReviewerAttestationDecision.APPROVE,
            ),
            user=user,
            request=request,
        )

    assert list_error.value.status_code == 503
    assert create_error.value.status_code == 503
    assert connection_attempts == []
    assert store.reviewer_attestations == before


def test_postgres_attestation_routes_refuse_single_partial_repository_binding(
    trust_repository,
    monkeypatch: pytest.MonkeyPatch,
):
    _repository, _connect, _workspace_id, approval_id, request, user, _payload = (
        _postgres_lease_issue_context(trust_repository, monkeypatch)
    )
    state = request.app.state
    state.postgres_runtime = None
    state.postgres_repositories = None
    state.approval_decision_repository = object()
    state.workspace_membership_repository = None
    state.trust_repository = None
    state.trust_event_repository = None
    monkeypatch.setattr(
        "app.trust_routes.get_settings",
        lambda: SimpleNamespace(postgres_auth_enabled=False),
    )
    before = dict(store.reviewer_attestations)

    with pytest.raises(HTTPException) as list_error:
        list_attestations(
            approval_request_id=approval_id,
            user=user,
            request=request,
        )
    with pytest.raises(HTTPException) as create_error:
        create_attestation(
            ReviewerAttestationCreate(
                approval_request_id=approval_id,
                decision=ReviewerAttestationDecision.APPROVE,
            ),
            user=user,
            request=request,
        )

    assert list_error.value.status_code == 503
    assert create_error.value.status_code == 503
    assert store.reviewer_attestations == before


def test_postgres_runtime_reads_durable_scorecards_without_sqlite_projection(
    trust_repository,
    monkeypatch: pytest.MonkeyPatch,
):
    _repository, connect, workspace_id, _approval_id, request, user, _payload = (
        _postgres_lease_issue_context(trust_repository, monkeypatch)
    )
    repositories = request.app.state.postgres_repositories
    request.app.state.postgres_runtime.repositories = repositories
    def runtime_connect():
        connection = connect()
        connection.commit()
        return connection

    request.app.state.postgres_runtime.connect = runtime_connect
    request.app.state.trust_repository = repositories.trust
    request.app.state.agent_task_repository = repositories.agent_tasks
    request.app.state.agent_run_repository = repositories.agent_runs

    durable_action = ProposedAction(
        id=f"action_{uuid.uuid4().hex}",
        provider=AgentProvider.CODEX,
        agent_id="durable-codex-installation",
        workspace_id=workspace_id,
        action_type=CapabilityActionType.RUN_COMMAND,
        command="python -m pytest",
    )
    durable_decision = PolicyDecision(
        proposed_action_id=durable_action.id,
        decision=PolicyDecisionType.ALLOW,
        risk_level=RiskLevel.LOW,
        reasons=["Within the approved capability."],
    )
    with connect() as conn:
        with conn.transaction():
            repositories.trust.insert_proposed_action(conn, durable_action)
            repositories.trust.insert_policy_decision(
                conn, durable_decision, workspace_id=workspace_id
            )

    sqlite_only_action = ProposedAction(
        id=f"sqlite_action_{uuid.uuid4().hex}",
        provider=AgentProvider.ANTIGRAVITY,
        agent_id="process-local-only",
        workspace_id=workspace_id,
        action_type=CapabilityActionType.RUN_COMMAND,
        command="should not appear",
    )
    monkeypatch.setitem(store.proposed_actions, sqlite_only_action.id, sqlite_only_action)
    scorecards = list_scorecards(user=user, request=request)
    durable_card = next(
        card
        for card in scorecards
        if card.provider == AgentProvider.CODEX
        and card.agent_id == "durable-codex-installation"
    )
    assert durable_card.evaluated_actions == 1
    assert durable_card.allowed_actions == 1
    assert durable_card.scope_adherence_rate == 1.0
    assert not any(card.agent_id == "process-local-only" for card in scorecards)

    durable_task = repositories.agent_tasks.list(workspace_id=workspace_id)[0]
    scored_runs = [
        AgentRun(
            task_id=durable_task.id,
            workspace_id=workspace_id,
            project_id=durable_task.project_id,
            status=AgentRunStatus.SUCCEEDED,
            objective="Successful first-attempt run",
        ),
        AgentRun(
            task_id=durable_task.id,
            workspace_id=workspace_id,
            project_id=durable_task.project_id,
            status=AgentRunStatus.FAILED,
            objective="Failed first-attempt run",
        ),
    ]
    with connect() as conn:
        for run in scored_runs:
            conn.execute(
                "INSERT INTO agent_runs(run_id,task_id,project_id,workspace_id,approval_id,status,"
                "current_step_id,lease_owner,lease_token,lease_expires_at,updated_at,payload) "
                "VALUES(%s,%s,%s,%s,NULL,%s,NULL,NULL,NULL,NULL,%s,%s)",
                (
                    run.id,
                    run.task_id,
                    run.project_id,
                    run.workspace_id,
                    run.status.value,
                    run.updated_at,
                    Jsonb(run.model_dump(mode="json")),
                ),
            )
            step = AgentRunStep(
                run_id=run.id,
                task_id=run.task_id,
                sequence=0,
                step_type=AgentRunStepType.VALIDATE_SCOPE,
                status=(
                    AgentRunStepStatus.SUCCEEDED
                    if run.status == AgentRunStatus.SUCCEEDED
                    else AgentRunStepStatus.FAILED
                ),
                attempt_count=1,
                idempotency_key=f"{run.id}:VALIDATE_SCOPE:1",
            )
            conn.execute(
                "INSERT INTO agent_run_steps(step_id,run_id,task_id,workspace_id,sequence,status,"
                "updated_at,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    step.id,
                    step.run_id,
                    step.task_id,
                    workspace_id,
                    step.sequence,
                    step.status.value,
                    step.updated_at,
                    Jsonb(step.model_dump(mode="json")),
                ),
            )

    scorecards = list_scorecards(user=user, request=request)
    task_card = next(
        card
        for card in scorecards
        if card.provider == durable_task.provider
        and card.agent_id == durable_task.external_agent_id
    )
    assert task_card.first_attempt_success_rate == 0.5

    bound = request.app.state.agent_run_repository
    request.app.state.agent_run_repository = object()
    with pytest.raises(HTTPException) as unavailable:
        list_scorecards(user=user, request=request)
    assert unavailable.value.status_code == 503
    request.app.state.agent_run_repository = bound
    exceptions = list_exceptions(user=user, request=request)
    failed_run = next(run for run in scored_runs if run.status == AgentRunStatus.FAILED)
    assert [(item.run_id, item.category) for item in exceptions] == [
        (failed_run.id, AgentRunStatus.FAILED.value)
    ]


def test_postgres_trust_reporting_does_not_fall_back_when_runtime_is_missing(
    trust_repository,
    monkeypatch: pytest.MonkeyPatch,
):
    _repository, _connect, _workspace_id, _approval_id, request, user, _payload = (
        _postgres_lease_issue_context(trust_repository, monkeypatch)
    )
    state = request.app.state
    state.postgres_runtime = None
    state.postgres_repositories = None
    monkeypatch.setattr(
        "app.trust_routes.get_settings",
        lambda: SimpleNamespace(postgres_auth_enabled=True),
    )
    monkeypatch.setattr(
        "app.trust_routes.build_reliability_scorecards",
        lambda *_args, **_kwargs: pytest.fail("scorecards used process-local state"),
    )
    monkeypatch.setattr(
        "app.trust_routes.build_exception_queue",
        lambda *_args, **_kwargs: pytest.fail("exceptions used process-local state"),
    )

    for endpoint in (list_scorecards, list_exceptions):
        with pytest.raises(HTTPException) as unavailable:
            endpoint(user=user, request=request)
        assert unavailable.value.status_code == 503


def test_postgres_exception_queue_reads_workspace_scoped_durable_actions(
    trust_repository,
    monkeypatch: pytest.MonkeyPatch,
):
    _repository, connect, workspace_id, _approval_id, request, user, _payload = (
        _postgres_lease_issue_context(trust_repository, monkeypatch)
    )
    repositories = request.app.state.postgres_repositories
    request.app.state.postgres_runtime.repositories = repositories
    def runtime_connect():
        connection = connect()
        connection.commit()
        return connection

    request.app.state.postgres_runtime.connect = runtime_connect
    request.app.state.agent_task_repository = repositories.agent_tasks
    request.app.state.agent_run_repository = repositories.agent_runs
    trust = repositories.trust
    action = ProposedAction(
        id=f"action_{uuid.uuid4().hex}",
        provider=AgentProvider.CODEX,
        agent_id="codex-installation-1",
        project_id=None,
        workspace_id=workspace_id,
        action_type=CapabilityActionType.RUN_COMMAND,
        command="rm -rf build",
    )
    decision = PolicyDecision(
        proposed_action_id=action.id,
        decision=PolicyDecisionType.BLOCK,
        risk_level=RiskLevel.HIGH,
        reasons=["Command is outside the approved capability."],
    )
    with connect() as conn:
        with conn.transaction():
            trust.insert_proposed_action(conn, action)
            trust.insert_policy_decision(conn, decision, workspace_id=workspace_id)

    task = repositories.agent_tasks.list(workspace_id=workspace_id)[0]
    run = AgentRun(
        task_id=task.id,
        workspace_id=workspace_id,
        project_id=task.project_id,
        status=AgentRunStatus.BLOCKED,
        objective="Run requiring operator review",
        last_error="Policy blocked the operation",
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO agent_runs(run_id,task_id,project_id,workspace_id,approval_id,status,"
            "current_step_id,lease_owner,lease_token,lease_expires_at,updated_at,payload) "
            "VALUES(%s,%s,%s,%s,NULL,%s,NULL,NULL,NULL,NULL,%s,%s)",
            (
                run.id,
                run.task_id,
                run.project_id,
                run.workspace_id,
                run.status.value,
                run.updated_at,
                Jsonb(run.model_dump(mode="json")),
            ),
        )

    before_sqlite_actions = set(store.proposed_actions)
    before_sqlite_decisions = set(store.policy_decisions)
    items = list_exceptions(user=user, request=request)

    assert {item.id for item in items} == {f"action:{action.id}", f"run:{run.id}"}
    action_item = next(item for item in items if item.id == f"action:{action.id}")
    run_item = next(item for item in items if item.id == f"run:{run.id}")
    assert action_item.category == "BLOCK"
    assert action_item.severity == RiskLevel.HIGH
    assert action_item.summary == "Command is outside the approved capability."
    assert run_item.category == "BLOCKED"
    assert run_item.severity == RiskLevel.CRITICAL
    assert run_item.summary == "Policy blocked the operation"
    assert set(store.proposed_actions) == before_sqlite_actions
    assert set(store.policy_decisions) == before_sqlite_decisions

    # Rows can be internally consistent with their payloads yet still bind a
    # run to a different project than its task. Such a record must fail closed.
    other_project = Project(name="Different project", workspace_id=workspace_id)
    with connect() as conn:
        conn.execute(
            "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
            (
                other_project.id,
                workspace_id,
                Jsonb(other_project.model_dump(mode="json")),
            ),
        )
        mismatched_run = run.model_copy(update={"project_id": other_project.id})
        conn.execute(
            "UPDATE agent_runs SET project_id=%s,payload=%s WHERE run_id=%s",
            (
                mismatched_run.project_id,
                Jsonb(mismatched_run.model_dump(mode="json")),
                run.id,
            ),
        )
    with pytest.raises(HTTPException) as corrupt_record:
        list_exceptions(user=user, request=request)
    assert corrupt_record.value.status_code == 503

    # A mismatched app binding must never silently fall through to the SQLite
    # reporting builder, even when durable PostgreSQL rows exist.
    bound = request.app.state.agent_run_repository
    request.app.state.agent_run_repository = object()
    with pytest.raises(HTTPException) as unavailable:
        list_exceptions(user=user, request=request)
    assert unavailable.value.status_code == 503
    request.app.state.agent_run_repository = bound


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
    assert error.value.status_code == 503


def test_postgres_lease_issue_fails_closed_when_auth_selected_but_runtime_missing(
    trust_repository,
    monkeypatch: pytest.MonkeyPatch,
):
    _repository, connect, workspace_id, _approval_id, request, user, payload = (
        _postgres_lease_issue_context(trust_repository, monkeypatch)
    )
    request.app.state.postgres_runtime = None
    request.app.state.postgres_repositories = None
    monkeypatch.setattr(
        "app.trust_routes.get_settings",
        lambda: SimpleNamespace(postgres_auth_enabled=True),
    )
    monkeypatch.setattr(
        "app.trust_routes.workspace_id_for_user",
        lambda *_args, **_kwargs: pytest.fail("lease issuance used local workspace state"),
    )
    monkeypatch.setattr(
        "app.trust_routes._approval_for_user",
        lambda *_args, **_kwargs: pytest.fail("lease issuance read local approval state"),
    )
    monkeypatch.setattr(
        "app.trust_routes.issue_capability_lease",
        lambda *_args, **_kwargs: pytest.fail("lease issuance mutated process-local trust state"),
    )
    before_local = (
        set(store.approval_requests),
        set(store.capability_leases),
        set(store.trust_events),
    )
    with connect() as conn:
        before_postgres = (
            conn.execute(
                "SELECT count(*) FROM capability_leases WHERE workspace_id=%s",
                (workspace_id,),
            ).fetchone()[0],
            conn.execute(
                "SELECT count(*) FROM trust_events WHERE workspace_id=%s",
                (workspace_id,),
            ).fetchone()[0],
        )

    with pytest.raises(HTTPException) as error:
        create_lease(payload, user, request)

    assert error.value.status_code == 503
    assert store.approval_requests.keys() == before_local[0]
    assert store.capability_leases.keys() == before_local[1]
    assert store.trust_events.keys() == before_local[2]
    with connect() as conn:
        after_postgres = (
            conn.execute(
                "SELECT count(*) FROM capability_leases WHERE workspace_id=%s",
                (workspace_id,),
            ).fetchone()[0],
            conn.execute(
                "SELECT count(*) FROM trust_events WHERE workspace_id=%s",
                (workspace_id,),
            ).fetchone()[0],
        )
    assert after_postgres == before_postgres


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


def _task_only_action_fixture(trust_repository):
    repository, connect, workspace_id, user_id, project_id, approval_id = trust_repository
    with connect() as conn:
        row = conn.execute(
            "SELECT payload FROM approval_requests WHERE approval_id=%s AND workspace_id=%s",
            (approval_id, workspace_id),
        ).fetchone()
        approval = ApprovalRequest.model_validate(row[0]).model_copy(
            update={
                "created_by_user_id": f"creator_{uuid.uuid4().hex}",
                "approved_by_user_id": user_id,
                "test_plan": ["pytest -q tests/approved.py"],
            }
        )
        approval.approved_payload_hash = compute_approval_payload_hash(approval)
        conn.execute(
            "UPDATE approval_requests SET payload=%s WHERE approval_id=%s AND workspace_id=%s",
            (Jsonb(approval.model_dump(mode="json")), approval_id, workspace_id),
        )
        task = AgentTask(
            id=f"task_{uuid.uuid4().hex}",
            provider=AgentProvider.CODEX,
            project_id=project_id,
            workspace_id=workspace_id,
            title="Task-only approved action",
            goal="Run one reviewed validation command.",
            repository="owner/repo",
            branch_preference="feature/trust-test",
            approval_request_id=approval_id,
            status=AgentTaskStatus.APPROVED,
        )
        conn.execute(
            "INSERT INTO agent_tasks(task_id,project_id,workspace_id,external_agent_id,"
            "approval_request_id,status,updated_at,payload) VALUES(%s,%s,%s,NULL,%s,%s,%s,%s)",
            (task.id, project_id, workspace_id, approval_id, task.status.value,
             task.updated_at, Jsonb(task.model_dump(mode="json"))),
        )
        lease = CapabilityLease(
            id=f"lease_{uuid.uuid4().hex}",
            approval_request_id=approval_id,
            workspace_id=workspace_id,
            approved_payload_hash=approval.approved_payload_hash,
            provider=task.provider,
            mode=CapabilityLeaseMode.STRICT,
            scope=CapabilityScope(
                repository=task.repository,
                branch=task.branch_preference,
                allowed_actions=[CapabilityActionType.RUN_COMMAND],
                allowed_commands=approval.test_plan,
                max_runtime_seconds=60,
            ),
            issued_by_user_id=user_id,
            expires_at=now_utc() + timedelta(minutes=5),
            receipt_nonce=uuid.uuid4().hex,
            receipt_signature="pending",
        )
        lease.receipt_signature = sign_payload(lease_receipt_payload(lease))
        repository.insert_capability_lease(conn, lease)
    return repository, connect, workspace_id, user_id, project_id, approval, task, lease


def _evaluate_task_only_action(repository, connect, *, workspace_id, task, approval, lease):
    action = ProposedAction(
        lease_id=lease.id,
        provider=task.provider,
        agent_id=task.external_agent_id,
        task_id=task.id,
        project_id=task.project_id,
        workspace_id=workspace_id,
        action_type=CapabilityActionType.RUN_COMMAND,
        repository=task.repository,
        branch=task.branch_preference,
        command=approval.test_plan[0],
        metadata={
            "approval_request_id": approval.id,
            "approved_payload_hash": approval.approved_payload_hash,
        },
    )
    with connect() as conn:
        with conn.transaction():
            decision, _ = repository.evaluate_proposed_action(
                conn,
                action=action,
                actor="agent:test-task-only",
                now=now_utc(),
                trust_event_repository=PostgresTrustEventRepository(lambda: conn),
            )
    return action, decision


def test_postgres_task_only_action_requires_opt_in_and_consumes_once(
    trust_repository,
    monkeypatch: pytest.MonkeyPatch,
):
    (
        repository, connect, workspace_id, user_id, _project_id, approval, task, lease
    ) = _task_only_action_fixture(trust_repository)
    action = ProposedAction(
        lease_id=lease.id,
        provider=task.provider,
        task_id=task.id,
        project_id=task.project_id,
        workspace_id=workspace_id,
        action_type=CapabilityActionType.RUN_COMMAND,
        repository=task.repository,
        branch=task.branch_preference,
        command=approval.test_plan[0],
        metadata={
            "approval_request_id": approval.id,
            "approved_payload_hash": approval.approved_payload_hash,
        },
    )
    monkeypatch.setenv("AIXION_POSTGRES_TASK_ACTIONS_ENABLED", "false")
    with pytest.raises(PostgresTrustRepositoryConflict, match="disabled"):
        with connect() as conn, conn.transaction():
            repository.evaluate_proposed_action(
                conn,
                action=action,
                actor="agent:test-task-only",
                now=now_utc(),
                trust_event_repository=PostgresTrustEventRepository(lambda: conn),
            )
    with connect() as conn:
        assert repository.get_proposed_action(conn, action.id, workspace_id) is None

    monkeypatch.setenv("AIXION_POSTGRES_TASK_ACTIONS_ENABLED", "true")
    action, decision = _evaluate_task_only_action(
        repository, connect, workspace_id=workspace_id, task=task,
        approval=approval, lease=lease,
    )
    assert decision.decision == PolicyDecisionType.REQUIRE_APPROVAL
    monkeypatch.setenv("AIXION_POSTGRES_TASK_ACTIONS_ENABLED", "false")
    with pytest.raises(PostgresTrustRepositoryConflict, match="disabled"):
        with connect() as conn, conn.transaction():
            repository.consume_effective_action(
                conn,
                action_id=action.id,
                workspace_id=workspace_id,
                consumption=ActionConsumption(actual_runtime_seconds=1),
                actor="agent:test-task-only",
                now=now_utc(),
            )
    with connect() as conn:
        assert repository.get_action_consumption(conn, action.id, workspace_id) is None
    monkeypatch.setenv("AIXION_POSTGRES_TASK_ACTIONS_ENABLED", "true")
    reviewer = AuthUser(
        id=user_id,
        email=f"{user_id}@example.test",
        display_name="Task action reviewer",
        role=UserRole.OWNER,
        workspace_id=workspace_id,
        email_verified=True,
    )
    authorization = record_action_authorization_postgres(
        action.id,
        ActionAuthorizationCreate(
            decision=ActionAuthorizationDecision.ALLOW,
            reason="Allow this exact task action.",
        ),
        user=reviewer,
        workspace_id=workspace_id,
        connect=connect,
        trust_repository=repository,
        trust_event_repository=PostgresTrustEventRepository(connect),
        now=now_utc(),
    )
    assert authorization.action_payload_hash
    replayed_authorization = record_action_authorization_postgres(
        action.id,
        ActionAuthorizationCreate(
            decision=ActionAuthorizationDecision.ALLOW,
            reason="Allow this exact task action.",
        ),
        user=reviewer,
        workspace_id=workspace_id,
        connect=connect,
        trust_repository=repository,
        trust_event_repository=PostgresTrustEventRepository(connect),
        now=now_utc(),
    )
    assert replayed_authorization == authorization
    consumption = ActionConsumption(actual_runtime_seconds=2, evidence={"result": "ok"})
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    assert dsn
    with connect() as conn:
        schema = conn.execute("SELECT current_schema()").fetchone()[0]
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(
            target=_consume_task_action_in_process,
            args=(
                dsn, schema, barrier, results, action.id, workspace_id, task.id,
                consumption.model_dump(mode="json"),
            ),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    outcomes = [results.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(timeout=30)
    assert all(process.exitcode == 0 for process in processes), outcomes
    assert all(status == "ok" for status, _payload in outcomes), outcomes
    assert all(payload == consumption.model_dump(mode="json") for _status, payload in outcomes)
    with connect() as conn:
        assert repository.get_action_consumption(conn, action.id, workspace_id) == consumption
        assert conn.execute(
            "SELECT count(*) FROM trust_events WHERE workspace_id=%s AND entity_id=%s "
            "AND event_type=%s",
            (workspace_id, action.id, TrustEventType.ACTION_CONSUMED.value),
        ).fetchone()[0] == 1
        stored_lease = repository.get_capability_lease(conn, lease.id, workspace_id)
        assert stored_lease is not None
        assert stored_lease.consumed_runtime_seconds == 2

    denied_action, denied_policy = _evaluate_task_only_action(
        repository, connect, workspace_id=workspace_id, task=task,
        approval=approval, lease=lease,
    )
    assert denied_policy.decision == PolicyDecisionType.REQUIRE_APPROVAL
    denial = record_action_authorization_postgres(
        denied_action.id,
        ActionAuthorizationCreate(
            decision=ActionAuthorizationDecision.BLOCK,
            reason="Deny this exact task action.",
        ),
        user=reviewer,
        workspace_id=workspace_id,
        connect=connect,
        trust_repository=repository,
        trust_event_repository=PostgresTrustEventRepository(connect),
        now=now_utc(),
    )
    assert denial.decision == ActionAuthorizationDecision.BLOCK
    with pytest.raises(PostgresTrustRepositoryConflict, match="effective ALLOW"):
        with connect() as conn, conn.transaction():
            repository.consume_task_action(
                conn,
                action_id=denied_action.id,
                workspace_id=workspace_id,
                task_id=task.id,
                consumption=ActionConsumption(),
                actor="agent:test-task-only",
                now=now_utc(),
            )


def test_postgres_task_action_consumption_serializes_with_lease_revocation(
    trust_repository,
    monkeypatch: pytest.MonkeyPatch,
):
    repository, connect, workspace_id, user_id, _project_id, _approval_id = trust_repository
    events = PostgresTrustEventRepository(connect)
    monkeypatch.setenv("AIXION_POSTGRES_TASK_ACTIONS_ENABLED", "true")

    def new_approval_fixture_tuple():
        approval_id = f"approval_{uuid.uuid4().hex}"
        with connect() as conn:
            source_row = conn.execute(
                "SELECT payload FROM approval_requests WHERE approval_id=%s AND workspace_id=%s",
                (_approval_id, workspace_id),
            ).fetchone()
            assert source_row is not None
            source = ApprovalRequest.model_validate(source_row[0])
            approval = source.model_copy(
                update={
                    "id": approval_id,
                    "created_by_user_id": f"creator_{uuid.uuid4().hex}",
                    "approved_by_user_id": user_id,
                    "test_plan": ["pytest -q tests/approved.py"],
                    "approved_payload_hash": None,
                }
            )
            approval.approved_payload_hash = compute_approval_payload_hash(approval)
            conn.execute(
                "INSERT INTO approval_requests(approval_id,project_id,workspace_id,work_order_id,"
                "source_agent_id,status,expires_at,created_at,updated_at,version,source_invocation_id,payload) "
                "VALUES(%s,%s,%s,NULL,NULL,%s,NULL,%s,%s,0,NULL,%s)",
                (
                    approval_id,
                    approval.project_id,
                    workspace_id,
                    approval.status.value,
                    approval.created_at,
                    approval.updated_at,
                    Jsonb(approval.model_dump(mode="json")),
                ),
            )
        return (*trust_repository[:5], approval_id)

    def prepare_action():
        (
            _repository, _connect, _workspace_id, _user_id, _project_id,
            approval, task, lease,
        ) = _task_only_action_fixture(new_approval_fixture_tuple())
        action, policy = _evaluate_task_only_action(
            repository,
            connect,
            workspace_id=workspace_id,
            task=task,
            approval=approval,
            lease=lease,
        )
        assert policy.decision == PolicyDecisionType.REQUIRE_APPROVAL
        reviewer = AuthUser(
            id=user_id,
            email=f"{user_id}@example.test",
            display_name="Lease race reviewer",
            role=UserRole.OWNER,
            workspace_id=workspace_id,
            email_verified=True,
        )
        record_action_authorization_postgres(
            action.id,
            ActionAuthorizationCreate(
                decision=ActionAuthorizationDecision.ALLOW,
                reason="Allow the exact task action for the lease race test.",
            ),
            user=reviewer,
            workspace_id=workspace_id,
            connect=connect,
            trust_repository=repository,
            trust_event_repository=events,
            now=now_utc(),
        )
        return action, task, lease

    def wait_until_blocked(pid: int, blocker_pid: int) -> None:
        # Query PostgreSQL's lock graph directly; the bounded polling only waits
        # for the worker to reach the known row-lock boundary.
        for _ in range(300):
            with connect() as observer:
                blockers = observer.execute(
                    "SELECT pg_blocking_pids(%s)", (pid,)
                ).fetchone()[0]
            if blocker_pid in blockers:
                return
            sleep(0.01)
        pytest.fail(f"PostgreSQL backend {pid} never waited on {blocker_pid}")

    def revoke_in_worker(lease: CapabilityLease, pid_queue, result_queue) -> None:
        try:
            with connect() as conn:
                pid_queue.put(conn.execute("SELECT pg_backend_pid()").fetchone()[0])
                with conn.transaction():
                    revoked = repository.revoke_capability_lease(
                        conn,
                        lease_id=lease.id,
                        workspace_id=workspace_id,
                        actor_user_id=user_id,
                        actor="owner@example.test",
                        reason="Revoke during task action consumption.",
                        now=now_utc(),
                        trust_event_repository=PostgresTrustEventRepository(lambda: conn),
                    )
            result_queue.put(revoked)
        except BaseException as error:  # pragma: no cover - surfaced by assertions
            result_queue.put(error)

    def consume_in_worker(action: ProposedAction, task: AgentTask, pid_queue, result_queue) -> None:
        try:
            with connect() as conn:
                pid_queue.put(conn.execute("SELECT pg_backend_pid()").fetchone()[0])
                with conn.transaction():
                    consumed = repository.consume_task_action(
                        conn,
                        action_id=action.id,
                        workspace_id=workspace_id,
                        task_id=task.id,
                        consumption=ActionConsumption(actual_runtime_seconds=2),
                        actor="agent:lease-revoke-race",
                        now=now_utc(),
                    )
            result_queue.put(consumed)
        except BaseException as error:  # pragma: no cover - surfaced by assertions
            result_queue.put(error)

    # Consume first while holding the lease row lock. Revocation reaches that
    # same row and must wait until the consumption transaction commits.
    consume_first_action, consume_first_task, consume_first_lease = prepare_action()
    consumed = ActionConsumption(actual_runtime_seconds=2)
    revoke_pid: queue_module.Queue[int] = queue_module.Queue()
    revoke_result: queue_module.Queue[object] = queue_module.Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with connect() as conn:
            with conn.transaction():
                consumer_pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
                assert repository.get_capability_lease(
                    conn, consume_first_lease.id, workspace_id, for_update=True
                ) is not None
                assert repository.consume_task_action(
                    conn,
                    action_id=consume_first_action.id,
                    workspace_id=workspace_id,
                    task_id=consume_first_task.id,
                    consumption=consumed,
                    actor="agent:lease-revoke-race",
                    now=now_utc(),
                ) == consumed
                revoke_future = pool.submit(
                    revoke_in_worker,
                    consume_first_lease,
                    revoke_pid,
                    revoke_result,
                )
                revoker_pid = revoke_pid.get(timeout=10)
                wait_until_blocked(revoker_pid, consumer_pid)
                assert not revoke_future.done()
        revoke_future.result(timeout=10)
        revoked = revoke_result.get(timeout=1)
        assert isinstance(revoked, CapabilityLease)
        assert revoked.status == CapabilityLeaseStatus.REVOKED

    with connect() as conn:
        stored = repository.get_capability_lease(conn, consume_first_lease.id, workspace_id)
        assert stored is not None and stored.consumed_runtime_seconds == 2
        assert repository.get_action_consumption(
            conn, consume_first_action.id, workspace_id
        ) == consumed
        assert conn.execute(
            "SELECT count(*) FROM trust_events WHERE workspace_id=%s AND entity_id=%s "
            "AND event_type=%s",
            (workspace_id, consume_first_action.id, TrustEventType.ACTION_CONSUMED.value),
        ).fetchone()[0] == 1
    with connect() as conn, conn.transaction():
        with pytest.raises(PostgresTrustRepositoryConflict, match="active signed capability lease"):
            repository.consume_task_action(
                conn,
                action_id=consume_first_action.id,
                workspace_id=workspace_id,
                task_id=consume_first_task.id,
                consumption=consumed,
                actor="agent:lease-revoke-race",
                now=now_utc(),
            )

    # Revoke first while retaining the lease row lock. Consumption must be
    # blocked until the revoke transaction commits, then reject without writing
    # a consumption event or increasing any lease budget.
    revoke_first_action, revoke_first_task, revoke_first_lease = prepare_action()
    consume_pid: queue_module.Queue[int] = queue_module.Queue()
    consume_result: queue_module.Queue[object] = queue_module.Queue()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with connect() as conn:
            with conn.transaction():
                revoker_pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
                revoked = repository.revoke_capability_lease(
                    conn,
                    lease_id=revoke_first_lease.id,
                    workspace_id=workspace_id,
                    actor_user_id=user_id,
                    actor="owner@example.test",
                    reason="Revoke before task action consumption.",
                    now=now_utc(),
                    trust_event_repository=PostgresTrustEventRepository(lambda: conn),
                )
                assert revoked.status == CapabilityLeaseStatus.REVOKED
                consume_future = pool.submit(
                    consume_in_worker,
                    revoke_first_action,
                    revoke_first_task,
                    consume_pid,
                    consume_result,
                )
                consumer_pid = consume_pid.get(timeout=10)
                wait_until_blocked(consumer_pid, revoker_pid)
                assert not consume_future.done()
        consume_future.result(timeout=10)
        consume_error = consume_result.get(timeout=1)
        assert isinstance(consume_error, PostgresTrustRepositoryConflict)
        assert "active signed capability lease" in str(consume_error)

    with connect() as conn:
        revoked_lease = repository.get_capability_lease(
            conn, revoke_first_lease.id, workspace_id
        )
        assert revoked_lease is not None
        assert revoked_lease.status == CapabilityLeaseStatus.REVOKED
        assert revoked_lease.consumed_runtime_seconds == 0
        assert repository.get_action_consumption(
            conn, revoke_first_action.id, workspace_id
        ) is None
        assert conn.execute(
            "SELECT count(*) FROM trust_events WHERE workspace_id=%s AND entity_id=%s "
            "AND event_type=%s",
            (workspace_id, revoke_first_action.id, TrustEventType.ACTION_CONSUMED.value),
        ).fetchone()[0] == 0

    # A signed but database-expired lease must be rejected using PostgreSQL's
    # clock even if the caller supplies a stale pre-expiry timestamp.
    expired_action, expired_task, expired_lease = prepare_action()
    with connect() as conn, conn.transaction():
        expired = expired_lease.model_copy(
            update={"expires_at": now_utc() - timedelta(seconds=1), "receipt_signature": "pending"}
        )
        expired.receipt_signature = sign_payload(lease_receipt_payload(expired))
        conn.execute(
            "UPDATE capability_leases SET expires_at=%s,payload=%s "
            "WHERE lease_id=%s AND workspace_id=%s",
            (
                expired.expires_at,
                Jsonb(expired.model_dump(mode="json")),
                expired.id,
                workspace_id,
            ),
        )
    stale_caller_time = expired_lease.issued_at
    with connect() as conn, conn.transaction():
        with pytest.raises(PostgresTrustRepositoryConflict, match="active signed capability lease"):
            repository.consume_task_action(
                conn,
                action_id=expired_action.id,
                workspace_id=workspace_id,
                task_id=expired_task.id,
                consumption=ActionConsumption(actual_runtime_seconds=2),
                actor="agent:expired-task-action",
                now=stale_caller_time,
            )
    with connect() as conn:
        persisted_expired = repository.get_capability_lease(
            conn, expired_lease.id, workspace_id
        )
        assert persisted_expired is not None
        assert persisted_expired.consumed_runtime_seconds == 0
        assert repository.get_action_consumption(conn, expired_action.id, workspace_id) is None
        assert conn.execute(
            "SELECT count(*) FROM trust_events WHERE workspace_id=%s AND entity_id=%s "
            "AND event_type=%s",
            (workspace_id, expired_action.id, TrustEventType.ACTION_CONSUMED.value),
        ).fetchone()[0] == 0


def test_postgres_task_only_action_route_allows_and_consumes_exact_action(
    trust_repository, monkeypatch: pytest.MonkeyPatch
):
    repository, connect, workspace_id, user_id, project_id, approval, task, lease = (
        _task_only_action_fixture(trust_repository)
    )
    monkeypatch.setenv("AIXION_POSTGRES_TASK_ACTIONS_ENABLED", "true")
    repositories = ApplicationRepositories.from_connector(connect)
    state = SimpleNamespace(
        postgres_runtime=SimpleNamespace(connect=connect, repositories=repositories),
        postgres_repositories=repositories,
        approval_decision_repository=repositories.approval_decisions,
        workspace_membership_repository=repositories.workspace_membership,
        trust_repository=repositories.trust,
        trust_event_repository=repositories.trust_events,
        project_repository=repositories.projects,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    reviewer = AuthUser(
        id=user_id,
        email=f"{user_id}@example.test",
        display_name="Task action route reviewer",
        role=UserRole.OWNER,
        workspace_id=workspace_id,
        email_verified=True,
    )
    monkeypatch.setattr(
        "app.trust_routes.workspace_id_for_request",
        lambda _user, _request: workspace_id,
    )
    action = ProposedAction(
        lease_id=lease.id,
        provider=task.provider,
        task_id=task.id,
        project_id=project_id,
        workspace_id=workspace_id,
        action_type=CapabilityActionType.RUN_COMMAND,
        repository=task.repository,
        branch=task.branch_preference,
        command=approval.test_plan[0],
        metadata={
            "approval_request_id": approval.id,
            "approved_payload_hash": approval.approved_payload_hash,
        },
    )
    evaluated = evaluate_manual_action(action, reviewer, request)
    assert evaluated.action.task_id == task.id
    assert evaluated.decision.decision == PolicyDecisionType.REQUIRE_APPROVAL
    authorization = decide_exact_action(
        action.id,
        ActionAuthorizationCreate(
            decision=ActionAuthorizationDecision.ALLOW,
            reason="Approve the exact task action.",
        ),
        reviewer,
        request,
    )
    assert authorization.action_payload_hash
    consumption = ActionConsumption(actual_runtime_seconds=1, evidence={"result": "complete"})
    consumed = consume_manual_action(action.id, consumption, reviewer, request)
    assert consumed == consumption
    with connect() as conn:
        assert repository.get_action_consumption(conn, action.id, workspace_id) == consumption
        assert conn.execute(
            "SELECT count(*) FROM trust_events WHERE workspace_id=%s AND entity_id=%s "
            "AND event_type=%s",
            (workspace_id, action.id, TrustEventType.ACTION_CONSUMED.value),
        ).fetchone()[0] == 1


def test_postgres_task_only_action_event_failure_rolls_back_evaluation(
    trust_repository,
    monkeypatch: pytest.MonkeyPatch,
):
    repository, connect, workspace_id, _user_id, _project_id, approval, task, lease = (
        _task_only_action_fixture(trust_repository)
    )
    monkeypatch.setenv("AIXION_POSTGRES_TASK_ACTIONS_ENABLED", "true")

    def fail_event(*_args, **_kwargs):
        raise RuntimeError("injected task action event failure")

    action = ProposedAction(
        lease_id=lease.id,
        provider=task.provider,
        task_id=task.id,
        project_id=task.project_id,
        workspace_id=workspace_id,
        action_type=CapabilityActionType.RUN_COMMAND,
        repository=task.repository,
        branch=task.branch_preference,
        command=approval.test_plan[0],
        metadata={
            "approval_request_id": approval.id,
            "approved_payload_hash": approval.approved_payload_hash,
        },
    )
    monkeypatch.setattr(PostgresTrustEventRepository, "append_in_transaction", fail_event)
    with pytest.raises(RuntimeError, match="injected task action event failure"):
        with connect() as conn, conn.transaction():
            repository.evaluate_proposed_action(
                conn,
                action=action,
                actor="agent:test-task-only",
                now=now_utc(),
                trust_event_repository=PostgresTrustEventRepository(lambda: conn),
            )
    with connect() as conn:
        assert repository.get_proposed_action(conn, action.id, workspace_id) is None
        assert repository.get_latest_policy_decision_for_action(conn, action.id, workspace_id) is None


def test_postgres_task_only_action_reviewer_event_failure_rolls_back_authorization(
    trust_repository,
    monkeypatch: pytest.MonkeyPatch,
):
    repository, connect, workspace_id, user_id, _project_id, approval, task, lease = (
        _task_only_action_fixture(trust_repository)
    )
    monkeypatch.setenv("AIXION_POSTGRES_TASK_ACTIONS_ENABLED", "true")
    action, decision = _evaluate_task_only_action(
        repository, connect, workspace_id=workspace_id, task=task,
        approval=approval, lease=lease,
    )
    assert decision.decision == PolicyDecisionType.REQUIRE_APPROVAL

    reviewer = AuthUser(
        id=user_id,
        email=f"{user_id}@example.test",
        display_name="Task action rollback reviewer",
        role=UserRole.OWNER,
        workspace_id=workspace_id,
        email_verified=True,
    )

    def fail_event(*_args, **_kwargs):
        raise RuntimeError("injected reviewer audit failure")

    monkeypatch.setattr(PostgresTrustEventRepository, "append_in_transaction", fail_event)
    with pytest.raises(RuntimeError, match="injected reviewer audit failure"):
        record_action_authorization_postgres(
            action.id,
            ActionAuthorizationCreate(
                decision=ActionAuthorizationDecision.ALLOW,
                reason="Allow only this action.",
            ),
            user=reviewer,
            workspace_id=workspace_id,
            connect=connect,
            trust_repository=repository,
            trust_event_repository=PostgresTrustEventRepository(connect),
            now=now_utc(),
        )

    with connect() as conn:
        assert repository.get_action_authorization_for_action(
            conn, action.id, workspace_id
        ) is None
        current = repository.get_latest_policy_decision_for_action(
            conn, action.id, workspace_id
        )
        assert current is not None
        assert current.decision == PolicyDecisionType.REQUIRE_APPROVAL
        assert conn.execute(
            "SELECT count(*) FROM trust_events WHERE workspace_id=%s AND entity_id=%s "
            "AND event_type=%s",
            (workspace_id, action.id, TrustEventType.ACTION_ALLOWED.value),
        ).fetchone()[0] == 0


def test_postgres_agent_run_creation_serializes_with_task_action_binding(
    trust_repository,
):
    """The task row lock makes the run absence check race-safe with run creation."""
    repository, connect, workspace_id, _user_id, project_id, approval, task, _lease = (
        _task_only_action_fixture(trust_repository)
    )
    lock_acquired = Event()
    release_lock = Event()
    run_attempting = Event()
    outcomes: queue_module.Queue[str] = queue_module.Queue()

    def lock_task_authority() -> None:
        try:
            with connect() as conn, conn.transaction():
                repository._lock_approved_task_authority(
                    conn, task_id=task.id, workspace_id=workspace_id
                )
                lock_acquired.set()
                if not release_lock.wait(timeout=10):
                    raise TimeoutError("test did not release task authority lock")
            outcomes.put("task-lock-committed")
        except Exception as error:  # pragma: no cover - thread error surfaced below
            outcomes.put(f"task-lock-error:{type(error).__name__}:{error}")

    def create_run() -> None:
        try:
            if not lock_acquired.wait(timeout=10):
                raise TimeoutError("task authority lock was not acquired")
            run_attempting.set()
            records = build_agent_run_creation_records(
                task=task,
                approval=approval,
                payload=AgentRunCreate(task_id=task.id),
                actor="test:concurrent-run",
            )
            with connect() as conn, conn.transaction():
                repository_for_run = ApplicationRepositories.from_connector(connect).agent_runs
                repository_for_run.create_in_transaction(
                    conn,
                    run=records.run,
                    steps=records.steps,
                    events=records.events,
                    audit=records.audit,
                    scheduled_audit=records.scheduled_audit,
                )
            outcomes.put("run-created")
        except Exception as error:  # pragma: no cover - thread error surfaced below
            outcomes.put(f"run-error:{type(error).__name__}:{error}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        lock_future = pool.submit(lock_task_authority)
        run_future = pool.submit(create_run)
        assert lock_acquired.wait(timeout=10)
        assert run_attempting.wait(timeout=10)
        # AgentRun creation must wait behind the task lock until the
        # task-action authority check has committed.
        sleep(0.1)
        assert not run_future.done()
        release_lock.set()
        lock_future.result(timeout=15)
        run_future.result(timeout=15)

    observed = {outcomes.get(timeout=1), outcomes.get(timeout=1)}
    assert observed == {"task-lock-committed", "run-created"}
    with connect() as conn, conn.transaction():
        with pytest.raises(PostgresTrustRepositoryConflict, match="has an AgentRun"):
            repository._lock_approved_task_authority(
                conn, task_id=task.id, workspace_id=workspace_id
            )
