from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.agent_run_models import (
    AgentRun,
    AgentRunStatus,
    AgentRunStep,
    AgentRunStepStatus,
)
from app import agent_run_routes
from app.agent_task_models import AgentTask, AgentTaskStatus
from app.approval_integrity import compute_approval_payload_hash
from app.main import app
from app.models import (
    AgentProvider,
    ApprovalRequest,
    ApprovalStatus,
    FileChange,
    Project,
    RiskAssessment,
    RiskLevel,
    AuthUser,
    UserRole,
)
from app.store import store

client = TestClient(app)


def setup_function() -> None:
    store.reset()


def _seed_approved_task() -> AgentTask:
    project = Project(name="Mission Control API", description="test")
    store.projects[project.id] = project
    approval = ApprovalRequest(
        project_id=project.id,
        title="Approved run",
        summary="safe",
        agent_name="codex",
        target_branch="feature/mission-control-api",
        files=[
            FileChange(
                path="docs/run.md",
                change_type="create",
                diff="+run",
                new_content="run\n",
            )
        ],
        test_plan=["python -m pytest"],
        rollback_plan="Close PR.",
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.APPROVED,
    )
    approval.approved_payload_hash = compute_approval_payload_hash(approval)
    store.approval_requests[approval.id] = approval
    task = AgentTask(
        provider=AgentProvider.CODEX,
        project_id=project.id,
        title="Mission Control API run",
        goal="Expose run truth.",
        repository="ramgolladi1503-sys/aixion-control-tower",
        branch_preference="feature/mission-control-api",
        approval_request_id=approval.id,
        status=AgentTaskStatus.APPROVED,
    )
    store.agent_tasks[task.id] = task
    store.persist()
    return task


def _create_run() -> tuple[str, AgentRun, AgentRunStep]:
    task = _seed_approved_task()
    payload = client.post("/agent/runs", json={"task_id": task.id}).json()
    run_id = payload["run"]["id"]
    run = store.agent_runs[run_id]
    step = store.agent_run_steps[run.current_step_id]
    return run_id, run, step


def test_create_list_detail_summary_and_metrics_routes() -> None:
    task = _seed_approved_task()
    created = client.post("/agent/runs", json={"task_id": task.id})
    assert created.status_code == 200
    payload = created.json()
    run_id = payload["run"]["id"]
    assert len(payload["steps"]) == 7

    listed = client.get("/agent/runs")
    detail = client.get(f"/agent/runs/{run_id}")
    summary = client.get("/agent/runs/summary")
    metrics = client.get("/agent/runs/metrics")

    assert listed.status_code == 200 and listed.json()[0]["id"] == run_id
    assert detail.status_code == 200 and detail.json()["run"]["task_id"] == task.id
    assert summary.status_code == 200 and summary.json()["total"] == 1
    assert metrics.status_code == 200
    assert "aixion_agent_runs_total" in metrics.text

    # HTTP read routes must use the app-bound durable repository, not process maps.
    store.agent_runs.clear()
    store.agent_run_steps.clear()
    store.agent_run_events.clear()
    assert client.get("/agent/runs").json()[0]["id"] == run_id
    durable_detail = client.get(f"/agent/runs/{run_id}")
    durable_steps = client.get(f"/agent/runs/{run_id}/steps")
    durable_events = client.get(f"/agent/runs/{run_id}/events")
    assert durable_detail.status_code == 200
    assert durable_detail.json()["run"]["id"] == run_id
    assert len(durable_detail.json()["steps"]) == 7
    assert durable_steps.status_code == 200 and len(durable_steps.json()) == 7
    assert durable_events.status_code == 200 and durable_events.json()
    durable_summary = client.get("/agent/runs/summary")
    assert durable_summary.status_code == 200
    assert durable_summary.json()["total"] == 1
    assert durable_summary.json()["queue_depth"] == 7
    paused = client.post(f"/agent/runs/{run_id}/pause", json={"reason": "route test"})
    assert paused.status_code == 200 and paused.json()["run"]["status"] == "PAUSED"
    resumed = client.post(f"/agent/runs/{run_id}/resume", json={"reason": "route test"})
    assert resumed.status_code == 200 and resumed.json()["run"]["status"] == "SCHEDULED"
    cancelled = client.post(f"/agent/runs/{run_id}/cancel", json={"reason": "route test"})
    assert cancelled.status_code == 200 and cancelled.json()["run"]["status"] == "CANCELLED"


@pytest.mark.parametrize("authoritative_mode", ["production", "postgres_auth"])
@pytest.mark.parametrize(
    "invoke",
    [
        lambda user: agent_run_routes.list_runs(user=user),
        lambda user: agent_run_routes.get_run_summary(user=user),
        lambda user: agent_run_routes.get_run("missing-run", user=user),
        lambda user: agent_run_routes.list_run_steps("missing-run", user=user),
        lambda user: agent_run_routes.list_run_events("missing-run", user=user),
        lambda user: agent_run_routes.recover_stale_run_leases(user=user),
        lambda user: agent_run_routes.bind_run_capability_lease(
            "missing-run",
            agent_run_routes.AgentRunCapabilityLeaseRequest(capability_lease_id="lease"),
            user=user,
        ),
        lambda user: agent_run_routes.heartbeat_agent_run(
            "missing-run",
            agent_run_routes.AgentRunHeartbeatRequest(worker_id="worker", lease_token="token"),
            user=user,
        ),
        lambda user: agent_run_routes.pause_run(
            "missing-run", agent_run_routes.AgentRunControlRequest(), user=user
        ),
        lambda user: agent_run_routes.resume_run(
            "missing-run", agent_run_routes.AgentRunControlRequest(), user=user
        ),
        lambda user: agent_run_routes.cancel_run(
            "missing-run", agent_run_routes.AgentRunControlRequest(), user=user
        ),
        lambda user: agent_run_routes.retry_run(
            "missing-run", agent_run_routes.AgentRunRetryRequest(), user=user
        ),
    ],
    ids=[
        "list",
        "summary",
        "detail",
        "steps",
        "events",
        "lease-recovery",
        "capability",
        "heartbeat",
        "pause",
        "resume",
        "cancel",
        "retry",
    ],
)
def test_run_routes_require_app_request_in_authoritative_modes(
    monkeypatch: pytest.MonkeyPatch,
    authoritative_mode: str,
    invoke,
) -> None:
    monkeypatch.setattr(
        agent_run_routes,
        "get_settings",
        lambda: SimpleNamespace(
            is_production=authoritative_mode == "production",
            postgres_auth_enabled=authoritative_mode == "postgres_auth",
        ),
    )
    monkeypatch.setattr(
        agent_run_routes,
        "agent_run_repository_for_request",
        lambda _request: pytest.fail("repository selection must not run without Request"),
    )
    monkeypatch.setattr(
        agent_run_routes,
        "_run_or_404",
        lambda *_args, **_kwargs: pytest.fail("SQLite run lookup must not run"),
    )
    monkeypatch.setattr(
        agent_run_routes,
        "recover_stale_leases",
        lambda **_kwargs: pytest.fail("SQLite lease recovery must not run"),
    )
    monkeypatch.setattr(
        agent_run_routes,
        "build_agent_run_summary",
        lambda *_args, **_kwargs: pytest.fail("SQLite summary read must not run"),
    )

    class ForbiddenLocalMap(dict):
        def values(self):
            pytest.fail("SQLite AgentRun listing must not run")

    monkeypatch.setattr(agent_run_routes.store, "agent_runs", ForbiddenLocalMap())
    user = AuthUser(
        id="owner",
        email="owner@example.test",
        display_name="Owner",
        role=UserRole.OWNER,
    )

    with pytest.raises(HTTPException) as error:
        invoke(user)

    assert error.value.status_code == 503
    assert error.value.headers == {"Retry-After": "1"}
    assert "app-bound request" in error.value.detail


@pytest.mark.parametrize(
    ("postgres_runtime", "postgres_run_repository", "notification_repository"),
    [
        (object(), None, None),
        (None, object(), None),
        (None, None, object()),
    ],
)
def test_metrics_fail_closed_when_postgres_authority_is_selected(
    monkeypatch: pytest.MonkeyPatch,
    postgres_runtime: object | None,
    postgres_run_repository: object | None,
    notification_repository: object | None,
) -> None:
    if postgres_runtime is not None:
        monkeypatch.setattr(app.state, "postgres_runtime", postgres_runtime, raising=False)
    if postgres_run_repository is not None:
        monkeypatch.setattr(
            app.state,
            "agent_run_repository",
            postgres_run_repository,
        )
    if notification_repository is not None:
        monkeypatch.setattr(
            app.state,
            "notification_outbox_repository",
            notification_repository,
            raising=False,
        )

    response = client.get("/agent/runs/metrics")

    assert response.status_code == 503
    assert "same durable runtime" in response.json()["detail"]
    assert "aixion_agent_runs_total" not in response.text


def test_fault_admin_routes_are_explicit_and_disableable() -> None:
    task = _seed_approved_task()
    run_id = client.post("/agent/runs", json={"task_id": task.id}).json()["run"]["id"]

    created = client.post(
        "/agent/runs/faults",
        json={
            "run_id": run_id,
            "step_type": "CREATE_BRANCH",
            "fault_kind": "GITHUB_TIMEOUT",
            "remaining_uses": 1,
        },
    )
    assert created.status_code == 200
    fault_id = created.json()["id"]
    assert created.json()["enabled"] is True

    disabled = client.delete(f"/agent/runs/faults/{fault_id}")
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False


def test_fault_admin_routes_are_hidden_in_production(monkeypatch) -> None:
    from types import SimpleNamespace

    from app import agent_run_routes

    task = _seed_approved_task()
    run_id = client.post("/agent/runs", json={"task_id": task.id}).json()["run"]["id"]
    monkeypatch.setattr(
        agent_run_routes,
        "get_settings",
        lambda: SimpleNamespace(is_production=True),
    )

    listed = client.get("/agent/runs/faults")
    created = client.post(
        "/agent/runs/faults",
        json={"run_id": run_id, "fault_kind": "GITHUB_TIMEOUT", "remaining_uses": 1},
    )

    assert listed.status_code == 404
    assert created.status_code == 404
    assert store.agent_run_faults == {}


def test_run_creation_rejects_unapproved_task() -> None:
    task = _seed_approved_task()
    task.status = AgentTaskStatus.PLANNING
    store.persist()
    response = client.post("/agent/runs", json={"task_id": task.id})
    assert response.status_code == 409
    assert "must be APPROVED" in str(response.json()["detail"])


def test_execute_route_expands_lease_to_cover_bounded_validation_budget() -> None:
    task = _seed_approved_task()
    run_id = client.post("/agent/runs", json={"task_id": task.id}).json()["run"]["id"]

    response = client.post(
        f"/agent/runs/{run_id}/execute-next",
        json={
            "worker_id": "lease-budget-test",
            "lease_seconds": 10,
            "timeout_seconds": 120,
        },
    )
    assert response.status_code == 200
    lease_events = [
        event
        for event in response.json()["events"]
        if event["event_type"] == "RUN_LEASE_ACQUIRED"
    ]
    assert lease_events
    # The lease covers five 120-second validation commands, their per-command
    # overhead, and the global safety margin.
    assert lease_events[-1]["metadata"]["lease_seconds"] == 1860


def test_execute_route_rehydrates_run_from_configured_repository() -> None:
    task = _seed_approved_task()
    run_id = client.post("/agent/runs", json={"task_id": task.id}).json()["run"]["id"]
    run = store.agent_runs.pop(run_id)
    repository = app.state.agent_run_repository
    durable_run = repository.get(run_id)
    if durable_run is None:
        # The test app and module store can use different SQLite paths under
        # isolated test configurations. Copy the committed fixture to the
        # app-bound adapter explicitly before removing its compatibility view.
        from app.approval_decisions import agent_run_repository

        repository.create(
            run=run.model_copy(deep=True),
            steps=agent_run_repository.list_steps_for_run(run_id),
            events=agent_run_repository.list_events_for_run(run_id),
            audit=next(
                event for event in store.audit_events
                if event.entity_id == run_id and event.event_type == "agent_run.created"
            ),
        )
    step_ids = [step.id for step in store.agent_run_steps.values() if step.run_id == run_id]
    event_ids = [event.id for event in store.agent_run_events.values() if event.run_id == run_id]
    for step_id in step_ids:
        store.agent_run_steps.pop(step_id)
    for event_id in event_ids:
        store.agent_run_events.pop(event_id)

    response = client.post(
        f"/agent/runs/{run_id}/execute-next",
        json={"worker_id": "repository-rehydration-test", "timeout_seconds": 30},
    )

    assert response.status_code == 200
    assert response.json()["run"]["id"] == run_id
    assert response.json()["steps"]
    assert store.agent_runs[run_id].status.value == "RUNNING"
    durable_run = repository.get(run_id)
    assert durable_run is not None
    durable_steps = repository.list_steps_for_run(run_id)
    assert durable_run.status.value == "RUNNING"
    assert durable_run.status.value in {"RUNNING", "SUCCEEDED", "NEEDS_HUMAN", "RETRY_WAIT"}
    assert durable_steps[0].status != AgentRunStepStatus.READY


def test_execute_route_rejects_timeout_that_cannot_fit_maximum_lease() -> None:
    task = _seed_approved_task()
    run_id = client.post("/agent/runs", json={"task_id": task.id}).json()["run"]["id"]

    response = client.post(
        f"/agent/runs/{run_id}/execute-next",
        json={
            "worker_id": "unsafe-timeout-test",
            "lease_seconds": 300,
            "timeout_seconds": 300,
        },
    )
    assert response.status_code == 422
    assert "Maximum safe value" in response.json()["detail"]


def test_execute_route_fails_closed_for_non_sqlite_runtime_before_global_trust_access(
    monkeypatch,
) -> None:
    from app import agent_run_routes

    run_id, run, step = _create_run()
    original = run.model_copy(deep=True)
    monkeypatch.setattr(
        agent_run_routes,
        "agent_run_repository_for_request",
        lambda _request: object(),
    )

    response = client.post(
        f"/agent/runs/{run_id}/execute-next",
        json={"worker_id": "postgres-guard-test", "timeout_seconds": 30},
    )

    assert response.status_code == 409
    assert "capability, trust, and worker effects" in response.json()["detail"]
    assert run == original
    assert step.status == AgentRunStepStatus.READY


def test_capability_binding_fails_closed_for_non_sqlite_runtime_before_store_access(
    monkeypatch,
) -> None:
    from app import agent_run_routes

    run_id, run, step = _create_run()
    original_run = run.model_copy(deep=True)
    original_step = step.model_copy(deep=True)
    monkeypatch.setattr(
        agent_run_routes,
        "agent_run_repository_for_request",
        lambda _request: object(),
    )

    response = client.post(
        f"/agent/runs/{run_id}/capability-lease",
        json={"capability_lease_id": "lease-not-used"},
    )

    assert response.status_code == 409
    assert "capability and trust records" in response.json()["detail"]
    assert run == original_run
    assert step == original_step


def test_mutating_controls_are_rejected_while_step_is_in_flight() -> None:
    run_id, run, step = _create_run()
    run.status = AgentRunStatus.RUNNING
    run.lease_owner = "active-worker"
    run.lease_token = "active-lease"
    step.status = AgentRunStepStatus.RUNNING
    step.lease_owner = "active-worker"
    step.lease_token = "active-lease"
    store.persist()

    execute = client.post(
        f"/agent/runs/{run_id}/execute-next",
        json={
            "worker_id": "duplicate-worker",
            "lease_seconds": 300,
            "timeout_seconds": 120,
        },
    )
    pause = client.post(f"/agent/runs/{run_id}/pause", json={"reason": "pause now"})
    cancel = client.post(f"/agent/runs/{run_id}/cancel", json={"reason": "cancel now"})

    assert execute.status_code == 409
    assert pause.status_code == 409
    assert cancel.status_code == 409
    assert "governed step is in flight" in execute.json()["detail"]
    assert "governed step is in flight" in pause.json()["detail"]
    assert "governed step is in flight" in cancel.json()["detail"]
    assert run.status == AgentRunStatus.RUNNING
    assert step.status == AgentRunStepStatus.RUNNING


def test_terminal_failed_run_requires_new_approval_instead_of_retry() -> None:
    run_id, run, step = _create_run()
    run.status = AgentRunStatus.FAILED
    step.status = AgentRunStepStatus.FAILED
    store.persist()

    response = client.post(
        f"/agent/runs/{run_id}/retry",
        json={"step_id": step.id, "reason": "retry terminal work"},
    )

    assert response.status_code == 409
    assert "Create a revised approval and a new run" in response.json()["detail"]
    assert run.status == AgentRunStatus.FAILED
    assert step.status == AgentRunStepStatus.FAILED
