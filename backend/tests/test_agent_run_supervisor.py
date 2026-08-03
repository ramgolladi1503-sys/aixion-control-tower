from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from app.agent_run_models import (
    AgentRunCreate,
    AgentRunEventType,
    AgentRunStatus,
    AgentRunStepExecutionResult,
    AgentRunStepStatus,
    AgentRunStepType,
    VerificationDecision,
)
from app.agent_run_supervisor import (
    DefaultAgentRunStepExecutor,
    create_agent_run,
    execute_next_step,
    recover_stale_leases,
    retry_agent_run_step,
)
from app.agent_task_models import AgentTask, AgentTaskStatus
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


def _approved_task() -> AgentTask:
    project = Project(name="Mission Control", description="durable agent execution")
    store.projects[project.id] = project
    approval = ApprovalRequest(
        project_id=project.id,
        title="Mission Control change",
        summary="Apply a safe approved change.",
        agent_name="codex",
        target_branch="feature/mission-control-test",
        files=[
            FileChange(
                path="docs/mission-control.md",
                change_type="create",
                diff="+mission control",
                new_content="mission control\n",
            )
        ],
        test_plan=["python -m pytest"],
        rollback_plan="Close the PR and delete the feature branch.",
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.APPROVED,
    )
    store.approval_requests[approval.id] = approval
    task = AgentTask(
        provider=AgentProvider.CODEX,
        project_id=project.id,
        title="Implement Mission Control",
        goal="Create an evidence-backed PR.",
        repository="ramgolladi1503-sys/aixion-control-tower",
        branch_preference="feature/mission-control-test",
        approval_request_id=approval.id,
        status=AgentTaskStatus.APPROVED,
    )
    store.agent_tasks[task.id] = task
    store.persist()
    return task


def _pass_executor(run, step, timeout_seconds: int) -> AgentRunStepExecutionResult:
    return AgentRunStepExecutionResult(
        success=True,
        decision=VerificationDecision.PASS,
        reason=f"{step.step_type.value} passed.",
        evidence={"step": step.step_type.value, "timeout_seconds": timeout_seconds},
        output_reference=(
            "https://github.com/example/repo/pull/1"
            if step.step_type.value == "CREATE_PULL_REQUEST"
            else None
        ),
    )


def test_create_run_is_idempotent_for_active_task() -> None:
    task = _approved_task()
    first = create_agent_run(AgentRunCreate(task_id=task.id), actor="owner@example.com")
    second = create_agent_run(AgentRunCreate(task_id=task.id), actor="owner@example.com")
    assert first.id == second.id
    steps = [step for step in store.agent_run_steps.values() if step.run_id == first.id]
    assert len(steps) == 7
    assert sum(step.status == AgentRunStepStatus.READY for step in steps) == 1


def test_run_executes_every_injected_step_in_order() -> None:
    task = _approved_task()
    run = create_agent_run(AgentRunCreate(task_id=task.id), actor="owner@example.com")

    for _ in range(7):
        result = execute_next_step(run.id, worker_id="worker-1", executor=_pass_executor)
        assert result.decision == VerificationDecision.PASS

    assert run.status == AgentRunStatus.SUCCEEDED
    steps = sorted(
        [step for step in store.agent_run_steps.values() if step.run_id == run.id],
        key=lambda step: step.sequence,
    )
    assert all(step.status == AgentRunStepStatus.SUCCEEDED for step in steps)
    assert all(step.attempt_count == 1 for step in steps)
    assert any(
        event.event_type == AgentRunEventType.RUN_STATE_CHANGED
        for event in store.agent_run_events.values()
    )


def test_default_seal_evidence_step_sets_durable_final_hash() -> None:
    task = _approved_task()
    run = create_agent_run(AgentRunCreate(task_id=task.id), actor="owner@example.com")
    steps = sorted(
        [step for step in store.agent_run_steps.values() if step.run_id == run.id],
        key=lambda step: step.sequence,
    )
    seal_step = steps[-1]
    assert seal_step.step_type == AgentRunStepType.SEAL_EVIDENCE

    for step in steps[:-1]:
        step.status = AgentRunStepStatus.SUCCEEDED
        step.decision = VerificationDecision.PASS
        step.attempt_count = 1
        step.evidence = {"step": step.step_type.value, "verified": True}
        step.evidence_hash = f"hash-{step.sequence}"
    seal_step.status = AgentRunStepStatus.READY
    run.current_step_id = seal_step.id
    run.current_step_index = seal_step.sequence
    store.persist()

    result = execute_next_step(
        run.id,
        worker_id="seal-worker",
        executor=DefaultAgentRunStepExecutor(),
    )

    assert result.decision == VerificationDecision.PASS
    assert run.status == AgentRunStatus.SUCCEEDED
    assert run.final_evidence_hash
    assert result.evidence["final_evidence_hash"] == run.final_evidence_hash
    assert store.agent_runs[run.id].final_evidence_hash == run.final_evidence_hash
    assert seal_step.status == AgentRunStepStatus.SUCCEEDED
    assert seal_step.evidence_hash


def test_transient_failure_uses_bounded_retry() -> None:
    task = _approved_task()
    run = create_agent_run(
        AgentRunCreate(task_id=task.id, max_attempts_per_step=2),
        actor="owner@example.com",
    )
    attempts = 0

    def flaky_executor(_run, _step, _timeout):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return AgentRunStepExecutionResult(
                success=False,
                decision=VerificationDecision.RETRY_TRANSIENT,
                reason="GitHub request timed out temporarily.",
            )
        return AgentRunStepExecutionResult(
            success=True,
            decision=VerificationDecision.PASS,
            reason="Recovered.",
        )

    first = execute_next_step(run.id, worker_id="worker-1", executor=flaky_executor)
    assert first.decision == VerificationDecision.RETRY_TRANSIENT
    assert run.status == AgentRunStatus.RETRY_WAIT
    step = store.agent_run_steps[run.current_step_id]
    assert step.status == AgentRunStepStatus.RETRY_WAIT
    step.next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    second = execute_next_step(run.id, worker_id="worker-1", executor=flaky_executor)
    assert second.decision == VerificationDecision.PASS
    assert step.status == AgentRunStepStatus.SUCCEEDED
    assert step.attempt_count == 2


def test_human_decision_can_return_step_to_ready() -> None:
    task = _approved_task()
    run = create_agent_run(AgentRunCreate(task_id=task.id), actor="owner@example.com")

    def needs_human(_run, _step, _timeout):
        return AgentRunStepExecutionResult(
            success=False,
            decision=VerificationDecision.NEEDS_REVISION,
            reason="Tests failed and the proposed patch needs revision.",
        )

    execute_next_step(run.id, worker_id="worker-1", executor=needs_human)
    step = store.agent_run_steps[run.current_step_id]
    assert run.status == AgentRunStatus.NEEDS_HUMAN
    assert step.status == AgentRunStepStatus.NEEDS_HUMAN

    retry_agent_run_step(
        run,
        step,
        actor="owner@example.com",
        reason="Revised plan approved.",
    )
    assert run.status == AgentRunStatus.SCHEDULED
    assert step.status == AgentRunStepStatus.READY


def test_watchdog_recovers_expired_lease_without_claiming_success() -> None:
    task = _approved_task()
    run = create_agent_run(AgentRunCreate(task_id=task.id), actor="owner@example.com")
    step = store.agent_run_steps[run.current_step_id]
    run.status = AgentRunStatus.RUNNING
    run.lease_owner = "dead-worker"
    run.lease_token = "dead-token"
    run.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    step.status = AgentRunStepStatus.RUNNING
    step.attempt_count = 1
    step.lease_owner = "dead-worker"
    step.lease_token = "dead-token"
    step.lease_expires_at = run.lease_expires_at
    store.persist()

    assert recover_stale_leases() == 1
    assert run.status == AgentRunStatus.RETRY_WAIT
    assert step.status == AgentRunStepStatus.RETRY_WAIT
    assert run.lease_owner is None
    assert any(
        event.event_type == AgentRunEventType.STALE_LEASE_RECOVERED
        for event in store.agent_run_events.values()
    )
