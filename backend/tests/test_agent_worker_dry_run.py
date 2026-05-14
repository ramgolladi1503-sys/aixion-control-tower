from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from app.agent_task_models import AgentTask, AgentTaskStatus
from app.agent_worker_dry_run import run_agent_worker_dry_run
from app.models import AgentProvider, Project
from app.store import store


def setup_function() -> None:
    store.reset()


def _approved_task() -> AgentTask:
    project = Project(name="Worker Dry Run", description="demo")
    store.projects[project.id] = project
    task = AgentTask(
        provider=AgentProvider.CODEX,
        project_id=project.id,
        title="Dry-run approved task",
        goal="Prove worker lifecycle without mutating repositories.",
        repository="ramgolladi1503-sys/aixion-control-tower",
        branch_preference="feature/agent-worker-dry-run",
        status=AgentTaskStatus.APPROVED,
        approval_request_id="approval_dry_run",
    )
    store.agent_tasks[task.id] = task
    store.persist()
    return task


def test_dry_run_claims_releases_and_writes_execution_and_result_events() -> None:
    task = _approved_task()

    result = run_agent_worker_dry_run(task_id=task.id, worker_id="test-worker")

    assert result.success is True
    assert result.task_id == task.id
    assert result.lease_token is not None
    assert result.final_status == AgentTaskStatus.DONE
    assert result.started_event_id in store.agent_task_events
    assert result.result_event_id in store.agent_task_events
    assert store.agent_tasks[task.id].status == AgentTaskStatus.DONE
    assert store.agent_tasks[task.id].worker_lease_owner is None
    assert store.agent_tasks[task.id].worker_lease_token is None
    assert store.agent_tasks[task.id].worker_lease_expires_at is None

    events = [event for event in store.agent_task_events.values() if event.task_id == task.id]
    assert [event.event_type for event in events] == ["EXECUTION_STARTED", "RESULT_READY"]
    assert all(event.metadata["dry_run"] is True for event in events)
    assert all(event.metadata["lease_token"] == result.lease_token for event in events)
    assert all(event.actor == "test-worker" for event in events)
    assert any(event.event_type == "agent_worker.task_claimed" for event in store.audit_events)
    assert any(event.event_type == "agent_worker.dry_run_started" for event in store.audit_events)
    assert any(event.event_type == "agent_worker.dry_run_completed" for event in store.audit_events)
    assert any(event.event_type == "agent_worker.task_released" for event in store.audit_events)


def test_dry_run_first_approved_picks_eligible_task() -> None:
    task = _approved_task()

    result = run_agent_worker_dry_run(first_approved=True)

    assert result.success is True
    assert result.task_id == task.id
    assert store.agent_tasks[task.id].status == AgentTaskStatus.DONE


def test_dry_run_refuses_without_task_selector() -> None:
    result = run_agent_worker_dry_run()

    assert result.success is False
    assert result.reason == "Provide task_id or set first_approved=true."


def test_dry_run_refuses_missing_task() -> None:
    result = run_agent_worker_dry_run(task_id="agent_task_missing")

    assert result.success is False
    assert result.task_id == "agent_task_missing"
    assert "not found" in result.reason


def test_dry_run_refuses_unapproved_task_without_mutating_status() -> None:
    project = Project(name="Worker Dry Run", description="demo")
    store.projects[project.id] = project
    task = AgentTask(
        provider=AgentProvider.CHATGPT,
        project_id=project.id,
        title="Not approved",
        goal="Should not run.",
        status=AgentTaskStatus.WAITING_FOR_APPROVAL,
        approval_request_id="approval_waiting",
    )
    store.agent_tasks[task.id] = task
    store.persist()

    result = run_agent_worker_dry_run(task_id=task.id)

    assert result.success is False
    assert result.final_status == AgentTaskStatus.WAITING_FOR_APPROVAL
    assert store.agent_tasks[task.id].status == AgentTaskStatus.WAITING_FOR_APPROVAL
    assert store.agent_task_events == {}


def test_dry_run_refuses_approved_task_missing_linked_approval() -> None:
    task = _approved_task()
    task.approval_request_id = None
    store.persist()

    result = run_agent_worker_dry_run(task_id=task.id)

    assert result.success is False
    assert result.reason == "Agent task is approved but missing approval_request_id"
    assert store.agent_tasks[task.id].status == AgentTaskStatus.APPROVED
    assert store.agent_task_events == {}


def test_dry_run_refuses_terminal_task() -> None:
    task = _approved_task()
    task.status = AgentTaskStatus.FAILED
    store.persist()

    result = run_agent_worker_dry_run(task_id=task.id)

    assert result.success is False
    assert result.reason == "Agent task is terminal: FAILED"
    assert store.agent_task_events == {}


def test_dry_run_refuses_active_lease() -> None:
    task = _approved_task()
    task.worker_lease_owner = "other-worker"
    task.worker_lease_token = "lease_active"
    task.worker_lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    store.persist()

    result = run_agent_worker_dry_run(task_id=task.id, worker_id="test-worker")

    assert result.success is False
    assert "already leased by other-worker" in result.reason
    assert store.agent_tasks[task.id].status == AgentTaskStatus.APPROVED
    assert store.agent_task_events == {}


def test_dry_run_can_take_expired_lease() -> None:
    task = _approved_task()
    task.worker_lease_owner = "old-worker"
    task.worker_lease_token = "lease_expired"
    task.worker_lease_expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    store.persist()

    result = run_agent_worker_dry_run(task_id=task.id, worker_id="new-worker")

    assert result.success is True
    assert result.lease_token is not None
    assert result.lease_token != "lease_expired"
    assert store.agent_tasks[task.id].status == AgentTaskStatus.DONE
    assert store.agent_tasks[task.id].worker_lease_owner is None


def test_first_approved_skips_active_leased_task_and_uses_next_available() -> None:
    leased = _approved_task()
    leased.worker_lease_owner = "busy-worker"
    leased.worker_lease_token = "lease_busy"
    leased.worker_lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    available = _approved_task()
    store.persist()

    result = run_agent_worker_dry_run(first_approved=True, worker_id="test-worker")

    assert result.success is True
    assert result.task_id == available.id
    assert store.agent_tasks[leased.id].status == AgentTaskStatus.APPROVED
    assert store.agent_tasks[available.id].status == AgentTaskStatus.DONE
