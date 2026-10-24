from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.agent_run_models import AgentRunStepType
from app.agent_run_supervisor import AgentRunConflict, PostgresPullRequestRequest
from app.agent_task_models import AgentTaskStatus
from app.postgres_agent_run_pull_request import (
    AgentRunPullRequestIntent,
    PullRequestIntentStatus,
)
from app.postgres_agent_run_pull_request_route import (
    PostgresCreatePullRequestCoordinator,
    PullRequestRemote,
    postgres_pull_request_enabled,
    postgres_pull_request_execution_dependencies,
)
from app.models import AgentProvider, ApprovalStatus
from app.trust_models import CapabilityActionType


HEAD_SHA = "a" * 40
BASE_SHA = "b" * 40


class FakeIntentRepository:
    def __init__(self) -> None:
        self.intent: AgentRunPullRequestIntent | None = None
        self.get_observations: list[dict] = []

    def get_for_step(self, **_kwargs):
        return self.intent

    def record_authorized_intent(self, *, record, **_kwargs):
        self.intent = record
        return record

    def claim_post_once(self, *, intent, **_kwargs):
        self.intent = intent.model_copy(update={"status": PullRequestIntentStatus.POST_CLAIMED})
        return self.intent

    def record_post_outcome(self, *, intent_id, outcome, **kwargs):
        assert self.intent and self.intent.id == intent_id
        if outcome == "VERIFIED":
            assert kwargs["marker_matches"] is True
            self.intent = self.intent.model_copy(update={
                "status": PullRequestIntentStatus.REMOTE_VERIFIED,
                "pull_number": kwargs["pull_number"],
                "pull_url": kwargs["pull_url"],
                "observed_repository": kwargs["observed_repository"],
                "observed_head_branch": kwargs["observed_head_branch"],
                "observed_base_branch": kwargs["observed_base_branch"],
                "marker_matches": True,
                "observed_head_sha": kwargs["head_sha"],
                "observed_base_sha": kwargs["base_sha"],
                "response_sha256": kwargs["response_sha256"],
            })
        else:
            self.intent = self.intent.model_copy(update={
                "status": PullRequestIntentStatus.RECONCILIATION_REQUIRED,
            })
        return self.intent

    def record_get_reconciliation(self, *, intent_id, matching_count, **kwargs):
        assert self.intent and self.intent.id == intent_id
        self.get_observations.append({"matching_count": matching_count, **kwargs})
        if matching_count == 1:
            self.intent = self.intent.model_copy(update={
                "status": PullRequestIntentStatus.REMOTE_VERIFIED,
                "pull_number": kwargs["pull_number"],
                "pull_url": kwargs["pull_url"],
                "observed_repository": kwargs["observed_repository"],
                "observed_head_branch": kwargs["observed_head_branch"],
                "observed_base_branch": kwargs["observed_base_branch"],
                "marker_matches": True,
                "observed_head_sha": kwargs["head_sha"],
                "observed_base_sha": kwargs["base_sha"],
                "response_sha256": kwargs["response_sha256"],
            })
        else:
            self.intent = self.intent.model_copy(update={
                "status": PullRequestIntentStatus.RECONCILIATION_REQUIRED,
            })
        return self.intent


class FakeProvider:
    def __init__(self, *, fail_post: bool = False, response_base_sha: str = BASE_SHA) -> None:
        self.fail_post = fail_post
        self.response_base_sha = response_base_sha
        self.post_calls = 0
        self.get_calls = 0
        self.remote: PullRequestRemote | None = None
        self.discovered: list[PullRequestRemote] = []

    def observe_branch(self, _repository, branch, *, timeout_seconds):
        assert timeout_seconds == 40
        return HEAD_SHA if branch.startswith("feature/") else BASE_SHA

    def create_pull_request(self, repository, title, body, head_branch, base_branch, *, timeout_seconds):
        self.post_calls += 1
        if self.fail_post:
            raise TimeoutError("simulated lost provider response")
        marker = next(line[5:-4] for line in body.splitlines() if line.startswith("<!-- "))
        self.remote = PullRequestRemote(
            repository=repository, number=17, title=title, body=body,
            head_branch=head_branch, base_branch=base_branch,
            html_url=f"https://github.com/{repository}/pull/17",
            head_sha=HEAD_SHA, base_sha=self.response_base_sha,
        )
        assert marker in self.remote.body
        return self.remote

    def find_by_intent(self, _repository, _head_branch, _base_branch, _marker, *, timeout_seconds):
        self.get_calls += 1
        return list(self.discovered)


def make_request(provider: FakeProvider, repository: FakeIntentRepository) -> PostgresPullRequestRequest:
    stamp = datetime.now(timezone.utc)
    run = SimpleNamespace(
        id="run_test", workspace_id="workspace_test", task_id="task_test",
        project_id="project_test", approval_request_id="approval_test",
        capability_lease_id="lease_test", status=SimpleNamespace(value="RUNNING"),
        lease_owner="worker_test", lease_token="lease-token", current_step_id="step_test",
        updated_at=stamp, pause_requested=False, cancel_requested=False,
    )
    step = SimpleNamespace(
        id="step_test", run_id=run.id, task_id=run.task_id,
        step_type=AgentRunStepType.CREATE_PULL_REQUEST,
        status=SimpleNamespace(value="RUNNING"), idempotency_key="step-idem",
        lease_token="lease-token", updated_at=stamp, attempt_count=1,
    )
    task = SimpleNamespace(
        id=run.task_id, workspace_id=run.workspace_id, project_id=run.project_id,
        approval_request_id=run.approval_request_id, repository="acme/project",
        branch_preference="feature/verified", status=AgentTaskStatus.APPROVED,
        title="Improve run completion", provider=AgentProvider.ANTIGRAVITY,
        external_agent_id=None,
    )
    approval = SimpleNamespace(
        id=run.approval_request_id, workspace_id=run.workspace_id,
        project_id=run.project_id, status=ApprovalStatus.APPROVED,
        title="Reviewed change",
        approved_payload_hash="sha256:" + "c" * 64,
    )
    lease = SimpleNamespace(
        id=run.capability_lease_id, workspace_id=run.workspace_id,
        approval_request_id=approval.id,
        scope=SimpleNamespace(
            repository=task.repository, branch=task.branch_preference,
            allowed_actions=[CapabilityActionType.CREATE_PULL_REQUEST],
        ),
    )
    return PostgresPullRequestRequest(
        expected_run=run, expected_step=step, task=task, approval=approval,
        lease=lease, worker_id="worker_test", worker_lease_token="lease-token",
        actor="owner@example.test", timeout_seconds=40,
        repository=task.repository, head_branch=task.branch_preference,
        base_branch="main", expected_head_sha=HEAD_SHA, provider=provider,
        intent_repository=repository,
        completion_factory=lambda intent, title, body: (intent, title, body),
    )


def test_postgres_pull_request_is_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIXION_POSTGRES_CREATE_PULL_REQUEST_ENABLED", raising=False)
    assert postgres_pull_request_enabled() is False
    monkeypatch.setenv("AIXION_POSTGRES_CREATE_PULL_REQUEST_ENABLED", "true")
    assert postgres_pull_request_enabled() is True


def test_dependency_resolver_rejects_disabled_or_missing_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.postgres_agent_run_pull_request_route as route

    monkeypatch.delenv("AIXION_POSTGRES_CREATE_PULL_REQUEST_ENABLED", raising=False)
    with pytest.raises(HTTPException) as disabled:
        postgres_pull_request_execution_dependencies(
            request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
            run=SimpleNamespace(), step=SimpleNamespace(), actor="reviewer",
        )
    assert disabled.value.status_code == 503

    monkeypatch.setenv("AIXION_POSTGRES_CREATE_PULL_REQUEST_ENABLED", "true")
    monkeypatch.setattr(route, "application_repositories_for_request", lambda _request: object())
    with pytest.raises(HTTPException) as incomplete:
        postgres_pull_request_execution_dependencies(
            request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
            run=SimpleNamespace(), step=SimpleNamespace(), actor="reviewer",
        )
    assert incomplete.value.status_code == 503


def test_verified_post_uses_single_post_and_hash_only_intent() -> None:
    provider = FakeProvider()
    intents = FakeIntentRepository()
    outcome = PostgresCreatePullRequestCoordinator().execute(make_request(provider, intents))
    intent, title, body = outcome
    assert provider.post_calls == 1
    assert provider.get_calls == 0
    assert intent.status == PullRequestIntentStatus.REMOTE_VERIFIED
    assert intent.expected_head_sha == HEAD_SHA
    assert intent.expected_base_sha == BASE_SHA
    assert intent.observed_head_sha == intent.expected_head_sha
    assert intent.observed_base_sha == intent.expected_base_sha
    assert "Reviewed change" not in str(intent.model_dump())
    assert title == "Reviewed change"
    assert "aixion-intent:" in body


def test_ambiguous_post_recovers_by_get_without_second_post() -> None:
    provider = FakeProvider(fail_post=True)
    intents = FakeIntentRepository()
    coordinator = PostgresCreatePullRequestCoordinator()
    request = make_request(provider, intents)
    with pytest.raises(AgentRunConflict):
        coordinator.execute(request)
    assert provider.post_calls == 1
    assert provider.get_calls == 1
    assert intents.intent and intents.intent.status == PullRequestIntentStatus.RECONCILIATION_REQUIRED

    # A later worker may only reconcile by GET. Simulate the remote PR now being
    # visible after the original POST timed out; no second POST is permitted.
    recovery_request = make_request(provider, intents)
    marker = intents.intent.intent_marker
    body = "\n".join(("## Aixion worker pull request", "", "AgentTask: `task_test`",
                       "ApprovalRequest: `approval_test`", "Provider: `ANTIGRAVITY`", "",
                       "This pull request was opened after mobile approval. It is for human review; it is not merged or self-approved.",
                       "", f"<!-- {marker} -->"))
    provider.discovered = [PullRequestRemote(
        repository="acme/project", number=17, title="Reviewed change", body=body,
        head_branch="feature/verified", base_branch="main",
        html_url="https://github.com/acme/project/pull/17",
        head_sha=HEAD_SHA, base_sha=BASE_SHA,
    )]
    result = coordinator.execute(recovery_request)
    assert result[0].status == PullRequestIntentStatus.REMOTE_VERIFIED
    assert provider.post_calls == 1
    assert provider.get_calls == 2


def test_post_response_with_changed_base_commit_fails_closed() -> None:
    provider = FakeProvider(response_base_sha="c" * 40)
    intents = FakeIntentRepository()
    coordinator = PostgresCreatePullRequestCoordinator()
    with pytest.raises(AgentRunConflict, match="not exactly verified"):
        coordinator.execute(make_request(provider, intents))
    assert provider.post_calls == 1
    assert provider.get_calls == 1
    assert intents.intent and intents.intent.status == PullRequestIntentStatus.RECONCILIATION_REQUIRED
    assert intents.get_observations[0]["matching_count"] == 0
