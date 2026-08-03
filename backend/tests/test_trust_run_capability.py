from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")
os.environ.setdefault("AIXION_PROFILE", "test")
os.environ.setdefault("AIXION_LEASE_SIGNING_KEY", "run-capability-test-signing-key")

from fastapi.testclient import TestClient

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
from app.trust_models import CapabilityLeaseMode

client = TestClient(app)


def setup_function() -> None:
    store.reset()


def _seed_approved_task() -> AgentTask:
    project = Project(name="Automatic capability", description="trust enforcement")
    store.projects[project.id] = project
    approval = ApprovalRequest(
        project_id=project.id,
        title="Approved strict run",
        summary="Modify one exact file and run one exact test.",
        agent_name="codex",
        target_branch="feature/automatic-capability",
        files=[
            FileChange(
                path="backend/app/automatic_capability.py",
                change_type="create",
                diff="+enabled",
                new_content="enabled = True\n",
            )
        ],
        test_plan=[
            "python -m pytest backend/tests/test_automatic_capability.py"
        ],
        rollback_plan="Close the feature pull request.",
        risk=RiskAssessment(level=RiskLevel.HIGH),
        status=ApprovalStatus.APPROVED,
        created_by_user_id="creator-user",
        approved_by_user_id="reviewer-user",
        approved_payload_hash="sealed-automatic-capability-payload",
    )
    store.approval_requests[approval.id] = approval
    task = AgentTask(
        provider=AgentProvider.CODEX,
        project_id=project.id,
        title="Automatic strict capability",
        goal="Create one evidence-backed feature pull request.",
        repository="owner/repo",
        branch_preference="feature/automatic-capability",
        approval_request_id=approval.id,
        status=AgentTaskStatus.APPROVED,
    )
    store.agent_tasks[task.id] = task
    store.persist()
    return task


def test_first_execution_issues_and_attaches_exact_strict_capability(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AIXION_TRUST_ENFORCEMENT", "true")
    task = _seed_approved_task()
    created = client.post("/agent/runs", json={"task_id": task.id})
    assert created.status_code == 200
    run_id = created.json()["run"]["id"]
    assert created.json()["run"]["capability_lease_id"] is None

    executed = client.post(
        f"/agent/runs/{run_id}/execute-next",
        json={
            "worker_id": "capability-test-worker",
            "lease_seconds": 300,
            "timeout_seconds": 120,
        },
    )
    assert executed.status_code == 200, executed.text
    run = store.agent_runs[run_id]
    assert run.capability_lease_id
    capability = store.capability_leases[run.capability_lease_id]
    assert capability.mode == CapabilityLeaseMode.STRICT
    assert capability.scope.repository == "owner/repo"
    assert capability.scope.branch == "feature/automatic-capability"
    assert capability.scope.allowed_path_prefixes == [
        "backend/app/automatic_capability.py"
    ]
    assert capability.scope.allowed_commands == [
        "python -m pytest backend/tests/test_automatic_capability.py"
    ]
    assert capability.scope.allow_auto_merge is False


def test_existing_run_capability_is_not_reissued(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_TRUST_ENFORCEMENT", "true")
    task = _seed_approved_task()
    run_id = client.post("/agent/runs", json={"task_id": task.id}).json()["run"]["id"]

    first = client.post(
        f"/agent/runs/{run_id}/execute-next",
        json={"worker_id": "first-worker", "timeout_seconds": 120},
    )
    assert first.status_code == 200
    first_lease_id = store.agent_runs[run_id].capability_lease_id
    assert first_lease_id

    second = client.post(
        f"/agent/runs/{run_id}/execute-next",
        json={"worker_id": "second-worker", "timeout_seconds": 120},
    )
    assert second.status_code == 409
    assert "Exact action approval" in second.json()["detail"]
    assert store.agent_runs[run_id].capability_lease_id == first_lease_id
    assert len(store.capability_leases) == 1
