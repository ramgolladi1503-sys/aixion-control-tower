from __future__ import annotations

import multiprocessing
import os
from datetime import timedelta
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb
from fastapi.testclient import TestClient

from app import main
from app.agent_task_repository import PostgresAgentTaskRepository
from app.agent_task_models import (
    AgentTask,
    AgentTaskEvent,
    AgentTaskEventType,
    AgentTaskRequestedAction,
    AgentTaskStatus,
)
from app.models import (
    ApprovalRequest,
    ApprovalStatus,
    AuditEvent,
    FileChange,
    Notification,
    RiskAssessment,
    RiskLevel,
    Workspace,
    Project,
    User,
    now_utc,
)
from app.approval_expiry import expire_due_approvals
from app.approval_repository import PostgresApprovalDecisionRepository
from app.store import store

pytest_plugins = ("test_postgres_lifespan_approval_inbox",)


def _expire_workspace_approvals_in_process(
    dsn: str,
    schema: str,
    workspace_id: str,
    barrier: multiprocessing.synchronize.Barrier,
    results: multiprocessing.queues.Queue,
) -> None:
    """Run one expiry sweep through a fresh process and database connection."""
    try:

        def connect():
            connection = psycopg.connect(dsn)
            connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            return connection

        repository = PostgresApprovalDecisionRepository(connect)
        barrier.wait(timeout=20)
        changed = expire_due_approvals(
            actor="parallel-expiry-test",
            repository=repository,
            workspace_id=workspace_id,
            limit=20,
        )
        results.put(("ok", changed))
    except BaseException as error:  # deliver child failures instead of hanging the parent
        results.put(("error", type(error).__name__, str(error)))


def _create_due_approval(repository, *, project_id: str, workspace_id: str) -> ApprovalRequest:
    now = now_utc()
    approval = ApprovalRequest(
        id=f"approval_expiry_{uuid.uuid4().hex}",
        project_id=project_id,
        workspace_id=workspace_id,
        title="Approval expiry integration",
        summary="A durable sweeper must expire and audit this exact request.",
        agent_name="Antigravity expiry integration",
        target_branch="feature/approval-expiry-integration",
        files=[FileChange(path="antigravity://tool/run_command", diff='{"command":"echo test"}')],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.PENDING_REVIEW,
        source_provider="ANTIGRAVITY",
        expires_at=now - timedelta(seconds=2),
        created_at=now - timedelta(minutes=5),
        updated_at=now - timedelta(minutes=5),
    )
    repository.create(
        approval=approval,
        audit_event=AuditEvent(
            event_type="approval.created_by_agent",
            entity_id=approval.id,
            workspace_id=workspace_id,
            actor="agent:expiry-test",
            created_at=approval.created_at,
            details={"source_provider": approval.source_provider},
        ),
        notification=Notification(
            title="Approval waiting",
            body="Review the requested action.",
            entity_type="approval_request",
            entity_id=approval.id,
            approval_request_id=approval.id,
            workspace_id=workspace_id,
        ),
    )
    return approval


def test_postgres_lifespan_expiry_sweeper_is_durable_bounded_and_fail_closed(
    postgres_lifespan_approval_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, workspace, project, _user, _agent, _agent_token = postgres_lifespan_approval_context
    runtime = main.PostgresRuntime.from_environment()
    repository = runtime.repositories.approval_decisions
    rollback_candidate = _create_due_approval(
        repository, project_id=project.id, workspace_id=workspace.id
    )

    assert [
        item.id
        for item in repository.list_due_pending(
            now=now_utc(),
            legacy_ttl_seconds=600,
            limit=1,
            workspace_id=workspace.id,
        )
    ] == [rollback_candidate.id]
    assert (
        repository.list_due_pending(
            now=now_utc(),
            legacy_ttl_seconds=600,
            limit=10,
            workspace_id="workspace-not-owned-by-this-test",
        )
        == []
    )

    # An audit write failure must roll back the state transition in the same
    # repository transaction, leaving the approval available for safe retry.
    with connect() as connection:
        connection.execute(
            "CREATE FUNCTION reject_expiry_audit() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN IF NEW.event_type='approval.expired' THEN RAISE EXCEPTION 'injected audit failure'; "
            "END IF; RETURN NEW; END $$"
        )
        connection.execute(
            "CREATE TRIGGER reject_expiry_audit BEFORE INSERT ON audit_events "
            "FOR EACH ROW EXECUTE FUNCTION reject_expiry_audit()"
        )
    with pytest.raises(Exception, match="injected audit failure"):
        main.expire_due_approvals(repository=repository, workspace_id=workspace.id, limit=1)
    assert repository.get(rollback_candidate.id).status == ApprovalStatus.PENDING_REVIEW
    with connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM audit_events WHERE entity_id=%s AND event_type='approval.expired'",
                (rollback_candidate.id,),
            ).fetchone()[0]
            == 0
        )
        connection.execute("DROP TRIGGER reject_expiry_audit ON audit_events")
        connection.execute("DROP FUNCTION reject_expiry_audit()")

    assert main.expire_due_approvals(repository=repository, workspace_id=workspace.id, limit=1) == 1
    assert main.expire_due_approvals(repository=repository, workspace_id=workspace.id, limit=1) == 0
    assert repository.get(rollback_candidate.id).status == ApprovalStatus.EXPIRED
    with connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM audit_events WHERE entity_id=%s AND event_type='approval.expired'",
                (rollback_candidate.id,),
            ).fetchone()[0]
            == 1
        )

    lifespan_candidate = _create_due_approval(
        repository, project_id=project.id, workspace_id=workspace.id
    )
    store_snapshot = {
        "approvals": set(store.approval_requests),
        "audits": {event.id for event in store.audit_events},
    }

    def reject_sqlite_access():
        pytest.fail("PostgreSQL lifespan expiry must not access SQLite store locking")

    monkeypatch.setattr(store, "atomic", reject_sqlite_access)
    with TestClient(main.app) as client:
        assert client.app.state.approval_decision_repository is repository
        assert client.app.state.approval_expiry_status == "healthy"
        assert client.app.state.approval_expiry_last_changed_count == 1
    assert repository.get(lifespan_candidate.id).status == ApprovalStatus.EXPIRED
    assert set(store.approval_requests) == store_snapshot["approvals"]
    assert {event.id for event in store.audit_events} == store_snapshot["audits"]


def test_postgres_expiry_race_across_independent_processes_is_atomic_and_workspace_scoped(
    postgres_lifespan_approval_context,
) -> None:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    connect, workspace, project, user, _agent, _agent_token = postgres_lifespan_approval_context
    schema = f"ct_expiry_race_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def race_connect():
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    try:
        from app.postgres_database import run_postgres_migrations

        run_postgres_migrations(race_connect)
        with race_connect() as connection:
            connection.execute(
                "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
                (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
            )
            user_record = User(
                id=user.id,
                email=user.email,
                display_name=user.display_name,
                role=user.role,
                password_hash="0" * 64,
                password_salt="0" * 32,
                workspace_id=workspace.id,
                email_verified=True,
            )
            connection.execute(
                "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,email_verified,payload) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    user_record.id,
                    workspace.id,
                    user_record.email,
                    user_record.email.lower(),
                    user_record.role.value,
                    user_record.disabled,
                    user_record.email_verified,
                    Jsonb(user_record.model_dump(mode="json")),
                ),
            )
            connection.execute(
                "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
                (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))),
            )
        repository = PostgresApprovalDecisionRepository(race_connect)
        approval = _create_due_approval(
            repository, project_id=project.id, workspace_id=workspace.id
        )
        task = AgentTask(
            project_id=project.id,
            workspace_id=workspace.id,
            title="Task linked to expiring approval",
            goal="Remain blocked when its approval expires.",
            requested_action=AgentTaskRequestedAction.CREATE_APPROVAL,
            status=AgentTaskStatus.WAITING_FOR_APPROVAL,
            approval_request_id=approval.id,
            created_by_user_id=user.id,
        )
        PostgresAgentTaskRepository(race_connect).create(
            task=task,
            event=AgentTaskEvent(
                task_id=task.id,
                event_type=AgentTaskEventType.TASK_CREATED,
                actor="test",
            ),
            audit=AuditEvent(
                event_type="agent_task.created",
                entity_id=task.id,
                workspace_id=workspace.id,
                actor="test",
            ),
        )

        other_workspace = Workspace(
            id=f"workspace_{uuid.uuid4().hex}",
            name="Other expiry workspace",
            owner_user_id=f"user_{uuid.uuid4().hex}",
        )
        other_project = Project(
            id=f"project_{uuid.uuid4().hex}",
            name="Other expiry project",
            workspace_id=other_workspace.id,
            github_repo="example/other-expiry",
        )
        with race_connect() as connection:
            connection.execute(
                "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
                (other_workspace.id, Jsonb(other_workspace.model_dump(mode="json"))),
            )
            connection.execute(
                "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
                (
                    other_project.id,
                    other_workspace.id,
                    Jsonb(other_project.model_dump(mode="json")),
                ),
            )
        other_approval = _create_due_approval(
            repository,
            project_id=other_project.id,
            workspace_id=other_workspace.id,
        )

        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        results = context.Queue()
        processes = [
            context.Process(
                target=_expire_workspace_approvals_in_process,
                args=(dsn, schema, workspace.id, barrier, results),
            )
            for _ in range(2)
        ]
        try:
            for process in processes:
                process.start()
            outcomes = [results.get(timeout=35) for _ in processes]
        finally:
            for process in processes:
                process.join(timeout=5)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
            results.close()

        assert all(process.exitcode == 0 for process in processes), outcomes
        assert all(outcome[0] == "ok" for outcome in outcomes), outcomes
        assert sum(outcome[1] for outcome in outcomes) == 1, outcomes
        assert repository.get(approval.id).status == ApprovalStatus.EXPIRED
        assert repository.get(other_approval.id).status == ApprovalStatus.PENDING_REVIEW
        with race_connect() as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM audit_events WHERE approval_id=%s "
                    "AND event_type='approval.expired'",
                    (approval.id,),
                ).fetchone()[0]
                == 1
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM audit_events WHERE approval_id=%s "
                    "AND event_type='approval.expired'",
                    (other_approval.id,),
                ).fetchone()[0]
                == 0
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM agent_task_events WHERE task_id=%s "
                    "AND event_type='NOTE' AND payload->>'message'=%s",
                    (task.id, "Linked approval expired; task remains blocked."),
                ).fetchone()[0]
                == 1
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM notifications WHERE task_id=%s AND workspace_id=%s",
                    (task.id, workspace.id),
                ).fetchone()[0]
                == 1
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM notifications WHERE approval_id=%s",
                    (other_approval.id,),
                ).fetchone()[0]
                == 1
            )
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
