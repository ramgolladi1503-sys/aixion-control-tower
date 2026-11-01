from __future__ import annotations

import multiprocessing
import os
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import psycopg
import pytest
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app.agent_task_cancel import append_cancel_requested_event, audit_cancel_requested
from app.agent_task_models import AgentTask, AgentTaskEvent, AgentTaskEventType, AgentTaskStatus
from app.agent_task_repository import (
    AgentTaskRepositoryConflict,
    PostgresAgentTaskRepository,
)
from app.models import AuditEvent, Project, Workspace
from app.postgres_database import run_postgres_migrations


def _cancel_in_process(
    dsn: str,
    schema: str,
    task_data: dict[str, Any],
    index: int,
    start_barrier: Any,
    outcomes: Any,
) -> None:
    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(
            sql.SQL("SET search_path TO {}").format(sql.Identifier(schema))
        )
        return connection

    expected = AgentTask.model_validate(task_data)
    updated = expected.model_copy(deep=True)
    actor = f"reviewer-{index}@example.test"
    event = append_cancel_requested_event(
        updated,
        actor=actor,
        reason="parallel operator cancellation",
    )
    audit = audit_cancel_requested(updated, actor=actor, event=event)

    start_barrier.wait(timeout=20)
    try:
        PostgresAgentTaskRepository(connect).commit_cancel(
            expected=expected,
            updated=updated,
            event=event,
            audit=audit,
        )
    except AgentTaskRepositoryConflict:
        outcomes.put(("conflict", os.getpid()))
    else:
        outcomes.put(("committed", os.getpid()))


@pytest.fixture
def postgres_task_cancel_process_db() -> Iterator[
    tuple[str, str, Callable[[], Connection]]
]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL integration tests")

    schema = f"ct_task_cancel_process_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(
            sql.SQL("SET search_path TO {}").format(sql.Identifier(schema))
        )
        return connection

    try:
        run_postgres_migrations(connect)
        yield dsn, schema, connect
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_concurrent_postgres_task_cancellation_commits_once_across_processes(
    postgres_task_cancel_process_db: tuple[str, str, Callable[[], Connection]],
) -> None:
    dsn, schema, connect = postgres_task_cancel_process_db
    suffix = uuid.uuid4().hex
    workspace = Workspace(
        id=f"workspace_{suffix}",
        name="Process cancellation race",
        owner_user_id=f"owner_{suffix}",
    )
    project = Project(
        id=f"project_{suffix}",
        name="Process cancellation race",
        workspace_id=workspace.id,
    )
    with connect() as connection:
        connection.execute(
            "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
            (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
        )
        connection.execute(
            "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
            (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))),
        )

    task = AgentTask(
        id=f"task_{suffix}",
        project_id=project.id,
        workspace_id=workspace.id,
        title="Cancel concurrently",
        goal="Only one cancellation transition may commit.",
        status=AgentTaskStatus.EXECUTING,
    )
    created_event = AgentTaskEvent(
        task_id=task.id,
        event_type=AgentTaskEventType.TASK_CREATED,
    )
    created_audit = AuditEvent(
        event_type="agent_task.created",
        entity_id=task.id,
        workspace_id=workspace.id,
    )
    repository = PostgresAgentTaskRepository(connect)
    repository.create(task=task, event=created_event, audit=created_audit)
    persisted_snapshot = repository.get(task.id)
    assert persisted_snapshot is not None

    context = multiprocessing.get_context("spawn")
    start_barrier = context.Barrier(2)
    outcomes_queue = context.Queue()
    workers = [
        context.Process(
            target=_cancel_in_process,
            args=(
                dsn,
                schema,
                persisted_snapshot.model_dump(mode="json"),
                index,
                start_barrier,
                outcomes_queue,
            )
        )
        for index in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)
    for worker in workers:
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=5)
            pytest.fail("Task cancellation process did not finish before timeout.")
        assert worker.exitcode == 0

    outcomes = [outcomes_queue.get(timeout=5) for _ in workers]
    outcomes_queue.close()

    assert sorted(outcome for outcome, _ in outcomes) == ["committed", "conflict"]
    assert len({process_id for _, process_id in outcomes}) == 2

    final_task = repository.get(task.id)
    assert final_task is not None
    assert final_task.status == AgentTaskStatus.CANCELLED
    events = repository.list_events(task.id, workspace_id=workspace.id)
    assert len(events) == 2
    cancelled_events = [
        event for event in events if event.event_type == AgentTaskEventType.CANCELLED
    ]
    assert len(cancelled_events) == 1
    assert cancelled_events[0].status == final_task.status
    with connect() as connection:
        audit_count = connection.execute(
            "SELECT count(*) FROM audit_events WHERE entity_id=%s "
            "AND event_type='agent_task.cancelled'",
            (task.id,),
        ).fetchone()[0]
    assert audit_count == 1
