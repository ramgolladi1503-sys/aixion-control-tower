from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

VALID_PROFILES = {"local", "demo", "test", "production"}
OUTBOX_DISPATCH_MODES = {"inline", "scheduled"}
REQUIRED_PRODUCTION_ENV_VARS = (
    "AIXION_POSTGRES_DSN",
    "AIXION_LEASE_SIGNING_KEY",
    "AIXION_FCM_PROJECT_ID",
    "AIXION_AUTH_RATE_LIMIT_HMAC_KEY",
    "AIXION_METRICS_TOKEN",
    "AIXION_METRICS_WORKSPACE_ID",
)
UNSAFE_PRODUCTION_DB_NAMES = {
    "aixion_control_tower_demo.sqlite3",
    "aixion_control_tower_test.sqlite3",
}

PROFILE_DEFAULTS: dict[str, dict[str, str]] = {
    "local": {
        "auth_enabled": "true",
        "trust_enforcement": "false",
        "db_path": "runtime/aixion_control_tower.sqlite3",
    },
    "demo": {
        "auth_enabled": "false",
        "trust_enforcement": "false",
        "db_path": "runtime/aixion_control_tower_demo.sqlite3",
    },
    "test": {
        "auth_enabled": "false",
        "trust_enforcement": "false",
        "db_path": "runtime/aixion_control_tower_test.sqlite3",
    },
    "production": {
        "auth_enabled": "true",
        "trust_enforcement": "true",
        "db_path": "runtime/aixion_control_tower.sqlite3",
    },
}


@dataclass(frozen=True)
class Settings:
    profile: str
    auth_enabled: bool
    db_path: Path
    postgres_dsn_configured: bool = False
    postgres_connect_timeout_seconds: int = 5
    postgres_statement_timeout_ms: int = 15000
    postgres_lock_timeout_ms: int = 3000
    postgres_idle_transaction_timeout_ms: int = 15000
    postgres_pool_min_size: int = 2
    postgres_pool_max_size: int = 12
    postgres_pool_wait_timeout_seconds: int = 5
    postgres_pool_max_waiting: int = 20
    postgres_runtime_probe_enabled: bool = False
    postgres_auth_rate_limits_enabled: bool = False
    postgres_auth_enabled: bool = False
    approval_recovery_poll_seconds: int = 60
    trust_enforcement: bool = False
    github_token_configured: bool = False
    github_app_configured: bool = False
    fcm_server_key_configured: bool = False
    fcm_project_id: str | None = None
    lease_signing_key_configured: bool = False
    public_base_url: str | None = None
    allow_unauthenticated_external_agent_demo: bool = False
    agent_approval_ttl_seconds: int = 600
    external_agent_token_ttl_seconds: int = 2_592_000
    external_agent_invocation_lease_ttl_seconds: int = 60
    outbox_dispatch_mode: str = "inline"
    push_dispatch_enabled: bool = False
    push_dispatch_poll_seconds: int = 5
    push_max_attempts: int = 5
    push_retry_base_seconds: int = 15
    push_retry_max_seconds: int = 300
    push_lease_seconds: int = 60
    email_dispatch_enabled: bool = False
    email_dispatch_poll_seconds: int = 5
    email_max_attempts: int = 5
    email_retry_base_seconds: int = 15
    email_retry_max_seconds: int = 300
    email_lease_seconds: int = 60
    email_smtp_host: str | None = None
    email_smtp_port: int = 587
    email_smtp_timeout_seconds: int = 10
    email_from: str | None = None
    email_smtp_credentials_configured: bool = False
    email_outbox_key_configured: bool = False
    auth_rate_limit_key_configured: bool = False
    auth_rate_limit_window_seconds: int = 60
    auth_register_limit_per_ip: int = 10
    auth_resend_limit_per_ip: int = 10
    auth_resend_limit_per_email: int = 1
    auth_login_limit_per_ip: int = 30
    auth_verify_limit_per_ip: int = 30
    auth_verify_limit_per_email: int = 5
    metrics_token: str = field(default="", repr=False, compare=False)
    metrics_workspace_id: str | None = None
    metrics_token_configured: bool = False
    metrics_workspace_configured: bool = False
    validation_errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_production(self) -> bool:
        return self.profile == "production"


def parse_bool(value: str, *, field_name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"{field_name} must be a boolean value, got {value!r}")


def parse_bounded_int(value: str, *, field_name: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value.strip())
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{field_name} must be between {minimum} and {maximum}")
    return parsed


def parse_outbox_dispatch_mode(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in OUTBOX_DISPATCH_MODES:
        allowed = ", ".join(sorted(OUTBOX_DISPATCH_MODES))
        raise ValueError(f"AIXION_OUTBOX_DISPATCH_MODE must be one of: {allowed}")
    return normalized


def _profile_from_env() -> str:
    profile = os.getenv("AIXION_PROFILE", "local").strip().lower()
    if profile not in VALID_PROFILES:
        valid = ", ".join(sorted(VALID_PROFILES))
        raise ValueError(f"AIXION_PROFILE must be one of: {valid}")
    return profile


def _env_present(name: str) -> bool:
    return bool(os.getenv(name, "").strip())


def _optional_env(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def _fcm_project_id_from_env() -> str | None:
    project_id = _optional_env("AIXION_FCM_PROJECT_ID")
    if project_id is None:
        return None
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project_id):
        raise ValueError(
            "AIXION_FCM_PROJECT_ID must be a 6-30 character Firebase project ID "
            "using lowercase letters, digits, or hyphens"
        )
    return project_id


def _github_app_configured() -> bool:
    required = (
        "AIXION_GITHUB_APP_ID",
        "AIXION_GITHUB_APP_INSTALLATION_ID",
        "AIXION_GITHUB_APP_PRIVATE_KEY",
    )
    return all(_env_present(name) for name in required)


def _production_validation_errors(settings: Settings) -> tuple[str, ...]:
    if not settings.is_production:
        return ()
    errors: list[str] = []
    errors.append(
        "Production profile is disabled: the opt-in PostgreSQL runtime is not yet the complete "
        "application persistence authority; finish the PostgreSQL cutover and operational gates "
        "before production startup."
    )
    if not settings.postgres_auth_enabled:
        errors.append(
            "AIXION_POSTGRES_AUTH_ENABLED must be true in production to select the "
            "lifespan-bound PostgreSQL application runtime"
        )
    if not settings.postgres_runtime_probe_enabled:
        errors.append(
            "AIXION_POSTGRES_RUNTIME_PROBE_ENABLED must be true in production"
        )
    if not settings.postgres_auth_rate_limits_enabled:
        errors.append(
            "AIXION_POSTGRES_AUTH_RATE_LIMITS_ENABLED must be true in production"
        )
    missing = sorted(name for name in REQUIRED_PRODUCTION_ENV_VARS if not _env_present(name))
    if missing:
        errors.append("Missing required production environment variables: " + ", ".join(missing))
    if not settings.auth_enabled:
        errors.append("AIXION_AUTH_ENABLED must not be false in production")
    if not settings.trust_enforcement:
        errors.append("AIXION_TRUST_ENFORCEMENT must not be false in production")
    if settings.outbox_dispatch_mode != "scheduled":
        errors.append("AIXION_OUTBOX_DISPATCH_MODE must be scheduled in production")
    if _env_present("AIXION_DB_PATH"):
        errors.append("AIXION_DB_PATH is SQLite-only and must not be configured in production")
    if settings.db_path.name in UNSAFE_PRODUCTION_DB_NAMES:
        errors.append("AIXION_DB_PATH must not point at demo or test database files in production")
    if not settings.github_token_configured and not settings.github_app_configured:
        errors.append(
            "Production requires either GITHUB_TOKEN or a complete Aixion GitHub App configuration"
        )
    if not settings.email_dispatch_enabled:
        errors.append("AIXION_EMAIL_DISPATCH_ENABLED must be true for production verification email")
    if not settings.push_dispatch_enabled:
        errors.append("AIXION_PUSH_DISPATCH_ENABLED must be true for production approval notifications")
    if not settings.email_smtp_host or not settings.email_from:
        errors.append("Production verification email requires AIXION_EMAIL_SMTP_HOST and AIXION_EMAIL_FROM")
    if not settings.email_smtp_credentials_configured:
        errors.append("Production verification email requires SMTP username and password")
    if not settings.email_outbox_key_configured:
        errors.append("Production verification email requires AIXION_EMAIL_OUTBOX_KEY")
    return tuple(errors)


def get_settings() -> Settings:
    profile = _profile_from_env()
    defaults = PROFILE_DEFAULTS[profile]
    github_env = "GITHUB_" + "TOKEN"
    fcm_project_id = _fcm_project_id_from_env()
    metrics_token_raw = os.getenv("AIXION_METRICS_TOKEN", "")
    metrics_token = metrics_token_raw.strip()
    metrics_workspace_id = _optional_env("AIXION_METRICS_WORKSPACE_ID")
    if metrics_token_raw and not metrics_token:
        raise ValueError("AIXION_METRICS_TOKEN must not be blank")
    if metrics_token and len(metrics_token) < 32:
        raise ValueError("AIXION_METRICS_TOKEN must contain at least 32 characters")
    if bool(metrics_token) != bool(metrics_workspace_id):
        raise ValueError(
            "AIXION_METRICS_TOKEN and AIXION_METRICS_WORKSPACE_ID must be configured together"
        )
    if metrics_workspace_id and len(metrics_workspace_id) > 128:
        raise ValueError("AIXION_METRICS_WORKSPACE_ID must be at most 128 characters")
    settings = Settings(
        profile=profile,
        auth_enabled=parse_bool(
            os.getenv("AIXION_AUTH_ENABLED", defaults["auth_enabled"]),
            field_name="AIXION_AUTH_ENABLED",
        ),
        trust_enforcement=parse_bool(
            os.getenv("AIXION_TRUST_ENFORCEMENT", defaults["trust_enforcement"]),
            field_name="AIXION_TRUST_ENFORCEMENT",
        ),
        db_path=Path(os.getenv("AIXION_DB_PATH", defaults["db_path"])),
        postgres_dsn_configured=_env_present("AIXION_POSTGRES_DSN"),
        postgres_connect_timeout_seconds=parse_bounded_int(
            os.getenv("AIXION_POSTGRES_CONNECT_TIMEOUT_SECONDS", "5"),
            field_name="AIXION_POSTGRES_CONNECT_TIMEOUT_SECONDS",
            minimum=1,
            maximum=30,
        ),
        postgres_statement_timeout_ms=parse_bounded_int(
            os.getenv("AIXION_POSTGRES_STATEMENT_TIMEOUT_MS", "15000"),
            field_name="AIXION_POSTGRES_STATEMENT_TIMEOUT_MS",
            minimum=100,
            maximum=120000,
        ),
        postgres_lock_timeout_ms=parse_bounded_int(
            os.getenv("AIXION_POSTGRES_LOCK_TIMEOUT_MS", "3000"),
            field_name="AIXION_POSTGRES_LOCK_TIMEOUT_MS",
            minimum=100,
            maximum=30000,
        ),
        postgres_idle_transaction_timeout_ms=parse_bounded_int(
            os.getenv("AIXION_POSTGRES_IDLE_TRANSACTION_TIMEOUT_MS", "15000"),
            field_name="AIXION_POSTGRES_IDLE_TRANSACTION_TIMEOUT_MS",
            minimum=100,
            maximum=120000,
        ),
        postgres_pool_min_size=parse_bounded_int(
            os.getenv("AIXION_POSTGRES_POOL_MIN_SIZE", "2"),
            field_name="AIXION_POSTGRES_POOL_MIN_SIZE",
            minimum=0,
            maximum=32,
        ),
        postgres_pool_max_size=parse_bounded_int(
            os.getenv("AIXION_POSTGRES_POOL_MAX_SIZE", "12"),
            field_name="AIXION_POSTGRES_POOL_MAX_SIZE",
            minimum=1,
            maximum=64,
        ),
        postgres_pool_wait_timeout_seconds=parse_bounded_int(
            os.getenv("AIXION_POSTGRES_POOL_WAIT_TIMEOUT_SECONDS", "5"),
            field_name="AIXION_POSTGRES_POOL_WAIT_TIMEOUT_SECONDS",
            minimum=1,
            maximum=60,
        ),
        postgres_pool_max_waiting=parse_bounded_int(
            os.getenv("AIXION_POSTGRES_POOL_MAX_WAITING", "20"),
            field_name="AIXION_POSTGRES_POOL_MAX_WAITING",
            minimum=0,
            maximum=1000,
        ),
        postgres_runtime_probe_enabled=parse_bool(
            os.getenv("AIXION_POSTGRES_RUNTIME_PROBE_ENABLED", "false"),
            field_name="AIXION_POSTGRES_RUNTIME_PROBE_ENABLED",
        ),
        postgres_auth_rate_limits_enabled=parse_bool(
            os.getenv("AIXION_POSTGRES_AUTH_RATE_LIMITS_ENABLED", "false"),
            field_name="AIXION_POSTGRES_AUTH_RATE_LIMITS_ENABLED",
        ),
        postgres_auth_enabled=parse_bool(
            os.getenv("AIXION_POSTGRES_AUTH_ENABLED", "false"),
            field_name="AIXION_POSTGRES_AUTH_ENABLED",
        ),
        approval_recovery_poll_seconds=parse_bounded_int(
            os.getenv("AIXION_APPROVAL_RECOVERY_POLL_SECONDS", "60"),
            field_name="AIXION_APPROVAL_RECOVERY_POLL_SECONDS",
            minimum=1,
            maximum=300,
        ),
        github_token_configured=_env_present(github_env),
        github_app_configured=_github_app_configured(),
        # Keep the response/config field name for existing Android clients.
        fcm_server_key_configured=bool(fcm_project_id),
        fcm_project_id=fcm_project_id,
        lease_signing_key_configured=_env_present("AIXION_LEASE_SIGNING_KEY"),
        public_base_url=_optional_env("AIXION_PUBLIC_BASE_URL"),
        allow_unauthenticated_external_agent_demo=parse_bool(
            os.getenv("AIXION_ALLOW_UNAUTHENTICATED_EXTERNAL_AGENT_DEMO", "false"),
            field_name="AIXION_ALLOW_UNAUTHENTICATED_EXTERNAL_AGENT_DEMO",
        ),
        agent_approval_ttl_seconds=parse_bounded_int(
            os.getenv("AIXION_AGENT_APPROVAL_TTL_SECONDS", "600"),
            field_name="AIXION_AGENT_APPROVAL_TTL_SECONDS",
            minimum=30,
            maximum=900,
        ),
        external_agent_token_ttl_seconds=parse_bounded_int(
            os.getenv("AIXION_EXTERNAL_AGENT_TOKEN_TTL_SECONDS", "2592000"),
            field_name="AIXION_EXTERNAL_AGENT_TOKEN_TTL_SECONDS",
            minimum=300,
            maximum=2_592_000,
        ),
        external_agent_invocation_lease_ttl_seconds=parse_bounded_int(
            os.getenv("AIXION_EXTERNAL_AGENT_INVOCATION_LEASE_TTL_SECONDS", "60"),
            field_name="AIXION_EXTERNAL_AGENT_INVOCATION_LEASE_TTL_SECONDS",
            minimum=30,
            maximum=300,
        ),
        outbox_dispatch_mode=parse_outbox_dispatch_mode(
            os.getenv("AIXION_OUTBOX_DISPATCH_MODE", "inline")
        ),
        push_dispatch_enabled=parse_bool(
            os.getenv("AIXION_PUSH_DISPATCH_ENABLED", "false"),
            field_name="AIXION_PUSH_DISPATCH_ENABLED",
        ),
        push_dispatch_poll_seconds=parse_bounded_int(
            os.getenv("AIXION_PUSH_DISPATCH_POLL_SECONDS", "5"),
            field_name="AIXION_PUSH_DISPATCH_POLL_SECONDS",
            minimum=1,
            maximum=60,
        ),
        push_max_attempts=parse_bounded_int(
            os.getenv("AIXION_PUSH_MAX_ATTEMPTS", "5"),
            field_name="AIXION_PUSH_MAX_ATTEMPTS",
            minimum=1,
            maximum=10,
        ),
        push_retry_base_seconds=parse_bounded_int(
            os.getenv("AIXION_PUSH_RETRY_BASE_SECONDS", "15"),
            field_name="AIXION_PUSH_RETRY_BASE_SECONDS",
            minimum=1,
            maximum=300,
        ),
        push_retry_max_seconds=parse_bounded_int(
            os.getenv("AIXION_PUSH_RETRY_MAX_SECONDS", "300"),
            field_name="AIXION_PUSH_RETRY_MAX_SECONDS",
            minimum=1,
            maximum=3600,
        ),
        push_lease_seconds=parse_bounded_int(
            os.getenv("AIXION_PUSH_LEASE_SECONDS", "60"),
            field_name="AIXION_PUSH_LEASE_SECONDS",
            minimum=30,
            maximum=600,
        ),
        email_dispatch_enabled=parse_bool(
            os.getenv("AIXION_EMAIL_DISPATCH_ENABLED", "false"),
            field_name="AIXION_EMAIL_DISPATCH_ENABLED",
        ),
        email_dispatch_poll_seconds=parse_bounded_int(
            os.getenv("AIXION_EMAIL_DISPATCH_POLL_SECONDS", "5"),
            field_name="AIXION_EMAIL_DISPATCH_POLL_SECONDS",
            minimum=1,
            maximum=60,
        ),
        email_max_attempts=parse_bounded_int(
            os.getenv("AIXION_EMAIL_MAX_ATTEMPTS", "5"),
            field_name="AIXION_EMAIL_MAX_ATTEMPTS",
            minimum=1,
            maximum=10,
        ),
        email_retry_base_seconds=parse_bounded_int(
            os.getenv("AIXION_EMAIL_RETRY_BASE_SECONDS", "15"),
            field_name="AIXION_EMAIL_RETRY_BASE_SECONDS",
            minimum=1,
            maximum=300,
        ),
        email_retry_max_seconds=parse_bounded_int(
            os.getenv("AIXION_EMAIL_RETRY_MAX_SECONDS", "300"),
            field_name="AIXION_EMAIL_RETRY_MAX_SECONDS",
            minimum=1,
            maximum=3600,
        ),
        email_lease_seconds=parse_bounded_int(
            os.getenv("AIXION_EMAIL_LEASE_SECONDS", "60"),
            field_name="AIXION_EMAIL_LEASE_SECONDS",
            minimum=30,
            maximum=600,
        ),
        email_smtp_host=_optional_env("AIXION_EMAIL_SMTP_HOST"),
        email_smtp_port=parse_bounded_int(
            os.getenv("AIXION_EMAIL_SMTP_PORT", "587"),
            field_name="AIXION_EMAIL_SMTP_PORT",
            minimum=1,
            maximum=65535,
        ),
        email_smtp_timeout_seconds=parse_bounded_int(
            os.getenv("AIXION_EMAIL_SMTP_TIMEOUT_SECONDS", "10"),
            field_name="AIXION_EMAIL_SMTP_TIMEOUT_SECONDS",
            minimum=1,
            maximum=30,
        ),
        email_from=_optional_env("AIXION_EMAIL_FROM"),
        email_smtp_credentials_configured=(
            _env_present("AIXION_EMAIL_SMTP_USERNAME")
            and _env_present("AIXION_EMAIL_SMTP_PASSWORD")
        ),
        email_outbox_key_configured=_env_present("AIXION_EMAIL_OUTBOX_KEY"),
        auth_rate_limit_key_configured=_env_present("AIXION_AUTH_RATE_LIMIT_HMAC_KEY"),
        auth_rate_limit_window_seconds=parse_bounded_int(
            os.getenv("AIXION_AUTH_RATE_LIMIT_WINDOW_SECONDS", "60"),
            field_name="AIXION_AUTH_RATE_LIMIT_WINDOW_SECONDS",
            minimum=10,
            maximum=3600,
        ),
        auth_register_limit_per_ip=parse_bounded_int(
            os.getenv("AIXION_AUTH_REGISTER_LIMIT_PER_IP", "10"),
            field_name="AIXION_AUTH_REGISTER_LIMIT_PER_IP",
            minimum=1,
            maximum=1000,
        ),
        auth_resend_limit_per_ip=parse_bounded_int(
            os.getenv("AIXION_AUTH_RESEND_LIMIT_PER_IP", "10"),
            field_name="AIXION_AUTH_RESEND_LIMIT_PER_IP",
            minimum=1,
            maximum=1000,
        ),
        auth_resend_limit_per_email=parse_bounded_int(
            os.getenv("AIXION_AUTH_RESEND_LIMIT_PER_EMAIL", "1"),
            field_name="AIXION_AUTH_RESEND_LIMIT_PER_EMAIL",
            minimum=1,
            maximum=100,
        ),
        auth_login_limit_per_ip=parse_bounded_int(
            os.getenv("AIXION_AUTH_LOGIN_LIMIT_PER_IP", "30"),
            field_name="AIXION_AUTH_LOGIN_LIMIT_PER_IP",
            minimum=1,
            maximum=1000,
        ),
        auth_verify_limit_per_ip=parse_bounded_int(
            os.getenv("AIXION_AUTH_VERIFY_LIMIT_PER_IP", "30"),
            field_name="AIXION_AUTH_VERIFY_LIMIT_PER_IP",
            minimum=1,
            maximum=1000,
        ),
        auth_verify_limit_per_email=parse_bounded_int(
            os.getenv("AIXION_AUTH_VERIFY_LIMIT_PER_EMAIL", "5"),
            field_name="AIXION_AUTH_VERIFY_LIMIT_PER_EMAIL",
            minimum=1,
            maximum=100,
        ),
        metrics_token=metrics_token,
        metrics_workspace_id=metrics_workspace_id,
        metrics_token_configured=bool(metrics_token),
        metrics_workspace_configured=bool(metrics_workspace_id),
    )
    if settings.push_retry_max_seconds < settings.push_retry_base_seconds:
        raise ValueError(
            "AIXION_PUSH_RETRY_MAX_SECONDS must be greater than or equal to "
            "AIXION_PUSH_RETRY_BASE_SECONDS"
        )
    if settings.email_retry_max_seconds < settings.email_retry_base_seconds:
        raise ValueError(
            "AIXION_EMAIL_RETRY_MAX_SECONDS must be greater than or equal to "
            "AIXION_EMAIL_RETRY_BASE_SECONDS"
        )
    if settings.postgres_pool_min_size > settings.postgres_pool_max_size:
        raise ValueError(
            "AIXION_POSTGRES_POOL_MIN_SIZE must be less than or equal to "
            "AIXION_POSTGRES_POOL_MAX_SIZE"
        )
    if _env_present("AIXION_EMAIL_SMTP_USERNAME") != _env_present("AIXION_EMAIL_SMTP_PASSWORD"):
        raise ValueError("AIXION_EMAIL_SMTP_USERNAME and AIXION_EMAIL_SMTP_PASSWORD must be configured together")
    if _env_present("AIXION_EMAIL_OUTBOX_KEY"):
        encryption_key = os.getenv("AIXION_EMAIL_OUTBOX_KEY", "").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{64}", encryption_key):
            raise ValueError("AIXION_EMAIL_OUTBOX_KEY must be 64 hexadecimal characters")
    if _env_present("AIXION_AUTH_RATE_LIMIT_HMAC_KEY"):
        rate_limit_key = os.getenv("AIXION_AUTH_RATE_LIMIT_HMAC_KEY", "").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{64}", rate_limit_key):
            raise ValueError("AIXION_AUTH_RATE_LIMIT_HMAC_KEY must be 64 hexadecimal characters")
    if settings.email_dispatch_enabled and (
        not settings.email_smtp_host
        or not settings.email_from
        or not settings.email_outbox_key_configured
    ):
        raise ValueError(
            "Email dispatch requires AIXION_EMAIL_SMTP_HOST, AIXION_EMAIL_FROM, "
            "and AIXION_EMAIL_OUTBOX_KEY"
        )
    if settings.postgres_auth_rate_limits_enabled and not settings.postgres_runtime_probe_enabled:
        raise ValueError(
            "AIXION_POSTGRES_AUTH_RATE_LIMITS_ENABLED requires "
            "AIXION_POSTGRES_RUNTIME_PROBE_ENABLED so the PostgreSQL runtime is opened and checked"
        )
    if settings.postgres_auth_enabled:
        if not settings.auth_enabled:
            raise ValueError(
                "AIXION_POSTGRES_AUTH_ENABLED requires AIXION_AUTH_ENABLED=true"
            )
        if not settings.postgres_runtime_probe_enabled:
            raise ValueError(
                "AIXION_POSTGRES_AUTH_ENABLED requires AIXION_POSTGRES_RUNTIME_PROBE_ENABLED"
            )
        if not settings.postgres_auth_rate_limits_enabled:
            raise ValueError(
                "AIXION_POSTGRES_AUTH_ENABLED requires AIXION_POSTGRES_AUTH_RATE_LIMITS_ENABLED"
            )
        if not settings.email_dispatch_enabled:
            raise ValueError(
                "AIXION_POSTGRES_AUTH_ENABLED requires AIXION_EMAIL_DISPATCH_ENABLED"
            )
    return Settings(
        profile=settings.profile,
        auth_enabled=settings.auth_enabled,
        trust_enforcement=settings.trust_enforcement,
        db_path=settings.db_path,
        postgres_dsn_configured=settings.postgres_dsn_configured,
        postgres_connect_timeout_seconds=settings.postgres_connect_timeout_seconds,
        postgres_statement_timeout_ms=settings.postgres_statement_timeout_ms,
        postgres_lock_timeout_ms=settings.postgres_lock_timeout_ms,
        postgres_idle_transaction_timeout_ms=settings.postgres_idle_transaction_timeout_ms,
        postgres_pool_min_size=settings.postgres_pool_min_size,
        postgres_pool_max_size=settings.postgres_pool_max_size,
        postgres_pool_wait_timeout_seconds=settings.postgres_pool_wait_timeout_seconds,
        postgres_pool_max_waiting=settings.postgres_pool_max_waiting,
        postgres_runtime_probe_enabled=settings.postgres_runtime_probe_enabled,
        postgres_auth_rate_limits_enabled=settings.postgres_auth_rate_limits_enabled,
        postgres_auth_enabled=settings.postgres_auth_enabled,
        approval_recovery_poll_seconds=settings.approval_recovery_poll_seconds,
        github_token_configured=settings.github_token_configured,
        github_app_configured=settings.github_app_configured,
        fcm_server_key_configured=settings.fcm_server_key_configured,
        fcm_project_id=settings.fcm_project_id,
        lease_signing_key_configured=settings.lease_signing_key_configured,
        public_base_url=settings.public_base_url,
        allow_unauthenticated_external_agent_demo=settings.allow_unauthenticated_external_agent_demo,
        agent_approval_ttl_seconds=settings.agent_approval_ttl_seconds,
        external_agent_token_ttl_seconds=settings.external_agent_token_ttl_seconds,
        external_agent_invocation_lease_ttl_seconds=settings.external_agent_invocation_lease_ttl_seconds,
        outbox_dispatch_mode=settings.outbox_dispatch_mode,
        push_dispatch_enabled=settings.push_dispatch_enabled,
        push_dispatch_poll_seconds=settings.push_dispatch_poll_seconds,
        push_max_attempts=settings.push_max_attempts,
        push_retry_base_seconds=settings.push_retry_base_seconds,
        push_retry_max_seconds=settings.push_retry_max_seconds,
        push_lease_seconds=settings.push_lease_seconds,
        email_dispatch_enabled=settings.email_dispatch_enabled,
        email_dispatch_poll_seconds=settings.email_dispatch_poll_seconds,
        email_max_attempts=settings.email_max_attempts,
        email_retry_base_seconds=settings.email_retry_base_seconds,
        email_retry_max_seconds=settings.email_retry_max_seconds,
        email_lease_seconds=settings.email_lease_seconds,
        email_smtp_host=settings.email_smtp_host,
        email_smtp_port=settings.email_smtp_port,
        email_smtp_timeout_seconds=settings.email_smtp_timeout_seconds,
        email_from=settings.email_from,
        email_smtp_credentials_configured=settings.email_smtp_credentials_configured,
        email_outbox_key_configured=settings.email_outbox_key_configured,
        auth_rate_limit_key_configured=settings.auth_rate_limit_key_configured,
        auth_rate_limit_window_seconds=settings.auth_rate_limit_window_seconds,
        auth_register_limit_per_ip=settings.auth_register_limit_per_ip,
        auth_resend_limit_per_ip=settings.auth_resend_limit_per_ip,
        auth_resend_limit_per_email=settings.auth_resend_limit_per_email,
        auth_login_limit_per_ip=settings.auth_login_limit_per_ip,
        auth_verify_limit_per_ip=settings.auth_verify_limit_per_ip,
        auth_verify_limit_per_email=settings.auth_verify_limit_per_email,
        metrics_token=settings.metrics_token,
        metrics_workspace_id=settings.metrics_workspace_id,
        metrics_token_configured=settings.metrics_token_configured,
        metrics_workspace_configured=settings.metrics_workspace_configured,
        validation_errors=_production_validation_errors(settings),
    )


def validate_startup_environment() -> Settings:
    settings = get_settings()
    if settings.validation_errors:
        raise RuntimeError("Invalid Aixion production environment: " + "; ".join(settings.validation_errors))
    return settings


def validate_outbox_worker_environment(worker_kind: str) -> Settings:
    """Validate only the scoped production contract for a one-shot outbox job.

    This deliberately does not weaken ``validate_startup_environment``: the API
    must continue to reject production until the complete application store is
    PostgreSQL-authoritative. The isolated push/email jobs may run only against
    PostgreSQL and only with the secrets required by their own dispatch path.
    """
    if worker_kind not in {"push", "email"}:
        raise ValueError("worker_kind must be 'push' or 'email'")

    settings = get_settings()
    errors: list[str] = []
    if not settings.is_production:
        errors.append("AIXION_PROFILE=production is required for one-shot outbox workers")
    if settings.outbox_dispatch_mode != "scheduled":
        errors.append("AIXION_OUTBOX_DISPATCH_MODE=scheduled is required for one-shot outbox workers")
    if _env_present("AIXION_DB_PATH"):
        errors.append("AIXION_DB_PATH is SQLite-only and must not be configured for outbox workers")

    enabled = settings.push_dispatch_enabled if worker_kind == "push" else settings.email_dispatch_enabled
    if enabled:
        if not settings.postgres_dsn_configured:
            errors.append("AIXION_POSTGRES_DSN is required for one-shot outbox workers")
        if worker_kind == "push":
            if not settings.fcm_project_id:
                errors.append("AIXION_FCM_PROJECT_ID is required for push dispatch")
        else:
            if not settings.email_smtp_host or not settings.email_from:
                errors.append("AIXION_EMAIL_SMTP_HOST and AIXION_EMAIL_FROM are required for email dispatch")
            if not settings.email_smtp_credentials_configured:
                errors.append("AIXION_EMAIL_SMTP_USERNAME and AIXION_EMAIL_SMTP_PASSWORD are required for email dispatch")
            if not settings.email_outbox_key_configured:
                errors.append("AIXION_EMAIL_OUTBOX_KEY is required for email dispatch")

    if errors:
        raise RuntimeError("Invalid outbox worker environment: " + "; ".join(errors))
    return settings
