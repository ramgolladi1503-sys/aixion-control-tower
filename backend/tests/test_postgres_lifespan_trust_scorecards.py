from __future__ import annotations

import uuid
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import main
from app.application_repositories import ApplicationRepositories
from app.models import AgentProvider, RiskLevel
from app.settings import get_settings
from app.store import store
from app.trust_models import (
    CapabilityActionType,
    PolicyDecision,
    PolicyDecisionType,
    ProposedAction,
)

pytest_plugins = ("test_postgres_lifespan_approval_inbox",)


def test_lifespan_scorecard_route_reads_its_bound_postgres_snapshot(
    postgres_lifespan_approval_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, workspace, _project, _user, _agent, _agent_token = (
        postgres_lifespan_approval_context
    )
    repositories = ApplicationRepositories.from_connector(connect)
    action = ProposedAction(
        id=f"action_{uuid.uuid4().hex}",
        provider=AgentProvider.CODEX,
        agent_id="lifespan-codex-installation",
        workspace_id=workspace.id,
        action_type=CapabilityActionType.RUN_COMMAND,
        command="python -m pytest",
    )
    decision = PolicyDecision(
        proposed_action_id=action.id,
        decision=PolicyDecisionType.ALLOW,
        risk_level=RiskLevel.LOW,
        reasons=["Within the approved capability."],
    )
    with connect() as conn:
        with conn.transaction():
            repositories.trust.insert_proposed_action(conn, action)
            repositories.trust.insert_policy_decision(
                conn, decision, workspace_id=workspace.id
            )

    settings = replace(
        get_settings(),
        profile="test",
        auth_enabled=True,
        postgres_auth_enabled=True,
        postgres_runtime_probe_enabled=False,
        postgres_auth_rate_limits_enabled=False,
        outbox_dispatch_mode="scheduled",
        push_dispatch_enabled=False,
        email_dispatch_enabled=False,
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    # The real lifespan must install every PostgreSQL binding. Remove the test
    # application's pre-existing local adapters so they cannot mask a missing
    # lifespan binding.
    for state_name in main._POSTGRES_APP_STATE_BINDINGS:
        monkeypatch.setattr(main.app.state, state_name, None, raising=False)

    def runtime_connect():
        connection = connect()
        # The test connector sets search_path before returning. Commit that
        # setup transaction so the route can set its own REPEATABLE READ mode.
        connection.commit()
        return connection

    runtime = main.PostgresRuntime(
        _dsn="isolated-test-schema",
        profile="test",
        settings=settings,
        _connector=lambda *_args, **_kwargs: runtime_connect(),
    )
    monkeypatch.setattr(main.PostgresRuntime, "from_environment", lambda: runtime)

    before_actions = set(store.proposed_actions)
    with TestClient(main.app) as client:
        runtime = client.app.state.postgres_runtime
        active_repositories = runtime.repositories
        assert client.app.state.postgres_repositories is active_repositories
        assert client.app.state.trust_repository is active_repositories.trust
        assert client.app.state.agent_task_repository is active_repositories.agent_tasks
        assert client.app.state.agent_run_repository is active_repositories.agent_runs

        response = client.get("/trust/scorecards")

    assert response.status_code == 200, response.text
    scorecards = response.json()
    card = next(
        item
        for item in scorecards
        if item["provider"] == AgentProvider.CODEX.value
        and item["agent_id"] == action.agent_id
    )
    assert card["evaluated_actions"] == 1
    assert card["allowed_actions"] == 1
    assert card["scope_adherence_rate"] == 1.0
    assert set(store.proposed_actions) == before_actions

