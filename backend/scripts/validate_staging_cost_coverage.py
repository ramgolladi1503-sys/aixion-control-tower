#!/usr/bin/env python3
"""Check that declared staging services have explicit cost-model rows.

This is an offline documentation consistency check. It parses the checked-in
Cloud Run manifests and the Scheduler Terraform resource declarations, then
requires each detected managed service to have a corresponding row in the
structured cost-category table. It does not estimate prices or evaluate
Terraform, and it intentionally fails if a new managed service has no mapping.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_API = ROOT / "deploy/gcp/cloud-run-staging.yaml"
DEFAULT_JOBS = ROOT / "deploy/gcp/cloud-run-jobs-staging.yaml"
DEFAULT_SCHEDULER = ROOT / "deploy/gcp/cloud-scheduler-staging.tf"
DEFAULT_COST_MODEL = ROOT / "docs/STAGING_HANDOFF_COST_MODEL.md"

# Canonical current service -> exact structured cost table row. Extending the
# topology requires an explicit reviewed mapping and a cost-model row.
SERVICE_COST_ROWS = {
    "Cloud Run API": ("Cloud Run API",),
    "Cloud Run Jobs": ("Cloud Run Jobs",),
    "Cloud SQL": (
        "Cloud SQL shared-core staging compute",
        "Cloud SQL storage, backup, and network",
    ),
    "Artifact Registry": ("Artifact Registry",),
    "Secret Manager": ("Secret Manager",),
    "Cloud Scheduler": ("Cloud Scheduler",),
    "Cloud Logging": ("Cloud Logging",),
    "Cloud Monitoring": ("Cloud Monitoring",),
    "Firebase Cloud Messaging": ("Firebase Cloud Messaging",),
}
KNOWN_TERRAFORM_RESOURCES = {
    # Google-managed identities and IAM bindings have no direct service usage
    # line item; their underlying Cloud Run/Scheduler service is costed above.
    "google_service_account",
    "google_cloud_run_v2_job_iam_member",
    "google_project_iam_custom_role",
    "google_project_iam_member",
    "google_cloud_scheduler_job",
}


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _parse_yaml_documents(text: str, label: str, issues: list[str]) -> list[dict[str, Any]]:
    try:
        return [_mapping(value) for value in yaml.safe_load_all(text) if value is not None]
    except yaml.YAMLError as error:
        issues.append(f"{label} is invalid YAML: {error}")
        return []


def _declared_services(
    api_text: str, jobs_text: str, scheduler_text: str, handoff_text: str, issues: list[str]
) -> set[str]:
    services: set[str] = set()
    api_docs = _parse_yaml_documents(api_text, "Cloud Run API manifest", issues)
    api_services = [
        doc for doc in api_docs
        if doc.get("apiVersion") == "serving.knative.dev/v1" and doc.get("kind") == "Service"
    ]
    if len(api_services) != 1:
        issues.append("expected exactly one Cloud Run API Service manifest")
    else:
        services.add("Cloud Run API")
    if any(doc not in api_services for doc in api_docs):
        issues.append("Cloud Run API manifest contains an unsupported resource document")

    job_docs = _parse_yaml_documents(jobs_text, "Cloud Run Jobs manifest", issues)
    jobs = [
        doc for doc in job_docs
        if doc.get("apiVersion") == "run.googleapis.com/v1" and doc.get("kind") == "Job"
    ]
    if len(jobs) != 2:
        issues.append("expected exactly two Cloud Run Job manifests")
    else:
        services.add("Cloud Run Jobs")
    if any(doc not in jobs for doc in job_docs):
        issues.append("Cloud Run Jobs manifest contains an unsupported resource document")

    manifest_text = api_text + "\n" + jobs_text
    if "run.googleapis.com/cloudsql-instances" in manifest_text:
        services.add("Cloud SQL")
    if ".pkg.dev/" in manifest_text or "artifactregistry" in manifest_text.lower():
        services.add("Artifact Registry")
    if "run.googleapis.com/secrets" in manifest_text or "secretKeyRef" in manifest_text:
        services.add("Secret Manager")
    terraform_resources = re.findall(r'resource\s+"(google_[^"]+)"\s+"[^"]+"\s*\{', scheduler_text)
    unknown_tf_resources = set(terraform_resources) - KNOWN_TERRAFORM_RESOURCES
    if unknown_tf_resources:
        issues.append(
            "Terraform declares an unmapped Google Cloud resource type: "
            + ", ".join(sorted(unknown_tf_resources))
        )
    scheduler_resources = [
        resource for resource in terraform_resources if resource == "google_cloud_scheduler_job"
    ]
    if len(scheduler_resources) == 2:
        services.add("Cloud Scheduler")
    elif "google_cloud_scheduler_job" in scheduler_text:
        issues.append("expected exactly two supported Cloud Scheduler job resources")

    # These supporting services are declared by the handoff's proposed staging
    # component table rather than deployable manifests. Require those labels in
    # that topology table so prose elsewhere cannot silently change the scope.
    component_rows = _markdown_table_first_cells_after(handoff_text, "| Component |")
    if any("Cloud Logging" in cell for cell in component_rows):
        services.add("Cloud Logging")
    if any("Cloud Monitoring" in cell or "Monitoring" in cell for cell in component_rows):
        services.add("Cloud Monitoring")
    if "AIXION_FCM_PROJECT_ID" in manifest_text or any("Firebase Cloud Messaging" in cell for cell in component_rows):
        services.add("Firebase Cloud Messaging")

    unknown = services - SERVICE_COST_ROWS.keys()
    if unknown:
        issues.append("no reviewed cost-row mapping exists for: " + ", ".join(sorted(unknown)))
    return services


def _markdown_table_first_cells_after(text: str, header: str) -> list[str]:
    lines = text.splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.strip().startswith(header))
    except StopIteration:
        return []
    cells: list[str] = []
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if not stripped.startswith("|"):
            break
        if re.fullmatch(r"\|[\s:|-]+\|", stripped):
            continue
        parts = [part.strip() for part in stripped.strip("|").split("|")]
        if parts:
            cells.append(parts[0])
    return cells


def _cost_rows(text: str) -> dict[str, str]:
    lines = text.splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == "| Cost category | Planning treatment |")
    except StopIteration:
        return {}
    rows: dict[str, str] = {}
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if not stripped.startswith("|"):
            break
        if re.fullmatch(r"\|[\s:|-]+\|", stripped):
            continue
        parts = [part.strip() for part in stripped.strip("|").split("|")]
        if len(parts) >= 2:
            rows[parts[0]] = parts[1]
    return rows


def validate_cost_coverage_text(
    api_text: str, jobs_text: str, scheduler_text: str, handoff_text: str
) -> list[str]:
    issues: list[str] = []
    services = _declared_services(api_text, jobs_text, scheduler_text, handoff_text, issues)
    cost_rows = _cost_rows(handoff_text)
    if not cost_rows:
        issues.append("cost model must contain the structured '| Cost category | Planning treatment |' table")
    for service in sorted(services):
        rows = SERVICE_COST_ROWS.get(service)
        if rows is None:
            continue
        for row in rows:
            treatment = cost_rows.get(row)
            if treatment is None:
                issues.append(f"declared {service} has no cost-model row '{row}'")
            elif not treatment.strip() or not re.search(r"\b(UNPRICED|\$|USD|free|variable|usage|not estimated|not priced)\b", treatment, re.IGNORECASE):
                issues.append(f"cost-model row '{row}' must state a price or explicitly retain an unpriced/usage treatment")
    return issues


def validate_paths(api_path: Path, jobs_path: Path, scheduler_path: Path, handoff_path: Path) -> list[str]:
    paths = [api_path, jobs_path, scheduler_path, handoff_path]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        return ["required cost coverage input is missing: " + ", ".join(missing)]
    return validate_cost_coverage_text(
        api_path.read_text(encoding="utf-8"),
        jobs_path.read_text(encoding="utf-8"),
        scheduler_path.read_text(encoding="utf-8"),
        handoff_path.read_text(encoding="utf-8"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", type=Path, default=DEFAULT_API)
    parser.add_argument("--jobs", type=Path, default=DEFAULT_JOBS)
    parser.add_argument("--scheduler", type=Path, default=DEFAULT_SCHEDULER)
    parser.add_argument("--cost-model", type=Path, default=DEFAULT_COST_MODEL)
    args = parser.parse_args()
    issues = validate_paths(args.api, args.jobs, args.scheduler, args.cost_model)
    if issues:
        print("NOT COST-COMPLETE")
        for issue in issues:
            print(f"- {issue}")
        return 1
    print("COST ROW COVERAGE COMPLETE (prices and owner inputs may remain unpriced)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
