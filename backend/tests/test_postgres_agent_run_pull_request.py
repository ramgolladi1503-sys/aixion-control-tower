from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import psycopg
import pytest
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app.agent_run_models import (
    AgentRun, AgentRunStatus, AgentRunStep, AgentRunStepStatus, AgentRunStepType,
)
from app.agent_task_models import AgentTask, AgentTaskStatus
from app.models import (
    AgentProvider, ApprovalRequest, ApprovalStatus, RiskAssessment, RiskLevel,
    User, UserRole, now_utc,
)
from app import postgres_database
from app.postgres_agent_run_pull_request import (
    AgentRunPullRequestIntent,
    PostgresAgentRunPullRequestIntentRepository,
    PullRequestIntentStatus,
    _exact_get_match,
    _decode,
    _COLUMNS,
    _payload,
    _valid_pull_url,
)
from app.postgres_database import _migration_checksum, run_postgres_migrations
from app.postgres_trust_repository import PostgresTrustRepository, PostgresTrustRepositoryConflict
from app.postgres_trust_event_repository import PostgresTrustEventRepository
from app.trust_crypto import sha256_hex, sign_payload
from app.trust_models import (
    CapabilityActionType, CapabilityLease, CapabilityLeaseMode, CapabilityLeaseStatus,
    CapabilityScope, PolicyDecision, PolicyDecisionType, ProposedAction,
)
from app.approval_integrity import compute_approval_payload_hash
from app.trust_policy import lease_receipt_payload


@pytest.fixture
def pg_pull_request_schema() -> Iterator[tuple[str, object]]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL pull-request tests")
    schema = f"ct_pull_request_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        conn = psycopg.connect(dsn)
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return conn

    try:
        yield schema, connect
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def pg_pull_request_context(pg_pull_request_schema) -> dict:
    schema, connect = pg_pull_request_schema
    run_postgres_migrations(connect)
    return _seed_pull_request_rows(schema, connect)


def _seed_pull_request_rows(schema: str, connect) -> dict:
    now = now_utc()
    workspace_id = f"workspace_{uuid.uuid4().hex}"
    project_id = f"project_{uuid.uuid4().hex}"
    owner_id = f"owner_{uuid.uuid4().hex}"
    task_id = f"task_{uuid.uuid4().hex}"
    approval_id = f"approval_{uuid.uuid4().hex}"
    actor = f"worker_{uuid.uuid4().hex}"
    repository_name = "owner/repo"
    head_branch = "feature/approved-pr"
    base_branch = "main"
    worker_token = f"worker-token-{uuid.uuid4().hex}"
    token_hash = sha256_hex({"lease_token": worker_token})
    approval = ApprovalRequest(
        id=approval_id, project_id=project_id, workspace_id=workspace_id,
        title="Pull request approval", summary="Approve one pull request",
        target_branch=head_branch, files=[], status=ApprovalStatus.APPROVED,
        risk=RiskAssessment(level=RiskLevel.LOW), approved_by_user_id=owner_id,
        created_at=now, updated_at=now,
    )
    approval.approved_payload_hash = compute_approval_payload_hash(approval)
    task = AgentTask(
        id=task_id, workspace_id=workspace_id, project_id=project_id,
        title="PR task", goal="Create an approved pull request",
        provider=AgentProvider.ANTIGRAVITY, repository=repository_name,
        branch_preference=head_branch, approval_request_id=approval_id,
        status=AgentTaskStatus.APPROVED, created_at=now, updated_at=now,
    )
    lease = CapabilityLease(
        id=f"lease_{uuid.uuid4().hex}", approval_request_id=approval_id,
        workspace_id=workspace_id, approved_payload_hash=approval.approved_payload_hash,
        provider=AgentProvider.ANTIGRAVITY, agent_id=task.external_agent_id,
        mode=CapabilityLeaseMode.BOUNDED,
        scope=CapabilityScope(
            repository=repository_name, branch=head_branch,
            allowed_actions=[CapabilityActionType.CREATE_PULL_REQUEST],
            max_runtime_seconds=600, max_cost_usd=1.0, max_pull_requests=1,
        ),
        issued_by_user_id=owner_id, expires_at=now + timedelta(hours=1),
        receipt_nonce=f"nonce_{uuid.uuid4().hex}", receipt_signature="pending",
        created_at=now, updated_at=now,
    )
    lease.receipt_signature = sign_payload(lease_receipt_payload(lease))
    run_id = f"run_{uuid.uuid4().hex}"
    step_id = f"step_{uuid.uuid4().hex}"
    idempotency_key = f"idem_{uuid.uuid4().hex}"
    run = AgentRun(
        id=run_id, task_id=task_id, workspace_id=workspace_id,
        approval_request_id=approval_id, capability_lease_id=lease.id,
        project_id=project_id, repository=repository_name,
        status=AgentRunStatus.RUNNING, current_step_id=step_id,
        lease_owner=actor, lease_token=worker_token,
        lease_expires_at=now + timedelta(minutes=10), updated_at=now,
    )
    step = AgentRunStep(
        id=step_id, run_id=run_id, task_id=task_id, sequence=1,
        step_type=AgentRunStepType.CREATE_PULL_REQUEST,
        status=AgentRunStepStatus.RUNNING, idempotency_key=idempotency_key,
        lease_owner=actor, lease_token=worker_token,
        lease_expires_at=run.lease_expires_at, updated_at=now,
    )
    expected_head_sha = "d" * 40
    expected_base_sha = "e" * 40
    title_sha256 = "a" * 64
    body_sha256 = "b" * 64
    intent_marker = f"aixion-intent:{uuid.uuid4()}"
    action_id = f"action_{uuid.uuid4().hex}"
    action = ProposedAction(
        id=action_id, lease_id=lease.id, provider=AgentProvider.ANTIGRAVITY,
        agent_id=task.external_agent_id, run_id=run_id, task_id=task_id,
        project_id=project_id, workspace_id=workspace_id,
        action_type=CapabilityActionType.CREATE_PULL_REQUEST,
        repository=repository_name, branch=head_branch,
        estimated_runtime_seconds=120,
        metadata={
            "step_id": step_id, "step_type": "CREATE_PULL_REQUEST",
            "task_id": task_id, "approval_id": approval_id, "lease_id": lease.id,
            "idempotency_key": idempotency_key, "repository": repository_name,
            "head_branch": head_branch, "base_branch": base_branch,
            "validated_head_sha": expected_head_sha,
            "validated_base_sha": expected_base_sha,
            "title_sha256": title_sha256, "body_sha256": body_sha256,
            "intent_marker": intent_marker,
        },
        created_at=now,
    )
    decision = PolicyDecision(
        proposed_action_id=action_id, lease_id=lease.id,
        decision=PolicyDecisionType.ALLOW, risk_level=RiskLevel.LOW,
        evaluated_at=now,
    )
    intent = AgentRunPullRequestIntent(
        id=f"intent_{uuid.uuid4().hex}", workspace_id=workspace_id,
        run_id=run_id, task_id=task_id, approval_id=approval_id, step_id=step_id,
        action_id=action_id, lease_id=lease.id, idempotency_key=idempotency_key,
        repository=repository_name, head_branch=head_branch, base_branch=base_branch,
        expected_head_sha=expected_head_sha, expected_base_sha=expected_base_sha,
        title_sha256=title_sha256, body_sha256=body_sha256, intent_marker=intent_marker,
        lease_token_hash=token_hash, run_revision=now, step_revision=now,
        status=PullRequestIntentStatus.INTENT, created_at=now, updated_at=now,
    )
    with connect() as conn:
        conn.execute("INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
                     (workspace_id, Jsonb({"id": workspace_id, "name": "test", "owner_user_id": owner_id})))
        conn.execute("INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
                     (project_id, workspace_id, Jsonb({"id": project_id, "name": "test", "workspace_id": workspace_id})))
        owner = User(
            id=owner_id, email=f"{owner_id}@example.test", display_name="Test Owner",
            role=UserRole.OWNER, password_hash="test", password_salt="test",
            workspace_id=workspace_id, email_verified=True,
        )
        conn.execute(
            "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,email_verified,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (owner_id, workspace_id, owner.email, owner.email.lower(), owner.role.value,
             owner.disabled, owner.email_verified, Jsonb(owner.model_dump(mode="json"))),
        )
        conn.execute(
            "INSERT INTO approval_requests(approval_id,project_id,workspace_id,work_order_id,source_agent_id,status,expires_at,created_at,updated_at,version,payload) VALUES(%s,%s,%s,NULL,NULL,%s,NULL,%s,%s,0,%s)",
            (approval_id, project_id, workspace_id, approval.status.value, now, now,
             Jsonb(approval.model_dump(mode="json"))),
        )
        conn.execute(
            "INSERT INTO agent_tasks(task_id,project_id,workspace_id,external_agent_id,approval_request_id,status,updated_at,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (task_id, project_id, workspace_id, task.external_agent_id, approval_id,
             task.status.value, task.updated_at, Jsonb(task.model_dump(mode="json"))),
        )
        trust = PostgresTrustRepository()
        trust.insert_capability_lease(conn, lease)
        conn.execute(
            "INSERT INTO agent_runs(run_id,task_id,project_id,workspace_id,approval_id,status,current_step_id,lease_owner,lease_token,lease_expires_at,updated_at,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (run_id, task_id, project_id, workspace_id, approval_id, run.status.value,
             step_id, actor, worker_token, run.lease_expires_at, now, Jsonb(run.model_dump(mode="json"))),
        )
        conn.execute(
            "INSERT INTO agent_run_steps(step_id,run_id,task_id,workspace_id,sequence,status,updated_at,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (step_id, run_id, task_id, workspace_id, step.sequence, step.status.value,
             now, Jsonb(step.model_dump(mode="json"))),
        )
        trust.insert_proposed_action(conn, action)
        trust.insert_policy_decision(conn, decision, workspace_id=workspace_id)
    return {
        "schema": schema, "connect": connect, "workspace_id": workspace_id,
        "project_id": project_id, "actor": actor, "token": worker_token,
        "approval": approval, "task": task, "lease": lease, "run": run,
        "step": step, "action": action, "decision": decision, "intent": intent,
        "repository": PostgresAgentRunPullRequestIntentRepository(connect),
    }


def _intent(**updates) -> AgentRunPullRequestIntent:
    now = datetime.now(timezone.utc)
    return AgentRunPullRequestIntent(
        id="intent-00000000-0000-4000-8000-000000000001",
        workspace_id="workspace-a", run_id="run-a", task_id="task-a",
        approval_id="approval-a", step_id="step-a", action_id="action-a",
        lease_id="lease-a", idempotency_key="step-a-key", repository="owner/repo",
        head_branch="feature/fix", base_branch="main", expected_head_sha="d" * 40,
        expected_base_sha="e" * 40, title_sha256="a" * 64,
        body_sha256="b" * 64, intent_marker="aixion-intent:00000000-0000-4000-8000-000000000001",
        lease_token_hash="c" * 64, run_revision=now, step_revision=now,
        status=PullRequestIntentStatus.INTENT, created_at=now, updated_at=now,
        **updates,
    )


def _authorize(context: dict) -> AgentRunPullRequestIntent:
    return context["repository"].record_authorized_intent(
        record=context["intent"], action=context["action"],
        expected_run=context["run"], expected_step=context["step"],
        timeout_seconds=120, actor=context["actor"], now=now_utc(),
        expected_base_branch=context["intent"].base_branch,
    )


def _evaluate_pr_action(context: dict, action: ProposedAction | None = None):
    trust = PostgresTrustRepository()
    with context["connect"]() as conn:
        with conn.transaction():
            return trust.evaluate_agent_run_action(
                conn, action=action or context["action"],
                workspace_id=context["workspace_id"], run_id=context["run"].id,
                step_id=context["step"].id, actor=context["actor"], now=now_utc(),
                trust_event_repository=PostgresTrustEventRepository(context["connect"]),
                expected_base_branch="main",
            )


class TestPullRequestMigration:
    def test_upgrade_from_pg_0033_preserves_rows_and_replays(
        self, pg_pull_request_schema, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        schema, connect = pg_pull_request_schema
        migrations = postgres_database.POSTGRES_MIGRATIONS
        target = next(
            index for index, item in enumerate(migrations)
            if item.migration_id == "pg_0034_agent_run_pull_request_intents"
        )
        pg_0033 = migrations[target - 1]
        assert pg_0033.migration_id == "pg_0033_agent_run_validation_intents"
        monkeypatch.setattr(postgres_database, "POSTGRES_MIGRATIONS", migrations[:target])
        run_postgres_migrations(connect)
        context = _seed_pull_request_rows(schema, connect)

        tables = (
            ("workspaces", "workspace_id"),
            ("agent_runs", "run_id"),
            ("agent_tasks", "task_id"),
            ("approval_requests", "approval_id"),
            ("agent_run_steps", "step_id"),
            ("proposed_actions", "action_id"),
            ("capability_leases", "lease_id"),
        )
        preserved = {}
        with connect() as conn:
            assert conn.execute(
                "SELECT count(*) FROM schema_migrations WHERE migration_id=%s",
                ("pg_0033_agent_run_validation_intents",),
            ).fetchone() == (1,)
            assert conn.execute(
                "SELECT count(*) FROM schema_migrations WHERE migration_id=%s",
                ("pg_0034_agent_run_pull_request_intents",),
            ).fetchone() == (0,)
            for table, key_column in tables:
                row_id = {
                    "workspaces": context["workspace_id"],
                    "agent_runs": context["run"].id,
                    "agent_tasks": context["task"].id,
                    "approval_requests": context["approval"].id,
                    "agent_run_steps": context["step"].id,
                    "proposed_actions": context["action"].id,
                    "capability_leases": context["lease"].id,
                }[table]
                row = conn.execute(
                    sql.SQL("SELECT * FROM {} WHERE {}=%s").format(
                        sql.Identifier(table), sql.Identifier(key_column),
                    ),
                    (row_id,),
                ).fetchone()
                assert row is not None, f"missing seeded {table} row before upgrade"
                preserved[table] = row

        monkeypatch.setattr(postgres_database, "POSTGRES_MIGRATIONS", migrations)
        run_postgres_migrations(connect)
        migration = migrations[target]
        with connect() as conn:
            assert conn.execute(
                "SELECT name,migration_checksum FROM schema_migrations WHERE migration_id=%s",
                (migration.migration_id,),
            ).fetchone() == (migration.name, _migration_checksum(migration))
            assert conn.execute(
                "SELECT to_regclass(%s),to_regclass(%s),to_regclass(%s)",
                (
                    "agent_run_pull_request_intents",
                    "idx_agent_run_pull_request_reconcile",
                    "idx_agent_runs_id_workspace",
                ),
            ).fetchone() == (
                "agent_run_pull_request_intents",
                "idx_agent_run_pull_request_reconcile",
                "idx_agent_runs_id_workspace",
            )
            columns = {
                row[0] for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name='agent_run_pull_request_intents'",
                    (schema,),
                ).fetchall()
            }
            assert {
                "intent_id", "workspace_id", "run_id", "task_id", "approval_id",
                "step_id", "action_id", "lease_id", "status", "title_sha256",
                "body_sha256", "last_get_status", "get_match_count",
                "get_response_sha256", "payload",
            } <= columns
            constraints = conn.execute(
                "SELECT contype,pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid=%s::regclass",
                (f"{schema}.agent_run_pull_request_intents",),
            ).fetchall()
            definitions = " ".join(definition for _kind, definition in constraints)
            assert "UNIQUE (workspace_id, run_id, step_id, idempotency_key)" in definitions
            assert "UNIQUE (repository, intent_marker)" in definitions
            assert "FOREIGN KEY (action_id, workspace_id)" in definitions
            assert "FOREIGN KEY (lease_id, workspace_id)" in definitions
            assert "ON DELETE RESTRICT" in definitions
            assert "RECONCILIATION_REQUIRED" in definitions
            assert "title_sha256" in definitions and "body_sha256" in definitions
            assert conn.execute(
                "SELECT to_regprocedure(%s)",
                ("aixion_agent_run_pull_request_status_transition()",),
            ).fetchone() == ("aixion_agent_run_pull_request_status_transition()",)
            assert conn.execute(
                "SELECT tgname FROM pg_trigger WHERE tgrelid=%s::regclass AND NOT tgisinternal",
                (f"{schema}.agent_run_pull_request_intents",),
            ).fetchall() == [("trg_agent_run_pull_request_status_transition",)]
            assert conn.execute(
                "SELECT count(*) FROM schema_migrations WHERE migration_id=%s",
                (migration.migration_id,),
            ).fetchone() == (1,)
            for table, key_column in tables:
                row_id = {
                    "workspaces": context["workspace_id"],
                    "agent_runs": context["run"].id,
                    "agent_tasks": context["task"].id,
                    "approval_requests": context["approval"].id,
                    "agent_run_steps": context["step"].id,
                    "proposed_actions": context["action"].id,
                    "capability_leases": context["lease"].id,
                }[table]
                assert conn.execute(
                    sql.SQL("SELECT * FROM {} WHERE {}=%s").format(
                        sql.Identifier(table), sql.Identifier(key_column),
                    ),
                    (row_id,),
                ).fetchone() == preserved[table]

        run_postgres_migrations(connect)
        with connect() as conn:
            assert conn.execute(
                "SELECT count(*) FROM schema_migrations WHERE migration_id=%s",
                (migration.migration_id,),
            ).fetchone() == (1,)
            assert conn.execute(
                "SELECT count(*) FROM agent_run_pull_request_intents"
            ).fetchone() == (0,)

    def test_migration_is_additive_scoped_and_replayable(self, pg_pull_request_schema) -> None:
        schema, connect = pg_pull_request_schema
        run_postgres_migrations(connect)
        migration = next(item for item in postgres_database.POSTGRES_MIGRATIONS
                         if item.migration_id == "pg_0034_agent_run_pull_request_intents")
        with connect() as conn:
            assert conn.execute(
                "SELECT name,migration_checksum FROM schema_migrations WHERE migration_id=%s",
                (migration.migration_id,),
            ).fetchone() == (migration.name, _migration_checksum(migration))
            constraints = conn.execute(
                "SELECT contype,pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid=%s::regclass",
                (f"{schema}.agent_run_pull_request_intents",),
            ).fetchall()
            definitions = " ".join(definition for _, definition in constraints)
            assert "UNIQUE (workspace_id, run_id, step_id, idempotency_key)" in definitions
            assert "UNIQUE (repository, intent_marker)" in definitions
            assert "FOREIGN KEY (action_id, workspace_id)" in definitions
            assert "FOREIGN KEY (lease_id, workspace_id)" in definitions
            assert "ON DELETE RESTRICT" in definitions
            assert "POST_CLAIMED" in definitions and "RECONCILIATION_REQUIRED" in definitions
            assert "title_sha256" in definitions and "body_sha256" in definitions
            assert "expected_head_sha" in definitions and "expected_base_sha" in definitions
            assert conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema=%s "
                "AND table_name='agent_run_pull_request_intents' AND column_name IN ('title','body','token')",
                (schema,),
            ).fetchall() == []
            assert {"last_get_status", "get_match_count", "get_response_sha256"} <= {
                row[0] for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema=%s "
                    "AND table_name='agent_run_pull_request_intents'",
                    (schema,),
                ).fetchall()
            }
            assert conn.execute(
                "SELECT tgname FROM pg_trigger WHERE tgrelid=%s::regclass AND NOT tgisinternal",
                (f"{schema}.agent_run_pull_request_intents",),
            ).fetchall() == [("trg_agent_run_pull_request_status_transition",)]
        run_postgres_migrations(connect)


def test_exact_get_reconciliation_requires_single_exact_identity() -> None:
    intent = _intent()
    good = dict(
        observation_status="CERTAIN", matching_count=1,
        observed_repository="owner/repo", observed_head_branch="feature/fix",
        observed_base_branch="main", marker_matches=True, pull_number=42,
        pull_url="https://github.com/owner/repo/pull/42", head_sha="d" * 40,
        base_sha="e" * 40, response_sha256="f" * 64,
    )
    assert _exact_get_match(intent, **good)
    for mutation in (
        {"matching_count": 0}, {"matching_count": 2}, {"observation_status": "UNCERTAIN"},
        {"observed_repository": "other/repo"}, {"observed_head_branch": "wrong"},
        {"observed_base_branch": "wrong"}, {"marker_matches": False},
        {"head_sha": "1" * 40}, {"base_sha": "2" * 40},
        {"pull_url": "https://github.com/owner/other/pull/42"},
        {"head_sha": "not-a-sha"}, {"response_sha256": None},
    ):
        assert not _exact_get_match(intent, **(good | mutation))


def test_intent_payload_stores_only_content_hashes() -> None:
    intent = _intent()
    payload = _payload(intent)
    assert payload["title_sha256"] == "a" * 64
    assert payload["body_sha256"] == "b" * 64
    assert "title" not in payload and "body" not in payload and "token" not in payload
    fields = [
        getattr(intent, "id" if field == "intent_id" else field)
        for field in _COLUMNS.removesuffix(",payload").split(",")
    ]
    assert _decode(tuple(fields) + (payload,)) == intent


def test_pull_url_is_bound_to_canonical_repository_and_number() -> None:
    assert _valid_pull_url("owner/repo", "https://github.com/owner/repo/pull/12", 12)
    assert not _valid_pull_url("owner/repo", "https://github.com/owner/other/pull/12", 12)
    assert not _valid_pull_url("owner/repo", "http://github.com/owner/repo/pull/12", 12)
    assert not _valid_pull_url("owner/repo", "https://github.com/owner/repo/pull/13", 12)


def test_authorized_intent_and_budget_reservation_commit_atomically(pg_pull_request_context) -> None:
    context = pg_pull_request_context
    saved = _authorize(context)
    assert saved == context["intent"]
    with context["connect"]() as conn:
        row = conn.execute(
            "SELECT status,payload FROM agent_run_pull_request_intents WHERE intent_id=%s AND workspace_id=%s",
            (saved.id, saved.workspace_id),
        ).fetchone()
        consumption = conn.execute(
            "SELECT payload->'consumption' FROM action_consumptions WHERE action_id=%s AND workspace_id=%s",
            (saved.action_id, saved.workspace_id),
        ).fetchone()
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE workspace_id=%s AND event_type='agent_run.pull_request_intent_recorded'",
            (saved.workspace_id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM agent_run_events WHERE workspace_id=%s AND run_id=%s",
            (saved.workspace_id, saved.run_id),
        ).fetchone()[0] == 1
    assert row is not None and row[0] == PullRequestIntentStatus.INTENT.value
    assert consumption is not None
    assert consumption[0]["actual_runtime_seconds"] == 120
    assert consumption[0]["pull_requests_created"] == 1


def test_trust_evaluator_allows_approved_head_with_configured_main_base(pg_pull_request_context) -> None:
    context = pg_pull_request_context
    decision, lease = _evaluate_pr_action(context)
    assert decision.decision == PolicyDecisionType.ALLOW
    assert lease.id == context["lease"].id
    assert context["approval"].target_branch == context["intent"].head_branch
    assert context["intent"].base_branch == "main"


@pytest.mark.parametrize("failure", ["wrong_step", "wrong_type", "wrong_base", "approval_head"])
def test_trust_evaluator_rejects_wrong_pr_step_type_or_head_base_scope(
    pg_pull_request_context, failure,
) -> None:
    context = pg_pull_request_context
    action = context["action"]
    if failure == "wrong_step":
        metadata = dict(action.metadata)
        metadata["step_type"] = "APPLY_CHANGES"
        action = action.model_copy(update={"metadata": metadata})
    elif failure == "wrong_type":
        action = action.model_copy(update={"action_type": CapabilityActionType.CREATE_BRANCH})
    elif failure == "wrong_base":
        metadata = dict(action.metadata)
        metadata["base_branch"] = "release"
        action = action.model_copy(update={"metadata": metadata})
    else:
        approval = context["approval"].model_copy(update={"target_branch": "main"})
        approval.approved_payload_hash = compute_approval_payload_hash(approval)
        with context["connect"]() as conn:
            conn.execute(
                "UPDATE approval_requests SET payload=%s WHERE approval_id=%s AND workspace_id=%s",
                (Jsonb(approval.model_dump(mode="json")), approval.id, approval.workspace_id),
            )
    with pytest.raises(PostgresTrustRepositoryConflict):
        _evaluate_pr_action(context, action)


@pytest.mark.parametrize("change", ["expired", "revoked"])
def test_trust_evaluator_rejects_expired_or_revoked_capability_lease(
    pg_pull_request_context, change,
) -> None:
    context = pg_pull_request_context
    if change == "expired":
        lease = context["lease"].model_copy(update={"expires_at": now_utc() - timedelta(seconds=1)})
        lease.receipt_signature = sign_payload(lease_receipt_payload(lease))
    else:
        lease = context["lease"].model_copy(update={"status": CapabilityLeaseStatus.REVOKED})
    with context["connect"]() as conn:
        conn.execute(
            "UPDATE capability_leases SET status=%s,expires_at=%s,consumed_pull_requests=%s,"
            "consumed_retries=%s,consumed_runtime_seconds=%s,consumed_cost_usd=%s,updated_at=%s,payload=%s "
            "WHERE lease_id=%s AND workspace_id=%s",
            (
                lease.status.value,
                lease.expires_at,
                lease.consumed_pull_requests,
                lease.consumed_retries,
                lease.consumed_runtime_seconds,
                lease.consumed_cost_usd,
                lease.updated_at,
                Jsonb(lease.model_dump(mode="json")),
                lease.id,
                lease.workspace_id,
            ),
        )
    with pytest.raises(PostgresTrustRepositoryConflict):
        _evaluate_pr_action(context)


@pytest.mark.parametrize("change", ["expired", "revoked"])
def test_expired_or_revoked_lease_before_claim_fails_closed(pg_pull_request_context, change) -> None:
    context = pg_pull_request_context
    authorized = _authorize(context)
    now = now_utc()
    if change == "expired":
        lease = context["lease"].model_copy(update={"expires_at": now - timedelta(seconds=1)})
        lease.receipt_signature = sign_payload(lease_receipt_payload(lease))
    else:
        lease = context["lease"].model_copy(update={"status": CapabilityLeaseStatus.REVOKED})
    with context["connect"]() as conn:
        conn.execute(
            "UPDATE capability_leases SET status=%s,expires_at=%s,consumed_pull_requests=%s,"
            "consumed_retries=%s,consumed_runtime_seconds=%s,consumed_cost_usd=%s,updated_at=%s,payload=%s "
            "WHERE lease_id=%s AND workspace_id=%s",
            (
                lease.status.value,
                lease.expires_at,
                lease.consumed_pull_requests,
                lease.consumed_retries,
                lease.consumed_runtime_seconds,
                lease.consumed_cost_usd,
                lease.updated_at,
                Jsonb(lease.model_dump(mode="json")),
                lease.id,
                lease.workspace_id,
            ),
        )
    with pytest.raises(PostgresTrustRepositoryConflict):
        context["repository"].claim_post_once(
            intent=authorized, expected_run=context["run"], expected_step=context["step"],
            worker_lease_token=context["token"],
        )
    persisted = context["repository"].get(intent_id=authorized.id, workspace_id=authorized.workspace_id)
    assert persisted is not None and persisted.status == PullRequestIntentStatus.INTENT
    with context["connect"]() as conn:
        assert conn.execute(
            "SELECT count(*) FROM action_consumptions WHERE action_id=%s",
            (authorized.action_id,),
        ).fetchone()[0] == 1


def test_concurrent_post_claim_has_exactly_one_winner(pg_pull_request_context) -> None:
    context = pg_pull_request_context
    authorized = _authorize(context)
    barrier = Barrier(2)

    def claim() -> str:
        barrier.wait()
        try:
            context["repository"].claim_post_once(
                intent=authorized, expected_run=context["run"], expected_step=context["step"],
                worker_lease_token=context["token"],
            )
            return "claimed"
        except PostgresTrustRepositoryConflict:
            return "fenced"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: claim(), range(2)))
    assert sorted(results) == ["claimed", "fenced"]
    persisted = context["repository"].get(intent_id=authorized.id, workspace_id=authorized.workspace_id)
    assert persisted is not None and persisted.status == PullRequestIntentStatus.POST_CLAIMED


@pytest.mark.parametrize(
    ("observation_status", "matching_count"),
    [("UNCERTAIN", 1), ("CERTAIN", 0), ("CERTAIN", 2)],
)
def test_uncertain_zero_or_multiple_get_reconciliation_never_reopens_post(
    pg_pull_request_context, observation_status, matching_count,
) -> None:
    context = pg_pull_request_context
    authorized = _authorize(context)
    claimed = context["repository"].claim_post_once(
        intent=authorized, expected_run=context["run"], expected_step=context["step"],
        worker_lease_token=context["token"],
    )
    ambiguous = context["repository"].record_post_outcome(
        intent_id=claimed.id, workspace_id=claimed.workspace_id, outcome="AMBIGUOUS",
        reason="simulated timeout after request dispatch",
    )
    unresolved = context["repository"].record_get_reconciliation(
        intent_id=ambiguous.id, workspace_id=ambiguous.workspace_id,
        observation_status=observation_status, matching_count=matching_count,
        response_sha256="f" * 64,
    )
    assert unresolved.status == PullRequestIntentStatus.RECONCILIATION_REQUIRED
    assert unresolved.last_get_status == observation_status
    assert unresolved.get_match_count == matching_count
    assert unresolved.get_response_sha256 == "f" * 64
    with pytest.raises(PostgresTrustRepositoryConflict):
        context["repository"].claim_post_once(
            intent=unresolved, expected_run=context["run"], expected_step=context["step"],
            worker_lease_token=context["token"],
        )
