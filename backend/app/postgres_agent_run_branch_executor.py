from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Callable, Protocol

from .agent_run_models import (
    AgentRun,
    AgentRunEvent,
    AgentRunEventType,
    AgentRunStep,
    AgentRunStepStatus,
)
from .agent_run_repository import AgentRunRepository
from .agent_task_models import AgentTaskEvent, AgentTaskEventType
from .models import AuditEvent, ApprovalRequest
from .agent_task_models import AgentTask
from .agent_worker_github_branch import GitHubBranchObservation
from .postgres_agent_run_branch import (
    AgentRunBranchIntent,
    BranchIntentStatus,
    PostgresAgentRunBranchIntentRepository,
)
from .postgres_trust_repository import PostgresTrustRepositoryConflict
from .trust_models import CapabilityLease, ProposedAction
from .trust_crypto import sha256_hex


class BranchExecutionState(StrEnum):
    BLOCKED = "BLOCKED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    VERIFIED = "VERIFIED"
    COMPLETED = "COMPLETED"


@dataclass(frozen=True)
class BranchCompletion:
    """CAS-bound AgentRun and task records needed for atomic verified completion."""

    expected_run: AgentRun
    updated_run: AgentRun
    expected_steps: list[AgentRunStep]
    updated_steps: list[AgentRunStep]
    events: list[AgentRunEvent]
    audit: AuditEvent
    branch_task_event: AgentTaskEvent
    branch_task_audit: AuditEvent
    now: datetime


@dataclass(frozen=True)
class BranchExecutionResult:
    state: BranchExecutionState
    intent: AgentRunBranchIntent | None
    reason: str
    verified_sha: str | None = None
    branch_url: str | None = None


class BranchProvider(Protocol):
    def observe_branch(self, repository: str, branch: str) -> GitHubBranchObservation: ...

    def create_branch(
        self, repository: str, branch: str, source_sha: str
    ) -> Any: ...


@dataclass(frozen=True)
class BranchExecutionRequest:
    workspace_id: str
    actor: str
    worker_lease_token: str
    expected_run: AgentRun
    expected_step: AgentRunStep
    task: AgentTask
    approval: ApprovalRequest
    lease: CapabilityLease
    repository: str
    source_branch: str
    target_ref: str
    timeout_seconds: int
    action_factory: Callable[[str], ProposedAction]
    intent_factory: Callable[[str, ProposedAction], AgentRunBranchIntent]
    completion_factory: Callable[[AgentRunBranchIntent, str], BranchCompletion]


class PostgresCreateBranchCoordinator:
    """Fail-closed CREATE_BRANCH sequencing around the durable one-shot POST claim.

    This coordinator intentionally does not move AgentTask to READY_FOR_PR. The
    caller may use the successful step result to proceed to later AgentRun steps.
    """

    def __init__(
        self,
        *,
        intent_repository: PostgresAgentRunBranchIntentRepository,
        run_repository: AgentRunRepository,
        provider: BranchProvider,
        clock: Callable[[], datetime],
    ) -> None:
        self._intents = intent_repository
        self._runs = run_repository
        self._provider = provider
        self._clock = clock

    def execute(self, request: BranchExecutionRequest) -> BranchExecutionResult:
        self._validate_request(request)
        intent = self._intents.get_for_step(
            workspace_id=request.workspace_id,
            run_id=request.expected_run.id,
            step_id=request.expected_step.id,
            idempotency_key=request.expected_step.idempotency_key,
        )
        if intent is not None:
            self._validate_existing_intent(request, intent)
            if intent.status == BranchIntentStatus.COMPLETED:
                return BranchExecutionResult(
                    BranchExecutionState.COMPLETED,
                    intent,
                    "Branch intent is already completed.",
                    intent.source_sha,
                    intent.branch_url,
                )

        post_claimed = intent is not None and intent.status in {
            BranchIntentStatus.POST_CLAIMED,
            BranchIntentStatus.REMOTE_VERIFIED,
            BranchIntentStatus.RECONCILIATION_REQUIRED,
        }

        if post_claimed:
            # The durable intent is authoritative for the pinned SHA and action
            # identity. Recovery is read-only and does not require a new policy
            # decision or a live capability lease.
            assert intent is not None
            action = request.action_factory(intent.source_sha)
            self._validate_action(request, action, intent.source_sha)
            if action.id != intent.action_id:
                raise PostgresTrustRepositoryConflict(
                    "Recovery action identity differs from the durable claimed intent"
                )
        else:
            # For a new or unclaimed intent, obtain the pinned SHA before
            # evaluation. An existing INTENT must still match its persisted pin.
            source = self._observe(request.repository, request.source_branch)
            if source is None or not source.found or not self._valid_sha(source.sha):
                return self._blocked(intent, "Pinned source ref could not be observed safely.")
            pinned_sha = source.sha
            if intent is not None and pinned_sha != intent.source_sha:
                return self._blocked(intent, "Unclaimed intent source ref no longer matches its pinned SHA.")

            action = request.action_factory(pinned_sha)
            self._validate_action(request, action, pinned_sha)
            if intent is not None and action.id != intent.action_id:
                raise PostgresTrustRepositoryConflict(
                    "Unclaimed action identity differs from the durable intent"
                )
            decision = self._intents.evaluate_action(
                action=action,
                workspace_id=request.workspace_id,
                run_id=request.expected_run.id,
                step_id=request.expected_step.id,
                actor=request.actor,
                now=self._clock(),
            )
            if (
                decision is None
                or decision.decision.value != "ALLOW"
                or decision.proposed_action_id != action.id
                or decision.lease_id != action.lease_id
            ):
                return self._blocked(intent, "CREATE_BRANCH action was not exactly authorized.")

            record = request.intent_factory(pinned_sha, action)
            self._validate_intent(request, record, action, pinned_sha)
            intent, _, _, _, _ = self._intents.begin_intent(
                record=record,
                expected_run=request.expected_run,
                expected_step=request.expected_step,
                timeout_seconds=request.timeout_seconds,
                actor=request.actor,
                now=self._clock(),
            )
            if intent.action_id != action.id:
                raise PostgresTrustRepositoryConflict("Durable intent does not bind the evaluated action")

        # Rebind is a CAS-only database operation; it has no provider effects.
        if intent.status == BranchIntentStatus.INTENT:
            intent = self._intents.rebind_intent_lease(
                intent=intent,
                expected_run=request.expected_run,
                expected_step=request.expected_step,
                worker_lease_token=request.worker_lease_token,
                actor=request.actor,
                now=self._clock(),
            )
            source_check = self._observe(request.repository, request.source_branch)
            if (
                source_check is None
                or not source_check.found
                or source_check.sha != intent.source_sha
            ):
                return self._record_unresolved(
                    request, intent, None, "Pinned source ref changed or became uncertain before claim."
                )
            target_check = self._observe(request.repository, request.target_ref)
            if target_check is None:
                return self._record_unresolved(
                    request, intent, None, "Target ref state is uncertain before claim."
                )
            if target_check.found:
                return self._record_unresolved(
                    request, intent, target_check, "Target ref already exists before the one-shot claim."
                )
            intent = self._intents.claim_create_once(
                intent=intent,
                expected_run=request.expected_run,
                expected_step=request.expected_step,
                worker_lease_token=request.worker_lease_token,
                now=self._clock(),
            )
            # This is the sole POST site. Once claimed, exceptions are ambiguous
            # and flow directly to GET-only reconciliation below.
            post_error: Exception | None = None
            try:
                self._provider.create_branch(
                    request.repository, request.target_ref, intent.source_sha
                )
            except Exception as error:  # provider acknowledgement may be lost
                post_error = error
        elif intent.status in {
            BranchIntentStatus.POST_CLAIMED,
            BranchIntentStatus.REMOTE_VERIFIED,
            BranchIntentStatus.RECONCILIATION_REQUIRED,
        }:
            intent = self._intents.rebind_for_reconciliation(
                intent=intent,
                expected_run=request.expected_run,
                expected_step=request.expected_step,
                worker_lease_token=request.worker_lease_token,
                actor=request.actor,
                now=self._clock(),
            )
            post_error = None
        else:
            return self._blocked(intent, "Branch intent is in an unsupported state.")

        # After a committed POST claim (including restart), all provider access
        # is GET-only. Provider exceptions are unknown, never absence.
        observed = self._observe(request.repository, request.target_ref)
        if observed is None:
            return self._record_unresolved(
                request,
                intent,
                None,
                "Target observation failed after the one-shot claim; POST will not be retried.",
            )
        verified_sha = observed.sha if observed.found and self._valid_sha(observed.sha) else None
        reason = ""
        if not observed.found:
            reason = "Target ref is not yet visible after the one-shot claim."
        elif verified_sha != intent.source_sha:
            reason = "Target ref SHA is missing, malformed, or differs from the pinned source SHA."
        if post_error is not None:
            reason = (
                "POST acknowledgement was uncertain; GET-only reconciliation observed the target."
                if verified_sha == intent.source_sha
                else f"POST outcome is uncertain ({type(post_error).__name__}); GET-only reconciliation required."
            )

        intent = self._intents.record_remote_observation(
            intent=intent,
            expected_run=request.expected_run,
            expected_step=request.expected_step,
            worker_lease_token=request.worker_lease_token,
            verified_sha=verified_sha,
            branch_url=observed.html_url if observed.found else None,
            reason=reason,
            now=self._clock(),
        )
        if intent.status != BranchIntentStatus.REMOTE_VERIFIED or verified_sha != intent.source_sha:
            return BranchExecutionResult(
                BranchExecutionState.RECONCILIATION_REQUIRED,
                intent,
                reason or "Remote branch was not verified at the pinned SHA.",
                verified_sha,
                observed.html_url,
            )

        self._assert_current_lease(request, intent)
        completion = request.completion_factory(intent, observed.html_url or "")
        self._validate_completion(request, intent, observed.html_url or "", completion)
        self._runs.commit_step_completion(
            expected_run=completion.expected_run,
            updated_run=completion.updated_run,
            expected_steps=completion.expected_steps,
            updated_steps=completion.updated_steps,
            events=completion.events,
            audit=completion.audit,
            now=completion.now,
            branch_intent_id=intent.id,
            branch_task_event=completion.branch_task_event,
            branch_task_audit=completion.branch_task_audit,
        )
        return BranchExecutionResult(
            BranchExecutionState.COMPLETED,
            intent,
            "Remote branch SHA was verified and completion committed atomically.",
            verified_sha,
            observed.html_url,
        )

    def _observe(self, repository: str, branch: str) -> GitHubBranchObservation | None:
        try:
            result = self._provider.observe_branch(repository, branch)
        except Exception:
            return None
        if not isinstance(result, GitHubBranchObservation):
            return None
        if result.repository != repository or result.branch != branch:
            return None
        return result

    def _record_unresolved(
        self,
        request: BranchExecutionRequest,
        intent: AgentRunBranchIntent,
        observation: GitHubBranchObservation | None,
        reason: str,
    ) -> BranchExecutionResult:
        # An ambiguity before claim leaves INTENT unclaimed. A later attempt
        # must repeat live authorization and both preflight reads before it can
        # claim; reconciliation statuses after claim remain permanently GET-only.
        if intent.status not in {BranchIntentStatus.INTENT, BranchIntentStatus.COMPLETED}:
            intent = self._intents.record_remote_observation(
                intent=intent,
                expected_run=request.expected_run,
                expected_step=request.expected_step,
                worker_lease_token=request.worker_lease_token,
                verified_sha=None,
                branch_url=observation.html_url if observation and observation.found else None,
                reason=reason,
                now=self._clock(),
            )
        return self._blocked(intent, reason)

    @staticmethod
    def _valid_sha(value: str | None) -> bool:
        return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None

    @staticmethod
    def _validate_request(request: BranchExecutionRequest) -> None:
        if (
            not request.workspace_id
            or request.expected_run.workspace_id != request.workspace_id
            or request.expected_step.run_id != request.expected_run.id
            or request.expected_step.task_id != request.task.id
            or request.expected_step.step_type.value != "CREATE_BRANCH"
            or request.expected_run.task_id != request.task.id
            or request.task.workspace_id != request.workspace_id
            or request.expected_run.capability_lease_id != request.lease.id
            or request.expected_run.approval_request_id != request.approval.id
            or request.task.approval_request_id != request.approval.id
            or request.approval.workspace_id != request.workspace_id
            or not request.actor
            or not request.worker_lease_token
            or request.expected_run.lease_token != request.worker_lease_token
            or request.expected_step.lease_token != request.worker_lease_token
            or not 1 <= request.timeout_seconds <= 3600
            or not request.repository
            or not request.source_branch
            or not request.target_ref
        ):
            raise ValueError("CREATE_BRANCH execution request is outside its workspace/lease/step scope")

    @staticmethod
    def _validate_action(
        request: BranchExecutionRequest, action: ProposedAction, pinned_sha: str
    ) -> None:
        if (
            action.action_type.value != "CREATE_BRANCH"
            or action.workspace_id != request.workspace_id
            or action.run_id != request.expected_run.id
            or action.task_id != request.task.id
            or action.lease_id != request.lease.id
            or action.repository != request.repository
            or action.branch != request.target_ref
            or action.provider != request.task.provider
            or action.agent_id != request.task.external_agent_id
            or action.project_id != request.task.project_id
            or action.metadata.get("source_sha") != pinned_sha
            or action.metadata.get("source_branch") != request.source_branch
            or action.metadata.get("target_ref") != request.target_ref
            or action.metadata.get("step_id") != request.expected_step.id
            or action.metadata.get("idempotency_key") != request.expected_step.idempotency_key
            or action.metadata.get("task_id") != request.task.id
            or action.metadata.get("approval_id") != request.approval.id
            or action.metadata.get("lease_id") != request.lease.id
            or action.metadata.get("repository") != request.repository
        ):
            raise ValueError("Proposed CREATE_BRANCH action does not match the exact request")

    @staticmethod
    def _validate_intent(
        request: BranchExecutionRequest,
        intent: AgentRunBranchIntent,
        action: ProposedAction,
        pinned_sha: str,
    ) -> None:
        if (
            intent.workspace_id != request.workspace_id
            or intent.run_id != request.expected_run.id
            or intent.task_id != request.task.id
            or intent.approval_id != request.approval.id
            or intent.step_id != request.expected_step.id
            or intent.action_id != action.id
            or intent.lease_id != request.lease.id
            or intent.repository != request.repository
            or intent.source_branch != request.source_branch
            or intent.target_ref != request.target_ref
            or intent.source_sha != pinned_sha
            or intent.idempotency_key != request.expected_step.idempotency_key
        ):
            raise ValueError("Branch intent does not match the exact approved action and step")

    @staticmethod
    def _validate_existing_intent(
        request: BranchExecutionRequest, intent: AgentRunBranchIntent
    ) -> None:
        if (
            intent.workspace_id != request.workspace_id
            or intent.run_id != request.expected_run.id
            or intent.task_id != request.task.id
            or intent.approval_id != request.approval.id
            or intent.step_id != request.expected_step.id
            or intent.lease_id != request.lease.id
            or intent.idempotency_key != request.expected_step.idempotency_key
            or intent.repository != request.repository
            or intent.source_branch != request.source_branch
            or intent.target_ref != request.target_ref
            or not PostgresCreateBranchCoordinator._valid_sha(intent.source_sha)
        ):
            raise PostgresTrustRepositoryConflict("Loaded branch intent does not match this execution scope")

    @staticmethod
    def _assert_current_lease(
        request: BranchExecutionRequest, intent: AgentRunBranchIntent
    ) -> None:
        if (
            request.expected_run.lease_token != request.worker_lease_token
            or request.expected_step.lease_token != request.worker_lease_token
            or request.expected_run.lease_owner != request.actor
            or request.expected_step.lease_owner != request.actor
            or request.expected_run.updated_at != intent.run_revision
            or request.expected_step.updated_at != intent.step_revision
            or intent.lease_token_hash != sha256_hex({"lease_token": request.worker_lease_token})
        ):
            raise PostgresTrustRepositoryConflict("Branch completion has stale worker lease authority")

    @staticmethod
    def _validate_completion(
        request: BranchExecutionRequest,
        intent: AgentRunBranchIntent,
        branch_url: str,
        completion: BranchCompletion,
    ) -> None:
        expected_steps = [item for item in completion.expected_steps if item.id == intent.step_id]
        updated_steps = [item for item in completion.updated_steps if item.id == intent.step_id]
        expected_branch_facts = {
            "branch_intent_id": intent.id,
            "repository": intent.repository,
            "source_branch": intent.source_branch,
            "target_ref": intent.target_ref,
            "source_sha": intent.source_sha,
            "branch_url": branch_url,
        }
        step_event = next(
            (
                event
                for event in completion.events
                if event.step_id == intent.step_id
                and event.run_id == intent.run_id
                and event.task_id == intent.task_id
                and event.actor == request.actor
                and event.event_type == AgentRunEventType.STEP_SUCCEEDED
            ),
            None,
        )
        if (
            completion.expected_run != request.expected_run
            or len(expected_steps) != 1
            or expected_steps[0] != request.expected_step
            or len(updated_steps) != 1
            or updated_steps[0].status != AgentRunStepStatus.SUCCEEDED
            or updated_steps[0].idempotency_key != intent.idempotency_key
            or completion.expected_run.workspace_id != intent.workspace_id
            or completion.expected_run.task_id != intent.task_id
            or completion.updated_run.workspace_id != intent.workspace_id
            or completion.updated_run.task_id != intent.task_id
            or completion.audit.workspace_id != intent.workspace_id
            or completion.audit.entity_id != intent.run_id
            or completion.audit.actor != request.actor
            or any(completion.audit.details.get(key) != value for key, value in expected_branch_facts.items())
            or completion.branch_task_event.task_id != intent.task_id
            or completion.branch_task_event.actor != request.actor
            or completion.branch_task_event.event_type != AgentTaskEventType.NOTE
            or completion.branch_task_event.status is not None
            or any(
                completion.branch_task_event.metadata.get(key) != value
                for key, value in expected_branch_facts.items()
            )
            or completion.branch_task_audit.workspace_id != intent.workspace_id
            or completion.branch_task_audit.entity_id != intent.task_id
            or completion.branch_task_audit.actor != request.actor
            or completion.branch_task_audit.event_type != "agent_task.branch_created"
            or any(
                completion.branch_task_audit.details.get(key) != value
                for key, value in expected_branch_facts.items()
            )
            or step_event is None
            or any(step_event.metadata.get(key) != value for key, value in expected_branch_facts.items())
        ):
            raise ValueError("Branch completion records are not bound to the verified intent and lease")

    @staticmethod
    def _blocked(
        intent: AgentRunBranchIntent | None, reason: str
    ) -> BranchExecutionResult:
        return BranchExecutionResult(BranchExecutionState.BLOCKED, intent, reason)
