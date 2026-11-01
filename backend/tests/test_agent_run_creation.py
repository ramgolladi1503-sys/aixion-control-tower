from __future__ import annotations

from app.agent_run_creation import build_agent_run_creation_records
from app.agent_run_models import (
    DEFAULT_AGENT_RUN_STEPS,
    AgentRunCreate,
    AgentRunEventType,
    AgentRunStatus,
    AgentRunStepStatus,
)
from app.agent_task_models import AgentTask, AgentTaskStatus
from app.models import (
    AgentProvider,
    ApprovalRequest,
    ApprovalStatus,
    FileChange,
    RiskAssessment,
    RiskLevel,
)


def _approved_records_inputs(
    *, scheduled_from: str | None = None
) -> tuple[AgentTask, ApprovalRequest, AgentRunCreate]:
    approval = ApprovalRequest(
        id="approval_creation_test",
        project_id="project_creation_test",
        workspace_id="workspace_creation_test",
        title="Create a verified run",
        summary="Build the initial run records.",
        agent_name="Codex",
        target_branch="feature/run-creation",
        files=[FileChange(path="README.md", diff="+test")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.APPROVED,
        approved_payload_hash="sha256:test",
    )
    task = AgentTask(
        id="task_creation_test",
        workspace_id="workspace_creation_test",
        provider=AgentProvider.CODEX,
        project_id="project_creation_test",
        title="Create a verified run",
        goal="Create the initial records.",
        repository="example/control-tower",
        branch_preference="feature/run-creation",
        source_task_id="source-task-1",
        source_session_id="source-session-1",
        approval_request_id=approval.id,
        status=AgentTaskStatus.APPROVED,
    )
    metadata = {"custom": "preserved"}
    if scheduled_from is not None:
        metadata["scheduled_from"] = scheduled_from
    payload = AgentRunCreate(
        task_id=task.id,
        correlation_id="corr_creation_test",
        max_attempts_per_step=4,
        metadata=metadata,
    )
    return task, approval, payload


def test_builder_preserves_initial_run_steps_events_and_audit() -> None:
    task, approval, payload = _approved_records_inputs()

    records = build_agent_run_creation_records(
        task=task,
        approval=approval,
        payload=payload,
        actor="owner@example.com",
    )

    assert records.run.task_id == task.id
    assert records.run.workspace_id == task.workspace_id
    assert records.run.approval_request_id == approval.id
    assert records.run.project_id == task.project_id
    assert records.run.repository == task.repository
    assert records.run.objective == task.goal
    assert records.run.correlation_id == payload.correlation_id
    assert records.run.max_attempts_per_step == 4
    assert records.run.metadata == {
        "custom": "preserved",
        "provider": str(task.provider),
        "source_task_id": task.source_task_id,
        "source_session_id": task.source_session_id,
        "branch_preference": task.branch_preference,
    }
    assert [step.step_type for step in records.steps] == list(DEFAULT_AGENT_RUN_STEPS)
    assert [step.sequence for step in records.steps] == list(range(len(DEFAULT_AGENT_RUN_STEPS)))
    assert [step.status for step in records.steps] == [
        AgentRunStepStatus.READY,
        *([AgentRunStepStatus.PENDING] * (len(DEFAULT_AGENT_RUN_STEPS) - 1)),
    ]
    assert all(step.run_id == records.run.id for step in records.steps)
    assert all(step.task_id == task.id for step in records.steps)
    assert records.run.current_step_id == records.steps[0].id
    assert [event.event_type for event in records.events] == [
        AgentRunEventType.STEP_READY,
        AgentRunEventType.RUN_CREATED,
    ]
    assert all(event.actor == "owner@example.com" for event in records.events)
    assert records.audit.event_type == "agent_run.created"
    assert records.audit.entity_id == records.run.id
    assert records.audit.workspace_id == task.workspace_id
    assert records.audit.details["step_count"] == len(DEFAULT_AGENT_RUN_STEPS)
    assert records.scheduled_audit is None
    assert records.run.status == AgentRunStatus.SCHEDULED


def test_builder_adds_schedule_audit_only_for_approval_scheduling() -> None:
    task, approval, payload = _approved_records_inputs(scheduled_from="approval_recovery")

    records = build_agent_run_creation_records(
        task=task,
        approval=approval,
        payload=payload,
        actor="owner@example.com",
    )

    assert records.scheduled_audit is not None
    assert records.scheduled_audit.event_type == "agent_task.run_scheduled"
    assert records.scheduled_audit.entity_id == task.id
    assert records.scheduled_audit.workspace_id == task.workspace_id
    assert records.scheduled_audit.details == {
        "agent_run_id": records.run.id,
        "approval_request_id": approval.id,
        "run_status": records.run.status,
    }
