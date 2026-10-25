from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

import psycopg
import pytest
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

import app.agent_worker_validation_runner as validation_runner
import app.agent_worker_claims as worker_claims
from app.agent_task_models import AgentTask, AgentTaskEvent, AgentTaskEventType, AgentTaskStatus
from app.agent_task_repository import PostgresAgentTaskRepository, SQLiteAgentTaskRepository
from app.approval_repository import (
    PostgresApprovalDecisionRepository,
    SQLiteApprovalDecisionRepository,
)
from app.postgres_database import run_postgres_migrations
from app.agent_worker_validation_runner import (
    CommandExecutionResult,
    run_agent_worker_validation_commands,
)
from app.models import (
    AgentProvider,
    ApprovalRequest,
    AuditEvent,
    FileChange,
    Project,
    RiskAssessment,
    RiskLevel,
    Workspace,
)
from app.store import store


@pytest.fixture
def postgres_validation_repositories() -> Iterator[
    tuple[
        Callable[[], Connection],
        PostgresAgentTaskRepository,
        PostgresApprovalDecisionRepository,
        AgentTask,
        ApprovalRequest,
    ]
]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL validation worker tests")
    schema = f"ct_worker_validation_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    try:
        run_postgres_migrations(connect)
        workspace = Workspace(
            id=f"workspace_{uuid.uuid4().hex}", name="Validation PG", owner_user_id="owner"
        )
        project = Project(
            id=f"project_{uuid.uuid4().hex}", name="Validation PG", workspace_id=workspace.id
        )
        approval = ApprovalRequest(
            project_id=project.id,
            workspace_id=workspace.id,
            title="Approved validation",
            summary="Run allowlisted validation",
            files=[FileChange(path="README.md", diff="+ok")],
            target_branch="main",
            test_plan=["python -m pytest"],
            status="APPROVED",
            risk=RiskAssessment(level=RiskLevel.LOW),
        )
        task = AgentTask(
            id=f"task_{uuid.uuid4().hex}",
            provider=AgentProvider.CODEX,
            project_id=project.id,
            workspace_id=workspace.id,
            title="Validation task",
            goal="Run the approved test",
            status=AgentTaskStatus.APPROVED,
            approval_request_id=approval.id,
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
                "INSERT INTO approval_requests(approval_id,project_id,workspace_id,work_order_id,source_agent_id,status,expires_at,created_at,updated_at,version,payload) "
                "VALUES(%s,%s,%s,NULL,NULL,%s,%s,%s,%s,0,%s)",
                (
                    approval.id,
                    project.id,
                    workspace.id,
                    approval.status.value,
                    approval.expires_at,
                    approval.created_at,
                    approval.updated_at,
                    Jsonb(approval.model_dump(mode="json")),
                ),
            )
        task_repository = PostgresAgentTaskRepository(connect)
        task_repository.create(
            task=task,
            event=AgentTaskEvent(task_id=task.id, event_type=AgentTaskEventType.TASK_CREATED),
            audit=AuditEvent(
                event_type="agent_task.created", entity_id=task.id, workspace_id=workspace.id
            ),
        )
        yield connect, task_repository, PostgresApprovalDecisionRepository(connect), task, approval
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )


def setup_function() -> None:
    store.reset()


def _project() -> Project:
    project = Project(
        name="Validation Runner", description="demo", workspace_id="workspace-worker-test"
    )
    store.projects[project.id] = project
    return project


def _approval(project: Project, test_plan: list[str] | None = None) -> ApprovalRequest:
    approval = ApprovalRequest(
        project_id=project.id,
        title="Approved validation run",
        summary="Run validation commands safely.",
        agent_name="validation-runner",
        target_branch="feature/validation-runner",
        files=[
            FileChange(
                path="docs/example.md",
                change_type="update",
                diff="+updated",
                new_content="updated\n",
            )
        ],
        test_plan=test_plan
        if test_plan is not None
        else ["python -m pytest", "ruff check backend"],
        rollback_plan="Revert generated branch.",
        risk=RiskAssessment(level=RiskLevel.LOW),
        status="APPROVED",
    )
    store.approval_requests[approval.id] = approval
    return approval


def _approved_task(approval: ApprovalRequest) -> AgentTask:
    task = AgentTask(
        provider=AgentProvider.CODEX,
        project_id=approval.project_id,
        title="Run validation commands",
        goal="Execute allowlisted validation commands.",
        repository="ramgolladi1503-sys/aixion-control-tower",
        status=AgentTaskStatus.APPROVED,
        approval_request_id=approval.id,
    )
    store.agent_tasks[task.id] = task
    store.persist()
    return task


def test_validation_runner_records_passed_commands() -> None:
    project = _project()
    approval = _approval(project)
    task = _approved_task(approval)
    calls: list[str] = []

    def fake_executor(command: str) -> CommandExecutionResult:
        calls.append(command)
        return CommandExecutionResult(
            command=command, exit_code=0, output_summary=f"passed {command}", duration_ms=7
        )

    result = run_agent_worker_validation_commands(
        task_id=task.id, worker_id="runner", executor=fake_executor
    )

    assert result.success is True
    assert result.task_id == task.id
    assert result.approval_request_id == approval.id
    assert result.command_count == 2
    assert calls == ["python -m pytest", "ruff check backend"]
    assert result.started_event_id in store.agent_task_events
    assert result.finished_event_id in store.agent_task_events
    assert store.agent_tasks[task.id].status == AgentTaskStatus.APPROVED
    assert store.agent_tasks[task.id].worker_lease_owner is None

    started = store.agent_task_events[result.started_event_id]
    finished = store.agent_task_events[result.finished_event_id]
    assert started.event_type == "TESTS_STARTED"
    assert started.status == AgentTaskStatus.TESTING
    assert (
        started.metadata["validation_policy"]["policy_version"]
        == "aixion-validation-execution-policy-v1"
    )
    assert finished.event_type == "TESTS_PASSED"
    assert finished.status == AgentTaskStatus.APPROVED
    assert finished.metadata["commands_executed"] is True
    assert finished.metadata["results"][0]["output_summary"] == "passed python -m pytest"
    assert finished.metadata["results"][0]["allowed_prefix"] == "python -m pytest"
    assert finished.metadata["results"][0]["duration_ms"] == 7
    assert finished.metadata["artifact_summary"]["passed_count"] == 2
    assert finished.metadata["artifact_summary"]["failed_count"] == 0
    assert finished.metadata["artifact_summary"]["total_duration_ms"] == 14
    assert any(
        audit.event_type == "agent_worker.validation_run_started" for audit in store.audit_events
    )
    assert any(
        audit.event_type == "agent_worker.validation_run_completed" for audit in store.audit_events
    )


def test_validation_runner_stops_on_first_failure_and_marks_task_failed() -> None:
    project = _project()
    approval = _approval(project, test_plan=["python -m pytest", "ruff check backend"])
    task = _approved_task(approval)
    calls: list[str] = []

    def fake_executor(command: str) -> CommandExecutionResult:
        calls.append(command)
        return CommandExecutionResult(
            command=command, exit_code=1, output_summary="failed", duration_ms=5
        )

    result = run_agent_worker_validation_commands(
        task_id=task.id, worker_id="runner", executor=fake_executor
    )

    assert result.success is False
    assert result.reason == "Validation commands failed."
    assert result.command_count == 1
    assert calls == ["python -m pytest"]
    assert store.agent_tasks[task.id].status == AgentTaskStatus.FAILED
    assert store.agent_tasks[task.id].worker_lease_owner is None
    finished = store.agent_task_events[result.finished_event_id]
    assert finished.event_type == "TESTS_FAILED"
    assert finished.status == AgentTaskStatus.FAILED
    assert finished.metadata["results"][0]["exit_code"] == 1
    assert finished.metadata["artifact_summary"]["failed_count"] == 1


def test_validation_runner_records_timeout_failure() -> None:
    project = _project()
    approval = _approval(project, test_plan=["python -m pytest"])
    task = _approved_task(approval)

    def fake_executor(command: str) -> CommandExecutionResult:
        return CommandExecutionResult(
            command=command,
            exit_code=124,
            timed_out=True,
            output_summary="timed out",
            duration_ms=120000,
        )

    result = run_agent_worker_validation_commands(
        task_id=task.id, worker_id="runner", executor=fake_executor
    )

    assert result.success is False
    assert result.results[0].timed_out is True
    assert store.agent_tasks[task.id].status == AgentTaskStatus.FAILED
    finished = store.agent_task_events[result.finished_event_id]
    assert finished.metadata["artifact_summary"]["timed_out_count"] == 1


def test_validation_runner_truncates_large_output_artifact() -> None:
    project = _project()
    approval = _approval(project, test_plan=["python -m pytest"])
    task = _approved_task(approval)
    huge_output = "x" * 5000

    def fake_executor(command: str) -> CommandExecutionResult:
        return CommandExecutionResult(command=command, exit_code=0, output_summary=huge_output)

    result = run_agent_worker_validation_commands(
        task_id=task.id, worker_id="runner", executor=fake_executor
    )

    assert result.success is True
    finished = store.agent_task_events[result.finished_event_id]
    assert len(finished.metadata["results"][0]["output_summary"]) == 4000
    assert finished.metadata["results"][0]["output_truncated"] is True
    assert finished.metadata["artifact_summary"]["output_truncated"] is True


def test_validation_runner_refuses_non_allowlisted_command_before_execution() -> None:
    project = _project()
    approval = _approval(project, test_plan=["python scripts/deploy.py"])
    task = _approved_task(approval)
    calls: list[str] = []

    def fake_executor(command: str) -> CommandExecutionResult:
        calls.append(command)
        return CommandExecutionResult(command=command, exit_code=0)

    result = run_agent_worker_validation_commands(task_id=task.id, executor=fake_executor)

    assert result.success is False
    assert result.reason == "Validation command is not allowlisted: python scripts/deploy.py"
    assert calls == []
    assert store.agent_task_events == {}
    assert store.agent_tasks[task.id].status == AgentTaskStatus.APPROVED
    assert store.agent_tasks[task.id].worker_lease_owner is None
    assert any(
        audit.event_type == "agent_worker.validation_run_refused" for audit in store.audit_events
    )
    refusal = next(
        audit
        for audit in store.audit_events
        if audit.event_type == "agent_worker.validation_run_refused"
    )
    assert refusal.details["validation_policy"]["shell_execution_allowed"] is False


def test_validation_runner_refuses_missing_approval() -> None:
    project = _project()
    approval = _approval(project)
    task = _approved_task(approval)
    del store.approval_requests[approval.id]
    store.persist()

    result = run_agent_worker_validation_commands(task_id=task.id)

    assert result.success is False
    assert result.reason == "Linked approval request not found."
    assert store.agent_task_events == {}
    assert store.agent_tasks[task.id].worker_lease_owner is None


def test_validation_runner_requires_task_selector() -> None:
    result = run_agent_worker_validation_commands()

    assert result.success is False
    assert result.reason == "Provide task_id or set first_approved=true."


def test_postgres_validation_runner_persists_events_atomically_without_sqlite_projection(
    postgres_validation_repositories,
) -> None:
    connect, task_repository, approval_repository, task, approval = postgres_validation_repositories
    result = run_agent_worker_validation_commands(
        task_id=task.id,
        worker_id="mission-control:run_pg:validation",
        executor=lambda command: CommandExecutionResult(command=command, exit_code=0),
        task_repository=task_repository,
        approval_repository=approval_repository,
    )

    assert result.success is True
    durable_task = task_repository.get(task.id)
    assert durable_task is not None
    assert durable_task.status == AgentTaskStatus.APPROVED
    assert durable_task.worker_lease_token is None
    events = task_repository.list_events(task.id, workspace_id=task.workspace_id)
    passed = next(event for event in events if event.event_type == AgentTaskEventType.TESTS_PASSED)
    assert passed.metadata["approval_request_id"] == approval.id
    assert passed.metadata["results"][0]["exit_code"] == 0
    assert not any(event.task_id == task.id for event in store.agent_task_events.values())
    with connect() as conn:
        audits = conn.execute(
            "SELECT event_type FROM audit_events WHERE entity_id=%s AND workspace_id=%s",
            (task.id, task.workspace_id),
        ).fetchall()
    assert "agent_worker.validation_run_completed" in {row[0] for row in audits}


def test_postgres_validation_lease_covers_configured_command_timeout(
    postgres_validation_repositories,
) -> None:
    connect, task_repository, approval_repository, task, _approval = postgres_validation_repositories

    def inspect_live_lease(command: str) -> CommandExecutionResult:
        with connect() as conn:
            payload = conn.execute(
                "SELECT payload FROM agent_tasks WHERE task_id=%s",
                (task.id,),
            ).fetchone()[0]
        expires_at = datetime.fromisoformat(payload["worker_lease_expires_at"])
        assert expires_at > datetime.now(timezone.utc) + timedelta(seconds=580)
        return CommandExecutionResult(command=command, exit_code=0)

    result = run_agent_worker_validation_commands(
        task_id=task.id,
        worker_id="mission-control:run_pg:validation",
        lease_seconds=300,
        timeout_seconds=600,
        executor=inspect_live_lease,
        task_repository=task_repository,
        approval_repository=approval_repository,
    )

    assert result.success is True
    with connect() as conn:
        renewal_count = conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE entity_id=%s "
            "AND event_type='agent_worker.validation_lease_renewed'",
            (task.id,),
        ).fetchone()[0]
    assert renewal_count == 1


def test_postgres_validation_runner_stops_when_lease_renewal_cannot_be_confirmed(
    postgres_validation_repositories,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect, task_repository, approval_repository, task, approval = postgres_validation_repositories
    executed: list[str] = []
    renewals = 0
    original_renewal = task_repository.renew_validation_lease

    def fail_second_renewal(**kwargs: object) -> AgentTask:
        nonlocal renewals
        renewals += 1
        if renewals == 2:
            raise RuntimeError("lease authority unavailable")
        return original_renewal(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(task_repository, "renew_validation_lease", fail_second_renewal)
    monkeypatch.setattr(
        approval_repository,
        "get",
        lambda _approval_id: approval.model_copy(
            update={"test_plan": ["python -m pytest", "python -m pytest -q"]}
        ),
    )
    result = run_agent_worker_validation_commands(
        task_id=task.id,
        worker_id="mission-control:run_pg:validation",
        executor=lambda command: executed.append(command) or CommandExecutionResult(
            command=command, exit_code=0,
        ),
        task_repository=task_repository,
        approval_repository=approval_repository,
    )

    assert executed == ["python -m pytest"]
    assert result.success is False
    assert result.command_count == 1
    assert len(result.results) == 1
    assert result.final_status is None
    assert "no further command was started" in result.reason
    durable_task = task_repository.get(task.id)
    assert durable_task is not None
    assert durable_task.status == AgentTaskStatus.APPROVED
    assert durable_task.worker_lease_token is not None


def test_postgres_validation_failure_marks_task_failed_and_releases_lease_atomically(
    postgres_validation_repositories,
) -> None:
    connect, task_repository, approval_repository, task, approval = postgres_validation_repositories
    result = run_agent_worker_validation_commands(
        task_id=task.id,
        worker_id="mission-control:run_pg:validation",
        executor=lambda command: CommandExecutionResult(command=command, exit_code=2),
        task_repository=task_repository,
        approval_repository=approval_repository,
    )

    assert result.success is False
    assert result.final_status == AgentTaskStatus.FAILED
    durable_task = task_repository.get(task.id)
    assert durable_task is not None
    assert durable_task.status == AgentTaskStatus.FAILED
    assert durable_task.worker_lease_owner is None
    assert durable_task.worker_lease_expires_at is None
    assert durable_task.worker_lease_token is None
    events = task_repository.list_events(task.id, workspace_id=task.workspace_id)
    failure = next(event for event in events if event.event_type == AgentTaskEventType.TESTS_FAILED)
    assert failure.status == AgentTaskStatus.FAILED
    assert failure.metadata["approval_request_id"] == approval.id
    assert failure.metadata["results"][0]["exit_code"] == 2
    with connect() as conn:
        audit = conn.execute(
            "SELECT event_type,details FROM audit_events WHERE entity_id=%s "
            "AND event_type='agent_worker.validation_run_completed'",
            (task.id,),
        ).fetchone()
    assert audit is not None
    assert audit[0] == "agent_worker.validation_run_completed"
    assert audit[1]["event_id"] == failure.id
    assert audit[1]["passed"] is False
    assert not any(event.task_id == task.id for event in store.agent_task_events.values())


def test_postgres_validation_runner_exception_records_unknown_outcome_and_releases_lease(
    postgres_validation_repositories,
) -> None:
    connect, task_repository, approval_repository, task, _approval = (
        postgres_validation_repositories
    )

    def raise_before_result(_command: str) -> CommandExecutionResult:
        raise OSError("private command output must not be persisted")

    result = run_agent_worker_validation_commands(
        task_id=task.id,
        worker_id="mission-control:run_pg:validation",
        executor=raise_before_result,
        task_repository=task_repository,
        approval_repository=approval_repository,
    )

    assert result.success is False
    assert result.final_status == AgentTaskStatus.FAILED
    assert result.reason.endswith("outcome is unknown.")
    assert result.results == []
    durable_task = task_repository.get(task.id)
    assert durable_task is not None
    assert durable_task.status == AgentTaskStatus.FAILED
    assert durable_task.worker_lease_owner is None
    assert durable_task.worker_lease_expires_at is None
    assert durable_task.worker_lease_token is None
    events = task_repository.list_events(task.id, workspace_id=task.workspace_id)
    failed = next(event for event in events if event.event_type == AgentTaskEventType.FAILED)
    assert failed.status == AgentTaskStatus.FAILED
    assert failed.metadata["execution_outcome"] == "UNKNOWN"
    assert "commands_executed" not in failed.metadata
    assert failed.metadata["results"] == []
    assert "private command output" not in str(failed.model_dump())
    with connect() as conn:
        audit = conn.execute(
            "SELECT event_type,details FROM audit_events WHERE entity_id=%s "
            "AND event_type='agent_worker.validation_runner_failed'",
            (task.id,),
        ).fetchone()
    assert audit is not None
    assert audit[1]["event_id"] == failed.id
    assert audit[1]["execution_outcome"] == "UNKNOWN"
    assert not any(event.task_id == task.id for event in store.agent_task_events.values())


def test_postgres_validation_runner_recovers_expired_started_task_without_rerunning(
    postgres_validation_repositories,
) -> None:
    connect, task_repository, _approval_repository, task, _approval = (
        postgres_validation_repositories
    )
    claimed, reason, _ = task_repository.claim(
        task_id=task.id, worker_id="abruptly-killed-worker", lease_seconds=60,
    )
    assert reason == ""
    assert claimed is not None
    started_task = claimed.model_copy(update={"updated_at": datetime.now(timezone.utc)})
    started = AgentTaskEvent(
        task_id=task.id,
        event_type=AgentTaskEventType.TESTS_STARTED,
        status=None,
        actor="abruptly-killed-worker",
        message="Worker started allowlisted validation commands.",
        metadata={
            "worker_id": "abruptly-killed-worker",
            "lease_token": claimed.worker_lease_token,
            "approval_request_id": task.approval_request_id,
            "runner_type": "validation_command_runner",
            "command_count": 1,
        },
    )
    started_audit = AuditEvent(
        event_type="agent_task.event_recorded",
        entity_id=task.id,
        workspace_id=task.workspace_id,
        actor="abruptly-killed-worker",
        details={
            "event_id": started.id,
            "status": None,
            "message": started.message,
            "notification_id": None,
        },
    )
    task_repository.commit_event(
        expected=claimed, updated=started_task, event=started, audit=started_audit,
    )
    expired = started_task.model_copy(update={
        "worker_lease_expires_at": datetime.now(timezone.utc) - timedelta(seconds=5),
    })
    with connect() as conn:
        conn.execute(
            "UPDATE agent_tasks SET updated_at=%s,payload=%s WHERE task_id=%s AND workspace_id=%s",
            (expired.updated_at, Jsonb(expired.model_dump(mode="json")), task.id, task.workspace_id),
        )
    executed: list[str] = []

    result = run_agent_worker_validation_commands(
        task_id=task.id,
        worker_id="replacement-worker",
        executor=lambda command: executed.append(command) or CommandExecutionResult(
            command=command, exit_code=0,
        ),
        task_repository=task_repository,
        approval_repository=_approval_repository,
    )

    assert executed == []
    assert result.success is False
    durable_task = task_repository.get(task.id)
    assert durable_task is not None
    assert durable_task.status == AgentTaskStatus.FAILED
    assert durable_task.worker_lease_token is None
    events = task_repository.list_events(task.id, workspace_id=task.workspace_id)
    failed = next(event for event in events if event.event_type == AgentTaskEventType.FAILED)
    assert failed.metadata["execution_outcome"] == "UNKNOWN"
    assert failed.metadata["failure_class"] == "WorkerProcessLost"
    assert failed.metadata["results"] is None
    assert failed.metadata["completed_command_count"] is None
    assert "commands_executed" not in failed.metadata
    with connect() as conn:
        audit = conn.execute(
            "SELECT details FROM audit_events WHERE entity_id=%s "
            "AND event_type='agent_worker.validation_worker_lost'",
            (task.id,),
        ).fetchone()
    assert audit is not None
    assert audit[0]["event_id"] == failed.id
    assert audit[0]["commands_completed"] is None


def test_postgres_validation_runner_rolls_back_pass_event_when_audit_fails(
    postgres_validation_repositories,
) -> None:
    connect, task_repository, approval_repository, task, _approval_record = (
        postgres_validation_repositories
    )
    with connect() as conn:
        conn.execute(
            "CREATE FUNCTION reject_validation_completion_audit() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN IF NEW.event_type='agent_worker.validation_run_completed' THEN "
            "RAISE EXCEPTION 'injected validation audit failure'; END IF; RETURN NEW; END $$"
        )
        conn.execute(
            "CREATE TRIGGER reject_validation_completion_audit BEFORE INSERT ON audit_events "
            "FOR EACH ROW EXECUTE FUNCTION reject_validation_completion_audit()"
        )

    with pytest.raises(Exception, match="injected validation audit failure"):
        run_agent_worker_validation_commands(
            task_id=task.id,
            worker_id="mission-control:run_pg:validation",
            executor=lambda command: CommandExecutionResult(command=command, exit_code=0),
            task_repository=task_repository,
            approval_repository=approval_repository,
        )

    durable_task = task_repository.get(task.id)
    assert durable_task is not None
    assert durable_task.worker_lease_token is not None
    events = task_repository.list_events(task.id, workspace_id=task.workspace_id)
    assert not any(event.event_type == AgentTaskEventType.TESTS_PASSED for event in events)
    assert any(event.event_type == AgentTaskEventType.TESTS_STARTED for event in events)
    with connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM audit_events WHERE entity_id=%s AND event_type='agent_worker.validation_run_completed'",
                (task.id,),
            ).fetchone()[0]
            == 0
        )
    assert not any(event.task_id == task.id for event in store.agent_task_events.values())


@pytest.mark.parametrize("repository_mode", ["missing", "sqlite"])
def test_postgres_auth_validation_runner_rejects_legacy_sqlite_authority(
    monkeypatch: pytest.MonkeyPatch,
    repository_mode: str,
) -> None:
    monkeypatch.setattr(
        worker_claims,
        "get_settings",
        lambda: type("Settings", (), {"is_production": False, "postgres_auth_enabled": True})(),
    )
    task_repository = SQLiteAgentTaskRepository(store._connect) if repository_mode == "sqlite" else None
    approval_repository = (
        SQLiteApprovalDecisionRepository(store._connect) if repository_mode == "sqlite" else None
    )

    with pytest.raises(ValueError, match="PostgreSQL task and approval repositories"):
        run_agent_worker_validation_commands(
            task_id="must-not-read-from-legacy-store",
            task_repository=task_repository,
            approval_repository=approval_repository,
        )


class _ValidationRuntimeConnector:
    def connect(self):
        raise AssertionError("test connector must not be invoked")


def test_validation_runner_rejects_postgres_repositories_from_different_runtimes_before_claim_or_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        worker_claims,
        "get_settings",
        lambda: type("Settings", (), {"is_production": True, "postgres_auth_enabled": True})(),
    )
    task_repository = PostgresAgentTaskRepository(_ValidationRuntimeConnector().connect)
    approval_repository = PostgresApprovalDecisionRepository(_ValidationRuntimeConnector().connect)
    claims: list[object] = []
    executions: list[str] = []

    def unexpected_claim(**kwargs):
        claims.append(kwargs)
        pytest.fail("different PostgreSQL runtimes must be rejected before claim")

    def unexpected_executor(command: str) -> CommandExecutionResult:
        executions.append(command)
        return CommandExecutionResult(command=command, exit_code=0)

    monkeypatch.setattr(validation_runner, "claim_agent_task_for_worker", unexpected_claim)

    with pytest.raises(ValueError, match="share the same PostgreSQL connection factory"):
        run_agent_worker_validation_commands(
            task_id="must-not-claim",
            executor=unexpected_executor,
            task_repository=task_repository,
            approval_repository=approval_repository,
        )

    assert claims == []
    assert executions == []


def test_validation_runner_accepts_same_runtime_postgres_pair_before_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        worker_claims,
        "get_settings",
        lambda: type("Settings", (), {"is_production": True, "postgres_auth_enabled": True})(),
    )
    runtime = _ValidationRuntimeConnector()
    task_repository = PostgresAgentTaskRepository(runtime.connect)
    approval_repository = PostgresApprovalDecisionRepository(runtime.connect)
    monkeypatch.setattr(task_repository, "recover_abandoned_validation_tasks", lambda **_kwargs: [])
    claims: list[object] = []
    executions: list[str] = []

    def expected_claim(**kwargs):
        claims.append(kwargs["repository"])
        return type(
            "Claim",
            (),
            {
                "success": False,
                "task": None,
                "task_id": kwargs["task_id"],
                "lease_token": None,
                "reason": "claim reached with shared PostgreSQL runtime",
            },
        )()

    def unexpected_executor(command: str) -> CommandExecutionResult:
        executions.append(command)
        return CommandExecutionResult(command=command, exit_code=0)

    monkeypatch.setattr(validation_runner, "claim_agent_task_for_worker", expected_claim)
    result = run_agent_worker_validation_commands(
        task_id="postgres-task",
        executor=unexpected_executor,
        task_repository=task_repository,
        approval_repository=approval_repository,
    )

    assert result.success is False
    assert result.reason == "claim reached with shared PostgreSQL runtime"
    assert claims == [task_repository]
    assert executions == []
