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
