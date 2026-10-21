from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

DEFAULT_MANIFEST = Path(__file__).resolve().parents[2] / "deploy/gcp/cloud-run-staging.yaml"
PLACEHOLDER_MARKERS = ("REPLACE_WITH_", "replace-with-", "TODO")
REQUIRED_ENV = {
    "AIXION_PROFILE": "production",
    "AIXION_AUTH_ENABLED": "true",
    "AIXION_TRUST_ENFORCEMENT": "true",
    "AIXION_OUTBOX_DISPATCH_MODE": "scheduled",
    "AIXION_POSTGRES_RUNTIME_PROBE_ENABLED": "true",
    "AIXION_POSTGRES_AUTH_RATE_LIMITS_ENABLED": "true",
    "AIXION_POSTGRES_AUTH_ENABLED": "true",
    "AIXION_PUSH_DISPATCH_ENABLED": "true",
    "AIXION_EMAIL_DISPATCH_ENABLED": "true",
}
SECRET_ENV = {
    "AIXION_POSTGRES_DSN",
    "AIXION_LEASE_SIGNING_KEY",
    "AIXION_AUTH_RATE_LIMIT_HMAC_KEY",
    "AIXION_METRICS_TOKEN",
    "AIXION_EMAIL_SMTP_USERNAME",
    "AIXION_EMAIL_SMTP_PASSWORD",
    "AIXION_EMAIL_OUTBOX_KEY",
    "GITHUB_TOKEN",
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


def validate_manifest_text(text: str) -> list[str]:
    """Return every detected blocking issue; an empty list means checks passed."""
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return [f"invalid YAML: {exc}"]

    issues: list[str] = []
    if not isinstance(document, dict):
        return ["manifest root must be a YAML mapping"]

    for value in _walk_strings(document):
        if any(marker in value for marker in PLACEHOLDER_MARKERS):
            issues.append("manifest contains an unresolved placeholder")

    if document.get("apiVersion") != "serving.knative.dev/v1":
        issues.append("apiVersion must be serving.knative.dev/v1")
    if document.get("kind") != "Service":
        issues.append("kind must be Service")

    metadata = document.get("metadata")
    if not isinstance(metadata, dict):
        issues.append("metadata must be a mapping")
        metadata = {}
    if not isinstance(metadata.get("name"), str) or not metadata["name"].strip():
        issues.append("metadata.name is required")
    namespace = metadata.get("namespace")
    project_number = str(namespace or "")
    if isinstance(namespace, bool) or not re.fullmatch(r"[0-9]{6,}", project_number):
        issues.append("metadata.namespace must be a numeric GCP project number")

    service_annotations = metadata.get("annotations")
    if not isinstance(service_annotations, dict):
        issues.append("metadata.annotations must be a mapping")
        service_annotations = {}
    if service_annotations.get("run.googleapis.com/ingress") != "all":
        issues.append("Cloud Run ingress must be explicitly set to all for mobile clients")
    if service_annotations.get("run.googleapis.com/invoker-iam-disabled") != "true":
        issues.append("public invocation must be explicit; API authentication remains mandatory")

    spec = document.get("spec")
    template = spec.get("template") if isinstance(spec, dict) else None
    template_metadata = template.get("metadata") if isinstance(template, dict) else None
    template_annotations = (
        template_metadata.get("annotations") if isinstance(template_metadata, dict) else None
    )
    if not isinstance(template_annotations, dict):
        issues.append("spec.template.metadata.annotations must be a mapping")
        template_annotations = {}
    cloudsql = template_annotations.get("run.googleapis.com/cloudsql-instances")
    if not isinstance(cloudsql, str) or not re.fullmatch(
        r"[a-z][a-z0-9-]{4,28}[a-z0-9]:asia-south1:[a-z][a-z0-9-]{0,62}",
        cloudsql,
    ):
        issues.append("Cloud SQL instance must name a concrete asia-south1 instance")

    secret_mapping = template_annotations.get("run.googleapis.com/secrets")
    secret_aliases: dict[str, str] = {}
    if not isinstance(secret_mapping, str):
        issues.append("run.googleapis.com/secrets must map Secret Manager aliases")
    else:
        for raw_mapping in secret_mapping.split(","):
            alias, separator, resource = raw_mapping.strip().partition(":")
            if not separator or not alias or not resource:
                issues.append("every Secret Manager mapping must contain alias and resource")
                continue
            if alias in secret_aliases:
                issues.append(f"Secret Manager alias {alias} is duplicated")
                continue
            expected_resource = (
                r"projects/" + re.escape(project_number) + r"/secrets/[A-Za-z0-9_-]+"
            )
            if not re.fullmatch(expected_resource, resource):
                issues.append(
                    f"Secret Manager alias {alias} must resolve within the declared project"
                )
                continue
            secret_aliases[alias] = resource

    pod = template.get("spec") if isinstance(template, dict) else None
    if not isinstance(pod, dict):
        issues.append("spec.template.spec must be a mapping")
        pod = {}
    service_account = pod.get("serviceAccountName")
    if not isinstance(service_account, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*@[A-Za-z0-9-]+\.iam\.gserviceaccount\.com",
        service_account,
    ):
        issues.append("serviceAccountName must be a concrete service-account email")

    containers = pod.get("containers")
    if not isinstance(containers, list) or len(containers) != 1:
        issues.append("exactly one API container is required")
        containers = []
    container = containers[0] if containers and isinstance(containers[0], dict) else {}
    image = container.get("image")
    if not isinstance(image, str) or not re.fullmatch(
        r"[^@\s]+@sha256:[a-f0-9]{64}", image
    ):
        issues.append("container image must be pinned to a lowercase SHA-256 digest")

    env_entries = container.get("env")
    if not isinstance(env_entries, list):
        issues.append("container.env must be a list")
        env_entries = []
    env_by_name: dict[str, dict[str, Any]] = {}
    for entry in env_entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            issues.append("every environment entry must have a name")
            continue
        name = entry["name"]
        if name in env_by_name:
            issues.append(f"environment variable {name} is duplicated")
            continue
        env_by_name[name] = entry

    for name, expected in REQUIRED_ENV.items():
        entry = env_by_name.get(name)
        if entry is None or entry.get("value") != expected:
            issues.append(f"{name} must be explicitly set to {expected}")
    if "AIXION_DB_PATH" in env_by_name:
        issues.append("AIXION_DB_PATH is forbidden in the PostgreSQL staging manifest")

    for name in sorted(SECRET_ENV):
        entry = env_by_name.get(name)
        value_from = entry.get("valueFrom") if entry else None
        ref = value_from.get("secretKeyRef") if isinstance(value_from, dict) else None
        if not isinstance(ref, dict) or not ref.get("name") or not ref.get("key"):
            issues.append(f"{name} must come from a named Secret Manager-backed key reference")
        elif ref["name"] not in secret_aliases:
            issues.append(f"{name} references an alias absent from run.googleapis.com/secrets")

    resources = container.get("resources")
    limits = resources.get("limits") if isinstance(resources, dict) else None
    if not isinstance(limits, dict) or not limits.get("cpu") or not limits.get("memory"):
        issues.append("container CPU and memory limits are required")

    probe = container.get("startupProbe")
    http_get = probe.get("httpGet") if isinstance(probe, dict) else None
    if (
        not isinstance(http_get, dict)
        or http_get.get("path") != "/health"
        or http_get.get("port") != 8000
    ):
        issues.append("startup probe must check /health on container port 8000")

    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail-closed offline validation for the Cloud Run staging manifest."
    )
    parser.add_argument(
        "manifest",
        nargs="?",
        type=Path,
        default=DEFAULT_MANIFEST,
        help=f"manifest path (default: {DEFAULT_MANIFEST})",
    )
    args = parser.parse_args(argv)

    try:
        text = args.manifest.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"manifest unreadable: {exc}", file=sys.stderr)
        return 2

    issues = validate_manifest_text(text)
    if issues:
        print(f"NOT APPLY-READY: {args.manifest}")
        for issue in issues:
            print(f"- {issue}")
        return 1

    print(f"Manifest preflight passed: {args.manifest}")
    print("This validates local manifest shape only; it does not authorize deployment.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
