from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.approval_integrity import compute_approval_payload_hash
from app import github_runner
from app.github_runner import PatchPlanRequest, _validate_execution
from app.models import ApprovalRequest, ApprovalStatus, FileChange, RiskAssessment, RiskLevel
from app.store import store


def setup_function() -> None:
    store.reset()


def _approval(status: ApprovalStatus = ApprovalStatus.APPROVED) -> ApprovalRequest:
    approval = ApprovalRequest(
        project_id="project_test",
        title="Approved docs change",
        summary="Update approved docs content",
        agent_name="test-agent",
        target_branch="feature/docs-change",
        files=[
            FileChange(
                path="docs/README.md",
                change_type="update",
                diff="+approved",
                new_content="approved content\n",
            )
        ],
        test_plan=[],
        rollback_plan="Revert docs commit",
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=status,
    )
    approval.approved_payload_hash = compute_approval_payload_hash(approval)
    store.approval_requests[approval.id] = approval
    return approval


def _plan(approval: ApprovalRequest) -> PatchPlanRequest:
    return PatchPlanRequest(
        approval_request_id=approval.id,
        repository_full_name="ramgolladi1503-sys/aixion-control-tower",
        base_branch="main",
        feature_branch="feature/docs-change",
    )


def test_patch_plan_allows_approved_unchanged_payload() -> None:
    approval = _approval()

    plan = _validate_execution(_plan(approval))

    assert plan.ready is True
    assert plan.blockers == []


def test_patch_plan_blocks_missing_approved_payload_hash() -> None:
    approval = _approval()
    approval.approved_payload_hash = None

    plan = _validate_execution(_plan(approval))

    assert plan.ready is False
    assert "Approval request is missing approved payload hash; re-approval is required." in plan.blockers


def test_patch_plan_blocks_payload_changed_after_approval() -> None:
    approval = _approval()
    approval.files[0].new_content = "tampered content\n"

    plan = _validate_execution(_plan(approval))

    assert plan.ready is False
    assert "Approval payload changed after approval; re-approval is required before execution." in plan.blockers


def test_patch_plan_blocks_denied_and_expired_statuses() -> None:
    for status in (ApprovalStatus.DENIED, ApprovalStatus.EXPIRED):
        store.reset()
        approval = _approval(status=status)
        plan = _validate_execution(_plan(approval))

        assert plan.ready is False
        assert "Approval request must be APPROVED before GitHub execution." in plan.blockers


@pytest.mark.parametrize(
    ("is_production", "postgres_auth_enabled"),
    [(True, False), (False, True)],
    ids=["production", "postgres-auth"],
)
def test_legacy_github_runner_fails_closed_before_local_approval_lookup(
    monkeypatch,
    is_production: bool,
    postgres_auth_enabled: bool,
) -> None:
    class ReadTrap(dict):
        def get(self, *_args, **_kwargs):
            pytest.fail("Authoritative mode must not read SQLite approval state")

    monkeypatch.setattr(store, "approval_requests", ReadTrap())
    monkeypatch.setattr(
        github_runner,
        "get_settings",
        lambda: SimpleNamespace(
            is_production=is_production,
            postgres_auth_enabled=postgres_auth_enabled,
        ),
    )
    payload = PatchPlanRequest(
        approval_request_id="sqlite-only-approval",
        repository_full_name="owner/repository",
        base_branch="main",
        feature_branch="feature/change",
    )

    with pytest.raises(HTTPException) as raised:
        _validate_execution(payload)

    assert raised.value.status_code == 503
    assert raised.value.headers["Retry-After"] == "1"
