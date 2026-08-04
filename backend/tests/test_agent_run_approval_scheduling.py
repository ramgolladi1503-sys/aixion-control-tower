from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from app.agent_task_models import AgentTask, AgentTaskStatus
from app.agent_task_routes import propagate_approval_decision_to_agent_task
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


def setup_function() -> None:
    store.reset()


def test_mobile_approval_schedules_exactly_one_durable_run() -> None:
    project = Project(name="Approval scheduling", description="Mission Control")
    store.projects[project.id] = project
    approval = ApprovalRequest(
        project_id=project.id,
        title="Approved Mission Control work",
        summary="Schedule an approved run.",
        agent_name="codex",
        target_branch="feature/approved-run",
        files=[
            FileChange(
                path="docs/approved-run.md",
                change_type="create",
                diff="+approved run",
                new_content="approved run\n",
            )
        ],
        test_plan=["python -m pytest"],
        rollback_plan="Close the feature pull request.",
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.APPROVED,
    )
    store.approval_requests[approval.id] = approval
    task = AgentTask(
        provider=AgentProvider.CODEX,
        project_id=project.id,
        title="Schedule Mission Control",
        goal="Create one durable run after approval.",
        repository="ramgolladi1503-sys/aixion-control-tower",
        branch_preference="feature/approved-run",
        approval_request_id=approval.id,
        status=AgentTaskStatus.WAITING_FOR_APPROVAL,
    )
    store.agent_tasks[task.id] = task

    propagated = propagate_approval_decision_to_agent_task(
        approval,
        ApprovalStatus.REQUESTED,
        "owner@example.com",
    )
    assert propagated is task
    assert task.status == AgentTaskStatus.APPROVED

    runs = [run for run in store.agent_runs.values() if run.task_id == task.id]
    assert len(runs) == 1
    assert runs[0].metadata["scheduled_from"] == "approval_decision"

    propagate_approval_decision_to_agent_task(
        approval,
        ApprovalStatus.APPROVED,
        "owner@example.com",
    )
    runs = [run for run in store.agent_runs.values() if run.task_id == task.id]
    assert len(runs) == 1

    decision_events = [
        event
        for event in store.agent_task_events.values()
        if event.task_id == task.id and event.event_type.value == "APPROVED"
    ]
    assert decision_events
    assert decision_events[-1].metadata["agent_run_id"] == runs[0].id
