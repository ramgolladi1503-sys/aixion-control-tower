from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from app.agent_task_models import AgentTask, AgentTaskStatus
from app.approval_integrity import compute_approval_payload_hash
from app.agent_run_models import AgentRunCreate
from app.agent_run_supervisor import create_agent_run
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
    approval.approved_payload_hash = compute_approval_payload_hash(approval)
    store.approval_requests[approval.id] = approval
    task = AgentTask(
        provider=AgentProvider.CODEX,
        project_id=project.id,
        title="Schedule Mission Control",
        goal="Create one durable run after approval.",
        repository="ramgolladi1503-sys/aixion-control-tower",
        branch_preference="feature/approved-run",
        approval_request_id=approval.id,
        status=AgentTaskStatus.APPROVED,
    )
    store.agent_tasks[task.id] = task
    store.persist()

    first = create_agent_run(
        AgentRunCreate(
            task_id=task.id,
            metadata={"scheduled_from": "approval_decision"},
        ),
        actor="owner@example.com",
    )

    runs = [run for run in store.agent_runs.values() if run.task_id == task.id]
    assert len(runs) == 1
    assert runs[0].metadata["scheduled_from"] == "approval_decision"

    second = create_agent_run(AgentRunCreate(task_id=task.id), actor="owner@example.com")
    runs = [run for run in store.agent_runs.values() if run.task_id == task.id]
    assert len(runs) == 1
    assert first.id == second.id == runs[0].id
