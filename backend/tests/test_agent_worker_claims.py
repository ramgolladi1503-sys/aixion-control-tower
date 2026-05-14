from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from app.agent_task_models import AgentTask, AgentTaskStatus
from app.agent_worker_claims import claim_agent_task_for_worker, claim_first_approved_agent_task_for_worker
from app.models import AgentProvider, Project
from app.store import store


def setup_function() -> None:
    store.reset()


def _approved_task(title: str = "Approved task") -> AgentTask:
    project = Project(name="Transactional Claim", description="demo")
    store.projects[project.id] = project
    task = AgentTask(
        provider=AgentProvider.CODEX,
        project_id=project.id,
        title=title,
        goal="Claim this task safely.",
        repository="ramgolladi1503-sys/aixion-control-tower",
        status=AgentTaskStatus.APPROVED,
        approval_request_id="approval_claim",
    )
    store.agent_tasks[task.id] = task
    store.persist()
    return task


def test_claim_agent_task_sets_lease_fields_transactionally() -> None:
    task = _approved_task()

    result = claim_agent_task_for_worker(task_id=task.id, worker_id="worker-a", lease_seconds=120)

    assert result.success is True
    assert result.task_id == task.id
    assert result.lease_token is not None
    assert result.lease_expires_at is not None
    assert result.task is not None
    assert result.task.worker_lease_owner == "worker-a"
    assert result.task.worker_lease_token == result.lease_token
    assert store.agent_tasks[task.id].worker_lease_owner == "worker-a"
    assert store.agent_tasks[task.id].worker_lease_token == result.lease_token

    store.load()
    persisted = store.agent_tasks[task.id]
    assert persisted.worker_lease_owner == "worker-a"
    assert persisted.worker_lease_token == result.lease_token


def test_claim_agent_task_refuses_active_lease() -> None:
    task = _approved_task()
    task.worker_lease_owner = "worker-a"
    task.worker_lease_token = "lease_active"
    task.worker_lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    store.persist()

    result = claim_agent_task_for_worker(task_id=task.id, worker_id="worker-b")

    assert result.success is False
    assert "already leased by worker-a" in result.reason
    assert store.agent_tasks[task.id].worker_lease_owner == "worker-a"
    assert store.agent_tasks[task.id].worker_lease_token == "lease_active"


def test_claim_agent_task_takes_expired_lease() -> None:
    task = _approved_task()
    task.worker_lease_owner = "old-worker"
    task.worker_lease_token = "lease_expired"
    task.worker_lease_expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    store.persist()

    result = claim_agent_task_for_worker(task_id=task.id, worker_id="new-worker")

    assert result.success is True
    assert result.lease_token is not None
    assert result.lease_token != "lease_expired"
    assert store.agent_tasks[task.id].worker_lease_owner == "new-worker"
    assert store.agent_tasks[task.id].worker_lease_token == result.lease_token


def test_claim_agent_task_refuses_non_approved_task() -> None:
    task = _approved_task()
    task.status = AgentTaskStatus.WAITING_FOR_APPROVAL
    store.persist()

    result = claim_agent_task_for_worker(task_id=task.id, worker_id="worker-a")

    assert result.success is False
    assert result.reason == "Agent task is not approved: WAITING_FOR_APPROVAL"
    assert store.agent_tasks[task.id].worker_lease_owner is None


def test_claim_first_approved_skips_active_lease_and_claims_next_available() -> None:
    busy = _approved_task("Busy approved task")
    busy.worker_lease_owner = "busy-worker"
    busy.worker_lease_token = "lease_busy"
    busy.worker_lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    available = _approved_task("Available approved task")
    store.persist()

    result = claim_first_approved_agent_task_for_worker(worker_id="worker-a")

    assert result.success is True
    assert result.task_id == available.id
    assert store.agent_tasks[busy.id].worker_lease_owner == "busy-worker"
    assert store.agent_tasks[available.id].worker_lease_owner == "worker-a"


def test_claim_first_approved_reports_when_no_available_task_exists() -> None:
    result = claim_first_approved_agent_task_for_worker(worker_id="worker-a")

    assert result.success is False
    assert result.reason == "No approved, linked, lease-available agent task found."
