from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from fastapi.testclient import TestClient

from app.agent_task_models import AgentTaskStatus
from app.agent_worker_github_branch import GitHubBranchCreateResult
from app.agent_worker_github_pr import GitHubPullRequestCreateResult
from app.agent_worker_orchestrator import run_approved_agent_task_worker_flow
from app.agent_worker_validation_runner import CommandExecutionResult
from app.main import app
from app.models import Project
from app.store import store

client = TestClient(app)


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
            number=116,
            title=title,
            head_branch=head_branch,
            base_branch=base_branch,
            html_url=f"https://github.com/{repository}/pull/116",
        )


def setup_function() -> None:
    store.reset()


def _project() -> Project:
    project = Project(name="Product Regression", description="approval-to-worker regression")
    store.projects[project.id] = project
    store.persist()
    return project


def _create_agent_task(project: Project) -> dict:
    response = client.post(
        "/agent/tasks",
        json={
            "project_id": project.id,
            "provider": "CODEX",
            "title": "Create regression proof",
            "goal": "Apply an approved change and open a review PR.",
            "requested_action": "CREATE_APPROVAL",
            "repository": "ramgolladi1503-sys/aixion-control-tower",
            "branch_preference": "feature/product-regression-proof",
            "source_url": "https://chat.openai.com/product-regression",
            "source_session_id": "session_product_regression",
        },
    )
    assert response.status_code == 200
    return response.json()


def _approval_payload(project: Project) -> dict:
    return {
        "project_id": project.id,
        "title": "Approve regression proof",
        "summary": "Approved change should reach PR only after approval and validation.",
        "agent_name": "codex-agent",
        "target_branch": "feature/product-regression-proof",
        "files": [
            {
                "path": "docs/product-regression-proof.md",
                "change_type": "create",
                "diff": "+ product regression proof",
                "new_content": "product regression proof\n",
            }
        ],
        "test_plan": ["python -m pytest backend/tests/test_product_regression_flow.py"],
        "rollback_plan": "Close the generated pull request or delete the feature branch.",
    }


def _clients() -> tuple[FakeBranchClient, FakeFileClient, FakePRClient]:
    branch_client = FakeBranchClient()
    return branch_client, FakeFileClient(branch_client), FakePRClient(branch_client)


def _passing_validation(command: str) -> CommandExecutionResult:
    return CommandExecutionResult(command=command, exit_code=0, output_summary="product regression passed")


def test_approved_agent_task_runs_full_approval_to_pr_regression_flow() -> None:
    project = _project()
    task = _create_agent_task(project)
    approval = client.post(f"/agent/tasks/{task['id']}/approval", json=_approval_payload(project)).json()

    decision = client.post(f"/approvals/{approval['id']}/approve")
    assert decision.status_code == 200
    assert client.get(f"/agent/tasks/{task['id']}").json()["status"] == AgentTaskStatus.APPROVED

    branch_client, file_client, pr_client = _clients()
    result = run_approved_agent_task_worker_flow(
        task_id=task["id"],
        worker_id="product-regression-worker",
        branch_client=branch_client,
        file_client=file_client,
        pr_client=pr_client,
        validation_executor=_passing_validation,
    )

    updated_task = client.get(f"/agent/tasks/{task['id']}").json()
    events = client.get(f"/agent/tasks/{task['id']}/events").json()

    assert result.success is True
    assert updated_task["status"] == AgentTaskStatus.READY_FOR_PR
    assert result.pr_result is not None
    assert result.pr_result.pull_request_url == "https://github.com/ramgolladi1503-sys/aixion-control-tower/pull/116"
    assert branch_client.calls == [
        "exists:feature/product-regression-proof",
        "sha:main",
        "create:feature/product-regression-proof",
    ]
    assert "write:docs/product-regression-proof.md" in file_client.calls
    assert pr_client.calls[-1] == "create-pr:feature/product-regression-proof->main"
    assert any(event["event_type"] == "APPROVED" for event in events)
    assert any(event["event_type"] == "DONE" for event in events)
    assert any(audit.event_type == "agent_worker.orchestration_completed" for audit in store.audit_events)


def test_denied_agent_task_cannot_reach_worker_branch_file_validation_or_pr_steps() -> None:
    project = _project()
    task = _create_agent_task(project)
    approval = client.post(f"/agent/tasks/{task['id']}/approval", json=_approval_payload(project)).json()

    decision = client.post(f"/approvals/{approval['id']}/deny", json={"decision": "deny", "reason": "Not safe enough"})
    assert decision.status_code == 200
    assert client.get(f"/agent/tasks/{task['id']}").json()["status"] == AgentTaskStatus.DENIED

    validation_calls: list[str] = []

    def validation_should_not_run(command: str) -> CommandExecutionResult:
        validation_calls.append(command)
        return CommandExecutionResult(command=command, exit_code=0, output_summary="should not run")

    branch_client, file_client, pr_client = _clients()
    result = run_approved_agent_task_worker_flow(
        task_id=task["id"],
        worker_id="product-regression-worker",
        branch_client=branch_client,
        file_client=file_client,
        pr_client=pr_client,
        validation_executor=validation_should_not_run,
    )

    updated_task = client.get(f"/agent/tasks/{task['id']}").json()
    events = client.get(f"/agent/tasks/{task['id']}/events").json()

    assert result.success is False
    assert "Agent task is terminal: DENIED" in result.reason
    assert updated_task["status"] == AgentTaskStatus.DENIED
    assert branch_client.calls == []
    assert file_client.calls == []
    assert pr_client.calls == []
    assert validation_calls == []
    assert any(event["event_type"] == "DENIED" for event in events)
    assert any(event["event_type"] == "FAILED" for event in events)
    assert any(audit.event_type == "agent_worker.orchestration_failed" for audit in store.audit_events)
