from __future__ import annotations

import base64
import hashlib
import os
from urllib.parse import quote
import unicodedata

import httpx
from fastapi import HTTPException, Request

from .agent_run_models import AgentRun, AgentRunStep, AgentRunStepType
from .agent_run_supervisor import build_postgres_apply_change_completion
from .agent_task_models import AgentTaskStatus
from .agent_task_repository import PostgresAgentTaskRepository
from .approval_repository import PostgresApprovalDecisionRepository
from .application_repository_access import application_repositories_for_request
from .application_repositories import ApplicationRepositories
from .models import ApprovalStatus, now_utc
from .postgres_agent_run_apply_changes import AgentRunApplyChangeIntent, PostgresAgentRunApplyChangeIntentRepository
from .postgres_agent_run_branch import BranchIntentStatus
from .postgres_agent_run_apply_changes_executor import (
    ApplyChangeRequest, FileObservation, FileMutationProvider, PostgresApplyChangesCoordinator,
    postgres_apply_changes_enabled,
)
from .postgres_runtime import PostgresRuntime
from .agent_run_repository import PostgresAgentRunRepository
from .postgres_trust_repository import PostgresTrustRepository
from .trust_crypto import sha256_hex
from .trust_models import CapabilityActionType, ProposedAction


def _valid_repo_path(value: str) -> bool:
    parts = value.split("/")
    return bool(
        value and not value.startswith("/") and "\\" not in value
        and value == value.strip() and all(part == part.strip() for part in parts)
        and all(part not in {"", ".", ".."} for part in parts)
        and all(unicodedata.category(char) not in {"Cc", "Cf"} for char in value)
    )


class GitHubApplyChangeProvider:
    # createCommitOnBranch accepts expectedHeadOid and performs the file change
    # in the same branch commit mutation. The route has a separate default-off
    # rollout gate until PostgreSQL route/e2e validation is complete.
    supports_atomic_base_commit = True

    def __init__(self, token: str, *, timeout_seconds: float = 20.0) -> None:
        if not token.strip() or not 1 <= timeout_seconds <= 60:
            raise ValueError("GitHub apply provider configuration is invalid")
        self._token = token.strip()
        self._timeout = timeout_seconds
        self._base = "https://api.github.com"

    def _headers(self) -> dict[str, str]:
        return {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28"}

    def observe_file(self, repository: str, path: str, branch: str) -> FileObservation:
        encoded_path = "/".join(quote(part, safe="") for part in path.split("/"))
        response = httpx.get(
            f"{self._base}/repos/{repository}/contents/{encoded_path}", headers=self._headers(),
            params={"ref": branch}, timeout=self._timeout,
        )
        if response.status_code == 404:
            return FileObservation(exists=False)
        if response.status_code != 200:
            raise RuntimeError(f"GitHub file observation failed: HTTP {response.status_code}")
        payload = response.json()
        if payload.get("type") != "file" or not isinstance(payload.get("sha"), str):
            raise RuntimeError("GitHub file observation returned an invalid object")
        encoded = payload.get("content")
        if not isinstance(encoded, str):
            raise RuntimeError("GitHub file observation omitted content")
        try:
            content = base64.b64decode(encoded, validate=False)
        except Exception as error:
            raise RuntimeError("GitHub file content encoding is invalid") from error
        commits_response = httpx.get(
            f"{self._base}/repos/{repository}/commits", headers=self._headers(),
            params={"path": path, "sha": branch, "per_page": 1}, timeout=self._timeout,
        )
        if commits_response.status_code != 200:
            raise RuntimeError(f"GitHub file commit attribution failed: HTTP {commits_response.status_code}")
        commits = commits_response.json()
        if not isinstance(commits, list) or not commits:
            raise RuntimeError("GitHub file commit attribution returned no commit")
        commit = commits[0]
        commit_sha = commit.get("sha")
        commit_message = commit.get("commit", {}).get("message")
        if not isinstance(commit_sha, str) or not isinstance(commit_message, str):
            raise RuntimeError("GitHub file commit attribution response is invalid")
        return FileObservation(
            exists=True, blob_sha=payload["sha"],
            content_sha256=hashlib.sha256(content).hexdigest(),
            commit_sha=commit_sha, commit_message=commit_message,
        )

    def observe_ref_sha(self, repository: str, branch: str) -> str:
        encoded_branch = "/".join(quote(part, safe="") for part in branch.split("/"))
        response = httpx.get(
            f"{self._base}/repos/{repository}/git/ref/heads/{encoded_branch}",
            headers=self._headers(), timeout=self._timeout,
        )
        if response.status_code != 200:
            raise RuntimeError(f"GitHub target ref observation failed: HTTP {response.status_code}")
        sha = response.json().get("object", {}).get("sha")
        if not isinstance(sha, str) or len(sha) != 40:
            raise RuntimeError("GitHub target ref response omitted a valid SHA")
        return sha

    def write_file(
        self, repository: str, path: str, branch: str, content: str,
        message: str, before_sha: str | None, expected_base_sha: str,
    ) -> str:
        if not expected_base_sha or len(expected_base_sha) != 40:
            raise ValueError("GitHub apply requires the pinned base commit SHA")
        if before_sha is not None and len(before_sha) != 40:
            raise ValueError("GitHub update baseline blob SHA is invalid")
        marker_prefix = "aixion-apply-intent:"
        if marker_prefix not in message:
            raise ValueError("GitHub apply commit message lacks its durable intent marker")
        client_mutation_id = message.rsplit(marker_prefix, 1)[1].split("]", 1)[0]
        if not client_mutation_id or any(char in client_mutation_id for char in "()[] \t\r\n"):
            raise ValueError("GitHub apply intent marker is malformed")
        query = (
            "mutation CreateApprovedFileChange($input: CreateCommitOnBranchInput!) { "
            "createCommitOnBranch(input: $input) { commit { oid message } ref { target { oid } } } }"
        )
        # One GraphQL mutation creates one commit with exactly one addition and
        # expectedHeadOid. Never retry this request; any transport failure is
        # ambiguous and must be reconciled by GET only.
        response = httpx.post(
            f"{self._base}/graphql", headers=self._headers(), timeout=self._timeout,
            json={
                "query": query,
                "variables": {
                    "input": {
                        "branch": {
                            "repositoryNameWithOwner": repository,
                            "branchName": branch,
                        },
                        "expectedHeadOid": expected_base_sha,
                        "clientMutationId": client_mutation_id,
                        "fileChanges": {
                            "additions": [{
                                "path": path,
                                "contents": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                            }],
                        },
                        "message": {
                            "headline": "Apply approved AgentTask file change",
                            "body": message,
                        },
                    },
                },
            },
        )
        if response.status_code != 200:
            raise RuntimeError(f"GitHub GraphQL file mutation failed: HTTP {response.status_code}")
        payload = response.json()
        if payload.get("errors"):
            raise RuntimeError("GitHub GraphQL file mutation returned an error")
        result = payload.get("data", {}).get("createCommitOnBranch")
        commit = result.get("commit") if isinstance(result, dict) else None
        commit_sha = commit.get("oid") if isinstance(commit, dict) else None
        ref = result.get("ref") if isinstance(result, dict) else None
        ref_target = ref.get("target") if isinstance(ref, dict) else None
        ref_sha = ref_target.get("oid") if isinstance(ref_target, dict) else None
        if (
            not isinstance(commit_sha, str) or len(commit_sha) != 40
            or not isinstance(ref_sha, str) or ref_sha != commit_sha
            or not isinstance(commit.get("message"), str)
            or "aixion-apply-intent:" not in commit["message"]
        ):
            raise RuntimeError("GitHub GraphQL mutation did not prove the resulting commit and intent marker")
        return commit_sha


def postgres_apply_changes_execution_dependencies(
    *, request: Request, run: AgentRun, step: AgentRunStep, actor: str,
) -> tuple[PostgresApplyChangesCoordinator, object]:
    if not postgres_apply_changes_enabled():
        raise HTTPException(
            status_code=503,
            detail=(
                "PostgreSQL APPLY_CHANGES is disabled by the default-off "
                "AIXION_POSTGRES_APPLY_CHANGES_ENABLED rollout gate."
            ),
        )
    if not GitHubApplyChangeProvider.supports_atomic_base_commit:
        raise HTTPException(status_code=503, detail="Configured GitHub provider lacks atomic branch-head compare-and-swap.")
    state = request.app.state
    runtime = getattr(state, "postgres_runtime", None)
    try:
        repositories = application_repositories_for_request(request)
    except Exception as error:
        raise HTTPException(status_code=503, detail="PostgreSQL apply requires the lifespan-owned repository bundle.") from error
    if (
        not isinstance(runtime, PostgresRuntime)
        or not isinstance(repositories, ApplicationRepositories)
        or runtime.repositories is not repositories
        or getattr(state, "postgres_repositories", None) is not repositories
        or getattr(state, "agent_run_repository", None) is not repositories.agent_runs
        or getattr(state, "agent_task_repository", None) is not repositories.agent_tasks
        or getattr(state, "approval_decision_repository", None) is not repositories.approval_decisions
        or getattr(state, "trust_repository", None) is not repositories.trust
        or getattr(state, "trust_event_repository", None) is not repositories.trust_events
        or not isinstance(repositories.agent_runs, PostgresAgentRunRepository)
        or not isinstance(repositories.agent_tasks, PostgresAgentTaskRepository)
        or not isinstance(repositories.approval_decisions, PostgresApprovalDecisionRepository)
        or not isinstance(repositories.trust, PostgresTrustRepository)
        or not isinstance(repositories.agent_run_apply_change_intents, PostgresAgentRunApplyChangeIntentRepository)
        or step.step_type != AgentRunStepType.APPLY_CHANGES
        or step.run_id != run.id or step.task_id != run.task_id
        or not run.workspace_id or not run.approval_request_id or not run.capability_lease_id
    ):
        raise HTTPException(status_code=503, detail="PostgreSQL APPLY_CHANGES authority repositories are incomplete.")
    task = repositories.agent_tasks.get(run.task_id)
    approval = repositories.approval_decisions.get(run.approval_request_id)
    with runtime.connect() as conn:
        lease = repositories.trust.get_capability_lease(conn, run.capability_lease_id, run.workspace_id, for_share=True)
    if (
        task is None or approval is None or lease is None
        or task.id != run.task_id or task.workspace_id != run.workspace_id
        or task.project_id != run.project_id or task.approval_request_id != approval.id
        or approval.id != run.approval_request_id or approval.workspace_id != run.workspace_id
        or approval.project_id != task.project_id or task.status != AgentTaskStatus.APPROVED
        or approval.status != ApprovalStatus.APPROVED or lease.id != run.capability_lease_id
    ):
        raise HTTPException(status_code=409, detail="Durable APPLY_CHANGES task, approval, or lease is incomplete.")
    if len(approval.files) != 1:
        raise HTTPException(status_code=409, detail="PostgreSQL APPLY_CHANGES currently requires exactly one approved file.")
    change = approval.files[0]
    change_type = change.change_type.strip().lower()
    path = change.path
    if change_type not in {"create", "update"} or change.new_content is None:
        raise HTTPException(status_code=409, detail="PostgreSQL APPLY_CHANGES supports only approved create/update content.")
    if not task.repository or not task.branch_preference or not _valid_repo_path(path):
        raise HTTPException(status_code=409, detail="Approved file target is outside the supported repository scope.")
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        raise HTTPException(status_code=503, detail="GitHub provider is not configured for PostgreSQL APPLY_CHANGES.")
    provider: FileMutationProvider = GitHubApplyChangeProvider(token)
    coordinator = PostgresApplyChangesCoordinator(
        intent_repository=repositories.agent_run_apply_change_intents,
        run_repository=repositories.agent_runs, provider=provider, clock=now_utc,
    )

    def request_factory(current_run, current_step, worker_lease_token: str, timeout_seconds: int):
        if current_run.id != run.id or current_step.id != step.id or current_run.capability_lease_id != lease.id:
            raise ValueError("APPLY_CHANGES execution changed its durable run scope")
        content_hash = hashlib.sha256(change.new_content.encode("utf-8")).hexdigest()
        branch_steps = [
            item for item in repositories.agent_runs.list_steps_for_run(run.id)
            if item.step_type == AgentRunStepType.CREATE_BRANCH
        ]
        if len(branch_steps) != 1:
            raise ValueError("APPLY_CHANGES requires exactly one durable CREATE_BRANCH step")
        branch_step = branch_steps[0]
        branch_intent = repositories.agent_run_branch_intents.get_for_step(
            workspace_id=run.workspace_id, run_id=run.id, step_id=branch_step.id,
            idempotency_key=branch_step.idempotency_key,
        )
        if (
            branch_intent is None or branch_intent.status != BranchIntentStatus.COMPLETED
            or branch_intent.repository != task.repository
            or branch_intent.target_ref != task.branch_preference
        ):
            raise ValueError("APPLY_CHANGES requires the completed run-bound branch intent")
        existing_intent = repositories.agent_run_apply_change_intents.get_for_step(
            workspace_id=run.workspace_id, run_id=run.id, step_id=step.id,
            idempotency_key=step.idempotency_key,
        )
        if existing_intent is None:
            if provider.observe_ref_sha(task.repository or "", task.branch_preference or "") != branch_intent.source_sha:
                raise ValueError("APPLY_CHANGES target ref advanced from the approved branch base")
            # Bind update/create preflight to the immutable branch source commit.
            base_observation = provider.observe_file(task.repository or "", path, branch_intent.source_sha)
            if change_type == "create" and base_observation.exists:
                raise ValueError("Approved create target already exists at the run's pinned branch base")
            if change_type == "update" and not base_observation.exists:
                raise ValueError("Approved update target is absent at the run's pinned branch base")
            approved_before_sha = base_observation.blob_sha if change_type == "update" else None
        else:
            if (
                existing_intent.path != path or existing_intent.repository != task.repository
                or existing_intent.branch != task.branch_preference
                or existing_intent.change_type != change_type
                or existing_intent.desired_content_sha256 != content_hash
            ):
                raise ValueError("Persisted apply intent differs from the current approved request")
            approved_before_sha = existing_intent.before_sha

        def action_factory(before_sha: str | None) -> ProposedAction:
            identity = sha256_hex({"run_id": run.id, "step_id": step.id,
                                   "idempotency_key": step.idempotency_key,
                                   "lease_id": lease.id, "path": path,
                                   "change_type": change_type, "before_sha": before_sha,
                                   "content_sha256": content_hash})
            return ProposedAction(
                id=f"action_{identity}", lease_id=lease.id, provider=task.provider,
                agent_id=task.external_agent_id, run_id=run.id, task_id=task.id,
                project_id=task.project_id, workspace_id=run.workspace_id,
                action_type=CapabilityActionType.MODIFY_FILES, repository=task.repository,
                branch=task.branch_preference, paths=[path],
                metadata={"step_id": step.id, "step_type": "APPLY_CHANGES",
                          "idempotency_key": step.idempotency_key, "task_id": task.id,
                          "approval_id": approval.id, "lease_id": lease.id,
                          "repository": task.repository, "target_ref": task.branch_preference,
                          "path": path, "change_type": change_type,
                          "before_sha": before_sha, "content_sha256": content_hash,
                          "base_commit_sha": branch_intent.source_sha},
            )

        def intent_factory(before_sha: str | None, action: ProposedAction) -> AgentRunApplyChangeIntent:
            stamp = now_utc()
            return AgentRunApplyChangeIntent(
                id=f"apply_intent_{sha256_hex(f'{run.id}:{step.id}:{step.idempotency_key}')}",
                workspace_id=run.workspace_id, run_id=run.id, task_id=task.id,
                approval_id=approval.id, step_id=step.id, action_id=action.id,
                lease_id=lease.id, idempotency_key=step.idempotency_key,
                repository=task.repository or "", branch=task.branch_preference or "",
                path=path, change_type=change_type, before_sha=before_sha,
                desired_content_sha256=content_hash,
                lease_token_hash=sha256_hex({"lease_token": worker_lease_token}),
                run_revision=current_run.updated_at, step_revision=current_step.updated_at,
                created_at=stamp, updated_at=stamp,
            )

        def completion_factory(intent, commit_sha):
            return build_postgres_apply_change_completion(
                run=current_run, steps=repositories.agent_runs.list_steps_for_run(run.id),
                intent=intent, commit_sha=commit_sha, actor=actor,
                worker_lease_token=worker_lease_token,
            )

        return ApplyChangeRequest(
            workspace_id=run.workspace_id, actor=actor, worker_lease_token=worker_lease_token,
            expected_run=current_run, expected_step=current_step, approval=approval, lease=lease,
            repository=task.repository or "", branch=task.branch_preference or "", path=path,
            base_commit_sha=branch_intent.source_sha,
            change_type=change_type, content=change.new_content, before_sha=approved_before_sha,
            timeout_seconds=timeout_seconds, action_factory=action_factory,
            intent_factory=intent_factory, completion_factory=completion_factory,
        )

    return coordinator, request_factory
