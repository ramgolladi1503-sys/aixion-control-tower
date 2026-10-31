from __future__ import annotations

from scripts.validate_cloud_run_manifest import DEFAULT_MANIFEST, validate_manifest_text


def _resolved_candidate() -> str:
    text = DEFAULT_MANIFEST.read_text(encoding="utf-8")
    replacements = {
        "REPLACE_WITH_GCP_PROJECT_NUMBER": "123456789012",
        "REPLACE_WITH_PROJECT_ID": "aixion-staging",
        "REPLACE_WITH_INSTANCE": "aixion-staging-pg",
        "replace-with-postgres-dsn": "aixion-postgres-dsn",
        "replace-with-lease-signing-key": "aixion-lease-signing-key",
        "replace-with-auth-rate-limit-key": "aixion-auth-rate-limit-key",
        "replace-with-metrics-token": "aixion-metrics-token",
        "replace-with-smtp-username": "aixion-smtp-username",
        "replace-with-smtp-password": "aixion-smtp-password",
        "replace-with-email-outbox-key": "aixion-email-outbox-key",
        "replace-with-github-token": "aixion-github-token",
        "REPLACE_WITH_RUNTIME_SERVICE_ACCOUNT": "aixion-runtime@aixion-staging.iam.gserviceaccount.com",
        "REPLACE_WITH_IMAGE_DIGEST": "a" * 64,
        "REPLACE_WITH_STAGING_WORKSPACE_ID": "workspace-staging",
        "REPLACE_WITH_FIREBASE_PROJECT_ID": "aixion-firebase",
        "REPLACE_WITH_APPROVED_SMTP_HOST": "smtp.example.com",
        "REPLACE_WITH_VERIFIED_SENDER": "noreply@example.com",
    }
    for source, destination in replacements.items():
        text = text.replace(source, destination)
    return text


def test_checked_in_cloud_run_draft_is_blocked_by_unresolved_placeholders() -> None:
    issues = validate_manifest_text(DEFAULT_MANIFEST.read_text(encoding="utf-8"))

    assert "manifest contains an unresolved placeholder" in issues


def test_resolved_candidate_passes_local_shape_and_security_checks() -> None:
    assert validate_manifest_text(_resolved_candidate()) == []


def test_candidate_with_direct_public_ingress_is_rejected() -> None:
    candidate = _resolved_candidate().replace(
        "    run.googleapis.com/ingress: internal-and-cloud-load-balancing",
        "    run.googleapis.com/ingress: all",
    )

    assert (
        "Cloud Run ingress must be internal-and-cloud-load-balancing"
        in validate_manifest_text(candidate)
    )


def test_candidate_with_sqlite_path_is_rejected() -> None:
    candidate = _resolved_candidate().replace(
        "            - name: AIXION_PROFILE\n",
        "            - name: AIXION_DB_PATH\n"
        "              value: /tmp/service.sqlite\n"
        "            - name: AIXION_PROFILE\n",
    )

    assert "AIXION_DB_PATH is forbidden in the PostgreSQL staging manifest" in (
        validate_manifest_text(candidate)
    )


def test_candidate_with_unpinned_container_image_is_rejected() -> None:
    candidate = _resolved_candidate().replace(
        f"image: asia-south1-docker.pkg.dev/aixion-staging/aixion/aixion-api@sha256:{'a' * 64}",
        "image: asia-south1-docker.pkg.dev/aixion-staging/aixion/aixion-api:latest",
    )

    assert "container image must be pinned to a lowercase SHA-256 digest" in (
        validate_manifest_text(candidate)
    )


def test_candidate_without_service_maximum_instance_cap_is_rejected() -> None:
    candidate = _resolved_candidate().replace(
        '    run.googleapis.com/maxScale: "1"\n',
        "",
    )

    assert (
        "Cloud Run service maximum instance count must be explicitly set to 1"
        in validate_manifest_text(candidate)
    )


def test_candidate_with_increased_service_maximum_instance_cap_is_rejected() -> None:
    candidate = _resolved_candidate().replace(
        '    run.googleapis.com/maxScale: "1"',
        '    run.googleapis.com/maxScale: "2"',
    )

    assert (
        "Cloud Run service maximum instance count must be explicitly set to 1"
        in validate_manifest_text(candidate)
    )


def test_candidate_with_plaintext_secret_is_rejected() -> None:
    candidate = _resolved_candidate().replace(
        "              valueFrom:\n"
        "                secretKeyRef:\n"
        "                  name: postgres-dsn\n"
        "                  key: \"1\"\n",
        "              value: postgres://user:password@example.com/db\n",
        1,
    )

    assert (
        "AIXION_POSTGRES_DSN must come from a named Secret Manager-backed key reference"
        in validate_manifest_text(candidate)
    )


def test_duplicate_environment_variable_is_rejected() -> None:
    candidate = _resolved_candidate().replace(
        "            - name: AIXION_PROFILE\n              value: production\n",
        "            - name: AIXION_PROFILE\n              value: production\n"
        "            - name: AIXION_PROFILE\n              value: local\n",
    )

    assert "environment variable AIXION_PROFILE is duplicated" in validate_manifest_text(
        candidate
    )


def test_manifest_must_be_parseable_yaml() -> None:
    issues = validate_manifest_text("apiVersion: [")

    assert len(issues) == 1
    assert issues[0].startswith("invalid YAML:")
