from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from .auth import require_maintainer
from .database_migrations import known_migration_ids
from .external_agent_readiness import ExternalAgentReadinessResponse, build_external_agent_readiness
from .models import AuthUser, UserRole, now_utc
from .postgres_runtime import PostgresRuntime
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
    database_backend: Literal["sqlite"]
    postgres_probe_enabled: bool
    postgres_auth_rate_limits_enabled: bool
    postgres_probe_status: Literal["disabled", "ready", "not_ready"]
    postgres_probe_schema_ready: bool | None = None
    postgres_pool_size: int = 0
    postgres_pool_available: int = 0
    postgres_pool_waiting: int = 0
    db_reachable: bool
    migrations_applied: bool
    expected_migration_ids: list[str] = Field(default_factory=list)
    applied_migration_ids: list[str] = Field(default_factory=list)
    recovery_snapshot_available: bool
    recovery_format_version: str
    github_token_configured: bool
    github_app_configured: bool
    fcm_server_key_configured: bool
    email_dispatch_enabled: bool
    email_smtp_configured: bool
    email_outbox_key_configured: bool
    auth_rate_limit_key_configured: bool
    metrics_token_configured: bool
    metrics_workspace_configured: bool
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


def build_runtime_readiness(
    *, postgres_runtime: PostgresRuntime | None = None
) -> RuntimeReadinessResponse:
    settings = get_settings()
    errors = list(settings.validation_errors)
    warnings: list[str] = []
    postgres_probe_status: Literal["disabled", "ready", "not_ready"] = "disabled"
    postgres_probe_schema_ready: bool | None = None
    postgres_pool_size = 0
    postgres_pool_available = 0
    postgres_pool_waiting = 0
    expected_migration_ids = sorted(known_migration_ids())
    applied_migration_ids: list[str] = []
    db_reachable = False
    migrations_applied = False
    recovery_snapshot_available = False

    if settings.postgres_runtime_probe_enabled:
        postgres_probe_status = "not_ready"
        if postgres_runtime is None:
            errors.append(
                "PostgreSQL runtime probe is enabled but the runtime is unavailable."
            )
        else:
            try:
                runtime_probe = postgres_runtime.probe()
                postgres_probe_schema_ready = runtime_probe.schema.ready
                postgres_probe_status = "ready" if runtime_probe.ready else "not_ready"
                postgres_pool_size = runtime_probe.pool_size
                postgres_pool_available = runtime_probe.pool_available
                postgres_pool_waiting = runtime_probe.pool_waiting
                if not runtime_probe.ready:
                    errors.append(
                        "PostgreSQL runtime probe reports an incomplete or mismatched schema."
                    )
            except Exception:
                errors.append("PostgreSQL runtime probe failed.")

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
        warnings.append(
            "AIXION_FCM_PROJECT_ID is not configured; push notifications will not be available."
        )
    else:
        warnings.append(
            "FCM project ID is configured, but this readiness probe does not verify ADC "
            "credentials or send permission."
        )

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
        and postgres_probe_status != "not_ready"
        and not errors
    )

    return RuntimeReadinessResponse(
        status="ready" if ready else "not_ready",
        profile=settings.profile,
        auth_enabled=settings.auth_enabled,
        trust_enforcement=settings.trust_enforcement,
        lease_signing_key_configured=settings.lease_signing_key_configured,
        database_backend="sqlite",
        postgres_probe_enabled=settings.postgres_runtime_probe_enabled,
        postgres_auth_rate_limits_enabled=settings.postgres_auth_rate_limits_enabled,
        postgres_probe_status=postgres_probe_status,
        postgres_probe_schema_ready=postgres_probe_schema_ready,
        postgres_pool_size=postgres_pool_size,
        postgres_pool_available=postgres_pool_available,
        postgres_pool_waiting=postgres_pool_waiting,
        db_reachable=db_reachable,
        migrations_applied=migrations_applied,
        expected_migration_ids=expected_migration_ids,
        applied_migration_ids=applied_migration_ids,
        recovery_snapshot_available=recovery_snapshot_available,
        recovery_format_version=RECOVERY_FORMAT_VERSION,
        github_token_configured=settings.github_token_configured,
        github_app_configured=settings.github_app_configured,
        fcm_server_key_configured=settings.fcm_server_key_configured,
        email_dispatch_enabled=settings.email_dispatch_enabled,
        email_smtp_configured=bool(
            settings.email_smtp_host
            and settings.email_from
            and settings.email_smtp_credentials_configured
        ),
        email_outbox_key_configured=settings.email_outbox_key_configured,
        auth_rate_limit_key_configured=settings.auth_rate_limit_key_configured,
        metrics_token_configured=settings.metrics_token_configured,
        metrics_workspace_configured=settings.metrics_workspace_configured,
        flight_recorder_valid=flight.valid,
        flight_recorder_event_count=flight.event_count,
        active_capability_leases=active_capability_leases,
        errors=errors,
        warnings=warnings,
    )


@router.get("/readiness", response_model=RuntimeReadinessResponse)
def get_runtime_readiness(
    request: Request,
    _: AuthUser = MaintainerDependency,
) -> RuntimeReadinessResponse:
    runtime = getattr(request.app.state, "postgres_runtime", None)
    return build_runtime_readiness(postgres_runtime=runtime)


@router.get("/external-agent-readiness", response_model=ExternalAgentReadinessResponse)
def get_external_agent_readiness(
    _: AuthUser = MaintainerDependency,
) -> ExternalAgentReadinessResponse:
    return build_external_agent_readiness()
