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
from app.agent_task_repository import AgentTaskRepositoryConflict, PostgresAgentTaskRepository
from app.models import (
    ApprovalRequest, ApprovalStatus, AuditEvent, FileChange, Project,
    RiskAssessment, RiskLevel, Workspace,
)
from app.postgres_database import run_postgres_migrations


@pytest.fixture
def agent_task_repository() -> Iterator[tuple[PostgresAgentTaskRepository, Callable[[], Connection]]]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL integration tests")
    schema = f"ct_agent_task_{uuid.uuid4().hex}"
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


def _seed_project(connect: Callable[[], Connection]) -> tuple[Workspace, Project]:
    suffix = uuid.uuid4().hex
    workspace = Workspace(id=f"workspace_{suffix}", name="AgentTask workspace", owner_user_id="owner")
    project = Project(id=f"project_{suffix}", name="AgentTask project", workspace_id=workspace.id)
    with connect() as conn:
        conn.execute(
            "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
            (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
        )
        conn.execute(
            "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
            (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))),
        )
    return workspace, project


def _seed_approved_task(connect: Callable[[], Connection]) -> AgentTask:
    suffix = uuid.uuid4().hex
    workspace = Workspace(id=f"workspace_{suffix}", name="Claim workspace", owner_user_id="owner")
    project = Project(id=f"project_{suffix}", name="Claim project", workspace_id=workspace.id)
    approval = ApprovalRequest(
        id=f"approval_{suffix}", project_id=project.id, workspace_id=workspace.id,
        title="Approved work", summary="Authorized worker claim.", target_branch="main",
        files=[FileChange(path="README.md", diff="+safe")],
        risk=RiskAssessment(level=RiskLevel.LOW), status=ApprovalStatus.APPROVED,
    )
    task = AgentTask(
        id=f"task_{suffix}", project_id=project.id, workspace_id=workspace.id,
        approval_request_id=approval.id, title="Approved work", goal="Run after approval",
        status=AgentTaskStatus.APPROVED,
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
        conn.execute(
            "INSERT INTO agent_tasks(task_id,project_id,workspace_id,approval_request_id,status,updated_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (task.id, project.id, workspace.id, approval.id, task.status.value,
             task.updated_at, Jsonb(task.model_dump(mode="json"))),
        )
    return task


def test_postgres_agent_task_create_and_workspace_scoped_reads(agent_task_repository) -> None:
    repository, connect = agent_task_repository
    workspace, project = _seed_project(connect)
    task = AgentTask(
        id=f"task_{uuid.uuid4().hex}", project_id=project.id, workspace_id=workspace.id,
        title="Review", goal="Review safely",
    )
    event = AgentTaskEvent(
        id=f"event_{uuid.uuid4().hex}", task_id=task.id,
        event_type=AgentTaskEventType.TASK_CREATED,
    )
    audit = AuditEvent(
        id=f"audit_{uuid.uuid4().hex}", event_type="agent_task.created",
        entity_id=task.id, workspace_id=workspace.id,
    )

    repository.create(task=task, event=event, audit=audit)

    assert repository.get(task.id) == task
    assert repository.list(workspace_id=workspace.id, project_id=project.id) == [task]
    assert repository.list(workspace_id="another_workspace") == []
    assert repository.list_events(task.id, workspace_id=workspace.id) == [event]
    assert repository.list_events(task.id, workspace_id="another_workspace") == []


def test_postgres_worker_outcome_releases_lease_and_commits_event_audit(agent_task_repository) -> None:
    repository, connect = agent_task_repository
    task = _seed_approved_task(connect)
    claimed, reason, _ = repository.claim(
        task_id=task.id, worker_id="branch-worker", lease_seconds=60,
    )
    assert reason == ""
    assert claimed is not None
    event = AgentTaskEvent(
        id=f"event_{uuid.uuid4().hex}", task_id=task.id,
        event_type=AgentTaskEventType.PR_CREATED, status=AgentTaskStatus.READY_FOR_PR,
        actor="branch-worker", message="Pull request creation recorded",
    )
    audit = AuditEvent(
        id=f"audit_{uuid.uuid4().hex}",
        event_type="agent_worker.github_pr_created",
        entity_id=task.id, workspace_id=task.workspace_id, actor="branch-worker",
        details={"event_id": event.id},
    )
    updated = claimed.model_copy(update={
        "status": AgentTaskStatus.READY_FOR_PR,
        "worker_lease_owner": None, "worker_lease_token": None,
        "worker_lease_expires_at": None,
    })

    repository.commit_worker_outcome(
        expected=claimed, updated=updated, event=event, audit=audit,
    )

    assert repository.get(task.id) == updated
    assert repository.list_events(task.id, workspace_id=task.workspace_id) == [event]
    with connect() as connection:
        row = connection.execute(
            "SELECT event_id FROM audit_events WHERE event_id=%s AND workspace_id=%s",
            (audit.id, task.workspace_id),
        ).fetchone()
    assert row == (audit.id,)


def test_postgres_worker_outcome_rolls_back_task_and_event_on_audit_conflict(agent_task_repository) -> None:
    repository, connect = agent_task_repository
    task = _seed_approved_task(connect)
    claimed, reason, _ = repository.claim(
        task_id=task.id, worker_id="branch-worker", lease_seconds=60,
    )
    assert reason == ""
    assert claimed is not None
    conflict_audit = AuditEvent(
        id="audit_worker_outcome_conflict",
        event_type="agent_worker.existing", entity_id=task.id,
        workspace_id=task.workspace_id,
    )
    with connect() as connection:
        connection.execute(
            "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,event_type,"
            "actor,details,created_at,payload) VALUES(%s,%s,NULL,'agent_task',%s,%s,%s,%s,%s,%s)",
            (conflict_audit.id, task.workspace_id, task.id, conflict_audit.event_type,
             conflict_audit.actor, Jsonb(conflict_audit.details), conflict_audit.created_at,
             Jsonb(conflict_audit.model_dump(mode="json"))),
        )
    event = AgentTaskEvent(
        id=f"event_{uuid.uuid4().hex}", task_id=task.id,
        event_type=AgentTaskEventType.NOTE, status=claimed.status,
        actor="branch-worker", message="Must roll back",
    )
    attempted_audit = conflict_audit.model_copy(update={
        "event_type": "agent_worker.github_branch_created",
        "actor": "branch-worker",
        "details": {"event_id": event.id},
    })
    updated = claimed.model_copy(update={
        "worker_lease_owner": None, "worker_lease_token": None,
        "worker_lease_expires_at": None,
    })

    with pytest.raises(AgentTaskRepositoryConflict, match="outcome record already exists"):
        repository.commit_worker_outcome(
            expected=claimed, updated=updated, event=event, audit=attempted_audit,
        )

    assert repository.get(task.id) == claimed
    assert repository.list_events(task.id, workspace_id=task.workspace_id) == []


def test_postgres_agent_task_creation_rolls_back_on_duplicate_audit_id(agent_task_repository) -> None:
    repository, connect = agent_task_repository
    workspace, project = _seed_project(connect)
    task_id = f"task_{uuid.uuid4().hex}"

    task = AgentTask(id=task_id, project_id=project.id, workspace_id=workspace.id, title="Review", goal="Review")
    event = AgentTaskEvent(
        id=f"event_{uuid.uuid4().hex}", task_id=task_id,
        event_type=AgentTaskEventType.TASK_CREATED,
    )
    audit = AuditEvent(
        id="audit_duplicate", event_type="agent_task.created",
        entity_id=task_id, workspace_id=workspace.id,
    )
    repository.create(task=task, event=event, audit=audit)

    second_task = AgentTask(
        id=f"task_{uuid.uuid4().hex}", project_id=project.id,
        workspace_id=workspace.id, title="Second", goal="Second",
    )
    second_event = AgentTaskEvent(
        id=f"event_{uuid.uuid4().hex}", task_id=second_task.id,
        event_type=AgentTaskEventType.TASK_CREATED,
    )
    duplicate_audit = audit.model_copy(update={"entity_id": second_task.id})
    with pytest.raises(AgentTaskRepositoryConflict):
        repository.create(task=second_task, event=second_event, audit=duplicate_audit)

    assert repository.get(task_id) is not None
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_tasks WHERE task_id=%s", (task_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM agent_task_events WHERE task_id=%s", (task_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM audit_events WHERE entity_id=%s", (task_id,)).fetchone()[0] == 1


def test_postgres_worker_claim_persists_lease_and_serializes_competing_workers(agent_task_repository) -> None:
    repository, connect = agent_task_repository
    task = _seed_approved_task(connect)
    assert repository.list_for_approval(task.approval_request_id or "") == [task]
    assert repository.list_for_approval("missing_approval") == []
    barrier = Barrier(2)

    def claim(worker_id: str):
        barrier.wait()
        return repository.claim(task_id=task.id, worker_id=worker_id, lease_seconds=60)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ("worker-a", "worker-b")))

    successes = [claimed for claimed, reason, _ in results if claimed is not None and not reason]
    assert len(successes) == 1
    persisted = repository.get(task.id)
    assert persisted is not None
    assert persisted.worker_lease_owner in {"worker-a", "worker-b"}
    assert persisted.worker_lease_token == successes[0].worker_lease_token
    assert persisted.worker_lease_expires_at is not None
    with connect() as conn:
        audit_rows = conn.execute(
            "SELECT payload FROM audit_events WHERE entity_type='agent_task' "
            "AND entity_id=%s AND event_type='agent_worker.task_claimed'",
            (task.id,),
        ).fetchall()
    assert len(audit_rows) == 1
    assert "lease_token" not in audit_rows[0][0]["details"]
