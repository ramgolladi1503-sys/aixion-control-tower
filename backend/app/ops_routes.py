from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .auth import require_maintainer
from .database_migrations import known_migration_ids
from .external_agent_readiness import ExternalAgentReadinessResponse, build_external_agent_readiness
from .models import AuthUser, UserRole, now_utc
from .recovery_routes import RECOVERY_FORMAT_VERSION, export_recovery_snapshot
from .settings import get_settings
from .store import store
from .trust_flight_recorder import verify_flight_recorder
from .trust_models import CapabilityLeaseStatus

router = APIRouter(prefix="/ops", tags=["operations"])
MaintainerDependency = Depends(require_maintainer)


class RuntimeReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    generated_at: datetime = Field(default_factory=now_utc)
    profile: str
    auth_enabled: bool
    trust_enforcement: bool
    lease_signing_key_configured: bool
    db_reachable: bool
    migrations_applied: bool
    expected_migration_ids: list[str] = Field(default_factory=list)
    applied_migration_ids: list[str] = Field(default_factory=list)
    recovery_snapshot_available: bool
    recovery_format_version: str
    github_token_configured: bool
    github_app_configured: bool
    fcm_server_key_configured: bool
    flight_recorder_valid: bool
    flight_recorder_event_count: int
    active_capability_leases: int
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

    flight = verify_flight_recorder()
    if not flight.valid:
        errors.append(
            "Trust flight recorder failed hash-chain verification: " + flight.reason
        )

    if settings.is_production and not settings.trust_enforcement:
        errors.append("Production trust enforcement is disabled.")
    if settings.is_production and not settings.lease_signing_key_configured:
        errors.append("Production capability lease signing key is not configured.")
    if not settings.github_token_configured and not settings.github_app_configured:
        warnings.append(
            "Neither GitHub token nor GitHub App credentials are configured; "
            "GitHub execution will not be available."
        )
    if not settings.fcm_server_key_configured:
        warnings.append("FCM server key is not configured; push notifications will not be available.")

    active_capability_leases = sum(
        lease.status == CapabilityLeaseStatus.ACTIVE
        and lease.expires_at > now_utc()
        for lease in store.capability_leases.values()
    )
    ready = (
        db_reachable
        and migrations_applied
        and recovery_snapshot_available
        and flight.valid
        and not errors
    )

    return RuntimeReadinessResponse(
        status="ready" if ready else "not_ready",
        profile=settings.profile,
        auth_enabled=settings.auth_enabled,
        trust_enforcement=settings.trust_enforcement,
        lease_signing_key_configured=settings.lease_signing_key_configured,
        db_reachable=db_reachable,
        migrations_applied=migrations_applied,
        expected_migration_ids=expected_migration_ids,
        applied_migration_ids=applied_migration_ids,
        recovery_snapshot_available=recovery_snapshot_available,
        recovery_format_version=RECOVERY_FORMAT_VERSION,
        github_token_configured=settings.github_token_configured,
        github_app_configured=settings.github_app_configured,
        fcm_server_key_configured=settings.fcm_server_key_configured,
        flight_recorder_valid=flight.valid,
        flight_recorder_event_count=flight.event_count,
        active_capability_leases=active_capability_leases,
        errors=errors,
        warnings=warnings,
    )


@router.get("/readiness", response_model=RuntimeReadinessResponse)
def get_runtime_readiness(_: AuthUser = MaintainerDependency) -> RuntimeReadinessResponse:
    return build_runtime_readiness()


@router.get("/external-agent-readiness", response_model=ExternalAgentReadinessResponse)
def get_external_agent_readiness(_: AuthUser = MaintainerDependency) -> ExternalAgentReadinessResponse:
    return build_external_agent_readiness()
