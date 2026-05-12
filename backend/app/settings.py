from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

VALID_PROFILES = {"local", "demo", "test", "production"}
REQUIRED_PRODUCTION_ENV_VARS = (
    "AIXION_DB_PATH",
    "GITHUB_" + "TOKEN",
    "FCM_" + "SERVER_" + "KEY",
)
UNSAFE_PRODUCTION_DB_NAMES = {
    "aixion_control_tower_demo.sqlite3",
    "aixion_control_tower_test.sqlite3",
}

PROFILE_DEFAULTS: dict[str, dict[str, str]] = {
    "local": {"auth_enabled": "true", "db_path": "runtime/aixion_control_tower.sqlite3"},
    "demo": {"auth_enabled": "false", "db_path": "runtime/aixion_control_tower_demo.sqlite3"},
    "test": {"auth_enabled": "false", "db_path": "runtime/aixion_control_tower_test.sqlite3"},
    "production": {"auth_enabled": "true", "db_path": "runtime/aixion_control_tower.sqlite3"},
}


@dataclass(frozen=True)
class Settings:
    profile: str
    auth_enabled: bool
    db_path: Path
    github_token_configured: bool = False
    fcm_server_key_configured: bool = False
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


def _production_validation_errors(settings: Settings) -> tuple[str, ...]:
    if not settings.is_production:
        return ()
    errors: list[str] = []
    missing = sorted(name for name in REQUIRED_PRODUCTION_ENV_VARS if not _env_present(name))
    if missing:
        errors.append("Missing required production environment variables: " + ", ".join(missing))
    if not settings.auth_enabled:
        errors.append("AIXION_AUTH_ENABLED must not be false in production")
    if settings.db_path.name in UNSAFE_PRODUCTION_DB_NAMES:
        errors.append("AIXION_DB_PATH must not point at demo or test database files in production")
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
        db_path=Path(os.getenv("AIXION_DB_PATH", defaults["db_path"])),
        github_token_configured=_env_present(github_env),
        fcm_server_key_configured=_env_present(fcm_env),
    )
    return Settings(
        profile=settings.profile,
        auth_enabled=settings.auth_enabled,
        db_path=settings.db_path,
        github_token_configured=settings.github_token_configured,
        fcm_server_key_configured=settings.fcm_server_key_configured,
        validation_errors=_production_validation_errors(settings),
    )


def validate_startup_environment() -> Settings:
    settings = get_settings()
    if settings.validation_errors:
        raise RuntimeError("Invalid Aixion production environment: " + "; ".join(settings.validation_errors))
    return settings
