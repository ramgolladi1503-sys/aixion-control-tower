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
    database_backend: Literal["sqlite", "postgresql", "mixed"]
    application_store_mode: Literal[
        "sqlite_local", "postgres_probe_only", "postgres_auth_runtime"
    ]
    application_store_ready: bool
    approval_recovery_status: Literal[
        "not_started", "starting", "healthy", "degraded", "failed"
    ] = "not_started"
    approval_recovery_failure_count: int = 0
    approval_recovery_last_completed_at: datetime | None = None
    approval_recovery_poll_seconds: int = 60
    approval_expiry_status: Literal[
        "not_started", "starting", "healthy", "failed"
    ] = "not_started"
    approval_expiry_failure_count: int = 0
    approval_expiry_last_completed_at: datetime | None = None
    approval_expiry_last_changed_count: int = 0
    approval_expiry_task_present: bool = False
    approval_expiry_task_running: bool = False
    approval_expiry_last_completion_fresh: bool = False
    approval_expiry_max_completion_age_seconds: int = 180
    app_state_repository_bindings: dict[
        str, Literal["postgresql", "sqlite", "unbound", "other", "unavailable"]
    ] = Field(default_factory=dict)
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


_APP_STATE_REPOSITORIES = {
    "approval_decisions": ("approval_decision_repository", "approval_decisions"),
    "agent_tasks": ("agent_task_repository", "agent_tasks"),
    "agent_runs": ("agent_run_repository", "agent_runs"),
    "projects": ("project_repository", "projects"),
    "workspace_membership": ("workspace_membership_repository", "workspace_membership"),
    "external_agent_auth": ("external_agent_auth_repository", "external_agent_auth"),
    "work_orders": ("work_order_repository", "work_orders"),
    "ideas": ("idea_repository", "ideas"),
    "audit_events": ("audit_event_repository", "audit_events"),
    "test_runs": ("test_run_repository", "test_runs"),
    "connectors": ("connector_repository", "connectors"),
    "notification_outbox": ("notification_outbox_repository", "notification_outbox"),
    "mcp_child_servers": ("mcp_child_server_repository", "mcp_child_servers"),
    "mcp_pending_requests": ("mcp_pending_repository", "mcp_pending_requests"),
}


def _is_sqlite_repository(repository: object) -> bool:
    """Recognize SQLite adapters and their test/instrumentation subclasses."""
    return any(
        base.__name__.startswith("SQLite")
        for base in type(repository).__mro__
    )


def _application_repository_bindings(
    *, application_state: object | None, postgres_runtime: PostgresRuntime | None
) -> dict[str, Literal["postgresql", "sqlite", "unbound", "other", "unavailable"]]:
    """Report app-state repository identity without claiming whole-app readiness."""
    if application_state is None:
        return {name: "unavailable" for name in _APP_STATE_REPOSITORIES}

    postgres_repositories = getattr(postgres_runtime, "repositories", None)
    result: dict[str, Literal["postgresql", "sqlite", "unbound", "other", "unavailable"]] = {}
    for name, (state_attribute, postgres_attribute) in _APP_STATE_REPOSITORIES.items():
        bound = getattr(application_state, state_attribute, None)
        if bound is None:
            result[name] = "unbound"
            continue
        expected = getattr(postgres_repositories, postgres_attribute, None)
        if expected is not None and bound is expected:
            result[name] = "postgresql"
        elif _is_sqlite_repository(bound):
            result[name] = "sqlite"
        else:
            result[name] = "other"
    return result


def build_runtime_readiness(
    *,
    postgres_runtime: PostgresRuntime | None = None,
    application_state: object | None = None,
) -> RuntimeReadinessResponse:
    settings = get_settings()
    errors = list(settings.validation_errors)
    warnings: list[str] = []
    recovery_status = getattr(application_state, "approval_recovery_status", "not_started")
    if recovery_status not in {"not_started", "starting", "healthy", "degraded", "failed"}:
        recovery_status = "failed"
    recovery_failure_count = max(
        0, int(getattr(application_state, "approval_recovery_failure_count", 0) or 0)
    )
    recovery_last_completed_at = getattr(
        application_state, "approval_recovery_last_completed_at", None
    )
    expiry_status = getattr(application_state, "approval_expiry_status", "not_started")
    if expiry_status not in {"not_started", "starting", "healthy", "failed"}:
        expiry_status = "failed"
    expiry_failure_count = max(
        0, int(getattr(application_state, "approval_expiry_failure_count", 0) or 0)
    )
    expiry_last_completed_at = getattr(
        application_state, "approval_expiry_last_completed_at", None
    )
    expiry_last_changed_count = max(
        0, int(getattr(application_state, "approval_expiry_last_changed_count", 0) or 0)
    )
    try:
        expiry_poll_seconds = max(
            1, int(getattr(settings, "approval_recovery_poll_seconds", 60))
        )
    except (TypeError, ValueError):
        expiry_poll_seconds = 60
    expiry_max_completion_age_seconds = expiry_poll_seconds * 3
    expiry_task = getattr(application_state, "approval_expiry_task", None)
    expiry_task_present = expiry_task is not None
    try:
        expiry_task_running = expiry_task_present and not expiry_task.done()
    except Exception:
        # Readiness must fail closed when task state cannot be inspected.
        expiry_task_running = False
    expiry_last_completion_fresh = False
    if isinstance(expiry_last_completed_at, datetime):
        try:
            if (
                expiry_last_completed_at.tzinfo is not None
                and expiry_last_completed_at.utcoffset() is not None
            ):
                current_time = now_utc()
                completed_at_utc = expiry_last_completed_at.astimezone(
                    current_time.tzinfo
                )
                age_seconds = (current_time - completed_at_utc).total_seconds()
                expiry_last_completion_fresh = (
                    age_seconds >= 0
                    and age_seconds <= expiry_max_completion_age_seconds
                )
        except (OverflowError, ValueError):
            expiry_last_completion_fresh = False
    if settings.is_production and recovery_status != "healthy":
        errors.append(
            "Approval recovery is not healthy: status=" + recovery_status
        )
    if settings.is_production:
        if expiry_status != "healthy":
            errors.append("Approval expiry worker is not healthy: status=" + expiry_status)
        if not expiry_task_present or not expiry_task_running:
            errors.append("Approval expiry worker task is not running")
        if not expiry_last_completion_fresh:
            errors.append("Approval expiry worker completion is stale or unavailable")
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

    if settings.postgres_runtime_probe_enabled or settings.postgres_auth_enabled:
        postgres_probe_status = "not_ready"
        if postgres_runtime is None:
            errors.append(
                "PostgreSQL runtime is unavailable while auth routing is selected or the probe is enabled."
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
    # Auth mode selects the lifespan-owned PostgreSQL bundle for many routes,
    # but process-global SQLite writers remain. Report that mixed authority
    # explicitly and never call it application-ready.
    if settings.postgres_auth_enabled:
        application_store_mode = "postgres_auth_runtime"
        database_backend = "mixed"
        application_store_ready = False
        errors.append(
            "Application persistence is mixed: PostgreSQL auth routing is active, but "
            "SQLite-backed authorities remain and the complete cutover is not ready."
        )
    elif postgres_runtime is not None:
        application_store_mode = "postgres_probe_only"
        database_backend = "sqlite"
        application_store_ready = False
    else:
        application_store_mode = "sqlite_local"
        database_backend = "sqlite"
        application_store_ready = not settings.is_production
    application_repository_bindings = _application_repository_bindings(
        application_state=application_state,
        postgres_runtime=postgres_runtime,
    )
    if settings.is_production:
        errors.append(
            "Application persistence is not production-ready: authoritative routes and workers "
            "are not bound to one complete PostgreSQL repository runtime."
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
        database_backend=database_backend,
        application_store_mode=application_store_mode,
        application_store_ready=application_store_ready,
        approval_recovery_status=recovery_status,
        approval_recovery_failure_count=recovery_failure_count,
        approval_recovery_last_completed_at=recovery_last_completed_at,
        approval_recovery_poll_seconds=getattr(
            settings, "approval_recovery_poll_seconds", 60
        ),
        approval_expiry_status=expiry_status,
        approval_expiry_failure_count=expiry_failure_count,
        approval_expiry_last_completed_at=expiry_last_completed_at,
        approval_expiry_last_changed_count=expiry_last_changed_count,
        approval_expiry_task_present=expiry_task_present,
        approval_expiry_task_running=expiry_task_running,
        approval_expiry_last_completion_fresh=expiry_last_completion_fresh,
        approval_expiry_max_completion_age_seconds=expiry_max_completion_age_seconds,
        app_state_repository_bindings=application_repository_bindings,
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
    return build_runtime_readiness(
        postgres_runtime=runtime,
        application_state=request.app.state,
    )


@router.get("/external-agent-readiness", response_model=ExternalAgentReadinessResponse)
def get_external_agent_readiness(
    _: AuthUser = MaintainerDependency,
) -> ExternalAgentReadinessResponse:
    return build_external_agent_readiness()
