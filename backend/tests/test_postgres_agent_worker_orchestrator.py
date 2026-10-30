from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from types import SimpleNamespace

import psycopg
import pytest
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app import agent_worker_claims as worker_claims
from app.agent_task_models import AgentTask, AgentTaskEventType, AgentTaskStatus
from app.agent_task_repository import PostgresAgentTaskRepository
from app.agent_worker_github_branch import GitHubBranchCreateResult
from app.agent_worker_github_pr import GitHubPullRequestCreateResult
from app.agent_worker_orchestrator import run_approved_agent_task_worker_flow
from app import agent_worker_orchestrator as orchestrator_module
from app.agent_worker_validation_runner import CommandExecutionResult
from app.agent_worker_workspace import WorkspaceCommandResult
from app.approval_repository import PostgresApprovalDecisionRepository
from app.postgres_audit_repository import PostgresAuditEventRepository
from app.models import (
    AgentProvider,
    ApprovalRequest,
    ApprovalStatus,
    FileChange,
    Project,
    RiskAssessment,
    RiskLevel,
    Workspace,
)
from app.postgres_database import run_postgres_migrations
from app.store import store


@pytest.fixture
def postgres_repositories() -> Iterator[tuple[PostgresAgentTaskRepository, PostgresApprovalDecisionRepository, Callable[[], Connection]]]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL orchestration integration tests")
    schema = f"ct_worker_orchestrator_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        conn = psycopg.connect(dsn)
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return conn

    try:
        run_postgres_migrations(connect)
        yield PostgresAgentTaskRepository(connect), PostgresApprovalDecisionRepository(connect), connect
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


class FakeBranchClient:
    def __init__(self) -> None:
        self.branches: set[str] = set()

    def branch_exists(self, repository: str, branch: str) -> bool:
        return branch in self.branches

    def get_branch_sha(self, repository: str, branch: str) -> str:
        return "source-sha"

    def create_branch(self, repository: str, branch: str, source_sha: str) -> GitHubBranchCreateResult:
        self.branches.add(branch)
        return GitHubBranchCreateResult(repository, branch, "main", source_sha, True, f"https://github.com/{repository}/tree/{branch}")


class FakeFileClient:
    def __init__(self, branches: FakeBranchClient) -> None:
        self.branches = branches

    def branch_exists(self, repository: str, branch: str) -> bool:
        return branch in self.branches.branches

    def get_file_sha(self, repository: str, path: str, branch: str) -> str | None:
        return None

    def create_or_update_file(self, repository: str, path: str, branch: str, content: str, message: str, sha: str | None = None) -> str:
        return "commit-file"

    def delete_file(self, repository: str, path: str, branch: str, message: str, sha: str) -> str:
        return "commit-delete"


class FakePRClient:
    def __init__(self, branches: FakeBranchClient) -> None:
        self.branches = branches

    def branch_exists(self, repository: str, branch: str) -> bool:
        return branch in self.branches.branches

    def open_pull_request_exists(self, repository: str, head_branch: str, base_branch: str) -> bool:
        return False

    def create_pull_request(self, repository: str, title: str, body: str, head_branch: str, base_branch: str) -> GitHubPullRequestCreateResult:
        return GitHubPullRequestCreateResult(repository, 41, title, head_branch, base_branch, f"https://github.com/{repository}/pull/41")


class FakeWorkspaceRunner:
    def run(self, command: list[str], *, cwd=None) -> WorkspaceCommandResult:
        if command[:2] == ["git", "clone"]:
            from pathlib import Path

            Path(command[-1]).mkdir(parents=True, exist_ok=True)
        return WorkspaceCommandResult(command=command, exit_code=0, output_summary="ok")


def _seed_approved_task(connect: Callable[[], Connection]) -> AgentTask:
    suffix = uuid.uuid4().hex
    workspace = Workspace(id=f"workspace_{suffix}", name="Orchestration workspace", owner_user_id="owner")
    project = Project(id=f"project_{suffix}", name="Orchestration project", workspace_id=workspace.id)
    approval = ApprovalRequest(
        id=f"approval_{suffix}", project_id=project.id, workspace_id=workspace.id,
        title="Approved work", summary="Run PostgreSQL orchestration integration coverage.",
        target_branch="feature/orchestrator-test",
        files=[FileChange(path="docs/orchestrator.md", change_type="create", diff="+ok", new_content="ok\n")],
        test_plan=["python -m pytest"], risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.APPROVED,
    )
    task = AgentTask(
        id=f"task_{suffix}", provider=AgentProvider.CODEX, project_id=project.id,
        workspace_id=workspace.id, approval_request_id=approval.id,
        title="Orchestrate approved work", goal="Create branch, apply file, validate, open PR.",
        repository="example/project", branch_preference="feature/orchestrator-test",
        status=AgentTaskStatus.APPROVED,
    )
    with connect() as conn:
        conn.execute("INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)", (workspace.id, Jsonb(workspace.model_dump(mode="json"))))
        conn.execute("INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)", (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))))
        conn.execute(
            "INSERT INTO approval_requests(approval_id,project_id,workspace_id,status,created_at,updated_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (approval.id, project.id, workspace.id, approval.status.value, approval.created_at, approval.updated_at, Jsonb(approval.model_dump(mode="json"))),
        )
        conn.execute(
            "INSERT INTO agent_tasks(task_id,project_id,workspace_id,approval_request_id,status,updated_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (task.id, project.id, workspace.id, approval.id, task.status.value, task.updated_at, Jsonb(task.model_dump(mode="json"))),
        )
    return task


def _run(
    task: AgentTask,
    task_repository: PostgresAgentTaskRepository,
    approval_repository: PostgresApprovalDecisionRepository,
    audit_repository: PostgresAuditEventRepository,
    *,
    validation_exit_code: int = 0,
    use_workspace: bool = False,
):
    branches = FakeBranchClient()

    def execute(command: str) -> CommandExecutionResult:
        return CommandExecutionResult(command=command, exit_code=validation_exit_code, output_summary="passed" if validation_exit_code == 0 else "failed")

    return run_approved_agent_task_worker_flow(
        task_id=task.id,
        worker_id="postgres-orchestrator-test",
        branch_client=branches,
        file_client=FakeFileClient(branches),
        pr_client=FakePRClient(branches),
        validation_executor=execute,
        workspace_runner=FakeWorkspaceRunner() if use_workspace else None,
        use_isolated_workspace=use_workspace,
        use_container_validation=False,
        task_repository=task_repository,
        approval_repository=approval_repository,
        audit_repository=audit_repository,
    )


@pytest.mark.parametrize("validation_exit_code,expected_status,expected_success", [(0, AgentTaskStatus.READY_FOR_PR, True), (1, AgentTaskStatus.FAILED, False)])
def test_postgres_orchestrator_persists_transactional_summary_without_global_store(
    postgres_repositories,
    validation_exit_code: int,
    expected_status: AgentTaskStatus,
    expected_success: bool,
) -> None:
    task_repository, approval_repository, connect = postgres_repositories
    audit_repository = PostgresAuditEventRepository(connect)
    store.reset()
    task = _seed_approved_task(connect)

    result = _run(
        task, task_repository, approval_repository, audit_repository,
        validation_exit_code=validation_exit_code,
    )

    durable = task_repository.get(task.id)
    assert result.success is expected_success
    assert durable is not None and durable.status == expected_status
    assert result.summary_event_id is not None
    events = task_repository.list_events(task.id, workspace_id=task.workspace_id)
    summary = next(event for event in events if event.id == result.summary_event_id)
    assert summary.event_type == (AgentTaskEventType.DONE if expected_success else AgentTaskEventType.FAILED)
    assert summary.metadata["orchestration_success"] is expected_success
    with connect() as conn:
        audit_rows = conn.execute(
            "SELECT event_type,details FROM audit_events WHERE entity_id=%s ORDER BY created_at,event_id",
            (task.id,),
        ).fetchall()
        assert any(row[0] == "agent_worker.orchestration_started" for row in audit_rows)
        final_type = "agent_worker.orchestration_completed" if expected_success else "agent_worker.orchestration_failed"
        matching_final = [row for row in audit_rows if row[0] == final_type]
        assert len(matching_final) == 1
        assert matching_final[0][1]["event_id"] == result.summary_event_id
    assert store.agent_tasks == {}
    assert store.agent_task_events == {}
    assert store.audit_events == []


def test_postgres_orchestrator_workspace_audit_uses_runtime_repository(
    postgres_repositories,
) -> None:
    task_repository, approval_repository, connect = postgres_repositories
    audit_repository = PostgresAuditEventRepository(connect)
    store.reset()
    task = _seed_approved_task(connect)

    result = _run(
        task, task_repository, approval_repository, audit_repository, use_workspace=True
    )

    assert result.success is True
    workspace_events = audit_repository.list_for_workspace(
        task.workspace_id,
        entity_id=task.id,
        event_type="agent_worker.workspace_prepared",
    )
    assert len(workspace_events) == 1
    assert workspace_events[0].workspace_id == task.workspace_id
    assert workspace_events[0].details["workspace_isolated"] is True
    assert store.audit_events == []
    assert store.agent_tasks == {}
    assert store.agent_task_events == {}


def test_postgres_orchestrator_requires_audit_repository_before_work(
    postgres_repositories,
) -> None:
    task_repository, approval_repository, connect = postgres_repositories
    store.reset()
    task = _seed_approved_task(connect)

    with pytest.raises(ValueError, match="runtime-bound audit repository"):
        run_approved_agent_task_worker_flow(
            task_id=task.id,
            task_repository=task_repository,
            approval_repository=approval_repository,
        )

    assert store.agent_tasks == {}
    assert store.agent_task_events == {}
    assert store.audit_events == []


def test_postgres_orchestrator_rejects_audit_repository_from_another_runtime(
    postgres_repositories,
    monkeypatch,
) -> None:
    task_repository, approval_repository, connect = postgres_repositories
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    foreign_schema = f"ct_worker_orchestrator_foreign_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(foreign_schema)))

    foreign_connect_calls: list[bool] = []

    def foreign_connect() -> Connection:
        foreign_connect_calls.append(True)
        conn = psycopg.connect(dsn)
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(foreign_schema)))
        return conn

    try:
        audit_repository = PostgresAuditEventRepository(foreign_connect)
        store.reset()
        task = _seed_approved_task(connect)

        monkeypatch.setattr(
            orchestrator_module,
            "_task",
            lambda *_args, **_kwargs: pytest.fail(
                "a foreign audit runtime must be rejected before task lookup"
            ),
        )

        with pytest.raises(
            ValueError,
            match="must share the same runtime connection factory",
        ):
            run_approved_agent_task_worker_flow(
                task_id=task.id,
                task_repository=task_repository,
                approval_repository=approval_repository,
                audit_repository=audit_repository,
            )

        assert foreign_connect_calls == []
        assert task_repository.get(task.id) == task
        assert task_repository.list_events(task.id, workspace_id=task.workspace_id) == []
        with connect() as conn:
            assert conn.execute(
                "SELECT count(*) FROM audit_events WHERE entity_id=%s", (task.id,)
            ).fetchone()[0] == 0
        assert store.agent_tasks == {}
        assert store.agent_task_events == {}
        assert store.audit_events == []
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(foreign_schema)))


@pytest.mark.parametrize("settings", [
    SimpleNamespace(is_production=True, postgres_auth_enabled=False),
    SimpleNamespace(is_production=False, postgres_auth_enabled=True),
])
def test_orchestrator_rejects_implicit_sqlite_repository_in_postgres_modes(
    monkeypatch, settings,
) -> None:
    store.reset()
    monkeypatch.setattr(worker_claims, "get_settings", lambda: settings)

    with pytest.raises(
        ValueError,
        match="PostgreSQL orchestration requires PostgreSQL task and approval repositories",
    ):
        run_approved_agent_task_worker_flow(task_id="task_missing")

    assert store.agent_tasks == {}
    assert store.agent_task_events == {}
    assert store.audit_events == []


def test_postgres_orchestration_summary_failure_rolls_back_and_reports_ambiguous_effects(
    postgres_repositories,
) -> None:
    task_repository, approval_repository, connect = postgres_repositories
    audit_repository = PostgresAuditEventRepository(connect)
    store.reset()
    task = _seed_approved_task(connect)
    with connect() as conn:
        conn.execute(
            """
            CREATE FUNCTION reject_orchestration_completed_audit() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.event_type = 'agent_worker.orchestration_completed' THEN
                    RAISE EXCEPTION 'forced summary audit persistence failure';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
        conn.execute(
            "CREATE TRIGGER reject_orchestration_completed_audit "
            "BEFORE INSERT ON audit_events FOR EACH ROW "
            "EXECUTE FUNCTION reject_orchestration_completed_audit()"
        )

    result = _run(task, task_repository, approval_repository, audit_repository)

    assert result.success is False
    assert result.pr_result is not None and result.pr_result.success is True
    assert result.final_status == AgentTaskStatus.READY_FOR_PR
    assert result.summary_event_id is None
    assert "persisted outcome is ambiguous" in result.reason
    assert "forced summary audit persistence failure" in result.reason
    assert task_repository.get(task.id).status == AgentTaskStatus.READY_FOR_PR
    events = task_repository.list_events(task.id, workspace_id=task.workspace_id)
    assert any(event.event_type == AgentTaskEventType.PR_CREATED for event in events)
    assert not any(event.event_type == AgentTaskEventType.DONE for event in events)
    with connect() as conn:
        audit_types = [row[0] for row in conn.execute("SELECT event_type FROM audit_events WHERE entity_id=%s", (task.id,)).fetchall()]
    assert "agent_worker.orchestration_started" in audit_types
    assert "agent_worker.orchestration_completed" not in audit_types
    assert store.agent_tasks == {}
    assert store.agent_task_events == {}
    assert store.audit_events == []


def test_cli_production_profile_requires_postgres_and_never_falls_back_to_sqlite(monkeypatch, capsys) -> None:
    store.reset()
    monkeypatch.setenv("AIXION_PROFILE", "production")
    monkeypatch.delenv("AIXION_POSTGRES_DSN", raising=False)
    monkeypatch.setattr(
        orchestrator_module,
        "run_approved_agent_task_worker_flow",
        lambda **kwargs: pytest.fail("production CLI must not fall back to SQLite"),
    )

    exit_code = orchestrator_module.main(["--task-id", "missing", "--json"])

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "Worker runtime unavailable" in output
    assert "no task work was started" in output
    assert store.agent_tasks == {}


def test_cli_postgres_flag_opens_probes_and_closes_runtime(monkeypatch, capsys) -> None:
    state = {"opened": False, "probed": False, "closed": False, "called": False}
    task_repository = object()
    approval_repository = object()
    audit_repository = object()

    class RuntimeStub:
        repositories = SimpleNamespace(
            agent_tasks=task_repository,
            approval_decisions=approval_repository,
            audit_events=audit_repository,
        )

        @classmethod
        def from_environment(cls):
            return cls()

        def open(self, *, wait: bool = False) -> None:
            assert wait is True
            state["opened"] = True

        def probe(self):
            state["probed"] = True
            schema = SimpleNamespace(ready=True, missing_migration_ids=())
            return SimpleNamespace(ready=True, schema=schema)

        def close(self) -> None:
            state["closed"] = True

    def run_flow(**kwargs):
        state["called"] = True
        assert kwargs["task_repository"] is task_repository
        assert kwargs["approval_repository"] is approval_repository
        assert kwargs["audit_repository"] is audit_repository
        return orchestrator_module.AgentWorkerOrchestrationResult(success=True, task_id=kwargs["task_id"])

    monkeypatch.setenv("AIXION_PROFILE", "local")
    monkeypatch.setattr(orchestrator_module, "PostgresRuntime", RuntimeStub)
    monkeypatch.setattr(orchestrator_module, "run_approved_agent_task_worker_flow", run_flow)

    exit_code = orchestrator_module.main(["--task-id", "task", "--postgres", "--json"])

    assert exit_code == 0
    assert state == {"opened": True, "probed": True, "closed": True, "called": True}
    assert '"success": true' in capsys.readouterr().out


def test_cli_postgres_auth_mode_selects_runtime_without_flag(monkeypatch, capsys) -> None:
    state = {"opened": False, "probed": False, "closed": False, "called": False}
    task_repository = object()
    approval_repository = object()
    audit_repository = object()

    class RuntimeStub:
        repositories = SimpleNamespace(
            agent_tasks=task_repository,
            approval_decisions=approval_repository,
            audit_events=audit_repository,
        )

        @classmethod
        def from_environment(cls):
            return cls()

        def open(self, *, wait: bool = False) -> None:
            assert wait is True
            state["opened"] = True

        def probe(self):
            state["probed"] = True
            schema = SimpleNamespace(ready=True, missing_migration_ids=())
            return SimpleNamespace(ready=True, schema=schema)

        def close(self) -> None:
            state["closed"] = True

    def run_flow(**kwargs):
        state["called"] = True
        assert kwargs["task_repository"] is task_repository
        assert kwargs["approval_repository"] is approval_repository
        assert kwargs["audit_repository"] is audit_repository
        return orchestrator_module.AgentWorkerOrchestrationResult(
            success=True, task_id=kwargs["task_id"]
        )

    store.reset()
    monkeypatch.setattr(
        orchestrator_module,
        "get_settings",
        lambda: SimpleNamespace(
            profile="local", is_production=False, postgres_auth_enabled=True
        ),
    )
    monkeypatch.setattr(orchestrator_module, "PostgresRuntime", RuntimeStub)
    monkeypatch.setattr(orchestrator_module, "run_approved_agent_task_worker_flow", run_flow)

    exit_code = orchestrator_module.main(["--task-id", "task", "--json"])

    assert exit_code == 0
    assert state == {"opened": True, "probed": True, "closed": True, "called": True}
    assert '"success": true' in capsys.readouterr().out
    assert store.agent_tasks == {}
    assert store.agent_task_events == {}
    assert store.audit_events == []


def test_cli_postgres_auth_mode_unready_runtime_fails_closed_without_worker_call(
    monkeypatch, capsys,
) -> None:
    state = {"opened": False, "probed": False, "closed": False}

    class UnreadyRuntimeStub:
        repositories = SimpleNamespace(
            agent_tasks=object(),
            approval_decisions=object(),
            audit_events=object(),
        )

        @classmethod
        def from_environment(cls):
            return cls()

        def open(self, *, wait: bool = False) -> None:
            assert wait is True
            state["opened"] = True

        def probe(self):
            state["probed"] = True
            schema = SimpleNamespace(
                ready=False, missing_migration_ids=("20261030_worker_schema",)
            )
            return SimpleNamespace(ready=False, schema=schema)

        def close(self) -> None:
            state["closed"] = True

    def unexpected_worker_call(**_kwargs):
        pytest.fail("an unready PostgreSQL runtime must not start worker flow")

    store.reset()
    monkeypatch.setattr(
        orchestrator_module,
        "get_settings",
        lambda: SimpleNamespace(
            profile="local", is_production=False, postgres_auth_enabled=True
        ),
    )
    monkeypatch.setattr(orchestrator_module, "PostgresRuntime", UnreadyRuntimeStub)
    monkeypatch.setattr(
        orchestrator_module,
        "run_approved_agent_task_worker_flow",
        unexpected_worker_call,
    )

    exit_code = orchestrator_module.main(["--task-id", "task", "--json"])

    assert exit_code == 1
    assert state == {"opened": True, "probed": True, "closed": True}
    output = capsys.readouterr().out
    assert '"success": false' in output
    assert "PostgreSQL schema is not ready" in output
    assert "20261030_worker_schema" in output
    assert store.agent_tasks == {}
    assert store.agent_task_events == {}
    assert store.audit_events == []
