from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import agent_auth
from app import agent_routes
from app import workspace_access
from app.agent_credential_models import AgentCreateWithCredentialPolicy, AgentCredentialRecord
from app.models import AgentAuthType, AgentProvider, AuthUser, ExternalAgent, UserRole


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


@pytest.mark.parametrize(
    "settings",
    [
        SimpleNamespace(is_production=True, postgres_auth_enabled=False),
        SimpleNamespace(is_production=False, postgres_auth_enabled=True),
    ],
    ids=["production", "postgres-auth"],
)
@pytest.mark.parametrize("operation", ["register", "rotate", "revoke"])
def test_external_agent_credential_services_reject_sqlite_authority(
    monkeypatch: pytest.MonkeyPatch,
    settings: SimpleNamespace,
    operation: str,
) -> None:
    class ForbiddenStore:
        def __getattr__(self, name: str):
            pytest.fail(f"local store must not be accessed: {name}")

    monkeypatch.setattr(agent_auth, "get_settings", lambda: settings)
    monkeypatch.setattr(agent_auth, "store", ForbiddenStore())
    actor = AuthUser(
        id="owner-1", email="owner@example.test", display_name="Owner", role=UserRole.OWNER
    )
    agent = ExternalAgent(
        id="agent-1",
        provider=AgentProvider.ANTIGRAVITY,
        display_name="Antigravity",
        auth_type=AgentAuthType.API_KEY,
        created_by_user_id=actor.id,
        workspace_id="workspace-1",
        secret_hash="old-hash",
    )
    credential = AgentCredentialRecord(agent_id=agent.id)
    before_agent = agent.model_copy(deep=True)
    before_credential = credential.model_copy(deep=True)
    local_repository = agent_auth.agent_auth_repository

    with pytest.raises(HTTPException) as error:
        if operation == "register":
            agent_auth.register_external_agent(
                AgentCreateWithCredentialPolicy(
                    provider=AgentProvider.ANTIGRAVITY,
                    display_name="New agent",
                ),
                actor,
            )
        elif operation == "rotate":
            agent_auth.rotate_external_agent_token(
                agent, credential=credential, repository=local_repository
            )
        else:
            agent_auth.revoke_external_agent_token(
                agent, credential=credential, repository=local_repository
            )

    assert error.value.status_code == 503
    assert error.value.headers == {"Retry-After": "1"}
    assert agent == before_agent
    assert credential == before_credential


def test_external_agent_credential_services_keep_local_profile_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        agent_auth,
        "get_settings",
        lambda: SimpleNamespace(is_production=False, postgres_auth_enabled=False),
    )
    agent = ExternalAgent(
        id="agent-1",
        provider=AgentProvider.ANTIGRAVITY,
        display_name="Antigravity",
        auth_type=AgentAuthType.API_KEY,
        secret_hash="old-hash",
    )
    credential = AgentCredentialRecord(agent_id=agent.id)

    rotated = agent_auth.rotate_external_agent_token(
        agent, credential=credential, repository=None
    )

    assert rotated.agent_token
    assert agent.secret_hash != "old-hash"
    assert agent_auth.revoke_external_agent_token(
        agent, credential=credential, repository=None
    ).enabled
    assert agent.secret_hash is None


@pytest.mark.parametrize(
    "settings",
    [
        SimpleNamespace(is_production=True, postgres_auth_enabled=False),
        SimpleNamespace(is_production=False, postgres_auth_enabled=True),
    ],
    ids=["production", "postgres-auth"],
)
def test_agent_authorization_without_request_fails_before_sqlite_scope_reads(
    monkeypatch: pytest.MonkeyPatch,
    settings: SimpleNamespace,
) -> None:
    class ForbiddenRepository:
        def __getattr__(self, name: str):
            pytest.fail(f"SQLite scope repository access must not occur: {name}")

    agent = ExternalAgent(
        id="agent-1",
        provider=AgentProvider.ANTIGRAVITY,
        display_name="Antigravity",
        auth_type=AgentAuthType.API_KEY,
        created_by_user_id="owner-1",
        workspace_id="workspace-1",
        allowed_project_ids=["project-1"],
        allowed_actions=["CREATE_APPROVAL"],
        secret_hash="agent-secret-hash",
    )
    monkeypatch.setattr(agent_auth, "get_settings", lambda: settings)
    monkeypatch.setattr(agent_auth, "project_repository", ForbiddenRepository())
    monkeypatch.setattr(workspace_access, "project_repository", ForbiddenRepository())
    monkeypatch.setattr(
        workspace_access, "workspace_membership_repository", ForbiddenRepository()
    )

    with pytest.raises(HTTPException) as error:
        agent_auth.assert_agent_can(
            agent,
            agent_auth.AgentAction.CREATE_APPROVAL,
            project_id="project-1",
            http_request=None,
        )

    assert error.value.status_code == 503
    assert error.value.headers == {"Retry-After": "1"}


def test_agent_authorization_without_request_keeps_local_scope_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = ExternalAgent(
        id="agent-1",
        provider=AgentProvider.ANTIGRAVITY,
        display_name="Antigravity",
        auth_type=AgentAuthType.API_KEY,
        created_by_user_id="owner-1",
        workspace_id="workspace-1",
        allowed_project_ids=["project-1"],
        allowed_actions=["CREATE_APPROVAL"],
        secret_hash="agent-secret-hash",
    )
    project_repository = SimpleNamespace(
        get=lambda project_id: SimpleNamespace(
            id=project_id, workspace_id="workspace-1"
        )
    )
    membership_repository = SimpleNamespace(
        get_workspace=lambda workspace_id: SimpleNamespace(id=workspace_id)
    )
    monkeypatch.setattr(
        agent_auth,
        "get_settings",
        lambda: SimpleNamespace(is_production=False, postgres_auth_enabled=False),
    )
    monkeypatch.setattr(agent_auth, "project_repository", project_repository)
    monkeypatch.setattr(workspace_access, "project_repository", project_repository)
    monkeypatch.setattr(
        workspace_access, "workspace_membership_repository", membership_repository
    )
    monkeypatch.setattr(agent_auth, "external_agent_scope_is_consistent", lambda *_args, **_kwargs: True)

    agent_auth.assert_agent_can(
        agent,
        agent_auth.AgentAction.CREATE_APPROVAL,
        project_id="project-1",
        http_request=None,
    )
