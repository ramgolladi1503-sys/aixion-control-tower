from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timezone

import pytest

os.environ.setdefault("AIXION_PROFILE", "test")
os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from fastapi.testclient import TestClient

from app.auth import require_user
from app import ops_routes
from app.main import app
from app.models import AuthUser, UserRole
from app.settings import get_settings
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
    assert body["application_store_mode"] == "sqlite_local"
    assert body["application_store_ready"] is True
    assert body["approval_recovery_status"] in {"not_started", "healthy", "degraded"}
    assert body["approval_recovery_poll_seconds"] == 60
    assert body["approval_expiry_status"] in {
        "not_started", "starting", "healthy", "failed"
    }
    assert body["approval_expiry_failure_count"] >= 0
    assert body["approval_expiry_last_changed_count"] >= 0
    assert body["app_state_repository_bindings"]["approval_decisions"] == "sqlite"
    assert body["app_state_repository_bindings"]["projects"] == "sqlite"
    assert body["app_state_repository_bindings"]["notification_outbox"] == "unbound"
    assert body["app_state_repository_bindings"]["mcp_pending_requests"] == "unbound"
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
    readiness = build_runtime_readiness(
        postgres_runtime=Runtime(),
        application_state=app.state,
    )

    assert readiness.postgres_probe_enabled is True
    assert readiness.postgres_probe_status == "ready"
    assert readiness.postgres_probe_schema_ready is True
    assert readiness.postgres_pool_size == 3
    assert readiness.postgres_pool_available == 2
    assert readiness.postgres_pool_waiting == 1
    assert readiness.postgres_auth_rate_limits_enabled is True
    assert readiness.database_backend == "sqlite"
    assert readiness.application_store_mode == "postgres_probe_only"
    assert readiness.application_store_ready is False
    assert readiness.app_state_repository_bindings["approval_decisions"] == "sqlite"


def test_readiness_reports_postgres_auth_as_mixed_and_not_ready(monkeypatch) -> None:
    class Runtime:
        def probe(self):
            schema = type("Schema", (), {"ready": True})()
            return type(
                "Probe",
                (),
                {
                    "ready": True,
                    "schema": schema,
                    "pool_size": 2,
                    "pool_available": 2,
                    "pool_waiting": 0,
                },
            )()

    settings = replace(
        get_settings(),
        auth_enabled=True,
        postgres_auth_enabled=True,
        postgres_runtime_probe_enabled=True,
        postgres_auth_rate_limits_enabled=True,
        email_dispatch_enabled=True,
    )
    monkeypatch.setattr(ops_routes, "get_settings", lambda: settings)
    readiness = build_runtime_readiness(
        postgres_runtime=Runtime(),
        application_state=app.state,
    )

    assert readiness.database_backend == "mixed"
    assert readiness.application_store_mode == "postgres_auth_runtime"
    assert readiness.application_store_ready is False
    assert readiness.postgres_probe_status == "ready"
    assert readiness.status == "not_ready"
    assert any("persistence is mixed" in error for error in readiness.errors)


def test_readiness_reports_bound_repository_identity_without_claiming_full_readiness(
    monkeypatch,
) -> None:
    postgres_approval = object()
    postgres_task = object()
    runtime_repositories = type(
        "Repositories",
        (),
        {"approval_decisions": postgres_approval, "agent_tasks": postgres_task},
    )()

    class Runtime:
        repositories = runtime_repositories

        def probe(self):
            schema = type("Schema", (), {"ready": True})()
            return type(
                "Probe",
                (),
                {
                    "ready": True,
                    "schema": schema,
                    "pool_size": 1,
                    "pool_available": 1,
                    "pool_waiting": 0,
                },
            )()

    state = type(
        "ApplicationState",
        (),
        {
            "approval_decision_repository": postgres_approval,
            "agent_task_repository": app.state.agent_task_repository,
            "agent_run_repository": None,
            "connector_repository": object(),
        },
    )()
    monkeypatch.setenv("AIXION_POSTGRES_RUNTIME_PROBE_ENABLED", "true")
    readiness = build_runtime_readiness(
        postgres_runtime=Runtime(),
        application_state=state,
    )

    assert readiness.app_state_repository_bindings["approval_decisions"] == "postgresql"
    assert readiness.app_state_repository_bindings["agent_tasks"] == "sqlite"
    assert readiness.app_state_repository_bindings["agent_runs"] == "unbound"
    assert readiness.app_state_repository_bindings["connectors"] == "other"
    assert readiness.app_state_repository_bindings["projects"] == "unbound"
    assert readiness.application_store_ready is False


def test_readiness_recognizes_sqlite_repository_subclasses() -> None:
    class InstrumentedApprovalRepository(type(app.state.approval_decision_repository)):
        pass

    repository = InstrumentedApprovalRepository(
        app.state.approval_decision_repository._connect
    )
    state = type(
        "ApplicationState",
        (),
        {"approval_decision_repository": repository},
    )()

    readiness = build_runtime_readiness(application_state=state)

    assert readiness.app_state_repository_bindings["approval_decisions"] == "sqlite"


def test_production_readiness_rejects_sqlite_application_store(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_PROFILE", "production")
    monkeypatch.setenv("AIXION_POSTGRES_RUNTIME_PROBE_ENABLED", "false")

    readiness = build_runtime_readiness()

    assert readiness.status == "not_ready"
    assert readiness.application_store_mode == "sqlite_local"
    assert readiness.application_store_ready is False
    assert any("not bound to one complete PostgreSQL" in error for error in readiness.errors)


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


def test_production_readiness_fails_closed_when_approval_recovery_is_degraded(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AIXION_PROFILE", "production")
    monkeypatch.setenv("AIXION_POSTGRES_RUNTIME_PROBE_ENABLED", "true")
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")
    monkeypatch.setenv("AIXION_TRUST_ENFORCEMENT", "true")
    application_state = type(
        "State",
        (),
        {
            "approval_recovery_status": "degraded",
            "approval_recovery_failure_count": 2,
        },
    )()

    readiness = build_runtime_readiness(application_state=application_state)

    assert readiness.approval_recovery_status == "degraded"
    assert readiness.approval_recovery_failure_count == 2
    assert readiness.status == "not_ready"
    assert any("Approval recovery is not healthy" in error for error in readiness.errors)


def test_readiness_reports_lifecycle_recovery_health() -> None:
    state = type(
        "State",
        (),
        {
            "approval_recovery_status": "degraded",
            "approval_recovery_failure_count": 3,
        },
    )()

    readiness = build_runtime_readiness(application_state=state)

    assert readiness.approval_recovery_status == "degraded"
    assert readiness.approval_recovery_failure_count == 3


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


def test_readiness_defaults_approval_expiry_worker_to_not_started() -> None:
    readiness = build_runtime_readiness(application_state=None)

    assert readiness.approval_expiry_status == "not_started"
    assert readiness.approval_expiry_failure_count == 0
    assert readiness.approval_expiry_last_completed_at is None
    assert readiness.approval_expiry_last_changed_count == 0


def test_readiness_reports_healthy_approval_expiry_worker() -> None:
    completed_at = datetime.now(timezone.utc)
    state = type(
        "State",
        (),
        {
            "approval_expiry_status": "healthy",
            "approval_expiry_failure_count": 0,
            "approval_expiry_last_completed_at": completed_at,
            "approval_expiry_last_changed_count": 2,
        },
    )()

    readiness = build_runtime_readiness(application_state=state)

    assert readiness.approval_expiry_status == "healthy"
    assert readiness.approval_expiry_failure_count == 0
    assert readiness.approval_expiry_last_completed_at == completed_at
    assert readiness.approval_expiry_last_changed_count == 2
    assert readiness.approval_expiry_task_present is False
    assert readiness.approval_expiry_task_running is False
    assert readiness.approval_expiry_last_completion_fresh is True


class _ExpiryTask:
    def __init__(self, done: bool = False) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


def _production_expiry_readiness(monkeypatch, **state_values):
    monkeypatch.setenv("AIXION_PROFILE", "production")
    monkeypatch.setenv("AIXION_POSTGRES_RUNTIME_PROBE_ENABLED", "false")
    settings = replace(
        get_settings(),
        profile="production",
        trust_enforcement=True,
        lease_signing_key_configured=True,
    )
    monkeypatch.setattr(ops_routes, "get_settings", lambda: settings)
    base_state = {
        "approval_recovery_status": "healthy",
        "approval_expiry_status": "healthy",
        "approval_expiry_failure_count": 0,
        "approval_expiry_last_completed_at": datetime.now(timezone.utc),
        "approval_expiry_task": _ExpiryTask(),
    }
    base_state.update(state_values)
    state = type("State", (), base_state)()
    return build_runtime_readiness(application_state=state)


def test_production_readiness_requires_live_expiry_task(monkeypatch) -> None:
    for task in (None, _ExpiryTask(done=True)):
        readiness = _production_expiry_readiness(
            monkeypatch,
            approval_expiry_task=task,
        )
        assert readiness.status == "not_ready"
        assert readiness.approval_expiry_task_running is False
        assert "Approval expiry worker task is not running" in readiness.errors


@pytest.mark.parametrize(
    "completed_at",
    [
        datetime(2020, 1, 1, tzinfo=timezone.utc),
        datetime(2026, 10, 23, 12, 0),  # Naive timestamps are not trusted.
        None,
    ],
)
def test_production_readiness_requires_fresh_aware_expiry_completion(
    monkeypatch, completed_at
) -> None:
    readiness = _production_expiry_readiness(
        monkeypatch,
        approval_expiry_last_completed_at=completed_at,
    )
    assert readiness.status == "not_ready"
    assert readiness.approval_expiry_last_completion_fresh is False
    assert "Approval expiry worker completion is stale or unavailable" in readiness.errors


def test_production_readiness_accepts_live_task_and_fresh_expiry_completion(monkeypatch) -> None:
    readiness = _production_expiry_readiness(monkeypatch)

    assert readiness.approval_expiry_task_present is True
    assert readiness.approval_expiry_task_running is True
    assert readiness.approval_expiry_last_completion_fresh is True
    assert readiness.approval_expiry_max_completion_age_seconds == 180
    assert not any(
        error.startswith("Approval expiry worker") for error in readiness.errors
    )


def test_readiness_reports_failed_approval_expiry_worker_without_changing_readiness() -> None:
    state = type(
        "State",
        (),
        {
            "approval_expiry_status": "failed",
            "approval_expiry_failure_count": 3,
            "approval_expiry_last_completed_at": None,
            "approval_expiry_last_changed_count": 0,
        },
    )()

    readiness = build_runtime_readiness(application_state=state)

    assert readiness.approval_expiry_status == "failed"
    assert readiness.approval_expiry_failure_count == 3
    assert readiness.approval_expiry_last_changed_count == 0
    assert readiness.approval_expiry_task_present is False
    assert readiness.approval_expiry_task_running is False
    # This is additive diagnostics; existing local readiness semantics are unchanged.
    assert readiness.status == "ready"


@pytest.mark.parametrize("expiry_status", ["not_started", "failed"])
def test_production_readiness_fails_closed_when_approval_expiry_is_unhealthy(
    monkeypatch, expiry_status: str
) -> None:
    monkeypatch.setenv("AIXION_PROFILE", "production")
    monkeypatch.setenv("AIXION_POSTGRES_RUNTIME_PROBE_ENABLED", "false")
    settings = replace(
        get_settings(),
        profile="production",
        trust_enforcement=True,
        lease_signing_key_configured=True,
    )
    monkeypatch.setattr(ops_routes, "get_settings", lambda: settings)
    state = type(
        "State",
        (),
        {
            "approval_recovery_status": "healthy",
            "approval_expiry_status": expiry_status,
            "approval_expiry_failure_count": 1 if expiry_status == "failed" else 0,
        },
    )()

    readiness = build_runtime_readiness(application_state=state)

    assert readiness.approval_expiry_status == expiry_status
    assert readiness.status == "not_ready"
    assert any(
        f"Approval expiry worker is not healthy: status={expiry_status}" == error
        for error in readiness.errors
    )
