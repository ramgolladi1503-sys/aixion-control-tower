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
