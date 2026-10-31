from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from app import agent_worker_claims as worker_claims
from app import agent_worker_orchestrator as orchestrator
from app.agent_task_models import AgentTask, AgentTaskStatus
from app.agent_task_repository import PostgresAgentTaskRepository, SQLiteAgentTaskRepository
from app.agent_worker_container_validation import ContainerValidationPolicy
from app.agent_worker_github_branch import GitHubBranchCreateResult
from app.agent_worker_github_pr import GitHubPullRequestCreateResult
from app.agent_worker_orchestrator import run_approved_agent_task_worker_flow
from app.agent_worker_validation_runner import CommandExecutionResult
from app.approval_repository import PostgresApprovalDecisionRepository, SQLiteApprovalDecisionRepository
from app.models import AgentProvider, ApprovalRequest, FileChange, Project, RiskAssessment, RiskLevel
from app.postgres_audit_repository import PostgresAuditEventRepository
from app.store import store


class FakeBranchClient:
    def __init__(self) -> None:
        self.branches: set[str] = set()
        self.calls: list[str] = []

    def branch_exists(self, repository: str, branch: str) -> bool:
        self.calls.append(f"exists:{branch}")
        return branch in self.branches

    def get_branch_sha(self, repository: str, branch: str) -> str:
        self.calls.append(f"sha:{branch}")
        return "source-sha"

    def create_branch(self, repository: str, branch: str, source_sha: str) -> GitHubBranchCreateResult:
        self.calls.append(f"create:{branch}")
        self.branches.add(branch)
        return GitHubBranchCreateResult(
            repository=repository,
            branch=branch,
            source_branch="main",
            source_sha=source_sha,
            branch_created=True,
            html_url=f"https://github.com/{repository}/tree/{branch}",
        )


class FakeFileClient:
    def __init__(self, branch_client: FakeBranchClient) -> None:
        self.branch_client = branch_client
        self.files: dict[str, str] = {}
        self.calls: list[str] = []
        self.commit_counter = 0

    def branch_exists(self, repository: str, branch: str) -> bool:
        self.calls.append(f"branch:{branch}")
        return branch in self.branch_client.branches

    def get_file_sha(self, repository: str, path: str, branch: str) -> str | None:
        self.calls.append(f"sha:{path}")
        return self.files.get(path)

    def create_or_update_file(
        self,
        repository: str,
        path: str,
        branch: str,
        content: str,
        message: str,
        sha: str | None = None,
    ) -> str:
        self.calls.append(f"write:{path}")
        self.commit_counter += 1
        self.files[path] = f"file-sha-{self.commit_counter}"
        return f"commit-{self.commit_counter}"

    def delete_file(self, repository: str, path: str, branch: str, message: str, sha: str) -> str:
        self.calls.append(f"delete:{path}")
        self.commit_counter += 1
        self.files.pop(path, None)
        return f"commit-delete-{self.commit_counter}"


class FakePRClient:
    def __init__(self, branch_client: FakeBranchClient) -> None:
        self.branch_client = branch_client
        self.calls: list[str] = []

    def branch_exists(self, repository: str, branch: str) -> bool:
        self.calls.append(f"branch:{branch}")
        return branch in self.branch_client.branches

    def open_pull_request_exists(self, repository: str, head_branch: str, base_branch: str) -> bool:
        self.calls.append(f"lookup:{head_branch}->{base_branch}")
        return False

    def create_pull_request(
        self,
        repository: str,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str,
    ) -> GitHubPullRequestCreateResult:
        self.calls.append(f"create-pr:{head_branch}->{base_branch}")
        return GitHubPullRequestCreateResult(
            repository=repository,
            number=99,
            title=title,
            head_branch=head_branch,
            base_branch=base_branch,
            html_url=f"https://github.com/{repository}/pull/99",
        )


class FakeWorkspaceRunner:
    def run(self, command: list[str], *, cwd=None):
        from app.agent_worker_workspace import WorkspaceCommandResult

        if command[:2] == ["git", "clone"]:
            Path(command[-1]).mkdir(parents=True, exist_ok=True)
        return WorkspaceCommandResult(command=command, exit_code=0, output_summary="ok")


class UnavailableContainerRuntime:
    def available(self) -> bool:
        return False

    def run_validation(self, *, command: str, cwd: Path, policy: ContainerValidationPolicy, timeout_seconds: int) -> CommandExecutionResult:
        raise AssertionError("container runtime should not run when unavailable")


class FakePostgresRuntimeConnector:
    def connect(self):
        raise AssertionError("test connector must not be invoked")


def setup_function() -> None:
    store.reset()


def _project() -> Project:
    project = Project(name="Worker Orchestrator", description="demo", workspace_id="workspace-worker-test")
    store.projects[project.id] = project
    return project


def _approval(project: Project) -> ApprovalRequest:
    approval = ApprovalRequest(
        project_id=project.id,
        workspace_id=project.workspace_id,
        title="Approved orchestrated worker output",
        summary="Run worker steps in order.",
        agent_name="orchestrator",
        target_branch="feature/orchestrated-worker",
        files=[FileChange(path="docs/orchestrated.md", change_type="create", diff="+new", new_content="new\n")],
        test_plan=["python -m pytest"],
        rollback_plan="Close PR or revert branch.",
        risk=RiskAssessment(level=RiskLevel.LOW),
        status="APPROVED",
    )
    store.approval_requests[approval.id] = approval
    return approval


def _approved_task(approval: ApprovalRequest) -> AgentTask:
    task = AgentTask(
        provider=AgentProvider.CODEX,
        project_id=approval.project_id,
        workspace_id=approval.workspace_id,
        title="Run orchestrated worker",
        goal="Branch, apply files, validate, and open PR.",
        repository="ramgolladi1503-sys/aixion-control-tower",
        branch_preference="feature/orchestrated-worker",
        status=AgentTaskStatus.APPROVED,
        approval_request_id=approval.id,
    )
    store.agent_tasks[task.id] = task
    store.persist()
    return task


def test_orchestrator_rejects_different_postgres_runtimes_before_task_lookup_or_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_repository = PostgresAgentTaskRepository(FakePostgresRuntimeConnector().connect)
    approval_repository = PostgresApprovalDecisionRepository(FakePostgresRuntimeConnector().connect)
    lookups: list[object] = []
    audit_before = list(store.audit_events)

    def unexpected_task_lookup(task_id, repository=None):
        lookups.append((task_id, repository))
        pytest.fail("different PostgreSQL runtimes must be rejected before task lookup")

    monkeypatch.setattr(orchestrator, "_task", unexpected_task_lookup)

    with pytest.raises(ValueError, match="share the same PostgreSQL connection factory"):
        run_approved_agent_task_worker_flow(
            task_id="postgres-task",
            task_repository=task_repository,
            approval_repository=approval_repository,
        )

    assert lookups == []
    assert store.audit_events == audit_before


def test_orchestrator_accepts_same_runtime_postgres_pair_before_task_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = FakePostgresRuntimeConnector()
    task_repository = PostgresAgentTaskRepository(runtime.connect)
    approval_repository = PostgresApprovalDecisionRepository(runtime.connect)
    audit_repository = PostgresAuditEventRepository(runtime.connect)
    lookups: list[object] = []

    def expected_task_lookup(task_id, repository=None):
        lookups.append((task_id, repository))
        return None

    monkeypatch.setattr(orchestrator, "_task", expected_task_lookup)
    result = run_approved_agent_task_worker_flow(
        task_id="postgres-task",
        task_repository=task_repository,
        approval_repository=approval_repository,
        audit_repository=audit_repository,
    )

    assert result.success is False
    assert result.reason == "Agent task not found."
    assert lookups == [("postgres-task", task_repository)]


def test_orchestrator_rejects_foreign_postgres_audit_runtime_before_task_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = FakePostgresRuntimeConnector()
    task_repository = PostgresAgentTaskRepository(runtime.connect)
    approval_repository = PostgresApprovalDecisionRepository(runtime.connect)
    audit_repository = PostgresAuditEventRepository(FakePostgresRuntimeConnector().connect)
    audit_before = list(store.audit_events)

    def unexpected_task_lookup(task_id, repository=None):
        pytest.fail("foreign PostgreSQL audit runtime must be rejected before task lookup")

    monkeypatch.setattr(orchestrator, "_task", unexpected_task_lookup)

    with pytest.raises(ValueError, match="must share the same runtime connection factory"):
        run_approved_agent_task_worker_flow(
            task_id="postgres-task",
            task_repository=task_repository,
            approval_repository=approval_repository,
            audit_repository=audit_repository,
        )

    assert store.audit_events == audit_before


@pytest.mark.parametrize("repository_mode", ["missing", "sqlite"])
def test_orchestrator_requires_postgres_pair_in_postgres_auth_mode_before_task_lookup(
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
    lookups: list[object] = []

    def unexpected_task_lookup(task_id, repository=None):
        lookups.append((task_id, repository))
        pytest.fail("PostgreSQL mode mismatch must be rejected before task lookup")

    monkeypatch.setattr(orchestrator, "_task", unexpected_task_lookup)

    with pytest.raises(ValueError, match="PostgreSQL task and approval repositories"):
        run_approved_agent_task_worker_flow(
            task_id="must-not-read-local-store",
            task_repository=task_repository,
            approval_repository=approval_repository,
        )

    assert lookups == []


def test_orchestrator_runs_branch_files_validation_and_pr_steps() -> None:
    project = _project()
    approval = _approval(project)
    task = _approved_task(approval)
    branch_client = FakeBranchClient()
    file_client = FakeFileClient(branch_client)
    pr_client = FakePRClient(branch_client)
    validation_calls: list[str] = []

    def fake_validation(command: str) -> CommandExecutionResult:
        validation_calls.append(command)
        return CommandExecutionResult(command=command, exit_code=0, output_summary="passed")

    result = run_approved_agent_task_worker_flow(
        task_id=task.id,
        worker_id="orchestrator",
        branch_client=branch_client,
        file_client=file_client,
        pr_client=pr_client,
        validation_executor=fake_validation,
    )

    assert result.success is True
    assert result.task_id == task.id
    assert result.branch_result is not None and result.branch_result.success is True
    assert result.file_result is not None and result.file_result.success is True
    assert result.validation_result is not None and result.validation_result.success is True
    assert result.pr_result is not None and result.pr_result.success is True
    assert result.pr_result.pull_request_url == "https://github.com/ramgolladi1503-sys/aixion-control-tower/pull/99"
    assert validation_calls == ["python -m pytest"]
    assert store.agent_tasks[task.id].status == AgentTaskStatus.READY_FOR_PR
    assert result.summary_event_id in store.agent_task_events

    summary = store.agent_task_events[result.summary_event_id]
    assert summary.event_type == "DONE"
    assert summary.metadata["orchestration_success"] is True
    assert summary.metadata["pull_request_url"] == result.pr_result.pull_request_url
    assert any(audit.event_type == "agent_worker.orchestration_started" for audit in store.audit_events)
    assert any(audit.event_type == "agent_worker.orchestration_completed" for audit in store.audit_events)


def test_orchestrator_stops_when_validation_fails() -> None:
    project = _project()
    approval = _approval(project)
    task = _approved_task(approval)
    branch_client = FakeBranchClient()
    file_client = FakeFileClient(branch_client)
    pr_client = FakePRClient(branch_client)

    def failing_validation(command: str) -> CommandExecutionResult:
        return CommandExecutionResult(command=command, exit_code=1, output_summary="failed")

    result = run_approved_agent_task_worker_flow(
        task_id=task.id,
        worker_id="orchestrator",
        branch_client=branch_client,
        file_client=file_client,
        pr_client=pr_client,
        validation_executor=failing_validation,
    )

    assert result.success is False
    assert result.reason == "Validation step failed: Validation commands failed."
    assert result.pr_result is None
    assert pr_client.calls == []
    assert store.agent_tasks[task.id].status == AgentTaskStatus.FAILED
    summary = store.agent_task_events[result.summary_event_id]
    assert summary.event_type == "FAILED"
    assert summary.metadata["orchestration_success"] is False
    assert any(audit.event_type == "agent_worker.orchestration_failed" for audit in store.audit_events)


def test_orchestrator_defaults_to_container_validation_and_fails_closed_without_runtime() -> None:
    project = _project()
    approval = _approval(project)
    task = _approved_task(approval)
    branch_client = FakeBranchClient()
    file_client = FakeFileClient(branch_client)
    pr_client = FakePRClient(branch_client)

    result = run_approved_agent_task_worker_flow(
        task_id=task.id,
        worker_id="orchestrator",
        branch_client=branch_client,
        file_client=file_client,
        pr_client=pr_client,
        workspace_runner=FakeWorkspaceRunner(),
        container_runtime=UnavailableContainerRuntime(),
    )

    assert result.success is False
    assert result.pr_result is None
    assert result.validation_result is not None
    assert result.validation_result.results[0].exit_code == 127
    assert "Container runtime unavailable" in result.validation_result.results[0].output_summary
    assert store.agent_tasks[task.id].status == AgentTaskStatus.FAILED
    assert any(audit.event_type == "agent_worker.container_validation_configured" for audit in store.audit_events)


def test_orchestrator_reports_missing_task() -> None:
    result = run_approved_agent_task_worker_flow(task_id="agent_task_missing")

    assert result.success is False
    assert result.reason == "Agent task not found."
