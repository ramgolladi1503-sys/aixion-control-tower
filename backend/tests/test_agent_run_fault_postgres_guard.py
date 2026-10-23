from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

import pytest
from fastapi.testclient import TestClient

from app.agent_run_models import AgentRunFaultConfig, AgentRunFaultKind
from app.main import app
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
