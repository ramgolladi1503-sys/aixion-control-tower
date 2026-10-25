from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest
from app import agent_run_supervisor

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from app.agent_run_models import (
    AgentRunCreate,
    AgentRunEventType,
    AgentRunStatus,
    AgentRunStep,
    AgentRunStepExecutionResult,
    AgentRunStepStatus,
    AgentRunStepType,
    VerificationDecision,
)
from app.agent_run_supervisor import (
    AgentRunValidationBundle,
    AgentRunConflict,
    DefaultAgentRunStepExecutor,
    acquire_run_lease,
    cancel_agent_run,
    create_agent_run,
    execute_next_step,
    heartbeat_run,
    inspect_validation_bundle,
    pause_agent_run,
    recover_stale_leases,
    release_run_lease,
    resume_agent_run,
    retry_agent_run_step,
)
from app.agent_run_repository import AgentRunRepositoryConflict, PostgresAgentRunRepository
from app.approval_decisions import (
    agent_run_repository,
    agent_task_repository,
    approval_decision_repository,
)
from app.agent_task_models import AgentTask, AgentTaskStatus
from app.approval_integrity import compute_approval_payload_hash
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


def test_postgres_run_scheduler_does_not_fall_back_to_stale_process_local_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = object.__new__(PostgresAgentRunRepository)
    looked_up: list[str] = []

    def get_from_postgres(_repository, run_id: str):
        looked_up.append(run_id)
        return None

    monkeypatch.setattr(PostgresAgentRunRepository, "get", get_from_postgres)
    monkeypatch.setattr(agent_run_supervisor, "agent_run_repository", repository)
    # A stale compatibility snapshot must not make the scheduler believe that
    # a run missing from the authoritative PostgreSQL repository exists.
    store.agent_runs["stale-local-run"] = object()

    with pytest.raises(ValueError, match="Agent run not found"):
        execute_next_step("stale-local-run", worker_id="worker-postgres")

    assert looked_up == ["stale-local-run"]
    assert list(store.agent_runs) == ["stale-local-run"]


@pytest.mark.parametrize(
    ("is_production", "postgres_auth_enabled", "postgres_runtime_probe_enabled"),
    [(True, False, False), (False, True, False), (False, False, True)],
)
def test_create_agent_run_requires_explicit_repositories_in_authoritative_modes(
    monkeypatch: pytest.MonkeyPatch,
    is_production: bool,
    postgres_auth_enabled: bool,
    postgres_runtime_probe_enabled: bool,
) -> None:
    monkeypatch.setattr(
        agent_run_supervisor,
        "get_settings",
        lambda: SimpleNamespace(
            is_production=is_production,
            postgres_auth_enabled=postgres_auth_enabled,
            postgres_runtime_probe_enabled=postgres_runtime_probe_enabled,
        ),
    )

    with pytest.raises(AgentRunConflict, match="explicit durable repositories"):
        create_agent_run(AgentRunCreate(task_id="must-not-read-sqlite"))


def test_create_agent_run_rejects_partial_repository_binding() -> None:
    with pytest.raises(AgentRunConflict, match="repositories together"):
        create_agent_run(
            AgentRunCreate(task_id="must-not-mix-stores"),
            task_repository=agent_task_repository,
        )


@pytest.mark.parametrize(
    "postgres_mode",
    ["postgres_auth_enabled", "postgres_runtime_probe_enabled"],
)
def test_create_agent_run_rejects_explicit_sqlite_repositories_in_postgres_mode(
    monkeypatch: pytest.MonkeyPatch,
    postgres_mode: str,
) -> None:
    monkeypatch.setattr(
        agent_run_supervisor,
        "get_settings",
        lambda: SimpleNamespace(
            is_production=False,
            postgres_auth_enabled=postgres_mode == "postgres_auth_enabled",
            postgres_runtime_probe_enabled=postgres_mode == "postgres_runtime_probe_enabled",
        ),
    )

    with pytest.raises(AgentRunConflict, match="requires PostgreSQL"):
        create_agent_run(
            AgentRunCreate(task_id="must-not-use-sqlite"),
            task_repository=agent_task_repository,
            approval_repository=approval_decision_repository,
            run_repository=agent_run_repository,
        )


@pytest.mark.parametrize(
    ("is_production", "postgres_auth_enabled", "postgres_runtime_probe_enabled"),
    [(True, False, False), (False, True, False), (False, False, True)],
)
def test_execute_next_step_requires_explicit_repository_in_authoritative_modes(
    monkeypatch: pytest.MonkeyPatch,
    is_production: bool,
    postgres_auth_enabled: bool,
    postgres_runtime_probe_enabled: bool,
) -> None:
    monkeypatch.setattr(
        agent_run_supervisor,
        "get_settings",
        lambda: SimpleNamespace(
            is_production=is_production,
            postgres_auth_enabled=postgres_auth_enabled,
            postgres_runtime_probe_enabled=postgres_runtime_probe_enabled,
        ),
    )

    with pytest.raises(AgentRunConflict, match="explicit durable repository"):
        execute_next_step("must-not-read-sqlite", worker_id="worker")


@pytest.mark.parametrize(
    "postgres_mode",
    ["postgres_auth_enabled", "postgres_runtime_probe_enabled"],
)
def test_execute_next_step_rejects_explicit_sqlite_repository_in_postgres_mode(
    monkeypatch: pytest.MonkeyPatch,
    postgres_mode: str,
) -> None:
    monkeypatch.setattr(
        agent_run_supervisor,
        "get_settings",
        lambda: SimpleNamespace(
            is_production=False,
            postgres_auth_enabled=postgres_mode == "postgres_auth_enabled",
            postgres_runtime_probe_enabled=postgres_mode == "postgres_runtime_probe_enabled",
        ),
    )

    with pytest.raises(AgentRunConflict, match="requires a PostgreSQL repository"):
        execute_next_step(
            "must-not-use-sqlite",
            worker_id="worker",
            run_repository=agent_run_repository,
        )


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
    approval.approved_payload_hash = compute_approval_payload_hash(approval)
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


def test_run_creation_failure_does_not_publish_compatibility_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _approved_task()

    def fail_create(**_kwargs: object) -> None:
        raise RuntimeError("simulated durable AgentRun failure")

    monkeypatch.setattr(agent_run_repository, "create", fail_create)
    with pytest.raises(RuntimeError, match="simulated durable AgentRun failure"):
        create_agent_run(AgentRunCreate(task_id=task.id), actor="owner@example.com")

    assert store.agent_runs == {}
    assert store.agent_run_steps == {}
    assert store.agent_run_events == {}
    with store._connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type IN "
            "('agent_run','agent_run_step','agent_run_event')"
        ).fetchone()[0] == 0


@pytest.mark.parametrize("tamper", ["payload", "status"])
def test_run_creation_rejects_noncanonical_approval_before_creating_run(tamper: str) -> None:
    task = _approved_task()
    approval = store.approval_requests[task.approval_request_id]
    if tamper == "payload":
        approval.files[0].diff = "+changed after approval"
    else:
        approval.status = ApprovalStatus.DENIED
    store.persist()

    with pytest.raises(AgentRunConflict):
        create_agent_run(AgentRunCreate(task_id=task.id), actor="owner@example.com")

    assert store.agent_runs == {}
    assert store.agent_run_steps == {}


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
    assert agent_run_repository.get(run.id) == run


def test_step_completion_commit_failure_restores_running_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _approved_task()
    run = create_agent_run(AgentRunCreate(task_id=task.id), actor="owner@example.com")
    step = store.agent_run_steps[run.current_step_id]

    def fail_completion(**_kwargs: object) -> None:
        raise RuntimeError("simulated completion transaction failure")

    monkeypatch.setattr(agent_run_repository, "commit_step_completion", fail_completion)

    with pytest.raises(AgentRunConflict, match="could not be committed"):
        execute_next_step(run.id, worker_id="worker-1", executor=_pass_executor)

    durable_run = agent_run_repository.get(run.id)
    assert durable_run is not None
    assert durable_run.status == AgentRunStatus.RUNNING
    assert durable_run.lease_token
    assert run == durable_run
    assert step.status == AgentRunStepStatus.RUNNING
    assert step.lease_token == durable_run.lease_token
    assert not any(
        event.event_type == AgentRunEventType.STEP_SUCCEEDED
        and event.step_id == step.id
        for event in store.agent_run_events.values()
    )
    assert not any(
        event.event_type == "agent_run.step_completed"
        and event.entity_id == run.id
        for event in store.audit_events
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


def test_default_executor_loads_scope_inputs_from_repositories_not_store_maps() -> None:
    task = _approved_task()
    run = create_agent_run(AgentRunCreate(task_id=task.id), actor="owner@example.com")
    step = store.agent_run_steps[run.current_step_id]
    approval = store.approval_requests[task.approval_request_id]
    store.agent_tasks.clear()
    store.approval_requests.clear()

    executor = DefaultAgentRunStepExecutor(
        task_repository=agent_task_repository,
        approval_repository=approval_decision_repository,
        run_repository=agent_run_repository,
    )
    result = executor(run, step, 30)

    assert result.decision == VerificationDecision.PASS
    assert result.evidence["repository"] == task.repository
    assert result.evidence["approval_status"] == approval.status.value


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
    store.persist()

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
    assert agent_run_repository.get(run.id) == run
    with store._connect() as connection:
        row = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type='agent_run_step' AND entity_id=?",
            (step.id,),
        ).fetchone()
        assert row is not None
        assert AgentRunStep.model_validate_json(row[0]).status == AgentRunStepStatus.READY
        assert connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type='audit_event' "
            "AND json_extract(payload,'$.event_type')='agent_run.step_retry_requested' "
            "AND json_extract(payload,'$.entity_id')=?",
            (run.id,),
        ).fetchone()[0] == 1


def test_step_retry_commit_failure_keeps_previous_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _approved_task()
    run = create_agent_run(AgentRunCreate(task_id=task.id), actor="owner@example.com")

    def needs_human(_run, _step, _timeout):
        return AgentRunStepExecutionResult(
            success=False,
            decision=VerificationDecision.NEEDS_REVISION,
            reason="Manual revision required.",
        )

    execute_next_step(run.id, worker_id="worker-1", executor=needs_human)
    step = store.agent_run_steps[run.current_step_id]
    expected_run = run.model_copy(deep=True)
    expected_step = step.model_copy(deep=True)

    def fail_control_transition(**_kwargs: object) -> None:
        raise AgentRunRepositoryConflict("simulated database conflict")

    monkeypatch.setattr(agent_run_repository, "commit_control_transition", fail_control_transition)
    with pytest.raises(AgentRunConflict, match="retry was not committed"):
        retry_agent_run_step(run, step, actor="owner@example.com", reason="Try again")

    assert run == expected_run
    assert step == expected_step
    assert agent_run_repository.get(run.id) == expected_run


def test_pause_and_resume_commit_run_step_event_and_audit_atomically() -> None:
    task = _approved_task()
    run = create_agent_run(AgentRunCreate(task_id=task.id), actor="owner@example.com")
    step = store.agent_run_steps[run.current_step_id]

    pause_agent_run(run, actor="owner@example.com", reason="Review before continuing")
    assert run.status == AgentRunStatus.PAUSED
    assert run.pause_requested
    assert agent_run_repository.get(run.id) == run
    with store._connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type='audit_event' "
            "AND json_extract(payload,'$.event_type')='agent_run.paused' "
            "AND json_extract(payload,'$.entity_id')=?",
            (run.id,),
        ).fetchone()[0] == 1

    resume_agent_run(run, actor="owner@example.com", reason="Review complete")
    assert run.status == AgentRunStatus.SCHEDULED
    assert not run.pause_requested
    assert step.status == AgentRunStepStatus.READY
    assert agent_run_repository.get(run.id) == run
    with store._connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type='audit_event' "
            "AND json_extract(payload,'$.event_type')='agent_run.resumed' "
            "AND json_extract(payload,'$.entity_id')=?",
            (run.id,),
        ).fetchone()[0] == 1


def test_pause_commit_failure_leaves_cache_and_durable_run_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _approved_task()
    run = create_agent_run(AgentRunCreate(task_id=task.id), actor="owner@example.com")
    expected_run = run.model_copy(deep=True)
    expected_step = store.agent_run_steps[run.current_step_id].model_copy(deep=True)

    def fail_control_transition(**_kwargs: object) -> None:
        raise AgentRunRepositoryConflict("simulated pause conflict")

    monkeypatch.setattr(agent_run_repository, "commit_control_transition", fail_control_transition)
    with pytest.raises(AgentRunConflict, match="Pause was not committed"):
        pause_agent_run(run, actor="owner@example.com", reason="Pause")

    assert run == expected_run
    assert store.agent_run_steps[expected_step.id] == expected_step
    assert agent_run_repository.get(run.id) == expected_run


def test_pause_rejects_an_active_worker_lease() -> None:
    task = _approved_task()
    run = create_agent_run(AgentRunCreate(task_id=task.id), actor="owner@example.com")
    acquire_run_lease(run, worker_id="worker-1", lease_seconds=30)

    with pytest.raises(AgentRunConflict, match="worker lease is active"):
        pause_agent_run(run, actor="owner@example.com", reason="Pause")


def test_cancel_commits_all_durable_steps_but_leaves_agent_task_state_unchanged() -> None:
    task = _approved_task()
    run = create_agent_run(AgentRunCreate(task_id=task.id), actor="owner@example.com")
    durable_steps = agent_run_repository.list_steps_for_run(run.id)
    del store.agent_run_steps[durable_steps[-1].id]

    cancel_agent_run(run, actor="owner@example.com", reason="Operator stopped this run")

    assert run.status == AgentRunStatus.CANCELLED
    assert run.cancel_requested
    assert store.agent_tasks[task.id].status == AgentTaskStatus.APPROVED
    persisted_steps = agent_run_repository.list_steps_for_run(run.id)
    assert len(persisted_steps) == len(durable_steps) == 7
    assert all(step.status == AgentRunStepStatus.CANCELLED for step in persisted_steps)
    assert all(step.status == AgentRunStepStatus.CANCELLED for step in _run_steps_for_test(run.id))
    with store._connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type='audit_event' "
            "AND json_extract(payload,'$.event_type')='agent_run.cancelled' "
            "AND json_extract(payload,'$.entity_id')=?",
            (run.id,),
        ).fetchone()[0] == 1


def test_cancel_commit_failure_keeps_run_and_steps_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _approved_task()
    run = create_agent_run(AgentRunCreate(task_id=task.id), actor="owner@example.com")
    expected_run = run.model_copy(deep=True)
    expected_steps = [step.model_copy(deep=True) for step in agent_run_repository.list_steps_for_run(run.id)]

    def fail_control_transition(**_kwargs: object) -> None:
        raise AgentRunRepositoryConflict("simulated cancellation conflict")

    monkeypatch.setattr(agent_run_repository, "commit_control_transition", fail_control_transition)
    with pytest.raises(AgentRunConflict, match="Cancellation was not committed"):
        cancel_agent_run(run, actor="owner@example.com", reason="Cancel")

    assert run == expected_run
    assert agent_run_repository.get(run.id) == expected_run
    assert agent_run_repository.list_steps_for_run(run.id) == expected_steps


def _run_steps_for_test(run_id: str):
    return [step for step in store.agent_run_steps.values() if step.run_id == run_id]


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
    durable_run = agent_run_repository.get(run.id)
    assert durable_run is not None
    assert durable_run.status == AgentRunStatus.RETRY_WAIT
    assert agent_run_repository.list_expired_leases(now=datetime.now(timezone.utc)) == []
    with store._connect() as connection:
        durable_step = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type='agent_run_step' AND entity_id=?",
            (step.id,),
        ).fetchone()
        assert durable_step is not None
        assert AgentRunStep.model_validate_json(durable_step[0]).status == AgentRunStepStatus.RETRY_WAIT
        assert connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type='audit_event' "
            "AND json_extract(payload,'$.event_type')='agent_run.stale_lease_recovered' "
            "AND json_extract(payload,'$.entity_id')=?",
            (run.id,),
        ).fetchone()[0] == 1


def test_running_checkpoint_failure_releases_lease_and_skips_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _approved_task()
    run = create_agent_run(AgentRunCreate(task_id=task.id), actor="owner@example.com")
    executor_called = False

    def fail_checkpoint(**_kwargs: object) -> None:
        raise RuntimeError("simulated RUNNING checkpoint failure")

    def executor(_run, _step, _timeout):
        nonlocal executor_called
        executor_called = True
        return _pass_executor(_run, _step, _timeout)

    monkeypatch.setattr(agent_run_repository, "commit_control_transition", fail_checkpoint)
    with pytest.raises(AgentRunConflict, match="RUNNING checkpoint"):
        execute_next_step(run.id, worker_id="worker-1", executor=executor)

    durable_run = agent_run_repository.get(run.id)
    assert durable_run is not None
    assert durable_run.status == AgentRunStatus.SCHEDULED
    assert durable_run.lease_owner is None
    assert durable_run.lease_token is None
    assert not executor_called
    assert agent_run_repository.list_steps_for_run(run.id)[0].status == AgentRunStepStatus.READY
    assert all(
        event.event_type != AgentRunEventType.STEP_STARTED
        for event in agent_run_repository.list_events_for_run(run.id)
    )


def test_concurrent_watchdogs_recover_a_stale_lease_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    original_recovery = agent_run_repository.recover_expired_lease
    both_ready = Barrier(2)

    def synchronize_recovery(**kwargs: object) -> None:
        both_ready.wait()
        original_recovery(**kwargs)

    monkeypatch.setattr(agent_run_repository, "recover_expired_lease", synchronize_recovery)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: recover_stale_leases(), range(2)))

    assert sum(outcomes) == 1
    assert run.status == AgentRunStatus.RETRY_WAIT
    assert step.status == AgentRunStepStatus.RETRY_WAIT
    with store._connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type='agent_run_event' "
            "AND json_extract(payload,'$.event_type')='STALE_LEASE_RECOVERED' "
            "AND json_extract(payload,'$.run_id')=?",
            (run.id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type='audit_event' "
            "AND json_extract(payload,'$.event_type')='agent_run.stale_lease_recovered' "
            "AND json_extract(payload,'$.entity_id')=?",
            (run.id,),
        ).fetchone()[0] == 1


def test_heartbeat_and_release_are_durable_lease_transactions() -> None:
    task = _approved_task()
    run = create_agent_run(AgentRunCreate(task_id=task.id), actor="owner@example.com")
    step = store.agent_run_steps[run.current_step_id]
    token = acquire_run_lease(run, worker_id="worker-1", lease_seconds=30)
    step.lease_owner = "worker-1"
    step.lease_token = token
    step.lease_expires_at = run.lease_expires_at
    store.persist()

    with pytest.raises(AgentRunConflict, match="heartbeat rejected"):
        heartbeat_run(run, worker_id="worker-1", lease_token="wrong-token", lease_seconds=30)

    renewed = heartbeat_run(run, worker_id="worker-1", lease_token=token, lease_seconds=60)
    assert renewed is run
    assert run.lease_expires_at is not None
    assert step.lease_expires_at == run.lease_expires_at
    durable_run = agent_run_repository.get(run.id)
    assert durable_run == run
    with store._connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type='agent_run_event' "
            "AND json_extract(payload,'$.event_type')='RUN_HEARTBEAT' "
            "AND json_extract(payload,'$.run_id')=?",
            (run.id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type='audit_event' "
            "AND json_extract(payload,'$.event_type')='agent_run.lease_heartbeat' "
            "AND json_extract(payload,'$.entity_id')=?",
            (run.id,),
        ).fetchone()[0] == 1

    release_run_lease(run, worker_id="worker-1", lease_token=token)
    assert run.lease_token is None
    assert step.lease_token is None
    assert agent_run_repository.get(run.id) == run
    with store._connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type='agent_run_event' "
            "AND json_extract(payload,'$.event_type')='RUN_LEASE_RELEASED' "
            "AND json_extract(payload,'$.run_id')=?",
            (run.id,),
        ).fetchone()[0] == 1


def test_heartbeat_after_lease_expiry_is_rejected() -> None:
    task = _approved_task()
    run = create_agent_run(AgentRunCreate(task_id=task.id), actor="owner@example.com")
    token = acquire_run_lease(run, worker_id="worker-1", lease_seconds=30)
    run.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    store.persist()

    with pytest.raises(AgentRunConflict, match="owner, token, or expiry"):
        heartbeat_run(run, worker_id="worker-1", lease_token=token, lease_seconds=30)

    durable_run = agent_run_repository.get(run.id)
    assert durable_run == run
    assert durable_run.lease_token == token


def test_validation_bundle_inspection_binds_clean_git_head_and_tree(tmp_path: Path) -> None:
    checkout = tmp_path / "bundle"
    checkout.mkdir()

    def git(*args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(checkout), *args],
            capture_output=True,
            text=True,
            check=True,
            shell=False,
        )
        return completed.stdout.strip()

    git("init", "--initial-branch=feature/approved")
    git("config", "user.email", "test@example.test")
    git("config", "user.name", "Validation test")
    (checkout / "README.md").write_text("pinned content\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-m", "seed approved snapshot")
    revision = git("rev-parse", "--verify", "HEAD")
    tree_sha = git("rev-parse", "--verify", "HEAD^{tree}")
    bundle = AgentRunValidationBundle(
        path=checkout,
        repository="owner/repository",
        branch="feature/approved",
        revision=revision,
        git_tree_sha=tree_sha,
    )

    assert inspect_validation_bundle(bundle) == tree_sha
    git("config", "core.excludesFile", str(checkout / ".git/info/exclude"))
    with (checkout / ".git/info/exclude").open("a", encoding="utf-8") as excludes:
        excludes.write("ignored-secret.txt\n")
    (checkout / "ignored-secret.txt").write_text("ignored contents\n", encoding="utf-8")
    with pytest.raises(AgentRunConflict, match="wrong ref, dirty, or has changed identity"):
        inspect_validation_bundle(bundle)
    (checkout / "ignored-secret.txt").unlink()
    (checkout / "README.md").write_text("changed after provider check\n", encoding="utf-8")
    with pytest.raises(AgentRunConflict, match="wrong ref, dirty, or has changed identity"):
        inspect_validation_bundle(bundle)
