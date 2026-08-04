from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from fastapi.testclient import TestClient

from app.agent_run_models import (
    AgentRun,
    AgentRunStatus,
    AgentRunStep,
    AgentRunStepStatus,
)
from app.agent_task_models import AgentTask, AgentTaskStatus
from app.main import app
from app.models import (
    AgentProvider,
    ApprovalRequest,
    ApprovalStatus,
    FileChange,
    Project,
    RiskAssessment,
    RiskLevel,
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
    assert lease_events[-1]["metadata"]["lease_seconds"] == 1500


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
