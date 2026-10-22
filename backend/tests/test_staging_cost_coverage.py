from __future__ import annotations

from pathlib import Path

from scripts.validate_staging_cost_coverage import validate_cost_coverage_text

ROOT = Path(__file__).resolve().parents[2]
API_PATH = ROOT / "deploy/gcp/cloud-run-staging.yaml"
JOBS_PATH = ROOT / "deploy/gcp/cloud-run-jobs-staging.yaml"
SCHEDULER_PATH = ROOT / "deploy/gcp/cloud-scheduler-staging.tf"
COST_PATH = ROOT / "docs/STAGING_HANDOFF_COST_MODEL.md"


def _inputs() -> tuple[str, str, str, str]:
    return (
        API_PATH.read_text(encoding="utf-8"),
        JOBS_PATH.read_text(encoding="utf-8"),
        SCHEDULER_PATH.read_text(encoding="utf-8"),
        COST_PATH.read_text(encoding="utf-8"),
    )


def test_current_managed_topology_has_explicit_cost_rows() -> None:
    assert validate_cost_coverage_text(*_inputs()) == []


def test_adding_scheduler_without_cost_row_fails_closed() -> None:
    api, jobs, scheduler, cost = _inputs()
    cost = "\n".join(line for line in cost.splitlines() if "| Cloud Scheduler |" not in line)

    issues = validate_cost_coverage_text(api, jobs, scheduler, cost)

    assert any("Cloud Scheduler" in issue and "no cost-model row" in issue for issue in issues)


def test_cost_row_without_price_or_explicit_unpriced_treatment_fails() -> None:
    api, jobs, scheduler, cost = _inputs()
    cost = cost.replace(
        "| Cloud Scheduler | **UNPRICED** until the owner-approved schedule count, region, and applicable pricing are entered in the dated calculator export. Two proposed schedules exist; do not treat them as zero-cost. |",
        "| Cloud Scheduler | Estimate pending. |",
    )

    issues = validate_cost_coverage_text(api, jobs, scheduler, cost)

    assert any("Cloud Scheduler" in issue and "must state a price" in issue for issue in issues)


def test_new_supported_cloud_run_resource_without_cost_mapping_is_rejected() -> None:
    api, jobs, scheduler, cost = _inputs()
    api += "\n---\napiVersion: serving.knative.dev/v1\nkind: Service\nmetadata:\n  name: aixion-unpriced-sidecar\n"

    issues = validate_cost_coverage_text(api, jobs, scheduler, cost)

    # The current supported topology is explicit: an additional API service
    # cannot silently multiply the cost assumptions while still passing.
    assert any("exactly one Cloud Run API Service" in issue for issue in issues)


def test_unsupported_cloud_run_manifest_resource_is_rejected() -> None:
    api, jobs, scheduler, cost = _inputs()
    api += "\n---\napiVersion: storage.googleapis.com/v1\nkind: Bucket\nmetadata:\n  name: unpriced-bucket\n"

    issues = validate_cost_coverage_text(api, jobs, scheduler, cost)

    assert any("unsupported resource document" in issue for issue in issues)


def test_new_terraform_cloud_resource_without_mapping_is_rejected() -> None:
    api, jobs, scheduler, cost = _inputs()
    scheduler += '\nresource "google_pubsub_topic" "events" { name = "events" }\n'

    issues = validate_cost_coverage_text(api, jobs, scheduler, cost)

    assert any("unmapped Google Cloud resource type: google_pubsub_topic" in issue for issue in issues)


def test_cloud_sql_storage_and_backup_row_is_required() -> None:
    api, jobs, scheduler, cost = _inputs()
    cost = "\n".join(
        line for line in cost.splitlines()
        if "| Cloud SQL storage, backup, and network |" not in line
    )

    issues = validate_cost_coverage_text(api, jobs, scheduler, cost)

    assert any("Cloud SQL storage, backup, and network" in issue for issue in issues)


def test_malformed_scheduler_resource_declaration_is_not_silently_ignored() -> None:
    api, jobs, scheduler, cost = _inputs()
    scheduler = scheduler.replace(
        'resource "google_cloud_scheduler_job" "push_dispatch" {',
        'resource "google_cloud_scheduler_job" {',
        1,
    )

    issues = validate_cost_coverage_text(api, jobs, scheduler, cost)

    assert any("exactly two supported Cloud Scheduler" in issue for issue in issues)
