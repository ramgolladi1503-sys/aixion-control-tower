from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app.agent_task_cancel import (
    append_cancel_requested_event,
    audit_cancel_requested,
    prepare_linked_approval_cancellation,
)
from app.agent_run_models import AgentRun, AgentRunStatus
from app.agent_task_models import AgentTask, AgentTaskEvent, AgentTaskEventType, AgentTaskStatus
from app.agent_task_repository import (
    AgentTaskRepositoryConflict,
    PostgresAgentTaskRepository,
    SQLiteAgentTaskRepository,
)
from app.models import (
    ApprovalRequest,
    AuditEvent,
    FileChange,
    Notification,
    Project,
    RiskAssessment,
    RiskLevel,
    Workspace,
)
from app.approval_repository import PostgresApprovalDecisionRepository, SQLiteApprovalDecisionRepository
from app.postgres_database import run_postgres_migrations
from app.store import store


@pytest.fixture
def postgres_agent_tasks() -> Iterator[tuple[PostgresAgentTaskRepository, Callable[[], Connection]]]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL integration tests")
    schema = f"ct_task_cancel_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        conn = psycopg.connect(dsn)
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return conn

    try:
        run_postgres_migrations(connect)
        yield PostgresAgentTaskRepository(connect), connect
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _cancel_records(expected: AgentTask) -> tuple[AgentTask, AgentTaskEvent, AuditEvent]:
    updated = expected.model_copy(deep=True)
    event = append_cancel_requested_event(updated, actor="reviewer@example.test", reason="operator request")
    audit = audit_cancel_requested(updated, actor="reviewer@example.test", event=event)
    return updated, event, audit


def _linked_cancel_records(
    expected: AgentTask,
    approval: ApprovalRequest,
) -> tuple[AgentTask, AgentTaskEvent, AuditEvent, ApprovalRequest, AuditEvent, Notification]:
    cancelled_approval, approval_audit, approval_notification = prepare_linked_approval_cancellation(
        expected, approval, actor="reviewer@example.test", reason="operator request"
    )
    assert cancelled_approval is not None and approval_audit is not None and approval_notification is not None
    updated = expected.model_copy(deep=True)
    event = append_cancel_requested_event(
        updated,
        actor="reviewer@example.test",
        reason="operator request",
        approval_decision_changed=True,
    )
    audit = audit_cancel_requested(updated, actor="reviewer@example.test", event=event)
    return updated, event, audit, cancelled_approval, approval_audit, approval_notification


def _approval_for_task(task: AgentTask) -> ApprovalRequest:
    return ApprovalRequest(
        id=task.approval_request_id,
        project_id=task.project_id,
        workspace_id=task.workspace_id,
        title="Approval under cancellation test",
        summary="A pending approval linked to one task.",
        target_branch="feature/cancel-task",
        files=[FileChange(path="README.md", diff="+change")],
        risk=RiskAssessment(level=RiskLevel.MEDIUM),
    )


def test_sqlite_cancel_commits_state_event_and_audit_and_rejects_stale_retry() -> None:
    store.reset()
    repository = SQLiteAgentTaskRepository(store._connect)
    task = AgentTask(
        id="task_cancel_atomic", title="Cancel", goal="Cancel atomically",
        status=AgentTaskStatus.EXECUTING,
    )
    created_event = AgentTaskEvent(task_id=task.id, event_type=AgentTaskEventType.TASK_CREATED)
    created_audit = AuditEvent(
        event_type="agent_task.created", entity_id=task.id,
    )
    repository.create(task=task, event=created_event, audit=created_audit)
    durable_expected = repository.get(task.id)
    assert durable_expected is not None
    updated, event, audit = _cancel_records(durable_expected)

    failed_updated, failed_event, failed_audit = _cancel_records(durable_expected)
    failed_audit.id = created_audit.id
    with pytest.raises(AgentTaskRepositoryConflict, match="record already exists"):
        repository.commit_cancel(
            expected=durable_expected, updated=failed_updated,
            event=failed_event, audit=failed_audit,
        )
    assert repository.get(task.id) == durable_expected
    assert repository.list_events(task.id) == [created_event]

    repository.commit_cancel(expected=durable_expected, updated=updated, event=event, audit=audit)

    assert repository.get(task.id) == updated
    assert repository.list_events(task.id)[-1] == event
    with store._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type='audit_event' "
            "AND json_extract(payload,'$.event_type')='agent_task.cancelled'"
        ).fetchone()[0] == 1
    retry_updated, retry_event, retry_audit = _cancel_records(durable_expected)
    with pytest.raises(AgentTaskRepositoryConflict, match="changed before cancellation"):
        repository.commit_cancel(
            expected=durable_expected, updated=retry_updated,
            event=retry_event, audit=retry_audit,
        )


@pytest.mark.parametrize("lease_shape", ["active", "expired", "incomplete"])
def test_sqlite_cancel_transaction_rejects_any_recorded_worker_lease(lease_shape: str) -> None:
    store.reset()
    repository = SQLiteAgentTaskRepository(store._connect)
    task = AgentTask(
        id=f"task_cancel_lease_{lease_shape}", title="Cancel", goal="Lease safety",
        status=AgentTaskStatus.EXECUTING,
    )
    task.worker_lease_owner = "worker-a"
    if lease_shape != "incomplete":
        task.worker_lease_token = "lease-a"
        delta = timedelta(minutes=5) if lease_shape == "active" else -timedelta(minutes=5)
        task.worker_lease_expires_at = datetime.now(timezone.utc) + delta
    created_event = AgentTaskEvent(task_id=task.id, event_type=AgentTaskEventType.TASK_CREATED)
    repository.create(
        task=task,
        event=created_event,
        audit=AuditEvent(event_type="agent_task.created", entity_id=task.id),
    )
    expected = repository.get(task.id)
    assert expected is not None
    updated, event, audit = _cancel_records(expected)

    with pytest.raises(ValueError, match="worker lease is recorded"):
        repository.commit_cancel(expected=expected, updated=updated, event=event, audit=audit)

    assert repository.get(task.id) == expected
    assert repository.list_events(task.id)[0].event_type == AgentTaskEventType.TASK_CREATED


def test_sqlite_cancel_transaction_refuses_task_with_nonterminal_agent_run() -> None:
    store.reset()
    repository = SQLiteAgentTaskRepository(store._connect)
    task = AgentTask(
        id="task_cancel_with_run", project_id="project_cancel_with_run",
        workspace_id="workspace_cancel_with_run", title="Cancel", goal="Run safety",
        status=AgentTaskStatus.APPROVED,
    )
    store.projects[task.project_id] = Project(
        id=task.project_id, name="Run safety", workspace_id=task.workspace_id
    )
    store.persist()
    repository.create(
        task=task,
        event=AgentTaskEvent(task_id=task.id, event_type=AgentTaskEventType.TASK_CREATED),
        audit=AuditEvent(
            event_type="agent_task.created", entity_id=task.id,
            workspace_id=task.workspace_id,
        ),
    )
    run = AgentRun(
        id="run_cancel_with_task", task_id=task.id, project_id=task.project_id,
        workspace_id=task.workspace_id, status=AgentRunStatus.RUNNING,
    )
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            ("agent_run", run.id, run.model_dump_json()),
        )
    expected = repository.get(task.id)
    assert expected is not None
    updated, event, audit = _cancel_records(expected)

    with pytest.raises(AgentTaskRepositoryConflict, match="AgentRun is linked"):
        repository.commit_cancel(expected=expected, updated=updated, event=event, audit=audit)

    assert repository.get(task.id) == expected
    assert repository.list_events(task.id)[0].event_type == AgentTaskEventType.TASK_CREATED


def test_sqlite_waiting_task_and_pending_approval_cancel_commit_or_rollback_together() -> None:
    store.reset()
    repository = SQLiteAgentTaskRepository(store._connect)
    project = Project(id="project_cancel_approval", name="Cancel approval", workspace_id="workspace_cancel_approval")
    task = AgentTask(
        id="task_cancel_approval",
        project_id=project.id,
        workspace_id=project.workspace_id,
        approval_request_id="approval_cancel_approval",
        title="Cancel linked request",
        goal="Cancel both records atomically",
        status=AgentTaskStatus.WAITING_FOR_APPROVAL,
    )
    approval = _approval_for_task(task)
    store.projects[project.id] = project
    store.approval_requests[approval.id] = approval
    store.persist()
    task_audit = AuditEvent(event_type="agent_task.created", entity_id=task.id, workspace_id=task.workspace_id)
    repository.create(
        task=task,
        event=AgentTaskEvent(task_id=task.id, event_type=AgentTaskEventType.TASK_CREATED),
        audit=task_audit,
    )
    expected = repository.get(task.id)
    durable_approval = SQLiteApprovalDecisionRepository(store._connect).get(approval.id)
    assert expected is not None and durable_approval == approval
    records = _linked_cancel_records(expected, durable_approval)
    updated, event, audit, cancelled, approval_audit, notification = records

    failed_approval_audit = approval_audit.model_copy(update={"id": task_audit.id})
    with pytest.raises(AgentTaskRepositoryConflict, match="record already exists"):
        repository.commit_cancel(
            expected=expected,
            updated=updated,
            event=event,
            audit=audit,
            expected_approval=durable_approval,
            cancelled_approval=cancelled,
            approval_audit=failed_approval_audit,
            approval_notification=notification,
        )
    assert repository.get(task.id) == expected
    assert SQLiteApprovalDecisionRepository(store._connect).get(approval.id) == approval

    repository.commit_cancel(
        expected=expected,
        updated=updated,
        event=event,
        audit=audit,
        expected_approval=durable_approval,
        cancelled_approval=cancelled,
        approval_audit=approval_audit,
        approval_notification=notification,
    )
    assert repository.get(task.id) == updated
    assert SQLiteApprovalDecisionRepository(store._connect).get(approval.id) == cancelled
    with store._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type='notification' "
            "AND json_extract(payload,'$.approval_request_id')=?",
            (approval.id,),
        ).fetchone()[0] == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "project_id",
        "workspace_id",
        "approval_request_id",
        "event_operation",
        "event_actor",
        "audit_actor",
        "audit_approval_id",
        "audit_previous_status",
        "audit_cleanup_claim",
    ],
)
def test_sqlite_cancel_rejects_records_not_bound_to_original_task_and_audit(mutation: str) -> None:
    task = AgentTask(
        id=f"task_cancel_integrity_{mutation}",
        project_id="project-original",
        workspace_id="workspace-original",
        approval_request_id="approval-original",
        title="Cancel",
        goal="Bind cancellation record integrity",
        status=AgentTaskStatus.EXECUTING,
    )
    updated, event, audit = _cancel_records(task)

    if mutation == "project_id":
        updated.project_id = "project-other"
    elif mutation == "workspace_id":
        updated.workspace_id = "workspace-other"
        audit.workspace_id = "workspace-other"
    elif mutation == "approval_request_id":
        updated.approval_request_id = "approval-other"
        audit.details["approval_request_id"] = "approval-other"
    elif mutation == "event_operation":
        event.metadata["operation_type"] = "other_operation"
    elif mutation == "event_actor":
        event.actor = "another-actor"
    elif mutation == "audit_actor":
        audit.actor = "another-actor"
    elif mutation == "audit_approval_id":
        audit.details["approval_request_id"] = "approval-other"
    elif mutation == "audit_previous_status":
        audit.details["previous_status"] = AgentTaskStatus.APPROVED
    elif mutation == "audit_cleanup_claim":
        audit.details["worker_cleanup_performed"] = True

    with pytest.raises(ValueError, match="cancellation records do not match"):
        SQLiteAgentTaskRepository(store._connect).commit_cancel(
            expected=task, updated=updated, event=event, audit=audit,
        )


def test_postgres_cancel_is_workspace_scoped_and_rejects_stale_retry(postgres_agent_tasks) -> None:
    repository, connect = postgres_agent_tasks
    suffix = uuid.uuid4().hex
    workspace = Workspace(id=f"workspace_{suffix}", name="Cancel workspace", owner_user_id="owner")
    project = Project(id=f"project_{suffix}", name="Cancel project", workspace_id=workspace.id)
    with connect() as conn:
        conn.execute(
            "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
            (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
        )
        conn.execute(
            "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
            (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))),
        )
    task = AgentTask(
        id=f"task_{suffix}", project_id=project.id, workspace_id=workspace.id,
        title="Cancel", goal="Cancel atomically", status=AgentTaskStatus.EXECUTING,
    )
    created_event = AgentTaskEvent(task_id=task.id, event_type=AgentTaskEventType.TASK_CREATED)
    created_audit = AuditEvent(
        event_type="agent_task.created", entity_id=task.id, workspace_id=workspace.id,
    )
    repository.create(task=task, event=created_event, audit=created_audit)
    durable_expected = repository.get(task.id)
    assert durable_expected is not None
    updated, event, audit = _cancel_records(durable_expected)

    failed_updated, failed_event, failed_audit = _cancel_records(durable_expected)
    failed_audit.id = created_audit.id
    with pytest.raises(AgentTaskRepositoryConflict, match="record already exists"):
        repository.commit_cancel(
            expected=durable_expected, updated=failed_updated,
            event=failed_event, audit=failed_audit,
        )
    assert repository.get(task.id) == durable_expected
    assert repository.list_events(task.id, workspace_id=workspace.id) == [created_event]

    repository.commit_cancel(expected=durable_expected, updated=updated, event=event, audit=audit)

    assert repository.get(task.id) == updated
    assert repository.list_events(task.id, workspace_id=workspace.id)[-1] == event
    assert repository.list_events(task.id, workspace_id="another_workspace") == []
    retry_updated, retry_event, retry_audit = _cancel_records(durable_expected)
    with pytest.raises(AgentTaskRepositoryConflict, match="changed before cancellation"):
        repository.commit_cancel(
            expected=durable_expected, updated=retry_updated,
            event=retry_event, audit=retry_audit,
        )
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_task_events WHERE task_id=%s", (task.id,)).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE entity_id=%s AND event_type='agent_task.cancelled'",
            (task.id,),
        ).fetchone()[0] == 1


def test_postgres_cancel_transaction_refuses_task_with_nonterminal_agent_run(postgres_agent_tasks) -> None:
    repository, connect = postgres_agent_tasks
    suffix = uuid.uuid4().hex
    workspace = Workspace(id=f"workspace_{suffix}", name="Run cancellation", owner_user_id="owner")
    project = Project(id=f"project_{suffix}", name="Run cancellation", workspace_id=workspace.id)
    with connect() as conn:
        conn.execute(
            "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
            (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
        )
        conn.execute(
            "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
            (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))),
        )
    task = AgentTask(
        id=f"task_{suffix}", project_id=project.id, workspace_id=workspace.id,
        title="Cancel", goal="Do not orphan an active run", status=AgentTaskStatus.APPROVED,
    )
    created_event = AgentTaskEvent(task_id=task.id, event_type=AgentTaskEventType.TASK_CREATED)
    repository.create(
        task=task,
        event=created_event,
        audit=AuditEvent(event_type="agent_task.created", entity_id=task.id, workspace_id=workspace.id),
    )
    run = AgentRun(
        id=f"run_{suffix}", task_id=task.id, project_id=project.id,
        workspace_id=workspace.id, status=AgentRunStatus.RUNNING,
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO agent_runs(run_id,task_id,project_id,workspace_id,approval_id,status,"
            "current_step_id,lease_owner,lease_token,lease_expires_at,updated_at,payload) "
            "VALUES(%s,%s,%s,%s,NULL,%s,NULL,NULL,NULL,NULL,%s,%s)",
            (run.id, task.id, project.id, workspace.id, run.status.value,
             run.updated_at, Jsonb(run.model_dump(mode="json"))),
        )
    expected = repository.get(task.id)
    assert expected is not None
    updated, event, audit = _cancel_records(expected)

    with pytest.raises(AgentTaskRepositoryConflict, match="AgentRun is linked"):
        repository.commit_cancel(expected=expected, updated=updated, event=event, audit=audit)

    assert repository.get(task.id) == expected
    assert repository.list_events(task.id, workspace_id=workspace.id) == [created_event]


def test_postgres_waiting_task_and_pending_approval_cancel_commit_or_rollback_together(postgres_agent_tasks) -> None:
    repository, connect = postgres_agent_tasks
    suffix = uuid.uuid4().hex
    workspace = Workspace(id=f"workspace_{suffix}", name="Cancel approval", owner_user_id="owner")
    project = Project(id=f"project_{suffix}", name="Cancel approval", workspace_id=workspace.id)
    with connect() as conn:
        conn.execute(
            "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
            (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
        )
        conn.execute(
            "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
            (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))),
        )
    task = AgentTask(
        id=f"task_{suffix}",
        project_id=project.id,
        workspace_id=workspace.id,
        approval_request_id=f"approval_{suffix}",
        title="Cancel linked request",
        goal="Cancel both records atomically",
        status=AgentTaskStatus.WAITING_FOR_APPROVAL,
    )
    approval = _approval_for_task(task)
    with connect() as conn:
        conn.execute(
            "INSERT INTO approval_requests(approval_id,project_id,workspace_id,work_order_id,source_agent_id,"
            "source_invocation_id,status,expires_at,created_at,updated_at,version,payload) "
            "VALUES(%s,%s,%s,NULL,NULL,NULL,%s,NULL,%s,%s,0,%s)",
            (
                approval.id,
                approval.project_id,
                approval.workspace_id,
                approval.status.value,
                approval.created_at,
                approval.updated_at,
                Jsonb(approval.model_dump(mode="json")),
            ),
        )
    task_audit = AuditEvent(event_type="agent_task.created", entity_id=task.id, workspace_id=task.workspace_id)
    repository.create(
        task=task,
        event=AgentTaskEvent(task_id=task.id, event_type=AgentTaskEventType.TASK_CREATED),
        audit=task_audit,
    )
    expected = repository.get(task.id)
    durable_approval = PostgresApprovalDecisionRepository(connect).get(approval.id)
    assert expected is not None and durable_approval == approval
    updated, event, audit, cancelled, approval_audit, notification = _linked_cancel_records(expected, durable_approval)

    failed_approval_audit = approval_audit.model_copy(update={"id": task_audit.id})
    with pytest.raises(AgentTaskRepositoryConflict, match="record already exists"):
        repository.commit_cancel(
            expected=expected,
            updated=updated,
            event=event,
            audit=audit,
            expected_approval=durable_approval,
            cancelled_approval=cancelled,
            approval_audit=failed_approval_audit,
            approval_notification=notification,
        )
    assert repository.get(task.id) == expected
    assert PostgresApprovalDecisionRepository(connect).get(approval.id) == approval

    repository.commit_cancel(
        expected=expected,
        updated=updated,
        event=event,
        audit=audit,
        expected_approval=durable_approval,
        cancelled_approval=cancelled,
        approval_audit=approval_audit,
        approval_notification=notification,
    )
    assert repository.get(task.id) == updated
    assert PostgresApprovalDecisionRepository(connect).get(approval.id) == cancelled
    with connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE approval_id=%s",
            (approval.id,),
        ).fetchone()[0] == 1
