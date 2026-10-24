from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
import re
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from fastapi import HTTPException, Request

from .agent_run_models import AgentRun, AgentRunStep, AgentRunStepType
from .agent_run_repository import PostgresAgentRunRepository
from .agent_run_supervisor import (
    AgentRunConflict,
    PostgresPullRequestRequest,
    build_postgres_pull_request_completion,
)
from .agent_task_models import AgentTask, AgentTaskStatus
from .agent_task_repository import PostgresAgentTaskRepository
from .approval_repository import PostgresApprovalDecisionRepository
from .application_repositories import ApplicationRepositories
from .application_repository_access import application_repositories_for_request
from .models import ApprovalRequest, ApprovalStatus, now_utc
from .postgres_agent_run_apply_changes import (
    ApplyChangeIntentStatus,
    PostgresAgentRunApplyChangeIntentRepository,
)
from .postgres_agent_run_branch import BranchIntentStatus
from .postgres_agent_run_branch import PostgresAgentRunBranchIntentRepository
from .postgres_agent_run_pull_request import (
    AgentRunPullRequestIntent,
    PostgresAgentRunPullRequestIntentRepository,
    PullRequestIntentStatus,
)
from .postgres_agent_run_validation import (
    PostgresAgentRunValidationIntentRepository,
    RunValidationIntentStatus,
)
from .postgres_runtime import PostgresRuntime
from .postgres_trust_repository import PostgresTrustRepository
from .trust_crypto import sha256_hex
from .trust_models import CapabilityActionType, ProposedAction

DEFAULT_PULL_REQUEST_BASE_BRANCH = "main"
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class PullRequestRemote:
    """Minimal provider response; tests inject deterministic implementations."""

    repository: str
    number: int
    title: str
    body: str
    head_branch: str
    base_branch: str
    html_url: str
    head_sha: str
    base_sha: str


class PullRequestProvider(Protocol):
    """Provider boundary. `find_by_intent` must be GET-only."""

    def observe_branch(self, repository: str, branch: str, *, timeout_seconds: int) -> str: ...

    def find_by_intent(
        self, repository: str, head_branch: str, base_branch: str, intent_marker: str,
        *, timeout_seconds: int,
    ) -> list[PullRequestRemote]: ...

    def create_pull_request(
        self, repository: str, title: str, body: str, head_branch: str, base_branch: str,
        *, timeout_seconds: int,
    ) -> PullRequestRemote: ...


def postgres_pull_request_enabled() -> bool:
    import os

    return os.getenv("AIXION_POSTGRES_CREATE_PULL_REQUEST_ENABLED", "false").strip().lower() in {
        "1", "true", "yes",
    }


def _title(task: AgentTask, approval: ApprovalRequest) -> str:
    return approval.title.strip() if approval.title.strip() else f"AgentTask: {task.title}"


def _body(task: AgentTask, approval: ApprovalRequest, marker: str) -> str:
    return "\n".join((
        "## Aixion worker pull request", "", f"AgentTask: `{task.id}`",
        f"ApprovalRequest: `{approval.id}`", f"Provider: `{task.provider.value}`", "",
        "This pull request was opened after mobile approval. It is for human review; it is not merged or self-approved.",
        "", f"<!-- {marker} -->",
    ))


def _response_digest(remote: PullRequestRemote) -> str:
    safe = {
        "repository": remote.repository, "number": remote.number,
        "head_branch": remote.head_branch, "base_branch": remote.base_branch,
        "html_url": remote.html_url, "head_sha": remote.head_sha,
        "base_sha": remote.base_sha,
        "body_sha256": hashlib.sha256(remote.body.encode("utf-8")).hexdigest(),
        "title_sha256": hashlib.sha256(remote.title.encode("utf-8")).hexdigest(),
    }
    return hashlib.sha256(json.dumps(safe, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _valid_remote(
    remote: PullRequestRemote, *, intent: AgentRunPullRequestIntent,
    title: str, body: str, expected_head_sha: str, expected_base_sha: str,
) -> bool:
    return bool(
        isinstance(remote, PullRequestRemote)
        and type(remote.number) is int and remote.number > 0
        and remote.repository == intent.repository
        and remote.head_branch == intent.head_branch
        and remote.base_branch == intent.base_branch
        and remote.title == title and remote.body == body
        and intent.intent_marker in remote.body
        and remote.head_sha == expected_head_sha
        and remote.base_sha == expected_base_sha
        and _GIT_SHA.fullmatch(remote.head_sha)
        and _GIT_SHA.fullmatch(remote.base_sha)
        and remote.html_url == f"https://github.com/{intent.repository}/pull/{remote.number}"
    )


def _collection_digest(remotes: list[PullRequestRemote]) -> str:
    return hashlib.sha256(
        json.dumps(
            sorted(_response_digest(item) for item in remotes),
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class PostgresCreatePullRequestCoordinator:
    """One-shot remote mutation with durable intent and GET-only recovery."""

    def execute(self, request: PostgresPullRequestRequest):
        run, step, task, approval, lease = (
            request.expected_run, request.expected_step, request.task,
            request.approval, request.lease,
        )
        if (
            not run.workspace_id or not run.capability_lease_id
            or run.status.value != "RUNNING" or run.lease_owner != request.worker_id
            or run.lease_token != request.worker_lease_token
            or run.current_step_id != step.id or step.step_type != AgentRunStepType.CREATE_PULL_REQUEST
            or step.status.value != "RUNNING" or step.run_id != run.id or step.task_id != task.id
            or task.id != run.task_id or task.workspace_id != run.workspace_id
            or task.project_id != run.project_id or task.approval_request_id != approval.id
            or task.status != AgentTaskStatus.APPROVED
            or approval.id != run.approval_request_id or approval.workspace_id != run.workspace_id
            or approval.project_id != task.project_id or approval.status != ApprovalStatus.APPROVED
            or lease.id != run.capability_lease_id or lease.workspace_id != run.workspace_id
            or lease.approval_request_id != approval.id or lease.scope.repository != task.repository
            or lease.scope.branch != task.branch_preference
            or CapabilityActionType.CREATE_PULL_REQUEST not in lease.scope.allowed_actions
            or task.repository != request.repository or task.branch_preference != request.head_branch
            or request.base_branch != DEFAULT_PULL_REQUEST_BASE_BRANCH
            or not 1 <= request.timeout_seconds <= 3600
        ):
            raise AgentRunConflict("PostgreSQL CREATE_PULL_REQUEST run, task, approval, or lease scope is invalid.")

        title = _title(task, approval)
        marker_uuid = uuid5(NAMESPACE_URL, f"aixion:{run.workspace_id}:{run.id}:{step.id}:{step.idempotency_key}")
        marker = f"aixion-intent:{marker_uuid}"
        body = _body(task, approval, marker)
        title_hash = hashlib.sha256(title.encode("utf-8")).hexdigest()
        body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        intent = request.intent_repository.get_for_step(
            workspace_id=run.workspace_id, run_id=run.id, step_id=step.id,
            idempotency_key=step.idempotency_key,
        )
        base_sha: str
        head_sha: str
        if intent is None or intent.status == PullRequestIntentStatus.INTENT:
            head_sha = request.provider.observe_branch(
                request.repository, request.head_branch, timeout_seconds=request.timeout_seconds
            )
            base_sha = request.provider.observe_branch(
                request.repository, request.base_branch, timeout_seconds=request.timeout_seconds
            )
            if not _GIT_SHA.fullmatch(head_sha) or head_sha != request.expected_head_sha or not _GIT_SHA.fullmatch(base_sha):
                raise AgentRunConflict("GitHub branch snapshots do not match validated durable revisions.")
        else:
            head_sha = intent.expected_head_sha
            base_sha = intent.expected_base_sha
            if head_sha != request.expected_head_sha:
                raise AgentRunConflict("Existing pull request intent differs from the validated head revision.")
        action_id = f"action_{sha256_hex(f'{run.id}:{step.id}:{step.idempotency_key}:{lease.id}:{head_sha}:{base_sha}:{marker}') }"
        action = ProposedAction(
            id=action_id, lease_id=lease.id, provider=task.provider,
            agent_id=task.external_agent_id, run_id=run.id, task_id=task.id,
            project_id=task.project_id, workspace_id=run.workspace_id,
            action_type=CapabilityActionType.CREATE_PULL_REQUEST,
            repository=request.repository, branch=request.head_branch,
            estimated_runtime_seconds=request.timeout_seconds,
            retry_number=max(0, step.attempt_count - 1),
            metadata={
                "repository": request.repository, "head_branch": request.head_branch,
                "base_branch": request.base_branch, "title_sha256": title_hash,
                "body_sha256": body_hash, "intent_marker": marker,
                "step_type": AgentRunStepType.CREATE_PULL_REQUEST.value,
                "idempotency_key": step.idempotency_key, "step_id": step.id,
                "task_id": task.id, "approval_id": approval.id, "lease_id": lease.id,
                "validated_head_sha": head_sha,
                "validated_base_sha": base_sha,
            },
        )
        now = now_utc()
        if intent is None:
            intent = AgentRunPullRequestIntent(
                id=f"pr_intent_{sha256_hex(f'{run.id}:{step.id}:{step.idempotency_key}')}",
                workspace_id=run.workspace_id, run_id=run.id, task_id=task.id,
                approval_id=approval.id, step_id=step.id, action_id=action.id,
                lease_id=lease.id, idempotency_key=step.idempotency_key,
                repository=request.repository, head_branch=request.head_branch,
                base_branch=request.base_branch, title_sha256=title_hash,
                body_sha256=body_hash, intent_marker=marker,
                expected_head_sha=head_sha, expected_base_sha=base_sha,
                lease_token_hash=sha256_hex({"lease_token": request.worker_lease_token}),
                run_revision=run.updated_at, step_revision=step.updated_at,
                status=PullRequestIntentStatus.INTENT, created_at=now, updated_at=now,
            )
            intent = request.intent_repository.record_authorized_intent(
                record=intent, action=action, expected_run=run, expected_step=step,
                timeout_seconds=request.timeout_seconds, actor=request.actor, now=now,
                expected_base_branch=request.base_branch,
            )
        elif any((
            intent.action_id != action.id, intent.repository != request.repository,
            intent.head_branch != request.head_branch, intent.base_branch != request.base_branch,
            intent.title_sha256 != title_hash, intent.body_sha256 != body_hash,
            intent.intent_marker != marker, intent.expected_head_sha != head_sha,
            intent.expected_base_sha != base_sha,
        )):
            raise AgentRunConflict("Existing PostgreSQL pull request intent differs from current approval evidence.")

        if intent.status == PullRequestIntentStatus.INTENT:
            intent = request.intent_repository.claim_post_once(
                intent=intent, expected_run=run, expected_step=step,
                worker_lease_token=request.worker_lease_token,
            )
            try:
                remote = request.provider.create_pull_request(
                    request.repository, title, body, request.head_branch, request.base_branch,
                    timeout_seconds=request.timeout_seconds,
                )
                if not _valid_remote(remote, intent=intent, title=title, body=body,
                                     expected_head_sha=head_sha, expected_base_sha=base_sha):
                    raise ValueError("Provider POST response failed exact identity verification")
                intent = request.intent_repository.record_post_outcome(
                    intent_id=intent.id, workspace_id=intent.workspace_id, outcome="VERIFIED",
                    pull_number=remote.number, pull_url=remote.html_url, head_sha=remote.head_sha,
                    base_sha=remote.base_sha, response_sha256=_response_digest(remote),
                    observed_repository=remote.repository,
                    observed_head_branch=remote.head_branch,
                    observed_base_branch=remote.base_branch,
                    marker_matches=marker in remote.body,
                    reason="POST response verified against repository, branches, marker, and validated revisions.",
                )
            except Exception as error:
                intent = request.intent_repository.record_post_outcome(
                    intent_id=intent.id, workspace_id=intent.workspace_id, outcome="AMBIGUOUS",
                    reason=f"POST result ambiguous ({type(error).__name__}); recovery is GET-only.",
                )

        if intent.status in {PullRequestIntentStatus.POST_CLAIMED, PullRequestIntentStatus.RECONCILIATION_REQUIRED}:
            try:
                candidates = request.provider.find_by_intent(
                    request.repository, request.head_branch, request.base_branch, marker,
                    timeout_seconds=request.timeout_seconds,
                )
                if not isinstance(candidates, list):
                    raise ValueError("Provider GET returned an invalid collection")
            except Exception:
                request.intent_repository.record_get_reconciliation(
                    intent_id=intent.id, workspace_id=intent.workspace_id,
                    observation_status="UNCERTAIN", matching_count=0,
                )
                raise AgentRunConflict("Pull request POST is ambiguous; GET-only reconciliation is unavailable.")
            matches = [candidate for candidate in candidates if _valid_remote(
                candidate, intent=intent, title=title, body=body,
                expected_head_sha=head_sha, expected_base_sha=base_sha,
            )]
            remote = matches[0] if len(matches) == 1 else None
            intent = request.intent_repository.record_get_reconciliation(
                intent_id=intent.id, workspace_id=intent.workspace_id,
                observation_status="CERTAIN", matching_count=len(matches),
                observed_repository=remote.repository if remote else None,
                observed_head_branch=remote.head_branch if remote else None,
                observed_base_branch=remote.base_branch if remote else None,
                marker_matches=True if remote else False,
                pull_number=remote.number if remote else None,
                pull_url=remote.html_url if remote else None,
                head_sha=remote.head_sha if remote else None,
                base_sha=remote.base_sha if remote else None,
                response_sha256=_collection_digest(candidates),
            )
        if intent.status != PullRequestIntentStatus.REMOTE_VERIFIED:
            raise AgentRunConflict("Pull request is not exactly verified; the one-shot POST remains consumed.")
        return request.completion_factory(intent, title, body)


def postgres_pull_request_execution_dependencies(
    *, request: Request, run: AgentRun, step: AgentRunStep, actor: str,
):
    """Resolve only complete app-lifespan PostgreSQL authority; no default provider."""
    if not postgres_pull_request_enabled():
        raise HTTPException(
            status_code=503,
            detail="PostgreSQL CREATE_PULL_REQUEST is disabled by AIXION_POSTGRES_CREATE_PULL_REQUEST_ENABLED.",
        )
    state = request.app.state
    runtime = getattr(state, "postgres_runtime", None)
    try:
        repositories = application_repositories_for_request(request)
    except Exception as error:
        raise HTTPException(status_code=503, detail="CREATE_PULL_REQUEST requires the lifespan-owned repository bundle.") from error
    if (
        not isinstance(runtime, PostgresRuntime)
        or not isinstance(repositories, ApplicationRepositories)
        or runtime.repositories is not repositories
        or getattr(state, "postgres_repositories", None) is not repositories
        or getattr(state, "agent_run_repository", None) is not repositories.agent_runs
        or getattr(state, "agent_task_repository", None) is not repositories.agent_tasks
        or getattr(state, "approval_decision_repository", None) is not repositories.approval_decisions
        or getattr(state, "trust_repository", None) is not repositories.trust
        or getattr(state, "agent_run_branch_intent_repository", None) is not repositories.agent_run_branch_intents
        or getattr(state, "agent_run_apply_change_intent_repository", None) is not repositories.agent_run_apply_change_intents
        or getattr(state, "agent_run_validation_intent_repository", None) is not repositories.agent_run_validation_intents
        or getattr(state, "agent_run_pull_request_intent_repository", None) is not repositories.agent_run_pull_request_intents
        or not isinstance(repositories.agent_runs, PostgresAgentRunRepository)
        or not isinstance(repositories.agent_tasks, PostgresAgentTaskRepository)
        or not isinstance(repositories.approval_decisions, PostgresApprovalDecisionRepository)
        or not isinstance(repositories.trust, PostgresTrustRepository)
        or not isinstance(repositories.agent_run_branch_intents, PostgresAgentRunBranchIntentRepository)
        or not isinstance(repositories.agent_run_apply_change_intents, PostgresAgentRunApplyChangeIntentRepository)
        or not isinstance(repositories.agent_run_validation_intents, PostgresAgentRunValidationIntentRepository)
        or not isinstance(repositories.agent_run_pull_request_intents, PostgresAgentRunPullRequestIntentRepository)
        or step.step_type != AgentRunStepType.CREATE_PULL_REQUEST
        or step.run_id != run.id or step.task_id != run.task_id
        or not run.workspace_id or not run.approval_request_id or not run.capability_lease_id
    ):
        raise HTTPException(status_code=503, detail="CREATE_PULL_REQUEST authority repositories are incomplete or mismatched.")
    provider = getattr(state, "agent_run_pull_request_provider", None)
    if provider is None or not all(callable(getattr(provider, name, None)) for name in (
        "observe_branch", "find_by_intent", "create_pull_request",
    )):
        raise HTTPException(status_code=503, detail="No explicit pull request provider is configured.")

    task = repositories.agent_tasks.get(run.task_id)
    approval = repositories.approval_decisions.get(run.approval_request_id)
    with runtime.connect() as conn:
        lease = repositories.trust.get_capability_lease(conn, run.capability_lease_id, run.workspace_id, for_share=True)
    if (
        task is None or approval is None or lease is None
        or task.id != run.task_id or task.workspace_id != run.workspace_id
        or task.project_id != run.project_id or task.approval_request_id != approval.id
        or task.repository != run.repository or not task.branch_preference
        or approval.id != run.approval_request_id or approval.workspace_id != run.workspace_id
        or approval.project_id != task.project_id or approval.status != ApprovalStatus.APPROVED
        or task.status != AgentTaskStatus.APPROVED
        or lease.id != run.capability_lease_id or lease.workspace_id != run.workspace_id
        or lease.approval_request_id != approval.id
        or lease.scope.repository != task.repository or lease.scope.branch != task.branch_preference
        or CapabilityActionType.CREATE_PULL_REQUEST not in lease.scope.allowed_actions
    ):
        raise HTTPException(status_code=409, detail="Durable CREATE_PULL_REQUEST task, approval, or capability lease scope is invalid.")

    steps = repositories.agent_runs.list_steps_for_run(run.id)
    branch_steps = [item for item in steps if item.step_type == AgentRunStepType.CREATE_BRANCH]
    apply_steps = [item for item in steps if item.step_type == AgentRunStepType.APPLY_CHANGES]
    validation_steps = [item for item in steps if item.step_type == AgentRunStepType.RUN_VALIDATION]
    if any(len(items) != 1 for items in (branch_steps, apply_steps, validation_steps)):
        raise HTTPException(status_code=409, detail="CREATE_PULL_REQUEST requires exactly one branch, apply, and validation step.")
    branch_step, apply_step, validation_step = branch_steps[0], apply_steps[0], validation_steps[0]
    if any(item.status.value != "SUCCEEDED" for item in (branch_step, apply_step, validation_step)):
        raise HTTPException(status_code=409, detail="CREATE_PULL_REQUEST requires successful durable branch, apply, and validation steps.")
    branch_intent = repositories.agent_run_branch_intents.get_for_step(
        workspace_id=run.workspace_id, run_id=run.id, step_id=branch_step.id,
        idempotency_key=branch_step.idempotency_key,
    )
    apply_intent = repositories.agent_run_apply_change_intents.get_for_step(
        workspace_id=run.workspace_id, run_id=run.id, step_id=apply_step.id,
        idempotency_key=apply_step.idempotency_key,
    )
    validation_intent = repositories.agent_run_validation_intents.get_for_step(
        workspace_id=run.workspace_id, run_id=run.id, step_id=validation_step.id,
        idempotency_key=validation_step.idempotency_key,
    )
    if (
        branch_intent is None or branch_intent.status != BranchIntentStatus.COMPLETED
        or branch_intent.repository != task.repository or branch_intent.target_ref != task.branch_preference
        or apply_intent is None or apply_intent.status != ApplyChangeIntentStatus.COMPLETED
        or apply_intent.repository != task.repository or apply_intent.branch != task.branch_preference
        or not apply_intent.commit_sha
        or validation_intent is None or validation_intent.status != RunValidationIntentStatus.COMPLETED
        or validation_intent.bundle_revision != apply_intent.commit_sha
        or validation_intent.workspace_id != run.workspace_id
        or validation_intent.run_id != run.id or validation_intent.task_id != task.id
        or validation_intent.approval_id != approval.id
    ):
        raise HTTPException(status_code=409, detail="Durable branch, apply, or validation intent is incomplete or inconsistent.")

    base_branch = getattr(state, "agent_run_pull_request_base_branch", DEFAULT_PULL_REQUEST_BASE_BRANCH)
    if not isinstance(base_branch, str) or base_branch != DEFAULT_PULL_REQUEST_BASE_BRANCH:
        raise HTTPException(status_code=503, detail="Only the configured protected base branch is supported for PostgreSQL PR creation.")
    coordinator = PostgresCreatePullRequestCoordinator()

    def request_factory(current_run, current_step, worker_lease_token: str, timeout_seconds: int):
        if current_run.id != run.id or current_step.id != step.id or current_run.capability_lease_id != lease.id:
            raise ValueError("CREATE_PULL_REQUEST execution changed its durable run authority")
        return PostgresPullRequestRequest(
            expected_run=current_run, expected_step=current_step, task=task, approval=approval,
            lease=lease, worker_id=current_run.lease_owner or "",
            worker_lease_token=worker_lease_token, actor=actor, timeout_seconds=timeout_seconds,
            repository=task.repository or "", head_branch=task.branch_preference or "",
            base_branch=base_branch, expected_head_sha=validation_intent.bundle_revision,
            provider=provider, intent_repository=repositories.agent_run_pull_request_intents,
            completion_factory=lambda intent, title, body: build_postgres_pull_request_completion(
                run=current_run, steps=repositories.agent_runs.list_steps_for_run(run.id),
                intent=intent, title=title, body=body, actor=actor,
                worker_lease_token=worker_lease_token,
            ),
        )

    return coordinator, request_factory
