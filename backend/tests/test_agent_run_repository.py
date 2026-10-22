from __future__ import annotations

import os
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import psycopg
import pytest
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app.agent_run_models import (
    AgentRun,
    AgentRunEvent,
    AgentRunEventType,
    AgentRunStatus,
    AgentRunStep,
    AgentRunStepStatus,
    AgentRunStepType,
)
from app.agent_run_repository import (
    AgentRunRepositoryConflict,
    AgentRunRepositoryIntegrityError,
    PostgresAgentRunRepository,
    SQLiteAgentRunRepository,
)
from app.database_migrations import run_migrations
from app.agent_task_models import AgentTask, AgentTaskStatus
from app.approval_integrity import compute_approval_payload_hash
from app.models import (
    ApprovalRequest,
    ApprovalStatus,
    AuditEvent,
    FileChange,
    Project,
    RiskAssessment,
    RiskLevel,
    Workspace,
)
from app.postgres_database import run_postgres_migrations
from app.agent_run_supervisor import (
    DefaultAgentRunStepExecutor,
    acquire_run_lease,
    heartbeat_run,
    recover_stale_leases,
    resume_agent_run,
)
from app.agent_run_state_machine import transition_run, transition_step
from app.application_repositories import ApplicationRepositories


def test_sqlite_get_reads_durable_run_and_rejects_identity_mismatch(tmp_path: Path) -> None:
    database = tmp_path / "agent_run_repository.sqlite3"

    def connect() -> sqlite3.Connection:
        return sqlite3.connect(database)

    with connect() as connection:
        run_migrations(connection)
    repository = SQLiteAgentRunRepository(connect)
    run = AgentRun(id="run_durable_test", task_id="task_durable_test")
    with connect() as connection:
        connection.execute(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            ("agent_run", run.id, run.model_dump_json()),
        )

    assert repository.get(run.id) == run
    assert repository.get("missing_run") is None
    assert repository.list_for_task(run.task_id) == [run]
    assert repository.list_for_task("missing_task") == []

    mismatched = run.model_copy(update={"id": "different_run"})
    with connect() as connection:
        connection.execute(
            "UPDATE kv_store SET payload=? WHERE entity_type='agent_run' AND entity_id=?",
            (mismatched.model_dump_json(), run.id),
        )
    with pytest.raises(AgentRunRepositoryIntegrityError, match="identity or scope"):
        repository.get(run.id)


def test_sqlite_create_commits_run_steps_events_and_audit_atomically(tmp_path: Path) -> None:
    database = tmp_path / "agent_run_create.sqlite3"

    def connect() -> sqlite3.Connection:
        return sqlite3.connect(database)

    with connect() as connection:
        run_migrations(connection)
    repository = SQLiteAgentRunRepository(connect)
    workspace = Workspace(
        id="workspace_agent_run_create",
        name="Run workspace",
        owner_user_id="owner_agent_run_create",
    )
    project = Project(
        id="project_agent_run_create",
        name="Run project",
        workspace_id=workspace.id,
    )
    approval = ApprovalRequest(
        id="approval_agent_run_create",
        project_id=project.id,
        workspace_id=workspace.id,
        title="Approved run",
        summary="Create a durable run.",
        target_branch="feature/run-create",
        files=[FileChange(path="README.md", diff="+run")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.APPROVED,
    )
    approval.approved_payload_hash = compute_approval_payload_hash(approval)
    task = AgentTask(
        id="task_agent_run_create",
        project_id=project.id,
        workspace_id=workspace.id,
        approval_request_id=approval.id,
        title="Approved task",
        goal="Run after approval",
        repository="owner/repository",
        branch_preference="feature/run-repository",
        status=AgentTaskStatus.APPROVED,
    )
    run = AgentRun(
        id="run_agent_run_create",
        task_id=task.id,
        project_id=project.id,
        workspace_id=workspace.id,
        approval_request_id=approval.id,
    )
    step = AgentRunStep(
        id="step_agent_run_create",
        run_id=run.id,
        task_id=task.id,
        sequence=0,
        step_type=AgentRunStepType.VALIDATE_SCOPE,
        status="READY",
        idempotency_key=f"{run.id}:0",
    )
    run.current_step_id = step.id
    event = AgentRunEvent(
        id="event_agent_run_create",
        run_id=run.id,
        task_id=task.id,
        step_id=step.id,
        correlation_id=run.correlation_id,
        event_type=AgentRunEventType.RUN_CREATED,
    )
    audit = AuditEvent(
        id="audit_agent_run_create",
        event_type="agent_run.created",
        entity_id=run.id,
        workspace_id=workspace.id,
    )
    with connect() as connection:
        connection.executemany(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            [
                ("workspace", workspace.id, workspace.model_dump_json()),
                ("project", project.id, project.model_dump_json()),
                ("approval_request", approval.id, approval.model_dump_json()),
                ("agent_task", task.id, task.model_dump_json()),
            ],
        )

    repository.create(run=run, steps=[step], events=[event], audit=audit)

    assert repository.list_for_workspace(
        workspace_id=workspace.id, task_id=task.id, limit=10
    ) == [run]
    assert repository.list_for_workspace(workspace_id="another_workspace") == []
    assert repository.list_steps_for_run(run.id) == [step]
    assert repository.list_steps_for_runs([run]) == [step]
    assert repository.list_steps_for_runs([]) == []
    assert repository.list_events_for_run(run.id) == [event]

    with connect() as connection:
        stored_types = connection.execute(
            "SELECT entity_type,COUNT(*) FROM kv_store WHERE entity_id IN (?, ?, ?, ?) "
            "GROUP BY entity_type ORDER BY entity_type",
            (run.id, step.id, event.id, audit.id),
        ).fetchall()
    assert stored_types == [
        ("agent_run", 1),
        ("agent_run_event", 1),
        ("agent_run_step", 1),
        ("audit_event", 1),
    ]
    assert repository.get_active_for_task(task.id) == run
    with pytest.raises(AgentRunRepositoryConflict, match="active run"):
        repository.create(run=run, steps=[step], events=[event], audit=audit)

    barrier = Barrier(2)

    def claim(worker_id: str) -> tuple[str, str | None]:
        barrier.wait()
        try:
            claimed, _event, _audit = repository.acquire_lease(
                run_id=run.id,
                worker_id=worker_id,
                lease_seconds=30,
                now=datetime.now(timezone.utc),
            )
        except AgentRunRepositoryConflict:
            return worker_id, None
        return worker_id, claimed.lease_token

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, ("worker-a", "worker-b")))
    winners = [(worker_id, token) for worker_id, token in claims if token]
    assert len(winners) == 1
    persisted = repository.get(run.id)
    assert persisted is not None
    assert persisted.lease_owner == winners[0][0]
    assert persisted.lease_token == winners[0][1]
    with pytest.raises(AgentRunRepositoryConflict, match="leased by"):
        repository.acquire_lease(
            run_id=run.id,
            worker_id="other-worker",
            lease_seconds=30,
            now=datetime.now(timezone.utc),
        )


@pytest.fixture
def postgres_agent_run_repository() -> Iterator[tuple[PostgresAgentRunRepository, Callable[[], Connection]]]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is not configured")
    schema = "agent_run_repo_" + uuid.uuid4().hex

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        run_postgres_migrations(connect)
        yield PostgresAgentRunRepository(connect), connect
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _seed_postgres_running_step(
    repository: PostgresAgentRunRepository,
    connect: Callable[[], Connection],
) -> tuple[AgentRun, AgentRunStep]:
    suffix = uuid.uuid4().hex
    workspace = Workspace(
        id=f"workspace_completion_{suffix}",
        name="Completion workspace",
        owner_user_id=f"owner_completion_{suffix}",
    )
    project = Project(
        id=f"project_completion_{suffix}",
        name="Completion project",
        workspace_id=workspace.id,
    )
    approval = ApprovalRequest(
        id=f"approval_completion_{suffix}",
        project_id=project.id,
        workspace_id=workspace.id,
        title="Approved completion test",
        summary="Exercise lease-fenced completion.",
        target_branch="feature/completion",
        files=[FileChange(path="README.md", diff="+approved\n")],
        test_plan=["pytest"],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.APPROVED,
    )
    approval.approved_payload_hash = compute_approval_payload_hash(approval)
    task = AgentTask(
        id=f"task_completion_{suffix}",
        project_id=project.id,
        workspace_id=workspace.id,
        approval_request_id=approval.id,
        title="Approved completion task",
        goal="Test durable lease fencing",
        repository="owner/repository",
        branch_preference="feature/completion",
        status=AgentTaskStatus.APPROVED,
    )
    run = AgentRun(
        id=f"run_completion_{suffix}",
        task_id=task.id,
        project_id=project.id,
        workspace_id=workspace.id,
        approval_request_id=approval.id,
    )
    step = AgentRunStep(
        id=f"step_completion_{suffix}",
        run_id=run.id,
        task_id=task.id,
        sequence=0,
        step_type=AgentRunStepType.VALIDATE_SCOPE,
        status=AgentRunStepStatus.READY,
        idempotency_key=f"{run.id}:0",
    )
    run.current_step_id = step.id
    created_event = AgentRunEvent(
        run_id=run.id,
        task_id=task.id,
        correlation_id=run.correlation_id,
        event_type=AgentRunEventType.RUN_CREATED,
    )
    created_audit = AuditEvent(
        event_type="agent_run.created",
        entity_id=run.id,
        workspace_id=workspace.id,
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
            "INSERT INTO approval_requests(approval_id,project_id,workspace_id,work_order_id,"
            "source_agent_id,status,expires_at,created_at,updated_at,version,payload) "
            "VALUES(%s,%s,%s,NULL,NULL,%s,%s,%s,%s,0,%s)",
            (
                approval.id, project.id, workspace.id, approval.status.value,
                approval.expires_at, approval.created_at, approval.updated_at,
                Jsonb(approval.model_dump(mode="json")),
            ),
        )
        conn.execute(
            "INSERT INTO agent_tasks(task_id,project_id,workspace_id,external_agent_id,"
            "approval_request_id,status,updated_at,payload) VALUES(%s,%s,%s,NULL,%s,%s,%s,%s)",
            (
                task.id, project.id, workspace.id, approval.id, task.status.value,
                task.updated_at, Jsonb(task.model_dump(mode="json")),
            ),
        )
    repository.create(run=run, steps=[step], events=[created_event], audit=created_audit)

    lease_now = datetime.now(timezone.utc)
    leased, lease_event, lease_audit = repository.acquire_lease(
        run_id=run.id,
        worker_id="completion-worker",
        lease_seconds=60,
        now=lease_now,
    )
    assert lease_event is not None and lease_audit is not None
    expected_steps = repository.list_steps_for_run(run.id)
    running_run = leased.model_copy(deep=True)
    transition_run(running_run, AgentRunStatus.RUNNING)
    running_step = expected_steps[0].model_copy(deep=True)
    transition_step(running_step, AgentRunStepStatus.RUNNING)
    running_step.lease_owner = leased.lease_owner
    running_step.lease_token = leased.lease_token
    running_step.lease_expires_at = leased.lease_expires_at
    started_event = AgentRunEvent(
        run_id=run.id,
        task_id=task.id,
        step_id=step.id,
        correlation_id=run.correlation_id,
        event_type=AgentRunEventType.STEP_STARTED,
        previous_status=AgentRunStepStatus.READY.value,
        new_status=AgentRunStepStatus.RUNNING.value,
        actor="completion-worker",
    )
    started_audit = AuditEvent(
        event_type="agent_run.step_started",
        entity_id=run.id,
        workspace_id=workspace.id,
        actor="completion-worker",
        details={"step_id": step.id, "event_ids": [started_event.id]},
    )
    repository.commit_control_transition(
        expected_run=leased,
        updated_run=running_run,
        expected_steps=expected_steps,
        updated_steps=[running_step],
        events=[started_event],
        audits=[started_audit],
    )
    return running_run, running_step


def _commit_test_step_success(
    repository: PostgresAgentRunRepository,
    run: AgentRun,
    step: AgentRunStep,
    *,
    now: datetime,
) -> None:
    updated_run = run.model_copy(deep=True)
    transition_run(updated_run, AgentRunStatus.SUCCEEDED)
    updated_step = step.model_copy(deep=True)
    transition_step(updated_step, AgentRunStepStatus.SUCCEEDED)
    event = AgentRunEvent(
        run_id=run.id,
        task_id=run.task_id,
        step_id=step.id,
        correlation_id=run.correlation_id,
        event_type=AgentRunEventType.STEP_SUCCEEDED,
        previous_status=step.status.value,
        new_status=AgentRunStepStatus.SUCCEEDED.value,
        actor="completion-worker",
    )
    audit = AuditEvent(
        event_type="agent_run.step_completed",
        entity_id=run.id,
        workspace_id=run.workspace_id,
        actor="completion-worker",
        details={"step_id": step.id, "event_ids": [event.id]},
    )
    repository.commit_step_completion(
        expected_run=run,
        updated_run=updated_run,
        expected_steps=[step],
        updated_steps=[updated_step],
        events=[event],
        audit=audit,
        now=now,
    )


def test_postgres_step_completion_accepts_active_matching_lease(
    postgres_agent_run_repository,
) -> None:
    repository, connect = postgres_agent_run_repository
    run, step = _seed_postgres_running_step(repository, connect)

    _commit_test_step_success(repository, run, step, now=datetime.now(timezone.utc))

    completed = repository.get(run.id)
    assert completed is not None and completed.status == AgentRunStatus.SUCCEEDED
    assert repository.list_steps_for_run(run.id)[0].status == AgentRunStepStatus.SUCCEEDED
    assert any(
        event.event_type == AgentRunEventType.STEP_SUCCEEDED
        for event in repository.list_events_for_run(run.id)
    )


def test_postgres_step_completion_rejects_expired_lease_without_writes(
    postgres_agent_run_repository,
) -> None:
    repository, connect = postgres_agent_run_repository
    run, step = _seed_postgres_running_step(repository, connect)
    assert run.lease_expires_at is not None

    with pytest.raises(AgentRunRepositoryConflict, match="lease expired"):
        _commit_test_step_success(repository, run, step, now=run.lease_expires_at)

    assert repository.get(run.id) == run
    assert repository.list_steps_for_run(run.id) == [step]
    assert not any(
        event.event_type == AgentRunEventType.STEP_SUCCEEDED
        for event in repository.list_events_for_run(run.id)
    )
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE entity_id=%s "
            "AND event_type='agent_run.step_completed'",
            (run.id,),
        ).fetchone()[0] == 0


def test_postgres_step_completion_rejects_superseded_lease_snapshot(
    postgres_agent_run_repository,
) -> None:
    repository, connect = postgres_agent_run_repository
    run, step = _seed_postgres_running_step(repository, connect)
    assert run.lease_expires_at is not None
    takeover_time = run.lease_expires_at + timedelta(seconds=1)
    superseding_run, _event, _audit = repository.acquire_lease(
        run_id=run.id,
        worker_id="replacement-worker",
        lease_seconds=60,
        now=takeover_time,
    )

    with pytest.raises(AgentRunRepositoryConflict, match="changed before step completion"):
        _commit_test_step_success(repository, run, step, now=datetime.now(timezone.utc))

    persisted = repository.get(run.id)
    assert persisted == superseding_run
    assert persisted is not None and persisted.lease_owner == "replacement-worker"
    assert repository.list_steps_for_run(run.id) == [step]
    assert not any(
        event.event_type == AgentRunEventType.STEP_SUCCEEDED
        for event in repository.list_events_for_run(run.id)
    )


def test_postgres_agent_run_create_is_workspace_scoped_and_durable(
    postgres_agent_run_repository: tuple[PostgresAgentRunRepository, Callable[[], Connection]],
) -> None:
    repository, connect = postgres_agent_run_repository
    workspace = Workspace(
        id="workspace_agent_run_repository",
        name="Run workspace",
        owner_user_id="owner_agent_run_repository",
    )
    project = Project(
        id="project_agent_run_repository",
        name="Run project",
        workspace_id=workspace.id,
    )
    approval = ApprovalRequest(
        id="approval_agent_run_repository",
        project_id=project.id,
        workspace_id=workspace.id,
        title="Approved run",
        summary="Create a durable run.",
        target_branch="feature/run-repository",
        files=[FileChange(path="README.md", diff="+run")],
        test_plan=["pytest"],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.APPROVED,
    )
    approval.approved_payload_hash = compute_approval_payload_hash(approval)
    task = AgentTask(
        id="task_agent_run_repository",
        project_id=project.id,
        workspace_id=workspace.id,
        approval_request_id=approval.id,
        title="Approved task",
        goal="Run after approval",
        repository="owner/repository",
        branch_preference="feature/run-repository",
        status=AgentTaskStatus.APPROVED,
    )
    run = AgentRun(
        id="run_agent_run_repository",
        task_id=task.id,
        project_id=project.id,
        workspace_id=workspace.id,
        approval_request_id=approval.id,
    )
    step = AgentRunStep(
        id="step_agent_run_repository",
        run_id=run.id,
        task_id=task.id,
        sequence=0,
        step_type=AgentRunStepType.VALIDATE_SCOPE,
        status=AgentRunStepStatus.READY,
        idempotency_key=f"{run.id}:0",
    )
    run.current_step_id = step.id
    event = AgentRunEvent(
        id="event_agent_run_repository",
        run_id=run.id,
        task_id=task.id,
        correlation_id=run.correlation_id,
        event_type=AgentRunEventType.RUN_CREATED,
    )
    audit = AuditEvent(
        id="audit_agent_run_repository",
        event_type="agent_run.created",
        entity_id=run.id,
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
        connection.execute(
            "INSERT INTO approval_requests(approval_id,project_id,workspace_id,work_order_id,"
            "source_agent_id,status,expires_at,created_at,updated_at,version,payload) "
            "VALUES(%s,%s,%s,NULL,NULL,%s,%s,%s,%s,0,%s)",
            (
                approval.id, project.id, workspace.id, approval.status.value,
                approval.expires_at, approval.created_at, approval.updated_at,
                Jsonb(approval.model_dump(mode="json")),
            ),
        )
        connection.execute(
            "INSERT INTO agent_tasks(task_id,project_id,workspace_id,external_agent_id,"
            "approval_request_id,status,updated_at,payload) VALUES(%s,%s,%s,NULL,%s,%s,%s,%s)",
            (
                task.id, project.id, workspace.id, approval.id, task.status.value,
                task.updated_at, Jsonb(task.model_dump(mode="json")),
            ),
        )

    repository.create(run=run, steps=[step], events=[event], audit=audit)
    assert repository.get(run.id) == run
    assert repository.list_for_workspace(
        workspace_id=workspace.id, task_id=task.id, limit=10
    ) == [run]
    assert repository.list_for_workspace(workspace_id="another_workspace") == []
    assert repository.list_for_task(task.id) == [run]
    assert repository.list_for_task("missing_task") == []
    assert repository.get_active_for_task(task.id) == run
    assert repository.get("missing_run") is None
    assert repository.list_steps_for_run(run.id) == [step]
    assert repository.list_steps_for_runs([run]) == [step]
    assert repository.list_steps_for_runs([]) == []
    assert repository.list_events_for_run(run.id) == [event]
    application_repositories = ApplicationRepositories.from_connector(connect)
    executor = DefaultAgentRunStepExecutor(
        task_repository=application_repositories.agent_tasks,
        approval_repository=application_repositories.approval_decisions,
        run_repository=application_repositories.agent_runs,
    )
    validation_result = executor(run, step, 30)
    assert validation_result.decision.value == "PASS"
    assert validation_result.evidence["repository"] == task.repository
    assert validation_result.evidence["approval_status"] == "APPROVED"
    expected_run = run
    expected_steps = repository.list_steps_for_run(run.id)
    updated_run = expected_run.model_copy(deep=True)
    previous_status, new_status = transition_run(
        updated_run,
        AgentRunStatus.PAUSED,
        reason="exercise caller-owned PostgreSQL transaction",
    )
    updated_steps = [step.model_copy(deep=True) for step in expected_steps]
    control_event = AgentRunEvent(
        run_id=updated_run.id,
        task_id=updated_run.task_id,
        step_id=updated_run.current_step_id,
        correlation_id=updated_run.correlation_id,
        event_type=AgentRunEventType.OPERATOR_ACTION,
        previous_status=previous_status.value,
        new_status=new_status.value,
        message="Test transaction-scoped AgentRun pause.",
        actor="postgres-control-test",
    )
    control_audit = AuditEvent(
        event_type="agent_run.paused",
        entity_id=updated_run.id,
        workspace_id=updated_run.workspace_id,
        actor="postgres-control-test",
        details={"event_ids": [control_event.id]},
    )
    with pytest.raises(RuntimeError, match="abort outer transaction"):
        with connect() as connection:
            with connection.transaction():
                snapshot_run, snapshot_steps = repository.get_control_snapshot_in_transaction(
                    connection,
                    run_id=run.id,
                    workspace_id=workspace.id,
                )
                assert snapshot_run == expected_run
                assert snapshot_steps == expected_steps
                repository.commit_control_transition_in_transaction(
                    connection,
                    expected_run=expected_run,
                    updated_run=updated_run,
                    expected_steps=expected_steps,
                    updated_steps=updated_steps,
                    events=[control_event],
                    audits=[control_audit],
                )
                raise RuntimeError("abort outer transaction")
    assert repository.get(run.id) == expected_run
    with connect() as connection:
        with connection.transaction():
            repository.commit_control_transition_in_transaction(
                connection,
                expected_run=expected_run,
                updated_run=updated_run,
                expected_steps=expected_steps,
                updated_steps=updated_steps,
                events=[control_event],
                audits=[control_audit],
            )
    paused = repository.get(run.id)
    assert paused == updated_run
    assert paused is not None
    assert repository.get(run.id) == paused
    assert paused.status.value == "PAUSED"
    assert repository.list_steps_for_run(run.id)[0].status.value == "READY"
    resumed = resume_agent_run(
        paused,
        actor="postgres-control-test",
        reason="exercise repository-bound resume",
        run_repository=repository,
    )
    assert repository.get(run.id) == resumed
    assert resumed.status.value == "SCHEDULED"
    assert repository.list_steps_for_run(run.id)[0].status.value == "READY"
    run = resumed
    lease_token = acquire_run_lease(
        run,
        worker_id="postgres-worker",
        lease_seconds=30,
        run_repository=repository,
    )
    leased = repository.get(run.id)
    assert leased is not None
    assert leased.lease_owner == "postgres-worker"
    assert leased.lease_token
    assert lease_token == leased.lease_token
    heartbeated = heartbeat_run(
        leased,
        worker_id="postgres-worker",
        lease_token=lease_token,
        lease_seconds=45,
        run_repository=repository,
    )
    assert repository.get(run.id) == heartbeated
    assert heartbeated.lease_owner == "postgres-worker"
    assert heartbeated.lease_token == lease_token
    leased = heartbeated

    second_run = run.model_copy(
        update={"id": "run_agent_run_repository_second", "current_step_id": "step_agent_run_repository_second"}
    )
    second_step = step.model_copy(
        update={"id": "step_agent_run_repository_second", "run_id": second_run.id}
    )
    second_event = event.model_copy(
        update={"id": "event_agent_run_repository_second", "run_id": second_run.id}
    )
    second_audit = audit.model_copy(
        update={"id": "audit_agent_run_repository_second", "entity_id": second_run.id}
    )
    with pytest.raises(AgentRunRepositoryConflict):
        repository.create(
            run=second_run,
            steps=[second_step],
            events=[second_event],
            audit=second_audit,
        )
    # The lease is durable state, so compare against the leased snapshot rather
    # than the original pre-lease object after the duplicate create is rejected.
    assert repository.get_active_for_task(task.id) == leased
    expired, _step, _event, _audit = repository.update_lease(
        run_id=run.id,
        worker_id="postgres-worker",
        lease_token=lease_token,
        lease_seconds=1,
        now=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    assert expired.lease_expires_at is not None
    assert expired.lease_expires_at < datetime.now(timezone.utc)
    assert recover_stale_leases(
        actor="postgres-watchdog-test",
        workspace_id=workspace.id,
        run_repository=repository,
    ) == 1
    recovered = repository.get(run.id)
    assert recovered is not None and recovered.lease_owner is None
