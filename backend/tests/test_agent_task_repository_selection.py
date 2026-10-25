from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import agent_routes
from app.models import AgentProvider, ExternalAgent


def test_agent_task_request_selection_fails_closed_when_postgres_bundle_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sqlite_fallback = object()
    monkeypatch.setattr(agent_routes, "agent_task_repository", sqlite_fallback)
    monkeypatch.setattr(
        "app.application_repository_access.get_settings",
        lambda: SimpleNamespace(postgres_auth_enabled=True),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    with pytest.raises(HTTPException) as raised:
        agent_routes._agent_task_repository_for_route(request)

    assert raised.value.status_code == 503
    assert raised.value.headers == {"Retry-After": "1"}
    assert raised.value.detail == "AgentTask repository is unavailable"


def test_agent_task_direct_local_adapter_selection_remains_explicit() -> None:
    repository = agent_routes._agent_task_repository_for_route(None)

    assert repository is agent_routes.agent_task_repository


@pytest.mark.parametrize(
    "settings",
    [
        SimpleNamespace(is_production=True, postgres_auth_enabled=False),
        SimpleNamespace(is_production=False, postgres_auth_enabled=True),
    ],
    ids=["production", "postgres-auth"],
)
def test_agent_task_missing_request_context_fails_before_store_or_audit(
    monkeypatch: pytest.MonkeyPatch,
    settings: SimpleNamespace,
) -> None:
    accessed: list[str] = []

    class Trap:
        def __getattr__(self, name: str):
            accessed.append(name)
            raise AssertionError(f"unexpected store access: {name}")

    monkeypatch.setattr(agent_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(agent_routes, "store", Trap())
    monkeypatch.setattr(
        agent_routes,
        "agent_task_repository",
        object(),
    )
    monkeypatch.setattr(agent_routes, "assert_agent_can", lambda *args, **kwargs: None)

    with pytest.raises(HTTPException) as raised:
        agent_routes.create_agent_task(
            payload=agent_routes.AgentTaskCreate(title="test", goal="test"),
            agent=SimpleNamespace(id="agent-1"),
            http_request=None,
        )

    assert raised.value.status_code == 503
    assert raised.value.headers == {"Retry-After": "1"}
    assert raised.value.detail == "AgentTask repository is unavailable"
    assert accessed == []


@pytest.mark.parametrize(
    ("settings", "repository_name"),
    [
        (SimpleNamespace(is_production=True, postgres_auth_enabled=False), "Approval"),
        (SimpleNamespace(is_production=False, postgres_auth_enabled=True), "Approval"),
        (SimpleNamespace(is_production=True, postgres_auth_enabled=False), "Project"),
        (SimpleNamespace(is_production=False, postgres_auth_enabled=True), "Project"),
        (SimpleNamespace(is_production=True, postgres_auth_enabled=False), "Work order"),
        (SimpleNamespace(is_production=False, postgres_auth_enabled=True), "Work order"),
    ],
    ids=[
        "approval-production", "approval-postgres-auth",
        "project-production", "project-postgres-auth",
        "work-order-production", "work-order-postgres-auth",
    ],
)
def test_agent_route_repository_helpers_require_request_in_authoritative_modes(
    monkeypatch: pytest.MonkeyPatch,
    settings: SimpleNamespace,
    repository_name: str,
) -> None:
    monkeypatch.setattr(agent_routes, "get_settings", lambda: settings)
    selector = {
        "Approval": agent_routes._approval_repository_for_route,
        "Project": agent_routes._project_repository_for_route,
        "Work order": agent_routes._work_order_repository_for_route,
    }[repository_name]

    with pytest.raises(HTTPException) as raised:
        selector(None)

    assert raised.value.status_code == 503
    assert raised.value.headers == {"Retry-After": "1"}
    assert raised.value.detail == f"{repository_name} repository is unavailable"


@pytest.mark.parametrize(
    "settings",
    [
        SimpleNamespace(is_production=True, postgres_auth_enabled=False),
        SimpleNamespace(is_production=False, postgres_auth_enabled=True),
    ],
    ids=["production", "postgres-auth"],
)
def test_create_agent_approval_without_request_fails_before_sqlite_access(
    monkeypatch: pytest.MonkeyPatch,
    settings: SimpleNamespace,
) -> None:
    accessed: list[str] = []

    class Trap:
        def __getattr__(self, name: str):
            accessed.append(name)
            raise AssertionError(f"unexpected local repository/store access: {name}")

    monkeypatch.setattr(agent_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(agent_routes, "approval_decision_repository", Trap())
    monkeypatch.setattr(agent_routes, "project_repository", Trap())
    monkeypatch.setattr(agent_routes, "store", Trap())

    with pytest.raises(HTTPException) as raised:
        agent_routes.create_agent_approval(
            payload=agent_routes.ApprovalRequestCreate(
                project_id="project-1",
                title="Run a harmless check",
                summary="Run a harmless check",
                target_branch="main",
                files=[],
                source_invocation_id="invocation-1",
            ),
            agent=SimpleNamespace(id="agent-1"),
            http_request=None,
        )

    assert raised.value.status_code == 503
    assert raised.value.headers == {"Retry-After": "1"}
    assert raised.value.detail == "Approval repository is unavailable"
    assert accessed == []


def test_agent_route_repository_helpers_keep_local_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        agent_routes,
        "get_settings",
        lambda: SimpleNamespace(is_production=False, postgres_auth_enabled=False),
    )

    assert (
        agent_routes._approval_repository_for_route(None)
        is agent_routes.approval_decision_repository
    )
    assert (
        agent_routes._project_repository_for_route(None)
        is agent_routes.project_repository
    )
    assert (
        agent_routes._work_order_repository_for_route(None)
        is agent_routes.store.work_order_repository
    )


@pytest.mark.parametrize(
    "settings",
    [
        SimpleNamespace(is_production=True, postgres_auth_enabled=False),
        SimpleNamespace(is_production=False, postgres_auth_enabled=True),
    ],
    ids=["production", "postgres-auth"],
)
def test_list_agents_without_request_fails_before_workspace_or_store_access(
    monkeypatch: pytest.MonkeyPatch,
    settings: SimpleNamespace,
) -> None:
    accessed: list[str] = []

    class Trap:
        @property
        def external_agents(self):
            accessed.append("external_agents")
            raise AssertionError("unexpected local agent store access")

    monkeypatch.setattr(agent_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(agent_routes, "store", Trap())
    monkeypatch.setattr(
        agent_routes,
        "workspace_id_for_user",
        lambda _user: accessed.append("workspace") or "workspace-1",
    )

    with pytest.raises(HTTPException) as raised:
        agent_routes.list_agents(SimpleNamespace(id="owner-1"), http_request=None)

    assert raised.value.status_code == 503
    assert raised.value.headers == {"Retry-After": "1"}
    assert raised.value.detail == "Agent repository is unavailable"
    assert accessed == []


def test_list_agents_retains_direct_local_adapter_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = ExternalAgent(
        id="agent-local-1",
        provider=AgentProvider.CODEX,
        display_name="Local test agent",
        workspace_id="workspace-1",
    )
    monkeypatch.setattr(
        agent_routes,
        "get_settings",
        lambda: SimpleNamespace(is_production=False, postgres_auth_enabled=False),
    )
    monkeypatch.setattr(agent_routes, "store", SimpleNamespace(external_agents={agent.id: agent}))
    monkeypatch.setattr(agent_routes, "workspace_id_for_user", lambda _user: "workspace-1")
    monkeypatch.setattr(agent_routes, "external_agent_scope_is_consistent", lambda _agent: True)

    listed = agent_routes.list_agents(SimpleNamespace(id="owner-1"), http_request=None)

    assert [item.id for item in listed] == [agent.id]
