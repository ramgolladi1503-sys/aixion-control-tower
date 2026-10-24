from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator

import psycopg
import pytest
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app.agent_task_models import AgentTask, AgentTaskEvent, AgentTaskEventType, AgentTaskStatus
from app.agent_task_repository import AgentTaskRepositoryConflict, PostgresAgentTaskRepository, SQLiteAgentTaskRepository
from app.models import AuditEvent, Notification, Project, Workspace, now_utc
from app.postgres_database import run_postgres_migrations
from app.store import store


@pytest.fixture
def postgres_agent_tasks() -> Iterator[tuple[PostgresAgentTaskRepository, Callable[[], Connection]]]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL integration tests")
    schema = f"ct_task_event_{uuid.uuid4().hex}"
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


def _event_commit_records(task: AgentTask, *, status: AgentTaskStatus, notification_id: str):
    updated = task.model_copy(deep=True)
    updated.status = status
    updated.updated_at = now_utc()
    notification = Notification(
        id=notification_id,
        title="Agent task status",
        body="Worker status changed.",
        entity_type="agent_task",
        entity_id=task.id,
        workspace_id=task.workspace_id,
    )
    event = AgentTaskEvent(
        task_id=task.id,
        event_type=AgentTaskEventType.EXECUTION_STARTED,
        message="Worker status changed.",
        status=status,
        metadata={"notification_id": notification.id},
    )
    audit = AuditEvent(
        event_type="agent_task.event_recorded",
        entity_id=task.id,
        workspace_id=task.workspace_id,
        details={
            "event_id": event.id,
            "status": event.status,
            "message": event.message,
            "notification_id": notification.id,
        },
    )
    return updated, event, audit, notification


def test_sqlite_event_audit_and_notification_commit_as_one_transition() -> None:
    store.reset()
    repository = SQLiteAgentTaskRepository(store._connect)
    task = AgentTask(id="task_event_tx", title="Worker", goal="Record status")
    created_event = AgentTaskEvent(task_id=task.id, event_type=AgentTaskEventType.TASK_CREATED)
    created_audit = AuditEvent(event_type="agent_task.created", entity_id=task.id)
    repository.create(task=task, event=created_event, audit=created_audit)
    expected = repository.get(task.id)
    assert expected is not None
    updated, event, audit, notification = _event_commit_records(
        expected, status=AgentTaskStatus.EXECUTING, notification_id="notification_event_tx"
    )

    repository.commit_event(
        expected=expected, updated=updated, event=event, audit=audit,
        notification=notification,
    )

    assert repository.get(task.id) == updated
    assert repository.list_events(task.id)[-1] == event
    with store._connect() as conn:
        persisted_notification = conn.execute(
            "SELECT payload FROM kv_store WHERE entity_type='notification' AND entity_id=?",
            (notification.id,),
        ).fetchone()
        assert persisted_notification is not None
        assert Notification.model_validate_json(persisted_notification[0]) == notification

    stale_updated, stale_event, stale_audit, stale_notification = _event_commit_records(
        expected, status=AgentTaskStatus.FAILED, notification_id="notification_stale_event"
    )
    with pytest.raises(AgentTaskRepositoryConflict, match="changed before event"):
        repository.commit_event(
            expected=expected, updated=stale_updated, event=stale_event,
            audit=stale_audit, notification=stale_notification,
        )


def test_postgres_event_notification_failure_rolls_back_task_event_and_audit(postgres_agent_tasks) -> None:
    repository, connect = postgres_agent_tasks
    suffix = uuid.uuid4().hex
    workspace = Workspace(id=f"workspace_{suffix}", name="Event workspace", owner_user_id="owner")
    project = Project(id=f"project_{suffix}", name="Event project", workspace_id=workspace.id)
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
        title="Worker", goal="Record status",
    )
    created_event = AgentTaskEvent(task_id=task.id, event_type=AgentTaskEventType.TASK_CREATED)
    created_audit = AuditEvent(
        event_type="agent_task.created", entity_id=task.id, workspace_id=workspace.id,
    )
    repository.create(task=task, event=created_event, audit=created_audit)
    expected = repository.get(task.id)
    assert expected is not None
    updated, event, audit, notification = _event_commit_records(
        expected, status=AgentTaskStatus.EXECUTING, notification_id=f"notification_{suffix}"
    )
    repository.commit_event(
        expected=expected, updated=updated, event=event, audit=audit,
        notification=notification,
    )
    assert repository.get(task.id) == updated

    failed_updated, failed_event, failed_audit, duplicate_notification = _event_commit_records(
        updated, status=AgentTaskStatus.FAILED, notification_id=notification.id
    )
    with pytest.raises(AgentTaskRepositoryConflict, match="event record already exists"):
        repository.commit_event(
            expected=updated, updated=failed_updated, event=failed_event,
            audit=failed_audit, notification=duplicate_notification,
        )

    assert repository.get(task.id) == updated
    assert len(repository.list_events(task.id, workspace_id=workspace.id)) == 2
    with connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE entity_id=%s AND event_type='agent_task.event_recorded'",
            (task.id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE task_id=%s", (task.id,),
        ).fetchone()[0] == 1
