from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")
os.environ.setdefault("AIXION_PROFILE", "test")
os.environ.setdefault("AIXION_LEASE_SIGNING_KEY", "run-trust-test-signing-key")

import pytest

from app.agent_run_models import AgentRunCreate, AgentRunStepStatus, AgentRunStepType
from app.agent_run_supervisor import create_agent_run
from app.agent_run_trust import (
    AgentRunTrustConflict,
    attach_capability_lease,
    authorize_run_step,
    consume_run_step_actions,
)
from app.agent_task_models import AgentTask, AgentTaskStatus
from app.models import (
    AgentProvider,
    ApprovalRequest,
    ApprovalStatus,
    AuthUser,
    FileChange,
    Project,
    RiskAssessment,
    RiskLevel,
    UserRole,
)
from app.store import store
from app.trust_action_authorization import record_action_authorization
from app.trust_action_authorization_models import (
    ActionAuthorizationCreate,
    ActionAuthorizationDecision,
)
from app.trust_models import (
    CapabilityActionType,
    CapabilityLeaseCreate,
    CapabilityLeaseMode,
    CapabilityScope,
)
from app.trust_service import issue_capability_lease


def setup_function() -> None:
    store.reset()


def _user(user_id: str) -> AuthUser:
    return AuthUser(
        id=user_id,
        email=f"{user_id}@example.com",
        display_name=user_id,
        role=UserRole.OWNER,
        email_verified=True,
    )


def _run_and_lease(mode: CapabilityLeaseMode = CapabilityLeaseMode.BOUNDED):
    project = Project(name="Run trust", description="test")
    store.projects[project.id] = project
    approval = ApprovalRequest(
        project_id=project.id,
        title="Run trust approval",
        summary="Approved file and validation.",
        agent_name="codex",
        target_branch="feature/run-trust",
        files=[
            FileChange(
                path="backend/app/trusted.py",
                change_type="update",
                diff="+trusted",
                new_content="trusted = True\n",
            )
        ],
        test_plan=["python -m pytest backend/tests/test_trusted.py"],
        rollback_plan="Close PR.",
        risk=RiskAssessment(level=RiskLevel.HIGH),
        status=ApprovalStatus.APPROVED,
        created_by_user_id="creator",
        approved_by_user_id="reviewer",
        approved_payload_hash="approved-run-trust-hash",
    )
    store.approval_requests[approval.id] = approval
    task = AgentTask(
        provider=AgentProvider.CODEX,
        project_id=project.id,
        title="Run trust task",
        goal="Create a safe PR.",
        repository="owner/repo",
        branch_preference="feature/run-trust",
        approval_request_id=approval.id,
        status=AgentTaskStatus.APPROVED,
    )
    store.agent_tasks[task.id] = task
    run = create_agent_run(AgentRunCreate(task_id=task.id), actor="owner@example.com")
    lease = issue_capability_lease(
        CapabilityLeaseCreate(
            approval_request_id=approval.id,
            provider=AgentProvider.CODEX,
            mode=mode,
            scope=CapabilityScope(
                repository="owner/repo",
                branch="feature/run-trust",
                allowed_actions=[
                    CapabilityActionType.CREATE_BRANCH,
                    CapabilityActionType.MODIFY_FILES,
                    CapabilityActionType.RUN_COMMAND,
                    CapabilityActionType.CREATE_PULL_REQUEST,
                ],
                allowed_path_prefixes=["backend/app/trusted.py"],
                allowed_commands=["python -m pytest backend/tests/test_trusted.py"],
                max_runtime_seconds=1200,
                max_cost_usd=2.0,
                max_retries=3,
                max_pull_requests=1,
            ),
        ),
        user=_user("issuer"),
    )
    attach_capability_lease(run, lease.id, actor="issuer@example.com")
    return run, lease


def _step(run, step_type: AgentRunStepType):
    return next(
        step
        for step in store.agent_run_steps.values()
        if step.run_id == run.id and step.step_type == step_type
    )


def test_production_enforcement_requires_attached_lease(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_TRUST_ENFORCEMENT", "true")
    run, _ = _run_and_lease()
    run.capability_lease_id = None
    branch_step = _step(run, AgentRunStepType.CREATE_BRANCH)
    with pytest.raises(AgentRunTrustConflict, match="requires a signed capability lease"):
        authorize_run_step(
            run,
            branch_step,
            actor="owner@example.com",
            timeout_seconds=120,
        )


def test_bounded_lease_allows_exact_run_side_effect(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_TRUST_ENFORCEMENT", "true")
    run, lease = _run_and_lease()
    branch_step = _step(run, AgentRunStepType.CREATE_BRANCH)
    actions = authorize_run_step(
        run,
        branch_step,
        actor="owner@example.com",
        timeout_seconds=120,
    )
    assert len(actions) == 1
    assert actions[0].lease_id == lease.id
    assert actions[0].action_type == CapabilityActionType.CREATE_BRANCH

    branch_step.status = AgentRunStepStatus.SUCCEEDED
    branch_step.duration_ms = 2500
    branch_step.evidence_hash = "branch-evidence"
    consume_run_step_actions(actions, branch_step, actor="owner@example.com")
    assert lease.consumed_runtime_seconds == 2
    assert actions[0].id in store.action_consumptions


def test_strict_lease_waits_for_exact_human_action_decision(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_TRUST_ENFORCEMENT", "true")
    run, _ = _run_and_lease(CapabilityLeaseMode.STRICT)
    branch_step = _step(run, AgentRunStepType.CREATE_BRANCH)

    with pytest.raises(AgentRunTrustConflict, match="Exact action approval"):
        authorize_run_step(
            run,
            branch_step,
            actor="owner@example.com",
            timeout_seconds=120,
        )
    action = next(
        item
        for item in store.proposed_actions.values()
        if item.run_id == run.id and item.metadata["step_id"] == branch_step.id
    )
    record_action_authorization(
        action.id,
        ActionAuthorizationCreate(
            decision=ActionAuthorizationDecision.ALLOW,
            reason="Approve this exact branch creation action.",
        ),
        user=_user("human-reviewer"),
    )

    actions = authorize_run_step(
        run,
        branch_step,
        actor="owner@example.com",
        timeout_seconds=120,
    )
    assert [item.id for item in actions] == [action.id]


def test_run_cannot_be_rebound_to_different_lease() -> None:
    run, lease = _run_and_lease()
    other = lease.model_copy(deep=True)
    other.id = "lease_other"
    store.capability_leases[other.id] = other
    with pytest.raises(AgentRunTrustConflict, match="cannot be rebound"):
        attach_capability_lease(run, other.id, actor="owner@example.com")
