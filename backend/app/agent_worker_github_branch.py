from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

import httpx

from .agent_task_models import AgentTask, AgentTaskEvent, AgentTaskEventType
from .agent_task_repository import AgentTaskRepository
from .agent_worker_claims import (
    claim_agent_task_for_worker,
    claim_first_approved_agent_task_for_worker,
    commit_agent_task_worker_outcome,
)
from .approval_decisions import agent_task_repository
from .agent_worker_branch_plan import _planned_branch_for_task, validate_branch_plan

GITHUB_API_BASE_URL = "https://api.github.com"
DEFAULT_SOURCE_BRANCH = "main"


@dataclass
class GitHubBranchCreateResult:
    repository: str
    branch: str
    source_branch: str
    source_sha: str
    branch_created: bool
    html_url: str | None = None


class GitHubBranchClient(Protocol):
    def branch_exists(self, repository: str, branch: str) -> bool: ...

    def get_branch_sha(self, repository: str, branch: str) -> str: ...

    def create_branch(self, repository: str, branch: str, source_sha: str) -> GitHubBranchCreateResult: ...


class GitHubRestBranchClient:
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

    def _ref_url(self, repository: str, branch: str) -> str:
        return f"{self.base_url}/repos/{repository}/git/ref/heads/{branch}"

    def branch_exists(self, repository: str, branch: str) -> bool:
        response = httpx.get(self._ref_url(repository, branch), headers=self._headers(), timeout=self.timeout_seconds)
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        raise RuntimeError(f"GitHub branch existence check failed: {response.status_code} {response.text[:300]}")

    def get_branch_sha(self, repository: str, branch: str) -> str:
        response = httpx.get(self._ref_url(repository, branch), headers=self._headers(), timeout=self.timeout_seconds)
        if response.status_code != 200:
            raise RuntimeError(f"GitHub source branch lookup failed: {response.status_code} {response.text[:300]}")
        payload = response.json()
        sha = payload.get("object", {}).get("sha")
        if not sha:
            raise RuntimeError("GitHub source branch response did not include object.sha")
        return str(sha)

    def create_branch(self, repository: str, branch: str, source_sha: str) -> GitHubBranchCreateResult:
        response = httpx.post(
            f"{self.base_url}/repos/{repository}/git/refs",
            headers=self._headers(),
            json={"ref": f"refs/heads/{branch}", "sha": source_sha},
            timeout=self.timeout_seconds,
        )
        if response.status_code not in {200, 201}:
            raise RuntimeError(f"GitHub branch creation failed: {response.status_code} {response.text[:300]}")
        html_url = f"https://github.com/{repository}/tree/{branch}"
        return GitHubBranchCreateResult(
            repository=repository,
            branch=branch,
            source_branch=DEFAULT_SOURCE_BRANCH,
            source_sha=source_sha,
            branch_created=True,
            html_url=html_url,
        )


@dataclass
class AgentWorkerGitHubBranchResult:
    success: bool
    task_id: str | None = None
    worker_id: str = "agent-worker-github-branch"
    lease_token: str | None = None
    repository: str | None = None
    branch: str | None = None
    source_branch: str = DEFAULT_SOURCE_BRANCH
    source_sha: str | None = None
    branch_created: bool = False
    branch_url: str | None = None
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


def _default_client() -> GitHubRestBranchClient:
    token = os.getenv("GITHUB_" + "TOKEN", "").strip()
    if not token:
        raise RuntimeError("GITHUB_TOKEN is required for GitHub branch creation")
    return GitHubRestBranchClient(token)


def _append_branch_created_event(
    task: AgentTask,
    worker_id: str,
    lease_token: str | None,
    result: GitHubBranchCreateResult,
) -> AgentTaskEvent:
    event = AgentTaskEvent(
        task_id=task.id,
        event_type=AgentTaskEventType.NOTE,
        message="Worker created a safe GitHub feature branch. No files, commits, or pull requests were created.",
        status=task.status,
        actor=worker_id,
        metadata={
            "worker_id": worker_id,
            "repository": result.repository,
            "branch": result.branch,
            "source_branch": result.source_branch,
            "source_sha": result.source_sha,
            "branch_created": result.branch_created,
            "branch_url": result.html_url,
            "repository_mutated": True,
            "files_written": False,
            "commits_created": False,
            "pull_request_opened": False,
            "operation_type": "github_branch_creation",
        },
    )
    return event


def run_agent_worker_github_branch_creation(
    *,
    task_id: str | None = None,
    first_approved: bool = False,
    worker_id: str = "agent-worker-github-branch",
    lease_seconds: int = 300,
    source_branch: str = DEFAULT_SOURCE_BRANCH,
    client: GitHubBranchClient | None = None,
    task_repository: AgentTaskRepository | None = None,
) -> AgentWorkerGitHubBranchResult:
    if not task_id and not first_approved:
        return AgentWorkerGitHubBranchResult(
            success=False,
            worker_id=worker_id,
            source_branch=source_branch,
            reason="Provide task_id or set first_approved=true.",
        )

    repository = task_repository or agent_task_repository
    claim = (
        claim_first_approved_agent_task_for_worker(
            worker_id=worker_id, lease_seconds=lease_seconds, repository=repository
        )
        if first_approved and not task_id
        else claim_agent_task_for_worker(
            task_id=str(task_id), worker_id=worker_id, lease_seconds=lease_seconds,
            repository=repository,
        )
    )
    if not claim.success or claim.task is None:
        return AgentWorkerGitHubBranchResult(
            success=False,
            task_id=claim.task_id,
            worker_id=worker_id,
            lease_token=claim.lease_token,
            source_branch=source_branch,
            final_status=claim.task.status if claim.task else None,
            reason=claim.reason,
        )

    task = claim.task
    lease_token = claim.lease_token
    branch = _planned_branch_for_task(task)
    validation_error = validate_branch_plan(task)
    if validation_error:
        try:
            commit_agent_task_worker_outcome(
                task, worker_id=worker_id, repository=repository,
                audit_type="agent_worker.github_branch_refused",
                details={"reason": validation_error},
            )
        except Exception as error:  # noqa: BLE001 - a failed durable release must remain visible.
            validation_error = f"{validation_error} Durable worker outcome could not be committed: {error}"
        return AgentWorkerGitHubBranchResult(
            success=False,
            task_id=task.id,
            worker_id=worker_id,
            lease_token=lease_token,
            repository=task.repository,
            branch=branch,
            source_branch=source_branch,
            final_status=task.status,
            reason=validation_error,
        )

    assert task.repository is not None
    try:
        github = client or _default_client()
        if github.branch_exists(task.repository, branch):
            raise RuntimeError(f"Target branch already exists: {branch}")
        source_sha = github.get_branch_sha(task.repository, source_branch)
        created = github.create_branch(task.repository, branch, source_sha)
        created.source_branch = source_branch
    except Exception as error:  # noqa: BLE001 - return clean worker failure instead of crashing CLI/API callers.
        failure_reason = str(error)
        try:
            commit_agent_task_worker_outcome(
                task, worker_id=worker_id, repository=repository,
                audit_type="agent_worker.github_branch_failed",
                details={
                    "repository": task.repository,
                    "branch": branch,
                    "source_branch": source_branch,
                    "reason": failure_reason,
                },
            )
        except Exception as commit_error:  # noqa: BLE001 - keep the external failure and persistence gap visible.
            failure_reason = f"{failure_reason} Durable worker outcome could not be committed: {commit_error}"
        return AgentWorkerGitHubBranchResult(
            success=False,
            task_id=task.id,
            worker_id=worker_id,
            lease_token=lease_token,
            repository=task.repository,
            branch=branch,
            source_branch=source_branch,
            final_status=task.status,
            reason=failure_reason,
        )

    event = _append_branch_created_event(task, worker_id, lease_token, created)
    try:
        commit_agent_task_worker_outcome(
            task, worker_id=worker_id, repository=repository,
            audit_type="agent_worker.github_branch_created",
            details={
                "repository": created.repository,
                "branch": created.branch,
                "source_branch": created.source_branch,
                "source_sha": created.source_sha,
                "branch_url": created.html_url,
            },
            event=event,
        )
    except Exception as error:  # noqa: BLE001 - external mutation may have happened; never report a durable success.
        return AgentWorkerGitHubBranchResult(
            success=False,
            task_id=task.id,
            worker_id=worker_id,
            lease_token=lease_token,
            repository=created.repository,
            branch=created.branch,
            source_branch=created.source_branch,
            source_sha=created.source_sha,
            branch_created=True,
            branch_url=created.html_url,
            final_status=task.status,
            reason=f"Branch creation may have succeeded, but its durable outcome could not be committed: {error}",
        )

    return AgentWorkerGitHubBranchResult(
        success=True,
        task_id=task.id,
        worker_id=worker_id,
        lease_token=lease_token,
        repository=created.repository,
        branch=created.branch,
        source_branch=created.source_branch,
        source_sha=created.source_sha,
        branch_created=True,
        branch_url=created.html_url,
        event_id=event.id,
        final_status=task.status,
        reason="GitHub feature branch created. No files, commits, or pull requests created.",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a safe GitHub feature branch for an approved AgentTask.")
    parser.add_argument("--task-id", default=None, help="Specific AgentTask id to branch for.")
    parser.add_argument("--first-approved", action="store_true", help="Use the first approved task with a linked approval.")
    parser.add_argument("--worker-id", default="agent-worker-github-branch", help="Worker id written into task events.")
    parser.add_argument("--lease-seconds", type=int, default=300, help="Claim lease duration in seconds.")
    parser.add_argument("--source-branch", default=DEFAULT_SOURCE_BRANCH, help="Source branch to branch from.")
    parser.add_argument("--json", action="store_true", help="Print JSON result.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    result = run_agent_worker_github_branch_creation(
        task_id=args.task_id,
        first_approved=args.first_approved,
        worker_id=args.worker_id,
        lease_seconds=args.lease_seconds,
        source_branch=args.source_branch,
    )
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        decision = "PASS" if result.success else "FAILED"
        print(f"Agent worker GitHub branch creation: {decision}")
        print(f"Task: {result.task_id or 'n/a'}")
        print(f"Repository: {result.repository or 'n/a'}")
        print(f"Branch: {result.branch or 'n/a'}")
        print(f"Reason: {result.reason}")
    return 0 if result.success else 1
