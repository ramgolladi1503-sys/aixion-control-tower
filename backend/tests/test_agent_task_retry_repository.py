from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import psycopg
import pytest
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app.agent_task_models import AgentTask, AgentTaskEvent, AgentTaskEventType, AgentTaskStatus
from app.agent_task_repository import (
    AgentTaskRepositoryConflict,
    PostgresAgentTaskRepository,
    SQLiteAgentTaskRepository,
)
from app.agent_task_retry import append_retry_requested_event, audit_retry_requested
from app.approval_integrity import compute_approval_payload_hash
from app.models import ApprovalRequest, ApprovalStatus, AuditEvent, Project, RiskAssessment, RiskLevel, Workspace
from app.postgres_database import run_postgres_migrations
from app.store import store


@pytest.fixture
def postgres_agent_tasks() -> Iterator[tuple[PostgresAgentTaskRepository, Callable[[], Connection]]]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL integration tests")
    schema = f"ct_task_retry_{uuid.uuid4().hex}"
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


def _retry_records(task: AgentTask, retry_count: int, max_retries: int):
    updated = task.model_copy(deep=True)
    event = append_retry_requested_event(
        updated,
        actor="reviewer@example.test",
        reason="retry after transient failure",
        retry_count=retry_count,
        max_retries=max_retries,
    )
    audit = audit_retry_requested(updated, actor="reviewer@example.test", event=event)
    return updated, event, audit


def _task_records(task: AgentTask, *, workspace_id: str | None = None):
    event = AgentTaskEvent(task_id=task.id, event_type=AgentTaskEventType.TASK_CREATED)
    audit = AuditEvent(
        event_type="agent_task.created", entity_id=task.id, workspace_id=workspace_id,
    )
    return event, audit


def test_sqlite_retry_is_atomic_and_limit_is_rechecked_under_lock() -> None:
    store.reset()
    repository = SQLiteAgentTaskRepository(store._connect)
    workspace = Workspace(
        id="workspace_retry_atomic", name="Retry workspace", owner_user_id="owner"
    )
    project = Project(
        id="project_retry_atomic", name="Retry project", workspace_id=workspace.id
    )
    approval = ApprovalRequest(
        id="approval_retry_atomic", project_id=project.id, workspace_id=workspace.id,
        title="Approved work", summary="Safe retry target", target_branch="feature/retry",
        files=[], risk=RiskAssessment(level=RiskLevel.LOW), status=ApprovalStatus.APPROVED,
    )
    approval.approved_payload_hash = compute_approval_payload_hash(approval)
    store.workspaces[workspace.id] = workspace
    store.projects[project.id] = project
    store.approval_requests[approval.id] = approval
    store.persist()
    task = AgentTask(
        id="task_retry_atomic", project_id=project.id, workspace_id=workspace.id,
        title="Retry", goal="Retry atomically", status=AgentTaskStatus.FAILED,
        approval_request_id=approval.id,
    )
    created_event, created_audit = _task_records(task, workspace_id=workspace.id)
    repository.create(task=task, event=created_event, audit=created_audit)
    expected = repository.get(task.id)
    assert expected is not None

    failed_update, failed_event, failed_audit = _retry_records(expected, 0, 3)
    failed_audit.id = created_audit.id
    with pytest.raises(AgentTaskRepositoryConflict, match="record already exists"):
        repository.commit_retry(
            expected=expected, updated=failed_update, event=failed_event,
            audit=failed_audit, max_retries=3,
        )
    assert repository.get(task.id) == expected
    assert repository.count_retries(task.id) == 0

    updated, event, audit = _retry_records(expected, 0, 3)
    repository.commit_retry(
        expected=expected, updated=updated, event=event, audit=audit, max_retries=3,
    )
    store.project_agent_task_transition(updated, event, audit)
    assert repository.get(task.id) == updated
    assert repository.count_retries(task.id) == 1

    stale_update, stale_event, stale_audit = _retry_records(expected, 0, 3)
    with pytest.raises(AgentTaskRepositoryConflict, match="changed before retry"):
        repository.commit_retry(
            expected=expected, updated=stale_update, event=stale_event,
            audit=stale_audit, max_retries=3,
        )

    failed_again = updated.model_copy(update={"status": AgentTaskStatus.FAILED})
    store.agent_tasks[task.id] = failed_again
    store.persist()
    limit_expected = repository.get(task.id)
    assert limit_expected is not None
    over_limit_update, over_limit_event, over_limit_audit = _retry_records(limit_expected, 1, 1)
    with pytest.raises(AgentTaskRepositoryConflict, match="Retry limit reached: 1 >= 1"):
        repository.commit_retry(
            expected=limit_expected, updated=over_limit_update,
            event=over_limit_event, audit=over_limit_audit, max_retries=1,
        )
    assert repository.get(task.id) == limit_expected
    assert repository.count_retries(task.id) == 1

    race_expected = repository.get(task.id)
    assert race_expected is not None
    barrier = Barrier(2)

    def attempt_sqlite_retry() -> str:
        race_update, race_event, race_audit = _retry_records(race_expected, 1, 2)
        barrier.wait()
        try:
            repository.commit_retry(
                expected=race_expected, updated=race_update, event=race_event,
                audit=race_audit, max_retries=2,
            )
            return "committed"
        except AgentTaskRepositoryConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: attempt_sqlite_retry(), range(2)))
    assert sorted(results) == ["committed", "conflict"]
    assert repository.count_retries(task.id) == 2


def test_sqlite_retry_requires_current_approval_and_matching_scope() -> None:
    store.reset()
    workspace = Workspace(
        id="workspace_retry_authority", name="Retry workspace", owner_user_id="owner"
    )
    project = Project(
        id="project_retry_authority", name="Retry project", workspace_id=workspace.id
    )
    approval = ApprovalRequest(
        id="approval_retry_authority", project_id=project.id, workspace_id=workspace.id,
        title="Previously approved work", summary="Retry authority check",
        target_branch="feature/retry", files=[], risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.FAILED,
    )
    approval.approved_payload_hash = compute_approval_payload_hash(approval)
    store.workspaces[workspace.id] = workspace
    store.projects[project.id] = project
    store.approval_requests[approval.id] = approval
    store.persist()
    repository = SQLiteAgentTaskRepository(store._connect)
    task = AgentTask(
        id="task_retry_authority", project_id=project.id, workspace_id=workspace.id,
        title="Retry", goal="Require a still-approved action",
        status=AgentTaskStatus.FAILED, approval_request_id=approval.id,
    )
    created_event, created_audit = _task_records(task, workspace_id=workspace.id)
    repository.create(task=task, event=created_event, audit=created_audit)
    expected = repository.get(task.id)
    assert expected is not None
    updated, event, audit = _retry_records(expected, 0, 3)

    with pytest.raises(AgentTaskRepositoryConflict, match="Linked approval must remain approved"):
        repository.commit_retry(
            expected=expected, updated=updated, event=event, audit=audit, max_retries=3,
        )

    assert repository.get(task.id) == expected
    assert repository.count_retries(task.id) == 0
    assert repository.list_events(task.id) == [created_event]

    mismatched_approval = approval.model_copy(
        update={"status": ApprovalStatus.APPROVED, "workspace_id": "other_workspace"}
    )
    with store._connect() as conn:
        conn.execute(
            "UPDATE kv_store SET payload=? WHERE entity_type='approval_request' AND entity_id=?",
            (mismatched_approval.model_dump_json(), approval.id),
        )
    with pytest.raises(AgentTaskRepositoryConflict, match="Linked approval must remain approved"):
        repository.commit_retry(
            expected=expected, updated=updated, event=event, audit=audit, max_retries=3,
        )
    assert repository.get(task.id) == expected
    assert repository.count_retries(task.id) == 0


def test_postgres_retry_commits_scope_and_rechecks_retry_limit(postgres_agent_tasks) -> None:
    repository, connect = postgres_agent_tasks
    suffix = uuid.uuid4().hex
    workspace = Workspace(id=f"workspace_{suffix}", name="Retry workspace", owner_user_id="owner")
    project = Project(id=f"project_{suffix}", name="Retry project", workspace_id=workspace.id)
    approval = ApprovalRequest(
        id=f"approval_{suffix}", project_id=project.id, workspace_id=workspace.id,
        title="Approved work", summary="Safe retry target", target_branch="feature/retry",
        files=[], risk=RiskAssessment(level=RiskLevel.LOW), status=ApprovalStatus.APPROVED,
    )
    approval.approved_payload_hash = compute_approval_payload_hash(approval)
    task = AgentTask(
        id=f"task_{suffix}", project_id=project.id, workspace_id=workspace.id,
        approval_request_id=approval.id, title="Retry", goal="Retry atomically",
        status=AgentTaskStatus.FAILED,
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
            (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
        )
        conn.execute(
            "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
            (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))),
        )
        conn.execute(
            "INSERT INTO approval_requests(approval_id,project_id,workspace_id,status,created_at,updated_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (approval.id, project.id, workspace.id, approval.status.value,
             approval.created_at, approval.updated_at, Jsonb(approval.model_dump(mode="json"))),
        )
    created_event, created_audit = _task_records(task, workspace_id=workspace.id)
    repository.create(task=task, event=created_event, audit=created_audit)
    expected = repository.get(task.id)
    assert expected is not None

    failed_update, failed_event, failed_audit = _retry_records(expected, 0, 3)
    failed_audit.id = created_audit.id
    with pytest.raises(AgentTaskRepositoryConflict, match="record already exists"):
        repository.commit_retry(
            expected=expected, updated=failed_update, event=failed_event,
            audit=failed_audit, max_retries=3,
        )
    assert repository.get(task.id) == expected
    assert repository.count_retries(task.id) == 0

    updated, event, audit = _retry_records(expected, 0, 3)
    repository.commit_retry(
        expected=expected, updated=updated, event=event, audit=audit, max_retries=3,
    )
    assert repository.get(task.id) == updated
    assert repository.count_retries(task.id) == 1
    assert repository.list_events(task.id, workspace_id="other_workspace") == []

    with connect() as conn:
        failed_again = updated.model_copy(update={"status": AgentTaskStatus.FAILED})
        conn.execute(
            "UPDATE agent_tasks SET status=%s,payload=%s WHERE task_id=%s AND workspace_id=%s",
            (failed_again.status.value, Jsonb(failed_again.model_dump(mode="json")),
             task.id, workspace.id),
        )
    limit_expected = repository.get(task.id)
    assert limit_expected is not None
    over_limit_update, over_limit_event, over_limit_audit = _retry_records(limit_expected, 1, 1)
    with pytest.raises(AgentTaskRepositoryConflict, match="Retry limit reached: 1 >= 1"):
        repository.commit_retry(
            expected=limit_expected, updated=over_limit_update,
            event=over_limit_event, audit=over_limit_audit, max_retries=1,
        )
    assert repository.get(task.id) == limit_expected
    assert repository.count_retries(task.id) == 1

    race_expected = repository.get(task.id)
    assert race_expected is not None
    barrier = Barrier(2)

    def attempt_postgres_retry() -> str:
        race_update, race_event, race_audit = _retry_records(race_expected, 1, 2)
        barrier.wait()
        try:
            repository.commit_retry(
                expected=race_expected, updated=race_update, event=race_event,
                audit=race_audit, max_retries=2,
            )
            return "committed"
        except AgentTaskRepositoryConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: attempt_postgres_retry(), range(2)))
    assert sorted(results) == ["committed", "conflict"]
    assert repository.count_retries(task.id) == 2

    # A stale task status alone is insufficient: its linked approval must still
    # be approved in the same authority before a new retry can be committed.
    with connect() as conn:
        durable_task = repository.get(task.id)
        assert durable_task is not None
        failed_again = durable_task.model_copy(update={"status": AgentTaskStatus.FAILED})
        failed_approval = approval.model_copy(update={"status": ApprovalStatus.FAILED})
        conn.execute(
            "UPDATE agent_tasks SET status=%s,payload=%s WHERE task_id=%s AND workspace_id=%s",
            (failed_again.status.value, Jsonb(failed_again.model_dump(mode="json")),
             task.id, workspace.id),
        )
        conn.execute(
            "UPDATE approval_requests SET status=%s,payload=%s WHERE approval_id=%s AND workspace_id=%s",
            (failed_approval.status.value, Jsonb(failed_approval.model_dump(mode="json")),
             approval.id, workspace.id),
        )
    expected = repository.get(task.id)
    assert expected is not None
    rejected_update, rejected_event, rejected_audit = _retry_records(expected, 2, 3)
    with pytest.raises(AgentTaskRepositoryConflict, match="Linked approval must remain approved"):
        repository.commit_retry(
            expected=expected, updated=rejected_update, event=rejected_event,
            audit=rejected_audit, max_retries=3,
        )
    assert repository.get(task.id) == expected
    assert repository.count_retries(task.id) == 2
