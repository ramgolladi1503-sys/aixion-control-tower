from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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
    monkeypatch.delenv("AIXION_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("AIXION_OUTBOX_DISPATCH_MODE", raising=False)

    settings = get_settings()

    assert settings.profile == "local"
    assert settings.auth_enabled is True
    assert settings.trust_enforcement is False
    assert settings.postgres_dsn_configured is False
    assert settings.postgres_connect_timeout_seconds == 5
    assert settings.postgres_statement_timeout_ms == 15000
    assert settings.postgres_lock_timeout_ms == 3000
    assert settings.postgres_idle_transaction_timeout_ms == 15000
    assert settings.postgres_pool_min_size == 2
    assert settings.postgres_pool_max_size == 12
    assert settings.postgres_pool_wait_timeout_seconds == 5
    assert settings.postgres_pool_max_waiting == 20
    assert settings.postgres_runtime_probe_enabled is False
    assert settings.postgres_auth_rate_limits_enabled is False
    assert settings.postgres_auth_enabled is False
    assert str(settings.db_path) == "runtime/aixion_control_tower.sqlite3"
    assert settings.agent_approval_ttl_seconds == 600
    assert settings.outbox_dispatch_mode == "inline"
    assert settings.push_dispatch_enabled is False
    assert settings.push_dispatch_poll_seconds == 5
    assert settings.push_max_attempts == 5
    assert settings.validation_errors == ()


def test_postgres_dsn_configuration_is_reported_without_exposing_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIXION_POSTGRES_DSN", "postgresql://user:secret@db.example/aixion")

    settings = get_settings()

    assert settings.postgres_dsn_configured is True
    assert "secret" not in repr(settings)


def test_metrics_scrape_secret_is_redacted_and_workspace_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "ci-only-metrics-token-with-at-least-32-bytes"
    monkeypatch.setenv("AIXION_METRICS_TOKEN", secret)
    monkeypatch.setenv("AIXION_METRICS_WORKSPACE_ID", "workspace-account-1")

    settings = get_settings()

    assert settings.metrics_token == secret
    assert settings.metrics_token_configured is True
    assert settings.metrics_workspace_configured is True
    assert settings.metrics_workspace_id == "workspace-account-1"
    assert secret not in repr(settings)


@pytest.mark.parametrize("secret", ["short", " " * 40])
def test_metrics_scrape_token_rejects_short_or_blank_values(
    monkeypatch: pytest.MonkeyPatch,
    secret: str,
) -> None:
    monkeypatch.setenv("AIXION_METRICS_TOKEN", secret)
    monkeypatch.setenv("AIXION_METRICS_WORKSPACE_ID", "workspace-account-1")

    with pytest.raises(ValueError, match="AIXION_METRICS_TOKEN"):
        get_settings()


def test_metrics_scrape_token_requires_workspace_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIXION_METRICS_TOKEN", "ci-only-metrics-token-with-at-least-32-bytes")
    monkeypatch.delenv("AIXION_METRICS_WORKSPACE_ID", raising=False)

    with pytest.raises(ValueError, match="must be configured together"):
        get_settings()


def test_postgres_auth_rate_limits_require_runtime_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIXION_PROFILE", "test")
    monkeypatch.setenv("AIXION_POSTGRES_AUTH_RATE_LIMITS_ENABLED", "true")
    monkeypatch.setenv("AIXION_POSTGRES_RUNTIME_PROBE_ENABLED", "false")

    with pytest.raises(ValueError, match="requires AIXION_POSTGRES_RUNTIME_PROBE_ENABLED"):
        get_settings()


@pytest.mark.parametrize(
    ("name", "value", "required_message"),
    [
        (
            "AIXION_POSTGRES_RUNTIME_PROBE_ENABLED",
            "false",
            "requires AIXION_POSTGRES_RUNTIME_PROBE_ENABLED",
        ),
        (
            "AIXION_POSTGRES_AUTH_RATE_LIMITS_ENABLED",
            "false",
            "requires AIXION_POSTGRES_AUTH_RATE_LIMITS_ENABLED",
        ),
        (
            "AIXION_EMAIL_DISPATCH_ENABLED",
            "false",
            "requires AIXION_EMAIL_DISPATCH_ENABLED",
        ),
    ],
)
def test_postgres_auth_runtime_requires_all_fail_closed_prerequisites(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
    required_message: str,
) -> None:
    monkeypatch.setenv("AIXION_PROFILE", "test")
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")
    monkeypatch.setenv("AIXION_POSTGRES_AUTH_ENABLED", "true")
    monkeypatch.setenv("AIXION_POSTGRES_RUNTIME_PROBE_ENABLED", "true")
    monkeypatch.setenv("AIXION_POSTGRES_AUTH_RATE_LIMITS_ENABLED", "true")
    monkeypatch.setenv("AIXION_EMAIL_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("AIXION_EMAIL_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("AIXION_EMAIL_FROM", "verify@example.com")
    monkeypatch.setenv("AIXION_EMAIL_OUTBOX_KEY", "ab" * 32)
    if name == "AIXION_POSTGRES_RUNTIME_PROBE_ENABLED":
        monkeypatch.setenv("AIXION_POSTGRES_AUTH_RATE_LIMITS_ENABLED", "false")
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=required_message):
        get_settings()


def test_postgres_auth_runtime_accepts_explicit_prerequisites(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIXION_PROFILE", "test")
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")
    monkeypatch.setenv("AIXION_POSTGRES_AUTH_ENABLED", "true")
    monkeypatch.setenv("AIXION_POSTGRES_RUNTIME_PROBE_ENABLED", "true")
    monkeypatch.setenv("AIXION_POSTGRES_AUTH_RATE_LIMITS_ENABLED", "true")
    monkeypatch.setenv("AIXION_EMAIL_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("AIXION_EMAIL_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("AIXION_EMAIL_FROM", "verify@example.com")
    monkeypatch.setenv("AIXION_EMAIL_OUTBOX_KEY", "ab" * 32)

    settings = get_settings()

    assert settings.postgres_auth_enabled is True


def test_postgres_auth_runtime_rejects_disabled_auth_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIXION_PROFILE", "test")
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "false")
    monkeypatch.setenv("AIXION_POSTGRES_AUTH_ENABLED", "true")
    monkeypatch.setenv("AIXION_POSTGRES_RUNTIME_PROBE_ENABLED", "true")
    monkeypatch.setenv("AIXION_POSTGRES_AUTH_RATE_LIMITS_ENABLED", "true")
    monkeypatch.setenv("AIXION_EMAIL_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("AIXION_EMAIL_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("AIXION_EMAIL_FROM", "verify@example.com")
    monkeypatch.setenv("AIXION_EMAIL_OUTBOX_KEY", "ab" * 32)

    with pytest.raises(ValueError, match="requires AIXION_AUTH_ENABLED=true"):
        get_settings()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AIXION_POSTGRES_CONNECT_TIMEOUT_SECONDS", "0"),
        ("AIXION_POSTGRES_STATEMENT_TIMEOUT_MS", "120001"),
        ("AIXION_POSTGRES_LOCK_TIMEOUT_MS", "99"),
        ("AIXION_POSTGRES_IDLE_TRANSACTION_TIMEOUT_MS", "bad"),
        ("AIXION_POSTGRES_POOL_MIN_SIZE", "33"),
        ("AIXION_POSTGRES_POOL_MAX_SIZE", "0"),
        ("AIXION_POSTGRES_POOL_WAIT_TIMEOUT_SECONDS", "61"),
        ("AIXION_POSTGRES_POOL_MAX_WAITING", "1001"),
    ],
)
def test_postgres_timeouts_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        get_settings()


def test_postgres_pool_minimum_cannot_exceed_maximum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIXION_POSTGRES_POOL_MIN_SIZE", "3")
    monkeypatch.setenv("AIXION_POSTGRES_POOL_MAX_SIZE", "2")

    with pytest.raises(ValueError, match="AIXION_POSTGRES_POOL_MIN_SIZE"):
        get_settings()


@pytest.mark.parametrize("value", ["29", "901", "not-an-integer"])
def test_agent_approval_ttl_must_be_within_supported_bounds(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("AIXION_AGENT_APPROVAL_TTL_SECONDS", value)

    with pytest.raises(ValueError, match="AIXION_AGENT_APPROVAL_TTL_SECONDS"):
        get_settings()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AIXION_PUSH_DISPATCH_POLL_SECONDS", "0"),
        ("AIXION_PUSH_DISPATCH_POLL_SECONDS", "61"),
        ("AIXION_PUSH_MAX_ATTEMPTS", "0"),
        ("AIXION_PUSH_MAX_ATTEMPTS", "11"),
        ("AIXION_PUSH_RETRY_BASE_SECONDS", "0"),
        ("AIXION_PUSH_RETRY_MAX_SECONDS", "3601"),
        ("AIXION_PUSH_LEASE_SECONDS", "29"),
        ("AIXION_PUSH_LEASE_SECONDS", "601"),
        ("AIXION_PUSH_DISPATCH_ENABLED", "sometimes"),
    ],
)
def test_push_dispatch_configuration_rejects_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        get_settings()


def test_push_dispatch_configuration_is_explicit_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIXION_PUSH_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("AIXION_PUSH_DISPATCH_POLL_SECONDS", "3")
    monkeypatch.setenv("AIXION_PUSH_MAX_ATTEMPTS", "4")
    monkeypatch.setenv("AIXION_PUSH_RETRY_BASE_SECONDS", "10")
    monkeypatch.setenv("AIXION_PUSH_RETRY_MAX_SECONDS", "120")
    monkeypatch.setenv("AIXION_PUSH_LEASE_SECONDS", "90")

    settings = get_settings()

    assert settings.push_dispatch_enabled is True
    assert settings.push_dispatch_poll_seconds == 3
    assert settings.push_max_attempts == 4
    assert settings.push_retry_base_seconds == 10
    assert settings.push_retry_max_seconds == 120
    assert settings.push_lease_seconds == 90


@pytest.mark.parametrize("mode", ["inline", "scheduled"])
def test_outbox_dispatch_mode_accepts_supported_values(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    monkeypatch.setenv("AIXION_OUTBOX_DISPATCH_MODE", mode)

    assert get_settings().outbox_dispatch_mode == mode


@pytest.mark.parametrize("mode", ["", "worker", "IN PROCESS"])
def test_outbox_dispatch_mode_rejects_unknown_values(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    monkeypatch.setenv("AIXION_OUTBOX_DISPATCH_MODE", mode)

    with pytest.raises(ValueError, match="AIXION_OUTBOX_DISPATCH_MODE"):
        get_settings()


def test_push_retry_cap_cannot_be_less_than_base_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIXION_PUSH_RETRY_BASE_SECONDS", "30")
    monkeypatch.setenv("AIXION_PUSH_RETRY_MAX_SECONDS", "20")

    with pytest.raises(ValueError, match="AIXION_PUSH_RETRY_MAX_SECONDS"):
        get_settings()


def test_email_dispatch_configuration_is_bounded_and_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIXION_EMAIL_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("AIXION_EMAIL_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("AIXION_EMAIL_FROM", "verify@example.com")
    monkeypatch.setenv("AIXION_EMAIL_OUTBOX_KEY", "ab" * 32)
    monkeypatch.setenv("AIXION_EMAIL_SMTP_USERNAME", "smtp-user")
    monkeypatch.setenv("AIXION_EMAIL_SMTP_PASSWORD", "smtp-password")
    monkeypatch.setenv("AIXION_EMAIL_MAX_ATTEMPTS", "4")
    monkeypatch.setenv("AIXION_EMAIL_RETRY_BASE_SECONDS", "7")
    monkeypatch.setenv("AIXION_EMAIL_RETRY_MAX_SECONDS", "120")

    settings = get_settings()

    assert settings.email_dispatch_enabled is True
    assert settings.email_smtp_host == "smtp.example.com"
    assert settings.email_smtp_credentials_configured is True
    assert settings.email_outbox_key_configured is True
    assert settings.email_max_attempts == 4
    assert settings.email_retry_base_seconds == 7
    assert settings.email_retry_max_seconds == 120


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AIXION_EMAIL_DISPATCH_POLL_SECONDS", "0"),
        ("AIXION_EMAIL_MAX_ATTEMPTS", "11"),
        ("AIXION_EMAIL_RETRY_BASE_SECONDS", "0"),
        ("AIXION_EMAIL_RETRY_MAX_SECONDS", "3601"),
        ("AIXION_EMAIL_LEASE_SECONDS", "29"),
        ("AIXION_EMAIL_SMTP_TIMEOUT_SECONDS", "31"),
        ("AIXION_EMAIL_OUTBOX_KEY", "short"),
    ],
)
def test_email_dispatch_rejects_invalid_configuration(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        get_settings()


def test_auth_rate_limit_configuration_is_explicit_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIXION_AUTH_RATE_LIMIT_HMAC_KEY", "cd" * 32)
    monkeypatch.setenv("AIXION_AUTH_RATE_LIMIT_WINDOW_SECONDS", "120")
    monkeypatch.setenv("AIXION_AUTH_REGISTER_LIMIT_PER_IP", "8")
    monkeypatch.setenv("AIXION_AUTH_RESEND_LIMIT_PER_EMAIL", "2")
    settings = get_settings()

    assert settings.auth_rate_limit_key_configured is True
    assert settings.auth_rate_limit_window_seconds == 120
    assert settings.auth_register_limit_per_ip == 8
    assert settings.auth_resend_limit_per_email == 2


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AIXION_AUTH_RATE_LIMIT_WINDOW_SECONDS", "9"),
        ("AIXION_AUTH_REGISTER_LIMIT_PER_IP", "0"),
        ("AIXION_AUTH_RESEND_LIMIT_PER_EMAIL", "101"),
        ("AIXION_AUTH_RATE_LIMIT_HMAC_KEY", "short"),
    ],
)
def test_auth_rate_limit_configuration_rejects_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        get_settings()


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


@pytest.mark.parametrize("project_id", ["Short", "-starts-with", "trailing-", "contains_underscore", "x" * 31])
def test_invalid_fcm_project_id_fails_before_url_construction(
    monkeypatch: pytest.MonkeyPatch,
    project_id: str,
) -> None:
    monkeypatch.setenv("AIXION_FCM_PROJECT_ID", project_id)

    with pytest.raises(ValueError, match="AIXION_FCM_PROJECT_ID"):
        get_settings()


def test_valid_fcm_project_id_is_available_to_notification_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIXION_FCM_PROJECT_ID", "aixion-control-tower-1")

    settings = get_settings()

    assert settings.fcm_project_id == "aixion-control-tower-1"
    assert settings.fcm_server_key_configured is True


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
    monkeypatch.delenv("AIXION_FCM_PROJECT_ID", raising=False)
    monkeypatch.delenv("AIXION_METRICS_TOKEN", raising=False)
    monkeypatch.delenv("AIXION_METRICS_WORKSPACE_ID", raising=False)
    _clear_github_app(monkeypatch)

    settings = get_settings()

    assert settings.profile == "production"
    assert settings.auth_enabled is True
    assert settings.trust_enforcement is True
    assert settings.github_token_configured is False
    assert settings.github_app_configured is False
    assert settings.fcm_server_key_configured is False
    assert settings.fcm_project_id is None
    assert settings.lease_signing_key_configured is False
    joined = " ".join(settings.validation_errors)
    assert "AIXION_DB_PATH" not in joined
    assert "AIXION_LEASE_SIGNING_KEY" in joined
    assert "AIXION_FCM_PROJECT_ID" in joined
    assert "AIXION_AUTH_RATE_LIMIT_HMAC_KEY" in joined
    assert "AIXION_METRICS_TOKEN" in joined
    assert "AIXION_METRICS_WORKSPACE_ID" in joined
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
    monkeypatch.setenv("AIXION_FCM_PROJECT_ID", "control-tower-prod")

    settings = get_settings()

    assert "AIXION_AUTH_ENABLED must not be false in production" in settings.validation_errors
    assert "AIXION_TRUST_ENFORCEMENT must not be false in production" in settings.validation_errors


@pytest.mark.parametrize("enabled", [False, True])
def test_production_requires_push_dispatch_for_approval_notifications(
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
) -> None:
    monkeypatch.setenv("AIXION_PROFILE", "production")
    monkeypatch.setenv("AIXION_PUSH_DISPATCH_ENABLED", str(enabled).lower())

    settings = get_settings()

    error = "AIXION_PUSH_DISPATCH_ENABLED must be true for production approval notifications"
    assert (error in settings.validation_errors) is (not enabled)


@pytest.mark.parametrize(("mode", "expected_error"), [("inline", True), ("scheduled", False)])
def test_production_requires_scheduled_outbox_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected_error: bool,
) -> None:
    monkeypatch.setenv("AIXION_PROFILE", "production")
    monkeypatch.setenv("AIXION_OUTBOX_DISPATCH_MODE", mode)

    settings = get_settings()

    error = "AIXION_OUTBOX_DISPATCH_MODE must be scheduled in production"
    assert (error in settings.validation_errors) is expected_error


def test_production_rejects_demo_or_test_database_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIXION_PROFILE", "production")
    monkeypatch.delenv("AIXION_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("AIXION_TRUST_ENFORCEMENT", raising=False)
    monkeypatch.setenv("AIXION_DB_PATH", "runtime/aixion_control_tower_demo.sqlite3")
    monkeypatch.setenv("AIXION_LEASE_SIGNING_KEY", "configured")
    monkeypatch.setenv("GITHUB_" + "TOKEN", "configured")
    monkeypatch.setenv("AIXION_FCM_PROJECT_ID", "control-tower-prod")

    settings = get_settings()

    assert (
        "AIXION_DB_PATH must not point at demo or test database files in production"
        in settings.validation_errors
    )


def test_production_profile_fails_closed_until_postgresql_store_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIXION_PROFILE", "production")
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")
    monkeypatch.setenv("AIXION_TRUST_ENFORCEMENT", "true")
    monkeypatch.setenv("AIXION_DB_PATH", "runtime/prod.sqlite3")
    monkeypatch.setenv("AIXION_LEASE_SIGNING_KEY", "configured")
    monkeypatch.setenv("GITHUB_" + "TOKEN", "configured")
    monkeypatch.setenv("AIXION_FCM_PROJECT_ID", "control-tower-prod")

    settings = get_settings()

    assert settings.profile == "production"
    assert any("single-process SQLite store" in error for error in settings.validation_errors)
    assert any("AIXION_EMAIL_DISPATCH_ENABLED" in error for error in settings.validation_errors)
    assert any("AIXION_EMAIL_OUTBOX_KEY" in error for error in settings.validation_errors)
    with pytest.raises(RuntimeError, match="Production profile is disabled"):
        validate_startup_environment()


def test_production_rejects_any_sqlite_database_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIXION_PROFILE", "production")
    monkeypatch.setenv("AIXION_DB_PATH", "runtime/production.sqlite3")

    settings = get_settings()

    assert (
        "AIXION_DB_PATH is SQLite-only and must not be configured in production"
        in settings.validation_errors
    )


def test_sqlite_store_rejects_production_even_if_environment_validation_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import importlib

    store_module = importlib.import_module("app.store")
    db_path = tmp_path / "production.sqlite3"
    monkeypatch.setattr(
        store_module,
        "validate_startup_environment",
        lambda: SimpleNamespace(is_production=True, db_path=db_path),
    )

    with pytest.raises(RuntimeError, match="SQLite-backed store cannot initialize"):
        store_module.SQLiteBackedStore()

    assert not db_path.exists()


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
    monkeypatch.setenv("AIXION_FCM_PROJECT_ID", "control-tower-prod")

    settings = get_settings()

    assert settings.github_token_configured is False
    assert settings.github_app_configured is True
    assert any(
        "implement and configure the PostgreSQL store" in error
        for error in settings.validation_errors
    )
    assert any("Production verification email requires" in error for error in settings.validation_errors)


def test_invalid_production_environment_fails_startup_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIXION_PROFILE", "production")
    monkeypatch.delenv("AIXION_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("AIXION_TRUST_ENFORCEMENT", raising=False)
    monkeypatch.delenv("AIXION_DB_PATH", raising=False)
    monkeypatch.delenv("AIXION_LEASE_SIGNING_KEY", raising=False)
    monkeypatch.delenv("GITHUB_" + "TOKEN", raising=False)
    monkeypatch.delenv("AIXION_FCM_PROJECT_ID", raising=False)
    _clear_github_app(monkeypatch)

    with pytest.raises(RuntimeError, match="Invalid Aixion production environment"):
        validate_startup_environment()
