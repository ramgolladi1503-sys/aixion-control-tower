from __future__ import annotations

from app.approval_lifecycle import can_transition, dashboard_bucket_for, grouped_approvals
from app.models import ApprovalRequest, ApprovalStatus, FileChange, RiskAssessment, RiskLevel


def _approval(status: ApprovalStatus) -> ApprovalRequest:
    return ApprovalRequest(
        project_id="project_test",
        title=f"Approval {status}",
        summary="Lifecycle test approval",
        agent_name="test-agent",
        target_branch="feature/test",
        files=[FileChange(path="README.md", diff="+test", new_content="test")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=status,
    )


def test_approved_items_are_not_action_required() -> None:
    grouped = grouped_approvals(
        [
            _approval(ApprovalStatus.REQUESTED),
            _approval(ApprovalStatus.APPROVED),
            _approval(ApprovalStatus.READY_FOR_PR),
            _approval(ApprovalStatus.DENIED),
        ]
    )

    assert [item.status for item in grouped["action_required"]] == [ApprovalStatus.REQUESTED]
    assert [item.status for item in grouped["approved_waiting"]] == [ApprovalStatus.APPROVED]
    assert [item.status for item in grouped["ready_for_pr"]] == [ApprovalStatus.READY_FOR_PR]
    assert [item.status for item in grouped["history"]] == [ApprovalStatus.DENIED]


def test_legacy_statuses_map_to_honest_dashboard_buckets() -> None:
    assert dashboard_bucket_for(ApprovalStatus.PENDING_REVIEW) == "action_required"
    assert dashboard_bucket_for(ApprovalStatus.REJECTED) == "history"
    assert dashboard_bucket_for(ApprovalStatus.APPLIED) == "executing"
    assert dashboard_bucket_for(ApprovalStatus.TESTS_RUNNING) == "executing"
    assert dashboard_bucket_for(ApprovalStatus.TESTS_FAILED) == "failed"
    assert dashboard_bucket_for(ApprovalStatus.TESTS_PASSED) == "completed"


def test_invalid_terminal_transitions_are_blocked() -> None:
    assert can_transition(ApprovalStatus.REQUESTED, ApprovalStatus.APPROVED)
    assert can_transition(ApprovalStatus.APPROVED, ApprovalStatus.EXECUTING)
    assert can_transition(ApprovalStatus.EXECUTING, ApprovalStatus.READY_FOR_PR)

    assert not can_transition(ApprovalStatus.DENIED, ApprovalStatus.EXECUTING)
    assert not can_transition(ApprovalStatus.EXPIRED, ApprovalStatus.EXECUTING)
    assert not can_transition(ApprovalStatus.MERGED, ApprovalStatus.EXECUTING)
    assert not can_transition(ApprovalStatus.REQUESTED, ApprovalStatus.READY_FOR_PR)
