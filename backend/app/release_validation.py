from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .audit_routes import AUDIT_EXPORT_MAX_LIMIT, AuditRetentionPolicy
from .models import AuthUser, UserRole, now_utc
from .ops_routes import build_runtime_readiness
from .recovery_routes import RECOVERY_FORMAT_VERSION, export_recovery_snapshot, validate_recovery_snapshot
from .settings import get_settings

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RELEASE_REPORT_PATH = REPO_ROOT / "docs" / "release_reports" / "backend_release_validation_summary.md"


class BackendReleaseValidationSummary(BaseModel):
    generated_at: datetime = Field(default_factory=now_utc)
    decision: Literal["PASS", "REVIEW_REQUIRED"]
    profile: str
    auth_enabled: bool
    readiness_status: str
    db_reachable: bool
    migrations_applied: bool
    expected_migration_ids: list[str] = Field(default_factory=list)
    applied_migration_ids: list[str] = Field(default_factory=list)
    recovery_snapshot_available: bool
    recovery_format_version: str
    recovery_snapshot_valid: bool
    recovery_warnings: list[str] = Field(default_factory=list)
    recovery_errors: list[str] = Field(default_factory=list)
    audit_retention_policy_version: str
    audit_online_retention_days: int
    audit_archive_retention_days: int
    audit_deletion_job_enabled: bool
    audit_export_max_limit: int
    github_token_configured: bool
    fcm_server_key_configured: bool
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


def _release_probe_owner() -> AuthUser:
    return AuthUser(
        id="release_validation_probe",
        email="release-validation@local",
        display_name="Release Validation Probe",
        role=UserRole.OWNER,
    )


def build_backend_release_validation_summary() -> BackendReleaseValidationSummary:
    settings = get_settings()
    readiness = build_runtime_readiness()
    audit_policy = AuditRetentionPolicy()

    recovery_snapshot = export_recovery_snapshot(_release_probe_owner())
    recovery_validation = validate_recovery_snapshot(recovery_snapshot)

    errors = [*readiness.errors, *recovery_validation.errors]
    warnings = [*readiness.warnings, *recovery_validation.warnings]
    decision: Literal["PASS", "REVIEW_REQUIRED"] = (
        "PASS" if readiness.status == "ready" and recovery_validation.valid and not errors else "REVIEW_REQUIRED"
    )

    return BackendReleaseValidationSummary(
        decision=decision,
        profile=settings.profile,
        auth_enabled=settings.auth_enabled,
        readiness_status=readiness.status,
        db_reachable=readiness.db_reachable,
        migrations_applied=readiness.migrations_applied,
        expected_migration_ids=readiness.expected_migration_ids,
        applied_migration_ids=readiness.applied_migration_ids,
        recovery_snapshot_available=readiness.recovery_snapshot_available,
        recovery_format_version=RECOVERY_FORMAT_VERSION,
        recovery_snapshot_valid=recovery_validation.valid,
        recovery_warnings=recovery_validation.warnings,
        recovery_errors=recovery_validation.errors,
        audit_retention_policy_version=audit_policy.policy_version,
        audit_online_retention_days=audit_policy.recommended_online_retention_days,
        audit_archive_retention_days=audit_policy.recommended_archive_retention_days,
        audit_deletion_job_enabled=audit_policy.deletion_job_enabled,
        audit_export_max_limit=AUDIT_EXPORT_MAX_LIMIT,
        github_token_configured=readiness.github_token_configured,
        fcm_server_key_configured=readiness.fcm_server_key_configured,
        warnings=warnings,
        errors=errors,
    )


def _markdown_list(values: list[str]) -> str:
    if not values:
        return "- none"
    return "\n".join(f"- {value}" for value in values)


def _markdown_code_list(values: list[str]) -> str:
    if not values:
        return "- none"
    return "\n".join(f"- `{value}`" for value in values)


def render_backend_release_validation_markdown(
    summary: BackendReleaseValidationSummary,
) -> str:
    return f"""# Backend Release Validation Summary

Generated at: `{summary.generated_at.isoformat()}`

Release decision: **{summary.decision}**

## Runtime readiness

- Profile: `{summary.profile}`
- Auth enabled: `{summary.auth_enabled}`
- Readiness status: `{summary.readiness_status}`
- Database reachable: `{summary.db_reachable}`
- GitHub token configured: `{summary.github_token_configured}`
- FCM server key configured: `{summary.fcm_server_key_configured}`

## Database migrations

- Migrations applied: `{summary.migrations_applied}`

Expected migrations:

{_markdown_code_list(summary.expected_migration_ids)}

Applied migrations:

{_markdown_code_list(summary.applied_migration_ids)}

## Recovery snapshot validation

- Recovery snapshot available: `{summary.recovery_snapshot_available}`
- Recovery format version: `{summary.recovery_format_version}`
- Recovery snapshot valid: `{summary.recovery_snapshot_valid}`

Recovery warnings:

{_markdown_list(summary.recovery_warnings)}

Recovery errors:

{_markdown_list(summary.recovery_errors)}

## Audit export and retention

- Retention policy version: `{summary.audit_retention_policy_version}`
- Recommended online retention days: `{summary.audit_online_retention_days}`
- Recommended archive retention days: `{summary.audit_archive_retention_days}`
- Audit deletion job enabled: `{summary.audit_deletion_job_enabled}`
- Audit export max limit: `{summary.audit_export_max_limit}`

## Overall warnings

{_markdown_list(summary.warnings)}

## Overall errors

{_markdown_list(summary.errors)}

## Operator note

This report is a release validation summary, not a deployment artifact. It does not replace CI,
manual release review, incident runbooks, database backups, or production monitoring.
"""


def write_backend_release_validation_summary(output_path: Path | str | None = None) -> Path:
    path = Path(output_path) if output_path is not None else DEFAULT_RELEASE_REPORT_PATH
    summary = build_backend_release_validation_summary()
    markdown = render_backend_release_validation_markdown(summary)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    return path
