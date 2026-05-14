from __future__ import annotations

import os

os.environ.setdefault("AIXION_PROFILE", "test")
os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from fastapi.testclient import TestClient

from app.auth import require_user
from app.main import app
from app.models import AuthUser, UserRole
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
    assert body["db_reachable"] is True
    assert body["migrations_applied"] is True
    assert "0001_baseline_kv_store" in body["expected_migration_ids"]
    assert "0001_baseline_kv_store" in body["applied_migration_ids"]
    assert body["recovery_snapshot_available"] is True
    assert body["recovery_format_version"] == "aixion-control-tower-recovery-v1"
    assert isinstance(body["github_token_configured"], bool)
    assert isinstance(body["fcm_server_key_configured"], bool)
    assert "db_path" not in body
    assert "GITHUB_TOKEN" not in str(body)
    assert "FCM_SERVER_KEY" not in str(body)


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
