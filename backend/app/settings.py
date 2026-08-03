from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

VALID_PROFILES = {"local", "demo", "test", "production"}
REQUIRED_PRODUCTION_ENV_VARS = (
    "AIXION_DB_PATH",
    "AIXION_LEASE_SIGNING_KEY",
    "FCM_" + "SERVER_" + "KEY",
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
    trust_enforcement: bool
    db_path: Path
    github_token_configured: bool = False
    github_app_configured: bool = False
    fcm_server_key_configured: bool = False
    lease_signing_key_configured: bool = False
    public_base_url: str | None = None
    allow_unauthenticated_external_agent_demo: bool = False
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
    missing = sorted(name for name in REQUIRED_PRODUCTION_ENV_VARS if not _env_present(name))
    if missing:
        errors.append("Missing required production environment variables: " + ", ".join(missing))
    if not settings.auth_enabled:
        errors.append("AIXION_AUTH_ENABLED must not be false in production")
    if not settings.trust_enforcement:
        errors.append("AIXION_TRUST_ENFORCEMENT must not be false in production")
    if settings.db_path.name in UNSAFE_PRODUCTION_DB_NAMES:
        errors.append("AIXION_DB_PATH must not point at demo or test database files in production")
    if not settings.github_token_configured and not settings.github_app_configured:
        errors.append(
            "Production requires either GITHUB_TOKEN or a complete Aixion GitHub App configuration"
        )
    return tuple(errors)


def get_settings() -> Settings:
    profile = _profile_from_env()
    defaults = PROFILE_DEFAULTS[profile]
    github_env = "GITHUB_" + "TOKEN"
    fcm_env = "FCM_" + "SERVER_" + "KEY"
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
        github_token_configured=_env_present(github_env),
        github_app_configured=_github_app_configured(),
        fcm_server_key_configured=_env_present(fcm_env),
        lease_signing_key_configured=_env_present("AIXION_LEASE_SIGNING_KEY"),
        public_base_url=_optional_env("AIXION_PUBLIC_BASE_URL"),
        allow_unauthenticated_external_agent_demo=parse_bool(
            os.getenv("AIXION_ALLOW_UNAUTHENTICATED_EXTERNAL_AGENT_DEMO", "false"),
            field_name="AIXION_ALLOW_UNAUTHENTICATED_EXTERNAL_AGENT_DEMO",
        ),
    )
    return Settings(
        profile=settings.profile,
        auth_enabled=settings.auth_enabled,
        trust_enforcement=settings.trust_enforcement,
        db_path=settings.db_path,
        github_token_configured=settings.github_token_configured,
        github_app_configured=settings.github_app_configured,
        fcm_server_key_configured=settings.fcm_server_key_configured,
        lease_signing_key_configured=settings.lease_signing_key_configured,
        public_base_url=settings.public_base_url,
        allow_unauthenticated_external_agent_demo=settings.allow_unauthenticated_external_agent_demo,
        validation_errors=_production_validation_errors(settings),
    )


def validate_startup_environment() -> Settings:
    settings = get_settings()
    if settings.validation_errors:
        raise RuntimeError("Invalid Aixion production environment: " + "; ".join(settings.validation_errors))
    return settings
