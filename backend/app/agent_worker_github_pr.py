from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

import httpx

from .agent_task_models import AgentTask, AgentTaskEvent, AgentTaskEventType, AgentTaskStatus
from .agent_worker_branch_plan import _planned_branch_for_task, validate_branch_plan
from .agent_worker_claims import claim_agent_task_for_worker, claim_first_approved_agent_task_for_worker
from .models import ApprovalRequest, AuditEvent
from .store import store

GITHUB_API_BASE_URL = "https://api.github.com"
DEFAULT_BASE_BRANCH = "main"


@dataclass
class GitHubPullRequestCreateResult:
    repository: str
    number: int
    title: str
    head_branch: str
    base_branch: str
    html_url: str


class GitHubPullRequestClient(Protocol):
    def branch_exists(self, repository: str, branch: str) -> bool: ...

    def open_pull_request_exists(self, repository: str, head_branch: str, base_branch: str) -> bool: ...

    def create_pull_request(
        self,
        repository: str,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str,
    ) -> GitHubPullRequestCreateResult: ...


class GitHubRestPullRequestClient:
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

    def open_pull_request_exists(self, repository: str, head_branch: str, base_branch: str) -> bool:
        owner = repository.split("/", 1)[0]
        response = httpx.get(
            f"{self.base_url}/repos/{repository}/pulls",
            headers=self._headers(),
            params={"state": "open", "head": f"{owner}:{head_branch}", "base": base_branch},
            timeout=self.timeout_seconds,
        )
        if response.status_code != 200:
            raise RuntimeError(f"GitHub open PR lookup failed: {response.status_code} {response.text[:300]}")
        return bool(response.json())

    def create_pull_request(
        self,
        repository: str,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str,
    ) -> GitHubPullRequestCreateResult:
        response = httpx.post(
            f"{self.base_url}/repos/{repository}/pulls",
            headers=self._headers(),
            json={"title": title, "body": body, "head": head_branch, "base": base_branch},
            timeout=self.timeout_seconds,
        )
        if response.status_code not in {200, 201}:
            raise RuntimeError(f"GitHub PR creation failed: {response.status_code} {response.text[:300]}")
        payload = response.json()
        return GitHubPullRequestCreateResult(
            repository=repository,
            number=int(payload["number"]),
            title=str(payload["title"]),
            head_branch=head_branch,
            base_branch=base_branch,
            html_url=str(payload["html_url"]),
        )


@dataclass
class AgentWorkerGitHubPRResult:
    success: bool
    task_id: str | None = None
    approval_request_id: str | None = None
    worker_id: str = "agent-worker-github-pr"
    lease_token: str | None = None
    repository: str | None = None
    head_branch: str | None = None
    base_branch: str = DEFAULT_BASE_BRANCH
    pull_request_number: int | None = None
    pull_request_url: str | None = None
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


def _audit(event_type: str, entity_id: str, details: dict[str, Any], actor: str) -> AuditEvent:
    event = AuditEvent(event_type=event_type, entity_id=entity_id, details=details, actor=actor)
    store.audit_events.append(event)
    return event


def _release_task(task: AgentTask, lease_token: str | None, worker_id: str) -> None:
    if lease_token and task.worker_lease_token != lease_token:
        _audit(
            "agent_worker.lease_release_skipped",
            task.id,
            {"worker_id": worker_id, "lease_token": lease_token, "current_lease_token": task.worker_lease_token},
            actor=worker_id,
        )
        return
    task.worker_lease_owner = None
    task.worker_lease_expires_at = None
    task.worker_lease_token = None
    task.updated_at = _now()
    _audit("agent_worker.task_released", task.id, {"worker_id": worker_id, "lease_token": lease_token}, actor=worker_id)


def _linked_approval(task: AgentTask) -> ApprovalRequest | None:
    if not task.approval_request_id:
        return None
    return store.approval_requests.get(task.approval_request_id)


def _default_client() -> GitHubRestPullRequestClient:
    token = os.getenv("GITHUB_" + "TOKEN", "").strip()
    if not token:
        raise RuntimeError("GITHUB_TOKEN is required for GitHub PR creation")
    return GitHubRestPullRequestClient(token)


def _pull_request_title(task: AgentTask, approval: ApprovalRequest | None) -> str:
    if approval and approval.title.strip():
        return approval.title.strip()
    return f"AgentTask: {task.title}"


def _pull_request_body(task: AgentTask, approval: ApprovalRequest | None) -> str:
    approval_id = approval.id if approval else task.approval_request_id
    return "\n".join(
        [
            "## Aixion worker PR",
            "",
            f"AgentTask: `{task.id}`",
            f"ApprovalRequest: `{approval_id}`",
            f"Provider: `{task.provider}`",
            "",
            "This PR was opened by the Aixion worker after mobile/operator approval.",
            "",
            "Scope:",
            "- Opens review PR only",
            "- Does not merge",
            "- Does not approve its own work",
            "- Keeps audit evidence in the AgentTask timeline",
        ]
    )


def _append_pr_created_event(
    task: AgentTask,
    approval: ApprovalRequest | None,
    worker_id: str,
    lease_token: str | None,
    pr: GitHubPullRequestCreateResult,
) -> AgentTaskEvent:
    task.status = AgentTaskStatus.READY_FOR_PR
    task.updated_at = _now()
    event = AgentTaskEvent(
        task_id=task.id,
        event_type=AgentTaskEventType.PR_CREATED,
        message="Worker opened a GitHub pull request for human review.",
        status=task.status,
        actor=worker_id,
        metadata={
            "worker_id": worker_id,
            "lease_token": lease_token,
            "approval_request_id": approval.id if approval else task.approval_request_id,
            "repository": pr.repository,
            "head_branch": pr.head_branch,
            "base_branch": pr.base_branch,
            "pull_request_number": pr.number,
            "pull_request_url": pr.html_url,
            "pull_request_opened": True,
            "merged": False,
            "operation_type": "github_pull_request_creation",
        },
    )
    store.agent_task_events[event.id] = event
    return event


def run_agent_worker_github_pr_creation(
    *,
    task_id: str | None = None,
    first_approved: bool = False,
    worker_id: str = "agent-worker-github-pr",
    lease_seconds: int = 300,
    base_branch: str = DEFAULT_BASE_BRANCH,
    client: GitHubPullRequestClient | None = None,
) -> AgentWorkerGitHubPRResult:
    if not task_id and not first_approved:
        return AgentWorkerGitHubPRResult(success=False, worker_id=worker_id, base_branch=base_branch, reason="Provide task_id or set first_approved=true.")

    claim = (
        claim_first_approved_agent_task_for_worker(worker_id=worker_id, lease_seconds=lease_seconds)
        if first_approved and not task_id
        else claim_agent_task_for_worker(task_id=str(task_id), worker_id=worker_id, lease_seconds=lease_seconds)
    )
    if not claim.success or claim.task is None:
        return AgentWorkerGitHubPRResult(
            success=False,
            task_id=claim.task_id,
            worker_id=worker_id,
            lease_token=claim.lease_token,
            base_branch=base_branch,
            final_status=claim.task.status if claim.task else None,
            reason=claim.reason,
        )

    task = claim.task
    lease_token = claim.lease_token
    approval = _linked_approval(task)
    head_branch = _planned_branch_for_task(task)
    validation_error = validate_branch_plan(task)
    if validation_error:
        _audit("agent_worker.github_pr_refused", task.id, {"worker_id": worker_id, "lease_token": lease_token, "reason": validation_error}, actor=worker_id)
        _release_task(task, lease_token, worker_id)
        store.persist()
        return AgentWorkerGitHubPRResult(
            success=False,
            task_id=task.id,
            approval_request_id=task.approval_request_id,
            worker_id=worker_id,
            lease_token=lease_token,
            repository=task.repository,
            head_branch=head_branch,
            base_branch=base_branch,
            final_status=task.status,
            reason=validation_error,
        )

    assert task.repository is not None
    github = client or _default_client()
    try:
        if not github.branch_exists(task.repository, head_branch):
            raise RuntimeError(f"Head branch does not exist: {head_branch}")
        if github.open_pull_request_exists(task.repository, head_branch, base_branch):
            raise RuntimeError(f"Open pull request already exists for {head_branch} -> {base_branch}")
        pr = github.create_pull_request(
            task.repository,
            _pull_request_title(task, approval),
            _pull_request_body(task, approval),
            head_branch,
            base_branch,
        )
    except Exception as error:  # noqa: BLE001 - return clean worker failure.
        _audit(
            "agent_worker.github_pr_failed",
            task.id,
            {
                "worker_id": worker_id,
                "lease_token": lease_token,
                "repository": task.repository,
                "head_branch": head_branch,
                "base_branch": base_branch,
                "reason": str(error),
            },
            actor=worker_id,
        )
        _release_task(task, lease_token, worker_id)
        store.persist()
        return AgentWorkerGitHubPRResult(
            success=False,
            task_id=task.id,
            approval_request_id=task.approval_request_id,
            worker_id=worker_id,
            lease_token=lease_token,
            repository=task.repository,
            head_branch=head_branch,
            base_branch=base_branch,
            final_status=task.status,
            reason=str(error),
        )

    event = _append_pr_created_event(task, approval, worker_id, lease_token, pr)
    _audit(
        "agent_worker.github_pr_created",
        task.id,
        {
            "worker_id": worker_id,
            "lease_token": lease_token,
            "approval_request_id": approval.id if approval else task.approval_request_id,
            "repository": pr.repository,
            "head_branch": pr.head_branch,
            "base_branch": pr.base_branch,
            "pull_request_number": pr.number,
            "pull_request_url": pr.html_url,
        },
        actor=worker_id,
    )
    _release_task(task, lease_token, worker_id)
    store.persist()

    return AgentWorkerGitHubPRResult(
        success=True,
        task_id=task.id,
        approval_request_id=approval.id if approval else task.approval_request_id,
        worker_id=worker_id,
        lease_token=lease_token,
        repository=pr.repository,
        head_branch=pr.head_branch,
        base_branch=pr.base_branch,
        pull_request_number=pr.number,
        pull_request_url=pr.html_url,
        event_id=event.id,
        final_status=task.status,
        reason="GitHub pull request opened for human review. No merge performed.",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Open a GitHub PR for an approved AgentTask branch.")
    parser.add_argument("--task-id", default=None, help="Specific AgentTask id to open PR for.")
    parser.add_argument("--first-approved", action="store_true", help="Use the first approved task with a linked approval.")
    parser.add_argument("--worker-id", default="agent-worker-github-pr", help="Worker id written into task events.")
    parser.add_argument("--lease-seconds", type=int, default=300, help="Claim lease duration in seconds.")
    parser.add_argument("--base-branch", default=DEFAULT_BASE_BRANCH, help="Base branch for the PR.")
    parser.add_argument("--json", action="store_true", help="Print JSON result.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    result = run_agent_worker_github_pr_creation(
        task_id=args.task_id,
        first_approved=args.first_approved,
        worker_id=args.worker_id,
        lease_seconds=args.lease_seconds,
        base_branch=args.base_branch,
    )
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        decision = "PASS" if result.success else "FAILED"
        print(f"Agent worker GitHub PR creation: {decision}")
        print(f"Task: {result.task_id or 'n/a'}")
        print(f"Repository: {result.repository or 'n/a'}")
        print(f"Pull request: {result.pull_request_url or 'n/a'}")
        print(f"Reason: {result.reason}")
    return 0 if result.success else 1
