from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from app.agent_task_models import AgentTask, AgentTaskStatus
from app.agent_worker_github_branch import GitHubBranchCreateResult, run_agent_worker_github_branch_creation
from app.models import AgentProvider, Project
from app.store import store


class FakeGitHubBranchClient:
    def __init__(self, *, branch_exists: bool = False, source_sha: str = "abc123") -> None:
        self.existing_branch = branch_exists
        self.source_sha = source_sha
        self.exists_calls: list[tuple[str, str]] = []
        self.sha_calls: list[tuple[str, str]] = []
        self.create_calls: list[tuple[str, str, str]] = []

    def branch_exists(self, repository: str, branch: str) -> bool:
        self.exists_calls.append((repository, branch))
        return self.existing_branch

    def get_branch_sha(self, repository: str, branch: str) -> str:
        self.sha_calls.append((repository, branch))
        return self.source_sha

    def create_branch(self, repository: str, branch: str, source_sha: str) -> GitHubBranchCreateResult:
        self.create_calls.append((repository, branch, source_sha))
        return GitHubBranchCreateResult(
            repository=repository,
            branch=branch,
            source_branch="main",
            source_sha=source_sha,
            branch_created=True,
            html_url=f"https://github.com/{repository}/tree/{branch}",
        )


def setup_function() -> None:
    store.reset()


def _approved_task(*, repository: str | None = "ramgolladi1503-sys/aixion-control-tower", branch_preference: str | None = "feature/github-branch-worker") -> AgentTask:
    project = Project(name="GitHub Branch Worker", description="demo")
    store.projects[project.id] = project
    task = AgentTask(
        provider=AgentProvider.CODEX,
        project_id=project.id,
        title="Create GitHub branch",
        goal="Create a safe feature branch without file writes.",
        repository=repository,
        branch_preference=branch_preference,
        status=AgentTaskStatus.APPROVED,
        approval_request_id="approval_github_branch",
    )
    store.agent_tasks[task.id] = task
    store.persist()
    return task


def test_github_branch_worker_creates_safe_branch_only() -> None:
    task = _approved_task()
    fake = FakeGitHubBranchClient(source_sha="sha-main")

    result = run_agent_worker_github_branch_creation(task_id=task.id, worker_id="branch-worker", client=fake)

    assert result.success is True
    assert result.task_id == task.id
    assert result.repository == "ramgolladi1503-sys/aixion-control-tower"
    assert result.branch == "feature/github-branch-worker"
    assert result.source_sha == "sha-main"
    assert result.branch_created is True
    assert fake.exists_calls == [("ramgolladi1503-sys/aixion-control-tower", "feature/github-branch-worker")]
    assert fake.sha_calls == [("ramgolladi1503-sys/aixion-control-tower", "main")]
    assert fake.create_calls == [("ramgolladi1503-sys/aixion-control-tower", "feature/github-branch-worker", "sha-main")]
    assert store.agent_tasks[task.id].status == AgentTaskStatus.APPROVED
    assert store.agent_tasks[task.id].worker_lease_owner is None

    event = store.agent_task_events[result.event_id]
    assert event.event_type == "NOTE"
    assert event.metadata["branch_created"] is True
    assert event.metadata["repository_mutated"] is True
    assert event.metadata["files_written"] is False
    assert event.metadata["commits_created"] is False
    assert event.metadata["pull_request_opened"] is False
    assert any(audit.event_type == "agent_worker.github_branch_created" for audit in store.audit_events)


def test_github_branch_worker_refuses_unsafe_branch_before_github_call() -> None:
    task = _approved_task(branch_preference="main")
    fake = FakeGitHubBranchClient()

    result = run_agent_worker_github_branch_creation(task_id=task.id, client=fake)

    assert result.success is False
    assert result.reason == "Planned branch is unsafe or protected: main"
    assert fake.exists_calls == []
    assert fake.sha_calls == []
    assert fake.create_calls == []
    assert store.agent_task_events == {}
    assert store.agent_tasks[task.id].worker_lease_owner is None
    assert any(audit.event_type == "agent_worker.github_branch_refused" for audit in store.audit_events)


def test_github_branch_worker_refuses_missing_repository_before_github_call() -> None:
    task = _approved_task(repository=None)
    fake = FakeGitHubBranchClient()

    result = run_agent_worker_github_branch_creation(task_id=task.id, client=fake)

    assert result.success is False
    assert result.reason == "Agent task is missing repository."
    assert fake.exists_calls == []
    assert store.agent_tasks[task.id].worker_lease_owner is None


def test_github_branch_worker_fails_if_branch_already_exists() -> None:
    task = _approved_task()
    fake = FakeGitHubBranchClient(branch_exists=True)

    result = run_agent_worker_github_branch_creation(task_id=task.id, worker_id="branch-worker", client=fake)

    assert result.success is False
    assert result.reason == "Target branch already exists: feature/github-branch-worker"
    assert fake.exists_calls == [("ramgolladi1503-sys/aixion-control-tower", "feature/github-branch-worker")]
    assert fake.sha_calls == []
    assert fake.create_calls == []
    assert store.agent_tasks[task.id].status == AgentTaskStatus.APPROVED
    assert store.agent_tasks[task.id].worker_lease_owner is None
    assert any(audit.event_type == "agent_worker.github_branch_failed" for audit in store.audit_events)


def test_github_branch_worker_requires_task_selector() -> None:
    result = run_agent_worker_github_branch_creation()

    assert result.success is False
    assert result.reason == "Provide task_id or set first_approved=true."
