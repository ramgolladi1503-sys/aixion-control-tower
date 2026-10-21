from __future__ import annotations

import os

import pytest

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from app.audit_repository import (
    AuditEventRepositoryIntegrityError,
    SQLiteAuditEventRepository,
)
from app.models import AuditEvent, Project
from app.store import store


def setup_function() -> None:
    store.reset()


def test_sqlite_audit_repository_is_workspace_scoped_and_reads_durable_rows() -> None:
    workspace_a = "workspace-a"
    workspace_b = "workspace-b"
    event_a = AuditEvent(
        event_type="approval.decision",
        entity_id="approval-a",
        workspace_id=workspace_a,
        details={"decision": "approve", "request_id": "a"},
    )
    event_b = AuditEvent(
        event_type="approval.decision",
        entity_id="approval-b",
        workspace_id=workspace_b,
        details={"decision": "deny", "request_id": "b"},
    )
    store.audit_events.extend((event_a, event_b))
    store.persist()
    store.audit_events.clear()

    repository = SQLiteAuditEventRepository(store._connect)
    assert repository.list_for_workspace(workspace_a) == [event_a]
    assert repository.get_for_workspace(event_a.id, workspace_a) == event_a
    assert repository.get_for_workspace(event_a.id, workspace_b) is None


def test_sqlite_audit_repository_filters_and_rejects_bad_limits() -> None:
    first = AuditEvent(
        event_type="agent_task.run_scheduled",
        entity_id="task-1",
        workspace_id="workspace-a",
        actor="scheduler",
        details={"agent_run_id": "run-1", "status": "SCHEDULED"},
    )
    second = AuditEvent(
        event_type="agent_task.run_scheduled",
        entity_id="task-1",
        workspace_id="workspace-a",
        actor="scheduler",
        details={"agent_run_id": "run-2", "status": "SCHEDULED"},
    )
    store.audit_events.extend((first, second))
    store.persist()
    repository = SQLiteAuditEventRepository(store._connect)

    assert repository.list_for_workspace(
        "workspace-a",
        event_type="agent_task.run_scheduled",
        detail_key="agent_run_id",
        detail_value="run-1",
    ) == [first]
    with pytest.raises(ValueError, match="between 1 and 1001"):
        repository.list_for_workspace("workspace-a", limit=0)


def test_sqlite_audit_repository_rejects_payload_identity_mismatch() -> None:
    event = AuditEvent(event_type="approval.created", entity_id="approval-1")
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            ("audit_event", "different-id", event.model_dump_json()),
        )
    repository = SQLiteAuditEventRepository(store._connect)

    with pytest.raises(AuditEventRepositoryIntegrityError, match="indexed identity"):
        repository.get_for_workspace("different-id", "")


def test_legacy_audit_workspace_resolution_uses_durable_project_row() -> None:
    project = Project(name="Legacy audit scope", workspace_id="workspace-legacy")
    event = AuditEvent(
        event_type="approval.created",
        entity_id="approval-legacy",
        details={"project_id": project.id},
    )
    store.projects[project.id] = project
    store.audit_events.append(event)
    store.persist()
    store.projects.clear()
    store.audit_events.clear()

    repository = SQLiteAuditEventRepository(store._connect)
    assert repository.list_for_workspace("workspace-legacy") == [event]
    assert repository.get_for_workspace(event.id, "workspace-legacy") == event
