from __future__ import annotations

import pytest

from app.settings import get_settings, parse_bool, validate_startup_environment


def _clear_github_app(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "AIXION_GITHUB_APP_ID",
        "AIXION_GITHUB_APP_INSTALLATION_ID",
        "AIXION_GITHUB_APP_PRIVATE_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def test_local_profile_defaults_to_auth_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIXION_PROFILE", raising=False)
    monkeypatch.delenv("AIXION_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("AIXION_TRUST_ENFORCEMENT", raising=False)
    monkeypatch.delenv("AIXION_DB_PATH", raising=False)

    settings = get_settings()

    assert settings.profile == "local"
    assert settings.auth_enabled is True
    assert settings.trust_enforcement is False
    assert str(settings.db_path) == "runtime/aixion_control_tower.sqlite3"
    assert settings.validation_errors == ()


def test_demo_profile_defaults_to_auth_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIXION_PROFILE", "demo")
    monkeypatch.delenv("AIXION_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("AIXION_TRUST_ENFORCEMENT", raising=False)
    monkeypatch.delenv("AIXION_DB_PATH", raising=False)

    settings = get_settings()

    assert settings.profile == "demo"
    assert settings.auth_enabled is False
    assert settings.trust_enforcement is False
    assert str(settings.db_path) == "runtime/aixion_control_tower_demo.sqlite3"
    assert settings.validation_errors == ()


def test_explicit_env_overrides_profile_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIXION_PROFILE", "demo")
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")
    monkeypatch.setenv("AIXION_TRUST_ENFORCEMENT", "true")
    monkeypatch.setenv("AIXION_DB_PATH", "runtime/custom.sqlite3")

    settings = get_settings()

    assert settings.profile == "demo"
    assert settings.auth_enabled is True
    assert settings.trust_enforcement is True
    assert str(settings.db_path) == "runtime/custom.sqlite3"


def test_invalid_profile_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIXION_PROFILE", "stagingish")

    with pytest.raises(ValueError, match="AIXION_PROFILE"):
        get_settings()


def test_invalid_boolean_fails_fast() -> None:
    with pytest.raises(ValueError, match="AIXION_AUTH_ENABLED"):
        parse_bool("maybe", field_name="AIXION_AUTH_ENABLED")


def test_production_requires_explicit_operational_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIXION_PROFILE", "production")
    monkeypatch.delenv("AIXION_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("AIXION_TRUST_ENFORCEMENT", raising=False)
    monkeypatch.delenv("AIXION_DB_PATH", raising=False)
    monkeypatch.delenv("AIXION_LEASE_SIGNING_KEY", raising=False)
    monkeypatch.delenv("GITHUB_" + "TOKEN", raising=False)
    monkeypatch.delenv("FCM_" + "SERVER_" + "KEY", raising=False)
    _clear_github_app(monkeypatch)

    settings = get_settings()

    assert settings.profile == "production"
    assert settings.auth_enabled is True
    assert settings.trust_enforcement is True
    assert settings.github_token_configured is False
    assert settings.github_app_configured is False
    assert settings.fcm_server_key_configured is False
    assert settings.lease_signing_key_configured is False
    joined = " ".join(settings.validation_errors)
    assert "AIXION_DB_PATH" in joined
    assert "AIXION_LEASE_SIGNING_KEY" in joined
    assert "FCM_" + "SERVER_" + "KEY" in joined
    assert "GITHUB_TOKEN" in joined


def test_production_rejects_disabled_auth_and_trust(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIXION_PROFILE", "production")
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "false")
    monkeypatch.setenv("AIXION_TRUST_ENFORCEMENT", "false")
    monkeypatch.setenv("AIXION_DB_PATH", "runtime/prod.sqlite3")
    monkeypatch.setenv("AIXION_LEASE_SIGNING_KEY", "configured")
    monkeypatch.setenv("GITHUB_" + "TOKEN", "configured")
    monkeypatch.setenv("FCM_" + "SERVER_" + "KEY", "configured")

    settings = get_settings()

    assert "AIXION_AUTH_ENABLED must not be false in production" in settings.validation_errors
    assert "AIXION_TRUST_ENFORCEMENT must not be false in production" in settings.validation_errors


def test_production_rejects_demo_or_test_database_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIXION_PROFILE", "production")
    monkeypatch.delenv("AIXION_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("AIXION_TRUST_ENFORCEMENT", raising=False)
    monkeypatch.setenv("AIXION_DB_PATH", "runtime/aixion_control_tower_demo.sqlite3")
    monkeypatch.setenv("AIXION_LEASE_SIGNING_KEY", "configured")
    monkeypatch.setenv("GITHUB_" + "TOKEN", "configured")
    monkeypatch.setenv("FCM_" + "SERVER_" + "KEY", "configured")

    settings = get_settings()

    assert (
        "AIXION_DB_PATH must not point at demo or test database files in production"
        in settings.validation_errors
    )


def test_valid_production_environment_passes_startup_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIXION_PROFILE", "production")
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")
    monkeypatch.setenv("AIXION_TRUST_ENFORCEMENT", "true")
    monkeypatch.setenv("AIXION_DB_PATH", "runtime/prod.sqlite3")
    monkeypatch.setenv("AIXION_LEASE_SIGNING_KEY", "configured")
    monkeypatch.setenv("GITHUB_" + "TOKEN", "configured")
    monkeypatch.setenv("FCM_" + "SERVER_" + "KEY", "configured")

    settings = validate_startup_environment()

    assert settings.profile == "production"
    assert settings.validation_errors == ()


def test_complete_github_app_configuration_can_replace_static_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIXION_PROFILE", "production")
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")
    monkeypatch.setenv("AIXION_TRUST_ENFORCEMENT", "true")
    monkeypatch.setenv("AIXION_DB_PATH", "runtime/prod.sqlite3")
    monkeypatch.setenv("AIXION_LEASE_SIGNING_KEY", "configured")
    monkeypatch.delenv("GITHUB_" + "TOKEN", raising=False)
    monkeypatch.setenv("AIXION_GITHUB_APP_ID", "123")
    monkeypatch.setenv("AIXION_GITHUB_APP_INSTALLATION_ID", "456")
    monkeypatch.setenv("AIXION_GITHUB_APP_PRIVATE_KEY", "private-key")
    monkeypatch.setenv("FCM_" + "SERVER_" + "KEY", "configured")

    settings = validate_startup_environment()

    assert settings.github_token_configured is False
    assert settings.github_app_configured is True
    assert settings.validation_errors == ()


def test_invalid_production_environment_fails_startup_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIXION_PROFILE", "production")
    monkeypatch.delenv("AIXION_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("AIXION_TRUST_ENFORCEMENT", raising=False)
    monkeypatch.delenv("AIXION_DB_PATH", raising=False)
    monkeypatch.delenv("AIXION_LEASE_SIGNING_KEY", raising=False)
    monkeypatch.delenv("GITHUB_" + "TOKEN", raising=False)
    monkeypatch.delenv("FCM_" + "SERVER_" + "KEY", raising=False)
    _clear_github_app(monkeypatch)

    with pytest.raises(RuntimeError, match="Invalid Aixion production environment"):
        validate_startup_environment()
