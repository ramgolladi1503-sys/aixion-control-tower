from __future__ import annotations

from scripts.validate_cloud_run_manifest import DEFAULT_MANIFEST, validate_manifest_text


def test_secret_reference_version_must_be_positive_numeric() -> None:
    candidate = DEFAULT_MANIFEST.read_text(encoding="utf-8").replace(
        'key: "1"',
        'key: "nonexistent-version"',
        1,
    )

    issues = validate_manifest_text(candidate)

    assert "AIXION_POSTGRES_DSN must use a positive numeric Secret Manager version" in issues


def test_pinned_numeric_secret_versions_are_accepted() -> None:
    issues = validate_manifest_text(DEFAULT_MANIFEST.read_text(encoding="utf-8"))

    assert not any("positive numeric Secret Manager version" in issue for issue in issues)
