from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import settings


def worker_settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "is_production": True,
        "outbox_dispatch_mode": "scheduled",
        "postgres_dsn_configured": True,
        "push_dispatch_enabled": True,
        "fcm_project_id": "aixion-staging-123",
        "email_dispatch_enabled": True,
        "email_smtp_host": "smtp.example.test",
        "email_from": "noreply@example.test",
        "email_smtp_credentials_configured": True,
        "email_outbox_key_configured": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_push_worker_accepts_only_its_scoped_production_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AIXION_DB_PATH", raising=False)
    configured = worker_settings(email_dispatch_enabled=False)
    monkeypatch.setattr(settings, "get_settings", lambda: configured)

    assert settings.validate_outbox_worker_environment("push") is configured


def test_email_worker_accepts_only_its_scoped_production_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AIXION_DB_PATH", raising=False)
    configured = worker_settings(push_dispatch_enabled=False, fcm_project_id=None)
    monkeypatch.setattr(settings, "get_settings", lambda: configured)

    assert settings.validate_outbox_worker_environment("email") is configured


def test_disabled_worker_does_not_require_database_or_provider_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AIXION_DB_PATH", raising=False)
    configured = worker_settings(
        postgres_dsn_configured=False,
        push_dispatch_enabled=False,
        fcm_project_id=None,
    )
    monkeypatch.setattr(settings, "get_settings", lambda: configured)

    assert settings.validate_outbox_worker_environment("push") is configured


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"is_production": False}, "AIXION_PROFILE=production"),
        ({"outbox_dispatch_mode": "inline"}, "AIXION_OUTBOX_DISPATCH_MODE=scheduled"),
        ({"postgres_dsn_configured": False}, "AIXION_POSTGRES_DSN"),
        ({"fcm_project_id": None}, "AIXION_FCM_PROJECT_ID"),
    ],
)
def test_push_worker_rejects_invalid_production_inputs(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
    message: str,
) -> None:
    monkeypatch.setattr(settings, "get_settings", lambda: worker_settings(**overrides))

    with pytest.raises(RuntimeError, match=message):
        settings.validate_outbox_worker_environment("push")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"postgres_dsn_configured": False}, "AIXION_POSTGRES_DSN"),
        ({"email_smtp_host": None}, "AIXION_EMAIL_SMTP_HOST"),
        ({"email_from": None}, "AIXION_EMAIL_FROM"),
        ({"email_smtp_credentials_configured": False}, "AIXION_EMAIL_SMTP_USERNAME"),
        ({"email_outbox_key_configured": False}, "AIXION_EMAIL_OUTBOX_KEY"),
    ],
)
def test_email_worker_rejects_missing_database_or_email_settings(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
    message: str,
) -> None:
    monkeypatch.setattr(settings, "get_settings", lambda: worker_settings(**overrides))

    with pytest.raises(RuntimeError, match=message):
        settings.validate_outbox_worker_environment("email")


def test_outbox_workers_reject_explicit_sqlite_database_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIXION_DB_PATH", "/tmp/should-not-be-used.sqlite3")
    monkeypatch.setattr(settings, "get_settings", lambda: worker_settings())

    with pytest.raises(RuntimeError, match="AIXION_DB_PATH is SQLite-only"):
        settings.validate_outbox_worker_environment("push")


def test_api_production_guard_remains_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        "get_settings",
        lambda: SimpleNamespace(validation_errors=("whole-app PostgreSQL binding is incomplete",)),
    )

    with pytest.raises(RuntimeError, match="whole-app PostgreSQL binding is incomplete"):
        settings.validate_startup_environment()


def test_unknown_worker_kind_is_rejected() -> None:
    with pytest.raises(ValueError, match="worker_kind must be 'push' or 'email'"):
        settings.validate_outbox_worker_environment("database")
