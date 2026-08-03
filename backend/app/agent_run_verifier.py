from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Iterable

from .agent_run_models import AgentRunStepExecutionResult, VerificationDecision


TRANSIENT_MARKERS = (
    "timeout",
    "timed out",
    "temporarily unavailable",
    "connection reset",
    "connection refused",
    "rate limit",
    "http 429",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "worker lost",
    "lease expired",
    "container startup",
)

POLICY_BLOCK_MARKERS = (
    "protected branch",
    "direct main",
    "branch must not be main",
    "secret",
    "credential",
    "approval signature",
    "approved payload hash",
    "outside approved scope",
    "forbidden file",
    "unapproved mutation",
)

REVISION_MARKERS = (
    "tests failed",
    "validation failed",
    "objective incomplete",
    "scope change",
    "additional files",
    "patch does not match",
    "regression",
)

HUMAN_MARKERS = (
    "needs human",
    "manual review",
    "ambiguous",
    "conflicting payload",
    "already exists",
    "lost acknowledgement",
)


@dataclass(frozen=True)
class BinaryEvaluationRecord:
    predicted_positive: bool
    actual_positive: bool
    confidence: float


@dataclass(frozen=True)
class BinaryEvaluationMetrics:
    count: int
    true_positive: int
    false_positive: int
    true_negative: int
    false_negative: int
    precision: float
    recall: float
    f1: float
    brier_score: float
    expected_calibration_error: float
    mean_entropy: float


def canonical_evidence_hash(payload: Any) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _contains_any(reason: str, markers: tuple[str, ...]) -> bool:
    lowered = reason.lower()
    return any(marker in lowered for marker in markers)


def classify_execution_result(
    *,
    success: bool,
    reason: str,
    evidence: dict[str, Any] | None = None,
    duplicate_safe: bool = False,
) -> AgentRunStepExecutionResult:
    payload = evidence or {}
    if success:
        return AgentRunStepExecutionResult(
            success=True,
            decision=VerificationDecision.PASS,
            reason=reason or "Execution contract passed.",
            evidence=payload,
        )

    if duplicate_safe and _contains_any(reason, ("already exists", "duplicate", "already applied")):
        return AgentRunStepExecutionResult(
            success=True,
            decision=VerificationDecision.PASS,
            reason="Duplicate delivery matched previously verified side effects and was treated as idempotent.",
            evidence={**payload, "duplicate_safe": True, "original_reason": reason},
        )

    if _contains_any(reason, POLICY_BLOCK_MARKERS):
        decision = VerificationDecision.BLOCK_POLICY
    elif _contains_any(reason, REVISION_MARKERS):
        decision = VerificationDecision.NEEDS_REVISION
    elif _contains_any(reason, HUMAN_MARKERS):
        decision = VerificationDecision.NEEDS_HUMAN
    elif _contains_any(reason, TRANSIENT_MARKERS):
        decision = VerificationDecision.RETRY_TRANSIENT
    else:
        decision = VerificationDecision.FAIL_PERMANENT

    return AgentRunStepExecutionResult(
        success=False,
        decision=decision,
        reason=reason or "Execution contract failed without a reason.",
        evidence=payload,
    )


def validate_scope_contract(task: Any, approval: Any) -> AgentRunStepExecutionResult:
    reasons: list[str] = []
    repository = (getattr(task, "repository", None) or "").strip()
    branch = (getattr(task, "branch_preference", None) or "").strip()
    approval_status = str(getattr(approval, "status", "")) if approval is not None else ""
    files = list(getattr(approval, "files", []) or []) if approval is not None else []
    test_plan = list(getattr(approval, "test_plan", []) or []) if approval is not None else []

    if approval is None:
        reasons.append("Linked approval is required.")
    elif approval_status.upper() != "APPROVED":
        reasons.append(f"Linked approval must be APPROVED, got {approval_status or 'missing'}.")
    if not repository or "/" not in repository:
        reasons.append("Agent task repository must use owner/name format.")
    if not branch:
        reasons.append("Feature branch preference is required.")
    elif branch in {"main", "master"}:
        reasons.append("Protected branch policy: branch must not be main or master.")
    if not files:
        reasons.append("Approved file plan is empty.")
    if not test_plan:
        reasons.append("Approved validation plan is empty.")

    evidence = {
        "repository": repository,
        "branch": branch,
        "approval_status": approval_status,
        "file_count": len(files),
        "test_count": len(test_plan),
    }
    if reasons:
        return classify_execution_result(success=False, reason=" ".join(reasons), evidence=evidence)
    return classify_execution_result(
        success=True,
        reason="Approval, repository, feature branch, file plan, and validation plan satisfy the execution scope contract.",
        evidence=evidence,
    )


def binary_entropy(probability: float) -> float:
    p = min(1.0, max(0.0, probability))
    if p in {0.0, 1.0}:
        return 0.0
    return -(p * math.log2(p) + (1.0 - p) * math.log2(1.0 - p))


def compute_binary_evaluation_metrics(
    records: Iterable[BinaryEvaluationRecord],
    *,
    calibration_bins: int = 10,
) -> BinaryEvaluationMetrics:
    rows = list(records)
    tp = sum(1 for row in rows if row.predicted_positive and row.actual_positive)
    fp = sum(1 for row in rows if row.predicted_positive and not row.actual_positive)
    tn = sum(1 for row in rows if not row.predicted_positive and not row.actual_positive)
    fn = sum(1 for row in rows if not row.predicted_positive and row.actual_positive)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    brier = (
        sum((min(1.0, max(0.0, row.confidence)) - float(row.actual_positive)) ** 2 for row in rows) / len(rows)
        if rows
        else 0.0
    )

    bins: list[list[BinaryEvaluationRecord]] = [[] for _ in range(max(1, calibration_bins))]
    for row in rows:
        confidence = min(1.0, max(0.0, row.confidence))
        index = min(len(bins) - 1, int(confidence * len(bins)))
        bins[index].append(row)
    ece = 0.0
    for bucket in bins:
        if not bucket or not rows:
            continue
        mean_confidence = sum(min(1.0, max(0.0, row.confidence)) for row in bucket) / len(bucket)
        accuracy = sum(float(row.actual_positive) for row in bucket) / len(bucket)
        ece += len(bucket) / len(rows) * abs(mean_confidence - accuracy)

    mean_entropy = sum(binary_entropy(row.confidence) for row in rows) / len(rows) if rows else 0.0
    return BinaryEvaluationMetrics(
        count=len(rows),
        true_positive=tp,
        false_positive=fp,
        true_negative=tn,
        false_negative=fn,
        precision=precision,
        recall=recall,
        f1=f1,
        brier_score=brier,
        expected_calibration_error=ece,
        mean_entropy=mean_entropy,
    )
