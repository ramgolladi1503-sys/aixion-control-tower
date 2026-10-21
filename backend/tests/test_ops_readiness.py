from __future__ import annotations

import os

os.environ.setdefault("AIXION_PROFILE", "test")
os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from fastapi.testclient import TestClient

from app.auth import require_user
from app.main import app
from app.models import AuthUser, UserRole
from app.ops_routes import build_runtime_readiness
from app.store import store

client = TestClient(app)


def setup_function() -> None:
    store.reset()
    app.dependency_overrides.clear()


def teardown_function() -> None:
    app.dependency_overrides.clear()


def _auth_user(role: UserRole) -> AuthUser:
    return AuthUser(
        id=f"user_{role.value.lower()}",
        email=f"{role.value.lower()}@example.com",
        display_name=role.value.title(),
        role=role,
    )


def test_readiness_reports_runtime_state_without_secret_values() -> None:
    response = client.get("/ops/readiness")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["auth_enabled"] is False
    assert body["database_backend"] == "sqlite"
    assert body["postgres_probe_enabled"] is False
    assert body["postgres_auth_rate_limits_enabled"] is False
    assert body["postgres_probe_status"] == "disabled"
    assert body["postgres_probe_schema_ready"] is None
    assert body["db_reachable"] is True
    assert body["migrations_applied"] is True
    assert "0001_baseline_kv_store" in body["expected_migration_ids"]
    assert "0001_baseline_kv_store" in body["applied_migration_ids"]
    assert body["recovery_snapshot_available"] is True
    assert body["recovery_format_version"] == "aixion-control-tower-recovery-v1"
    assert isinstance(body["github_token_configured"], bool)
    assert isinstance(body["fcm_server_key_configured"], bool)
    assert body["email_dispatch_enabled"] is False
    assert body["email_smtp_configured"] is False
    assert body["email_outbox_key_configured"] is False
    assert isinstance(body["auth_rate_limit_key_configured"], bool)
    assert body["metrics_token_configured"] is False
    assert body["metrics_workspace_configured"] is False
    assert "db_path" not in body
    assert "GITHUB_TOKEN" not in str(body)
    assert "FCM_SERVER_KEY" not in str(body)
    assert "SMTP_PASSWORD" not in str(body)
    assert "AIXION_EMAIL_OUTBOX_KEY" not in str(body)
    assert "AIXION_AUTH_RATE_LIMIT_HMAC_KEY" not in str(body)
    assert "AIXION_METRICS_TOKEN" not in str(body)


def test_readiness_reports_metrics_configuration_without_secret_value(monkeypatch) -> None:
    token = "test-only-prometheus-token-with-more-than-32-bytes"
    monkeypatch.setenv("AIXION_METRICS_TOKEN", token)
    monkeypatch.setenv("AIXION_METRICS_WORKSPACE_ID", "workspace-account-1")

    response = client.get("/ops/readiness")

    assert response.status_code == 200
    body = response.json()
    assert body["metrics_token_configured"] is True
    assert body["metrics_workspace_configured"] is True
    assert token not in response.text


def test_readiness_reports_postgres_probe_without_claiming_it_is_active_store(
    monkeypatch,
) -> None:
    class Runtime:
        def probe(self):
            schema = type("Schema", (), {"ready": True})()
            return type(
                "Probe",
                (),
                {
                    "ready": True,
                    "schema": schema,
                    "pool_size": 3,
                    "pool_available": 2,
                    "pool_waiting": 1,
                },
            )()

    monkeypatch.setenv("AIXION_POSTGRES_RUNTIME_PROBE_ENABLED", "true")
    monkeypatch.setenv("AIXION_POSTGRES_AUTH_RATE_LIMITS_ENABLED", "true")
    readiness = build_runtime_readiness(postgres_runtime=Runtime())

    assert readiness.postgres_probe_enabled is True
    assert readiness.postgres_probe_status == "ready"
    assert readiness.postgres_probe_schema_ready is True
    assert readiness.postgres_pool_size == 3
    assert readiness.postgres_pool_available == 2
    assert readiness.postgres_pool_waiting == 1
    assert readiness.postgres_auth_rate_limits_enabled is True
    assert readiness.database_backend == "sqlite"


def test_readiness_fails_closed_when_enabled_postgres_probe_is_unavailable(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AIXION_POSTGRES_RUNTIME_PROBE_ENABLED", "true")

    readiness = build_runtime_readiness()

    assert readiness.status == "not_ready"
    assert readiness.postgres_probe_status == "not_ready"
    assert any("runtime is unavailable" in error for error in readiness.errors)


def test_readiness_sanitizes_postgres_probe_failures(monkeypatch) -> None:
    class Runtime:
        def probe(self):
            raise RuntimeError("postgres://user:secret@private-host/db")

    monkeypatch.setenv("AIXION_POSTGRES_RUNTIME_PROBE_ENABLED", "true")
    readiness = build_runtime_readiness(postgres_runtime=Runtime())

    assert readiness.status == "not_ready"
    assert "secret" not in str(readiness.model_dump())
    assert "PostgreSQL runtime probe failed." in readiness.errors


def test_readiness_does_not_claim_fcm_delivery_is_verified(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_FCM_PROJECT_ID", "aixion-control-tower-1")

    response = client.get("/ops/readiness")

    assert response.status_code == 200
    body = response.json()
    assert body["fcm_server_key_configured"] is True
    assert any(
        "does not verify ADC credentials or send permission" in warning
        for warning in body["warnings"]
    )


def test_readiness_allows_maintainer_role() -> None:
    app.dependency_overrides[require_user] = lambda: _auth_user(UserRole.MAINTAINER)

    response = client.get("/ops/readiness")

    assert response.status_code == 200


def test_readiness_rejects_reviewer_role() -> None:
    app.dependency_overrides[require_user] = lambda: _auth_user(UserRole.REVIEWER)

    response = client.get("/ops/readiness")

    assert response.status_code == 403


def test_readiness_reports_not_ready_when_database_check_fails(monkeypatch) -> None:
    def fail_applied_migrations() -> list[dict[str, str]]:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(store, "applied_migrations", fail_applied_migrations)

    response = client.get("/ops/readiness")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["db_reachable"] is False
    assert body["migrations_applied"] is False
    assert body["recovery_snapshot_available"] is False
    assert any(error.startswith("Database readiness check failed") for error in body["errors"])
