from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")
os.environ.setdefault("AIXION_PROFILE", "test")
os.environ.setdefault(
    "AIXION_LEASE_SIGNING_KEY",
    "exact-action-integrity-test-signing-key",
)

import pytest

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
from app.trust_consumption import consume_effective_action
from app.trust_models import (
    ActionConsumption,
    CapabilityActionType,
    CapabilityLeaseCreate,
    CapabilityLeaseMode,
    CapabilityScope,
    PolicyDecisionType,
    ProposedAction,
)
from app.trust_service import (
    TrustControlConflict,
    evaluate_gateway_action,
    issue_capability_lease,
)


def setup_function() -> None:
    store.reset()


def _user(user_id: str, role: UserRole) -> AuthUser:
    return AuthUser(
        id=user_id,
        email=f"{user_id}@example.com",
        display_name=user_id,
        role=role,
        email_verified=True,
    )


def _authorized_action() -> tuple[ProposedAction, str]:
    project = Project(name="Exact action integrity", description="test")
    store.projects[project.id] = project
    approval = ApprovalRequest(
        project_id=project.id,
        title="Approve one exact mutation",
        summary="Modify one exact file.",
        agent_name="codex",
        target_branch="feature/exact-action-integrity",
        files=[
            FileChange(
                path="backend/app/exact_action.py",
                change_type="create",
                diff="+exact",
                new_content="exact = True\n",
            )
        ],
        test_plan=[],
        rollback_plan="Close the feature pull request.",
        risk=RiskAssessment(level=RiskLevel.HIGH),
        status=ApprovalStatus.APPROVED,
        created_by_user_id="creator",
        approved_by_user_id="approval-reviewer",
        approved_payload_hash="sealed-exact-action-payload",
    )
    store.approval_requests[approval.id] = approval
    task = AgentTask(
        provider=AgentProvider.CODEX,
        project_id=project.id,
        title="Exact action task",
        goal="Exercise exact action authorization.",
        repository="owner/repo",
        branch_preference="feature/exact-action-integrity",
        approval_request_id=approval.id,
        status=AgentTaskStatus.APPROVED,
    )
    store.agent_tasks[task.id] = task
    lease = issue_capability_lease(
        CapabilityLeaseCreate(
            approval_request_id=approval.id,
            provider=AgentProvider.CODEX,
            mode=CapabilityLeaseMode.STRICT,
            scope=CapabilityScope(
                repository="owner/repo",
                branch="feature/exact-action-integrity",
                allowed_actions=[CapabilityActionType.MODIFY_FILES],
                allowed_path_prefixes=["backend/app/exact_action.py"],
                allowed_commands=[],
                max_runtime_seconds=300,
                max_cost_usd=1.0,
                max_retries=0,
                max_pull_requests=0,
            ),
        ),
        user=_user("issuer", UserRole.OWNER),
    )
    action = ProposedAction(
        lease_id=lease.id,
        provider=AgentProvider.CODEX,
        task_id=task.id,
        project_id=project.id,
        action_type=CapabilityActionType.MODIFY_FILES,
        repository="owner/repo",
        branch="feature/exact-action-integrity",
        paths=["backend/app/exact_action.py"],
        estimated_runtime_seconds=10,
    )
    initial = evaluate_gateway_action(action, actor="agent:codex")
    assert initial.decision.decision == PolicyDecisionType.REQUIRE_APPROVAL
    authorization = record_action_authorization(
        action.id,
        ActionAuthorizationCreate(
            decision=ActionAuthorizationDecision.ALLOW,
            reason="Approve only this exact immutable mutation.",
        ),
        user=_user("human-reviewer", UserRole.REVIEWER),
    )
    return action, authorization.id


def test_signed_exact_action_can_be_consumed() -> None:
    action, _ = _authorized_action()
    consumption = ActionConsumption(
        actual_runtime_seconds=5,
        evidence={"commit": "abc123"},
    )
    assert (
        consume_effective_action(
            action.id,
            consumption,
            actor="agent:codex",
        )
        == consumption
    )


def test_action_payload_tampering_invalidates_human_allow() -> None:
    action, _ = _authorized_action()
    store.proposed_actions[action.id].paths = ["backend/app/other.py"]
    with pytest.raises(TrustControlConflict, match="payload hash"):
        consume_effective_action(
            action.id,
            ActionConsumption(actual_runtime_seconds=1),
            actor="agent:codex",
        )


def test_authorization_signature_tampering_invalidates_human_allow() -> None:
    action, authorization_id = _authorized_action()
    store.action_authorizations[authorization_id].signature = "tampered"
    with pytest.raises(TrustControlConflict, match="signature"):
        consume_effective_action(
            action.id,
            ActionConsumption(actual_runtime_seconds=1),
            actor="agent:codex",
        )
