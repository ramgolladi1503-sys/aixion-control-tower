from __future__ import annotations

import pytest

from app.settings import get_settings, parse_bool


def test_local_profile_defaults_to_auth_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIXION_PROFILE", raising=False)
    monkeypatch.delenv("AIXION_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("AIXION_DB_PATH", raising=False)

    settings = get_settings()

    assert settings.profile == "local"
    assert settings.auth_enabled is True
    assert str(settings.db_path) == "runtime/aixion_control_tower.sqlite3"


def test_demo_profile_defaults_to_auth_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIXION_PROFILE", "demo")
    monkeypatch.delenv("AIXION_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("AIXION_DB_PATH", raising=False)

    settings = get_settings()

    assert settings.profile == "demo"
    assert settings.auth_enabled is False
    assert str(settings.db_path) == "runtime/aixion_control_tower_demo.sqlite3"


def test_explicit_env_overrides_profile_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIXION_PROFILE", "demo")
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")
    monkeypatch.setenv("AIXION_DB_PATH", "runtime/custom.sqlite3")

    settings = get_settings()

    assert settings.profile == "demo"
    assert settings.auth_enabled is True
    assert str(settings.db_path) == "runtime/custom.sqlite3"


def test_invalid_profile_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIXION_PROFILE", "stagingish")

    with pytest.raises(ValueError, match="AIXION_PROFILE"):
        get_settings()


def test_invalid_boolean_fails_fast() -> None:
    with pytest.raises(ValueError, match="AIXION_AUTH_ENABLED"):
        parse_bool("maybe", field_name="AIXION_AUTH_ENABLED")
