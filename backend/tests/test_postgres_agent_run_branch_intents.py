from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import psycopg
import pytest
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app.agent_run_models import (
    AgentRun, AgentRunEvent, AgentRunEventType, AgentRunStatus, AgentRunStep,
    AgentRunStepStatus, AgentRunStepType,
)
from app.agent_task_models import AgentTask, AgentTaskEvent, AgentTaskEventType, AgentTaskStatus
from app.approval_integrity import compute_approval_payload_hash
from app.models import AgentProvider, ApprovalRequest, ApprovalStatus, AuditEvent, RiskLevel, User, UserRole, now_utc
from app.agent_run_repository import AgentRunRepositoryConflict, PostgresAgentRunRepository
from app.postgres_agent_run_branch import (
    AgentRunBranchIntent,
    BranchIntentStatus,
    PostgresAgentRunBranchIntentRepository,
    _payload,
)
from app.postgres_database import run_postgres_migrations
from app.postgres_trust_repository import (
    PostgresTrustRepository,
    PostgresTrustRepositoryConflict,
    PostgresTrustRepositoryError,
)
from app.trust_models import (
    CapabilityActionType,
    CapabilityLease,
    CapabilityLeaseMode,
    CapabilityLeaseStatus,
    CapabilityScope,
    PolicyDecision,
    PolicyDecisionType,
    ProposedAction,
)
from app.models import RiskAssessment
from app.trust_crypto import sha256_hex
from app.trust_crypto import sign_payload
from app.trust_policy import lease_receipt_payload


@pytest.fixture
def pg_branch_context() -> Iterator[dict]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL branch-intent tests")
    schema = f"ct_branch_intents_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        conn = psycopg.connect(dsn)
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return conn

    now = now_utc()
    workspace_id = f"workspace_{uuid.uuid4().hex}"
    project_id = f"project_{uuid.uuid4().hex}"
    owner_id = f"owner_{uuid.uuid4().hex}"
    task_id = f"task_{uuid.uuid4().hex}"
    approval_id = f"approval_{uuid.uuid4().hex}"
    actor = f"worker_{uuid.uuid4().hex}"
    repository_name = "owner/repo"
    source_branch = "main"
    target_ref = "feature/approved-branch"
    source_sha = "a" * 40
    lease_token = "lease-token-for-branch-test"
    token_hash = sha256_hex({"lease_token": lease_token})
    approval = ApprovalRequest(
        id=approval_id,
        project_id=project_id,
        workspace_id=workspace_id,
        title="Branch intent approval",
        summary="Approved test branch operation",
        target_branch=target_ref,
        status=ApprovalStatus.APPROVED,
        risk=RiskAssessment(level=RiskLevel.LOW),
        approved_by_user_id=owner_id,
        created_at=now,
        updated_at=now,
    )
    approval.approved_payload_hash = compute_approval_payload_hash(approval)
    task = AgentTask(
        id=task_id,
        workspace_id=workspace_id,
        project_id=project_id,
        title="Branch intent task",
        goal="Create an approved branch",
        provider=AgentProvider.ANTIGRAVITY,
        repository=repository_name,
        branch_preference=target_ref,
        approval_request_id=approval_id,
        status=AgentTaskStatus.APPROVED,
        created_at=now,
        updated_at=now,
    )
    lease = CapabilityLease(
        id=f"lease_{uuid.uuid4().hex}",
        approval_request_id=approval_id,
        workspace_id=workspace_id,
        approved_payload_hash=approval.approved_payload_hash,
        provider=AgentProvider.ANTIGRAVITY,
        agent_id=task.external_agent_id,
        mode=CapabilityLeaseMode.BOUNDED,
        scope=CapabilityScope(
            repository=repository_name,
            branch=target_ref,
            allowed_actions=[CapabilityActionType.CREATE_BRANCH],
            max_runtime_seconds=600,
            max_cost_usd=1.0,
        ),
        issued_by_user_id=owner_id,
        expires_at=now + timedelta(hours=1),
        receipt_nonce=f"nonce_{uuid.uuid4().hex}",
        receipt_signature="pending",
        created_at=now,
        updated_at=now,
    )
    lease.receipt_signature = sign_payload(lease_receipt_payload(lease))
    run_id = f"run_{uuid.uuid4().hex}"
    step_id = f"step_{uuid.uuid4().hex}"
    idempotency_key = f"idempotency_{uuid.uuid4().hex}"
    run = AgentRun(
        id=run_id,
        task_id=task_id,
        workspace_id=workspace_id,
        approval_request_id=approval_id,
        capability_lease_id=lease.id,
        project_id=project_id,
        repository=repository_name,
        status=AgentRunStatus.RUNNING,
        current_step_id=step_id,
        lease_owner=actor,
        lease_token=lease_token,
        lease_expires_at=now + timedelta(minutes=10),
        updated_at=now,
    )
    step = AgentRunStep(
        id=step_id,
        run_id=run_id,
        task_id=task_id,
        sequence=1,
        step_type=AgentRunStepType.CREATE_BRANCH,
        status=AgentRunStepStatus.RUNNING,
        idempotency_key=idempotency_key,
        lease_owner=actor,
        lease_token=lease_token,
        lease_expires_at=run.lease_expires_at,
        updated_at=now,
    )
    action_id = f"action_{uuid.uuid4().hex}"
    action = ProposedAction(
        id=action_id,
        lease_id=lease.id,
        provider=AgentProvider.ANTIGRAVITY,
        agent_id=task.external_agent_id,
        run_id=run_id,
        task_id=task_id,
        project_id=project_id,
        workspace_id=workspace_id,
        action_type=CapabilityActionType.CREATE_BRANCH,
        repository=repository_name,
        branch=target_ref,
        estimated_runtime_seconds=120,
        metadata={
            "step_id": step_id,
            "step_type": "CREATE_BRANCH",
            "task_id": task_id,
            "approval_id": approval_id,
            "lease_id": lease.id,
            "idempotency_key": idempotency_key,
            "repository": repository_name,
            "source_branch": source_branch,
            "source_sha": source_sha,
            "target_ref": target_ref,
        },
        created_at=now,
    )
    decision = PolicyDecision(
        proposed_action_id=action_id,
        lease_id=lease.id,
        decision=PolicyDecisionType.ALLOW,
        risk_level=RiskLevel.LOW,
        evaluated_at=now,
    )
    intent = AgentRunBranchIntent(
        id=f"intent_{uuid.uuid4().hex}",
        workspace_id=workspace_id,
        run_id=run_id,
        task_id=task_id,
        approval_id=approval_id,
        step_id=step_id,
        action_id=action_id,
        lease_id=lease.id,
        idempotency_key=idempotency_key,
        repository=repository_name,
        source_branch=source_branch,
        target_ref=target_ref,
        source_sha=source_sha,
        lease_token_hash=token_hash,
        initial_lease_token_hash=token_hash,
        run_revision=now,
        step_revision=now,
        status=BranchIntentStatus.INTENT,
        created_at=now,
        updated_at=now,
    )
    try:
        run_postgres_migrations(connect)
        with connect() as conn:
            conn.execute(
                "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
                (workspace_id, Jsonb({"id": workspace_id, "name": "test", "owner_user_id": "owner"})),
            )
            conn.execute(
                "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
                (project_id, workspace_id, Jsonb({"id": project_id, "name": "test", "workspace_id": workspace_id})),
            )
            owner = User(
                id=owner_id, email=f"{owner_id}@example.test", display_name="Test Owner",
                role=UserRole.OWNER, password_hash="test", password_salt="test",
                workspace_id=workspace_id, email_verified=True,
            )
            conn.execute(
                "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,email_verified,payload) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (owner_id, workspace_id, owner.email, owner.email.lower(), owner.role.value,
                 owner.disabled, owner.email_verified, Jsonb(owner.model_dump(mode="json"))),
            )
            conn.execute(
                "INSERT INTO approval_requests(approval_id,project_id,workspace_id,work_order_id,source_agent_id,status,expires_at,created_at,updated_at,version,payload) "
                "VALUES(%s,%s,%s,NULL,NULL,%s,NULL,%s,%s,0,%s)",
                (approval_id, project_id, workspace_id, approval.status.value, now, now, Jsonb(approval.model_dump(mode="json"))),
            )
            conn.execute(
                "INSERT INTO agent_tasks(task_id,project_id,workspace_id,external_agent_id,approval_request_id,status,updated_at,payload) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (task_id, project_id, workspace_id, task.external_agent_id, approval_id, task.status.value, task.updated_at, Jsonb(task.model_dump(mode="json"))),
            )
            trust = PostgresTrustRepository()
            trust.insert_capability_lease(conn, lease)
            conn.execute(
                "INSERT INTO agent_runs(run_id,task_id,project_id,workspace_id,approval_id,status,current_step_id,lease_owner,lease_token,lease_expires_at,updated_at,payload) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (run_id, task_id, project_id, workspace_id, approval_id, run.status.value, step_id, actor, lease_token, run.lease_expires_at, now, Jsonb(run.model_dump(mode="json"))),
            )
            conn.execute(
                "INSERT INTO agent_run_steps(step_id,run_id,task_id,workspace_id,sequence,status,updated_at,payload) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (step_id, run_id, task_id, workspace_id, step.sequence, step.status.value, now, Jsonb(step.model_dump(mode="json"))),
            )
            trust.insert_proposed_action(conn, action)
            trust.insert_policy_decision(conn, decision, workspace_id=workspace_id)
            columns = "intent_id,workspace_id,run_id,task_id,approval_id,step_id,action_id,lease_id,idempotency_key,repository,source_branch,target_ref,source_sha,lease_token_hash,initial_lease_token_hash,run_revision,step_revision,status,branch_url,reason,created_at,updated_at,payload"
            values = (intent.id, workspace_id, run_id, task_id, approval_id, step_id, action_id, lease.id, idempotency_key, repository_name, source_branch, target_ref, source_sha, token_hash, token_hash, now, now, intent.status.value, None, "", now, now, Jsonb(_payload(intent)))
            conn.execute(f"INSERT INTO agent_run_branch_intents({columns}) VALUES({','.join(['%s'] * len(values))})", values)
        yield {
            "connect": connect, "workspace_id": workspace_id, "project_id": project_id,
            "task": task, "approval": approval, "lease": lease, "run": run, "step": step,
            "action": action, "decision": decision, "intent": intent, "actor": actor,
            "token": lease_token, "repository": PostgresAgentRunBranchIntentRepository(connect),
        }
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_postgres_branch_intent_rejects_action_target_mismatch_before_consume(pg_branch_context) -> None:
    c = pg_branch_context
    with c["connect"]() as conn:
        conn.execute(
            "DELETE FROM agent_run_branch_intents WHERE intent_id=%s",
            (c["intent"].id,),
        )
    bad_intent = c["intent"].model_copy(update={"target_ref": "feature/unapproved"})
    with pytest.raises(PostgresTrustRepositoryConflict, match="differs from persisted approved action"):
        c["repository"].begin_intent(
            record=bad_intent,
            expected_run=c["run"],
            expected_step=c["step"],
            timeout_seconds=120,
            actor=c["actor"],
            now=now_utc(),
        )
    with c["connect"]() as conn:
        assert conn.execute("SELECT count(*) FROM action_consumptions").fetchone()[0] == 0


def test_postgres_branch_create_claim_has_one_cross_connection_winner(pg_branch_context) -> None:
    c = pg_branch_context
    barrier = Barrier(2)

    def claim() -> str:
        barrier.wait()
        try:
            c["repository"].claim_create_once(
                intent=c["intent"], expected_run=c["run"], expected_step=c["step"],
                worker_lease_token=c["token"], now=now_utc(),
            )
            return "claimed"
        except PostgresTrustRepositoryConflict:
            return "fenced"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: claim(), range(2)))
    assert sorted(results) == ["claimed", "fenced"]
    persisted = c["repository"].get_for_step(
        workspace_id=c["workspace_id"], run_id=c["run"].id,
        step_id=c["step"].id, idempotency_key=c["step"].idempotency_key,
    )
    assert persisted is not None
    assert persisted.status == BranchIntentStatus.POST_CLAIMED


def test_postgres_branch_claim_fails_after_capability_revocation_or_expiry(pg_branch_context) -> None:
    c = pg_branch_context
    for update in (
        {"status": CapabilityLeaseStatus.REVOKED, "revoked_at": now_utc()},
        {"expires_at": now_utc() - timedelta(seconds=1)},
    ):
        changed = c["lease"].model_copy(update={**update, "updated_at": now_utc()})
        with c["connect"]() as conn:
            conn.execute(
                "UPDATE capability_leases SET status=%s,expires_at=%s,updated_at=%s,payload=%s WHERE lease_id=%s",
                (changed.status.value, changed.expires_at, changed.updated_at,
                 Jsonb(changed.model_dump(mode="json")), changed.id),
            )
        with pytest.raises(PostgresTrustRepositoryConflict):
            c["repository"].claim_create_once(
                intent=c["intent"], expected_run=c["run"], expected_step=c["step"],
                worker_lease_token=c["token"], now=now_utc(),
            )
        with c["connect"]() as conn:
            conn.execute(
                "UPDATE capability_leases SET status=%s,expires_at=%s,updated_at=%s,payload=%s WHERE lease_id=%s",
                (c["lease"].status.value, c["lease"].expires_at, c["lease"].updated_at,
                 Jsonb(c["lease"].model_dump(mode="json")), c["lease"].id),
            )


def test_postgres_branch_intent_read_is_workspace_scoped(pg_branch_context) -> None:
    c = pg_branch_context
    assert c["repository"].get_for_step(
        workspace_id=f"other_{c['workspace_id']}", run_id=c["run"].id,
        step_id=c["step"].id, idempotency_key=c["step"].idempotency_key,
    ) is None


@pytest.mark.parametrize("mutation", ["task_repository", "task_branch", "lease_scope", "lease_signature", "approval_target", "decision_lease"])
def test_postgres_branch_claim_fences_authority_mutated_after_intent(
    pg_branch_context, mutation: str
) -> None:
    c = pg_branch_context
    with c["connect"]() as conn:
        if mutation == "task_repository":
            changed = c["task"].model_copy(update={"repository": "attacker/other"})
            conn.execute(
                "UPDATE agent_tasks SET payload=%s WHERE task_id=%s",
                (Jsonb(changed.model_dump(mode="json")), changed.id),
            )
        elif mutation == "task_branch":
            changed = c["task"].model_copy(update={"branch_preference": "feature/other"})
            conn.execute(
                "UPDATE agent_tasks SET payload=%s WHERE task_id=%s",
                (Jsonb(changed.model_dump(mode="json")), changed.id),
            )
        elif mutation == "lease_scope":
            # Preserve indexed scope values and alter an allowed action so this
            # failure specifically demonstrates lease-receipt signature checking.
            changed_scope = c["lease"].scope.model_copy(update={"max_runtime_seconds": 601})
            changed = c["lease"].model_copy(update={"scope": changed_scope})
            conn.execute(
                "UPDATE capability_leases SET payload=%s WHERE lease_id=%s",
                (Jsonb(changed.model_dump(mode="json")), changed.id),
            )
        elif mutation == "lease_signature":
            changed = c["lease"].model_copy(update={"receipt_signature": "invalid-signature"})
            conn.execute(
                "UPDATE capability_leases SET payload=%s WHERE lease_id=%s",
                (Jsonb(changed.model_dump(mode="json")), changed.id),
            )
        elif mutation == "approval_target":
            changed = c["approval"].model_copy(update={"target_branch": "feature/other"})
            changed.approved_payload_hash = compute_approval_payload_hash(changed)
            conn.execute(
                "UPDATE approval_requests SET payload=%s WHERE approval_id=%s",
                (Jsonb(changed.model_dump(mode="json")), changed.id),
            )
            changed_lease = c["lease"].model_copy(
                update={"approved_payload_hash": changed.approved_payload_hash}
            )
            changed_lease.receipt_signature = sign_payload(
                lease_receipt_payload(changed_lease)
            )
            conn.execute(
                "UPDATE capability_leases SET payload=%s WHERE lease_id=%s",
                (Jsonb(changed_lease.model_dump(mode="json")), changed_lease.id),
            )
        else:
            # The schema's composite FK binds decision.lease_id to its action;
            # changing only the JSON payload must fail indexed/payload integrity.
            changed = c["decision"].model_copy(update={"lease_id": "lease_wrong"})
            conn.execute(
                "UPDATE policy_decisions SET payload=%s WHERE decision_id=%s",
                (Jsonb(changed.model_dump(mode="json")), changed.id),
            )
    expected_error = PostgresTrustRepositoryError if mutation == "decision_lease" else PostgresTrustRepositoryConflict
    with pytest.raises(expected_error):
        c["repository"].claim_create_once(
            intent=c["intent"], expected_run=c["run"], expected_step=c["step"],
            worker_lease_token=c["token"], now=now_utc(),
        )
    with c["connect"]() as conn:
        assert conn.execute(
            "SELECT status FROM agent_run_branch_intents WHERE intent_id=%s", (c["intent"].id,)
        ).fetchone()[0] == "INTENT"


def test_postgres_branch_observation_revalidates_lease_scope(pg_branch_context) -> None:
    c = pg_branch_context
    claimed = c["repository"].claim_create_once(
        intent=c["intent"], expected_run=c["run"], expected_step=c["step"],
        worker_lease_token=c["token"], now=now_utc(),
    )
    changed_scope = c["lease"].scope.model_copy(update={"repository": "attacker/other"})
    changed = c["lease"].model_copy(update={"scope": changed_scope})
    with c["connect"]() as conn:
        conn.execute(
            "UPDATE capability_leases SET payload=%s WHERE lease_id=%s",
            (Jsonb(changed.model_dump(mode="json")), changed.id),
        )
    with pytest.raises(PostgresTrustRepositoryConflict):
        c["repository"].record_remote_observation(
            intent=claimed, expected_run=c["run"], expected_step=c["step"],
            worker_lease_token=c["token"], verified_sha=c["intent"].source_sha,
            branch_url=None, reason="test", now=now_utc(),
        )
    with c["connect"]() as conn:
        assert conn.execute(
            "SELECT status FROM agent_run_branch_intents WHERE intent_id=%s", (claimed.id,)
        ).fetchone()[0] == "POST_CLAIMED"


def test_postgres_branch_intent_consumption_rolls_back_as_one_transaction(pg_branch_context) -> None:
    c = pg_branch_context
    with c["connect"]() as conn:
        conn.execute("DELETE FROM agent_run_branch_intents WHERE intent_id=%s", (c["intent"].id,))
        conn.execute(
            """
            CREATE FUNCTION reject_branch_intent_insert() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'injected branch intent persistence failure';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        conn.execute(
            "CREATE TRIGGER reject_branch_intent_insert BEFORE INSERT ON agent_run_branch_intents "
            "FOR EACH ROW EXECUTE FUNCTION reject_branch_intent_insert()"
        )

    with pytest.raises(psycopg.Error, match="injected branch intent persistence failure"):
        c["repository"].begin_intent(
            record=c["intent"], expected_run=c["run"], expected_step=c["step"],
            timeout_seconds=120, actor=c["actor"], now=now_utc(),
        )

    with c["connect"]() as conn:
        assert conn.execute(
            "SELECT count(*) FROM action_consumptions WHERE action_id=%s", (c["action"].id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM agent_run_branch_intents WHERE intent_id=%s", (c["intent"].id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM trust_events WHERE entity_id=%s AND event_type='ACTION_CONSUMED'",
            (c["action"].id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM agent_run_events WHERE run_id=%s", (c["run"].id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE entity_id=%s", (c["run"].id,)
        ).fetchone()[0] == 0
        lease = PostgresTrustRepository().get_capability_lease(
            conn, c["lease"].id, c["workspace_id"]
        )
        assert lease is not None
        assert lease.consumed_runtime_seconds == 0


def _complete_verified_branch(c: dict, intent: AgentRunBranchIntent, *, now) -> None:
    completed_run = c["run"].model_copy(update={
        "status": AgentRunStatus.SUCCEEDED,
        "current_step_id": None,
        "lease_owner": None,
        "lease_token": None,
        "lease_expires_at": None,
        "heartbeat_at": None,
        "completed_at": now,
        "updated_at": now,
    })
    completed_step = c["step"].model_copy(update={
        "status": AgentRunStepStatus.SUCCEEDED,
        "lease_owner": None,
        "lease_token": None,
        "lease_expires_at": None,
        "completed_at": now,
        "updated_at": now,
    })
    run_event = AgentRunEvent(
        run_id=c["run"].id, task_id=c["task"].id, step_id=c["step"].id,
        correlation_id=c["run"].correlation_id,
        event_type=AgentRunEventType.STEP_SUCCEEDED, actor=c["actor"],
        message="Create branch verified", created_at=now,
    )
    run_audit = AuditEvent(
        event_type="agent_run.step_completed", entity_id=c["run"].id,
        workspace_id=c["workspace_id"], actor=c["actor"],
        details={"step_id": c["step"].id}, created_at=now,
    )
    task_event = AgentTaskEvent(
        task_id=c["task"].id, actor=c["actor"], event_type=AgentTaskEventType.NOTE,
        message="Created approved branch", metadata={"branch_intent_id": intent.id},
        created_at=now,
    )
    task_audit = AuditEvent(
        event_type="agent_task.branch_created", entity_id=c["task"].id,
        workspace_id=c["workspace_id"], actor=c["actor"],
        details={"branch_intent_id": intent.id}, created_at=now,
    )
    PostgresAgentRunRepository(c["connect"]).commit_step_completion(
        expected_run=c["run"], updated_run=completed_run,
        expected_steps=[c["step"]], updated_steps=[completed_step],
        events=[run_event], audit=run_audit, now=now,
        branch_intent_id=intent.id, branch_task_event=task_event,
        branch_task_audit=task_audit,
    )


def test_postgres_verified_effect_can_be_reported_after_lease_revocation(pg_branch_context) -> None:
    """Revocation cannot hide a side effect already verified remotely; completion does no POST."""
    c = pg_branch_context
    repo = c["repository"]
    claimed = repo.claim_create_once(
        intent=c["intent"], expected_run=c["run"], expected_step=c["step"],
        worker_lease_token=c["token"], now=now_utc(),
    )
    verified = repo.record_remote_observation(
        intent=claimed, expected_run=c["run"], expected_step=c["step"],
        worker_lease_token=c["token"], verified_sha=c["intent"].source_sha,
        branch_url="https://github.com/owner/repo/tree/feature/approved-branch",
        reason="GET confirmed exact source SHA", now=now_utc(),
    )
    revoked = c["lease"].model_copy(update={
        "status": CapabilityLeaseStatus.REVOKED,
        "revoked_at": now_utc(),
        "updated_at": now_utc(),
    })
    with c["connect"]() as conn:
        conn.execute(
            "UPDATE capability_leases SET status=%s,revoked_at=%s,updated_at=%s,payload=%s "
            "WHERE lease_id=%s AND workspace_id=%s",
            (revoked.status.value, revoked.revoked_at, revoked.updated_at,
             Jsonb(revoked.model_dump(mode="json")), revoked.id, c["workspace_id"]),
        )

    _complete_verified_branch(c, verified, now=now_utc())
    persisted = repo.get_for_step(
        workspace_id=c["workspace_id"], run_id=c["run"].id,
        step_id=c["step"].id, idempotency_key=c["step"].idempotency_key,
    )
    assert persisted is not None and persisted.status == BranchIntentStatus.COMPLETED
    with c["connect"]() as conn:
        assert conn.execute(
            "SELECT status FROM agent_runs WHERE run_id=%s", (c["run"].id,)
        ).fetchone()[0] == "SUCCEEDED"
        assert conn.execute(
            "SELECT count(*) FROM agent_task_events WHERE task_id=%s", (c["task"].id,)
        ).fetchone()[0] == 1


def test_postgres_pg0030_to_pg0031_branch_claim_upgrade(pg_branch_context) -> None:
    """Replays the 0030 schema state then verifies the forward 0031 upgrade."""
    c = pg_branch_context
    with c["connect"]() as conn:
        conn.execute(
            "ALTER TABLE agent_run_branch_intents DROP CONSTRAINT agent_run_branch_intents_status_check"
        )
        conn.execute(
            "ALTER TABLE agent_run_branch_intents ADD CONSTRAINT agent_run_branch_intents_status_check "
            "CHECK (status IN ('INTENT','REMOTE_VERIFIED','RECONCILIATION_REQUIRED','COMPLETED'))"
        )
        conn.execute("DROP INDEX idx_agent_run_branch_intents_reconcile")
        conn.execute(
            "CREATE INDEX idx_agent_run_branch_intents_reconcile "
            "ON agent_run_branch_intents(workspace_id,status,updated_at,intent_id) "
            "WHERE status IN ('INTENT','RECONCILIATION_REQUIRED')"
        )
        conn.execute(
            "DELETE FROM schema_migrations WHERE migration_id='pg_0031_agent_run_branch_post_claim'"
        )

    run_postgres_migrations(c["connect"])
    claimed_payload = c["intent"].model_copy(update={"status": BranchIntentStatus.POST_CLAIMED})
    with c["connect"]() as conn:
        conn.execute(
            "UPDATE agent_run_branch_intents SET status='POST_CLAIMED',payload=%s WHERE intent_id=%s",
            (Jsonb(_payload(claimed_payload)), claimed_payload.id),
        )
        assert conn.execute(
            "SELECT status FROM agent_run_branch_intents WHERE intent_id=%s", (claimed_payload.id,)
        ).fetchone()[0] == "POST_CLAIMED"
        assert conn.execute(
            "SELECT count(*) FROM schema_migrations WHERE migration_id='pg_0031_agent_run_branch_post_claim'"
        ).fetchone()[0] == 1


def test_postgres_branch_observation_mismatch_downgrades_verified_state(pg_branch_context) -> None:
    c = pg_branch_context
    repo = c["repository"]
    claimed = repo.claim_create_once(
        intent=c["intent"], expected_run=c["run"], expected_step=c["step"],
        worker_lease_token=c["token"], now=now_utc(),
    )
    verified = repo.record_remote_observation(
        intent=claimed, expected_run=c["run"], expected_step=c["step"],
        worker_lease_token=c["token"], verified_sha=c["intent"].source_sha,
        branch_url="https://github.com/owner/repo/tree/feature/approved-branch",
        reason="verified", now=now_utc(),
    )
    assert verified.status == BranchIntentStatus.REMOTE_VERIFIED
    mismatch = repo.record_remote_observation(
        intent=verified, expected_run=c["run"], expected_step=c["step"],
        worker_lease_token=c["token"], verified_sha="b" * 40,
        branch_url=verified.branch_url, reason="remote branch points elsewhere", now=now_utc(),
    )
    assert mismatch.status == BranchIntentStatus.RECONCILIATION_REQUIRED


def test_postgres_branch_completion_rejects_stale_intent_revision_atomically(pg_branch_context) -> None:
    c = pg_branch_context
    repo = c["repository"]
    claimed = repo.claim_create_once(
        intent=c["intent"], expected_run=c["run"], expected_step=c["step"],
        worker_lease_token=c["token"], now=now_utc(),
    )
    verified = repo.record_remote_observation(
        intent=claimed, expected_run=c["run"], expected_step=c["step"],
        worker_lease_token=c["token"], verified_sha=c["intent"].source_sha,
        branch_url="https://github.com/owner/repo/tree/feature/approved-branch",
        reason="verified", now=now_utc(),
    )
    stale_revision = verified.run_revision + timedelta(seconds=1)
    stale = verified.model_copy(update={"run_revision": stale_revision})
    with c["connect"]() as conn:
        conn.execute(
            "UPDATE agent_run_branch_intents SET run_revision=%s,payload=%s WHERE intent_id=%s",
            (stale_revision, Jsonb(_payload(stale)), stale.id),
        )

    now = now_utc()
    completed_run = c["run"].model_copy(update={
        "status": AgentRunStatus.SUCCEEDED,
        "current_step_id": None,
        "lease_owner": None,
        "lease_token": None,
        "lease_expires_at": None,
        "heartbeat_at": None,
        "completed_at": now,
        "updated_at": now,
    })
    completed_step = c["step"].model_copy(update={
        "status": AgentRunStepStatus.SUCCEEDED,
        "lease_owner": None,
        "lease_token": None,
        "lease_expires_at": None,
        "completed_at": now,
        "updated_at": now,
    })
    run_event = AgentRunEvent(
        run_id=c["run"].id,
        task_id=c["task"].id,
        step_id=c["step"].id,
        correlation_id=c["run"].correlation_id,
        event_type=AgentRunEventType.STEP_SUCCEEDED,
        actor=c["actor"],
        message="Create branch verified",
        created_at=now,
    )
    run_audit = AuditEvent(
        event_type="agent_run.step_completed", entity_id=c["run"].id,
        workspace_id=c["workspace_id"], actor=c["actor"],
        details={"step_id": c["step"].id}, created_at=now,
    )
    task_event = AgentTaskEvent(
        task_id=c["task"].id, actor=c["actor"],
        event_type=AgentTaskEventType.NOTE,
        message="Created approved branch",
        metadata={"branch_intent_id": verified.id}, created_at=now,
    )
    task_audit = AuditEvent(
        event_type="agent_task.branch_created", entity_id=c["task"].id,
        workspace_id=c["workspace_id"], actor=c["actor"],
        details={"branch_intent_id": verified.id}, created_at=now,
    )
    with pytest.raises(AgentRunRepositoryConflict, match="completion CAS"):
        PostgresAgentRunRepository(c["connect"]).commit_step_completion(
            expected_run=c["run"], updated_run=completed_run,
            expected_steps=[c["step"]], updated_steps=[completed_step],
            events=[run_event], audit=run_audit, now=now,
            branch_intent_id=verified.id, branch_task_event=task_event,
            branch_task_audit=task_audit,
        )
    with c["connect"]() as conn:
        assert conn.execute(
            "SELECT status FROM agent_runs WHERE run_id=%s", (c["run"].id,)
        ).fetchone()[0] == "RUNNING"
        assert conn.execute(
            "SELECT status FROM agent_run_branch_intents WHERE intent_id=%s", (verified.id,)
        ).fetchone()[0] == "REMOTE_VERIFIED"
        assert conn.execute(
            "SELECT count(*) FROM agent_task_events WHERE task_id=%s", (c["task"].id,)
        ).fetchone()[0] == 0
