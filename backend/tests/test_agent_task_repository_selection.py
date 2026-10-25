from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import agent_routes


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
