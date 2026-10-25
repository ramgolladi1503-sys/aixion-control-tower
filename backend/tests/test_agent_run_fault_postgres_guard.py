from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.agent_run_models import (
    AgentRunFaultConfig,
    AgentRunFaultCreate,
    AgentRunFaultKind,
)
from app import agent_run_routes
from app.main import app
from app.models import AuthUser, UserRole
from app.store import store

client = TestClient(app)


def setup_function() -> None:
    store.reset()


@pytest.mark.parametrize("selected_state", ["postgres_runtime", "postgres_repositories"])
def test_fault_admin_routes_fail_closed_when_postgres_is_selected(
    monkeypatch: pytest.MonkeyPatch,
    selected_state: str,
) -> None:
    fault = AgentRunFaultConfig(
        id="agent_run_fault_postgres_guard_test",
        run_id="local-run-must-not-be-read",
        fault_kind=AgentRunFaultKind.GITHUB_TIMEOUT,
        enabled=True,
        remaining_uses=1,
    )
    store.agent_run_faults[fault.id] = fault
    before_faults = {
        fault_id: value.model_copy(deep=True)
        for fault_id, value in store.agent_run_faults.items()
    }
    before_audit_events = list(store.audit_events)
    persist_calls: list[bool] = []
    monkeypatch.setattr(store, "persist", lambda: persist_calls.append(True))
    monkeypatch.setattr(app.state, selected_state, object(), raising=False)
    monkeypatch.setattr(
        "app.agent_run_routes.get_settings",
        lambda: SimpleNamespace(is_production=False, postgres_auth_enabled=True),
    )

    responses = [
        client.get("/agent/runs/faults"),
        client.post(
            "/agent/runs/faults",
            json={
                "run_id": "postgres-run-not-in-local-store",
                "fault_kind": "GITHUB_TIMEOUT",
                "remaining_uses": 1,
            },
        ),
        client.delete(f"/agent/runs/faults/{fault.id}"),
    ]

    for response in responses:
        assert response.status_code == 503
        assert "durable PostgreSQL" in response.json()["detail"]
    assert store.agent_run_faults == before_faults
    assert store.audit_events == before_audit_events
    assert persist_calls == []


def test_fault_admin_direct_calls_fail_closed_without_request_or_postgres_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = AuthUser(
        id="owner-postgres-fault-direct",
        email="owner@example.test",
        display_name="Owner",
        role=UserRole.OWNER,
        workspace_id="workspace-postgres-fault-direct",
    )
    payload = AgentRunFaultCreate(
        run_id="must-not-read-local-run",
        fault_kind=AgentRunFaultKind.GITHUB_TIMEOUT,
        remaining_uses=1,
    )
    fault = AgentRunFaultConfig(
        id="must-not-read-local-fault",
        run_id="must-not-read-local-run",
        fault_kind=AgentRunFaultKind.GITHUB_TIMEOUT,
        enabled=True,
        remaining_uses=1,
    )
    store.agent_run_faults[fault.id] = fault
    before = {
        fault_id: value.model_copy(deep=True)
        for fault_id, value in store.agent_run_faults.items()
    }
    persist_calls: list[bool] = []
    monkeypatch.setattr(store, "persist", lambda: persist_calls.append(True))
    monkeypatch.setattr(
        agent_run_routes,
        "get_settings",
        lambda: SimpleNamespace(is_production=False, postgres_auth_enabled=True),
    )

    direct_calls = (
        lambda: agent_run_routes.list_faults(user=actor),
        lambda: agent_run_routes.create_fault(payload=payload, user=actor),
        lambda: agent_run_routes.disable_fault(fault_id=fault.id, user=actor),
    )
    for call in direct_calls:
        with pytest.raises(HTTPException) as error:
            call()
        assert error.value.status_code == 503
        assert "durable PostgreSQL" in str(error.value.detail)

    assert store.agent_run_faults == before
    assert store.audit_events == []
    assert persist_calls == []


def test_probe_only_postgres_runtime_keeps_sqlite_fault_controls_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        agent_run_routes,
        "get_settings",
        lambda: SimpleNamespace(is_production=False, postgres_auth_enabled=False),
    )
    monkeypatch.setattr(app.state, "postgres_runtime", object(), raising=False)

    assert agent_run_routes.list_faults(
        user=AuthUser(
            id="owner-probe-only",
            email="owner@example.test",
            display_name="Owner",
            role=UserRole.OWNER,
            workspace_id="workspace-probe-only",
        )
    ) == []
