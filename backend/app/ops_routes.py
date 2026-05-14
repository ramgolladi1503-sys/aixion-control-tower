from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .auth import require_maintainer
from .database_migrations import known_migration_ids
from .models import AuthUser, UserRole, now_utc
from .recovery_routes import RECOVERY_FORMAT_VERSION, export_recovery_snapshot
from .settings import get_settings
from .store import store

router = APIRouter(prefix="/ops", tags=["operations"])
MaintainerDependency = Depends(require_maintainer)


class RuntimeReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    generated_at: datetime = Field(default_factory=now_utc)
    profile: str
    auth_enabled: bool
    db_reachable: bool
    migrations_applied: bool
    expected_migration_ids: list[str] = Field(default_factory=list)
    applied_migration_ids: list[str] = Field(default_factory=list)
    recovery_snapshot_available: bool
    recovery_format_version: str
    github_token_configured: bool
    fcm_server_key_configured: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _probe_owner() -> AuthUser:
    return AuthUser(
        id="ops_readiness_probe",
        email="ops-readiness@local",
        display_name="Ops Readiness Probe",
        role=UserRole.OWNER,
    )


def build_runtime_readiness() -> RuntimeReadinessResponse:
    settings = get_settings()
    errors = list(settings.validation_errors)
    warnings: list[str] = []
    expected_migration_ids = sorted(known_migration_ids())
    applied_migration_ids: list[str] = []
    db_reachable = False
    migrations_applied = False
    recovery_snapshot_available = False

    try:
        applied_migrations = store.applied_migrations()
        applied_migration_ids = sorted(item["migration_id"] for item in applied_migrations)
        db_reachable = True
        missing_migration_ids = sorted(set(expected_migration_ids) - set(applied_migration_ids))
        migrations_applied = not missing_migration_ids
        if missing_migration_ids:
            errors.append("Missing database migrations: " + ", ".join(missing_migration_ids))
    except Exception as exc:  # pragma: no cover - exact sqlite exceptions vary by runtime
        errors.append(f"Database readiness check failed: {exc}")

    if db_reachable and migrations_applied:
        try:
            snapshot = export_recovery_snapshot(_probe_owner())
            recovery_snapshot_available = snapshot.format_version == RECOVERY_FORMAT_VERSION
            if not recovery_snapshot_available:
                errors.append(f"Unexpected recovery format version: {snapshot.format_version}")
        except Exception as exc:  # pragma: no cover - defensive operational check
            errors.append(f"Recovery snapshot readiness check failed: {exc}")

    if not settings.github_token_configured:
        warnings.append("GitHub token is not configured; GitHub execution will not be available.")
    if not settings.fcm_server_key_configured:
        warnings.append("FCM server key is not configured; push notifications will not be available.")

    ready = db_reachable and migrations_applied and recovery_snapshot_available and not errors

    return RuntimeReadinessResponse(
        status="ready" if ready else "not_ready",
        profile=settings.profile,
        auth_enabled=settings.auth_enabled,
        db_reachable=db_reachable,
        migrations_applied=migrations_applied,
        expected_migration_ids=expected_migration_ids,
        applied_migration_ids=applied_migration_ids,
        recovery_snapshot_available=recovery_snapshot_available,
        recovery_format_version=RECOVERY_FORMAT_VERSION,
        github_token_configured=settings.github_token_configured,
        fcm_server_key_configured=settings.fcm_server_key_configured,
        errors=errors,
        warnings=warnings,
    )


@router.get("/readiness", response_model=RuntimeReadinessResponse)
def get_runtime_readiness(_: AuthUser = MaintainerDependency) -> RuntimeReadinessResponse:
    return build_runtime_readiness()
