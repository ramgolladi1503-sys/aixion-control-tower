"""Guard against overstating planning-only staging cost evidence.

These checks keep the reviewed handoff fail-closed: reference arithmetic is not
an all-in quote, and a proposed Cloud Run spend cap is not a project-wide cap.
They do not validate prices or authorize cloud activity.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COST_SCENARIOS = ROOT / "docs" / "STAGING_COST_SCENARIOS.md"
HANDOFF_COST_MODEL = ROOT / "docs" / "STAGING_HANDOFF_COST_MODEL.md"


def test_cloud_run_scenarios_remain_explicitly_partial_estimates() -> None:
    text = " ".join(COST_SCENARIOS.read_text(encoding="utf-8").lower().split())

    assert "planning worksheet only; not a quote" in text
    assert "estimate only cloud run request-based api compute/requests and two" in text
    for excluded in (
        "cloud sql",
        "database storage and backups",
        "network egress",
        "artifact registry",
        "cloud logging/monitoring",
        "secret manager",
        "taxes",
    ):
        assert excluded in text
    assert "must not be represented as the all-in monthly cost" in text
    assert "high case exceeds the proposed `$75/month` amount before adding the database" in text


def test_proposed_spend_cap_is_not_described_as_a_hard_project_ceiling() -> None:
    text = " ".join(
        HANDOFF_COST_MODEL.read_text(encoding="utf-8").lower().replace("**", "").split()
    )

    assert "a proposed $75/month amount is not an approved exposure limit" in text
    assert "$75 is not a hard project-wide ceiling" in text
    assert "an alerts-only budget is not a spending ceiling" in text
    assert "cloud sql and several supporting charges are outside cloud run spend-cap coverage" in text


def test_owner_only_cost_inputs_remain_unresolved_until_evidence_is_attached() -> None:
    text = HANDOFF_COST_MODEL.read_text(encoding="utf-8")
    worksheet_start = text.index("### Workload inputs for the dated calculator estimate")
    worksheet_end = text.index("### Proposed spend limit and enforcement boundary")
    worksheet = text[worksheet_start:worksheet_end]

    for required_row in (
        "| GCP project and billing account |",
        "| Region |",
        "| API calls |",
        "| PostgreSQL compute |",
        "| Scheduled jobs |",
        "| Network and egress |",
        "| Maximum exposure |",
    ):
        row = next((line for line in worksheet.splitlines() if line.startswith(required_row)), None)
        assert row is not None, f"required owner-input row is missing: {required_row}"
        assert "**PENDING**" in row, f"owner-only value was filled without review evidence: {required_row}"


def test_cloud_handoff_stays_blocked_on_postgres_and_authorized_cloud_evidence() -> None:
    text = " ".join(HANDOFF_COST_MODEL.read_text(encoding="utf-8").lower().split())

    assert "the production postgresql runtime gate in slice 1 passes" in text
    assert "no api enablement, iam binding, billing association, or resource creation is authorized" in text
    assert "a dated google pricing calculator export" in text
    assert "approved maximum exposure for uncapped services" in text
