from __future__ import annotations

import os

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


def test_dry_run_writes_execution_and_result_events() -> None:
    task = _approved_task()

    result = run_agent_worker_dry_run(task_id=task.id, worker_id="test-worker")

    assert result.success is True
    assert result.task_id == task.id
    assert result.final_status == AgentTaskStatus.DONE
    assert result.started_event_id in store.agent_task_events
    assert result.result_event_id in store.agent_task_events
    assert store.agent_tasks[task.id].status == AgentTaskStatus.DONE

    events = [event for event in store.agent_task_events.values() if event.task_id == task.id]
    assert [event.event_type for event in events] == ["EXECUTION_STARTED", "RESULT_READY"]
    assert all(event.metadata["dry_run"] is True for event in events)
    assert all(event.actor == "test-worker" for event in events)
    assert any(event.event_type == "agent_worker.dry_run_started" for event in store.audit_events)
    assert any(event.event_type == "agent_worker.dry_run_completed" for event in store.audit_events)


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
