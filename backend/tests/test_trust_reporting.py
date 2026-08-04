from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")
os.environ.setdefault("AIXION_PROFILE", "test")
os.environ.setdefault("AIXION_LEASE_SIGNING_KEY", "reporting-test-signing-key")

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
    ProposedAction,
)
from app.trust_reporting import (
    build_exception_queue,
    build_reliability_scorecards,
)
from app.trust_service import evaluate_gateway_action, issue_capability_lease


def setup_function() -> None:
    store.reset()


def _user(user_id: str, role: UserRole = UserRole.OWNER) -> AuthUser:
    return AuthUser(
        id=user_id,
        email=f"{user_id}@example.com",
        display_name=user_id,
        role=role,
        email_verified=True,
    )


def _strict_action() -> ProposedAction:
    project = Project(name="Reporting", description="effective decisions")
    store.projects[project.id] = project
    approval = ApprovalRequest(
        project_id=project.id,
        title="Reporting approval",
        summary="Approve one exact file.",
        agent_name="codex",
        target_branch="feature/reporting",
        files=[
            FileChange(
                path="backend/app/reporting.py",
                change_type="create",
                diff="+reporting",
                new_content="reporting = True\n",
            )
        ],
        test_plan=["python -m pytest backend/tests/test_reporting.py"],
        rollback_plan="Close the feature pull request.",
        risk=RiskAssessment(level=RiskLevel.HIGH),
        status=ApprovalStatus.APPROVED,
        created_by_user_id="creator",
        approved_by_user_id="reviewer",
        approved_payload_hash="sealed-reporting-hash",
    )
    store.approval_requests[approval.id] = approval
    task = AgentTask(
        provider=AgentProvider.CODEX,
        project_id=project.id,
        title="Reporting task",
        goal="Exercise exact approval reporting.",
        repository="owner/repo",
        branch_preference="feature/reporting",
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
                branch="feature/reporting",
                allowed_actions=[CapabilityActionType.MODIFY_FILES],
                allowed_path_prefixes=["backend/app/reporting.py"],
                allowed_commands=[],
                max_runtime_seconds=300,
                max_cost_usd=1.0,
                max_retries=1,
                max_pull_requests=0,
            ),
        ),
        user=_user("issuer"),
    )
    action = ProposedAction(
        lease_id=lease.id,
        provider=AgentProvider.CODEX,
        task_id=task.id,
        project_id=project.id,
        action_type=CapabilityActionType.MODIFY_FILES,
        repository="owner/repo",
        branch="feature/reporting",
        paths=["backend/app/reporting.py"],
    )
    result = evaluate_gateway_action(action, actor="agent:codex")
    assert result.decision.decision.value == "REQUIRE_APPROVAL"
    return action


def test_resolved_exact_action_disappears_from_exception_queue() -> None:
    action = _strict_action()
    before = build_exception_queue()
    assert any(item.action_id == action.id for item in before)

    record_action_authorization(
        action.id,
        ActionAuthorizationCreate(
            decision=ActionAuthorizationDecision.ALLOW,
            reason="Approve only this immutable file action.",
        ),
        user=_user("human-reviewer", UserRole.REVIEWER),
    )

    after = build_exception_queue()
    assert not any(item.action_id == action.id for item in after)
    card = next(
        item
        for item in build_reliability_scorecards()
        if item.provider == AgentProvider.CODEX
    )
    assert card.evaluated_actions == 1
    assert card.allowed_actions == 1
    assert card.approval_escalations == 0


def test_human_denial_remains_as_blocked_exception() -> None:
    action = _strict_action()
    record_action_authorization(
        action.id,
        ActionAuthorizationCreate(
            decision=ActionAuthorizationDecision.BLOCK,
            reason="The exact action is not acceptable.",
        ),
        user=_user("human-reviewer", UserRole.REVIEWER),
    )

    queue = build_exception_queue()
    item = next(entry for entry in queue if entry.action_id == action.id)
    assert item.category == "BLOCK"
    assert item.severity == RiskLevel.HIGH
