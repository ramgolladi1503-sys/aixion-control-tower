from __future__ import annotations

from scripts.validate_cloud_run_manifest import DEFAULT_MANIFEST, validate_manifest_text


def test_digest_pinned_image_from_foreign_registry_is_rejected() -> None:
    candidate = (
        DEFAULT_MANIFEST.read_text(encoding="utf-8")
        .replace("REPLACE_WITH_PROJECT_ID", "aixion-staging")
        .replace("REPLACE_WITH_INSTANCE", "aixion-staging-pg")
        .replace("REPLACE_WITH_IMAGE_DIGEST", "a" * 64)
        .replace(
            "asia-south1-docker.pkg.dev/aixion-staging/aixion/aixion-api",
            "attacker.example/aixion-api",
        )
    )

    issues = validate_manifest_text(candidate)

    assert (
        "container image must use this project's asia-south1 Artifact Registry "
        "aixion/aixion-api repository" in issues
    )


def test_artifact_registry_project_must_match_cloud_sql_project() -> None:
    candidate = (
        DEFAULT_MANIFEST.read_text(encoding="utf-8")
        .replace("REPLACE_WITH_PROJECT_ID", "aixion-staging")
        .replace("REPLACE_WITH_INSTANCE", "aixion-staging-pg")
        .replace("REPLACE_WITH_IMAGE_DIGEST", "a" * 64)
        .replace(
            "asia-south1-docker.pkg.dev/aixion-staging/aixion/aixion-api",
            "asia-south1-docker.pkg.dev/other-project/aixion/aixion-api",
        )
    )

    issues = validate_manifest_text(candidate)

    assert "container image and Cloud SQL instance must use the same GCP project" in issues
