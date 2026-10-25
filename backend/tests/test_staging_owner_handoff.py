from __future__ import annotations

from pathlib import Path

import pytest

from scripts.validate_staging_owner_handoff import evaluate_owner_record

ROOT = Path(__file__).resolve().parents[2]
OWNER_RECORD = ROOT / "docs" / "STAGING_OWNER_DECISION_AND_COST_INPUT.md"


def test_current_owner_record_is_reported_blocked_with_explicit_pending_categories() -> None:
    report = evaluate_owner_record(OWNER_RECORD.read_text(encoding="utf-8"))

    assert report.status.startswith("BLOCKED")
    assert report.owner_input_rows_pending["Accountable owner and project"] > 0
    assert report.implementation_gates_pending
    assert report.live_action_rows_not_authorized
    assert report.owner_decision_pending
    assert report.cloud_actions_authorized is False


def test_missing_required_owner_decision_row_fails_closed() -> None:
    text = OWNER_RECORD.read_text(encoding="utf-8")
    text = "\n".join(
        line for line in text.splitlines()
        if not line.startswith("| Dedicated GCP staging project ID |")
    )

    report = evaluate_owner_record(text)

    assert any("Dedicated GCP staging project ID" in issue for issue in report.local_check_issues)
    assert report.status.startswith("BLOCKED")


@pytest.mark.parametrize(
    "row_prefix",
    [
        "| Accountable staging project owner",
        "| Cloud SQL edition/tier",
        "| Other uncapped services",
        "| Approval/decision/audit records",
        "| Primary staging operator",
        "| IAM/API/secret bindings",
        "| Migration and rollback procedure",
    ],
)
def test_missing_exit_gate_input_rows_keep_handoff_blocked(row_prefix: str) -> None:
    text = OWNER_RECORD.read_text(encoding="utf-8")
    filtered = [line for line in text.splitlines() if not line.startswith(row_prefix)]
    assert len(filtered) < len(text.splitlines()), f"test row not found: {row_prefix}"

    report = evaluate_owner_record("\n".join(filtered))

    assert report.status.startswith("BLOCKED")
    assert any(row_prefix.removeprefix("| ") in issue for issue in report.local_check_issues)


def test_unknown_owner_input_remains_a_blocker() -> None:
    text = OWNER_RECORD.read_text(encoding="utf-8").replace(
        "| Accountable staging project owner (name/role) | **PENDING** | **PENDING** | **PENDING** | **PENDING** |",
        "| Accountable staging project owner (name/role) | **UNKNOWN** | evidence-1 | reviewer-1 | recorded |",
        1,
    )

    report = evaluate_owner_record(text)

    assert report.status.startswith("BLOCKED")
    assert report.owner_input_rows_pending["Accountable owner and project"] == 6


def test_local_validator_issues_are_included_in_blocking_report() -> None:
    report = evaluate_owner_record(
        OWNER_RECORD.read_text(encoding="utf-8"),
        ["Cloud Run manifest contains an unresolved project placeholder"],
    )

    assert report.local_check_issues == [
        "Cloud Run manifest contains an unresolved project placeholder"
    ]
    assert report.status.startswith("BLOCKED")


def test_tool_never_claims_to_authorize_cloud_actions() -> None:
    text = OWNER_RECORD.read_text(encoding="utf-8").replace(
        "- Owner decision: **PENDING — NOT APPROVED**",
        "- Owner decision: **APPROVED**",
    )

    report = evaluate_owner_record(text)

    assert report.cloud_actions_authorized is False


def test_named_owner_signoff_fields_remain_blockers_when_decision_is_marked_approved() -> None:
    text = OWNER_RECORD.read_text(encoding="utf-8").replace(
        "- Owner decision: **PENDING — NOT APPROVED**",
        "- Owner decision: **APPROVED**",
    ).replace(
        "- Owner name/role: **PENDING**",
        "- Owner name/role: **UNKNOWN**",
    ).replace(
        "- Reviewer name/role: **PENDING**",
        "- Reviewer name/role: reviewer-1",
    ).replace(
        "- Decision date and record version: **PENDING**",
        "- Decision date and record version: 2026-10-25 / v1",
    ).replace(
        "- Conditions/exceptions: **PENDING**",
        "- Conditions/exceptions: none recorded",
    ).replace(
        "- Restricted evidence references: **PENDING**",
        "- Restricted evidence references: ref-1",
    )

    report = evaluate_owner_record(text)

    assert report.owner_decision_pending is True
    assert report.status.startswith("BLOCKED")
