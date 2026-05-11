from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

VALID_PROFILES = {"local", "demo", "test", "production"}

PROFILE_DEFAULTS: dict[str, dict[str, str]] = {
    "local": {
        "auth_enabled": "true",
        "db_path": "runtime/aixion_control_tower.sqlite3",
    },
    "demo": {
        "auth_enabled": "false",
        "db_path": "runtime/aixion_control_tower_demo.sqlite3",
    },
    "test": {
        "auth_enabled": "false",
        "db_path": "runtime/aixion_control_tower_test.sqlite3",
    },
    "production": {
        "auth_enabled": "true",
        "db_path": "runtime/aixion_control_tower.sqlite3",
    },
}


@dataclass(frozen=True)
class Settings:
    profile: str
    auth_enabled: bool
    db_path: Path


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


def get_settings() -> Settings:
    """Read runtime settings from profile defaults plus explicit env overrides.

    Explicit environment variables always win over profile defaults. This keeps
    existing scripts compatible while giving local/demo/test/production a single
    documented source of truth.
    """
    profile = _profile_from_env()
    defaults = PROFILE_DEFAULTS[profile]

    auth_enabled_value = os.getenv("AIXION_AUTH_ENABLED", defaults["auth_enabled"])
    db_path_value = os.getenv("AIXION_DB_PATH", defaults["db_path"])

    return Settings(
        profile=profile,
        auth_enabled=parse_bool(auth_enabled_value, field_name="AIXION_AUTH_ENABLED"),
        db_path=Path(db_path_value),
    )
