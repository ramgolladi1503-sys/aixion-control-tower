from __future__ import annotations

from scripts.validate_cloud_run_manifest import DEFAULT_MANIFEST, validate_manifest_text


def test_secret_environment_alias_must_exist_in_cloud_run_secret_mapping() -> None:
    candidate = DEFAULT_MANIFEST.read_text(encoding="utf-8").replace(
        "postgres-dsn:projects/REPLACE_WITH_GCP_PROJECT_NUMBER/secrets/replace-with-postgres-dsn",
        "missing-dsn:projects/REPLACE_WITH_GCP_PROJECT_NUMBER/secrets/replace-with-postgres-dsn",
    )

    issues = validate_manifest_text(candidate)

    assert "AIXION_POSTGRES_DSN references an alias absent from run.googleapis.com/secrets" in (
        issues
    )


def test_secret_manager_mapping_must_use_declared_project_number() -> None:
    candidate = DEFAULT_MANIFEST.read_text(encoding="utf-8").replace(
        "postgres-dsn:projects/REPLACE_WITH_GCP_PROJECT_NUMBER/secrets/replace-with-postgres-dsn",
        "postgres-dsn:projects/999999999999/secrets/replace-with-postgres-dsn",
    )

    issues = validate_manifest_text(candidate)

    assert "Secret Manager alias postgres-dsn must resolve within the declared project" in (
        issues
    )
