from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import replace

import pytest

from app.agent_run_models import (
    AgentRun,
    AgentRunEvent,
    AgentRunStatus,
    AgentRunStep,
    AgentRunStepStatus,
    AgentRunStepType,
    VerificationDecision,
)
from app.agent_run_repository import PostgresAgentRunRepository
from app.agent_run_supervisor import (
    AgentRunConflict,
    build_postgres_branch_completion,
    execute_next_step,
)
from app.agent_task_models import AgentTask
from app.models import ApprovalRequest
from app.postgres_agent_run_branch import (
    AgentRunBranchIntent,
    BranchIntentStatus,
)
from app.postgres_agent_run_branch_executor import (
    BranchExecutionResult,
    BranchExecutionState,
    BranchExecutionRequest,
    PostgresCreateBranchCoordinator,
)
from app.store import store


class _MemoryPostgresRunRepository(PostgresAgentRunRepository):
    def __init__(self, run: AgentRun, steps: list[AgentRunStep]) -> None:
        self.run = run.model_copy(deep=True)
        self.steps = [step.model_copy(deep=True) for step in steps]
        self.events: list[AgentRunEvent] = []
        self.branch_completion_calls = 0

    def get(self, run_id: str):
        return self.run.model_copy(deep=True) if self.run.id == run_id else None

    def list_steps_for_run(self, run_id: str):
        return [step.model_copy(deep=True) for step in self.steps] if run_id == self.run.id else []

    def list_events_for_run(self, run_id: str):
        return [event.model_copy(deep=True) for event in self.events if event.run_id == run_id]

    def acquire_lease(self, *, run_id, worker_id, lease_seconds, now):
        assert run_id == self.run.id
        self.run.lease_owner = worker_id
        self.run.lease_token = "pg-test-lease"
        self.run.lease_expires_at = now + timedelta(seconds=lease_seconds)
        self.run.updated_at = now
        return self.get(run_id), None, None

    def commit_control_transition(
        self, *, expected_run, updated_run, expected_steps, updated_steps, events, audits
    ):
        assert self.run == expected_run
        self.run = updated_run.model_copy(deep=True)
        self.steps = [step.model_copy(deep=True) for step in updated_steps]
        self.events.extend(event.model_copy(deep=True) for event in events)

    def commit_step_completion(
        self, *, expected_run, updated_run, expected_steps, updated_steps,
        events, audit, now, branch_intent_id=None, branch_task_event=None,
        branch_task_audit=None,
    ):
        assert self.run == expected_run
        self.run = updated_run.model_copy(deep=True)
        self.steps = [step.model_copy(deep=True) for step in updated_steps]
        self.events.extend(event.model_copy(deep=True) for event in events)
        if branch_intent_id:
            self.branch_completion_calls += 1

    def update_lease(self, *, run_id, worker_id, lease_token, lease_seconds, now):
        raise AssertionError("successful branch completion must not release through generic path")


class _CompletingCoordinator(PostgresCreateBranchCoordinator):
    def __init__(self, repository: _MemoryPostgresRunRepository) -> None:
        self.repository = repository
        self.calls = 0

    def execute(self, request: BranchExecutionRequest) -> BranchExecutionResult:
        self.calls += 1
        intent = _intent_for(request)
        completion = request.completion_factory(intent, "https://github.com/org/repo/tree/feature-x")
        self.repository.commit_step_completion(
            expected_run=completion.expected_run,
            updated_run=completion.updated_run,
            expected_steps=completion.expected_steps,
            updated_steps=completion.updated_steps,
            events=completion.events,
            audit=completion.audit,
            now=completion.now,
            branch_intent_id=intent.id,
            branch_task_event=completion.branch_task_event,
            branch_task_audit=completion.branch_task_audit,
        )
        return BranchExecutionResult(
            BranchExecutionState.COMPLETED,
            intent,
            "verified",
            intent.source_sha,
            "https://github.com/org/repo/tree/feature-x",
        )


class _ReturningCoordinator(PostgresCreateBranchCoordinator):
    def __init__(self, state: BranchExecutionState) -> None:
        self.state = state

    def execute(self, request: BranchExecutionRequest) -> BranchExecutionResult:
        intent = _intent_for(request)
        if self.state == BranchExecutionState.RECONCILIATION_REQUIRED:
            intent.status = BranchIntentStatus.RECONCILIATION_REQUIRED
        return BranchExecutionResult(self.state, intent, "provider state requires attention")


def _intent_for(request: BranchExecutionRequest) -> AgentRunBranchIntent:
    now = datetime.now(timezone.utc)
    return AgentRunBranchIntent(
        id="branch-intent-1",
        workspace_id=request.expected_run.workspace_id,
        run_id=request.expected_run.id,
        task_id=request.expected_run.task_id,
        approval_id=request.expected_run.approval_request_id,
        step_id=request.expected_step.id,
        action_id="action-1",
        lease_id=request.expected_run.capability_lease_id,
        idempotency_key=request.expected_step.idempotency_key,
        repository="org/repo",
        source_branch="main",
        target_ref="feature-x",
        source_sha="a" * 40,
        lease_token_hash="b" * 64,
        initial_lease_token_hash="b" * 64,
        run_revision=request.expected_run.updated_at,
        step_revision=request.expected_step.updated_at,
        status=BranchIntentStatus.REMOTE_VERIFIED,
        created_at=now,
        updated_at=now,
    )


def _setup() -> tuple[AgentRun, AgentRunStep, _MemoryPostgresRunRepository]:
    store.reset()
    run = AgentRun(
        id="run-1",
        task_id="task-1",
        workspace_id="workspace-1",
        approval_request_id="approval-1",
        capability_lease_id="lease-1",
        project_id="project-1",
        repository="org/repo",
        status=AgentRunStatus.SCHEDULED,
        current_step_id="step-1",
        current_step_index=1,
    )
    step = AgentRunStep(
        id="step-1",
        run_id=run.id,
        task_id=run.task_id,
        sequence=1,
        step_type=AgentRunStepType.CREATE_BRANCH,
        status=AgentRunStepStatus.READY,
        idempotency_key="branch-key-1",
    )
    store.agent_runs[run.id] = run
    store.agent_run_steps[step.id] = step
    repository = _MemoryPostgresRunRepository(run, [step])
    return run, step, repository


def _request_factory(run, step, lease_token, timeout_seconds):
    return BranchExecutionRequest(
        workspace_id=run.workspace_id,
        actor="worker-1",
        worker_lease_token=lease_token,
        expected_run=run.model_copy(deep=True),
        expected_step=step.model_copy(deep=True),
        task=AgentTask(id=run.task_id, title="test", goal="test"),
        approval=ApprovalRequest(
            id=run.approval_request_id,
            project_id=run.project_id,
            title="test",
            summary="test",
            target_branch="feature-x",
            files=[],
            risk={"level": "LOW"},
        ),
        lease=object(),
        repository="org/repo",
        source_branch="main",
        target_ref="feature-x",
        timeout_seconds=timeout_seconds,
        action_factory=lambda sha: None,
        intent_factory=lambda sha, action: None,
        completion_factory=lambda intent, url: build_postgres_branch_completion(
            run=run.model_copy(deep=True),
            steps=[step.model_copy(deep=True)],
            intent=intent,
            branch_url=url,
            actor="worker-1",
            worker_lease_token=lease_token,
        ),
    )


def test_pg_branch_completion_reloads_and_skips_legacy_executor() -> None:
    run, _, repository = _setup()
    coordinator = _CompletingCoordinator(repository)
    result = execute_next_step(
        run.id,
        worker_id="worker-1",
        run_repository=repository,
        postgres_branch_coordinator=coordinator,
        postgres_branch_request_factory=_request_factory,
        executor=lambda *_: pytest.fail("legacy executor must not run"),
    )
    assert result.success is True
    assert result.decision == VerificationDecision.PASS
    assert coordinator.calls == 1
    assert repository.branch_completion_calls == 1
    assert repository.steps[0].status == AgentRunStepStatus.SUCCEEDED
    assert repository.steps[0].output_reference == "https://github.com/org/repo/tree/feature-x"


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (BranchExecutionState.BLOCKED, VerificationDecision.BLOCK_POLICY),
        (BranchExecutionState.RECONCILIATION_REQUIRED, VerificationDecision.NEEDS_HUMAN),
    ],
)
def test_pg_branch_unresolved_state_uses_governed_completion(state, expected) -> None:
    run, _, repository = _setup()
    result = execute_next_step(
        run.id,
        worker_id="worker-1",
        run_repository=repository,
        postgres_branch_coordinator=_ReturningCoordinator(state),
        postgres_branch_request_factory=_request_factory,
        executor=lambda *_: pytest.fail("legacy executor must not run"),
    )
    assert result.decision == expected
    assert repository.steps[0].status in {
        AgentRunStepStatus.BLOCKED,
        AgentRunStepStatus.NEEDS_HUMAN,
    }


def test_pg_branch_rejects_request_factory_lease_mismatch_without_legacy_fallback() -> None:
    run, _, repository = _setup()

    def mismatched_factory(run_snapshot, step_snapshot, lease_token, timeout_seconds):
        request = _request_factory(
            run_snapshot, step_snapshot, lease_token, timeout_seconds
        )
        return replace(request, worker_lease_token="different-lease")

    result = execute_next_step(
        run.id,
        worker_id="worker-1",
        run_repository=repository,
        postgres_branch_coordinator=_ReturningCoordinator(BranchExecutionState.COMPLETED),
        postgres_branch_request_factory=mismatched_factory,
        executor=lambda *_: pytest.fail("legacy executor must not run"),
    )
    assert result.decision == VerificationDecision.NEEDS_HUMAN
    assert repository.steps[0].status == AgentRunStepStatus.NEEDS_HUMAN


def test_pg_branch_injection_must_be_paired_and_sqlite_rejects_it() -> None:
    run, _, repository = _setup()
    with pytest.raises(AgentRunConflict, match="injected together"):
        execute_next_step(
            run.id,
            worker_id="worker-1",
            run_repository=repository,
            postgres_branch_coordinator=object(),
        )

    with pytest.raises(AgentRunConflict, match="unavailable for SQLite"):
        execute_next_step(
            run.id,
            worker_id="worker-1",
            postgres_branch_coordinator=PostgresCreateBranchCoordinator(
                intent_repository=None,
                run_repository=None,
                provider=None,
                clock=None,
            ),
            postgres_branch_request_factory=_request_factory,
        )
