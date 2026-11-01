from __future__ import annotations

import secrets
from dataclasses import dataclass

from .agent_run_models import (
    DEFAULT_AGENT_RUN_STEPS,
    AgentRun,
    AgentRunCreate,
    AgentRunEvent,
    AgentRunEventType,
    AgentRunStep,
    AgentRunStepStatus,
)
from .agent_task_models import AgentTask
from .models import ApprovalRequest, AuditEvent


@dataclass(frozen=True)
class AgentRunCreationRecords:
    """Records to persist when scheduling a run from an approved task."""

    run: AgentRun
    steps: list[AgentRunStep]
    events: list[AgentRunEvent]
    audit: AuditEvent
    scheduled_audit: AuditEvent | None


def build_agent_run_creation_records(
    *, task: AgentTask, approval: ApprovalRequest, payload: AgentRunCreate, actor: str
) -> AgentRunCreationRecords:
    """Build the initial AgentRun records from already-validated snapshots."""
    run = AgentRun(
        task_id=task.id,
        workspace_id=task.workspace_id,
        approval_request_id=approval.id,
        project_id=task.project_id,
        repository=task.repository,
        objective=task.goal,
        correlation_id=payload.correlation_id or f"corr_{secrets.token_hex(12)}",
        max_attempts_per_step=payload.max_attempts_per_step,
        metadata={
            **payload.metadata,
            "provider": str(task.provider),
            "source_task_id": task.source_task_id,
            "source_session_id": task.source_session_id,
            "branch_preference": task.branch_preference,
        },
    )
    steps: list[AgentRunStep] = []
    events: list[AgentRunEvent] = []
    for index, step_type in enumerate(DEFAULT_AGENT_RUN_STEPS):
        step = AgentRunStep(
            run_id=run.id,
            task_id=task.id,
            sequence=index,
            step_type=step_type,
            status=AgentRunStepStatus.READY if index == 0 else AgentRunStepStatus.PENDING,
            max_attempts=payload.max_attempts_per_step,
            idempotency_key=f"{run.id}:{index}:{step_type.value}",
        )
        steps.append(step)
        if index == 0:
            run.current_step_id = step.id
            events.append(
                AgentRunEvent(
                    run_id=run.id,
                    task_id=run.task_id,
                    step_id=step.id,
                    correlation_id=run.correlation_id,
                    event_type=AgentRunEventType.STEP_READY,
                    new_status=step.status.value,
                    message="First Mission Control step is ready.",
                    actor=actor,
                    attempt_number=step.attempt_count,
                )
            )

    events.append(
        AgentRunEvent(
            run_id=run.id,
            task_id=run.task_id,
            correlation_id=run.correlation_id,
            event_type=AgentRunEventType.RUN_CREATED,
            new_status=run.status.value,
            message="Durable Mission Control run created from an approved AgentTask.",
            actor=actor,
            metadata={"step_count": len(DEFAULT_AGENT_RUN_STEPS)},
        )
    )
    audit = AuditEvent(
        event_type="agent_run.created",
        entity_id=run.id,
        workspace_id=run.workspace_id,
        actor=actor,
        details={
            "task_id": run.task_id,
            "approval_request_id": run.approval_request_id,
            "correlation_id": run.correlation_id,
            "step_count": len(steps),
        },
    )
    scheduled_audit = None
    if payload.metadata.get("scheduled_from") in {
        "approval_decision",
        "approval_recovery",
    }:
        scheduled_audit = AuditEvent(
            event_type="agent_task.run_scheduled",
            entity_id=task.id,
            workspace_id=task.workspace_id,
            actor=actor,
            details={
                "agent_run_id": run.id,
                "approval_request_id": approval.id,
                "run_status": run.status,
            },
        )
    return AgentRunCreationRecords(
        run=run,
        steps=steps,
        events=events,
        audit=audit,
        scheduled_audit=scheduled_audit,
    )
