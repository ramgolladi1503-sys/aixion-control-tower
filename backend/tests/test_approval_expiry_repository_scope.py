from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
import uuid

import pytest

from app import approval_expiry
from app.approval_expiry import (
    expire_approval,
    expire_due_approval,
    expire_due_approvals,
)
from app.approval_repository import ApprovalExpirationCommit
from app.models import (
    ApprovalRequest,
    ApprovalStatus,
    FileChange,
    RiskAssessment,
    RiskLevel,
    now_utc,
)
from app.store import store


class NonSQLiteApprovalRepository:
    def __init__(self, approval: ApprovalRequest) -> None:
        self.approval = approval

    def get(self, approval_id: str) -> ApprovalRequest | None:
        return self.approval if approval_id == self.approval.id else None

    def expire_pending(
        self,
        *,
        approval_id: str,
        expected_approval: ApprovalRequest,
        expired_approval: ApprovalRequest,
        audit_event: object,
    ) -> ApprovalExpirationCommit:
        assert approval_id == self.approval.id
        assert expected_approval == self.approval
        self.approval = expired_approval
        return ApprovalExpirationCommit(approval=expired_approval)


class RecordingExpiryRepository:
    def __init__(self) -> None:
        self.list_calls: list[dict[str, object]] = []

    def list_due_pending(self, **kwargs: object) -> list[ApprovalRequest]:
        self.list_calls.append(kwargs)
        return []

    def get(self, approval_id: str) -> ApprovalRequest | None:
        return None


@pytest.mark.parametrize("expiry_call", (expire_approval, expire_due_approval, expire_due_approvals))
@pytest.mark.parametrize(
    ("is_production", "postgres_auth_enabled"),
    ((True, False), (False, True)),
)
def test_expiry_entrypoints_require_explicit_repository_in_production_profiles(
    monkeypatch: pytest.MonkeyPatch,
    expiry_call,
    is_production: bool,
    postgres_auth_enabled: bool,
) -> None:
    def unexpected_sqlite_default():
        pytest.fail("Production expiry must reject before selecting SQLite")

    monkeypatch.setattr(
        approval_expiry,
        "get_settings",
        lambda: SimpleNamespace(
            is_production=is_production,
            postgres_auth_enabled=postgres_auth_enabled,
        ),
    )
    monkeypatch.setattr(
        approval_expiry, "approval_decision_repository", unexpected_sqlite_default
    )
    monkeypatch.setattr(store, "atomic", unexpected_sqlite_default)
    approval = ApprovalRequest(
        id="approval_missing_repository",
        project_id="project-expiry",
        workspace_id="workspace-expiry",
        title="Expiry repository required",
        summary="Production expiry must use the configured durable repository.",
        agent_name="test-agent",
        target_branch="main",
        files=[FileChange(path="README.md", diff="+change")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.REQUESTED,
    )

    with pytest.raises(RuntimeError, match="explicit durable repository"):
        if expiry_call is expire_approval:
            expiry_call(
                approval,
                actor="test",
                reason="test",
            )
        elif expiry_call is expire_due_approval:
            expiry_call(approval)
        else:
            expiry_call()


def test_local_expiry_entrypoints_keep_default_repository_compatibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = RecordingExpiryRepository()
    monkeypatch.setattr(approval_expiry, "approval_decision_repository", repository)
    monkeypatch.setattr(
        approval_expiry,
        "get_settings",
        lambda: SimpleNamespace(
            is_production=False,
            postgres_auth_enabled=False,
            agent_approval_ttl_seconds=3600,
        ),
    )

    approval = ApprovalRequest(
        id="approval_local_default_repository",
        project_id="project-expiry",
        workspace_id="workspace-expiry",
        title="Local compatibility",
        summary="Local development retains its configured default repository.",
        agent_name="test-agent",
        target_branch="main",
        files=[FileChange(path="README.md", diff="+change")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.REQUESTED,
    )

    assert expire_approval(
        approval, actor="test", reason="test", force=True
    ) is False
    assert expire_due_approval(approval) is False
    assert expire_due_approvals() == 0
    assert repository.list_calls == [
        {
            "now": repository.list_calls[0]["now"],
            "legacy_ttl_seconds": 3600,
            "limit": 250,
            "workspace_id": None,
        }
    ]


def test_due_approval_sweep_preserves_explicit_workspace_scope_and_global_maintenance_default() -> None:
    repository = RecordingExpiryRepository()

    assert expire_due_approvals(repository=repository, workspace_id="workspace-a") == 0  # type: ignore[arg-type]
    assert expire_due_approvals(repository=repository) == 0  # type: ignore[arg-type]

    assert [call["workspace_id"] for call in repository.list_calls] == ["workspace-a", None]
    assert all(call["limit"] == 250 for call in repository.list_calls)


def test_non_sqlite_expiry_does_not_lock_or_publish_to_sqlite_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approval = ApprovalRequest(
        id=f"approval_{uuid.uuid4().hex}",
        project_id="project-expiry-scope",
        workspace_id="workspace-expiry-scope",
        title="Expire through an external repository",
        summary="The committed external record must remain the only authority.",
        agent_name="test-agent",
        target_branch="feature/approval-expiry-scope",
        files=[FileChange(path="README.md", diff="+change")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.REQUESTED,
        expires_at=now_utc() - timedelta(seconds=1),
    )
    repository = NonSQLiteApprovalRepository(approval)
    store.approval_requests.pop(approval.id, None)

    def unexpected_sqlite_lock():
        pytest.fail("Non-SQLite expiry must not acquire the SQLite store lock")

    monkeypatch.setattr(store, "atomic", unexpected_sqlite_lock)

    expired = expire_approval(
        approval,
        actor="expiry-scope-test",
        reason="test_deadline",
        force=True,
        repository=repository,  # type: ignore[arg-type]
    )

    assert expired is True
    assert repository.approval.status == ApprovalStatus.EXPIRED
    assert approval.id not in store.approval_requests
    assert not any(
        event.entity_id == approval.id and event.event_type == "approval.expired"
        for event in store.audit_events
    )


def test_non_sqlite_expiry_uses_newer_durable_status_over_stale_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_pending = ApprovalRequest(
        id=f"approval_{uuid.uuid4().hex}",
        project_id="project-expiry-race",
        workspace_id="workspace-expiry-race",
        title="Stale pending snapshot",
        summary="The repository has already committed a terminal decision.",
        agent_name="test-agent",
        target_branch="feature/approval-expiry-race",
        files=[FileChange(path="README.md", diff="+change")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.REQUESTED,
        expires_at=now_utc() - timedelta(seconds=1),
    )
    durable_approved = stale_pending.model_copy(
        update={"status": ApprovalStatus.APPROVED}
    )
    repository = NonSQLiteApprovalRepository(durable_approved)
    store.approval_requests.pop(stale_pending.id, None)

    def unexpected_sqlite_lock():
        pytest.fail("Non-SQLite expiry must not acquire the SQLite store lock")

    monkeypatch.setattr(store, "atomic", unexpected_sqlite_lock)

    expired = expire_approval(
        stale_pending,
        actor="expiry-race-test",
        reason="test_deadline",
        force=True,
        repository=repository,  # type: ignore[arg-type]
    )

    assert expired is False
    assert repository.approval.status == ApprovalStatus.APPROVED
    assert stale_pending.id not in store.approval_requests
