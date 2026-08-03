from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")
os.environ.setdefault("AIXION_PROFILE", "test")
os.environ.setdefault("AIXION_LEASE_SIGNING_KEY", "test-signing-key-with-sufficient-entropy")

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
from app.trust_credentials import (
    CredentialIntrospectionRequest,
    CredentialRevocationRequest,
    introspect_credential,
    revoke_credential_grant,
)
from app.trust_flight_recorder import verify_flight_recorder
from app.trust_models import (
    ActionConsumption,
    CapabilityActionType,
    CapabilityLeaseCreate,
    CapabilityLeaseMode,
    CapabilityScope,
    CredentialGrantCreate,
    PolicyDecisionType,
    ProposedAction,
    ReviewerAttestationCreate,
    ReviewerAttestationDecision,
)
from app.trust_service import (
    TrustControlConflict,
    build_exception_queue,
    build_reliability_scorecards,
    consume_gateway_action,
    evaluate_gateway_action,
    issue_capability_lease,
    issue_credential_grant,
    record_reviewer_attestation,
    revoke_capability_lease,
)


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


def _approved_task() -> tuple[ApprovalRequest, AgentTask]:
    project = Project(name="Trust Control", description="policy test")
    store.projects[project.id] = project
    approval = ApprovalRequest(
        project_id=project.id,
        title="Approved trust change",
        summary="Modify one approved file and run one approved test.",
        agent_name="codex",
        target_branch="feature/trust-change",
        files=[
            FileChange(
                path="backend/app/trusted.py",
                change_type="update",
                diff="+trusted",
                new_content="trusted = True\n",
            )
        ],
        test_plan=["python -m pytest backend/tests/test_trusted.py"],
        rollback_plan="Close the pull request.",
        risk=RiskAssessment(level=RiskLevel.HIGH),
        status=ApprovalStatus.APPROVED,
        created_by_user_id="creator",
        approved_by_user_id="reviewer_1",
        approved_payload_hash="approved-payload-hash",
    )
    store.approval_requests[approval.id] = approval
    task = AgentTask(
        provider=AgentProvider.CODEX,
        project_id=project.id,
        title="Apply approved trust change",
        goal="Create a validated PR.",
        repository="owner/repo",
        branch_preference="feature/trust-change",
        approval_request_id=approval.id,
        status=AgentTaskStatus.APPROVED,
    )
    store.agent_tasks[task.id] = task
    store.persist()
    return approval, task


def _lease_payload(approval_id: str, required_reviewers: int = 1) -> CapabilityLeaseCreate:
    return CapabilityLeaseCreate(
        approval_request_id=approval_id,
        provider=AgentProvider.CODEX,
        mode=CapabilityLeaseMode.BOUNDED,
        required_reviewer_count=required_reviewers,
        scope=CapabilityScope(
            repository="owner/repo",
            branch="feature/trust-change",
            allowed_actions=[
                CapabilityActionType.MODIFY_FILES,
                CapabilityActionType.RUN_COMMAND,
                CapabilityActionType.CREATE_PULL_REQUEST,
            ],
            allowed_path_prefixes=["backend/app/trusted.py"],
            allowed_commands=["python -m pytest backend/tests/test_trusted.py"],
            max_runtime_seconds=300,
            max_cost_usd=2.0,
            max_pull_requests=1,
        ),
    )


def test_multi_reviewer_lease_is_signed_scoped_and_idempotent() -> None:
    approval, _ = _approved_task()
    reviewer_2 = _user("reviewer_2", UserRole.REVIEWER)
    attestation = record_reviewer_attestation(
        ReviewerAttestationCreate(
            approval_request_id=approval.id,
            decision=ReviewerAttestationDecision.APPROVE,
            reason="Scope and rollback are acceptable.",
        ),
        user=reviewer_2,
    )
    assert attestation.signature

    payload = _lease_payload(approval.id, required_reviewers=2)
    lease = issue_capability_lease(payload, user=_user("issuer"))
    duplicate = issue_capability_lease(payload, user=_user("issuer"))

    assert lease.id == duplicate.id
    assert lease.receipt_signature
    assert lease.required_reviewer_count == 2
    assert lease.scope.allowed_path_prefixes == ["backend/app/trusted.py"]
    assert verify_flight_recorder().valid is True


def test_lease_rejects_broader_paths_commands_and_self_approval() -> None:
    approval, _ = _approved_task()
    broad = _lease_payload(approval.id)
    broad.scope.allowed_path_prefixes = ["backend/app"]
    with pytest.raises(TrustControlConflict, match="exceeds approved files"):
        issue_capability_lease(broad, user=_user("issuer"))

    command = _lease_payload(approval.id)
    command.scope.allowed_commands = ["bash -lc pytest"]
    with pytest.raises(TrustControlConflict, match="approved test plan"):
        issue_capability_lease(command, user=_user("issuer"))

    approval.created_by_user_id = "reviewer_1"
    with pytest.raises(TrustControlConflict, match="Self-approved"):
        issue_capability_lease(_lease_payload(approval.id), user=_user("issuer"))


def test_gateway_allows_consumes_and_prevents_conflicting_replay() -> None:
    approval, task = _approved_task()
    lease = issue_capability_lease(_lease_payload(approval.id), user=_user("issuer"))
    action = ProposedAction(
        lease_id=lease.id,
        provider=AgentProvider.CODEX,
        task_id=task.id,
        action_type=CapabilityActionType.MODIFY_FILES,
        repository="owner/repo",
        branch="feature/trust-change",
        paths=["backend/app/trusted.py"],
        estimated_runtime_seconds=30,
        estimated_cost_usd=0.25,
    )
    result = evaluate_gateway_action(action, actor="agent:codex")
    assert result.decision.decision == PolicyDecisionType.ALLOW

    consumption = ActionConsumption(
        actual_runtime_seconds=25,
        actual_cost_usd=0.20,
        evidence={"commit": "abc123"},
    )
    recorded = consume_gateway_action(action.id, consumption, actor="agent:codex")
    assert recorded == consumption
    assert lease.consumed_runtime_seconds == 25
    assert lease.consumed_cost_usd == 0.20

    assert consume_gateway_action(action.id, consumption, actor="agent:codex") == consumption
    with pytest.raises(TrustControlConflict, match="immutable"):
        consume_gateway_action(
            action.id,
            ActionConsumption(actual_runtime_seconds=26),
            actor="agent:codex",
        )
    assert verify_flight_recorder().valid is True


def test_short_lived_credential_is_scoped_introspectable_and_revocable() -> None:
    approval, _ = _approved_task()
    lease = issue_capability_lease(_lease_payload(approval.id), user=_user("issuer"))
    response = issue_credential_grant(
        CredentialGrantCreate(
            lease_id=lease.id,
            audience="aixion-worker",
            subject="agent:codex",
            scope=["run:execute", "evidence:write"],
            expires_in_seconds=120,
        ),
        user=_user("owner"),
    )
    active = introspect_credential(
        CredentialIntrospectionRequest(
            bearer_token=response.bearer_token,
            audience="aixion-worker",
            required_scope=["run:execute"],
        )
    )
    assert active.active is True
    assert active.grant_id == response.grant.id

    wrong_scope = introspect_credential(
        CredentialIntrospectionRequest(
            bearer_token=response.bearer_token,
            audience="aixion-worker",
            required_scope=["production:deploy"],
        )
    )
    assert wrong_scope.active is False

    revoke_credential_grant(
        response.grant,
        user=_user("owner"),
        reason=CredentialRevocationRequest(reason="Run cancelled.").reason,
    )
    revoked = introspect_credential(
        CredentialIntrospectionRequest(
            bearer_token=response.bearer_token,
            audience="aixion-worker",
        )
    )
    assert revoked.active is False
    assert "revoked" in revoked.reason.lower()


def test_revoking_lease_revokes_all_child_credentials() -> None:
    approval, _ = _approved_task()
    lease = issue_capability_lease(_lease_payload(approval.id), user=_user("issuer"))
    response = issue_credential_grant(
        CredentialGrantCreate(
            lease_id=lease.id,
            audience="worker",
            subject="agent:codex",
            scope=["run:execute"],
        ),
        user=_user("owner"),
    )
    revoke_capability_lease(
        lease,
        user=_user("owner"),
        reason="Operator revoked the run capability.",
    )
    assert response.grant.revoked is True


def test_scorecard_and_exception_queue_are_evidence_driven() -> None:
    approval, task = _approved_task()
    lease = issue_capability_lease(_lease_payload(approval.id), user=_user("issuer"))
    allowed = ProposedAction(
        lease_id=lease.id,
        provider=AgentProvider.CODEX,
        task_id=task.id,
        action_type=CapabilityActionType.MODIFY_FILES,
        repository="owner/repo",
        branch="feature/trust-change",
        paths=["backend/app/trusted.py"],
    )
    blocked = ProposedAction(
        lease_id=lease.id,
        provider=AgentProvider.CODEX,
        task_id=task.id,
        action_type=CapabilityActionType.MODIFY_FILES,
        repository="owner/repo",
        branch="feature/trust-change",
        paths=[".env"],
    )
    evaluate_gateway_action(allowed, actor="agent:codex")
    evaluate_gateway_action(blocked, actor="agent:codex")

    cards = build_reliability_scorecards()
    card = next(item for item in cards if item.provider == AgentProvider.CODEX)
    assert card.evaluated_actions == 2
    assert card.allowed_actions == 1
    assert card.blocked_actions == 1
    assert card.scope_adherence_rate == 0.5

    exceptions = build_exception_queue()
    assert any(item.action_id == blocked.id for item in exceptions)
    assert any(item.category == "BLOCK" for item in exceptions)
