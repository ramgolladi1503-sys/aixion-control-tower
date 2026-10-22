from __future__ import annotations

import os

import pytest

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from app.agent_run_models import AgentRun, AgentRunCreate, AgentRunStatus
from app.agent_task_repository import SQLiteAgentTaskRepository
from app.agent_task_models import AgentTask, AgentTaskStatus
from app import approval_recovery
from app.approval_recovery import reconcile_linked_approval_tasks
from app.approval_decisions import agent_run_repository, approval_decision_repository
from app.agent_run_supervisor import create_agent_run
from app.approval_integrity import compute_approval_payload_hash
from app.models import (
    AgentProvider,
    ApprovalRequest,
    ApprovalStatus,
    AuditEvent,
    FileChange,
    Notification,
    Project,
    RiskAssessment,
    RiskLevel,
    Workspace,
)
from app.store import store


def setup_function() -> None:
    store.reset()


def _linked_records() -> tuple[str, str]:
    project = Project(name="Recovery", description="Approval recovery test")
    approval = ApprovalRequest(
        project_id=project.id,
        title="Recover approved task",
        summary="Simulate crash after approval commit.",
        target_branch="feature/recovery",
        files=[FileChange(path="README.md", diff="+safe")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.APPROVED,
    )
    approval.approved_payload_hash = compute_approval_payload_hash(approval)
    task = AgentTask(
        provider=AgentProvider.MANUAL,
        project_id=project.id,
        title="Recover task projection",
        goal="Reconcile from durable approval state.",
        approval_request_id=approval.id,
        status=AgentTaskStatus.WAITING_FOR_APPROVAL,
    )
    store.projects[project.id] = project
    store.approval_requests[approval.id] = approval
    store.agent_tasks[task.id] = task
    store.audit_events.append(
        AuditEvent(
            event_type="approval.decision",
            entity_id=approval.id,
            workspace_id=approval.workspace_id,
            details={
                "previous_status": ApprovalStatus.REQUESTED,
                "new_status": ApprovalStatus.APPROVED,
            },
        )
    )
    store.persist()
    # A reload models process death after the approval commit but before task
    # propagation. Assertions below therefore inspect deserialized DB state.
    approval_id, task_id = approval.id, task.id
    store.load()
    return approval_id, task_id


def test_startup_reconciles_approved_task_and_schedules_one_run() -> None:
    _, task_id = _linked_records()
    # Model a process whose compatibility maps are missing the already
    # committed approval and task rows while the SQLite repository has them.
    store.approval_requests.clear()
    store.agent_tasks.clear()
    store.audit_events.clear()

    assert reconcile_linked_approval_tasks() == 1
    task = store.agent_tasks[task_id]
    assert task.status == AgentTaskStatus.APPROVED
    task_events = [event for event in store.agent_task_events.values() if event.task_id == task_id]
    assert task_events[0].metadata["previous_status"] == ApprovalStatus.REQUESTED
    runs = [run for run in store.agent_runs.values() if run.task_id == task.id]
    assert len(runs) == 1
    assert runs[0].status == AgentRunStatus.SCHEDULED
    scheduled_run_id = runs[0].id
    store.load()
    assert store.agent_runs[scheduled_run_id].status == AgentRunStatus.SCHEDULED
    assert any(
        event.event_type == "agent_task.run_scheduled"
        and event.entity_id == task.id
        and event.details.get("agent_run_id") == scheduled_run_id
        for event in store.audit_events
    )


def test_startup_reconciliation_is_idempotent_after_success() -> None:
    _, task_id = _linked_records()

    assert reconcile_linked_approval_tasks() == 1
    task = store.agent_tasks[task_id]
    event_count = len([event for event in store.agent_task_events.values() if event.task_id == task.id])
    notification_count = len(store.notifications)
    assert reconcile_linked_approval_tasks() == 0

    assert len([event for event in store.agent_task_events.values() if event.task_id == task.id]) == event_count
    assert len(store.notifications) == notification_count
    assert len([run for run in store.agent_runs.values() if run.task_id == task.id]) == 1


def test_recovery_schedule_failure_can_be_retried_without_duplicate_task_event(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    approval_id, task_id = _linked_records()
    original_create_run = approval_recovery.create_agent_run
    attempts = 0

    def fail_first_schedule(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("simulated transient AgentRun persistence failure")
        return original_create_run(*args, **kwargs)

    monkeypatch.setattr(approval_recovery, "create_agent_run", fail_first_schedule)
    with caplog.at_level("ERROR", logger="app.approval_recovery"):
        assert reconcile_linked_approval_tasks() == 0

    assert store.agent_tasks[task_id].status == AgentTaskStatus.APPROVED
    assert not [run for run in store.agent_runs.values() if run.task_id == task_id]
    assert any(
        f"approval_id={approval_id}" in record.message
        and f"task_id={task_id}" in record.message
        and "phase=schedule_first_run" in record.message
        and "error_type=RuntimeError" in record.message
        for record in caplog.records
    )

    assert reconcile_linked_approval_tasks() == 1
    task_events = [event for event in store.agent_task_events.values() if event.task_id == task_id]
    assert len(task_events) == 1
    runs = [run for run in store.agent_runs.values() if run.task_id == task_id]
    assert len(runs) == 1
    assert runs[0].approval_request_id == approval_id
    assert reconcile_linked_approval_tasks() == 0
    assert [run for run in store.agent_runs.values() if run.task_id == task_id] == runs


def test_recovery_repairs_missing_schedule_audit_without_creating_another_run() -> None:
    approval_id, task_id = _linked_records()
    task = store.agent_tasks[task_id]
    workspace = Workspace(name="Recovery workspace", owner_user_id="recovery-owner")
    project = store.projects[task.project_id]
    project.workspace_id = workspace.id
    task.workspace_id = workspace.id
    approval = store.approval_requests[approval_id]
    approval.workspace_id = workspace.id
    store.workspaces[workspace.id] = workspace
    task.status = AgentTaskStatus.APPROVED
    store.persist()
    run = create_agent_run(
        AgentRunCreate(
            task_id=task_id,
            metadata={"scheduled_from": "approval_decision"},
        ),
        actor="test",
    )
    # create_agent_run only writes the run-created audit; the schedule event
    # is the separate post-create write whose crash window this test models.
    store.load()
    # The run is durable, but the schedule audit and process maps are lost,
    # matching a restart after run creation and before audit append.
    store.agent_runs.clear()
    store.audit_events.clear()

    assert reconcile_linked_approval_tasks() == 1
    assert agent_run_repository.list_for_task(task_id) == [run]
    assert agent_run_repository.has_run_scheduled_audit(
        task_id=task_id,
        run_id=run.id,
        workspace_id=task.workspace_id,
    )
    store.load()
    schedule_audits = [
        event
        for event in store.audit_events
        if event.event_type == "agent_task.run_scheduled"
        and event.entity_id == task_id
        and event.details.get("agent_run_id") == run.id
    ]
    assert len(schedule_audits) == 1
    assert schedule_audits[0].workspace_id == task.workspace_id
    assert schedule_audits[0].details["approval_request_id"] == approval_id
    assert reconcile_linked_approval_tasks() == 0
    assert agent_run_repository.list_for_task(task_id) == [run]


def test_nonterminal_approval_does_not_reconcile_task() -> None:
    approval_id, task_id = _linked_records()
    approval = store.approval_requests[approval_id]
    task = store.agent_tasks[task_id]
    approval.status = ApprovalStatus.REQUESTED
    store.persist()

    assert reconcile_linked_approval_tasks() == 0
    assert task.status == AgentTaskStatus.WAITING_FOR_APPROVAL
    assert not store.agent_runs


def test_startup_does_not_reopen_cancelled_linked_task() -> None:
    _, task_id = _linked_records()
    task = store.agent_tasks[task_id]
    task.status = AgentTaskStatus.CANCELLED
    store.persist()
    store.load()

    assert reconcile_linked_approval_tasks() == 0
    assert store.agent_tasks[task_id].status == AgentTaskStatus.CANCELLED
    assert not store.agent_runs
    assert not [event for event in store.agent_task_events.values() if event.task_id == task_id]


def test_denied_decision_event_is_repaired_without_duplicate_notification() -> None:
    approval_id, task_id = _linked_records()
    approval = store.approval_requests[approval_id]
    task = store.agent_tasks[task_id]
    approval.status = ApprovalStatus.DENIED
    task.status = AgentTaskStatus.DENIED
    store.audit_events.append(
        AuditEvent(
            event_type="approval.decision",
            entity_id=approval.id,
            workspace_id=approval.workspace_id,
            details={
                "previous_status": ApprovalStatus.APPROVED,
                "new_status": ApprovalStatus.DENIED,
            },
        )
    )
    existing_notification = Notification(
        title="Agent task denied: Recover task projection",
        body="Linked approval was denied. Worker must not continue.",
        entity_type="agent_task",
        entity_id=task.id,
    )
    store.notifications[existing_notification.id] = existing_notification
    store.persist()
    store.load()
    # Recovery must use the durable approval/task/event repositories rather
    # than treating process-local snapshots as its input authority.
    store.approval_requests.clear()
    store.agent_tasks.clear()
    store.audit_events.clear()

    assert reconcile_linked_approval_tasks() == 1

    events = [event for event in store.agent_task_events.values() if event.task_id == task_id]
    assert len(events) == 1
    assert events[0].event_type.value == "DENIED"
    assert events[0].metadata["new_status"] == ApprovalStatus.DENIED
    assert events[0].metadata["previous_status"] == ApprovalStatus.APPROVED
    assert set(store.notifications) == {existing_notification.id}
    assert not store.agent_runs
    assert reconcile_linked_approval_tasks() == 0


def test_recovery_does_not_restart_task_with_terminal_run() -> None:
    approval_id, task_id = _linked_records()
    approval = store.approval_requests[approval_id]
    task = store.agent_tasks[task_id]
    task.status = AgentTaskStatus.APPROVED
    failed_run = AgentRun(
        task_id=task.id,
        approval_request_id=approval.id,
        project_id=task.project_id,
        status=AgentRunStatus.FAILED,
    )
    store.agent_runs[failed_run.id] = failed_run
    store.persist()
    store.load()

    assert reconcile_linked_approval_tasks() == 1
    assert [run.id for run in store.agent_runs.values() if run.task_id == task_id] == [failed_run.id]
    assert len([event for event in store.agent_task_events.values() if event.task_id == task_id]) == 1
    assert reconcile_linked_approval_tasks() == 0
    assert [run.id for run in store.agent_runs.values() if run.task_id == task_id] == [failed_run.id]


def test_recovery_reads_run_history_from_repository_not_process_map() -> None:
    _, task_id = _linked_records()
    task = store.agent_tasks[task_id]
    failed_run = AgentRun(
        task_id=task.id,
        approval_request_id=task.approval_request_id,
        project_id=task.project_id,
        status=AgentRunStatus.FAILED,
    )
    store.agent_runs[failed_run.id] = failed_run
    store.persist()
    store.agent_runs.clear()

    assert reconcile_linked_approval_tasks() == 1
    assert store.agent_tasks[task_id].status == AgentTaskStatus.APPROVED
    assert store.agent_runs == {}
    assert reconcile_linked_approval_tasks() == 0
    assert agent_run_repository.list_for_task(task_id) == [failed_run]
    store.load()
    assert store.agent_runs[failed_run.id] == failed_run
    assert store.agent_tasks[task_id].status == AgentTaskStatus.APPROVED


def test_recovery_fails_closed_for_non_sqlite_repository_bindings() -> None:
    _linked_records()
    before = (
        dict(store.agent_tasks),
        dict(store.agent_task_events),
        dict(store.agent_runs),
    )

    with pytest.raises(RuntimeError, match="supported repository profile"):
        reconcile_linked_approval_tasks(
            approval_repository=object(),  # type: ignore[arg-type]
            task_repository=object(),  # type: ignore[arg-type]
            run_repository=object(),  # type: ignore[arg-type]
        )

    assert (
        store.agent_tasks,
        store.agent_task_events,
        store.agent_runs,
    ) == before


def test_recovery_rejects_sqlite_repositories_from_different_databases() -> None:
    with pytest.raises(RuntimeError, match="share one SQLite connection provider"):
        reconcile_linked_approval_tasks(
            approval_repository=approval_decision_repository,
            task_repository=SQLiteAgentTaskRepository(lambda: store._connect()),
            run_repository=agent_run_repository,
        )
