from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from app.agent_run_models import VerificationDecision
from app.agent_run_verifier import (
    BinaryEvaluationRecord,
    binary_entropy,
    canonical_evidence_hash,
    classify_execution_result,
    compute_binary_evaluation_metrics,
    validate_scope_contract,
)
from app.agent_task_models import AgentTask, AgentTaskStatus
from app.approval_integrity import compute_approval_payload_hash
from app.models import (
    AgentProvider,
    ApprovalRequest,
    ApprovalStatus,
    FileChange,
    RiskAssessment,
    RiskLevel,
)


def test_execution_verifier_distinguishes_retry_revision_and_policy_block() -> None:
    transient = classify_execution_result(success=False, reason="GitHub request timed out temporarily.")
    revision = classify_execution_result(success=False, reason="Tests failed with a code regression.")
    blocked = classify_execution_result(success=False, reason="Forbidden file mutation outside approved scope.")

    assert transient.decision == VerificationDecision.RETRY_TRANSIENT
    assert revision.decision == VerificationDecision.NEEDS_REVISION
    assert blocked.decision == VerificationDecision.BLOCK_POLICY


def test_evidence_hash_is_stable_across_dictionary_order() -> None:
    assert canonical_evidence_hash({"b": 2, "a": 1}) == canonical_evidence_hash({"a": 1, "b": 2})


def test_binary_metrics_cover_precision_recall_calibration_and_entropy() -> None:
    metrics = compute_binary_evaluation_metrics(
        [
            BinaryEvaluationRecord(predicted_positive=True, actual_positive=True, confidence=0.9),
            BinaryEvaluationRecord(predicted_positive=True, actual_positive=False, confidence=0.8),
            BinaryEvaluationRecord(predicted_positive=False, actual_positive=True, confidence=0.3),
            BinaryEvaluationRecord(predicted_positive=False, actual_positive=False, confidence=0.1),
        ],
        calibration_bins=5,
    )

    assert metrics.count == 4
    assert metrics.true_positive == 1
    assert metrics.false_positive == 1
    assert metrics.false_negative == 1
    assert metrics.true_negative == 1
    assert metrics.precision == 0.5
    assert metrics.recall == 0.5
    assert metrics.f1 == 0.5
    assert 0.0 <= metrics.brier_score <= 1.0
    assert 0.0 <= metrics.expected_calibration_error <= 1.0
    assert 0.0 <= metrics.mean_entropy <= 1.0


def test_entropy_is_low_for_confident_probability_and_high_for_ambiguous_probability() -> None:
    assert binary_entropy(0.99) < binary_entropy(0.5)
    assert binary_entropy(0.5) == 1.0


def _approved_scope() -> tuple[AgentTask, ApprovalRequest]:
    approval = ApprovalRequest(
        project_id="project-1",
        workspace_id="workspace-1",
        title="Reviewed change",
        summary="Update the approved document.",
        agent_name="antigravity",
        target_branch="feature/reviewed-change",
        files=[
            FileChange(
                path="docs/reviewed.md",
                change_type="create",
                diff="+reviewed\n",
                new_content="reviewed\n",
            )
        ],
        test_plan=["python -m pytest"],
        rollback_plan="Revert the change.",
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.APPROVED,
        approved_payload_hash_version=2,
    )
    approval.approved_payload_hash = compute_approval_payload_hash(approval)
    task = AgentTask(
        provider=AgentProvider.ANTIGRAVITY,
        workspace_id=approval.workspace_id,
        project_id=approval.project_id,
        title=approval.title,
        goal=approval.summary,
        repository="owner/repository",
        branch_preference=approval.target_branch,
        approval_request_id=approval.id,
        status=AgentTaskStatus.APPROVED,
    )
    return task, approval


def test_scope_validation_requires_matching_approved_link_and_frozen_payload() -> None:
    task, approval = _approved_scope()

    result = validate_scope_contract(task, approval)

    assert result.decision == VerificationDecision.PASS
    assert result.evidence["approval_link_valid"] is True
    assert result.evidence["approved_payload_hash_valid"] is True


def test_scope_validation_blocks_changed_approval_payload() -> None:
    task, approval = _approved_scope()
    approval.summary = "Changed after approval."

    result = validate_scope_contract(task, approval)

    assert result.decision == VerificationDecision.BLOCK_POLICY
    assert result.evidence["approved_payload_hash_valid"] is False
    assert "approved payload hash" in result.reason.lower()


def test_scope_validation_blocks_link_to_another_approval() -> None:
    task, approval = _approved_scope()
    task.approval_request_id = "approval-from-another-task"

    result = validate_scope_contract(task, approval)

    assert result.decision == VerificationDecision.BLOCK_POLICY
    assert result.evidence["approval_link_valid"] is False
    assert "approval scope" in result.reason.lower()


def test_scope_validation_blocks_workspace_or_project_mismatch() -> None:
    task, approval = _approved_scope()
    task.workspace_id = "workspace-2"
    task.project_id = "project-2"

    result = validate_scope_contract(task, approval)

    assert result.decision == VerificationDecision.BLOCK_POLICY
    assert result.evidence["approval_link_valid"] is False
