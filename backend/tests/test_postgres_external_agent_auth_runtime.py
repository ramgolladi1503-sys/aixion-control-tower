from __future__ import annotations

from types import SimpleNamespace

import pytest
import psycopg
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app import agent_auth
from app import application_repository_access
from app.application_repositories import ApplicationRepositories
from app.postgres_external_agent_auth_repository import (
    PostgresExternalAgentAuthRepository,
)
from app.postgres_runtime import PostgresRuntime


def test_runtime_external_agent_auth_uses_shared_connection_provider(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def connector(dsn: str, **kwargs: object) -> object:
        seen["dsn"] = dsn
        seen["kwargs"] = kwargs
        return object()

    monkeypatch.setenv("AIXION_PROFILE", "test")
    monkeypatch.setenv(
        "AIXION_POSTGRES_DSN",
        "postgresql://runtime-test@localhost/aixion_test",
    )
    runtime = PostgresRuntime.from_environment(connector=connector)  # type: ignore[arg-type]

    repository = runtime.external_agent_auth

    assert isinstance(repository, PostgresExternalAgentAuthRepository)
    assert repository._connect() is not None
    assert seen["dsn"] == "postgresql://runtime-test@localhost/aixion_test"
    assert isinstance(seen["kwargs"], dict)


@pytest.mark.parametrize(
    "settings",
    [
        SimpleNamespace(is_production=False, postgres_auth_enabled=True),
        SimpleNamespace(is_production=True, postgres_auth_enabled=True),
    ],
    ids=["postgres-auth", "production"],
)
def test_external_agent_auth_mismatched_app_runtime_is_retryable_before_local_access(
    monkeypatch, settings
) -> None:
    class ForbiddenSQLiteRepository:
        def authenticate_and_consume(self, **_kwargs):
            pytest.fail(
                "mismatched PostgreSQL auth runtime must not read local SQLite auth"
            )

    monkeypatch.setattr(agent_auth, "get_settings", lambda: settings)
    monkeypatch.setattr(application_repository_access, "get_settings", lambda: settings)
    monkeypatch.setattr(agent_auth, "agent_auth_repository", ForbiddenSQLiteRepository())

    # Both are real typed repository bundles, but the app's selected bundle is
    # not the bundle owned by its runtime. The request exercises the production
    # selector and dependency path rather than replacing the selector with a stub.
    runtime_bundle = ApplicationRepositories.from_connector(
        lambda: pytest.fail("the mismatch must fail before opening PostgreSQL")
    )
    selected_bundle = ApplicationRepositories.from_connector(
        lambda: pytest.fail("the mismatch must fail before opening PostgreSQL")
    )
    app = FastAPI()
    app.state.postgres_runtime = SimpleNamespace(repositories=runtime_bundle)
    app.state.postgres_repositories = selected_bundle

    @app.get("/external-agent")
    def external_agent_route(
        authenticated_agent=Depends(agent_auth.require_external_agent),
    ):
        return {"id": authenticated_agent.id}

    response = TestClient(app).get(
        "/external-agent",
        headers={
            "X-Aixion-Agent-Id": "agent-test",
            "X-Aixion-Agent-Token": "secret-test",
        },
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": "External agent authentication runtime is unavailable"
    }
    assert response.headers["retry-after"] == "1"


def test_external_agent_auth_database_failure_is_retryable_before_local_access(
    monkeypatch,
) -> None:
    class UnavailablePostgresRepository:
        def authenticate_and_consume(self, **_kwargs):
            raise psycopg.OperationalError("injected PostgreSQL connection failure")

    class ForbiddenSQLiteRepository:
        def authenticate_and_consume(self, **_kwargs):
            pytest.fail("PostgreSQL outage must not fall back to local SQLite auth")

    settings = SimpleNamespace(is_production=False, postgres_auth_enabled=True)
    monkeypatch.setattr(agent_auth, "get_settings", lambda: settings)
    monkeypatch.setattr(application_repository_access, "get_settings", lambda: settings)
    monkeypatch.setattr(agent_auth, "agent_auth_repository", ForbiddenSQLiteRepository())
    monkeypatch.setattr(
        agent_auth,
        "external_agent_auth_repository_for_request",
        lambda _request: UnavailablePostgresRepository(),
    )

    app = FastAPI()

    @app.get("/external-agent")
    def external_agent_route(
        authenticated_agent=Depends(agent_auth.require_external_agent),
    ):
        return {"id": authenticated_agent.id}

    response = TestClient(app).get(
        "/external-agent",
        headers={
            "X-Aixion-Agent-Id": "agent-test",
            "X-Aixion-Agent-Token": "secret-test",
        },
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": "External agent authentication runtime is unavailable"
    }
    assert response.headers["retry-after"] == "1"
