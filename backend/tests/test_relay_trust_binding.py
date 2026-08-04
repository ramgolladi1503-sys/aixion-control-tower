from __future__ import annotations

import os
from datetime import timedelta

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")
os.environ.setdefault("AIXION_PROFILE", "test")
os.environ.setdefault("AIXION_LEASE_SIGNING_KEY", "relay-binding-test-key")

import pytest

from app.agent_run_models import AgentRun
from app.agent_task_models import AgentTask, AgentTaskStatus
from app.models import AgentProvider, ApprovalRequest, ApprovalStatus, Project, now_utc
from app.relay_models import (
    RelayHost,
    RelayPlatform,
    RelayProvider,
    RelaySession,
)
from app.relay_service import RelayConflict
from app.relay_trust_binding import evaluate_bound_relay_action
from app.store import store
from app.trust_models import (
    CapabilityActionType,
    CapabilityLease,
    CapabilityLeaseMode,
    CapabilityScope,
    PolicyDecisionType,
    ProposedAction,
)


def setup_function() -> None:
    store.reset()


def _seed_bound_session() -> tuple[RelayHost, RelaySession, AgentTask, AgentRun]:
    project = Project(name="Relay binding", description="test")
    store.projects[project.id] = project
    approval = ApprovalRequest(
        project_id=project.id,
        title="Bound action",
        summary="Run one exact command.",
        agent_name="codex",
        target_branch="feature/relay-binding",
        test_plan=["python -m pytest backend/tests/test_safe.py"],
        rollback_plan="Close the feature pull request.",
        status=ApprovalStatus.APPROVED,
        approved_payload_hash="sealed-relay-binding-payload",
    )
    store.approval_requests[approval.id] = approval
    task = AgentTask(
        provider=AgentProvider.CODEX,
        project_id=project.id,
        title="Bound relay task",
        goal="Execute one approved command.",
        repository="owner/repo",
        branch_preference="feature/relay-binding",
        approval_request_id=approval.id,
        status=AgentTaskStatus.APPROVED,
    )
    store.agent_tasks[task.id] = task
    lease = CapabilityLease(
        approval_request_id=approval.id,
        approved_payload_hash=approval.approved_payload_hash,
        provider=AgentProvider.CODEX,
        mode=CapabilityLeaseMode.BOUNDED,
        scope=CapabilityScope(
            repository=task.repository,
            branch=task.branch_preference,
            allowed_actions=[CapabilityActionType.RUN_COMMAND],
            allowed_commands=["python -m pytest backend/tests/test_safe.py"],
            max_runtime_seconds=300,
            max_cost_usd=1.0,
            max_retries=1,
            max_pull_requests=0,
        ),
        issued_by_user_id="owner",
        expires_at=now_utc() + timedelta(minutes=10),
        receipt_nonce="relay-binding-nonce",
        receipt_signature="relay-binding-signature",
    )
    store.capability_leases[lease.id] = lease
    run = AgentRun(
        task_id=task.id,
        approval_request_id=approval.id,
        capability_lease_id=lease.id,
        project_id=project.id,
        repository=task.repository,
        objective=task.goal,
    )
    store.agent_runs[run.id] = run
    relay = RelayHost(
        name="Mac relay",
        platform=RelayPlatform.MACOS,
        hostname="mac.local",
        machine_fingerprint="b" * 64,
        token_hash="relay-token-hash",
        workspace_roots=["/Users/test/work"],
        allowed_repositories=[task.repository],
    )
    store.relay_hosts[relay.id] = relay
    session = RelaySession(
        relay_id=relay.id,
        provider=RelayProvider.CODEX,
        adapter_id="codex-app-server",
        objective=task.goal,
        workspace_path="/Users/test/work/repo",
        repository=task.repository,
        project_id=project.id,
        task_id=task.id,
        run_id=run.id,
    )
    store.relay_sessions[session.id] = session
    return relay, session, task, run


def test_bound_action_inherits_lease_task_repository_and_branch() -> None:
    relay, session, task, run = _seed_bound_session()
    result = evaluate_bound_relay_action(
        relay,
        session,
        ProposedAction(
            action_type=CapabilityActionType.RUN_COMMAND,
            command="python -m pytest backend/tests/test_safe.py",
            estimated_runtime_seconds=60,
        ),
    )

    assert result.decision.decision == PolicyDecisionType.ALLOW
    assert result.action.lease_id == run.capability_lease_id
    assert result.action.run_id == run.id
    assert result.action.task_id == task.id
    assert result.action.repository == task.repository
    assert result.action.branch == task.branch_preference
    assert result.action.metadata["durable_run_bound"] is True


def test_provider_cannot_substitute_repository_branch_or_lease() -> None:
    relay, session, _, _ = _seed_bound_session()
    for action in (
        ProposedAction(
            action_type=CapabilityActionType.RUN_COMMAND,
            repository="other/repo",
            command="python -m pytest backend/tests/test_safe.py",
        ),
        ProposedAction(
            action_type=CapabilityActionType.RUN_COMMAND,
            branch="main",
            command="python -m pytest backend/tests/test_safe.py",
        ),
        ProposedAction(
            action_type=CapabilityActionType.RUN_COMMAND,
            lease_id="lease_attacker",
            command="python -m pytest backend/tests/test_safe.py",
        ),
    ):
        with pytest.raises(RelayConflict, match="conflicts"):
            evaluate_bound_relay_action(relay, session, action)


def test_linked_run_without_capability_lease_fails_closed() -> None:
    relay, session, _, run = _seed_bound_session()
    run.capability_lease_id = None
    with pytest.raises(RelayConflict, match="no capability lease"):
        evaluate_bound_relay_action(
            relay,
            session,
            ProposedAction(
                action_type=CapabilityActionType.RUN_COMMAND,
                command="python -m pytest backend/tests/test_safe.py",
            ),
        )
