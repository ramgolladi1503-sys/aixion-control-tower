from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import agent_run_trust
from app.agent_run_models import AgentRunStepStatus, AgentRunStepType


class _PoisonProcessLocalStore:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"process-local store access occurred: {name}")


@pytest.mark.parametrize(
    "authority_settings",
    [
        SimpleNamespace(is_production=True, postgres_auth_enabled=False, trust_enforcement=True),
        SimpleNamespace(is_production=False, postgres_auth_enabled=True, trust_enforcement=True),
    ],
    ids=["production", "postgres-auth"],
)
@pytest.mark.parametrize("operation", ["attach", "authorize", "consume"])
def test_agent_run_trust_helpers_fail_before_process_local_store_access(
    monkeypatch: pytest.MonkeyPatch,
    authority_settings: SimpleNamespace,
    operation: str,
) -> None:
    monkeypatch.setattr(agent_run_trust, "get_settings", lambda: authority_settings)
    monkeypatch.setattr(agent_run_trust, "store", _PoisonProcessLocalStore())

    with pytest.raises(
        agent_run_trust.AgentRunTrustConflict,
        match="app-bound durable PostgreSQL path",
    ):
        if operation == "attach":
            agent_run_trust.attach_capability_lease(
                object(), "lease-id", actor="owner@example.test"
            )
        elif operation == "authorize":
            agent_run_trust.authorize_run_step(
                object(),
                SimpleNamespace(step_type=AgentRunStepType.CREATE_BRANCH),
                actor="owner@example.test",
                timeout_seconds=30,
            )
        else:
            agent_run_trust.consume_run_step_actions(
                [object()],
                SimpleNamespace(
                    status=AgentRunStepStatus.SUCCEEDED,
                    duration_ms=1_000,
                ),
                actor="owner@example.test",
            )
