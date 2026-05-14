from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from app.agent_task_models import AgentTask, AgentTaskStatus
from app.agent_worker_github_pr import GitHubPullRequestCreateResult, run_agent_worker_github_pr_creation
from app.models import AgentProvider, ApprovalRequest, FileChange, Project, RiskAssessment, RiskLevel
from app.store import store


class FakeGitHubPullRequestClient:
    def __init__(self, *, branch_exists: bool = True, open_pr_exists: bool = False) -> None:
        self.existing_branch = branch_exists
        self.existing_pr = open_pr_exists
        self.branch_checks: list[tuple[str, str]] = []
        self.open_pr_checks: list[tuple[str, str, str]] = []
        self.create_calls: list[tuple[str, str, str, str, str]] = []

    def branch_exists(self, repository: str, branch: str) -> bool:
        self.branch_checks.append((repository, branch))
        return self.existing_branch

    def open_pull_request_exists(self, repository: str, head_branch: str, base_branch: str) -> bool:
        self.open_pr_checks.append((repository, head_branch, base_branch))
        return self.existing_pr

    def create_pull_request(
        self,
        repository: str,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str,
    ) -> GitHubPullRequestCreateResult:
        self.create_calls.append((repository, title, body, head_branch, base_branch))
        return GitHubPullRequestCreateResult(
            repository=repository,
            number=42,
            title=title,
            head_branch=head_branch,
            base_branch=base_branch,
            html_url=f"https://github.com/{repository}/pull/42",
        )


def setup_function() -> None:
    store.reset()


def _project() -> Project:
    project = Project(name="GitHub PR Worker", description="demo")
    store.projects[project.id] = project
    return project


def _approval(project: Project) -> ApprovalRequest:
    approval = ApprovalRequest(
        project_id=project.id,
        title="Approved worker PR",
        summary="Open a pull request for approved worker output.",
        agent_name="github-pr-worker",
        target_branch="feature/github-pr-worker",
        files=[FileChange(path="docs/example.md", change_type="update", diff="+updated", new_content="updated\n")],
        test_plan=["python -m pytest"],
        rollback_plan="Close PR or revert branch.",
        risk=RiskAssessment(level=RiskLevel.LOW),
    )
    store.approval_requests[approval.id] = approval
    return approval


def _approved_task(approval: ApprovalRequest, *, branch_preference: str = "feature/github-pr-worker") -> AgentTask:
    task = AgentTask(
        provider=AgentProvider.CODEX,
        project_id=approval.project_id,
        title="Open worker PR",
        goal="Open a human-review pull request.",
        repository="ramgolladi1503-sys/aixion-control-tower",
        branch_preference=branch_preference,
        status=AgentTaskStatus.APPROVED,
        approval_request_id=approval.id,
    )
    store.agent_tasks[task.id] = task
    store.persist()
    return task


def test_github_pr_worker_opens_review_pr_and_marks_ready_for_pr() -> None:
    project = _project()
    approval = _approval(project)
    task = _approved_task(approval)
    fake = FakeGitHubPullRequestClient()

    result = run_agent_worker_github_pr_creation(task_id=task.id, worker_id="pr-worker", client=fake)

    assert result.success is True
    assert result.task_id == task.id
    assert result.approval_request_id == approval.id
    assert result.repository == "ramgolladi1503-sys/aixion-control-tower"
    assert result.head_branch == "feature/github-pr-worker"
    assert result.base_branch == "main"
    assert result.pull_request_number == 42
    assert result.pull_request_url == "https://github.com/ramgolladi1503-sys/aixion-control-tower/pull/42"
    assert fake.branch_checks == [("ramgolladi1503-sys/aixion-control-tower", "feature/github-pr-worker")]
    assert fake.open_pr_checks == [("ramgolladi1503-sys/aixion-control-tower", "feature/github-pr-worker", "main")]
    assert len(fake.create_calls) == 1
    assert fake.create_calls[0][1] == "Approved worker PR"
    assert store.agent_tasks[task.id].status == AgentTaskStatus.READY_FOR_PR
    assert store.agent_tasks[task.id].worker_lease_owner is None

    event = store.agent_task_events[result.event_id]
    assert event.event_type == "PR_CREATED"
    assert event.status == AgentTaskStatus.READY_FOR_PR
    assert event.metadata["pull_request_opened"] is True
    assert event.metadata["merged"] is False
    assert event.metadata["pull_request_url"] == result.pull_request_url
    assert any(audit.event_type == "agent_worker.github_pr_created" for audit in store.audit_events)


def test_github_pr_worker_refuses_unsafe_branch_before_github_call() -> None:
    project = _project()
    approval = _approval(project)
    task = _approved_task(approval, branch_preference="main")
    fake = FakeGitHubPullRequestClient()

    result = run_agent_worker_github_pr_creation(task_id=task.id, client=fake)

    assert result.success is False
    assert result.reason == "Planned branch is unsafe or protected: main"
    assert fake.branch_checks == []
    assert fake.create_calls == []
    assert store.agent_task_events == {}
    assert store.agent_tasks[task.id].worker_lease_owner is None
    assert any(audit.event_type == "agent_worker.github_pr_refused" for audit in store.audit_events)


def test_github_pr_worker_fails_when_head_branch_missing() -> None:
    project = _project()
    approval = _approval(project)
    task = _approved_task(approval)
    fake = FakeGitHubPullRequestClient(branch_exists=False)

    result = run_agent_worker_github_pr_creation(task_id=task.id, client=fake)

    assert result.success is False
    assert result.reason == "Head branch does not exist: feature/github-pr-worker"
    assert fake.open_pr_checks == []
    assert fake.create_calls == []
    assert store.agent_tasks[task.id].status == AgentTaskStatus.APPROVED
    assert store.agent_tasks[task.id].worker_lease_owner is None
    assert any(audit.event_type == "agent_worker.github_pr_failed" for audit in store.audit_events)


def test_github_pr_worker_fails_when_open_pr_already_exists() -> None:
    project = _project()
    approval = _approval(project)
    task = _approved_task(approval)
    fake = FakeGitHubPullRequestClient(open_pr_exists=True)

    result = run_agent_worker_github_pr_creation(task_id=task.id, client=fake)

    assert result.success is False
    assert result.reason == "Open pull request already exists for feature/github-pr-worker -> main"
    assert fake.create_calls == []
    assert store.agent_tasks[task.id].status == AgentTaskStatus.APPROVED
    assert store.agent_tasks[task.id].worker_lease_owner is None


def test_github_pr_worker_requires_task_selector() -> None:
    result = run_agent_worker_github_pr_creation()

    assert result.success is False
    assert result.reason == "Provide task_id or set first_approved=true."
