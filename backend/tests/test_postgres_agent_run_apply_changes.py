from __future__ import annotations

import base64
from datetime import datetime, timezone
from hashlib import sha256
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.postgres_agent_run_apply_changes import (
    AgentRunApplyChangeIntent,
    ApplyChangeIntentStatus,
    _consumption_matches_intent,
    _lease_allows_path,
    _lease_matches_task,
)
from app.postgres_agent_run_apply_changes_executor import (
    ApplyChangeRequest,
    ApplyChangeState,
    FileObservation,
    PostgresApplyChangesCoordinator,
    postgres_apply_changes_enabled,
)
from app.postgres_agent_run_apply_changes_route import (
    GitHubApplyChangeProvider,
    postgres_apply_changes_execution_dependencies,
)
from app.trust_models import ActionConsumption


_BASE_SHA = "a" * 40
_COMMIT_SHA = "b" * 40
_CONTENT = "approved file contents\n"
_NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)


class _FakeIntentRepository:
    def __init__(self) -> None:
        self.intent: AgentRunApplyChangeIntent | None = None
        self.authorized = 0
        self.claims = 0
        self.observations: list[ApplyChangeIntentStatus] = []
        self.rebinds = 0

    def get_for_step(self, **_kwargs):
        return self.intent

    def record_authorized_intent(self, *, record, **_kwargs):
        self.authorized += 1
        self.intent = record
        return record

    def claim_write_once(self, *, intent, **_kwargs):
        assert self.intent == intent
        assert intent.status == ApplyChangeIntentStatus.INTENT
        self.claims += 1
        self.intent = intent.model_copy(update={"status": ApplyChangeIntentStatus.POST_CLAIMED})
        return self.intent

    def record_observation(self, *, intent, expected_status, status, commit_sha, reason, now):
        assert self.intent == intent
        assert intent.status == expected_status
        self.observations.append(status)
        self.intent = intent.model_copy(update={
            "status": status, "commit_sha": commit_sha, "reason": reason, "updated_at": now,
        })
        return self.intent

    def rebind_verified_completion(self, *, intent, **_kwargs):
        assert self.intent == intent
        assert intent.status == ApplyChangeIntentStatus.REMOTE_VERIFIED
        self.rebinds += 1
        return intent


class _FakeProvider:
    """Models an adapter with an atomic pinned-base check at its write boundary."""

    def __init__(self, *, ambiguous_after_commit: bool = False, omit_marker: bool = False) -> None:
        self.head_sha = _BASE_SHA
        self.ambiguous_after_commit = ambiguous_after_commit
        self.omit_marker = omit_marker
        self.write_calls = 0
        self.ref_reads = 0
        self.file_reads = 0
        self.observation = FileObservation(exists=False)

    def observe_ref_sha(self, _repository: str, _branch: str) -> str:
        self.ref_reads += 1
        return self.head_sha

    def observe_file(self, _repository: str, _path: str, _branch: str) -> FileObservation:
        self.file_reads += 1
        return self.observation

    def write_file(
        self, _repository: str, _path: str, _branch: str,
        content: str, message: str, _before_sha: str | None, expected_base_sha: str,
    ) -> str:
        # The test double rejects a changed base atomically. The production
        # GitHub Contents provider explicitly does not claim this capability.
        if self.head_sha != expected_base_sha:
            raise RuntimeError("atomic base check rejected advanced branch")
        self.write_calls += 1
        marker = "" if self.omit_marker else message.split("aixion-apply-intent:", 1)[1].split("]", 1)[0]
        self.observation = FileObservation(
            exists=True,
            blob_sha="c" * 40,
            content_sha256=sha256(content.encode()).hexdigest(),
            commit_sha=_COMMIT_SHA,
            commit_message=f"test [aixion-apply-intent:{marker}]" if marker else "test",
        )
        if self.ambiguous_after_commit:
            raise TimeoutError("response lost after remote commit")
        return _COMMIT_SHA


class _FakeRunRepository:
    def __init__(self) -> None:
        self.completions = 0
        self.fail_completion_once = False

    def commit_step_completion(self, **_kwargs) -> None:
        self.completions += 1
        if self.fail_completion_once:
            self.fail_completion_once = False
            raise RuntimeError("simulated atomic DB completion rollback")


def _request(repository: _FakeIntentRepository, *, provider: _FakeProvider, runs: _FakeRunRepository):
    run = SimpleNamespace(
        id="run-1", task_id="task-1", workspace_id="workspace-1",
        lease_token="worker-token", capability_lease_id="capability-1",
        approval_request_id="approval-1", updated_at=_NOW,
    )
    step = SimpleNamespace(
        id="step-1", run_id=run.id, task_id=run.task_id,
        step_type=SimpleNamespace(value="APPLY_CHANGES"),
        idempotency_key="run-1:apply:1", lease_token="worker-token",
        updated_at=_NOW, attempt_count=1,
    )
    approval = SimpleNamespace(
        id="approval-1",
        files=[SimpleNamespace(path="src/approved.txt", change_type="create", new_content=_CONTENT)],
    )
    lease = SimpleNamespace(id="capability-1")

    def action_factory(before_sha):
        return SimpleNamespace(id="action-1", before_sha=before_sha)

    def intent_factory(before_sha, _action):
        return AgentRunApplyChangeIntent(
            id="intent-1", workspace_id=run.workspace_id, run_id=run.id,
            task_id=run.task_id, approval_id=approval.id, step_id=step.id,
            action_id="action-1", lease_id=lease.id,
            idempotency_key=step.idempotency_key,
            repository="owner/repo", branch="feature/approved",
            path="src/approved.txt", change_type="create", before_sha=before_sha,
            desired_content_sha256=sha256(_CONTENT.encode()).hexdigest(),
            lease_token_hash="d" * 64, run_revision=_NOW, step_revision=_NOW,
            created_at=_NOW, updated_at=_NOW,
        )

    def completion_factory(intent, commit_sha):
        return SimpleNamespace(
            expected_run=run, updated_run=run, expected_steps=[step],
            updated_steps=[step], events=[], audit=[], now=_NOW,
            intent=intent, commit_sha=commit_sha,
        )

    return ApplyChangeRequest(
        workspace_id=run.workspace_id, actor="owner@example.test",
        worker_lease_token="worker-token", expected_run=run, expected_step=step,
        approval=approval, lease=lease, repository="owner/repo",
        branch="feature/approved", base_commit_sha=_BASE_SHA,
        path="src/approved.txt", change_type="create", content=_CONTENT,
        before_sha=None, timeout_seconds=15, action_factory=action_factory,
        intent_factory=intent_factory, completion_factory=completion_factory,
    )


def _coordinator(repository, provider, runs):
    return PostgresApplyChangesCoordinator(
        intent_repository=repository, run_repository=runs, provider=provider,
        clock=lambda: _NOW,
    )


def test_apply_change_success_uses_one_write_and_atomic_completion() -> None:
    intents, provider, runs = _FakeIntentRepository(), _FakeProvider(), _FakeRunRepository()
    result = _coordinator(intents, provider, runs).execute(
        _request(intents, provider=provider, runs=runs)
    )
    assert result.state == ApplyChangeState.COMPLETED
    assert result.commit_sha == _COMMIT_SHA
    assert provider.write_calls == 1
    assert intents.authorized == intents.claims == intents.rebinds == 1
    assert runs.completions == 1
    assert intents.intent is not None
    assert intents.intent.status == ApplyChangeIntentStatus.REMOTE_VERIFIED


def test_ambiguous_committed_write_reconciles_by_intent_attribution_without_retry() -> None:
    intents = _FakeIntentRepository()
    provider = _FakeProvider(ambiguous_after_commit=True)
    runs = _FakeRunRepository()
    coordinator = _coordinator(intents, provider, runs)
    request = _request(intents, provider=provider, runs=runs)

    first = coordinator.execute(request)
    assert first.state == ApplyChangeState.RECONCILIATION_REQUIRED
    assert provider.write_calls == 1
    assert runs.completions == 0

    provider.ambiguous_after_commit = False
    recovered = coordinator.execute(request)
    assert recovered.state == ApplyChangeState.COMPLETED
    assert provider.write_calls == 1
    assert runs.completions == 1


def test_matching_bytes_without_intent_commit_attribution_never_completes_or_retries() -> None:
    intents = _FakeIntentRepository()
    provider = _FakeProvider(ambiguous_after_commit=True, omit_marker=True)
    runs = _FakeRunRepository()
    coordinator = _coordinator(intents, provider, runs)
    request = _request(intents, provider=provider, runs=runs)

    assert coordinator.execute(request).state == ApplyChangeState.RECONCILIATION_REQUIRED
    provider.ambiguous_after_commit = False
    second = coordinator.execute(request)
    assert second.state == ApplyChangeState.RECONCILIATION_REQUIRED
    assert provider.write_calls == 1
    assert runs.completions == 0


def test_verified_remote_change_recovers_after_atomic_completion_rollback_without_write_retry() -> None:
    intents = _FakeIntentRepository()
    provider = _FakeProvider()
    runs = _FakeRunRepository()
    runs.fail_completion_once = True
    coordinator = _coordinator(intents, provider, runs)
    request = _request(intents, provider=provider, runs=runs)

    first = coordinator.execute(request)
    assert first.state == ApplyChangeState.RECONCILIATION_REQUIRED
    assert first.intent is not None
    assert first.intent.status == ApplyChangeIntentStatus.REMOTE_VERIFIED
    assert provider.write_calls == 1

    recovered = coordinator.execute(request)
    assert recovered.state == ApplyChangeState.COMPLETED
    assert provider.write_calls == 1
    assert intents.rebinds == 2
    assert runs.completions == 2


def test_advanced_target_branch_fails_atomic_base_check_before_file_mutation() -> None:
    intents, provider, runs = _FakeIntentRepository(), _FakeProvider(), _FakeRunRepository()
    coordinator = _coordinator(intents, provider, runs)
    request = _request(intents, provider=provider, runs=runs)
    # Simulate an unrelated branch update after coordinator preflight but before
    # the provider's atomic mutation boundary.
    original_ref_read = provider.observe_ref_sha

    def observe_then_advance(repository: str, branch: str) -> str:
        observed = original_ref_read(repository, branch)
        provider.head_sha = "e" * 40
        return observed

    provider.observe_ref_sha = observe_then_advance
    result = coordinator.execute(request)
    assert result.state == ApplyChangeState.RECONCILIATION_REQUIRED
    assert provider.write_calls == 0
    assert runs.completions == 0


def test_production_route_is_closed_by_default_rollout_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    assert GitHubApplyChangeProvider.supports_atomic_base_commit is True
    monkeypatch.delenv("AIXION_POSTGRES_APPLY_CHANGES_ENABLED", raising=False)
    with pytest.raises(HTTPException) as rejected:
        postgres_apply_changes_execution_dependencies(
            request=SimpleNamespace(), run=SimpleNamespace(),
            step=SimpleNamespace(), actor="owner@example.test",
        )
    assert rejected.value.status_code == 503
    assert "default-off" in rejected.value.detail


def test_apply_change_rollout_gate_requires_explicit_true_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIXION_POSTGRES_APPLY_CHANGES_ENABLED", "true")
    assert postgres_apply_changes_enabled()
    monkeypatch.setenv("AIXION_POSTGRES_APPLY_CHANGES_ENABLED", "enabled")
    assert not postgres_apply_changes_enabled()


def test_github_graphql_write_is_one_pinned_base_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "data": {
                    "createCommitOnBranch": {
                        "commit": {
                            "oid": _COMMIT_SHA,
                            "message": "approved [aixion-apply-intent:intent-1]",
                        },
                        "ref": {"target": {"oid": _COMMIT_SHA}},
                    },
                },
            }

    def fake_post(url: str, **kwargs):
        calls.append({"url": url, **kwargs})
        return Response()

    monkeypatch.setattr("app.postgres_agent_run_apply_changes_route.httpx.post", fake_post)
    provider = GitHubApplyChangeProvider("test-token")
    result = provider.write_file(
        "owner/repo", "src/approved.txt", "feature/approved", _CONTENT,
        "Apply approved change [aixion-apply-intent:intent-1]", None, _BASE_SHA,
    )
    assert result == _COMMIT_SHA
    assert len(calls) == 1
    assert calls[0]["url"] == "https://api.github.com/graphql"
    mutation_input = calls[0]["json"]["variables"]["input"]
    assert mutation_input["expectedHeadOid"] == _BASE_SHA
    assert mutation_input["clientMutationId"] == "intent-1"
    assert mutation_input["branch"] == {
        "repositoryNameWithOwner": "owner/repo", "branchName": "feature/approved",
    }
    assert mutation_input["fileChanges"]["additions"] == [{
        "path": "src/approved.txt",
        "contents": base64.b64encode(_CONTENT.encode()).decode("ascii"),
    }]
    assert mutation_input["fileChanges"].get("deletions") is None
    assert "aixion-apply-intent:intent-1" in mutation_input["message"]["body"]


@pytest.mark.parametrize(
    "path",
    ["../escape", "/absolute", "a//b", "a\\b", "a/./b", " src/file", "src/file "],
)
def test_apply_change_rejects_noncanonical_paths(path: str) -> None:
    intents, provider, runs = _FakeIntentRepository(), _FakeProvider(), _FakeRunRepository()
    request = _request(intents, provider=provider, runs=runs)
    request = request.__class__(**{**request.__dict__, "path": path})
    with pytest.raises(ValueError, match="one-file approved create/update contract"):
        _coordinator(intents, provider, runs).execute(request)
    assert provider.write_calls == 0


def test_apply_change_does_not_trim_a_different_approved_path() -> None:
    intents, provider, runs = _FakeIntentRepository(), _FakeProvider(), _FakeRunRepository()
    request = _request(intents, provider=provider, runs=runs)
    request.approval.files[0].path = " src/approved.txt "
    with pytest.raises(ValueError, match="one-file approved create/update contract"):
        _coordinator(intents, provider, runs).execute(request)
    assert provider.write_calls == 0


def test_postgres_apply_claim_scope_rejects_path_outside_capability_prefix() -> None:
    lease = SimpleNamespace(scope=SimpleNamespace(allowed_path_prefixes=["src/", "docs/"]))
    assert _lease_allows_path(lease, "src/file.py")
    assert _lease_allows_path(lease, "docs/guide.md")
    assert not _lease_allows_path(lease, "secrets/file.txt")
    assert not _lease_allows_path(lease, "src-private/file.py")


def test_postgres_apply_rebind_rejects_stale_or_mismatched_consumption() -> None:
    intent = AgentRunApplyChangeIntent(
        id="intent-1", workspace_id="workspace-1", run_id="run-1", task_id="task-1",
        approval_id="approval-1", step_id="step-1", action_id="action-1", lease_id="lease-1",
        idempotency_key="idem-1", repository="owner/repo", branch="feature/approved",
        path="src/file.py", change_type="create", desired_content_sha256="a" * 64,
        lease_token_hash="b" * 64, run_revision=_NOW, step_revision=_NOW,
        created_at=_NOW, updated_at=_NOW,
    )
    evidence = {
        "workspace_id": intent.workspace_id, "run_id": intent.run_id,
        "task_id": intent.task_id, "approval_id": intent.approval_id,
        "step_id": intent.step_id, "action_id": intent.action_id, "lease_id": intent.lease_id,
        "intent_id": intent.id, "path": intent.path,
        "content_sha256": intent.desired_content_sha256, "runtime_seconds_reserved": 60,
        "authorized_attempt_count": 1, "authorized_retry_consumed": False,
    }
    valid = ActionConsumption(actual_runtime_seconds=60, retry_consumed=False, evidence=evidence)
    assert _consumption_matches_intent(valid, intent)
    assert not _consumption_matches_intent(None, intent)
    assert not _consumption_matches_intent(
        valid.model_copy(update={"evidence": {**evidence, "intent_id": "stale-intent"}}),
        intent,
    )
    assert not _consumption_matches_intent(
        valid.model_copy(update={"evidence": {**evidence, "content_sha256": "c" * 64}}),
        intent,
    )
    assert not _consumption_matches_intent(
        valid.model_copy(update={"actual_runtime_seconds": 61}), intent,
    )
    assert not _consumption_matches_intent(
        valid.model_copy(update={"retry_consumed": True}), intent,
    )
    assert not _consumption_matches_intent(
        valid.model_copy(update={"evidence": {
            **evidence, "authorized_attempt_count": 0,
        }}), intent,
    )
    assert not _consumption_matches_intent(
        valid.model_copy(update={"evidence": {
            **evidence, "authorized_attempt_count": True,
        }}), intent,
    )


@pytest.mark.parametrize(
    ("provider", "agent_id"),
    [("wrong-provider", "agent-1"), ("codex", "wrong-agent")],
)
def test_postgres_apply_rebind_rejects_lease_provider_or_agent_mismatch(
    provider: str, agent_id: str,
) -> None:
    task = SimpleNamespace(provider="codex", external_agent_id="agent-1")
    lease = SimpleNamespace(provider=provider, agent_id=agent_id)
    assert not _lease_matches_task(lease, task)
    assert _lease_matches_task(SimpleNamespace(provider="codex", agent_id="agent-1"), task)
