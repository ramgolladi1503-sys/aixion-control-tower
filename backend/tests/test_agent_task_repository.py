from __future__ import annotations

import os
from datetime import timedelta

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from fastapi.testclient import TestClient
import pytest

from app.agent_task_repository import (
    AgentTaskRepositoryConflict,
    SQLiteAgentTaskRepository,
)
from app.agent_task_models import AgentTask, AgentTaskEvent, AgentTaskEventType, AgentTaskStatus
from app.main import app
from app.models import AuditEvent, now_utc
from app.store import store

client = TestClient(app)


def setup_function() -> None:
    store.reset()


def test_agent_task_creation_and_reads_survive_empty_process_maps() -> None:
    response = client.post(
        "/agent/tasks",
        json={"provider": "ANTIGRAVITY", "title": "Review change", "goal": "Check a change"},
    )
    assert response.status_code == 200
    task_id = response.json()["id"]
    event_id = next(iter(store.agent_task_events))

    store.agent_tasks.clear()
    store.agent_task_events.clear()
    store.audit_events.clear()

    detail = client.get(f"/agent/tasks/{task_id}")
    event_list = client.get(f"/agent/tasks/{task_id}/events")
    task_list = client.get("/agent/tasks")

    assert detail.status_code == 200
    assert detail.json()["id"] == task_id
    assert event_list.status_code == 200
    assert event_list.json()[0]["id"] == event_id
    assert task_list.status_code == 200
    assert [item["id"] for item in task_list.json()] == [task_id]


def test_sqlite_task_creation_rolls_back_task_event_and_audit_together() -> None:
    repository = SQLiteAgentTaskRepository(store._connect)
    task = AgentTask(id="task_repo_atomic", title="Atomic", goal="Create atomically")
    first_event = AgentTaskEvent(
        id="event_repo_atomic", task_id=task.id, event_type=AgentTaskEventType.TASK_CREATED
    )
    audit = AuditEvent(
        id="audit_repo_atomic", event_type="agent_task.created", entity_id=task.id
    )
    repository.create(task=task, event=first_event, audit=audit)

    duplicate_task = task.model_copy(update={"id": "task_repo_duplicate"})
    duplicate_event = first_event.model_copy(
        update={"id": "event_repo_duplicate", "task_id": duplicate_task.id}
    )
    with pytest.raises(AgentTaskRepositoryConflict):
        repository.create(
            task=duplicate_task,
            event=duplicate_event,
            audit=audit.model_copy(update={"entity_id": duplicate_task.id}),
        )

    with store._connect() as conn:
        counts = {
            entity_type: conn.execute(
                "SELECT COUNT(*) FROM kv_store WHERE entity_type=?", (entity_type,)
            ).fetchone()[0]
            for entity_type in ("agent_task", "agent_task_event", "audit_event")
        }
    assert counts == {"agent_task": 1, "agent_task_event": 1, "audit_event": 1}


def test_sqlite_list_for_approval_reads_durable_tasks_without_map_state() -> None:
    repository = SQLiteAgentTaskRepository(store._connect)
    task = AgentTask(
        id="task_for_approval_lookup",
        approval_request_id="approval_for_task_lookup",
        title="Linked task",
        goal="Verify durable relationship lookup",
    )
    event = AgentTaskEvent(
        id="event_for_approval_lookup",
        task_id=task.id,
        event_type=AgentTaskEventType.TASK_CREATED,
    )
    audit = AuditEvent(
        id="audit_for_approval_lookup",
        event_type="agent_task.created",
        entity_id=task.id,
    )
    repository.create(task=task, event=event, audit=audit)
    store.agent_tasks.clear()

    assert repository.list_for_approval("approval_for_task_lookup") == [task]
    assert repository.list_for_approval("another_approval") == []


def test_sqlite_repository_enforces_bounded_list_limit() -> None:
    repository = SQLiteAgentTaskRepository(store._connect)
    with pytest.raises(ValueError, match="between 1 and 500"):
        repository.list(limit=501)


def test_sqlite_worker_outcome_releases_lease_and_writes_event_audit_atomically() -> None:
    repository = SQLiteAgentTaskRepository(store._connect)
    task = AgentTask(
        id="task_worker_outcome", title="Worker outcome", goal="Commit one bounded result",
        status=AgentTaskStatus.APPROVED, workspace_id="workspace_worker_outcome",
        project_id="project_worker_outcome", approval_request_id="approval_worker_outcome",
        worker_lease_owner="branch-worker", worker_lease_token="lease-worker-outcome",
        worker_lease_expires_at=now_utc() + timedelta(minutes=1),
    )
    with store._connect() as connection:
        connection.execute(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            ("agent_task", task.id, task.model_dump_json()),
        )
    event = AgentTaskEvent(
        id="event_worker_outcome", task_id=task.id,
        event_type=AgentTaskEventType.NOTE, status=task.status,
        actor="branch-worker", message="Branch created",
    )
    audit = AuditEvent(
        id="audit_worker_outcome", event_type="agent_worker.github_branch_created",
        entity_id=task.id, workspace_id=task.workspace_id, actor="branch-worker",
        details={"event_id": event.id},
    )
    updated = task.model_copy(update={
        "worker_lease_owner": None, "worker_lease_token": None,
        "worker_lease_expires_at": None,
    })

    repository.commit_worker_outcome(
        expected=task, updated=updated, event=event, audit=audit,
    )

    assert repository.get(task.id) == updated
    assert repository.list_events(task.id) == [event]
    with store._connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type='audit_event' AND entity_id=?",
            (audit.id,),
        ).fetchone()[0] == 1


def test_sqlite_worker_outcome_rolls_back_when_audit_insert_conflicts() -> None:
    repository = SQLiteAgentTaskRepository(store._connect)
    task = AgentTask(
        id="task_worker_outcome_rollback", title="Worker outcome", goal="Keep lease on rollback",
        status=AgentTaskStatus.APPROVED, workspace_id="workspace_worker_outcome",
        project_id="project_worker_outcome", approval_request_id="approval_worker_outcome",
        worker_lease_owner="branch-worker", worker_lease_token="lease-worker-outcome",
        worker_lease_expires_at=now_utc() + timedelta(minutes=1),
    )
    with store._connect() as connection:
        connection.execute(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            ("agent_task", task.id, task.model_dump_json()),
        )
        connection.execute(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            ("audit_event", "audit_worker_outcome_conflict", AuditEvent(
                id="audit_worker_outcome_conflict", event_type="agent_worker.existing",
                entity_id=task.id, workspace_id=task.workspace_id,
            ).model_dump_json()),
        )
    event = AgentTaskEvent(
        id="event_worker_outcome_rollback", task_id=task.id,
        event_type=AgentTaskEventType.NOTE, status=task.status, actor="branch-worker",
    )
    audit = AuditEvent(
        id="audit_worker_outcome_conflict", event_type="agent_worker.github_branch_created",
        entity_id=task.id, workspace_id=task.workspace_id, actor="branch-worker",
        details={"event_id": event.id},
    )
    updated = task.model_copy(update={
        "worker_lease_owner": None, "worker_lease_token": None,
        "worker_lease_expires_at": None,
    })

    with pytest.raises(AgentTaskRepositoryConflict, match="outcome record already exists"):
        repository.commit_worker_outcome(
            expected=task, updated=updated, event=event, audit=audit,
        )

    assert repository.get(task.id) == task
    assert repository.list_events(task.id) == []
