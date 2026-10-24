from __future__ import annotations

import os
from unittest.mock import Mock, patch

import httpx
import pytest

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from app.agent_task_models import AgentTask, AgentTaskStatus
from app.agent_task_repository import SQLiteAgentTaskRepository
from app.agent_worker_github_branch import (
    GitHubBranchCreateResult,
    GitHubBranchObservation,
    GitHubBranchPermissionError,
    GitHubBranchProviderError,
    GitHubBranchTransportError,
    GitHubRestBranchClient,
    run_agent_worker_github_branch_creation,
)
from app.models import ApprovalRequest, ApprovalStatus, AgentProvider, Project, RiskAssessment, RiskLevel
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
    project = Project(name="GitHub Branch Worker", description="demo", workspace_id="workspace-worker-test")
    store.projects[project.id] = project
    approval = ApprovalRequest(
        project_id=project.id,
        workspace_id=project.workspace_id,
        title="GitHub branch authorization",
        summary="Approval required before worker claims this task.",
        target_branch=branch_preference or "feature/github-branch-worker",
        files=[],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.APPROVED,
    )
    store.approval_requests[approval.id] = approval
    task = AgentTask(
        provider=AgentProvider.CODEX,
        project_id=project.id,
        workspace_id=project.workspace_id,
        title="Create GitHub branch",
        goal="Create a safe feature branch without file writes.",
        repository=repository,
        branch_preference=branch_preference,
        status=AgentTaskStatus.APPROVED,
        approval_request_id=approval.id,
    )
    store.agent_tasks[task.id] = task
    store.persist()
    return task


def test_github_branch_worker_creates_safe_branch_only() -> None:
    task = _approved_task()
    fake = FakeGitHubBranchClient(source_sha="sha-main")

    result = run_agent_worker_github_branch_creation(
        task_id=task.id, worker_id="branch-worker", client=fake,
        task_repository=SQLiteAgentTaskRepository(store._connect),
    )

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


def _github_response(status_code: int, payload: object = None, *, text: str = "") -> Mock:
    response = Mock()
    response.status_code = status_code
    response.text = text
    response.json.return_value = payload
    return response


def test_github_rest_observes_target_ref_sha_and_url() -> None:
    response = _github_response(
        200,
        {
            "object": {"sha": "deadbeef"},
            "_links": {"html": "https://github.com/acme/repo/tree/feature/demo"},
        },
    )
    client = GitHubRestBranchClient("token", base_url="https://api.example.test")

    with patch("app.agent_worker_github_branch.httpx.get", return_value=response) as get:
        observed = client.observe_branch("acme/repo", "feature/demo")

    assert observed == GitHubBranchObservation(
        repository="acme/repo",
        branch="feature/demo",
        found=True,
        sha="deadbeef",
        html_url="https://github.com/acme/repo/tree/feature/demo",
    )
    assert get.call_args.args[0] == "https://api.example.test/repos/acme/repo/git/ref/heads/feature/demo"
    assert get.call_args.kwargs["timeout"] == 20.0


def test_github_rest_observes_not_found_without_treating_as_failure() -> None:
    client = GitHubRestBranchClient("token")
    with patch("app.agent_worker_github_branch.httpx.get", return_value=_github_response(404)):
        observed = client.observe_branch("acme/repo", "feature/missing")

    assert observed == GitHubBranchObservation(
        repository="acme/repo", branch="feature/missing", found=False
    )


def test_github_rest_observation_classifies_permission_transport_and_provider_errors() -> None:
    client = GitHubRestBranchClient("token")
    for status_code in (401, 403):
        with patch(
            "app.agent_worker_github_branch.httpx.get",
            return_value=_github_response(status_code, text="denied"),
        ), pytest.raises(GitHubBranchPermissionError):
            client.observe_branch("acme/repo", "feature/demo")

    with patch(
        "app.agent_worker_github_branch.httpx.get", side_effect=httpx.ConnectError("offline")
    ), pytest.raises(GitHubBranchTransportError):
        client.observe_branch("acme/repo", "feature/demo")

    with patch(
        "app.agent_worker_github_branch.httpx.get", return_value=_github_response(503, text="unavailable")
    ), pytest.raises(GitHubBranchProviderError):
        client.observe_branch("acme/repo", "feature/demo")


def test_github_rest_observation_rejects_malformed_success_payload() -> None:
    client = GitHubRestBranchClient("token")
    response = _github_response(200, {"object": {"sha": "deadbeef"}, "_links": {}})
    with patch("app.agent_worker_github_branch.httpx.get", return_value=response), pytest.raises(
        GitHubBranchProviderError, match="valid GitHub HTML URL"
    ):
        client.observe_branch("acme/repo", "feature/demo")
