from __future__ import annotations

from datetime import datetime, timezone

from .agent_run_models import (
    AgentRun,
    AgentRunStatus,
    AgentRunStep,
    AgentRunStepStatus,
)


class InvalidAgentRunTransition(ValueError):
    pass


TERMINAL_RUN_STATUSES = {
    AgentRunStatus.BLOCKED,
    AgentRunStatus.SUCCEEDED,
    AgentRunStatus.FAILED,
    AgentRunStatus.CANCELLED,
}

TERMINAL_STEP_STATUSES = {
    AgentRunStepStatus.BLOCKED,
    AgentRunStepStatus.SUCCEEDED,
    AgentRunStepStatus.FAILED,
    AgentRunStepStatus.CANCELLED,
    AgentRunStepStatus.SKIPPED,
}

RUN_TRANSITIONS: dict[AgentRunStatus, set[AgentRunStatus]] = {
    AgentRunStatus.SCHEDULED: {
        AgentRunStatus.RUNNING,
        AgentRunStatus.PAUSED,
        AgentRunStatus.CANCELLED,
        AgentRunStatus.BLOCKED,
        AgentRunStatus.FAILED,
    },
    AgentRunStatus.RUNNING: {
        AgentRunStatus.EVALUATING,
        AgentRunStatus.RETRY_WAIT,
        AgentRunStatus.PAUSED,
        AgentRunStatus.NEEDS_HUMAN,
        AgentRunStatus.BLOCKED,
        AgentRunStatus.SUCCEEDED,
        AgentRunStatus.FAILED,
        AgentRunStatus.CANCELLED,
    },
    AgentRunStatus.EVALUATING: {
        AgentRunStatus.RUNNING,
        AgentRunStatus.RETRY_WAIT,
        AgentRunStatus.NEEDS_HUMAN,
        AgentRunStatus.BLOCKED,
        AgentRunStatus.SUCCEEDED,
        AgentRunStatus.FAILED,
        AgentRunStatus.CANCELLED,
    },
    AgentRunStatus.RETRY_WAIT: {
        AgentRunStatus.RUNNING,
        AgentRunStatus.PAUSED,
        AgentRunStatus.NEEDS_HUMAN,
        AgentRunStatus.FAILED,
        AgentRunStatus.CANCELLED,
    },
    AgentRunStatus.PAUSED: {
        AgentRunStatus.SCHEDULED,
        AgentRunStatus.RUNNING,
        AgentRunStatus.CANCELLED,
    },
    AgentRunStatus.NEEDS_HUMAN: {
        AgentRunStatus.SCHEDULED,
        AgentRunStatus.RUNNING,
        AgentRunStatus.BLOCKED,
        AgentRunStatus.FAILED,
        AgentRunStatus.CANCELLED,
    },
    AgentRunStatus.BLOCKED: set(),
    AgentRunStatus.SUCCEEDED: set(),
    AgentRunStatus.FAILED: set(),
    AgentRunStatus.CANCELLED: set(),
}

STEP_TRANSITIONS: dict[AgentRunStepStatus, set[AgentRunStepStatus]] = {
    AgentRunStepStatus.PENDING: {
        AgentRunStepStatus.READY,
        AgentRunStepStatus.PAUSED,
        AgentRunStepStatus.CANCELLED,
        AgentRunStepStatus.SKIPPED,
    },
    AgentRunStepStatus.READY: {
        AgentRunStepStatus.RUNNING,
        AgentRunStepStatus.PAUSED,
        AgentRunStepStatus.CANCELLED,
        AgentRunStepStatus.SKIPPED,
    },
    AgentRunStepStatus.RUNNING: {
        AgentRunStepStatus.SUCCEEDED,
        AgentRunStepStatus.RETRY_WAIT,
        AgentRunStepStatus.NEEDS_HUMAN,
        AgentRunStepStatus.BLOCKED,
        AgentRunStepStatus.FAILED,
        AgentRunStepStatus.CANCELLED,
    },
    AgentRunStepStatus.RETRY_WAIT: {
        AgentRunStepStatus.READY,
        AgentRunStepStatus.NEEDS_HUMAN,
        AgentRunStepStatus.FAILED,
        AgentRunStepStatus.CANCELLED,
    },
    AgentRunStepStatus.PAUSED: {
        AgentRunStepStatus.READY,
        AgentRunStepStatus.CANCELLED,
    },
    AgentRunStepStatus.NEEDS_HUMAN: {
        AgentRunStepStatus.READY,
        AgentRunStepStatus.BLOCKED,
        AgentRunStepStatus.FAILED,
        AgentRunStepStatus.CANCELLED,
    },
    AgentRunStepStatus.BLOCKED: set(),
    AgentRunStepStatus.SUCCEEDED: set(),
    AgentRunStepStatus.FAILED: set(),
    AgentRunStepStatus.CANCELLED: set(),
    AgentRunStepStatus.SKIPPED: set(),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def can_transition_run(current: AgentRunStatus, target: AgentRunStatus) -> bool:
    return target == current or target in RUN_TRANSITIONS[current]


def can_transition_step(current: AgentRunStepStatus, target: AgentRunStepStatus) -> bool:
    return target == current or target in STEP_TRANSITIONS[current]


def transition_run(run: AgentRun, target: AgentRunStatus, *, reason: str = "") -> tuple[AgentRunStatus, AgentRunStatus]:
    previous = run.status
    if target == previous:
        return previous, target
    if not can_transition_run(previous, target):
        raise InvalidAgentRunTransition(f"Illegal AgentRun transition: {previous} -> {target}")
    now = _now()
    run.status = target
    run.updated_at = now
    if target == AgentRunStatus.RUNNING and run.started_at is None:
        run.started_at = now
    if target in TERMINAL_RUN_STATUSES:
        run.completed_at = now
        run.lease_owner = None
        run.lease_token = None
        run.lease_expires_at = None
    if reason:
        run.last_error = reason if target in {AgentRunStatus.FAILED, AgentRunStatus.BLOCKED} else run.last_error
    return previous, target


def transition_step(
    step: AgentRunStep,
    target: AgentRunStepStatus,
    *,
    reason: str = "",
) -> tuple[AgentRunStepStatus, AgentRunStepStatus]:
    previous = step.status
    if target == previous:
        return previous, target
    if not can_transition_step(previous, target):
        raise InvalidAgentRunTransition(f"Illegal AgentRunStep transition: {previous} -> {target}")
    now = _now()
    step.status = target
    step.updated_at = now
    if target == AgentRunStepStatus.RUNNING:
        if step.started_at is None:
            step.started_at = now
        step.attempt_count += 1
    if target in TERMINAL_STEP_STATUSES:
        step.completed_at = now
        if step.started_at is not None:
            step.duration_ms = max(0, int((now - step.started_at).total_seconds() * 1000))
        step.lease_owner = None
        step.lease_token = None
        step.lease_expires_at = None
    if reason:
        step.reason = reason
        if target in {AgentRunStepStatus.FAILED, AgentRunStepStatus.BLOCKED}:
            step.last_error = reason
    return previous, target


def derive_run_status(steps: list[AgentRunStep], *, pause_requested: bool = False, cancel_requested: bool = False) -> AgentRunStatus:
    if cancel_requested:
        return AgentRunStatus.CANCELLED
    if any(step.status == AgentRunStepStatus.BLOCKED for step in steps):
        return AgentRunStatus.BLOCKED
    if any(step.status == AgentRunStepStatus.NEEDS_HUMAN for step in steps):
        return AgentRunStatus.NEEDS_HUMAN
    if any(step.status == AgentRunStepStatus.FAILED for step in steps):
        return AgentRunStatus.FAILED
    if pause_requested or any(step.status == AgentRunStepStatus.PAUSED for step in steps):
        return AgentRunStatus.PAUSED
    if steps and all(step.status in {AgentRunStepStatus.SUCCEEDED, AgentRunStepStatus.SKIPPED} for step in steps):
        return AgentRunStatus.SUCCEEDED
    if any(step.status == AgentRunStepStatus.RETRY_WAIT for step in steps):
        return AgentRunStatus.RETRY_WAIT
    if any(step.status == AgentRunStepStatus.RUNNING for step in steps):
        return AgentRunStatus.RUNNING
    return AgentRunStatus.SCHEDULED
