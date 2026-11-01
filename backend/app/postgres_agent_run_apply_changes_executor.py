from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import os
from typing import Callable, Protocol
import unicodedata

from .agent_run_models import AgentRun, AgentRunStep
from .models import ApprovalRequest
from .agent_run_repository import AgentRunRepository
from .postgres_agent_run_apply_changes import (
    AgentRunApplyChangeIntent,
    ApplyChangeIntentStatus,
    PostgresAgentRunApplyChangeIntentRepository,
)
from .trust_models import CapabilityLease, ProposedAction


@dataclass(frozen=True)
class FileObservation:
    exists: bool
    blob_sha: str | None = None
    content_sha256: str | None = None
    commit_sha: str | None = None
    commit_message: str | None = None


class FileMutationProvider(Protocol):
    def observe_ref_sha(self, repository: str, branch: str) -> str: ...

    def observe_file(self, repository: str, path: str, branch: str) -> FileObservation: ...

    def write_file(
        self, repository: str, path: str, branch: str, content: str,
        message: str, before_sha: str | None, expected_base_sha: str,
    ) -> str: ...


class ApplyChangeState(StrEnum):
    BLOCKED = "BLOCKED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    COMPLETED = "COMPLETED"


@dataclass(frozen=True)
class ApplyChangeRequest:
    workspace_id: str
    actor: str
    worker_lease_token: str
    expected_run: AgentRun
    expected_step: AgentRunStep
    approval: ApprovalRequest
    lease: CapabilityLease
    repository: str
    branch: str
    base_commit_sha: str
    path: str
    change_type: str
    content: str
    before_sha: str | None
    timeout_seconds: int
    action_factory: Callable[[str | None], ProposedAction]
    intent_factory: Callable[[str | None, ProposedAction], AgentRunApplyChangeIntent]
    completion_factory: Callable[[AgentRunApplyChangeIntent, str], object]


@dataclass(frozen=True)
class ApplyChangeResult:
    state: ApplyChangeState
    intent: AgentRunApplyChangeIntent | None
    reason: str
    commit_sha: str | None = None


def postgres_apply_changes_enabled() -> bool:
    """Explicit default-off rollout gate for PostgreSQL file mutations."""
    return os.getenv("AIXION_POSTGRES_APPLY_CHANGES_ENABLED", "false").strip().lower() in {
        "1", "true", "yes",
    }


class PostgresApplyChangesCoordinator:
    """One approved create/update, one durable write claim, then GET-only recovery."""

    def __init__(
        self, *, intent_repository: PostgresAgentRunApplyChangeIntentRepository,
        run_repository: AgentRunRepository, provider: FileMutationProvider,
        clock: Callable[[], datetime],
    ) -> None:
        self._intents = intent_repository
        self._runs = run_repository
        self._provider = provider
        self._clock = clock

    def execute(self, request: ApplyChangeRequest) -> ApplyChangeResult:
        self._validate(request)
        content_hash = __import__("hashlib").sha256(request.content.encode("utf-8")).hexdigest()
        intent = self._intents.get_for_step(
            workspace_id=request.workspace_id, run_id=request.expected_run.id,
            step_id=request.expected_step.id,
            idempotency_key=request.expected_step.idempotency_key,
        )
        if intent is None:
            try:
                observed = self._provider.observe_file(request.repository, request.path, request.branch)
            except Exception as error:
                return ApplyChangeResult(ApplyChangeState.BLOCKED, None, f"GitHub preflight failed: {error}")
            if (
                (request.change_type == "create" and observed.exists)
                or (request.change_type == "update" and (not observed.exists or observed.blob_sha != request.before_sha))
            ):
                return ApplyChangeResult(ApplyChangeState.BLOCKED, None, "GitHub file state differs from approved preflight.")
            action = request.action_factory(observed.blob_sha)
            intent = request.intent_factory(observed.blob_sha, action)
            try:
                intent = self._intents.record_authorized_intent(
                    record=intent, action=action, expected_run=request.expected_run,
                    expected_step=request.expected_step, timeout_seconds=request.timeout_seconds,
                    actor=request.actor, now=self._clock(),
                )
            except Exception as error:
                return ApplyChangeResult(ApplyChangeState.BLOCKED, None, f"Exact action authorization failed: {error}")
        if not self._matches(request, intent, content_hash):
            return ApplyChangeResult(ApplyChangeState.BLOCKED, intent, "Persisted apply intent differs from exact approved file action.")
        if intent.status == ApplyChangeIntentStatus.COMPLETED:
            return ApplyChangeResult(ApplyChangeState.COMPLETED, intent, "Apply intent is already completed.", intent.commit_sha)

        if intent.status in {ApplyChangeIntentStatus.POST_CLAIMED, ApplyChangeIntentStatus.RECONCILIATION_REQUIRED}:
            try:
                observed = self._provider.observe_file(request.repository, request.path, request.branch)
            except Exception:
                return ApplyChangeResult(ApplyChangeState.RECONCILIATION_REQUIRED, intent, "GitHub state is unavailable; no write retry is permitted.")
            marker = f"aixion-apply-intent:{intent.id}"
            if (
                observed.exists and observed.content_sha256 == intent.desired_content_sha256
                and observed.commit_sha and marker in (observed.commit_message or "")
            ):
                try:
                    intent = self._intents.record_observation(
                        intent=intent, expected_status=intent.status,
                        status=ApplyChangeIntentStatus.REMOTE_VERIFIED,
                        commit_sha=observed.commit_sha, reason="Observed the exact intent marker and approved content after one-shot write claim.",
                        now=self._clock(),
                    )
                except Exception as error:
                    return ApplyChangeResult(ApplyChangeState.RECONCILIATION_REQUIRED, intent, f"Could not persist remote verification: {error}")
            else:
                try:
                    intent = self._intents.record_observation(
                        intent=intent, expected_status=intent.status,
                        status=ApplyChangeIntentStatus.RECONCILIATION_REQUIRED,
                        commit_sha=observed.commit_sha,
                        reason="Remote file does not prove the approved result; write remains permanently fenced.",
                        now=self._clock(),
                    )
                except Exception:
                    pass
                return ApplyChangeResult(ApplyChangeState.RECONCILIATION_REQUIRED, intent, intent.reason)
        elif intent.status == ApplyChangeIntentStatus.INTENT:
            try:
                intent = self._intents.claim_write_once(
                    intent=intent, expected_run=request.expected_run,
                    expected_step=request.expected_step,
                    worker_lease_token=request.worker_lease_token, now=self._clock(),
                )
            except Exception as error:
                return ApplyChangeResult(ApplyChangeState.BLOCKED, intent, f"One-shot write claim failed: {error}")
            try:
                if self._provider.observe_ref_sha(request.repository, request.branch) != request.base_commit_sha:
                    intent = self._intents.record_observation(
                        intent=intent, expected_status=ApplyChangeIntentStatus.POST_CLAIMED,
                        status=ApplyChangeIntentStatus.RECONCILIATION_REQUIRED,
                        commit_sha=None,
                        reason="Target ref advanced after authorization; write was not sent and remains fenced.",
                        now=self._clock(),
                    )
                    return ApplyChangeResult(ApplyChangeState.RECONCILIATION_REQUIRED, intent, intent.reason)
            except Exception as error:
                intent = self._intents.record_observation(
                    intent=intent, expected_status=ApplyChangeIntentStatus.POST_CLAIMED,
                    status=ApplyChangeIntentStatus.RECONCILIATION_REQUIRED,
                    commit_sha=None, reason=f"Could not verify target ref before write: {error}", now=self._clock(),
                )
                return ApplyChangeResult(ApplyChangeState.RECONCILIATION_REQUIRED, intent, intent.reason)
            try:
                commit_sha = self._provider.write_file(
                    request.repository, request.path, request.branch, request.content,
                    f"Apply approved AgentTask change: {request.change_type} {request.path} "
                    f"({request.expected_run.task_id}) [aixion-apply-intent:{intent.id}]",
                    intent.before_sha, request.base_commit_sha,
                )
            except Exception as error:
                # The committed claim makes this ambiguous forever; only next-call GET reconciliation is allowed.
                try:
                    intent = self._intents.record_observation(
                        intent=intent, expected_status=ApplyChangeIntentStatus.POST_CLAIMED,
                        status=ApplyChangeIntentStatus.RECONCILIATION_REQUIRED,
                        commit_sha=None, reason=f"GitHub write outcome is ambiguous: {error}", now=self._clock(),
                    )
                except Exception:
                    pass
                return ApplyChangeResult(ApplyChangeState.RECONCILIATION_REQUIRED, intent, intent.reason)
            try:
                observed = self._provider.observe_file(request.repository, request.path, request.branch)
            except Exception:
                observed = None
            marker = f"aixion-apply-intent:{intent.id}"
            if (
                observed is None or not observed.exists
                or observed.content_sha256 != content_hash
                or observed.commit_sha != commit_sha
                or marker not in (observed.commit_message or "")
            ):
                return ApplyChangeResult(ApplyChangeState.RECONCILIATION_REQUIRED, intent, "Write response did not prove exact remote file contents.")
            intent = self._intents.record_observation(
                intent=intent, expected_status=ApplyChangeIntentStatus.POST_CLAIMED,
                status=ApplyChangeIntentStatus.REMOTE_VERIFIED, commit_sha=commit_sha,
                reason="GitHub write and exact remote content were verified.", now=self._clock(),
            )
        if intent.status != ApplyChangeIntentStatus.REMOTE_VERIFIED:
            return ApplyChangeResult(ApplyChangeState.RECONCILIATION_REQUIRED, intent, "Apply intent is not remotely verified.")
        try:
            # A verified intent never writes again. Refresh exact content by GET before
            # committing or recovering the AgentRun outcome under a new worker lease.
            observed = self._provider.observe_file(request.repository, request.path, request.branch)
            marker = f"aixion-apply-intent:{intent.id}"
            if (
                not observed.exists or observed.content_sha256 != intent.desired_content_sha256
                or not observed.commit_sha or marker not in (observed.commit_message or "")
            ):
                return ApplyChangeResult(
                    ApplyChangeState.RECONCILIATION_REQUIRED, intent,
                    "Previously verified content no longer matches; no provider write is permitted.",
                )
            intent = self._intents.rebind_verified_completion(
                intent=intent, expected_run=request.expected_run,
                expected_step=request.expected_step,
                worker_lease_token=request.worker_lease_token, now=self._clock(),
            )
            completion = request.completion_factory(intent, intent.commit_sha or "")
            self._runs.commit_step_completion(
                expected_run=completion.expected_run,
                updated_run=completion.updated_run,
                expected_steps=completion.expected_steps,
                updated_steps=completion.updated_steps,
                events=completion.events,
                audit=completion.audit,
                now=completion.now,
                apply_change_intent_id=intent.id,
            )
        except Exception as error:
            return ApplyChangeResult(ApplyChangeState.RECONCILIATION_REQUIRED, intent, f"Remote mutation is verified but atomic outcome is pending: {error}")
        completed = intent.model_copy(update={"status": ApplyChangeIntentStatus.COMPLETED})
        return ApplyChangeResult(ApplyChangeState.COMPLETED, completed, "Approved file change was committed atomically.", intent.commit_sha)

    @staticmethod
    def _matches(request: ApplyChangeRequest, intent: AgentRunApplyChangeIntent, content_hash: str) -> bool:
        return (
            intent.workspace_id == request.workspace_id
            and intent.run_id == request.expected_run.id
            and intent.task_id == request.expected_run.task_id
            and intent.approval_id == request.approval.id
            and intent.step_id == request.expected_step.id
            and intent.lease_id == request.lease.id
            and intent.idempotency_key == request.expected_step.idempotency_key
            and intent.repository == request.repository and intent.branch == request.branch
            and intent.path == request.path and intent.change_type == request.change_type
            and intent.before_sha == request.before_sha
            and intent.desired_content_sha256 == content_hash
        )

    @staticmethod
    def _validate(request: ApplyChangeRequest) -> None:
        if (
            request.expected_step.step_type.value != "APPLY_CHANGES"
            or request.change_type not in {"create", "update"}
            or not request.path or request.path.startswith("/") or "\\" in request.path
            or request.path != request.path.strip()
            or any(part != part.strip() for part in request.path.split("/"))
            or any(part in {"", ".", ".."} for part in request.path.split("/"))
            or any(unicodedata.category(char) in {"Cc", "Cf"} for char in request.path)
            or request.expected_run.workspace_id != request.workspace_id
            or request.expected_run.lease_token != request.worker_lease_token
            or request.expected_step.lease_token != request.worker_lease_token
            or request.expected_run.capability_lease_id != request.lease.id
            or len(request.base_commit_sha) != 40
            or request.expected_run.approval_request_id != request.approval.id
            or request.approval.files is None or len(request.approval.files) != 1
            or request.approval.files[0].path != request.path
            or request.approval.files[0].change_type.strip().lower() != request.change_type
            or request.approval.files[0].new_content != request.content
        ):
            raise ValueError("APPLY_CHANGES request is outside the one-file approved create/update contract")
