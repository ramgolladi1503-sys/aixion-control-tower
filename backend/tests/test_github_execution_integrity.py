from __future__ import annotations

from app.approval_integrity import compute_approval_payload_hash
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
