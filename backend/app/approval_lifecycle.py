from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from .models import ApprovalRequest, ApprovalStatus


LEGACY_STATUS_MAP: dict[ApprovalStatus, ApprovalStatus] = {
    ApprovalStatus.PENDING_REVIEW: ApprovalStatus.REQUESTED,
    ApprovalStatus.REJECTED: ApprovalStatus.DENIED,
    ApprovalStatus.APPLIED: ApprovalStatus.EXECUTING,
    ApprovalStatus.TESTS_RUNNING: ApprovalStatus.EXECUTING,
    ApprovalStatus.TESTS_FAILED: ApprovalStatus.FAILED,
    ApprovalStatus.TESTS_PASSED: ApprovalStatus.MERGED,
}

VALID_TRANSITIONS: dict[ApprovalStatus, set[ApprovalStatus]] = {
    ApprovalStatus.REQUESTED: {
        ApprovalStatus.APPROVED,
        ApprovalStatus.DENIED,
        ApprovalStatus.EXPIRED,
        ApprovalStatus.CANCELLED,
    },
    ApprovalStatus.APPROVED: {
        ApprovalStatus.EXECUTING,
        ApprovalStatus.CANCELLED,
    },
    ApprovalStatus.EXECUTING: {
        ApprovalStatus.READY_FOR_PR,
        ApprovalStatus.FAILED,
    },
    ApprovalStatus.READY_FOR_PR: {
        ApprovalStatus.MERGED,
        ApprovalStatus.FAILED,
        ApprovalStatus.CANCELLED,
    },
    ApprovalStatus.FAILED: {
        ApprovalStatus.APPROVED,
        ApprovalStatus.CANCELLED,
    },
    ApprovalStatus.DENIED: set(),
    ApprovalStatus.EXPIRED: set(),
    ApprovalStatus.MERGED: set(),
    ApprovalStatus.CANCELLED: set(),
    ApprovalStatus.BLOCKED: {
        ApprovalStatus.CANCELLED,
    },
    ApprovalStatus.REVISION_REQUESTED: {
        ApprovalStatus.REQUESTED,
        ApprovalStatus.CANCELLED,
    },
    ApprovalStatus.PENDING_REVIEW: {
        ApprovalStatus.APPROVED,
        ApprovalStatus.DENIED,
        ApprovalStatus.EXPIRED,
        ApprovalStatus.CANCELLED,
    },
    ApprovalStatus.REJECTED: set(),
    ApprovalStatus.APPLIED: {
        ApprovalStatus.READY_FOR_PR,
        ApprovalStatus.FAILED,
    },
    ApprovalStatus.TESTS_RUNNING: {
        ApprovalStatus.READY_FOR_PR,
        ApprovalStatus.FAILED,
    },
    ApprovalStatus.TESTS_PASSED: {
        ApprovalStatus.MERGED,
    },
    ApprovalStatus.TESTS_FAILED: {
        ApprovalStatus.APPROVED,
        ApprovalStatus.CANCELLED,
    },
}

DASHBOARD_BUCKETS: dict[str, set[ApprovalStatus]] = {
    "action_required": {
        ApprovalStatus.REQUESTED,
        ApprovalStatus.BLOCKED,
        ApprovalStatus.REVISION_REQUESTED,
    },
    "approved_waiting": {
        ApprovalStatus.APPROVED,
    },
    "executing": {
        ApprovalStatus.EXECUTING,
    },
    "ready_for_pr": {
        ApprovalStatus.READY_FOR_PR,
    },
    "failed": {
        ApprovalStatus.FAILED,
    },
    "completed": {
        ApprovalStatus.MERGED,
    },
    "history": {
        ApprovalStatus.DENIED,
        ApprovalStatus.EXPIRED,
        ApprovalStatus.CANCELLED,
    },
}


def canonical_status(status: ApprovalStatus) -> ApprovalStatus:
    return LEGACY_STATUS_MAP.get(status, status)


def can_transition(previous: ApprovalStatus, new: ApprovalStatus) -> bool:
    previous = canonical_status(previous)
    new = canonical_status(new)
    return new in VALID_TRANSITIONS.get(previous, set())


def dashboard_bucket_for(status: ApprovalStatus) -> str:
    canonical = canonical_status(status)
    for bucket, statuses in DASHBOARD_BUCKETS.items():
        if canonical in statuses:
            return bucket
    return "history"


def grouped_approvals(approvals: Iterable[ApprovalRequest]) -> dict[str, list[ApprovalRequest]]:
    grouped: dict[str, list[ApprovalRequest]] = {bucket: [] for bucket in DASHBOARD_BUCKETS}
    grouped.update(defaultdict(list))
    for approval in approvals:
        grouped[dashboard_bucket_for(approval.status)].append(approval)
    return grouped
