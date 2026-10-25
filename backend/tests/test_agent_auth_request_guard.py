from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import agent_auth
from app import agent_routes
from app.agent_credential_models import AgentCreateWithCredentialPolicy
from app.models import AgentProvider, AuthUser, UserRole


@pytest.mark.parametrize(
    "settings",
    [
        SimpleNamespace(is_production=True, postgres_auth_enabled=False),
        SimpleNamespace(is_production=False, postgres_auth_enabled=True),
    ],
    ids=["production", "postgres-auth"],
)
def test_direct_agent_auth_call_fails_closed_without_sqlite(
    monkeypatch: pytest.MonkeyPatch,
    settings: SimpleNamespace,
) -> None:
    class ForbiddenSQLiteRepository:
        def authenticate_and_consume(self, **_kwargs):
            pytest.fail("SQLite authentication repository must not be selected")

    monkeypatch.setattr(agent_auth, "get_settings", lambda: settings)
    monkeypatch.setattr(
        agent_auth, "agent_auth_repository", ForbiddenSQLiteRepository()
    )

    with pytest.raises(HTTPException) as error:
        agent_auth.require_external_agent(
            x_aixion_agent_id="agent-1",
            x_aixion_agent_token="secret",
            http_request=None,
        )

    assert error.value.status_code == 503
    assert error.value.headers == {"Retry-After": "5"}


def test_direct_agent_auth_call_remains_available_in_local_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class LocalRepository:
        def authenticate_and_consume(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                agent=None, credential=None, audit_event=None,
                reason="missing_credentials", retry_after_seconds=None,
            )

    monkeypatch.setattr(
        agent_auth,
        "get_settings",
        lambda: SimpleNamespace(is_production=False, postgres_auth_enabled=False),
    )
    monkeypatch.setattr(agent_auth, "agent_auth_repository", LocalRepository())

    with pytest.raises(HTTPException) as error:
        agent_auth.require_external_agent(http_request=None)

    assert error.value.status_code == 401
    assert len(calls) == 1


def test_fastapi_dependency_path_supplies_request_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = SimpleNamespace(id="agent-1")
    observed_requests = []

    class RequestBoundRepository:
        def authenticate_and_consume(self, **_kwargs):
            return SimpleNamespace(
                agent=agent, credential=None, audit_event=None,
                reason=None, retry_after_seconds=None,
            )

    def select_repository(request):
        observed_requests.append(request)
        return RequestBoundRepository()

    monkeypatch.setattr(
        agent_auth,
        "get_settings",
        lambda: SimpleNamespace(is_production=True, postgres_auth_enabled=False),
    )
    monkeypatch.setattr(
        agent_auth, "external_agent_auth_repository_for_request", select_repository
    )
    app = FastAPI()

    @app.get("/agent")
    def agent_route(authenticated_agent=Depends(agent_auth.require_external_agent)):
        return {"id": authenticated_agent.id}

    response = TestClient(app).get(
        "/agent",
        headers={"X-Aixion-Agent-Id": "agent-1", "X-Aixion-Agent-Token": "secret"},
    )

    assert response.status_code == 200
    assert response.json() == {"id": "agent-1"}
    assert len(observed_requests) == 1
    assert observed_requests[0] is not None


@pytest.mark.parametrize(
    "settings",
    [
        SimpleNamespace(is_production=True, postgres_auth_enabled=False),
        SimpleNamespace(is_production=False, postgres_auth_enabled=True),
    ],
    ids=["production", "postgres-auth"],
)
@pytest.mark.parametrize("operation", ["create", "get", "rotate", "revoke"])
def test_direct_agent_management_routes_fail_closed_without_request(
    monkeypatch: pytest.MonkeyPatch,
    settings: SimpleNamespace,
    operation: str,
) -> None:
    class ForbiddenStore:
        def __getattr__(self, name: str):
            pytest.fail(f"SQLite store access must not occur: {name}")

    monkeypatch.setattr(agent_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(agent_routes, "store", ForbiddenStore())
    monkeypatch.setattr(
        agent_routes,
        "register_external_agent",
        lambda *_args, **_kwargs: pytest.fail("agent registration must not run"),
    )
    monkeypatch.setattr(
        agent_routes,
        "external_agent_auth_repository_for_request",
        lambda *_args, **_kwargs: pytest.fail("request repository selector must not run"),
    )
    actor = AuthUser(
        id="owner-1", email="owner@example.test", display_name="Owner", role=UserRole.OWNER
    )
    payload = AgentCreateWithCredentialPolicy(
        provider=AgentProvider.ANTIGRAVITY,
        display_name="Antigravity",
    )

    with pytest.raises(HTTPException) as error:
        if operation == "create":
            agent_routes.create_agent(payload, actor, None)
        elif operation == "get":
            agent_routes.get_agent("agent-1", actor, None)
        elif operation == "rotate":
            agent_routes.rotate_agent_token("agent-1", None, actor, None)
        else:
            agent_routes.revoke_agent_token("agent-1", actor, None)

    assert error.value.status_code == 503
    assert error.value.headers == {"Retry-After": "1"}


def test_direct_agent_lookup_remains_available_in_local_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = SimpleNamespace(id="agent-1", workspace_id=None)
    monkeypatch.setattr(
        agent_routes,
        "get_settings",
        lambda: SimpleNamespace(is_production=False, postgres_auth_enabled=False),
    )
    monkeypatch.setattr(
        agent_routes,
        "store",
        SimpleNamespace(external_agents={"agent-1": agent}),
    )
    monkeypatch.setattr(agent_routes, "external_agent_scope_is_consistent", lambda _agent: True)

    selected, credential = agent_routes._get_agent_or_404("agent-1")

    assert selected is agent
    assert credential is None
