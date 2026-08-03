from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")
os.environ.setdefault("AIXION_PROFILE", "test")

from app.agent_run_faults import consume_fault
from app.agent_run_metrics import build_agent_run_summary, prometheus_agent_run_metrics
from app.agent_run_models import (
    AgentRun,
    AgentRunFaultConfig,
    AgentRunFaultKind,
    AgentRunStatus,
    AgentRunStep,
    AgentRunStepStatus,
    AgentRunStepType,
)
from app.store import store


def setup_function() -> None:
    store.reset()


def test_fault_is_scoped_consumed_and_disabled() -> None:
    fault = AgentRunFaultConfig(
        run_id="run_1",
        step_type=AgentRunStepType.CREATE_BRANCH,
        fault_kind=AgentRunFaultKind.GITHUB_TIMEOUT,
        remaining_uses=1,
    )
    store.agent_run_faults[fault.id] = fault

    assert consume_fault("other_run", AgentRunStepType.CREATE_BRANCH) is None
    assert consume_fault("run_1", AgentRunStepType.APPLY_CHANGES) is None
    consumed = consume_fault("run_1", AgentRunStepType.CREATE_BRANCH)
    assert consumed is fault
    assert fault.remaining_uses == 0
    assert fault.enabled is False
    assert consume_fault("run_1", AgentRunStepType.CREATE_BRANCH) is None


def test_prometheus_metrics_expose_run_step_and_trust_truth() -> None:
    run = AgentRun(task_id="task_1", status=AgentRunStatus.RETRY_WAIT)
    step = AgentRunStep(
        run_id=run.id,
        task_id=run.task_id,
        sequence=0,
        step_type=AgentRunStepType.CREATE_BRANCH,
        status=AgentRunStepStatus.RETRY_WAIT,
        attempt_count=2,
        idempotency_key=f"{run.id}:0",
    )
    store.agent_runs[run.id] = run
    store.agent_run_steps[step.id] = step

    summary = build_agent_run_summary()
    assert summary.total == 1
    assert summary.retry_wait == 1
    assert summary.queue_depth == 1

    output = prometheus_agent_run_metrics()
    assert 'aixion_agent_runs_total{status="RETRY_WAIT"} 1' in output
    assert "aixion_agent_run_queue_depth 1" in output
    assert "aixion_agent_run_retry_attempts_total 1" in output
    assert "aixion_capability_leases_total" in output
    assert "aixion_policy_decisions_total" in output
    assert "aixion_trust_flight_recorder_valid 1" in output
    assert "aixion_trust_flight_recorder_events_total 0" in output
