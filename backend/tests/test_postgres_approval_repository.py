from __future__ import annotations

import os
import multiprocessing
import hashlib
import uuid
from dataclasses import replace
from collections.abc import Callable, Iterator
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
import time

import psycopg
import pytest
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app.agent_task_models import AgentTask, AgentTaskEvent, AgentTaskEventType, AgentTaskStatus
from app.agent_credential_models import AgentCredentialRecord
from app.agent_run_repository import AgentRunRepositoryConflict, PostgresAgentRunRepository
from app import approval_expiry, approval_repository as approval_repository_module
from app.approval_integrity import compute_approval_payload_hash
from app.approval_repository import (
    ApprovalDecisionConflict,
    ApprovalDeliveryEvidenceIntegrityError,
    ApprovalInvocationLeaseRejected,
    PostgresApprovalDecisionRepository,
)
from app.mcp_pending_repository import PostgresMCPPendingRepository
from app.models import (
    AgentAuthType,
    AgentProvider,
    ApprovalRequest,
    ApprovalStatus,
    AuditEvent,
    ExternalAgent,
    FileChange,
    Notification,
    MCPPendingRequest,
    MCPPendingStatus,
    Project,
    RiskAssessment,
    RiskLevel,
    WorkOrder,
    Workspace,
    now_utc,
)
from app.postgres_database import run_postgres_migrations
from app import postgres_database


_POSTGRES_DECISION_RACE_BARRIER: multiprocessing.synchronize.Barrier | None = None


def _initialize_postgres_decision_race(
    barrier: multiprocessing.synchronize.Barrier,
) -> None:
    global _POSTGRES_DECISION_RACE_BARRIER
    _POSTGRES_DECISION_RACE_BARRIER = barrier


def _commit_postgres_decision_in_process(
    dsn: str,
    schema: str,
    approval_payload: dict[str, object],
    target_status: str,
    name: str,
) -> str:
    """Compete from a separate OS process against the same durable approval."""
    barrier = _POSTGRES_DECISION_RACE_BARRIER
    if barrier is None:
        raise RuntimeError("PostgreSQL decision race barrier was not initialized")

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(
            sql.SQL("SET search_path TO {}").format(sql.Identifier(schema))
        )
        return connection

    approval = ApprovalRequest.model_validate(approval_payload)
    updated = _updated_approval(approval, ApprovalStatus(target_status))
    barrier.wait(timeout=10)
    repository = PostgresApprovalDecisionRepository(connect)
    try:
        repository.commit_decision(
            approval_id=approval.id,
            expected_status=ApprovalStatus.REQUESTED,
            expected_approval=approval,
            updated_approval=updated,
            audit_event=_audit(approval, updated, name),
            decision_notification=_notification(approval, name),
        )
        return "committed"
    except ApprovalDecisionConflict:
        return "conflict"


@pytest.fixture
def postgres_repository() -> Iterator[
    tuple[PostgresApprovalDecisionRepository, Callable[[], Connection]]
]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL integration tests")

    schema = f"ct_test_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(
            sql.SQL("SET search_path TO {}").format(sql.Identifier(schema))
        )
        return connection

    try:
        migrations_first_run = run_postgres_migrations(connect)
        migrations_second_run = run_postgres_migrations(connect)
        assert migrations_first_run == migrations_second_run
        yield PostgresApprovalDecisionRepository(connect), connect
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _seed(
    connect: Callable[[], Connection], *, with_task: bool = False
) -> tuple[Workspace, Project, ApprovalRequest, AgentTask | None]:
    suffix = uuid.uuid4().hex
    workspace = Workspace(
        id=f"workspace_{suffix}",
        name="PostgreSQL integration workspace",
        owner_user_id=f"user_{suffix}",
    )
    project = Project(
        id=f"project_{suffix}",
        name="PostgreSQL integration project",
        workspace_id=workspace.id,
    )
    approval = ApprovalRequest(
        id=f"approval_{suffix}",
        project_id=project.id,
        workspace_id=workspace.id,
        title="PostgreSQL decision transaction",
        summary="Persist the exact decision and audit together.",
        target_branch="feature/postgres-approval",
        files=[FileChange(path="README.md", diff="+approved")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.REQUESTED,
    )
    task = (
        AgentTask(
            id=f"task_{suffix}",
            project_id=project.id,
            workspace_id=workspace.id,
            title="Linked approval task",
            goal="Propagate the decision atomically.",
            approval_request_id=approval.id,
            status=AgentTaskStatus.WAITING_FOR_APPROVAL,
        )
        if with_task
        else None
    )

    with connect() as conn:
        conn.execute(
            "INSERT INTO workspaces(workspace_id, payload) VALUES (%s, %s)",
            (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
        )
        conn.execute(
            "INSERT INTO projects(project_id, workspace_id, payload) VALUES (%s, %s, %s)",
            (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))),
        )
        conn.execute(
            "INSERT INTO approval_requests "
            "(approval_id, project_id, workspace_id, source_agent_id, status, expires_at, "
            "created_at, updated_at, payload) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                approval.id,
                project.id,
                workspace.id,
                approval.source_agent_id,
                approval.status.value,
                approval.expires_at,
                approval.created_at,
                approval.updated_at,
                Jsonb(approval.model_dump(mode="json")),
            ),
        )
        if task is not None:
            conn.execute(
                "INSERT INTO agent_tasks "
                "(task_id, project_id, workspace_id, approval_request_id, status, updated_at, payload) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    task.id,
                    project.id,
                    workspace.id,
                    approval.id,
                    task.status.value,
                    task.updated_at,
                    Jsonb(task.model_dump(mode="json")),
                ),
            )
    return workspace, project, approval, task


def _insert_agent_credential(connect, agent: ExternalAgent) -> tuple[ExternalAgent, str]:
    token_hash = hashlib.sha256(b"postgres-approval-test-token").hexdigest()
    agent = agent.model_copy(update={"secret_hash": token_hash})
    credential = AgentCredentialRecord(agent_id=agent.id)
    with connect() as conn:
        conn.execute(
            "INSERT INTO external_agents "
            "(agent_id,workspace_id,created_by_user_id,provider,auth_type,enabled,secret_hash,payload) "
            "VALUES(%s,%s,NULL,%s,%s,%s,%s,%s)",
            (
                agent.id, agent.workspace_id, agent.provider.value,
                agent.auth_type.value, agent.enabled, token_hash,
                Jsonb(agent.model_dump(mode="json")),
            ),
        )
        conn.execute(
            "INSERT INTO external_agent_credentials "
            "(credential_id,agent_id,workspace_id,token_expires_at,token_revoked_at,"
            "token_rotated_at,last_used_at,failed_auth_count,rate_limit_per_minute,"
            "rate_limit_window_started_at,rate_limit_request_count,created_at,updated_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                credential.id, agent.id, agent.workspace_id,
                credential.token_expires_at, credential.token_revoked_at,
                credential.token_rotated_at, credential.last_used_at,
                credential.failed_auth_count, credential.rate_limit_per_minute,
                credential.rate_limit_window_started_at,
                credential.rate_limit_request_count, credential.created_at,
                credential.updated_at, Jsonb(credential.model_dump(mode="json")),
            ),
        )
    return agent, token_hash


def _persist_live_approved_invocation(
    connect: Callable[[], Connection],
    *,
    approval: ApprovalRequest,
    agent: ExternalAgent,
    source_invocation_id: str,
) -> ApprovalRequest:
    now = now_utc()
    approved = approval.model_copy(
        update={
            "source_provider": AgentProvider.ANTIGRAVITY,
            "source_agent_id": agent.id,
            "source_invocation_id": source_invocation_id,
            "status": ApprovalStatus.APPROVED,
            "approved_payload_hash_version": 2,
            "approved_at": now,
            "expires_at": now + timedelta(minutes=5),
        }
    )
    approved = approved.model_copy(
        update={"approved_payload_hash": compute_approval_payload_hash(approved)}
    )
    lease_expires_at = min(now + timedelta(seconds=60), approved.expires_at)
    with connect() as conn:
        conn.execute(
            "UPDATE approval_requests SET source_agent_id=%s,source_invocation_id=%s,"
            "status=%s,expires_at=%s,payload=%s WHERE approval_id=%s",
            (
                agent.id,
                source_invocation_id,
                approved.status.value,
                approved.expires_at,
                Jsonb(approved.model_dump(mode="json")),
                approved.id,
            ),
        )
        conn.execute(
            "INSERT INTO approval_invocation_leases "
            "(approval_id,workspace_id,source_agent_id,source_invocation_id,heartbeat_at,expires_at) "
            "VALUES(%s,%s,%s,%s,%s,%s)",
            (
                approved.id,
                approved.workspace_id,
                agent.id,
                source_invocation_id,
                now,
                lease_expires_at,
            ),
        )
    return approved


def test_postgres_phone_decision_requires_live_connector_lease_without_side_effects(
    postgres_repository,
) -> None:
    repository, connect = postgres_repository
    workspace, project, approval, _ = _seed(connect)
    agent = ExternalAgent(
        id=f"agent_decision_lease_{uuid.uuid4().hex}",
        provider=AgentProvider.ANTIGRAVITY,
        display_name="Decision lease fence connector",
        auth_type=AgentAuthType.API_KEY,
        workspace_id=workspace.id,
    )
    agent, _token_hash = _insert_agent_credential(connect, agent)
    invocation_id = f"decision-lease:{uuid.uuid4().hex}"
    created = now_utc()
    approval = approval.model_copy(
        update={
            "source_provider": AgentProvider.ANTIGRAVITY,
            "source_agent_id": agent.id,
            "source_invocation_id": invocation_id,
            "expires_at": created + timedelta(minutes=5),
            "updated_at": created,
        }
    )
    with connect() as connection:
        connection.execute(
            "UPDATE approval_requests SET source_agent_id=%s,source_invocation_id=%s,"
            "expires_at=%s,updated_at=%s,payload=%s "
            "WHERE approval_id=%s",
            (
                agent.id,
                invocation_id,
                approval.expires_at,
                approval.updated_at,
                Jsonb(approval.model_dump(mode="json")),
                approval.id,
            ),
        )
        lease_expiry = created - timedelta(seconds=1)
        connection.execute(
            "INSERT INTO approval_invocation_leases "
            "(approval_id,workspace_id,source_agent_id,source_invocation_id,heartbeat_at,expires_at) "
            "VALUES(%s,%s,%s,%s,%s,%s)",
            (
                approval.id,
                workspace.id,
                agent.id,
                invocation_id,
                lease_expiry - timedelta(seconds=1),
                lease_expiry,
            ),
        )

    with connect() as connection:
        audit_count_before = connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
        notification_count_before = connection.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
    approved = _updated_approval(approval, ApprovalStatus.APPROVED)
    with pytest.raises(ApprovalDecisionConflict, match="invocation is no longer live"):
        repository.commit_decision(
            approval_id=approval.id,
            expected_status=ApprovalStatus.REQUESTED,
            expected_approval=approval,
            updated_approval=approved,
            audit_event=_audit(approval, approved, f"expired-lease-{uuid.uuid4().hex}"),
            decision_notification=_notification(approval, f"expired-lease-{uuid.uuid4().hex}"),
        )

    persisted = repository.get(approval.id)
    assert persisted == approval
    with connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == audit_count_before
        assert connection.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == notification_count_before


def _seed_mcp_pending(
    connect: Callable[[], Connection], approval: ApprovalRequest
) -> MCPPendingRequest:
    pending = MCPPendingRequest(
        id=f"mcp_pending_{uuid.uuid4().hex}",
        project_id=approval.project_id,
        workspace_id=approval.workspace_id,
        approval_request_id=approval.id,
        server_name="test-child",
        tool_name="write_file",
        arguments={"path": "README.md"},
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO mcp_pending_requests(pending_id,approval_id,project_id,workspace_id,"
            "status,attempts,lease_expires_at,created_at,updated_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                pending.id,
                approval.id,
                approval.project_id,
                approval.workspace_id,
                pending.status.value,
                pending.attempts,
                pending.lease_expires_at,
                pending.created_at,
                pending.updated_at,
                Jsonb(pending.model_dump(mode="json")),
            ),
        )
    return pending


@pytest.mark.parametrize(
    ("decision_status", "pending_status"),
    [
        (ApprovalStatus.APPROVED, MCPPendingStatus.WAITING_FOR_APPROVAL),
        (ApprovalStatus.DENIED, MCPPendingStatus.BLOCKED_BY_DECISION),
        (ApprovalStatus.REVISION_REQUESTED, MCPPendingStatus.BLOCKED_BY_DECISION),
    ],
)
def test_postgres_mcp_pending_tracks_approval_decision_atomically(
    postgres_repository,
    decision_status: ApprovalStatus,
    pending_status: MCPPendingStatus,
) -> None:
    repository, connect = postgres_repository
    _, _, approval, _ = _seed(connect)
    pending = _seed_mcp_pending(connect, approval)
    updated = _updated_approval(approval, decision_status)

    repository.commit_decision(
        approval_id=approval.id,
        expected_status=ApprovalStatus.REQUESTED,
        expected_approval=approval,
        updated_approval=updated,
        audit_event=_audit(approval, updated, f"mcp-{decision_status.value}"),
        decision_notification=_notification(approval, f"mcp-{decision_status.value}"),
    )

    with connect() as conn:
        approval_row = conn.execute(
            "SELECT status FROM approval_requests WHERE approval_id=%s", (approval.id,)
        ).fetchone()
        pending_row = conn.execute(
            "SELECT status,payload FROM mcp_pending_requests WHERE pending_id=%s",
            (pending.id,),
        ).fetchone()
    assert approval_row == (decision_status.value,)
    assert pending_row[0] == pending_status.value
    assert MCPPendingRequest.model_validate(pending_row[1]).status == pending_status


def test_postgres_mcp_pending_decision_update_failure_rolls_back_approval(
    postgres_repository,
) -> None:
    repository, connect = postgres_repository
    _, _, approval, _ = _seed(connect)
    pending = _seed_mcp_pending(connect, approval)
    updated = _updated_approval(approval, ApprovalStatus.DENIED)
    with connect() as conn:
        conn.execute(
            "CREATE FUNCTION reject_mcp_pending_decision_update() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected pending update failure'; END $$"
        )
        conn.execute(
            "CREATE TRIGGER reject_mcp_pending_decision_update BEFORE UPDATE "
            "ON mcp_pending_requests FOR EACH ROW EXECUTE FUNCTION reject_mcp_pending_decision_update()"
        )

    with pytest.raises(psycopg.errors.RaiseException, match="injected pending update failure"):
        repository.commit_decision(
            approval_id=approval.id,
            expected_status=ApprovalStatus.REQUESTED,
            expected_approval=approval,
            updated_approval=updated,
            audit_event=_audit(approval, updated, "mcp-pending-rollback"),
            decision_notification=_notification(approval, "mcp-pending-rollback"),
        )

    assert repository.get(approval.id) == approval
    with connect() as conn:
        pending_row = conn.execute(
            "SELECT status,payload FROM mcp_pending_requests WHERE pending_id=%s",
            (pending.id,),
        ).fetchone()
        assert pending_row[0] == MCPPendingStatus.WAITING_FOR_APPROVAL.value
        assert MCPPendingRequest.model_validate(pending_row[1]).status == MCPPendingStatus.WAITING_FOR_APPROVAL
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE approval_id=%s", (approval.id,)
        ).fetchone()[0] == 0


@pytest.mark.parametrize("mismatch", ["approval_project_scope", "pending_status"])
def test_postgres_mcp_pending_decision_rejects_inconsistent_link(
    postgres_repository,
    mismatch: str,
) -> None:
    repository, connect = postgres_repository
    workspace, _, approval, _ = _seed(connect)
    pending = _seed_mcp_pending(connect, approval)
    if mismatch == "approval_project_scope":
        other_project = Project(
            id=f"project_other_{uuid.uuid4().hex}",
            name="Other project in same workspace",
            workspace_id=workspace.id,
        )
        with connect() as conn:
            conn.execute(
                "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
                (other_project.id, workspace.id, Jsonb(other_project.model_dump(mode="json"))),
            )
        invalid = pending.model_copy(update={"project_id": other_project.id})
    else:
        invalid = pending.model_copy(update={"status": MCPPendingStatus.DEAD_LETTER})
    with connect() as conn:
        if mismatch == "approval_project_scope":
            conn.execute(
                "UPDATE mcp_pending_requests SET project_id=%s,payload=%s WHERE pending_id=%s",
                (invalid.project_id, Jsonb(invalid.model_dump(mode="json")), pending.id),
            )
        else:
            conn.execute(
                "UPDATE mcp_pending_requests SET status=%s,payload=%s WHERE pending_id=%s",
                (invalid.status.value, Jsonb(invalid.model_dump(mode="json")), pending.id),
            )
    updated = _updated_approval(approval, ApprovalStatus.DENIED)

    with pytest.raises(ApprovalDecisionConflict, match="Linked MCP pending request"):
        repository.commit_decision(
            approval_id=approval.id,
            expected_status=ApprovalStatus.REQUESTED,
            expected_approval=approval,
            updated_approval=updated,
            audit_event=_audit(approval, updated, f"mcp-pending-mismatch-{mismatch}"),
            decision_notification=_notification(approval, f"mcp-pending-mismatch-{mismatch}"),
        )

    assert repository.get(approval.id) == approval
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE approval_id=%s", (approval.id,)
        ).fetchone()[0] == 0


def test_postgres_mcp_approval_and_forward_claim_race_has_consistent_lock_order(
    postgres_repository,
) -> None:
    repository, connect = postgres_repository
    _, _, approval, _ = _seed(connect)
    pending = _seed_mcp_pending(connect, approval)
    pending_repository = PostgresMCPPendingRepository(connect)
    barrier = Barrier(2)

    def decide() -> str:
        updated = _updated_approval(approval, ApprovalStatus.APPROVED)
        barrier.wait(timeout=10)
        repository.commit_decision(
            approval_id=approval.id,
            expected_status=ApprovalStatus.REQUESTED,
            expected_approval=approval,
            updated_approval=updated,
            audit_event=_audit(approval, updated, "mcp-claim-race"),
            decision_notification=_notification(approval, "mcp-claim-race"),
        )
        return "approved"

    def claim() -> MCPPendingRequest | None:
        barrier.wait(timeout=10)
        return pending_repository.claim_forwarding(
            pending_id=pending.id,
            approval_id=approval.id,
            workspace_id=approval.workspace_id,
            project_id=approval.project_id,
            server_name=pending.server_name,
            expected_server_id="missing-mcp-server",
            expected_server_revision=now_utc(),
            owner="test-worker",
            now=now_utc(),
            lease_expires_at=now_utc() + timedelta(minutes=1),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        decision_future = executor.submit(decide)
        claim_future = executor.submit(claim)
        assert decision_future.result(timeout=10) == "approved"
        claimed = claim_future.result(timeout=10)

    assert repository.get(approval.id).status == ApprovalStatus.APPROVED
    durable_pending = pending_repository.get(pending.id)
    assert durable_pending is not None
    assert durable_pending.status in {
        MCPPendingStatus.WAITING_FOR_APPROVAL,
        MCPPendingStatus.FORWARDING,
    }
    assert (claimed is None) == (durable_pending.status == MCPPendingStatus.WAITING_FOR_APPROVAL)


def test_postgres_approved_invocation_delivery_claim_is_at_most_once(postgres_repository) -> None:
    repository, connect = postgres_repository
    workspace, _project, approval, _task = _seed(connect)
    agent = ExternalAgent(
        id=f"agent_{uuid.uuid4().hex}",
        provider=AgentProvider.ANTIGRAVITY,
        display_name="Antigravity connector",
        workspace_id=workspace.id,
    )
    agent, presented_token_hash = _insert_agent_credential(connect, agent)
    approved = _persist_live_approved_invocation(
        connect,
        approval=approval,
        agent=agent,
        source_invocation_id="antigravity:v1:postgres-claim",
    )
    event = AuditEvent(
        event_type="approval.delivery_claimed",
        actor=f"agent:{agent.id}",
        entity_id=approved.id,
        workspace_id=workspace.id,
        details={"source_invocation_id": approved.source_invocation_id},
    )
    kwargs = {
        "approval_id": approved.id,
        "workspace_id": workspace.id,
        "source_agent_id": agent.id,
        "source_invocation_id": approved.source_invocation_id,
        "expected_payload_hash": approved.approved_payload_hash,
        "presented_token_hash": presented_token_hash,
        "audit_event": event,
    }

    barrier = Barrier(2)

    def claim() -> bool:
        barrier.wait(timeout=5)
        return repository.claim_approved_delivery(**kwargs)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: claim(), range(2)))
    assert sorted(outcomes) == [False, True]
    with connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=%s",
            (approved.id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type='approval.delivery_claimed' "
            "AND entity_id=%s",
            (approved.id,),
        ).fetchone()[0] == 1

    failure_event = AuditEvent(
        event_type="approval.delivery_failed",
        actor=f"agent:{agent.id}",
        entity_id=approved.id,
        workspace_id=workspace.id,
        details={
            "source_agent_id": agent.id,
            "source_invocation_id": approved.source_invocation_id,
            "approved_payload_hash": approved.approved_payload_hash,
            "reason_code": "CLAIM_RESPONSE_AMBIGUOUS",
        },
    )
    failure_args = {
        "approval_id": approved.id,
        "workspace_id": workspace.id,
        "source_agent_id": agent.id,
        "source_invocation_id": approved.source_invocation_id,
        "expected_payload_hash": approved.approved_payload_hash,
        "reason_code": "CLAIM_RESPONSE_AMBIGUOUS",
        "audit_event": failure_event,
    }
    assert repository.record_approved_delivery_failure(**failure_args) is True
    assert repository.record_approved_delivery_failure(
        **{**failure_args, "audit_event": failure_event.model_copy(update={"id": "duplicate-failure-audit"})}
    ) is False
    with connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM approval_delivery_failures WHERE approval_id=%s",
            (approved.id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type='approval.delivery_failed' "
            "AND entity_id=%s",
            (approved.id,),
        ).fetchone()[0] == 1


def test_postgres_delivery_outcomes_are_batched_scoped_and_explicit(
    postgres_repository,
) -> None:
    repository, connect = postgres_repository
    workspace, _project, approval, _ = _seed(connect)
    agent = ExternalAgent(
        id=f"agent_delivery_evidence_{uuid.uuid4().hex}",
        provider=AgentProvider.ANTIGRAVITY,
        display_name="Delivery evidence connector",
        workspace_id=workspace.id,
    )
    agent, _token_hash = _insert_agent_credential(connect, agent)
    claimed = _persist_live_approved_invocation(
        connect,
        approval=approval,
        agent=agent,
        source_invocation_id=f"claimed:{uuid.uuid4().hex}",
    )
    failed_approval = approval.model_copy(
        update={"id": f"approval_delivery_failure_{uuid.uuid4().hex}"}
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO approval_requests "
            "(approval_id,project_id,workspace_id,source_agent_id,status,expires_at,"
            "created_at,updated_at,payload) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                failed_approval.id,
                failed_approval.project_id,
                failed_approval.workspace_id,
                failed_approval.source_agent_id,
                failed_approval.status.value,
                failed_approval.expires_at,
                failed_approval.created_at,
                failed_approval.updated_at,
                Jsonb(failed_approval.model_dump(mode="json")),
            ),
        )
    failed = _persist_live_approved_invocation(
        connect,
        approval=failed_approval,
        agent=agent,
        source_invocation_id=f"failed:{uuid.uuid4().hex}",
    )
    claimed_at = now_utc()
    with connect() as conn:
        for item in (claimed, failed):
            conn.execute(
                "INSERT INTO approval_delivery_claims "
                "(approval_id,workspace_id,source_agent_id,source_invocation_id,claimed_at) "
                "VALUES(%s,%s,%s,%s,%s)",
                (
                    item.id,
                    workspace.id,
                    agent.id,
                    item.source_invocation_id,
                    claimed_at,
                ),
            )
        conn.execute(
            "INSERT INTO approval_delivery_failures "
            "(approval_id,workspace_id,source_agent_id,source_invocation_id,reason_code,reported_at) "
            "VALUES(%s,%s,%s,%s,%s,%s)",
            (
                failed.id,
                workspace.id,
                agent.id,
                failed.source_invocation_id,
                "CLAIM_RESPONSE_AMBIGUOUS",
                claimed_at + timedelta(seconds=1),
            ),
        )

    outcomes = repository.list_delivery_outcomes(
        workspace_id=workspace.id,
        approval_ids=[claimed.id, failed.id, "approval-without-evidence"],
    )

    assert outcomes == {
        claimed.id: "CLAIM_RECORDED",
        failed.id: "FAILURE_REPORTED",
        "approval-without-evidence": "NOT_CONFIRMED",
    }

    foreign_workspace, _project, foreign_approval, _ = _seed(connect)
    foreign_agent = ExternalAgent(
        id=f"agent_foreign_delivery_evidence_{uuid.uuid4().hex}",
        provider=AgentProvider.ANTIGRAVITY,
        display_name="Foreign delivery evidence connector",
        workspace_id=foreign_workspace.id,
    )
    foreign_agent, _token_hash = _insert_agent_credential(connect, foreign_agent)
    foreign_approved = _persist_live_approved_invocation(
        connect,
        approval=foreign_approval,
        agent=foreign_agent,
        source_invocation_id=f"foreign:{uuid.uuid4().hex}",
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO approval_delivery_claims "
            "(approval_id,workspace_id,source_agent_id,source_invocation_id,claimed_at) "
            "VALUES(%s,%s,%s,%s,%s)",
            (
                foreign_approved.id,
                foreign_workspace.id,
                foreign_agent.id,
                foreign_approved.source_invocation_id,
                claimed_at,
            ),
        )
    with pytest.raises(ApprovalDeliveryEvidenceIntegrityError, match="workspace approval"):
        repository.list_delivery_outcomes(
            workspace_id=workspace.id, approval_ids=[foreign_approved.id]
        )


@pytest.mark.parametrize("expiry_target", ("approval", "lease"))
def test_postgres_claim_waiting_on_approval_lock_rechecks_expiry(
    postgres_repository, monkeypatch, expiry_target: str
) -> None:
    repository, connect = postgres_repository
    workspace, _project, approval, _task = _seed(connect)
    agent = ExternalAgent(
        id=f"agent_claim_lock_expiry_{uuid.uuid4().hex}",
        provider=AgentProvider.ANTIGRAVITY,
        display_name="Claim lock-expiry connector",
        workspace_id=workspace.id,
    )
    agent, presented_token_hash = _insert_agent_credential(connect, agent)
    invocation_id = f"claim-lock-expiry:{uuid.uuid4().hex}"
    approved = _persist_live_approved_invocation(
        connect,
        approval=approval,
        agent=agent,
        source_invocation_id=invocation_id,
    )
    deadline = now_utc() + timedelta(seconds=2)
    approval_expiry = (
        deadline
        if expiry_target == "approval"
        else now_utc() + timedelta(minutes=5)
    )
    approved = approved.model_copy(update={"expires_at": approval_expiry})
    with connect() as connection:
        connection.execute(
            "UPDATE approval_requests SET expires_at=%s,payload=%s WHERE approval_id=%s",
            (approval_expiry, Jsonb(approved.model_dump(mode="json")), approved.id),
        )
        connection.execute(
            "UPDATE approval_invocation_leases SET expires_at=%s WHERE approval_id=%s",
            (deadline, approved.id),
        )

    claim_started_at = now_utc()
    assert claim_started_at < deadline
    claim_event = AuditEvent(
        event_type="approval.delivery_claimed",
        actor=f"agent:{agent.id}",
        entity_id=approved.id,
        workspace_id=workspace.id,
        created_at=claim_started_at,
        details={"source_invocation_id": invocation_id},
    )
    credentials_locked = Event()
    original_validate_credentials = approval_repository_module._validate_postgres_delivery_credential

    def observe_credential_lock(*args, **kwargs):
        result = original_validate_credentials(*args, **kwargs)
        if kwargs.get("source_agent_id") == agent.id:
            credentials_locked.set()
        return result

    monkeypatch.setattr(
        approval_repository_module,
        "_validate_postgres_delivery_credential",
        observe_credential_lock,
    )
    blocker = connect()
    blocker.execute("BEGIN")
    blocker.execute(
        "SELECT approval_id FROM approval_requests WHERE approval_id=%s FOR UPDATE",
        (approved.id,),
    ).fetchone()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                repository.claim_approved_delivery,
                approval_id=approved.id,
                workspace_id=workspace.id,
                source_agent_id=agent.id,
                source_invocation_id=invocation_id,
                expected_payload_hash=approved.approved_payload_hash,
                presented_token_hash=presented_token_hash,
                audit_event=claim_event,
            )
            assert credentials_locked.wait(timeout=2)
            assert now_utc() < deadline
            assert not future.done()
            time.sleep(max(0.0, (deadline - now_utc()).total_seconds()) + 0.05)
            assert now_utc() >= deadline
            blocker.rollback()
            with pytest.raises(ApprovalInvocationLeaseRejected, match="expired"):
                future.result(timeout=5)
    finally:
        blocker.close()

    with connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=%s",
            (approved.id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type='approval.delivery_claimed' "
            "AND entity_id=%s",
            (approved.id,),
        ).fetchone()[0] == 0

def test_postgres_delivery_failure_report_requires_matching_claim(postgres_repository) -> None:
    repository, connect = postgres_repository
    workspace, _project, approval, _task = _seed(connect)
    agent = ExternalAgent(
        id=f"agent_{uuid.uuid4().hex}",
        provider=AgentProvider.ANTIGRAVITY,
        display_name="Antigravity connector",
        workspace_id=workspace.id,
    )
    agent, presented_token_hash = _insert_agent_credential(connect, agent)
    approved = _persist_live_approved_invocation(
        connect,
        approval=approval,
        agent=agent,
        source_invocation_id="antigravity:v1:unclaimed",
    )
    failure_event = AuditEvent(
        event_type="approval.delivery_failed",
        actor=f"agent:{agent.id}",
        entity_id=approved.id,
        workspace_id=workspace.id,
        details={
            "source_agent_id": agent.id,
            "source_invocation_id": approved.source_invocation_id,
            "approved_payload_hash": approved.approved_payload_hash,
            "reason_code": "CLAIM_RESPONSE_AMBIGUOUS",
        },
    )

    with pytest.raises(ApprovalDecisionConflict, match="matching durable claim"):
        repository.record_approved_delivery_failure(
            approval_id=approved.id,
            workspace_id=workspace.id,
            source_agent_id=agent.id,
            source_invocation_id=approved.source_invocation_id,
            expected_payload_hash=approved.approved_payload_hash,
            reason_code="CLAIM_RESPONSE_AMBIGUOUS",
            audit_event=failure_event,
        )

    claim_event = AuditEvent(
        event_type="approval.delivery_claimed",
        actor=f"agent:{agent.id}",
        entity_id=approved.id,
        workspace_id=workspace.id,
        details={"source_invocation_id": approved.source_invocation_id},
    )
    assert repository.claim_approved_delivery(
        approval_id=approved.id,
        workspace_id=workspace.id,
        source_agent_id=agent.id,
        source_invocation_id=approved.source_invocation_id,
        expected_payload_hash=approved.approved_payload_hash,
        presented_token_hash=presented_token_hash,
        audit_event=claim_event,
    ) is True
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with connect() as conn:
            conn.execute(
                "INSERT INTO approval_delivery_failures "
                "(approval_id,workspace_id,source_agent_id,source_invocation_id,reason_code,reported_at) "
                "VALUES(%s,%s,%s,%s,'CLAIM_RESPONSE_AMBIGUOUS',%s)",
                (
                    approved.id,
                    f"{workspace.id}-other",
                    agent.id,
                    approved.source_invocation_id,
                    now_utc(),
                ),
            )


def test_postgres_delivery_failure_identity_migration_rejects_legacy_mismatch(
    postgres_repository,
) -> None:
    _repository, connect = postgres_repository
    workspace, _project, approval, _task = _seed(connect)
    agent = ExternalAgent(
        id=f"agent_{uuid.uuid4().hex}",
        provider=AgentProvider.ANTIGRAVITY,
        display_name="Antigravity connector",
        workspace_id=workspace.id,
    )
    approved = approval.model_copy(
        update={
            "source_provider": AgentProvider.ANTIGRAVITY,
            "source_agent_id": agent.id,
            "source_invocation_id": "antigravity:v1:legacy-failure",
            "status": ApprovalStatus.APPROVED,
        }
    )
    approved = approved.model_copy(
        update={"approved_payload_hash": compute_approval_payload_hash(approved)}
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO external_agents "
            "(agent_id,workspace_id,created_by_user_id,provider,auth_type,enabled,secret_hash,payload) "
            "VALUES(%s,%s,NULL,%s,%s,%s,NULL,%s)",
            (
                agent.id,
                workspace.id,
                agent.provider.value,
                agent.auth_type.value,
                agent.enabled,
                Jsonb(agent.model_dump(mode="json")),
            ),
        )
        conn.execute(
            "UPDATE approval_requests SET source_agent_id=%s,source_invocation_id=%s,status=%s,payload=%s "
            "WHERE approval_id=%s",
            (
                agent.id,
                approved.source_invocation_id,
                approved.status.value,
                Jsonb(approved.model_dump(mode="json")),
                approved.id,
            ),
        )
        conn.execute("ALTER TABLE approval_delivery_failures "
                     "DROP CONSTRAINT approval_delivery_failure_claim_identity_fk")
        conn.execute("DROP INDEX idx_approval_delivery_claims_full_identity")
        conn.execute(
            "INSERT INTO approval_delivery_claims "
            "(approval_id,workspace_id,source_agent_id,source_invocation_id,claimed_at) "
            "VALUES(%s,%s,%s,%s,%s)",
            (
                approved.id,
                workspace.id,
                agent.id,
                approved.source_invocation_id,
                now_utc(),
            ),
        )
        conn.execute(
            "INSERT INTO approval_delivery_failures "
            "(approval_id,workspace_id,source_agent_id,source_invocation_id,reason_code,reported_at) "
            "VALUES(%s,%s,%s,%s,'CLAIM_RESPONSE_AMBIGUOUS',%s)",
            (
                approved.id,
                f"{workspace.id}-wrong",
                agent.id,
                approved.source_invocation_id,
                now_utc(),
            ),
        )
        conn.execute(
            "DELETE FROM schema_migrations "
            "WHERE migration_id='pg_0009_delivery_failure_claim_identity'"
        )

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        run_postgres_migrations(connect)

    with connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM schema_migrations "
            "WHERE migration_id='pg_0009_delivery_failure_claim_identity'"
        ).fetchone() is None


def test_postgres_invocation_migration_upgrades_prior_schema_with_existing_approvals(
    postgres_repository,
) -> None:
    repository, connect = postgres_repository
    _, _, approval, _ = _seed(connect)

    with connect() as conn:
        conn.execute("DROP INDEX idx_approval_invocation_identity")
        conn.execute("ALTER TABLE approval_requests DROP CONSTRAINT approval_invocation_payload_check")
        conn.execute("ALTER TABLE approval_requests DROP COLUMN source_invocation_id")
        conn.execute(
            "DELETE FROM schema_migrations "
            "WHERE migration_id='pg_0006_approval_invocation_identity'"
        )

    run_postgres_migrations(connect)

    assert repository.get(approval.id) == approval
    with connect() as conn:
        assert conn.execute(
            "SELECT source_invocation_id FROM approval_requests WHERE approval_id=%s",
            (approval.id,),
        ).fetchone()[0] is None
        assert conn.execute(
            "SELECT to_regclass('idx_approval_invocation_identity')"
        ).fetchone()[0] == "idx_approval_invocation_identity"


def test_postgres_migration_runner_rejects_changed_applied_definition(
    postgres_repository, monkeypatch
) -> None:
    _, connect = postgres_repository
    original = postgres_database.POSTGRES_MIGRATIONS
    monkeypatch.setattr(
        postgres_database,
        "POSTGRES_MIGRATIONS",
        (replace(original[0], statements=original[0].statements + ("SELECT 1",)), *original[1:]),
    )

    with pytest.raises(RuntimeError, match="migration definition changed"):
        run_postgres_migrations(connect)


def test_postgres_migration_runner_requires_review_for_checksum_free_ledger(
    postgres_repository,
) -> None:
    _, connect = postgres_repository
    with connect() as conn:
        conn.execute(
            "ALTER TABLE schema_migrations "
            "ALTER COLUMN migration_checksum DROP NOT NULL"
        )
        conn.execute(
            "UPDATE schema_migrations SET migration_checksum = NULL"
        )

    with pytest.raises(RuntimeError, match="operator verification is required"):
        run_postgres_migrations(connect)


def _updated_approval(
    approval: ApprovalRequest, status: ApprovalStatus
) -> ApprovalRequest:
    updated = approval.model_copy(update={"status": status, "updated_at": now_utc()})
    if status == ApprovalStatus.APPROVED:
        updated = updated.model_copy(
            update={"approved_payload_hash": compute_approval_payload_hash(updated)}
        )
    return updated


def _audit(
    approval: ApprovalRequest,
    updated: ApprovalRequest,
    name: str,
    *,
    event_type: str = "approval.decision",
) -> AuditEvent:
    return AuditEvent(
        id=f"audit_{name}",
        event_type=event_type,
        entity_id=approval.id,
        workspace_id=approval.workspace_id,
        details={
            "previous_status": approval.status,
            "new_status": updated.status,
            "approved_payload_hash": updated.approved_payload_hash,
        },
    )


def _notification(approval: ApprovalRequest, name: str) -> Notification:
    return Notification(
        id=f"notification_{name}",
        title="Approval decision recorded",
        body="Open the app to view the decision.",
        entity_type="approval_request",
        entity_id=approval.id,
        workspace_id=approval.workspace_id,
    )


def _creation_records(approval: ApprovalRequest, name: str) -> tuple[AuditEvent, Notification]:
    return (
        AuditEvent(
            id=f"audit_create_{name}",
            event_type="approval.created",
            actor="owner@example.test",
            entity_id=approval.id,
            workspace_id=approval.workspace_id,
            details={"project_id": approval.project_id, "status": approval.status},
        ),
        Notification(
            id=f"notification_create_{name}",
            title="Approval requested",
            body="Open the app to review the request.",
            entity_type="approval_request",
            entity_id=approval.id,
            workspace_id=approval.workspace_id,
        ),
    )


def _new_approval_from(existing: ApprovalRequest, suffix: str) -> ApprovalRequest:
    return existing.model_copy(update={"id": f"approval_created_{suffix}"})


def test_postgres_create_persists_approval_audit_and_notification_atomically(
    postgres_repository: tuple[
        PostgresApprovalDecisionRepository, Callable[[], Connection]
    ],
) -> None:
    repository, connect = postgres_repository
    workspace, project, existing, _ = _seed(connect)
    agent = ExternalAgent(
        id="agent_creation_atomic",
        provider=AgentProvider.ANTIGRAVITY,
        display_name="Antigravity source",
        auth_type=AgentAuthType.API_KEY,
        allowed_project_ids=[project.id],
        workspace_id=workspace.id,
    )
    work_order = WorkOrder(
        id="work_order_creation_atomic",
        project_id=project.id,
        goal="Run the approved agent work",
        workspace_id=workspace.id,
        source_agent_id=agent.id,
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO external_agents "
            "(agent_id,workspace_id,created_by_user_id,provider,auth_type,enabled,secret_hash,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (agent.id, agent.workspace_id, agent.created_by_user_id, agent.provider.value,
             agent.auth_type.value, agent.enabled, agent.secret_hash,
             Jsonb(agent.model_dump(mode="json"))),
        )
        conn.execute(
            "INSERT INTO work_orders "
            "(work_order_id,project_id,workspace_id,idea_id,source_agent_id,created_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (work_order.id, work_order.project_id, work_order.workspace_id, work_order.idea_id,
             work_order.source_agent_id, work_order.created_at,
             Jsonb(work_order.model_dump(mode="json"))),
        )
    approval = _new_approval_from(existing, "atomic").model_copy(
        update={
            "source_agent_id": agent.id,
            "work_order_id": work_order.id,
            "source_invocation_id": "antigravity:v1:call-atomic",
        }
    )
    audit_event, notification = _creation_records(approval, "atomic")

    repository.create(
        approval=approval,
        audit_event=audit_event,
        notification=notification,
    )

    persisted = repository.get(approval.id)
    assert persisted == approval
    assert persisted is not None
    assert persisted.source_invocation_id == "antigravity:v1:call-atomic"
    assert repository.get_by_invocation(
        workspace_id=workspace.id,
        source_agent_id=agent.id,
        source_invocation_id="antigravity:v1:call-atomic",
    ) == approval

    duplicate = _new_approval_from(approval, "atomic_duplicate")
    duplicate_audit, duplicate_notification = _creation_records(duplicate, "atomic_duplicate")
    with pytest.raises(ApprovalDecisionConflict, match="identifiers already exist"):
        repository.create(
            approval=duplicate,
            audit_event=duplicate_audit,
            notification=duplicate_notification,
        )
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE approval_id=%s", (approval.id,)
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM notifications WHERE approval_id=%s", (approval.id,)
        ).fetchone()[0] == 1


def test_postgres_create_rejects_missing_work_order_without_partial_writes(
    postgres_repository: tuple[
        PostgresApprovalDecisionRepository, Callable[[], Connection]
    ],
) -> None:
    repository, connect = postgres_repository
    _, _, existing, _ = _seed(connect)
    approval = _new_approval_from(existing, "missing_work_order").model_copy(
        update={"work_order_id": "work_order_missing"}
    )
    audit_event, notification = _creation_records(approval, "missing_work_order")

    with pytest.raises(ApprovalDecisionConflict, match="work order is missing"):
        repository.create(
            approval=approval,
            audit_event=audit_event,
            notification=notification,
        )

    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM approval_requests WHERE approval_id=%s", (approval.id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE event_id=%s", (audit_event.id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM notifications WHERE notification_id=%s", (notification.id,)
        ).fetchone()[0] == 0


def test_postgres_create_rejects_work_order_for_another_project_without_partial_writes(
    postgres_repository: tuple[
        PostgresApprovalDecisionRepository, Callable[[], Connection]
    ],
) -> None:
    repository, connect = postgres_repository
    workspace, project, existing, _ = _seed(connect)
    other_project = Project(
        id=f"project_work_order_mismatch_{uuid.uuid4().hex}",
        name="Different project",
        workspace_id=workspace.id,
    )
    work_order = WorkOrder(
        id=f"work_order_mismatch_{uuid.uuid4().hex}",
        project_id=other_project.id,
        goal="Belongs to another project",
        workspace_id=workspace.id,
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
            (other_project.id, other_project.workspace_id, Jsonb(other_project.model_dump(mode="json"))),
        )
        conn.execute(
            "INSERT INTO work_orders "
            "(work_order_id,project_id,workspace_id,idea_id,source_agent_id,created_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (
                work_order.id,
                work_order.project_id,
                work_order.workspace_id,
                work_order.idea_id,
                work_order.source_agent_id,
                work_order.created_at,
                Jsonb(work_order.model_dump(mode="json")),
            ),
        )
    approval = _new_approval_from(existing, "mismatched_work_order").model_copy(
        update={"work_order_id": work_order.id}
    )
    audit_event, notification = _creation_records(approval, "mismatched_work_order")

    with pytest.raises(ApprovalDecisionConflict, match="ownership does not match"):
        repository.create(
            approval=approval,
            audit_event=audit_event,
            notification=notification,
        )

    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM approval_requests WHERE approval_id=%s", (approval.id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE event_id=%s", (audit_event.id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM notifications WHERE notification_id=%s", (notification.id,)
        ).fetchone()[0] == 0


def test_postgres_agent_task_approval_rejects_missing_work_order_atomically(
    postgres_repository: tuple[
        PostgresApprovalDecisionRepository, Callable[[], Connection]
    ],
) -> None:
    repository, connect = postgres_repository
    workspace, project, existing, _ = _seed(connect)
    task = AgentTask(
        id=f"task_missing_work_order_{uuid.uuid4().hex}",
        project_id=project.id,
        workspace_id=workspace.id,
        title="Task needing approval",
        goal="Wait for review",
    )
    approval = _new_approval_from(existing, "task_missing_work_order").model_copy(
        update={
            "work_order_id": "work_order_missing_for_task",
            "status": ApprovalStatus.REQUESTED,
        }
    )
    linked_task = task.model_copy(
        update={
            "approval_request_id": approval.id,
            "status": AgentTaskStatus.WAITING_FOR_APPROVAL,
        }
    )
    approval_audit = AuditEvent(
        event_type="approval.created",
        entity_id=approval.id,
        workspace_id=workspace.id,
        details={"project_id": project.id},
    )
    task_event = AgentTaskEvent(
        task_id=task.id,
        event_type=AgentTaskEventType.APPROVAL_CREATED,
        status=AgentTaskStatus.WAITING_FOR_APPROVAL,
        actor="reviewer@example.test",
    )
    task_audit = AuditEvent(
        event_type="agent_task.approval_created",
        entity_id=task.id,
        workspace_id=workspace.id,
    )
    notification = Notification(
        title="Agent task approval needed",
        body="Review the approval request.",
        entity_type="agent_task",
        entity_id=task.id,
        workspace_id=workspace.id,
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO agent_tasks "
            "(task_id,project_id,workspace_id,approval_request_id,status,updated_at,payload) "
            "VALUES(%s,%s,%s,NULL,%s,%s,%s)",
            (
                task.id,
                project.id,
                workspace.id,
                task.status.value,
                task.updated_at,
                Jsonb(task.model_dump(mode="json")),
            ),
        )

    with pytest.raises(ApprovalDecisionConflict, match="work order is missing"):
        repository.create_for_agent_task(
            approval=approval,
            audit_event=approval_audit,
            task=linked_task,
            task_event=task_event,
            task_audit_event=task_audit,
            notification=notification,
        )

    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM approval_requests WHERE approval_id=%s", (approval.id,)
        ).fetchone()[0] == 0
        task_row = conn.execute(
            "SELECT approval_request_id FROM agent_tasks WHERE task_id=%s", (task.id,)
        ).fetchone()
        assert task_row[0] is None
        assert conn.execute(
            "SELECT count(*) FROM agent_task_events WHERE event_id=%s", (task_event.id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE event_id IN (%s,%s)",
            (approval_audit.id, task_audit.id),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM notifications WHERE notification_id=%s", (notification.id,)
        ).fetchone()[0] == 0


def test_postgres_list_for_workspace_queries_durable_tenant_scoped_rows(
    postgres_repository: tuple[
        PostgresApprovalDecisionRepository, Callable[[], Connection]
    ],
) -> None:
    repository, connect = postgres_repository
    workspace_a, _, approval_a, _ = _seed(connect)
    workspace_b = Workspace(
        id=f"workspace_list_{uuid.uuid4().hex}",
        name="Other workspace",
        owner_user_id=f"owner_{uuid.uuid4().hex}",
    )
    project_b = Project(
        id=f"project_list_{uuid.uuid4().hex}",
        name="Other project",
        workspace_id=workspace_b.id,
    )
    approval_b = approval_a.model_copy(
        update={
            "id": f"approval_list_{uuid.uuid4().hex}",
            "project_id": project_b.id,
            "workspace_id": workspace_b.id,
        }
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
            (workspace_b.id, Jsonb(workspace_b.model_dump(mode="json"))),
        )
        conn.execute(
            "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
            (project_b.id, workspace_b.id, Jsonb(project_b.model_dump(mode="json"))),
        )
        conn.execute(
            "INSERT INTO approval_requests "
            "(approval_id,project_id,workspace_id,work_order_id,source_agent_id,status,"
            "expires_at,created_at,updated_at,version,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s)",
            (
                approval_b.id, project_b.id, workspace_b.id, approval_b.work_order_id,
                approval_b.source_agent_id, approval_b.status.value, approval_b.expires_at,
                approval_b.created_at, approval_b.updated_at,
                Jsonb(approval_b.model_dump(mode="json")),
            ),
        )

    assert [item.id for item in repository.list_for_workspace(workspace_a.id)] == [approval_a.id]
    assert {item.id for item in repository.list_for_workspace()} == {approval_a.id, approval_b.id}


def test_postgres_agent_task_approval_creation_commits_task_timeline_and_audit_atomically(
    postgres_repository: tuple[
        PostgresApprovalDecisionRepository, Callable[[], Connection]
    ],
) -> None:
    repository, connect = postgres_repository
    workspace, project, existing, _ = _seed(connect)
    task = AgentTask(
        id=f"task_create_{uuid.uuid4().hex}",
        project_id=project.id,
        workspace_id=workspace.id,
        title="Task needing approval",
        goal="Wait for review",
    )
    approval = existing.model_copy(update={"id": f"approval_task_{uuid.uuid4().hex}"})
    linked_task = task.model_copy(
        update={
            "approval_request_id": approval.id,
            "status": AgentTaskStatus.WAITING_FOR_APPROVAL,
        }
    )
    approval_audit = AuditEvent(
        event_type="approval.created",
        entity_id=approval.id,
        workspace_id=workspace.id,
        details={"project_id": project.id},
    )
    task_event = AgentTaskEvent(
        task_id=task.id,
        event_type=AgentTaskEventType.APPROVAL_CREATED,
        status=AgentTaskStatus.WAITING_FOR_APPROVAL,
        actor="reviewer@example.test",
    )
    task_audit = AuditEvent(
        event_type="agent_task.approval_created",
        entity_id=task.id,
        workspace_id=workspace.id,
    )
    notification = Notification(
        title="Agent task approval needed",
        body="Review the approval request.",
        entity_type="agent_task",
        entity_id=task.id,
        workspace_id=workspace.id,
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO agent_tasks "
            "(task_id,project_id,workspace_id,approval_request_id,status,updated_at,payload) "
            "VALUES(%s,%s,%s,NULL,%s,%s,%s)",
            (
                task.id, project.id, workspace.id, task.status.value, task.updated_at,
                Jsonb(task.model_dump(mode="json")),
            ),
        )

    repository.create_for_agent_task(
        approval=approval,
        audit_event=approval_audit,
        task=linked_task,
        task_event=task_event,
        task_audit_event=task_audit,
        notification=notification,
    )

    with connect() as conn:
        task_row = conn.execute(
            "SELECT approval_request_id,status,payload FROM agent_tasks WHERE task_id=%s",
            (task.id,),
        ).fetchone()
        event_count = conn.execute(
            "SELECT count(*) FROM agent_task_events WHERE task_id=%s AND event_id=%s",
            (task.id, task_event.id),
        ).fetchone()[0]
        notification_row = conn.execute(
            "SELECT task_id,approval_id,entity_type,payload FROM notifications WHERE notification_id=%s",
            (notification.id,),
        ).fetchone()
        audit_count = conn.execute(
            "SELECT count(*) FROM audit_events WHERE event_id IN (%s,%s)",
            (approval_audit.id, task_audit.id),
        ).fetchone()[0]
    assert task_row[0] == approval.id
    assert task_row[1] == AgentTaskStatus.WAITING_FOR_APPROVAL.value
    assert AgentTask.model_validate(task_row[2]).approval_request_id == approval.id
    assert event_count == 1
    assert notification_row[0] == task.id
    assert notification_row[1] is None
    assert notification_row[2] == "agent_task"
    assert Notification.model_validate(notification_row[3]).entity_id == task.id
    assert audit_count == 2


def test_postgres_agent_task_approval_creation_rolls_back_on_timeline_failure(
    postgres_repository: tuple[
        PostgresApprovalDecisionRepository, Callable[[], Connection]
    ],
) -> None:
    repository, connect = postgres_repository
    workspace, project, existing, _ = _seed(connect)
    task = AgentTask(
        id=f"task_rollback_{uuid.uuid4().hex}",
        project_id=project.id,
        workspace_id=workspace.id,
        title="Task needing approval",
        goal="Remain unlinked if creation fails",
    )
    approval = existing.model_copy(update={"id": f"approval_rollback_{uuid.uuid4().hex}"})
    linked_task = task.model_copy(
        update={
            "approval_request_id": approval.id,
            "status": AgentTaskStatus.WAITING_FOR_APPROVAL,
        }
    )
    approval_audit = AuditEvent(
        event_type="approval.created", entity_id=approval.id, workspace_id=workspace.id
    )
    task_event = AgentTaskEvent(
        task_id=task.id,
        event_type=AgentTaskEventType.APPROVAL_CREATED,
        status=AgentTaskStatus.WAITING_FOR_APPROVAL,
    )
    task_audit = AuditEvent(
        event_type="agent_task.approval_created",
        entity_id=task.id,
        workspace_id=workspace.id,
    )
    notification = Notification(
        title="Agent task approval needed",
        body="Review the approval request.",
        entity_type="agent_task",
        entity_id=task.id,
        workspace_id=workspace.id,
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO agent_tasks "
            "(task_id,project_id,workspace_id,approval_request_id,status,updated_at,payload) "
            "VALUES(%s,%s,%s,NULL,%s,%s,%s)",
            (
                task.id, project.id, workspace.id, task.status.value, task.updated_at,
                Jsonb(task.model_dump(mode="json")),
            ),
        )
        conn.execute(
            "CREATE FUNCTION reject_task_timeline_insert() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'timeline unavailable'; END; $$ LANGUAGE plpgsql"
        )
        conn.execute(
            "CREATE TRIGGER reject_task_timeline BEFORE INSERT ON agent_task_events "
            "FOR EACH ROW EXECUTE FUNCTION reject_task_timeline_insert()"
        )

    with pytest.raises(psycopg.errors.RaiseException, match="timeline unavailable"):
        repository.create_for_agent_task(
            approval=approval,
            audit_event=approval_audit,
            task=linked_task,
            task_event=task_event,
            task_audit_event=task_audit,
            notification=notification,
        )

    with connect() as conn:
        task_row = conn.execute(
            "SELECT approval_request_id FROM agent_tasks WHERE task_id=%s", (task.id,)
        ).fetchone()
        assert task_row[0] is None
        assert conn.execute(
            "SELECT count(*) FROM approval_requests WHERE approval_id=%s", (approval.id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE event_id IN (%s,%s)",
            (approval_audit.id, task_audit.id),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM notifications WHERE notification_id=%s", (notification.id,)
        ).fetchone()[0] == 0


def test_postgres_create_rejects_duplicate_approval_without_duplicate_outbox(
    postgres_repository: tuple[
        PostgresApprovalDecisionRepository, Callable[[], Connection]
    ],
) -> None:
    repository, connect = postgres_repository
    _, _, existing, _ = _seed(connect)
    approval = _new_approval_from(existing, "duplicate")
    audit_event, notification = _creation_records(approval, "duplicate")
    repository.create(approval=approval, audit_event=audit_event, notification=notification)

    with pytest.raises(ApprovalDecisionConflict, match="identifiers already exist"):
        repository.create(approval=approval, audit_event=audit_event, notification=notification)

    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE approval_id=%s", (approval.id,)
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM notifications WHERE approval_id=%s", (approval.id,)
        ).fetchone()[0] == 1


def test_postgres_create_rolls_back_approval_and_audit_when_outbox_fails(
    postgres_repository: tuple[
        PostgresApprovalDecisionRepository, Callable[[], Connection]
    ],
) -> None:
    repository, connect = postgres_repository
    _, _, existing, _ = _seed(connect)
    approval = _new_approval_from(existing, "rollback")
    audit_event, notification = _creation_records(approval, "rollback")
    with connect() as conn:
        conn.execute(
            "CREATE FUNCTION fail_test_notification_insert() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'notification outbox unavailable'; END; $$ LANGUAGE plpgsql"
        )
        conn.execute(
            "CREATE TRIGGER fail_test_notification BEFORE INSERT ON notifications "
            "FOR EACH ROW EXECUTE FUNCTION fail_test_notification_insert()"
        )

    with pytest.raises(psycopg.errors.RaiseException, match="notification outbox unavailable"):
        repository.create(approval=approval, audit_event=audit_event, notification=notification)

    assert repository.get(approval.id) is None
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE approval_id=%s", (approval.id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM notifications WHERE approval_id=%s", (approval.id,)
        ).fetchone()[0] == 0


def test_postgres_commit_updates_approval_task_audit_and_outbox_atomically(
    postgres_repository: tuple[
        PostgresApprovalDecisionRepository, Callable[[], Connection]
    ],
) -> None:
    repository, connect = postgres_repository
    _, _, approval, task = _seed(connect, with_task=True)
    assert task is not None
    updated = _updated_approval(approval, ApprovalStatus.APPROVED)

    decision_audit = _audit(approval, updated, "atomic")
    result = repository.commit_decision(
        approval_id=approval.id,
        expected_status=ApprovalStatus.REQUESTED,
        expected_approval=approval,
        updated_approval=updated,
        audit_event=decision_audit,
        decision_notification=_notification(approval, "atomic"),
        run_repository=PostgresAgentRunRepository(connect),
    )

    assert result.linked_task is not None
    assert result.linked_task.status == AgentTaskStatus.APPROVED
    assert result.linked_task_event is not None
    assert result.linked_task_event.event_type == AgentTaskEventType.APPROVED
    assert result.linked_task_audit_event is not None
    assert result.linked_task_notification is not None
    assert result.linked_run is not None
    assert result.linked_run.task_id == task.id
    assert result.linked_run.approval_request_id == approval.id
    assert repository.get(approval.id) == updated
    assert repository.latest_decision_audit(
        approval_id=approval.id,
        workspace_id=approval.workspace_id,
        new_status=ApprovalStatus.APPROVED,
    ) == decision_audit
    assert repository.latest_decision_audit(
        approval_id=approval.id,
        workspace_id="another_workspace",
        new_status=ApprovalStatus.APPROVED,
    ) is None
    with connect() as conn:
        persisted_task = conn.execute(
            "SELECT payload FROM agent_tasks WHERE task_id = %s", (task.id,)
        ).fetchone()
        audit_count = conn.execute(
            "SELECT count(*) FROM audit_events WHERE approval_id = %s", (approval.id,)
        ).fetchone()[0]
        notification = conn.execute(
            "SELECT status FROM notifications WHERE approval_id = %s", (approval.id,)
        ).fetchone()
        task_event_count = conn.execute(
            "SELECT count(*) FROM agent_task_events WHERE task_id=%s AND event_type='APPROVED'",
            (task.id,),
        ).fetchone()[0]
        task_audit_count = conn.execute(
            "SELECT count(*) FROM audit_events WHERE entity_type='agent_task' AND entity_id=%s "
            "AND event_type='agent_task.approval_decision_propagated'",
            (task.id,),
        ).fetchone()[0]
        task_notification = conn.execute(
            "SELECT task_id,approval_id,entity_type FROM notifications WHERE task_id=%s",
            (task.id,),
        ).fetchone()
    assert AgentTask.model_validate(persisted_task[0]).status == AgentTaskStatus.APPROVED
    assert audit_count == 1
    assert notification[0] == "PENDING"
    assert task_event_count == 1
    assert task_audit_count == 1
    assert task_notification == (task.id, None, "agent_task")
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        with connect() as conn:
            conn.execute(
                "UPDATE audit_events SET actor = 'changed' WHERE approval_id = %s",
                (approval.id,),
            )


def test_postgres_approval_rolls_back_when_linked_run_creation_fails(
    postgres_repository: tuple[
        PostgresApprovalDecisionRepository, Callable[[], Connection]
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, connect = postgres_repository
    _, _, approval, task = _seed(connect, with_task=True)
    assert task is not None
    updated = _updated_approval(approval, ApprovalStatus.APPROVED)
    run_repository = PostgresAgentRunRepository(connect)

    def fail_run_creation(*_args, **_kwargs) -> None:
        raise AgentRunRepositoryConflict("simulated run persistence failure")

    monkeypatch.setattr(run_repository, "create_in_transaction", fail_run_creation)
    with pytest.raises(ApprovalDecisionConflict, match="could not be scheduled"):
        repository.commit_decision(
            approval_id=approval.id,
            expected_status=ApprovalStatus.REQUESTED,
            expected_approval=approval,
            updated_approval=updated,
            audit_event=_audit(approval, updated, "run_rollback"),
            decision_notification=_notification(approval, "run_rollback"),
            run_repository=run_repository,
        )

    assert repository.get(approval.id) == approval
    with connect() as conn:
        task_status = conn.execute(
            "SELECT status FROM agent_tasks WHERE task_id=%s", (task.id,)
        ).fetchone()
        assert task_status == (AgentTaskStatus.WAITING_FOR_APPROVAL.value,)
        assert conn.execute(
            "SELECT count(*) FROM agent_task_events WHERE task_id=%s AND event_type='APPROVED'",
            (task.id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE approval_id=%s", (approval.id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM notifications WHERE approval_id=%s OR task_id=%s",
            (approval.id, task.id),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM agent_runs WHERE task_id=%s", (task.id,)
        ).fetchone()[0] == 0


def test_postgres_decision_refuses_to_overwrite_linked_task_outside_approval_wait(
    postgres_repository: tuple[
        PostgresApprovalDecisionRepository, Callable[[], Connection]
    ],
) -> None:
    repository, connect = postgres_repository
    _, _, approval, task = _seed(connect, with_task=True)
    assert task is not None
    cancelled_task = task.model_copy(update={"status": AgentTaskStatus.CANCELLED})
    with connect() as conn:
        conn.execute(
            "UPDATE agent_tasks SET status=%s,payload=%s WHERE task_id=%s",
            (
                cancelled_task.status.value,
                Jsonb(cancelled_task.model_dump(mode="json")),
                task.id,
            ),
        )

    updated = _updated_approval(approval, ApprovalStatus.APPROVED)
    with pytest.raises(ApprovalDecisionConflict, match="no longer waiting"):
        repository.commit_decision(
            approval_id=approval.id,
            expected_status=ApprovalStatus.REQUESTED,
            expected_approval=approval,
            updated_approval=updated,
            audit_event=_audit(approval, updated, "task-already-cancelled"),
            decision_notification=_notification(approval, "task-already-cancelled"),
        )

    assert repository.get(approval.id) == approval
    with connect() as conn:
        persisted_task = conn.execute(
            "SELECT status,payload FROM agent_tasks WHERE task_id=%s", (task.id,)
        ).fetchone()
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE workspace_id=%s", (approval.workspace_id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM notifications WHERE workspace_id=%s", (approval.workspace_id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM agent_task_events WHERE task_id=%s", (task.id,)
        ).fetchone()[0] == 0
    assert persisted_task[0] == AgentTaskStatus.CANCELLED.value
    assert AgentTask.model_validate(persisted_task[1]) == cancelled_task


def test_postgres_audit_failure_rolls_back_the_decision(
    postgres_repository: tuple[
        PostgresApprovalDecisionRepository, Callable[[], Connection]
    ],
) -> None:
    repository, connect = postgres_repository
    _, _, approval, _ = _seed(connect)
    updated = _updated_approval(approval, ApprovalStatus.APPROVED)
    duplicate_audit = AuditEvent(
        id="audit_duplicate",
        event_type="approval.expired",
        entity_id=approval.id,
        workspace_id=approval.workspace_id,
        details={"previous_status": ApprovalStatus.REQUESTED, "new_status": ApprovalStatus.EXPIRED},
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO audit_events "
            "(event_id, workspace_id, approval_id, entity_type, entity_id, event_type, "
            "actor, details, created_at, payload) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                duplicate_audit.id,
                duplicate_audit.workspace_id,
                approval.id,
                "approval_request",
                duplicate_audit.entity_id,
                duplicate_audit.event_type,
                duplicate_audit.actor,
                Jsonb(duplicate_audit.details),
                duplicate_audit.created_at,
                Jsonb(duplicate_audit.model_dump(mode="json")),
            ),
        )

    with pytest.raises(psycopg.errors.UniqueViolation):
        repository.commit_decision(
            approval_id=approval.id,
            expected_status=ApprovalStatus.REQUESTED,
            expected_approval=approval,
            updated_approval=updated,
            audit_event=_audit(approval, updated, "duplicate"),
            decision_notification=_notification(approval, "rollback"),
        )

    assert repository.get(approval.id) == approval
    with connect() as conn:
        notification_count = conn.execute(
            "SELECT count(*) FROM notifications WHERE approval_id = %s", (approval.id,)
        ).fetchone()[0]
        audit_count = conn.execute(
            "SELECT count(*) FROM audit_events WHERE approval_id = %s", (approval.id,)
        ).fetchone()[0]
    assert notification_count == 0
    assert audit_count == 1


@pytest.mark.parametrize(
    ("entity_type", "event_type"),
    [
        ("project", "project.created"),
        ("external_agent", "agent.registered"),
        ("external_agent", "agent.token_revoked"),
    ],
)
def test_postgres_audit_table_stores_scoped_nonapproval_events(
    postgres_repository: tuple[
        PostgresApprovalDecisionRepository, Callable[[], Connection]
    ],
    entity_type: str,
    event_type: str,
) -> None:
    _, connect = postgres_repository
    workspace, project, _, _ = _seed(connect)
    event = AuditEvent(
        id=f"audit_{uuid.uuid4().hex}",
        event_type=event_type,
        entity_id=(project.id if entity_type == "project" else f"agent_{uuid.uuid4().hex}"),
        workspace_id=workspace.id,
        actor="owner@example.test",
        details={"source": "legacy_migration"},
    )

    with connect() as conn:
        conn.execute(
            "INSERT INTO audit_events "
            "(event_id, workspace_id, approval_id, entity_type, entity_id, event_type, "
            "actor, details, created_at, payload) "
            "VALUES (%s, %s, NULL, %s, %s, %s, %s, %s, %s, %s)",
            (
                event.id,
                event.workspace_id,
                entity_type,
                event.entity_id,
                event.event_type,
                event.actor,
                Jsonb(event.details),
                event.created_at,
                Jsonb(event.model_dump(mode="json")),
            ),
        )
        row = conn.execute(
            "SELECT entity_type, entity_id, event_type, payload "
            "FROM audit_events WHERE event_id = %s",
            (event.id,),
        ).fetchone()

    assert row[:3] == (entity_type, event.entity_id, event_type)
    assert AuditEvent.model_validate(row[3]) == event


def test_postgres_notification_failure_rolls_back_approval_task_and_audit(
    postgres_repository: tuple[
        PostgresApprovalDecisionRepository, Callable[[], Connection]
    ],
) -> None:
    repository, connect = postgres_repository
    _, _, approval, task = _seed(connect, with_task=True)
    assert task is not None
    updated = _updated_approval(approval, ApprovalStatus.APPROVED)
    with connect() as conn:
        conn.execute(
            """
            CREATE FUNCTION fail_test_notification_insert() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'notification outbox unavailable';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        conn.execute(
            """
            CREATE TRIGGER fail_test_notification_insert
            BEFORE INSERT ON notifications
            FOR EACH ROW EXECUTE FUNCTION fail_test_notification_insert()
            """
        )

    with pytest.raises(psycopg.errors.RaiseException):
        repository.commit_decision(
            approval_id=approval.id,
            expected_status=ApprovalStatus.REQUESTED,
            expected_approval=approval,
            updated_approval=updated,
            audit_event=_audit(approval, updated, "notification-failure"),
            decision_notification=_notification(approval, "notification-failure"),
        )

    assert repository.get(approval.id) == approval
    with connect() as conn:
        persisted_task = conn.execute(
            "SELECT payload FROM agent_tasks WHERE task_id = %s", (task.id,)
        ).fetchone()
        audit_count = conn.execute(
            "SELECT count(*) FROM audit_events WHERE approval_id = %s", (approval.id,)
        ).fetchone()[0]
        notification_count = conn.execute(
            "SELECT count(*) FROM notifications WHERE approval_id = %s", (approval.id,)
        ).fetchone()[0]
    assert AgentTask.model_validate(persisted_task[0]).status == AgentTaskStatus.WAITING_FOR_APPROVAL
    assert audit_count == 0
    assert notification_count == 0


def test_postgres_competing_decisions_have_one_winner(
    postgres_repository: tuple[
        PostgresApprovalDecisionRepository, Callable[[], Connection]
    ],
) -> None:
    repository, connect = postgres_repository
    _, _, approval, _ = _seed(connect)
    barrier = Barrier(2)

    def decide(status: ApprovalStatus, name: str) -> str:
        updated = _updated_approval(approval, status)
        barrier.wait(timeout=5)
        try:
            repository.commit_decision(
                approval_id=approval.id,
                expected_status=ApprovalStatus.REQUESTED,
                expected_approval=approval,
                updated_approval=updated,
                audit_event=_audit(approval, updated, name),
                decision_notification=_notification(approval, name),
            )
            return "committed"
        except ApprovalDecisionConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [
            executor.submit(decide, ApprovalStatus.APPROVED, "race-approve"),
            executor.submit(decide, ApprovalStatus.DENIED, "race-deny"),
        ]
        outcomes = [future.result(timeout=10) for future in results]

    assert sorted(outcomes) == ["committed", "conflict"]
    final_approval = repository.get(approval.id)
    assert final_approval is not None
    assert final_approval.status in {ApprovalStatus.APPROVED, ApprovalStatus.DENIED}
    with connect() as conn:
        audit_count = conn.execute(
            "SELECT count(*) FROM audit_events WHERE approval_id = %s", (approval.id,)
        ).fetchone()[0]
        notification_count = conn.execute(
            "SELECT count(*) FROM notifications WHERE approval_id = %s", (approval.id,)
        ).fetchone()[0]
    assert audit_count == 1
    assert notification_count == 1


def test_postgres_competing_decisions_have_one_winner_across_processes(
    postgres_repository: tuple[
        PostgresApprovalDecisionRepository, Callable[[], Connection]
    ],
) -> None:
    repository, connect = postgres_repository
    _workspace, _project, approval, _task = _seed(connect)
    with connect() as conn:
        schema = conn.execute("SELECT current_schema()").fetchone()[0]

    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN")
    assert dsn, "fixture requires AIXION_TEST_POSTGRES_DSN"
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    with ProcessPoolExecutor(
        max_workers=2,
        mp_context=context,
        initializer=_initialize_postgres_decision_race,
        initargs=(barrier,),
    ) as executor:
        futures = [
            executor.submit(
                _commit_postgres_decision_in_process,
                dsn,
                schema,
                approval.model_dump(mode="json"),
                target_status,
                name,
            )
            for target_status, name in (
                (ApprovalStatus.APPROVED.value, "process-race-approve"),
                (ApprovalStatus.DENIED.value, "process-race-deny"),
            )
        ]
        outcomes = [future.result(timeout=20) for future in futures]

    assert sorted(outcomes) == ["committed", "conflict"]
    final_approval = repository.get(approval.id)
    assert final_approval is not None
    assert final_approval.status in {ApprovalStatus.APPROVED, ApprovalStatus.DENIED}
    with connect() as conn:
        audit_count = conn.execute(
            "SELECT count(*) FROM audit_events WHERE approval_id = %s",
            (approval.id,),
        ).fetchone()[0]
        notification_count = conn.execute(
            "SELECT count(*) FROM notifications WHERE approval_id = %s",
            (approval.id,),
        ).fetchone()[0]
    assert audit_count == 1
    assert notification_count == 1


def test_postgres_expiration_is_atomic_and_rejects_a_late_decision(
    postgres_repository: tuple[
        PostgresApprovalDecisionRepository, Callable[[], Connection]
    ],
) -> None:
    repository, connect = postgres_repository
    _, _, approval, _ = _seed(connect)
    pending = _seed_mcp_pending(connect, approval)
    expired = approval.model_copy(
        update={"status": ApprovalStatus.EXPIRED, "updated_at": now_utc()}
    )
    repository.expire_pending(
        approval_id=approval.id,
        expected_approval=approval,
        expired_approval=expired,
        audit_event=_audit(approval, expired, "expired", event_type="approval.expired"),
    )

    assert repository.get(approval.id) == expired
    with pytest.raises(ApprovalDecisionConflict):
        repository.commit_decision(
            approval_id=approval.id,
            expected_status=ApprovalStatus.REQUESTED,
            expected_approval=approval,
            updated_approval=_updated_approval(approval, ApprovalStatus.APPROVED),
            audit_event=_audit(
                approval,
                _updated_approval(approval, ApprovalStatus.APPROVED),
                "late",
            ),
            decision_notification=_notification(approval, "late"),
        )
    with connect() as conn:
        audit_count = conn.execute(
            "SELECT count(*) FROM audit_events WHERE approval_id = %s", (approval.id,)
        ).fetchone()[0]
        pending_row = conn.execute(
            "SELECT status,payload FROM mcp_pending_requests WHERE pending_id=%s",
            (pending.id,),
        ).fetchone()
    assert audit_count == 1
    assert pending_row[0] == MCPPendingStatus.BLOCKED_BY_DECISION.value
    assert MCPPendingRequest.model_validate(pending_row[1]).status == MCPPendingStatus.BLOCKED_BY_DECISION


def test_postgres_linked_task_expiration_commits_task_effects_atomically(
    postgres_repository: tuple[
        PostgresApprovalDecisionRepository, Callable[[], Connection]
    ],
) -> None:
    repository, connect = postgres_repository
    _workspace, _project, approval, task = _seed(connect, with_task=True)
    assert task is not None
    expired = approval.model_copy(
        update={"status": ApprovalStatus.EXPIRED, "updated_at": now_utc()}
    )
    expiry_audit = AuditEvent(
        id=f"audit_expire_{uuid.uuid4().hex}",
        event_type="approval.expired",
        actor="expiry-test",
        entity_id=approval.id,
        workspace_id=approval.workspace_id,
        details={
            "previous_status": approval.status,
            "new_status": ApprovalStatus.EXPIRED,
            "expires_at": expired.expires_at,
        },
        created_at=expired.updated_at,
    )

    commit = repository.expire_pending(
        approval_id=approval.id,
        expected_approval=approval,
        expired_approval=expired,
        audit_event=expiry_audit,
    )

    assert commit.linked_task is not None
    assert commit.linked_task.status == AgentTaskStatus.WAITING_FOR_APPROVAL
    assert commit.linked_task_event is not None
    assert commit.linked_task_audit_event is not None
    assert commit.linked_task_notification is not None
    with connect() as conn:
        stored_task = conn.execute(
            "SELECT status,payload FROM agent_tasks WHERE task_id=%s", (task.id,)
        ).fetchone()
        timeline_count = conn.execute(
            "SELECT count(*) FROM agent_task_events WHERE task_id=%s", (task.id,)
        ).fetchone()[0]
        task_audit_count = conn.execute(
            "SELECT count(*) FROM audit_events WHERE entity_type='agent_task' AND entity_id=%s",
            (task.id,),
        ).fetchone()[0]
        task_notification_count = conn.execute(
            "SELECT count(*) FROM notifications WHERE task_id=%s", (task.id,)
        ).fetchone()[0]
    assert stored_task[0] == AgentTaskStatus.WAITING_FOR_APPROVAL.value
    assert AgentTask.model_validate(stored_task[1]).status == AgentTaskStatus.WAITING_FOR_APPROVAL
    assert timeline_count == 1
    assert task_audit_count == 1
    assert task_notification_count == 1


def test_postgres_linked_task_expiration_rolls_back_on_outbox_failure(
    postgres_repository: tuple[
        PostgresApprovalDecisionRepository, Callable[[], Connection]
    ],
) -> None:
    repository, connect = postgres_repository
    _workspace, _project, approval, task = _seed(connect, with_task=True)
    assert task is not None
    expired = approval.model_copy(
        update={"status": ApprovalStatus.EXPIRED, "updated_at": now_utc()}
    )
    expiry_audit = AuditEvent(
        id=f"audit_expire_rollback_{uuid.uuid4().hex}",
        event_type="approval.expired",
        actor="expiry-test",
        entity_id=approval.id,
        workspace_id=approval.workspace_id,
        details={
            "previous_status": approval.status,
            "new_status": ApprovalStatus.EXPIRED,
            "expires_at": expired.expires_at,
        },
        created_at=expired.updated_at,
    )
    with connect() as conn:
        conn.execute(
            "CREATE FUNCTION reject_task_expiry_notification() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN "
            "IF NEW.task_id IS NOT NULL THEN RAISE EXCEPTION 'expiry outbox unavailable'; END IF; "
            "RETURN NEW; END $$"
        )
        conn.execute(
            "CREATE TRIGGER reject_task_expiry_notification BEFORE INSERT ON notifications "
            "FOR EACH ROW EXECUTE FUNCTION reject_task_expiry_notification()"
        )

    with pytest.raises(psycopg.Error, match="expiry outbox unavailable"):
        repository.expire_pending(
            approval_id=approval.id,
            expected_approval=approval,
            expired_approval=expired,
            audit_event=expiry_audit,
        )

    assert repository.get(approval.id) == approval
    with connect() as conn:
        task_row = conn.execute(
            "SELECT status,payload FROM agent_tasks WHERE task_id=%s", (task.id,)
        ).fetchone()
        assert task_row[0] == AgentTaskStatus.WAITING_FOR_APPROVAL.value
        assert AgentTask.model_validate(task_row[1]).updated_at == task.updated_at
        assert conn.execute(
            "SELECT count(*) FROM agent_task_events WHERE task_id=%s", (task.id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE entity_id=%s", (approval.id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM notifications WHERE task_id=%s", (task.id,)
        ).fetchone()[0] == 0


def test_postgres_expiry_service_emits_workspace_scoped_audit(
    postgres_repository: tuple[
        PostgresApprovalDecisionRepository, Callable[[], Connection]
    ],
) -> None:
    repository, connect = postgres_repository
    workspace, _, approval, _ = _seed(connect)

    assert approval_expiry.expire_approval(
        approval,
        actor="expiry-service-test",
        reason="test_deadline",
        now=now_utc(),
        force=True,
        repository=repository,
    )

    with connect() as conn:
        audit = conn.execute(
            "SELECT workspace_id,event_type FROM audit_events "
            "WHERE entity_type='approval_request' AND entity_id=%s",
            (approval.id,),
        ).fetchone()
    assert audit == (workspace.id, "approval.expired")


@pytest.mark.parametrize("tamper", ["payload", "audit_workspace", "audit_deadline"])
def test_postgres_expiration_rejects_payload_or_audit_scope_tampering(
    postgres_repository: tuple[
        PostgresApprovalDecisionRepository, Callable[[], Connection]
    ],
    tamper: str,
) -> None:
    repository, connect = postgres_repository
    _, _, approval, _ = _seed(connect)
    expired = approval.model_copy(
        update={"status": ApprovalStatus.EXPIRED, "updated_at": now_utc()}
    )
    audit_event = _audit(
        approval, expired, f"expiration_{tamper}", event_type="approval.expired"
    )
    expected_message = "preserve the approval request payload"
    if tamper == "payload":
        expired.summary = "tampered during expiration"
    elif tamper == "audit_workspace":
        audit_event.workspace_id = "another-workspace"
        expected_message = "audit workspace"
    else:
        audit_event.details["expires_at"] = "incorrect-deadline"
        expected_message = "audit deadline"

    with pytest.raises(ValueError, match=expected_message):
        repository.expire_pending(
            approval_id=approval.id,
            expected_approval=approval,
            expired_approval=expired,
            audit_event=audit_event,
        )

    assert repository.get(approval.id) == approval
    with connect() as conn:
        audit_count = conn.execute(
            "SELECT count(*) FROM audit_events WHERE approval_id = %s",
            (approval.id,),
        ).fetchone()[0]
    assert audit_count == 0
