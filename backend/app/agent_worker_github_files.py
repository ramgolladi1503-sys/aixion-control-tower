from __future__ import annotations

import argparse
import base64
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

import httpx

from .agent_task_models import AgentTask, AgentTaskEvent, AgentTaskEventType
from .agent_task_repository import AgentTaskRepository
from .agent_worker_branch_plan import _planned_branch_for_task, validate_branch_plan
from .agent_worker_claims import (
    claim_agent_task_for_worker,
    claim_first_approved_agent_task_for_worker,
    commit_agent_task_worker_outcome,
)
from .agent_worker_file_plan import validate_file_patch_plan
from .approval_decisions import agent_task_repository, approval_decision_repository
from .approval_repository import ApprovalDecisionRepository
from .models import ApprovalRequest, FileChange

GITHUB_API_BASE_URL = "https://api.github.com"


@dataclass
class GitHubFileApplyItem:
    path: str
    change_type: str
    commit_sha: str


@dataclass
class GitHubFileApplyResult:
    repository: str
    branch: str
    files_written: int
    files_deleted: int
    commits_created: int
    items: list[GitHubFileApplyItem]


class GitHubFileClient(Protocol):
    def branch_exists(self, repository: str, branch: str) -> bool: ...

    def get_file_sha(self, repository: str, path: str, branch: str) -> str | None: ...

    def create_or_update_file(
        self,
        repository: str,
        path: str,
        branch: str,
        content: str,
        message: str,
        sha: str | None = None,
    ) -> str: ...

    def delete_file(self, repository: str, path: str, branch: str, message: str, sha: str) -> str: ...


class GitHubRestFileClient:
    def __init__(self, token: str, *, base_url: str = GITHUB_API_BASE_URL, timeout_seconds: float = 20.0) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def branch_exists(self, repository: str, branch: str) -> bool:
        response = httpx.get(
            f"{self.base_url}/repos/{repository}/git/ref/heads/{branch}",
            headers=self._headers(),
            timeout=self.timeout_seconds,
        )
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        raise RuntimeError(f"GitHub branch existence check failed: {response.status_code} {response.text[:300]}")

    def get_file_sha(self, repository: str, path: str, branch: str) -> str | None:
        response = httpx.get(
            f"{self.base_url}/repos/{repository}/contents/{path}",
            headers=self._headers(),
            params={"ref": branch},
            timeout=self.timeout_seconds,
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise RuntimeError(f"GitHub file lookup failed for {path}: {response.status_code} {response.text[:300]}")
        payload = response.json()
        if payload.get("type") != "file":
            raise RuntimeError(f"GitHub path is not a file: {path}")
        sha = payload.get("sha")
        if not sha:
            raise RuntimeError(f"GitHub file response missing sha for {path}")
        return str(sha)

    def create_or_update_file(
        self,
        repository: str,
        path: str,
        branch: str,
        content: str,
        message: str,
        sha: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha
        response = httpx.put(
            f"{self.base_url}/repos/{repository}/contents/{path}",
            headers=self._headers(),
            json=payload,
            timeout=self.timeout_seconds,
        )
        if response.status_code not in {200, 201}:
            raise RuntimeError(f"GitHub file write failed for {path}: {response.status_code} {response.text[:300]}")
        commit_sha = response.json().get("commit", {}).get("sha")
        if not commit_sha:
            raise RuntimeError(f"GitHub file write response missing commit sha for {path}")
        return str(commit_sha)

    def delete_file(self, repository: str, path: str, branch: str, message: str, sha: str) -> str:
        response = httpx.request(
            "DELETE",
            f"{self.base_url}/repos/{repository}/contents/{path}",
            headers=self._headers(),
            json={"message": message, "sha": sha, "branch": branch},
            timeout=self.timeout_seconds,
        )
        if response.status_code != 200:
            raise RuntimeError(f"GitHub file delete failed for {path}: {response.status_code} {response.text[:300]}")
        commit_sha = response.json().get("commit", {}).get("sha")
        if not commit_sha:
            raise RuntimeError(f"GitHub file delete response missing commit sha for {path}")
        return str(commit_sha)


@dataclass
class AgentWorkerGitHubFilesResult:
    success: bool
    task_id: str | None = None
    approval_request_id: str | None = None
    worker_id: str = "agent-worker-github-files"
    lease_token: str | None = None
    repository: str | None = None
    branch: str | None = None
    files_written: int = 0
    files_deleted: int = 0
    commits_created: int = 0
    items: list[GitHubFileApplyItem] = field(default_factory=list)
    event_id: str | None = None
    final_status: str | None = None
    reason: str = ""
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["generated_at"] = self.generated_at.isoformat()
        return payload


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _linked_approval(
    task: AgentTask,
    repository: ApprovalDecisionRepository | None = None,
) -> ApprovalRequest | None:
    if not task.approval_request_id:
        return None
    if repository is not None:
        return repository.get(task.approval_request_id)
    return approval_decision_repository.get(task.approval_request_id)


def _default_client() -> GitHubRestFileClient:
    token = os.getenv("GITHUB_" + "TOKEN", "").strip()
    if not token:
        raise RuntimeError("GITHUB_TOKEN is required for GitHub file application")
    return GitHubRestFileClient(token)


def _commit_message(change: FileChange, task: AgentTask) -> str:
    return f"Apply approved AgentTask change: {change.change_type.lower().strip()} {change.path.strip()} ({task.id})"


def _apply_file_changes(
    *,
    task: AgentTask,
    approval: ApprovalRequest,
    branch: str,
    client: GitHubFileClient,
) -> GitHubFileApplyResult:
    assert task.repository is not None
    if not client.branch_exists(task.repository, branch):
        raise RuntimeError(f"Target branch does not exist: {branch}")

    items: list[GitHubFileApplyItem] = []
    files_written = 0
    files_deleted = 0
    for change in approval.files:
        path = change.path.strip()
        change_type = change.change_type.lower().strip()
        current_sha = client.get_file_sha(task.repository, path, branch)
        if change_type == "create" and current_sha is not None:
            raise RuntimeError(f"Create refused because file already exists: {path}")
        if change_type in {"update", "delete"} and current_sha is None:
            raise RuntimeError(f"{change_type.title()} refused because file does not exist: {path}")
        if change_type in {"create", "update"}:
            assert change.new_content is not None
            commit_sha = client.create_or_update_file(
                task.repository,
                path,
                branch,
                change.new_content,
                _commit_message(change, task),
                sha=current_sha,
            )
            files_written += 1
        else:
            assert current_sha is not None
            commit_sha = client.delete_file(task.repository, path, branch, _commit_message(change, task), current_sha)
            files_deleted += 1
        items.append(GitHubFileApplyItem(path=path, change_type=change_type, commit_sha=commit_sha))

    return GitHubFileApplyResult(
        repository=task.repository,
        branch=branch,
        files_written=files_written,
        files_deleted=files_deleted,
        commits_created=len(items),
        items=items,
    )


def _append_files_applied_event(
    task: AgentTask,
    approval: ApprovalRequest,
    worker_id: str,
    result: GitHubFileApplyResult,
) -> AgentTaskEvent:
    event = AgentTaskEvent(
        task_id=task.id,
        event_type=AgentTaskEventType.NOTE,
        message="Worker applied approved file changes to the safe feature branch. No pull request was opened.",
        status=task.status,
        actor=worker_id,
        metadata={
            "worker_id": worker_id,
            "approval_request_id": approval.id,
            "repository": result.repository,
            "branch": result.branch,
            "files_written": result.files_written,
            "files_deleted": result.files_deleted,
            "commits_created": result.commits_created,
            "items": [asdict(item) for item in result.items],
            "repository_mutated": True,
            "pull_request_opened": False,
            "operation_type": "github_file_application",
        },
    )
    return event


def run_agent_worker_github_file_application(
    *,
    task_id: str | None = None,
    first_approved: bool = False,
    worker_id: str = "agent-worker-github-files",
    lease_seconds: int = 300,
    client: GitHubFileClient | None = None,
    task_repository: AgentTaskRepository | None = None,
    approval_repository: ApprovalDecisionRepository | None = None,
) -> AgentWorkerGitHubFilesResult:
    if not task_id and not first_approved:
        return AgentWorkerGitHubFilesResult(success=False, worker_id=worker_id, reason="Provide task_id or set first_approved=true.")

    active_task_repository = task_repository or agent_task_repository
    active_approval_repository = approval_repository or approval_decision_repository
    claim = (
        claim_first_approved_agent_task_for_worker(
            worker_id=worker_id, lease_seconds=lease_seconds, repository=active_task_repository
        )
        if first_approved and not task_id
        else claim_agent_task_for_worker(
            task_id=str(task_id), worker_id=worker_id, lease_seconds=lease_seconds,
            repository=active_task_repository,
        )
    )
    if not claim.success or claim.task is None:
        return AgentWorkerGitHubFilesResult(
            success=False,
            task_id=claim.task_id,
            worker_id=worker_id,
            lease_token=claim.lease_token,
            final_status=claim.task.status if claim.task else None,
            reason=claim.reason,
        )

    task = claim.task
    lease_token = claim.lease_token
    branch = _planned_branch_for_task(task)
    approval = _linked_approval(task, active_approval_repository)
    validation_error = validate_branch_plan(task) or validate_file_patch_plan(approval)
    if validation_error:
        try:
            commit_agent_task_worker_outcome(
                task, worker_id=worker_id, repository=active_task_repository,
                audit_type="agent_worker.github_files_refused",
                details={"reason": validation_error},
            )
        except Exception as error:  # noqa: BLE001 - do not mask an uncommitted lease release.
            validation_error = f"{validation_error} Durable worker outcome could not be committed: {error}"
        return AgentWorkerGitHubFilesResult(
            success=False,
            task_id=task.id,
            approval_request_id=task.approval_request_id,
            worker_id=worker_id,
            lease_token=lease_token,
            repository=task.repository,
            branch=branch,
            final_status=task.status,
            reason=validation_error,
        )

    assert approval is not None
    try:
        github = client or _default_client()
        applied = _apply_file_changes(task=task, approval=approval, branch=branch, client=github)
    except Exception as error:  # noqa: BLE001 - report worker failure cleanly.
        failure_reason = str(error)
        try:
            commit_agent_task_worker_outcome(
                task, worker_id=worker_id, repository=active_task_repository,
                audit_type="agent_worker.github_files_failed",
                details={"repository": task.repository, "branch": branch, "reason": failure_reason},
            )
        except Exception as commit_error:  # noqa: BLE001 - preserve ambiguity after external writes.
            failure_reason = f"{failure_reason} Durable worker outcome could not be committed: {commit_error}"
        return AgentWorkerGitHubFilesResult(
            success=False,
            task_id=task.id,
            approval_request_id=approval.id,
            worker_id=worker_id,
            lease_token=lease_token,
            repository=task.repository,
            branch=branch,
            final_status=task.status,
            reason=failure_reason,
        )

    event = _append_files_applied_event(task, approval, worker_id, applied)
    try:
        commit_agent_task_worker_outcome(
            task, worker_id=worker_id, repository=active_task_repository,
            audit_type="agent_worker.github_files_applied",
            details={
                "approval_request_id": approval.id,
                "repository": applied.repository,
                "branch": applied.branch,
                "files_written": applied.files_written,
                "files_deleted": applied.files_deleted,
                "commits_created": applied.commits_created,
            },
            event=event,
        )
    except Exception as error:  # noqa: BLE001 - external writes may have happened; never report durable success.
        return AgentWorkerGitHubFilesResult(
            success=False,
            task_id=task.id,
            approval_request_id=approval.id,
            worker_id=worker_id,
            lease_token=lease_token,
            repository=applied.repository,
            branch=applied.branch,
            files_written=applied.files_written,
            files_deleted=applied.files_deleted,
            commits_created=applied.commits_created,
            items=applied.items,
            event_id=event.id,
            final_status=task.status,
            reason=f"File changes may have been applied, but their durable outcome could not be committed: {error}",
        )

    return AgentWorkerGitHubFilesResult(
        success=True,
        task_id=task.id,
        approval_request_id=approval.id,
        worker_id=worker_id,
        lease_token=lease_token,
        repository=applied.repository,
        branch=applied.branch,
        files_written=applied.files_written,
        files_deleted=applied.files_deleted,
        commits_created=applied.commits_created,
        items=applied.items,
        event_id=event.id,
        final_status=task.status,
        reason="Approved file changes applied to feature branch. No pull request opened.",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apply approved file changes to a safe GitHub feature branch.")
    parser.add_argument("--task-id", default=None, help="Specific AgentTask id to apply files for.")
    parser.add_argument("--first-approved", action="store_true", help="Use the first approved task with a linked approval.")
    parser.add_argument("--worker-id", default="agent-worker-github-files", help="Worker id written into task events.")
    parser.add_argument("--lease-seconds", type=int, default=300, help="Claim lease duration in seconds.")
    parser.add_argument("--json", action="store_true", help="Print JSON result.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    result = run_agent_worker_github_file_application(
        task_id=args.task_id,
        first_approved=args.first_approved,
        worker_id=args.worker_id,
        lease_seconds=args.lease_seconds,
    )
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        decision = "PASS" if result.success else "FAILED"
        print(f"Agent worker GitHub file application: {decision}")
        print(f"Task: {result.task_id or 'n/a'}")
        print(f"Approval: {result.approval_request_id or 'n/a'}")
        print(f"Repository: {result.repository or 'n/a'}")
        print(f"Branch: {result.branch or 'n/a'}")
        print(f"Reason: {result.reason}")
    return 0 if result.success else 1
