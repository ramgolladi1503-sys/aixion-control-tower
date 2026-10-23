from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.agent_run_models import (
    AgentRun,
    AgentRunEventType,
    AgentRunStep,
    AgentRunStepStatus,
    AgentRunStepType,
)
from app.agent_task_models import AgentTaskEventType
from app.agent_worker_github_branch import GitHubBranchObservation
from app.models import AgentProvider, AuditEvent, RiskLevel
from app.postgres_agent_run_branch import AgentRunBranchIntent, BranchIntentStatus
from app.postgres_agent_run_branch_executor import (
    BranchCompletion,
    BranchExecutionRequest,
    BranchExecutionState,
    PostgresCreateBranchCoordinator,
)
from app.trust_crypto import sha256_hex
from app.trust_models import (
    CapabilityActionType,
    PolicyDecision,
    PolicyDecisionType,
    ProposedAction,
)


NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)
SHA = "a" * 40
REF_URL = "https://github.com/acme/example/tree/feature/test"
SOURCE_URL = "https://github.com/acme/example/tree/main"


class FakeProvider:
    def __init__(self, source=None, target=None, post_error=None):
        self.log = []
        self.observations = {"main": list(source or []), "feature/test": list(target or [])}
        self.post_error = post_error
        self.post_count = 0

    def observe_branch(self, repository, branch):
        self.log.append(("get", branch))
        result = self.observations[branch].pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def create_branch(self, repository, branch, source_sha):
        self.log.append(("post", branch, source_sha))
        self.post_count += 1
        if self.post_error:
            raise self.post_error


class FakeIntentRepository:
    def __init__(self, log, *, existing=None, allow=True):
        self.log = log
        self.intent = existing
        self.allow = allow

    def get_for_step(self, **kwargs):
        self.log.append(("load-intent",))
        return self.intent

    def evaluate_action(self, **kwargs):
        self.log.append(("evaluate", kwargs["action"].id))
        decision = PolicyDecision(
            proposed_action_id=kwargs["action"].id,
            lease_id=kwargs["action"].lease_id,
            decision=PolicyDecisionType.ALLOW if self.allow else PolicyDecisionType.BLOCK,
            risk_level=RiskLevel.MEDIUM,
        )
        return decision

    def begin_intent(self, *, record, **kwargs):
        self.log.append(("begin", record.source_sha))
        if self.intent is None:
            self.intent = record
        return self.intent, None, [], [], None

    def rebind_intent_lease(self, *, intent, **kwargs):
        self.log.append(("rebind-intent",))
        return intent

    def rebind_for_reconciliation(self, *, intent, **kwargs):
        self.log.append(("rebind-reconcile",))
        return intent

    def claim_create_once(self, *, intent, **kwargs):
        self.log.append(("claim",))
        self.intent = intent.model_copy(update={"status": BranchIntentStatus.POST_CLAIMED})
        return self.intent

    def record_remote_observation(self, *, intent, verified_sha, branch_url, reason, **kwargs):
        self.log.append(("record", verified_sha))
        status = (
            BranchIntentStatus.REMOTE_VERIFIED
            if verified_sha == intent.source_sha
            else BranchIntentStatus.RECONCILIATION_REQUIRED
        )
        self.intent = intent.model_copy(
            update={"status": status, "branch_url": branch_url, "reason": reason}
        )
        return self.intent


class FakeRunRepository:
    def __init__(self, log):
        self.log = log
        self.completion = None

    def commit_step_completion(self, **kwargs):
        self.log.append(("commit", kwargs["branch_intent_id"]))
        self.completion = kwargs


def found(branch, sha=SHA, url=REF_URL):
    return GitHubBranchObservation("acme/example", branch, True, sha, url)


def absent(branch):
    return GitHubBranchObservation("acme/example", branch, False)


def setup(*, source=None, target=None, existing=None, allow=True, post_error=None, revoked_lease=False):
    log = []
    provider = FakeProvider(source=source, target=target, post_error=post_error)
    provider.log = log
    intent_repo = FakeIntentRepository(log, existing=existing, allow=allow)
    run_repo = FakeRunRepository(log)
    run = AgentRun(
        id="run-1", task_id="task-1", workspace_id="ws-1", approval_request_id="approval-1",
        capability_lease_id="lease-1", project_id="project-1", repository="acme/example",
        status="RUNNING", current_step_id="step-1", lease_owner="worker-1",
        lease_token="worker-token", lease_expires_at=NOW.replace(year=2027), updated_at=NOW,
    )
    step = AgentRunStep(
        id="step-1", run_id="run-1", task_id="task-1", sequence=1,
        step_type=AgentRunStepType.CREATE_BRANCH, status=AgentRunStepStatus.RUNNING,
        idempotency_key="idem-1", lease_owner="worker-1", lease_token="worker-token",
        lease_expires_at=NOW.replace(year=2027), updated_at=NOW,
    )
    task = SimpleNamespace(
        id="task-1", workspace_id="ws-1", provider=AgentProvider.OTHER,
        external_agent_id=None, project_id="project-1", approval_request_id="approval-1",
    )
    approval = SimpleNamespace(id="approval-1", workspace_id="ws-1")
    lease = SimpleNamespace(
        id="lease-1",
        status="REVOKED" if revoked_lease else "ACTIVE",
        expires_at=NOW.replace(year=2025) if revoked_lease else NOW.replace(year=2027),
    )

    def action_factory(source_sha):
        return ProposedAction(
            id="action-1", action_type=CapabilityActionType.CREATE_BRANCH,
            workspace_id="ws-1", run_id="run-1", task_id="task-1", lease_id="lease-1",
            project_id="project-1",
            repository="acme/example", branch="feature/test",
            metadata={"source_sha": source_sha, "source_branch": "main",
                      "target_ref": "feature/test", "step_id": "step-1",
                      "idempotency_key": "idem-1", "task_id": "task-1",
                      "approval_id": "approval-1", "lease_id": "lease-1",
                      "repository": "acme/example"},
        )

    def intent_factory(source_sha, action):
        return AgentRunBranchIntent(
            id="intent-1", workspace_id="ws-1", run_id="run-1", task_id="task-1",
            approval_id="approval-1", step_id="step-1", action_id=action.id, lease_id="lease-1",
            idempotency_key="idem-1", repository="acme/example", source_branch="main",
            target_ref="feature/test", source_sha=source_sha,
            lease_token_hash=sha256_hex({"lease_token": "worker-token"}),
            initial_lease_token_hash=sha256_hex({"lease_token": "worker-token"}),
            run_revision=NOW, step_revision=NOW, created_at=NOW, updated_at=NOW,
        )

    def completion_factory(intent, branch_url):
        branch_facts = {
            "branch_intent_id": intent.id,
            "repository": intent.repository,
            "source_branch": intent.source_branch,
            "target_ref": intent.target_ref,
            "source_sha": intent.source_sha,
            "branch_url": branch_url,
        }
        event = SimpleNamespace(
            step_id="step-1", run_id="run-1", task_id="task-1", actor="worker-1",
            event_type=AgentRunEventType.STEP_SUCCEEDED, metadata=dict(branch_facts),
        )
        task_event = SimpleNamespace(
            task_id="task-1", actor="worker-1", event_type=AgentTaskEventType.NOTE,
            status=None, metadata=dict(branch_facts),
        )
        task_audit = AuditEvent(
            event_type="agent_task.branch_created", entity_id="task-1", workspace_id="ws-1",
            actor="worker-1", details=dict(branch_facts),
        )
        updated_step = step.model_copy(update={"status": AgentRunStepStatus.SUCCEEDED})
        return BranchCompletion(
            expected_run=run, updated_run=run, expected_steps=[step], updated_steps=[updated_step],
            events=[event], audit=AuditEvent(
                event_type="step", entity_id="run-1", workspace_id="ws-1", actor="worker-1",
                details=dict(branch_facts),
            ),
            branch_task_event=task_event, branch_task_audit=task_audit, now=NOW,
        )

    request = BranchExecutionRequest(
        workspace_id="ws-1", actor="worker-1", worker_lease_token="worker-token",
        expected_run=run, expected_step=step, task=task, approval=approval, lease=lease,
        repository="acme/example", source_branch="main", target_ref="feature/test",
        timeout_seconds=60, action_factory=action_factory, intent_factory=intent_factory,
        completion_factory=completion_factory,
    )
    coordinator = PostgresCreateBranchCoordinator(
        intent_repository=intent_repo, run_repository=run_repo,
        provider=provider, clock=lambda: NOW,
    )
    return coordinator, request, provider, intent_repo, run_repo, log


def test_create_sequence_pins_source_then_claims_then_posts_then_gets_and_commits():
    coordinator, request, provider, _, run_repo, log = setup(
        source=[found("main"), found("main")], target=[absent("feature/test"), found("feature/test")]
    )

    result = coordinator.execute(request)

    assert result.state == BranchExecutionState.COMPLETED
    assert provider.post_count == 1
    assert [entry[0] for entry in log] == [
        "load-intent", "get", "evaluate", "begin", "rebind-intent", "get", "get", "claim",
        "post", "get", "record", "commit",
    ]
    assert run_repo.completion["branch_intent_id"] == "intent-1"


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ([RuntimeError("source lookup failed")], [absent("feature/test")]),
        ([found("main"), found("main", "b" * 40)], [absent("feature/test")]),
        ([found("main"), found("main")], [RuntimeError("target lookup failed")]),
        ([found("main"), found("main")], [found("feature/test")]),
    ],
)
def test_source_or_target_ambiguity_never_claims_or_posts(source, target):
    coordinator, request, provider, _, run_repo, log = setup(source=source, target=target)

    result = coordinator.execute(request)

    assert result.state == BranchExecutionState.BLOCKED
    assert provider.post_count == 0
    assert not any(entry[0] == "claim" for entry in log)
    assert run_repo.completion is None


def test_blocked_policy_never_begins_intent_or_posts():
    coordinator, request, provider, _, _, log = setup(
        source=[found("main")], target=[absent("feature/test")], allow=False
    )

    result = coordinator.execute(request)

    assert result.state == BranchExecutionState.BLOCKED
    assert provider.post_count == 0
    assert "begin" not in [entry[0] for entry in log]


def test_existing_unclaimed_intent_still_requires_current_allow_decision():
    coordinator, request, provider, intent_repo, _, _ = setup(
        source=[found("main")], target=[absent("feature/test")], allow=False,
    )
    action = request.action_factory(SHA)
    intent_repo.intent = request.intent_factory(SHA, action)

    result = coordinator.execute(request)

    assert result.state == BranchExecutionState.BLOCKED
    assert provider.post_count == 0
    assert "evaluate" in [entry[0] for entry in intent_repo.log]
    assert "begin" not in [entry[0] for entry in intent_repo.log]


def test_post_exception_gets_target_once_and_never_reposts():
    coordinator, request, provider, _, run_repo, log = setup(
        source=[found("main"), found("main")],
        target=[absent("feature/test"), found("feature/test")],
        post_error=TimeoutError("ack lost"),
    )

    result = coordinator.execute(request)

    assert result.state == BranchExecutionState.COMPLETED
    assert provider.post_count == 1
    assert [entry[0] for entry in log].count("get") == 4
    assert run_repo.completion is not None


@pytest.mark.parametrize(
    "claimed_status",
    [
        BranchIntentStatus.POST_CLAIMED,
        BranchIntentStatus.REMOTE_VERIFIED,
        BranchIntentStatus.RECONCILIATION_REQUIRED,
    ],
)
def test_restart_after_claim_with_revoked_lease_rebinds_and_uses_get_only(claimed_status):
    existing = AgentRunBranchIntent(
        id="intent-1", workspace_id="ws-1", run_id="run-1", task_id="task-1",
        approval_id="approval-1", step_id="step-1", action_id="action-1", lease_id="lease-1",
        idempotency_key="idem-1", repository="acme/example", source_branch="main",
        target_ref="feature/test", source_sha=SHA,
        lease_token_hash=sha256_hex({"lease_token": "worker-token"}),
        initial_lease_token_hash=sha256_hex({"lease_token": "worker-token"}),
        run_revision=NOW, step_revision=NOW, status=claimed_status,
        created_at=NOW, updated_at=NOW,
    )
    coordinator, request, provider, _, run_repo, log = setup(
        source=[], target=[found("feature/test")], existing=existing,
        allow=False, revoked_lease=True,
    )

    result = coordinator.execute(request)

    assert result.state == BranchExecutionState.COMPLETED
    assert provider.post_count == 0
    assert "rebind-reconcile" in [entry[0] for entry in log]
    assert "claim" not in [entry[0] for entry in log]
    assert "evaluate" not in [entry[0] for entry in log]
    assert "begin" not in [entry[0] for entry in log]
    assert [entry for entry in log if entry[0] == "get"] == [("get", "feature/test")]
    assert run_repo.completion is not None


def test_mismatched_target_sha_never_completes():
    coordinator, request, provider, _, run_repo, _ = setup(
        source=[found("main"), found("main")],
        target=[absent("feature/test"), found("feature/test", "b" * 40)],
    )

    result = coordinator.execute(request)

    assert result.state == BranchExecutionState.RECONCILIATION_REQUIRED
    assert result.verified_sha == "b" * 40
    assert run_repo.completion is None


def test_post_claim_target_not_found_never_completes_or_reposts():
    coordinator, request, provider, _, run_repo, _ = setup(
        source=[found("main"), found("main")],
        target=[absent("feature/test"), absent("feature/test")],
    )

    result = coordinator.execute(request)

    assert result.state == BranchExecutionState.RECONCILIATION_REQUIRED
    assert provider.post_count == 1
    assert run_repo.completion is None


def test_completion_requires_exact_task_audit_binding():
    coordinator, request, _, _, run_repo, _ = setup(
        source=[found("main"), found("main")], target=[absent("feature/test"), found("feature/test")]
    )
    base_factory = request.completion_factory

    def invalid_completion(intent, url):
        completion = base_factory(intent, url)
        completion.branch_task_event.metadata["branch_intent_id"] = "other-intent"
        return completion

    request = BranchExecutionRequest(**{**request.__dict__, "completion_factory": invalid_completion})

    with pytest.raises(ValueError, match="completion records"):
        coordinator.execute(request)
    assert run_repo.completion is None
