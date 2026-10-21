from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import metrics_auth
from app.main import app
from app.models import AuthUser, UserRole

client = TestClient(app)
SCRAPE_TOKEN = "test-only-prometheus-token-with-more-than-32-bytes"
WORKSPACE_ID = "workspace-account-1"


def setup_function() -> None:
    app.dependency_overrides.clear()


def teardown_function() -> None:
    app.dependency_overrides.clear()


def _configure_token(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_PROFILE", "test")
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")
    monkeypatch.setenv("AIXION_METRICS_TOKEN", SCRAPE_TOKEN)
    monkeypatch.setenv("AIXION_METRICS_WORKSPACE_ID", WORKSPACE_ID)


def _request_user(role: UserRole, workspace_id: str | None = WORKSPACE_ID) -> AuthUser:
    return AuthUser(
        id=f"test-{role.value.lower()}",
        email="maintainer@example.test",
        display_name="Test user",
        role=role,
        workspace_id=workspace_id,
        email_verified=True,
    )


def test_scrape_token_authenticates_metrics_for_its_bound_workspace(monkeypatch) -> None:
    _configure_token(monkeypatch)
    observed: list[str | None] = []

    from app import agent_run_routes

    monkeypatch.setattr(
        agent_run_routes,
        "prometheus_agent_run_metrics",
        lambda workspace_id: observed.append(workspace_id) or "aixion_test_metric 1\n",
    )

    response = client.get(
        "/agent/runs/metrics",
        headers={"Authorization": f"Bearer {SCRAPE_TOKEN}"},
    )

    assert response.status_code == 200
    assert response.text == "aixion_test_metric 1\n"
    assert observed == [WORKSPACE_ID]


def test_scrape_includes_workspace_scoped_postgres_outbox_metrics_when_bound(monkeypatch) -> None:
    _configure_token(monkeypatch)
    repository = object()
    observed: list[tuple[object, str | None]] = []

    from app import agent_run_routes

    monkeypatch.setattr(app.state, "notification_outbox_repository", repository, raising=False)
    monkeypatch.setattr(agent_run_routes, "prometheus_agent_run_metrics", lambda _scope: "base_metric 1\n")
    monkeypatch.setattr(
        agent_run_routes,
        "prometheus_postgres_notification_metrics",
        lambda repo, scope: observed.append((repo, scope)) or "outbox_metric 1\n",
    )

    response = client.get(
        "/agent/runs/metrics",
        headers={"Authorization": f"Bearer {SCRAPE_TOKEN}"},
    )

    assert response.status_code == 200
    assert response.text == "base_metric 1\noutbox_metric 1\n"
    assert observed == [(repository, WORKSPACE_ID)]


def test_invalid_scrape_token_fails_closed(monkeypatch) -> None:
    _configure_token(monkeypatch)

    response = client.get(
        "/agent/runs/metrics",
        headers={"Authorization": "Bearer wrong-token"},
    )

    assert response.status_code == 401


def test_missing_scrape_token_requires_normal_authenticated_session(monkeypatch) -> None:
    _configure_token(monkeypatch)

    response = client.get("/agent/runs/metrics")

    assert response.status_code == 401


def test_maintainer_session_fallback_is_preserved(monkeypatch) -> None:
    _configure_token(monkeypatch)
    monkeypatch.setattr(
        metrics_auth,
        "require_user",
        lambda **_: _request_user(UserRole.MAINTAINER),
    )

    response = client.get(
        "/agent/runs/metrics",
        headers={"Authorization": "Bearer maintainer-session"},
    )

    assert response.status_code == 200


def test_reviewer_session_cannot_scrape_metrics(monkeypatch) -> None:
    _configure_token(monkeypatch)
    monkeypatch.setattr(
        metrics_auth,
        "require_user",
        lambda **_: _request_user(UserRole.REVIEWER),
    )

    response = client.get(
        "/agent/runs/metrics",
        headers={"Authorization": "Bearer reviewer-session"},
    )

    assert response.status_code == 403


def test_scrape_token_dependency_never_grants_a_global_workspace(monkeypatch) -> None:
    _configure_token(monkeypatch)
    monkeypatch.delenv("AIXION_METRICS_WORKSPACE_ID")
    request = client.build_request("GET", "/agent/runs/metrics")

    with pytest.raises((ValueError, HTTPException)):
        metrics_auth.require_metrics_or_maintainer(
            authorization=f"Bearer {SCRAPE_TOKEN}",
            request=request,
        )
