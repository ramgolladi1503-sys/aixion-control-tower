from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from app.agent_task_models import AgentTask, AgentTaskStatus
from app.agent_worker_github_files import run_agent_worker_github_file_application
from app.models import AgentProvider, ApprovalRequest, FileChange, Project, RiskAssessment, RiskLevel
from app.store import store


class FakeGitHubFileClient:
    def __init__(self, *, branch_exists: bool = True, files: dict[str, str] | None = None) -> None:
        self.existing_branch = branch_exists
        self.files = files or {}
        self.branch_checks: list[tuple[str, str]] = []
        self.sha_checks: list[tuple[str, str, str]] = []
        self.write_calls: list[tuple[str, str, str, str | None]] = []
        self.delete_calls: list[tuple[str, str, str, str]] = []
        self.commit_counter = 0

    def branch_exists(self, repository: str, branch: str) -> bool:
        self.branch_checks.append((repository, branch))
        return self.existing_branch

    def get_file_sha(self, repository: str, path: str, branch: str) -> str | None:
        self.sha_checks.append((repository, path, branch))
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
        self.write_calls.append((repository, path, branch, sha))
        self.commit_counter += 1
        self.files[path] = f"sha-{path}-{self.commit_counter}"
        return f"commit-write-{self.commit_counter}"

    def delete_file(self, repository: str, path: str, branch: str, message: str, sha: str) -> str:
        self.delete_calls.append((repository, path, branch, sha))
        self.commit_counter += 1
        self.files.pop(path, None)
        return f"commit-delete-{self.commit_counter}"


def setup_function() -> None:
    store.reset()


def _project() -> Project:
    project = Project(name="GitHub Files Worker", description="demo")
    store.projects[project.id] = project
    return project


def _approval(project: Project, files: list[FileChange] | None = None) -> ApprovalRequest:
    approval = ApprovalRequest(
        project_id=project.id,
        title="Approved GitHub file changes",
        summary="Apply approved changes to a safe feature branch.",
        agent_name="github-files-worker",
        target_branch="feature/github-file-worker",
        files=files
        if files is not None
        else [FileChange(path="docs/example.md", change_type="create", diff="+new", new_content="new\n")],
        test_plan=["python -m pytest"],
        rollback_plan="Revert generated branch.",
        risk=RiskAssessment(level=RiskLevel.LOW),
    )
    store.approval_requests[approval.id] = approval
    return approval


def _approved_task(approval: ApprovalRequest, *, branch_preference: str = "feature/github-file-worker") -> AgentTask:
    task = AgentTask(
        provider=AgentProvider.CODEX,
        project_id=approval.project_id,
        title="Apply files",
        goal="Apply approved files to GitHub branch.",
        repository="ramgolladi1503-sys/aixion-control-tower",
        branch_preference=branch_preference,
        status=AgentTaskStatus.APPROVED,
        approval_request_id=approval.id,
    )
    store.agent_tasks[task.id] = task
    store.persist()
    return task


def test_github_files_worker_creates_file_on_existing_branch() -> None:
    project = _project()
    approval = _approval(project)
    task = _approved_task(approval)
    fake = FakeGitHubFileClient()

    result = run_agent_worker_github_file_application(task_id=task.id, worker_id="files-worker", client=fake)

    assert result.success is True
    assert result.task_id == task.id
    assert result.approval_request_id == approval.id
    assert result.repository == "ramgolladi1503-sys/aixion-control-tower"
    assert result.branch == "feature/github-file-worker"
    assert result.files_written == 1
    assert result.files_deleted == 0
    assert result.commits_created == 1
    assert result.items[0].commit_sha == "commit-write-1"
    assert fake.branch_checks == [("ramgolladi1503-sys/aixion-control-tower", "feature/github-file-worker")]
    assert fake.write_calls == [("ramgolladi1503-sys/aixion-control-tower", "docs/example.md", "feature/github-file-worker", None)]
    assert fake.delete_calls == []
    assert store.agent_tasks[task.id].status == AgentTaskStatus.APPROVED
    assert store.agent_tasks[task.id].worker_lease_owner is None

    event = store.agent_task_events[result.event_id]
    assert event.event_type == "NOTE"
    assert event.metadata["files_written"] == 1
    assert event.metadata["files_deleted"] == 0
    assert event.metadata["commits_created"] == 1
    assert event.metadata["pull_request_opened"] is False
    assert any(audit.event_type == "agent_worker.github_files_applied" for audit in store.audit_events)


def test_github_files_worker_updates_existing_file() -> None:
    project = _project()
    approval = _approval(
        project,
        files=[FileChange(path="docs/example.md", change_type="update", diff="+updated", new_content="updated\n")],
    )
    task = _approved_task(approval)
    fake = FakeGitHubFileClient(files={"docs/example.md": "sha-existing"})

    result = run_agent_worker_github_file_application(task_id=task.id, client=fake)

    assert result.success is True
    assert result.files_written == 1
    assert fake.write_calls == [("ramgolladi1503-sys/aixion-control-tower", "docs/example.md", "feature/github-file-worker", "sha-existing")]


def test_github_files_worker_deletes_existing_file() -> None:
    project = _project()
    approval = _approval(project, files=[FileChange(path="docs/old.md", change_type="delete", diff="-old", new_content=None)])
    task = _approved_task(approval)
    fake = FakeGitHubFileClient(files={"docs/old.md": "sha-old"})

    result = run_agent_worker_github_file_application(task_id=task.id, client=fake)

    assert result.success is True
    assert result.files_written == 0
    assert result.files_deleted == 1
    assert fake.delete_calls == [("ramgolladi1503-sys/aixion-control-tower", "docs/old.md", "feature/github-file-worker", "sha-old")]


def test_github_files_worker_refuses_when_branch_missing() -> None:
    project = _project()
    approval = _approval(project)
    task = _approved_task(approval)
    fake = FakeGitHubFileClient(branch_exists=False)

    result = run_agent_worker_github_file_application(task_id=task.id, client=fake)

    assert result.success is False
    assert result.reason == "Target branch does not exist: feature/github-file-worker"
    assert fake.write_calls == []
    assert store.agent_tasks[task.id].worker_lease_owner is None
    assert any(audit.event_type == "agent_worker.github_files_failed" for audit in store.audit_events)


def test_github_files_worker_refuses_create_when_file_exists() -> None:
    project = _project()
    approval = _approval(project)
    task = _approved_task(approval)
    fake = FakeGitHubFileClient(files={"docs/example.md": "sha-existing"})

    result = run_agent_worker_github_file_application(task_id=task.id, client=fake)

    assert result.success is False
    assert result.reason == "Create refused because file already exists: docs/example.md"
    assert fake.write_calls == []


def test_github_files_worker_refuses_update_when_file_missing() -> None:
    project = _project()
    approval = _approval(
        project,
        files=[FileChange(path="docs/missing.md", change_type="update", diff="+updated", new_content="updated")],
    )
    task = _approved_task(approval)
    fake = FakeGitHubFileClient()

    result = run_agent_worker_github_file_application(task_id=task.id, client=fake)

    assert result.success is False
    assert result.reason == "Update refused because file does not exist: docs/missing.md"
    assert fake.write_calls == []


def test_github_files_worker_refuses_blocked_file_before_github_call() -> None:
    project = _project()
    approval = _approval(
        project,
        files=[FileChange(path="credentials.py", change_type="create", diff="+placeholder", new_content="placeholder\n")],
    )
    task = _approved_task(approval)
    fake = FakeGitHubFileClient()

    result = run_agent_worker_github_file_application(task_id=task.id, client=fake)

    assert result.success is False
    assert result.reason == "Unsafe file path: credentials.py"
    assert fake.branch_checks == []
    assert fake.write_calls == []
    assert any(audit.event_type == "agent_worker.github_files_refused" for audit in store.audit_events)


def test_github_files_worker_requires_task_selector() -> None:
    result = run_agent_worker_github_file_application()

    assert result.success is False
    assert result.reason == "Provide task_id or set first_approved=true."
