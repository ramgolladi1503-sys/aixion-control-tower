from __future__ import annotations

import os

import pytest

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from app.agent_run_models import AgentRun, AgentRunStatus, AgentRunStep, AgentRunStepStatus, AgentRunStepType
from app.agent_run_state_machine import (
    InvalidAgentRunTransition,
    derive_run_status,
    transition_run,
    transition_step,
)


def test_run_state_machine_rejects_terminal_restart() -> None:
    run = AgentRun(task_id="task_1", status=AgentRunStatus.SUCCEEDED)
    with pytest.raises(InvalidAgentRunTransition, match="Illegal AgentRun transition"):
        transition_run(run, AgentRunStatus.RUNNING)


def test_step_state_machine_counts_attempt_when_execution_starts() -> None:
    step = AgentRunStep(
        run_id="run_1",
        task_id="task_1",
        sequence=0,
        step_type=AgentRunStepType.VALIDATE_SCOPE,
        status=AgentRunStepStatus.READY,
        idempotency_key="run_1:0",
    )
    transition_step(step, AgentRunStepStatus.RUNNING)
    assert step.attempt_count == 1
    assert step.started_at is not None
    transition_step(step, AgentRunStepStatus.SUCCEEDED)
    assert step.completed_at is not None
    assert step.duration_ms is not None


def test_derive_run_status_is_fail_closed() -> None:
    succeeded = AgentRunStep(
        run_id="run_1",
        task_id="task_1",
        sequence=0,
        step_type=AgentRunStepType.VALIDATE_SCOPE,
        status=AgentRunStepStatus.SUCCEEDED,
        idempotency_key="run_1:0",
    )
    blocked = AgentRunStep(
        run_id="run_1",
        task_id="task_1",
        sequence=1,
        step_type=AgentRunStepType.CREATE_BRANCH,
        status=AgentRunStepStatus.BLOCKED,
        idempotency_key="run_1:1",
    )
    assert derive_run_status([succeeded, blocked]) == AgentRunStatus.BLOCKED


def test_derive_run_status_requires_all_steps_to_pass() -> None:
    ready = AgentRunStep(
        run_id="run_1",
        task_id="task_1",
        sequence=0,
        step_type=AgentRunStepType.VALIDATE_SCOPE,
        status=AgentRunStepStatus.READY,
        idempotency_key="run_1:0",
    )
    assert derive_run_status([ready]) == AgentRunStatus.SCHEDULED
    ready.status = AgentRunStepStatus.SUCCEEDED
    assert derive_run_status([ready]) == AgentRunStatus.SUCCEEDED
