#!/usr/bin/env python3
"""Offline, fail-closed preflight for the draft Cloud Run dispatcher jobs.

This validates manifest shape and selected privilege/safety invariants only. It
does not authorize or perform any Google Cloud operation.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_JOBS = ROOT / "deploy/gcp/cloud-run-jobs-staging.yaml"
DEFAULT_SCHEDULER = ROOT / "deploy/gcp/cloud-scheduler-staging.tf"
PLACEHOLDER_MARKERS = ("REPLACE_WITH_", "replace-with-", "TODO")
PROJECT_ID_RE = r"[a-z][a-z0-9-]{4,28}[a-z0-9]"
SECRET_ENV_BY_JOB = {
    "aixion-push-dispatch": {"AIXION_POSTGRES_DSN"},
    "aixion-email-dispatch": {
        "AIXION_POSTGRES_DSN",
        "AIXION_EMAIL_SMTP_USERNAME",
        "AIXION_EMAIL_SMTP_PASSWORD",
        "AIXION_EMAIL_OUTBOX_KEY",
    },
}
COMMAND_BY_JOB = {
    "aixion-push-dispatch": "scripts/run_postgres_push_dispatch_once.py",
    "aixion-email-dispatch": "scripts/run_postgres_email_dispatch_once.py",
}
def _walk_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_strings(key)
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _terraform_string_default(text: str, variable: str) -> str | None:
    """Read a literal string default from one simple Terraform variable block."""
    block = re.search(
        rf'variable\s+"{re.escape(variable)}"\s*\{{(?P<body>.*?)\n\}}',
        text,
        re.DOTALL,
    )
    if block is None:
        return None
    default = re.search(r'\bdefault\s*=\s*"([^"\n]*)"', block.group("body"))
    return default.group(1) if default else None


def _terraform_string_assignments(text: str, variable: str) -> list[str]:
    """Read explicit `-var`/tfvars-style string assignments for one variable."""
    values: list[str] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("//"):
            continue
        match = re.fullmatch(rf'{re.escape(variable)}\s*=\s*("(?:[^"\\]|\\.)*")\s*(?:#.*)?', stripped)
        if match is None:
            if re.match(rf"{re.escape(variable)}\s*=", stripped):
                raise ValueError(
                    f"{variable} assignment at line {line_number} must be a literal string"
                )
            continue
        try:
            value = json.loads(match.group(1))
        except json.JSONDecodeError as error:
            raise ValueError(
                f"{variable} assignment at line {line_number} is not valid quoted string syntax"
            ) from error
        if not isinstance(value, str):
            raise ValueError(f"{variable} assignment at line {line_number} must be a string")
        if "${" in value or "%{" in value:
            raise ValueError(
                f"{variable} assignment at line {line_number} must not use Terraform interpolation"
            )
        values.append(value)
    return values


def _effective_terraform_string(
    text: str,
    variable: str,
    overrides: list[str],
    issues: list[str],
) -> str | None:
    default = _terraform_string_default(text, variable)
    if default is None:
        issues.append(f"Cloud Scheduler must declare literal {variable} input")
        return None
    selected = overrides[-1] if overrides else default
    if any(marker in selected for marker in PLACEHOLDER_MARKERS):
        issues.append(f"Terraform {variable} remains unresolved after applying overrides")
        return None
    if variable == "project_id" and not re.fullmatch(PROJECT_ID_RE, selected):
        issues.append("Terraform project_id must be a valid concrete GCP project ID")
        return None
    if variable == "firebase_project_id" and not re.fullmatch(PROJECT_ID_RE, selected):
        issues.append("Terraform firebase_project_id must be a valid concrete GCP project ID")
        return None
    if variable in {"cloud_run_region", "scheduler_region"} and not re.fullmatch(
        r"[a-z]+-[a-z]+[0-9]", selected
    ):
        issues.append(f"Terraform {variable} must be a concrete region")
        return None
    return selected


def _parse_jobs(text: str, issues: list[str]) -> list[dict[str, Any]]:
    try:
        documents = list(yaml.safe_load_all(text))
    except yaml.YAMLError as exc:
        issues.append(f"invalid Cloud Run Jobs YAML: {exc}")
        return []
    jobs = [document for document in documents if document is not None]
    if len(jobs) != 2:
        issues.append("exactly two Cloud Run Job documents are required")
    for value in _walk_strings(jobs):
        if any(marker in value for marker in PLACEHOLDER_MARKERS):
            issues.append("job manifest contains an unresolved project, image, or secret placeholder")
            break
    return [_mapping(job) for job in jobs]


def _validate_secret_aliases(
    annotations: dict[str, Any], project_number: str, issues: list[str], job_name: str
) -> set[str]:
    raw = annotations.get("run.googleapis.com/secrets")
    if not isinstance(raw, str):
        issues.append(f"{job_name} must map Secret Manager aliases")
        return set()
    aliases: set[str] = set()
    for mapping in raw.split(","):
        alias, separator, resource = mapping.strip().partition(":")
        expected_resource = rf"projects/{re.escape(project_number)}/secrets/[A-Za-z0-9_-]+"
        if not separator or not alias or not re.fullmatch(expected_resource, resource):
            issues.append(f"{job_name} Secret Manager aliases must be project-scoped resources")
            continue
        if alias in aliases:
            issues.append(f"{job_name} contains duplicate Secret Manager alias {alias}")
        aliases.add(alias)
    return aliases


def _validate_job(
    document: dict[str, Any], issues: list[str]
) -> tuple[str | None, str | None, str | None, str | None]:
    if document.get("apiVersion") != "run.googleapis.com/v1" or document.get("kind") != "Job":
        issues.append("each dispatcher document must use Cloud Run API v1 kind Job")
    metadata = _mapping(document.get("metadata"))
    name = metadata.get("name")
    if name not in COMMAND_BY_JOB:
        issues.append("job name must be aixion-push-dispatch or aixion-email-dispatch")
        name = "unknown"
    namespace = metadata.get("namespace")
    project_number = str(namespace or "")
    if isinstance(namespace, bool) or not re.fullmatch(r"[0-9]{6,}", project_number):
        issues.append(f"{name} must specify a numeric GCP project number")
        project_number = ""

    labels = _mapping(metadata.get("labels"))
    region = labels.get("cloud.googleapis.com/location")
    if not isinstance(region, str) or not re.fullmatch(r"[a-z]+-[a-z]+[0-9]", region):
        issues.append(f"{name} must declare a concrete Cloud Run region label")
        region = ""
    description = _mapping(metadata.get("annotations")).get("run.googleapis.com/description")
    if not isinstance(description, str) or "PRE-BILLING DRAFT ONLY" not in description:
        issues.append(f"{name} must remain explicitly marked PRE-BILLING DRAFT ONLY")

    spec = _mapping(document.get("spec"))
    template = _mapping(spec.get("template"))
    template_metadata = _mapping(template.get("metadata"))
    template_annotations = _mapping(template_metadata.get("annotations"))
    cloudsql = template_annotations.get("run.googleapis.com/cloudsql-instances")
    cloudsql_match = (
        re.fullmatch(rf"(?P<project>{PROJECT_ID_RE}):(?P<region>[a-z]+-[a-z]+[0-9]):(?P<instance>[a-z][a-z0-9-]{{0,62}})", cloudsql)
        if isinstance(cloudsql, str)
        else None
    )
    if cloudsql_match is None:
        issues.append(f"{name} must name its concrete same-region Cloud SQL instance")
        cloudsql_project = None
    else:
        cloudsql_project = cloudsql_match.group("project")
        if cloudsql_match.group("region") != region:
            issues.append(f"{name} Cloud SQL region must match the job region")

    aliases = _validate_secret_aliases(template_annotations, project_number, issues, str(name))
    job_spec = _mapping(template.get("spec"))
    if job_spec.get("parallelism") != 1 or job_spec.get("taskCount") != 1:
        issues.append(f"{name} must use exactly one task and parallelism 1")
    task_template = _mapping(job_spec.get("template"))
    task_spec = _mapping(task_template.get("spec"))
    if task_spec.get("maxRetries") != 0:
        issues.append(f"{name} must set Cloud Run task maxRetries to 0")
    if task_spec.get("timeoutSeconds") not in {"300s", 300}:
        issues.append(f"{name} must set the reviewed 300-second task timeout")
    service_account = task_spec.get("serviceAccountName")
    account_re = rf"[a-z][a-z0-9-]{{4,28}}[a-z0-9]@{PROJECT_ID_RE}\.iam\.gserviceaccount\.com"
    if not isinstance(service_account, str) or not re.fullmatch(account_re, service_account):
        issues.append(f"{name} must use a concrete job-specific runtime service account")

    containers = task_spec.get("containers")
    if not isinstance(containers, list) or len(containers) != 1:
        issues.append(f"{name} must contain exactly one dispatcher container")
        container: dict[str, Any] = {}
    else:
        container = _mapping(containers[0])
    image = container.get("image")
    image_pattern = (
        rf"(?P<region>[a-z]+-[a-z]+[0-9])-docker\.pkg\.dev/"
        rf"(?P<project>{PROJECT_ID_RE})/aixion/aixion-api@sha256:[a-f0-9]{{64}}"
    )
    image_match = re.fullmatch(image_pattern, image) if isinstance(image, str) else None
    if image_match is None:
        issues.append(f"{name} image must be this project's immutable Artifact Registry digest")
    else:
        if image_match.group("project") != cloudsql_project:
            issues.append(f"{name} image and Cloud SQL instance must use the same project")
        if image_match.group("region") != region:
            issues.append(f"{name} image and Cloud Run job must use the same region")
    if container.get("command") != ["python"]:
        issues.append(f"{name} container command must be python")
    if container.get("args") != [COMMAND_BY_JOB.get(str(name))]:
        issues.append(f"{name} must run its corresponding bounded one-shot script")

    env_items = container.get("env")
    if not isinstance(env_items, list):
        issues.append(f"{name} container env must be a list")
        env_items = []
    env: dict[str, dict[str, Any]] = {}
    for item in env_items:
        item_map = _mapping(item)
        env_name = item_map.get("name")
        if not isinstance(env_name, str) or env_name in env:
            issues.append(f"{name} environment names must be present and unique")
            continue
        env[env_name] = item_map

    expected_flags = {
        "AIXION_PROFILE": "production",
        "AIXION_OUTBOX_DISPATCH_MODE": "scheduled",
        "AIXION_POSTGRES_RUNTIME_PROBE_ENABLED": "true",
        "AIXION_PUSH_DISPATCH_ENABLED": "true" if name == "aixion-push-dispatch" else "false",
        "AIXION_EMAIL_DISPATCH_ENABLED": "true" if name == "aixion-email-dispatch" else "false",
    }
    for env_name, expected in expected_flags.items():
        if env.get(env_name, {}).get("value") != expected:
            issues.append(f"{name} must set {env_name} to {expected}")
    if "AIXION_DB_PATH" in env:
        issues.append(f"{name} must not configure SQLite AIXION_DB_PATH")

    expected_secrets = SECRET_ENV_BY_JOB.get(str(name), set())
    expected_aliases = {
        "AIXION_POSTGRES_DSN": "postgres-dsn",
        "AIXION_EMAIL_SMTP_USERNAME": "smtp-username",
        "AIXION_EMAIL_SMTP_PASSWORD": "smtp-password",
        "AIXION_EMAIL_OUTBOX_KEY": "email-outbox-key",
    }
    allowed_aliases = {expected_aliases[secret] for secret in expected_secrets}
    if aliases != allowed_aliases:
        issues.append(f"{name} Secret Manager aliases exceed or omit its provider secret boundary")
    referenced_secrets: set[str] = set()
    for env_name, item in env.items():
        value_from = _mapping(item.get("valueFrom"))
        secret_ref = _mapping(value_from.get("secretKeyRef"))
        if not secret_ref:
            continue
        referenced_secrets.add(env_name)
        alias = secret_ref.get("name")
        version = str(secret_ref.get("key", ""))
        if env_name not in expected_secrets:
            issues.append(f"{name} must not receive provider secrets outside its secret boundary")
        if alias not in aliases:
            issues.append(f"{name} {env_name} must reference a declared Secret Manager alias")
        if not re.fullmatch(r"[1-9][0-9]*", version):
            issues.append(f"{name} {env_name} must reference a positive numeric secret version")
    for env_name in expected_secrets:
        if env_name not in referenced_secrets:
            issues.append(f"{name} must load {env_name} from Secret Manager")

    # A push sender uses its job service account's ADC, never a downloaded key.
    if name == "aixion-push-dispatch":
        if env.get("AIXION_FCM_PROJECT_ID", {}).get("value") is None:
            issues.append("push job must define the Firebase project ID as non-secret configuration")
        if "GOOGLE_APPLICATION_CREDENTIALS" in env or any(
            "service-account" in secret.lower() or "private-key" in secret.lower()
            for secret in referenced_secrets
        ):
            issues.append("push job must use Google service identity/ADC, not downloaded credentials")
    if name == "aixion-email-dispatch":
        for required in ("AIXION_EMAIL_SMTP_HOST", "AIXION_EMAIL_FROM", "AIXION_EMAIL_SMTP_PORT"):
            if required not in env or "value" not in env[required]:
                issues.append(f"email job must define {required} configuration")
        if "AIXION_FCM_PROJECT_ID" in env:
            issues.append("email job must not receive push-provider configuration")

    resources = _mapping(_mapping(container.get("resources")).get("limits"))
    if resources.get("cpu") != "1" or resources.get("memory") != "512Mi":
        issues.append(f"{name} must retain the reviewed bounded 1 CPU / 512MiB task limits")
    return (
        service_account if isinstance(service_account, str) else None,
        region or None,
        project_number or None,
        cloudsql_project,
    )


def _terraform_resource(text: str, kind: str, name: str) -> str | None:
    match = re.search(rf'resource\s+"{re.escape(kind)}"\s+"{re.escape(name)}"\s*\{{', text)
    if match is None:
        return None
    start = match.end()
    depth = 1
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index]
    return None


def _validate_scheduler(
    text: str,
    issues: list[str],
    *,
    job_projects: set[str],
    job_regions: set[str],
    push_runtime_identity: str | None,
    terraform_overrides: dict[str, list[str]],
) -> None:
    effective: dict[str, str | None] = {}
    for variable in (
        "project_id", "cloud_run_region", "scheduler_region", "push_schedule",
        "email_schedule", "scheduler_timezone", "scheduler_service_account_id",
        "firebase_project_id", "push_runtime_service_account_email",
    ):
        effective[variable] = _effective_terraform_string(
            text, variable, terraform_overrides.get(variable, []), issues
        )
    terraform_project = effective["project_id"]
    terraform_region = effective["cloud_run_region"]
    if terraform_project is not None and job_projects and job_projects != {terraform_project}:
        issues.append("Cloud Scheduler project_id must match the Job image and Cloud SQL project")
    if terraform_region is not None and job_regions and job_regions != {terraform_region}:
        issues.append("Cloud Scheduler cloud_run_region must match the Job image and location")

    push_identity = effective["push_runtime_service_account_email"]
    if push_identity is not None and not re.fullmatch(
        rf"[a-z][a-z0-9-]{{4,28}}[a-z0-9]@{PROJECT_ID_RE}\.iam\.gserviceaccount\.com",
        push_identity,
    ):
        issues.append("FCM sender must target a concrete push runtime service account")
    if push_runtime_identity is None or push_identity != push_runtime_identity:
        issues.append("FCM sender identity must match the push Cloud Run job runtime identity")

    scheduler_account_id = effective["scheduler_service_account_id"]
    if scheduler_account_id is not None and not re.fullmatch(
        r"[a-z][a-z0-9-]{4,28}[a-z0-9]", scheduler_account_id
    ):
        issues.append("Scheduler service account ID must be 6-30 lowercase letters, digits, or dashes")
    if "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.cloud_run_region}/jobs/aixion-push-dispatch:run" not in text:
        issues.append("push Scheduler target must call the exact Cloud Run Jobs v2 run endpoint")
    if "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.cloud_run_region}/jobs/aixion-email-dispatch:run" not in text:
        issues.append("email Scheduler target must call the exact Cloud Run Jobs v2 run endpoint")
    if text.count("oauth_token {") != 2 or text.count('scope                 = "https://www.googleapis.com/auth/cloud-platform"') != 2:
        issues.append("both Scheduler targets must use OAuth tokens for Google APIs")
    if "oidc_token {" in text or 'scope                 = "https://www.googleapis.com/auth/cloud-platform"' not in text:
        issues.append("Scheduler must use OAuth, not OIDC, for the Google API target")
    if text.count('http_method = "POST"') != 2 or text.count('retry_count = 0') != 2:
        issues.append("Scheduler must POST each job trigger and use zero scheduler retries")
    if "attempt_deadline = \"300s\"" not in text:
        issues.append("Scheduler HTTP attempt deadline must be 300 seconds")
    project_iam_resources = re.findall(
        r'resource\s+"(google_project_iam_(?:member|binding|policy|custom_role))"\s+"([^"]+)"',
        text,
    )
    if project_iam_resources.count(("google_project_iam_member", "fcm_push_sender")) != 1 \
            or project_iam_resources.count(("google_project_iam_custom_role", "fcm_sender")) != 1 \
            or len(project_iam_resources) != 2:
        issues.append("FCM sender template must not add unrelated project-wide IAM grants")
    custom_role_resources = re.findall(
        r'resource\s+"google_project_iam_custom_role"\s+"([^"]+)"', text
    )
    if custom_role_resources != ["fcm_sender"]:
        issues.append("FCM sender must use exactly one dedicated project custom role")
    role_block = _terraform_resource(text, "google_project_iam_custom_role", "fcm_sender")
    if role_block is None:
        issues.append("FCM sender custom role is required")
    else:
        if 'project     = var.firebase_project_id' not in role_block:
            issues.append("FCM sender custom role must be scoped to the Firebase project")
        if 'permissions = ["cloudmessaging.messages.create"]' not in role_block:
            issues.append("FCM sender custom role must grant only cloudmessaging.messages.create")
    fcm_member = _terraform_resource(text, "google_project_iam_member", "fcm_push_sender")
    if fcm_member is None:
        issues.append("FCM sender must bind its custom role to the push runtime identity")
    else:
        if 'project = var.firebase_project_id' not in fcm_member:
            issues.append("FCM sender binding must be scoped to the Firebase project")
        if 'role    = google_project_iam_custom_role.fcm_sender.name' not in fcm_member:
            issues.append("FCM sender binding must use the dedicated FCM custom role")
        if 'member  = "serviceAccount:${var.push_runtime_service_account_email}"' not in fcm_member:
            issues.append("FCM sender binding must target only the push job runtime identity")

    expected_iam = {
        "push_invoker": "aixion-push-dispatch",
        "email_invoker": "aixion-email-dispatch",
    }
    for resource_name, job_name in expected_iam.items():
        block = _terraform_resource(text, "google_cloud_run_v2_job_iam_member", resource_name)
        if block is None:
            issues.append(f"{job_name} must have an explicit job-level IAM member")
            continue
        if 'role     = "roles/run.invoker"' not in block:
            issues.append(f"{job_name} scheduler identity must have only the Cloud Run invoker role")
        if f'name     = "{job_name}"' not in block:
            issues.append(f"{job_name} IAM grant must be scoped to that exact job")
        if 'member   = "serviceAccount:${google_service_account.scheduler_invoker.email}"' not in block:
            issues.append(f"{job_name} IAM grant must target the dedicated scheduler identity")

    for resource_name, expected_job in (
        ("push_dispatch", "aixion-push-dispatch"),
        ("email_dispatch", "aixion-email-dispatch"),
    ):
        block = _terraform_resource(text, "google_cloud_scheduler_job", resource_name)
        if block is None:
            issues.append(f"{expected_job} must have its own Cloud Scheduler resource")
            continue
        if f"jobs/{expected_job}:run" not in block:
            issues.append(f"{expected_job} Scheduler resource must target only that job")
        if "schedule         = var." not in block or "time_zone        = var.scheduler_timezone" not in block:
            issues.append(f"{expected_job} schedule and timezone must be explicit owner inputs")
        if "paused           = true" not in block:
            issues.append(f"{expected_job} Scheduler resource must remain paused in pre-billing")
        if 'service_account_email = google_service_account.scheduler_invoker.email' not in block:
            issues.append(f"{expected_job} must use the dedicated scheduler OAuth identity")


def validate_manifest_text(
    jobs_text: str,
    scheduler_text: str,
    *,
    terraform_overrides: dict[str, list[str]] | None = None,
) -> list[str]:
    """Return detected blockers; empty means local shape checks passed only."""
    issues: list[str] = []
    jobs = _parse_jobs(jobs_text, issues)
    regions: set[str] = set()
    cloudsql_projects: set[str] = set()
    service_accounts: list[str] = []
    if len(jobs) == 2:
        found: dict[str, dict[str, Any]] = {}
        for document in jobs:
            metadata = _mapping(document.get("metadata"))
            name = metadata.get("name")
            if isinstance(name, str):
                if name in found:
                    issues.append(f"duplicate Cloud Run Job {name}")
                found[name] = document
        if set(found) != set(COMMAND_BY_JOB):
            issues.append("the push and email dispatcher job names are both required")
        project_numbers: set[str] = set()
        for name in COMMAND_BY_JOB:
            if name not in found:
                continue
            identity, region, project_number, cloudsql_project = _validate_job(found[name], issues)
            if identity:
                service_accounts.append(identity)
            if region:
                regions.add(region)
            if project_number:
                project_numbers.add(project_number)
            if cloudsql_project:
                cloudsql_projects.add(cloudsql_project)
        if len(service_accounts) == 2 and service_accounts[0] == service_accounts[1]:
            issues.append("push and email jobs must use separate runtime service accounts")
        if len(project_numbers) > 1 or len(regions) > 1 or len(cloudsql_projects) > 1:
            issues.append("push and email jobs must use the same project and region")
    _validate_scheduler(
        scheduler_text,
        issues,
        job_projects=cloudsql_projects,
        job_regions=regions,
        push_runtime_identity=(service_accounts[0] if service_accounts else None),
        terraform_overrides=terraform_overrides or {},
    )
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=Path, default=DEFAULT_JOBS)
    parser.add_argument("--scheduler", type=Path, default=DEFAULT_SCHEDULER)
    parser.add_argument(
        "--tfvars", type=Path, action="append", default=[],
        help="Explicit .tfvars input to statically resolve (repeatable)",
    )
    parser.add_argument(
        "-var", dest="var_assignments", action="append", default=[], metavar="NAME=VALUE",
        help="Explicit Terraform string override (repeatable; later values take precedence)",
    )
    args = parser.parse_args(argv)
    try:
        scheduler_text = args.scheduler.read_text(encoding="utf-8")
        overrides: dict[str, list[str]] = {}
        supported = {
            "project_id", "cloud_run_region", "scheduler_region", "push_schedule",
            "email_schedule", "scheduler_timezone", "scheduler_service_account_id",
        }
        for path in args.tfvars:
            tfvars_text = path.read_text(encoding="utf-8")
            for variable in supported:
                overrides.setdefault(variable, []).extend(
                    _terraform_string_assignments(tfvars_text, variable)
                )
        for assignment in args.var_assignments:
            name, separator, value = assignment.partition("=")
            if not separator or name not in supported or not value:
                raise ValueError(f"unsupported Terraform string override: {assignment!r}")
            overrides.setdefault(name, []).append(value)
        issues = validate_manifest_text(
            args.jobs.read_text(encoding="utf-8"),
            scheduler_text,
            terraform_overrides=overrides,
        )
    except (OSError, ValueError) as exc:
        print(f"NOT APPLY-READY: invalid deployment preflight input: {exc}", file=sys.stderr)
        return 2
    if issues:
        print("NOT APPLY-READY:")
        for issue in issues:
            print(f"- {issue}")
        return 1
    print("LOCAL SHAPE CHECKS PASSED; owner, runtime, billing, IAM, and deployment gates remain.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
