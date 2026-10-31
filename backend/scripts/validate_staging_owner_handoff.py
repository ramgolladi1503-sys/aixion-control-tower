#!/usr/bin/env python3
"""Summarize offline Slice 7 blockers without contacting Google Cloud.

Exit status 0 means only that the owner record and local static checks contain
no detected blockers. It never authorizes a cloud action or deploy.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

from scripts.validate_cloud_run_jobs_manifest import validate_manifest_text as validate_jobs  # noqa: E402
from scripts.validate_cloud_run_manifest import validate_manifest_text as validate_api  # noqa: E402
from scripts.validate_staging_cost_coverage import validate_cost_coverage_text  # noqa: E402

DEFAULT_OWNER_RECORD = ROOT / "docs/STAGING_OWNER_DECISION_AND_COST_INPUT.md"
DEFAULT_COST_MODEL = ROOT / "docs/STAGING_HANDOFF_COST_MODEL.md"
DEFAULT_API = ROOT / "deploy/gcp/cloud-run-staging.yaml"
DEFAULT_JOBS = ROOT / "deploy/gcp/cloud-run-jobs-staging.yaml"
DEFAULT_SCHEDULER = ROOT / "deploy/gcp/cloud-scheduler-staging.tf"

REQUIRED_SECTIONS = {
    "1": "Accountable owner and project",
    "2": "Region and data-residency decision",
    "3": "Staging workload and topology inputs",
    "4": "Dated cost estimate and exposure decision",
    "5": "Retention, recovery, and data-handling approvals",
    "6": "Operators, alert recipients, and response",
    "7": "Project-specific implementation review",
    "8": "Separate authorization register for live cloud actions",
    "9": "Owner approval record",
}
REQUIRED_ROW_LABELS = {
    "1": (
        "Accountable staging project owner",
        "Dedicated GCP staging project ID",
        "Project number, verified against selected project",
        "Billing account associated",
        "Association verification date and verifier",
        "Confirm project is dedicated to staging",
    ),
    "2": (
        "Approved Cloud Run region",
        "Approved Cloud SQL region",
        "Approved Scheduler, Artifact Registry, Logging, and Monitoring locations",
        "Data-residency requirement",
        "Firebase/FCM project boundary",
    ),
    "3": (
        "Test accounts/workspaces",
        "Approval requests per day/month",
        "API requests per day/month",
        "Push/email notifications per day/month",
        "Push/email job cadence and latency target",
        "Average and maximum job runtime/invocations",
        "Cloud Run API CPU, memory, concurrency, timeout, min/max instances",
        "Cloud SQL edition/tier",
        "Database storage, growth, and autoscaling limit",
        "Backup/PITR schedule and retention",
        "Logs/metrics ingestion and retention",
        "Artifact Registry storage/retention",
        "Expected network ingress/egress and destinations",
        "Other billable services, support plans, or shared usage",
    ),
    "4": (
        "Calculator URL/share reference",
        "Export file/reference in approved restricted storage",
        "Estimate date and pricing date",
        "Project and region represented",
        "Currency, taxes, discounts, and free-tier assumptions",
        "Services/SKUs included and quantity assumptions",
        "Monthly low / expected / peak totals",
        "Excluded costs and reason for exclusion",
        "Independent reviewer and review date",
        "Whole-project budget notification target",
        "Cloud Run spend cap",
        "Project-wide maximum exposure",
        "Cloud SQL compute, storage, backups, and PITR maximum acceptable monthly exposure",
        "Other uncapped services",
        "Total residual exposure",
        "Authorized containment action and named operator",
        "Budget notification thresholds",
    ),
    "5": (
        "Account/profile/deletion records",
        "Sessions/authentication records",
        "Approval/decision/audit records",
        "Connector credentials and credential audit",
        "Application/infrastructure logs",
        "PostgreSQL automated backups and PITR/WAL",
        "Logical exports and recovery copies",
        "RPO and measured RTO",
    ),
    "6": (
        "Primary staging operator",
        "Backup staging operator",
        "Billing/cost alert recipient",
        "Reliability/security incident escalation",
        "Backup/restore operator",
        "Approved alert channel and test-recipient reference",
    ),
    "7": (
        "Slice 1 complete PostgreSQL",
        "Project-specific immutable image built and scanned",
        "Project/region-specific Cloud Run and Jobs manifests",
        "IAM/API/secret bindings",
        "Migration and rollback procedure",
        "Cloud SQL backup/PITR and isolated restore",
        "Health/readiness and approval-delivery alerts",
        "Spend notification/cap and uncapped-resource stop/teardown",
        "Privacy/retention and data-flow review",
    ),
    "8": (
        "Read-only project/billing/API/IAM inspection",
        "Enable billing or paid APIs",
        "Create/modify IAM bindings or secrets",
        "Create Cloud SQL",
        "Run migration/import or move real data",
        "Deploy or route traffic",
        "Cloud backup/restore, spend-control, rollback, or teardown rehearsal",
    ),
    "9": (
        "Owner decision:",
        "Owner name/role:",
        "Reviewer name/role:",
        "Decision date and record version:",
        "Conditions/exceptions:",
        "Restricted evidence references:",
    ),
}
OWNER_SIGNOFF_FIELDS = (
    "- Owner decision:",
    "- Owner name/role:",
    "- Reviewer name/role:",
    "- Decision date and record version:",
    "- Conditions/exceptions:",
    "- Restricted evidence references:",
)
BLOCKING_MARKER = re.compile(
    r"\b(?:PENDING|UNKNOWN|UNRESOLVED|TBD|NOT\s+ESTABLISHED|NOT\s+APPROVED|NOT\s+AUTHORIZED)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class HandoffReport:
    owner_input_rows_pending: dict[str, int]
    implementation_gates_pending: list[str]
    live_action_rows_not_authorized: list[str]
    owner_decision_pending: bool
    local_check_issues: list[str]
    status: str
    cloud_actions_authorized: bool = False


def _sections(markdown: str) -> tuple[dict[str, list[str]], list[str]]:
    sections: dict[str, list[str]] = {}
    issues: list[str] = []
    active: str | None = None
    for line in markdown.splitlines():
        heading = re.match(r"^##\s+(\d+)\.\s+(.+?)\s*$", line)
        if heading:
            active = heading.group(1)
            if active in sections:
                issues.append(f"owner record repeats section {active}")
            sections.setdefault(active, [])
            continue
        if active is not None:
            sections[active].append(line)
    for number, title in REQUIRED_SECTIONS.items():
        if number not in sections:
            issues.append(f"owner record is missing section {number}: {title}")
            continue
        labels = [row[0].lower() for row in _table_rows(sections[number]) if row]
        labels.extend(
            line.strip().lower()
            for line in sections[number]
            if line.strip().startswith("-")
        )
        for required in REQUIRED_ROW_LABELS[number]:
            if not any(required.lower() in label for label in labels):
                issues.append(f"owner record section {number} is missing required item: {required}")
    return sections, issues


def _table_rows(lines: Sequence[str]) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        if re.fullmatch(r"\|[\s:|-]+\|", stripped):
            continue
        rows.append([cell.strip() for cell in stripped.strip("|").split("|")])
    return rows


def _has_blocking_marker(value: str) -> bool:
    return BLOCKING_MARKER.search(value) is not None


def evaluate_owner_record(markdown: str, local_check_issues: Sequence[str] = ()) -> HandoffReport:
    sections, issues = _sections(markdown)
    owner_pending: dict[str, int] = {}
    for number in ("1", "2", "3", "4", "5", "6"):
        title = REQUIRED_SECTIONS[number]
        rows = _table_rows(sections.get(number, []))
        pending = sum(
            1 for row in rows if any(_has_blocking_marker(cell) for cell in row[1:])
        )
        if pending:
            owner_pending[title] = pending

    implementation_pending = []
    for row in _table_rows(sections.get("7", [])):
        if len(row) >= 3 and _has_blocking_marker(row[-1]):
            implementation_pending.append(row[0])

    unauthorized = []
    for row in _table_rows(sections.get("8", [])):
        if (
            len(row) >= 5
            and row[0].lower() != "live action requiring separate authorization"
            and _has_blocking_marker(row[-1])
        ):
            unauthorized.append(row[0])

    owner_decision_pending = any(
        line.strip().startswith(field) and _has_blocking_marker(line)
        for field in OWNER_SIGNOFF_FIELDS
        for line in sections.get("9", [])
    )
    all_issues = issues + list(local_check_issues)
    blocked = bool(
        owner_pending
        or implementation_pending
        or unauthorized
        or owner_decision_pending
        or all_issues
    )
    return HandoffReport(
        owner_input_rows_pending=owner_pending,
        implementation_gates_pending=implementation_pending,
        live_action_rows_not_authorized=unauthorized,
        owner_decision_pending=owner_decision_pending,
        local_check_issues=all_issues,
        status=(
            "BLOCKED — OWNER INPUTS OR LOCAL GATES REMAIN"
            if blocked
            else "RECORD CHECKS CLEAR"
        ),
    )


def _load_and_validate(paths: argparse.Namespace) -> HandoffReport:
    owner_text = paths.owner_record.read_text(encoding="utf-8")
    api_text = paths.api.read_text(encoding="utf-8")
    jobs_text = paths.jobs.read_text(encoding="utf-8")
    scheduler_text = paths.scheduler.read_text(encoding="utf-8")
    cost_text = paths.cost_model.read_text(encoding="utf-8")

    local_issues = []
    for issue in validate_api(api_text):
        local_issues.append(f"Cloud Run API manifest: {issue}")
    for issue in validate_jobs(jobs_text, scheduler_text):
        local_issues.append(f"Cloud Run Jobs/Scheduler manifests: {issue}")
    for issue in validate_cost_coverage_text(api_text, jobs_text, scheduler_text, cost_text):
        local_issues.append(f"Topology/cost coverage: {issue}")
    return evaluate_owner_record(owner_text, local_issues)


def _print_local_issue_summary(issues: Sequence[str]) -> None:
    grouped: dict[str, list[str]] = {}
    for issue in issues:
        category, separator, detail = issue.partition(": ")
        if not separator:
            category, detail = "Local validation", issue
        grouped.setdefault(category, [])
        if detail not in grouped[category]:
            grouped[category].append(detail)
    for category, details in grouped.items():
        print(f"- {category}: {len(details)} distinct issue(s)")
        for detail in details[:3]:
            print(f"  - {detail}")
        if len(details) > 3:
            print(f"  - and {len(details) - 3} more; use --json for the full list")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner-record", type=Path, default=DEFAULT_OWNER_RECORD)
    parser.add_argument("--cost-model", type=Path, default=DEFAULT_COST_MODEL)
    parser.add_argument("--api", type=Path, default=DEFAULT_API)
    parser.add_argument("--jobs", type=Path, default=DEFAULT_JOBS)
    parser.add_argument("--scheduler", type=Path, default=DEFAULT_SCHEDULER)
    parser.add_argument("--json", action="store_true", help="emit a machine-readable report")
    args = parser.parse_args(argv)
    try:
        report = _load_and_validate(args)
    except OSError as exc:
        report = evaluate_owner_record("", [f"Required offline input is unreadable: {exc}"])

    if args.json:
        print(json.dumps(asdict(report), indent=2, sort_keys=True))
    else:
        print(f"SLICE 7 OFFLINE HANDOFF: {report.status}")
        if report.owner_input_rows_pending:
            print("Owner-input rows still pending:")
            for section, count in report.owner_input_rows_pending.items():
                print(f"- {section}: {count} row(s)")
        if report.implementation_gates_pending:
            print("Implementation gates still pending:")
            for gate in report.implementation_gates_pending:
                print(f"- {gate}")
        if report.live_action_rows_not_authorized:
            print("Live actions remain not authorized:")
            for action in report.live_action_rows_not_authorized:
                print(f"- {action}")
        if report.owner_decision_pending:
            print("- Owner decision remains pending / not approved")
        if report.local_check_issues:
            print("Local static checks:")
            _print_local_issue_summary(report.local_check_issues)
        print("Cloud actions authorized: NO (this offline tool cannot grant authorization)")

    return 1 if report.status.startswith("BLOCKED") else 0


if __name__ == "__main__":
    raise SystemExit(main())
