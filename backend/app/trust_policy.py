from __future__ import annotations

import shlex
from datetime import datetime, timezone
from pathlib import PurePosixPath

from .models import RiskLevel
from .trust_crypto import verify_payload_signature
from .trust_models import (
    CapabilityActionType,
    CapabilityLease,
    CapabilityLeaseMode,
    CapabilityLeaseStatus,
    PolicyDecision,
    PolicyDecisionType,
    ProposedAction,
)

POLICY_VERSION = "trust-policy-v1"
PROTECTED_BRANCHES = {"main", "master"}
SENSITIVE_PATH_PARTS = {
    ".env",
    ".git",
    ".ssh",
    "credentials",
    "private_key",
    "secrets",
    "id_rsa",
}
SHELL_META = {"&&", "||", ";", "|", ">", ">>", "<", "`", "$("}
HIGH_RISK_ACTIONS = {
    CapabilityActionType.DEPLOY,
    CapabilityActionType.READ_SECRET,
    CapabilityActionType.WRITE_DATABASE,
}


def lease_receipt_payload(lease: CapabilityLease) -> dict:
    return {
        "id": lease.id,
        "approval_request_id": lease.approval_request_id,
        "approved_payload_hash": lease.approved_payload_hash,
        "agent_id": lease.agent_id,
        "provider": lease.provider,
        "mode": lease.mode,
        "scope": lease.scope.model_dump(mode="json"),
        "issued_by_user_id": lease.issued_by_user_id,
        "issued_at": lease.issued_at,
        "expires_at": lease.expires_at,
        "receipt_nonce": lease.receipt_nonce,
    }


def _decision(
    action: ProposedAction,
    decision: PolicyDecisionType,
    risk: RiskLevel,
    reasons: list[str],
    obligations: list[str] | None = None,
) -> PolicyDecision:
    return PolicyDecision(
        proposed_action_id=action.id,
        lease_id=action.lease_id,
        decision=decision,
        risk_level=risk,
        reasons=reasons,
        obligations=obligations or [],
        evaluated_policy_version=POLICY_VERSION,
    )


def _clean_path(value: str) -> str | None:
    candidate = value.strip().replace("\\", "/")
    if not candidate or candidate.startswith("/"):
        return None
    path = PurePosixPath(candidate)
    if ".." in path.parts or "." == str(path):
        return None
    return str(path)


def _path_allowed(path: str, prefixes: list[str]) -> bool:
    clean = _clean_path(path)
    if clean is None:
        return False
    lowered_parts = {part.lower() for part in PurePosixPath(clean).parts}
    if lowered_parts & SENSITIVE_PATH_PARTS:
        return False
    for prefix in prefixes:
        clean_prefix = _clean_path(prefix.rstrip("/"))
        if clean_prefix and (clean == clean_prefix or clean.startswith(clean_prefix + "/")):
            return True
    return False


def _command_allowed(command: str, allowed: list[str]) -> bool:
    cleaned = command.strip()
    if not cleaned:
        return False
    try:
        tokens = shlex.split(cleaned)
    except ValueError:
        return False
    if not tokens:
        return False
    if any(marker in cleaned for marker in SHELL_META):
        return cleaned in allowed
    for approved in allowed:
        approved_clean = approved.strip()
        if cleaned == approved_clean or cleaned.startswith(approved_clean + " "):
            return True
    return False


def _domain_allowed(domain: str, allowed: list[str]) -> bool:
    candidate = domain.strip().lower().rstrip(".")
    if not candidate or "/" in candidate or ":" in candidate:
        return False
    for configured in allowed:
        base = configured.strip().lower().rstrip(".")
        if candidate == base or candidate.endswith("." + base):
            return True
    return False


def evaluate_proposed_action(
    action: ProposedAction,
    lease: CapabilityLease | None,
    *,
    now: datetime | None = None,
) -> PolicyDecision:
    instant = now or datetime.now(timezone.utc)
    reasons: list[str] = []

    if lease is None:
        if action.action_type in HIGH_RISK_ACTIONS:
            return _decision(
                action,
                PolicyDecisionType.BLOCK,
                RiskLevel.CRITICAL,
                ["High-risk actions require an active signed capability lease."],
            )
        return _decision(
            action,
            PolicyDecisionType.REQUIRE_APPROVAL,
            RiskLevel.HIGH,
            ["No capability lease was supplied."],
            ["Create a scoped approval and issue a short-lived capability lease."],
        )

    if lease.id != action.lease_id:
        reasons.append("Action lease identifier does not match the evaluated lease.")
    if lease.status != CapabilityLeaseStatus.ACTIVE:
        reasons.append(f"Capability lease is not active: {lease.status}.")
    if lease.expires_at <= instant:
        reasons.append("Capability lease has expired.")
    if not verify_payload_signature(lease_receipt_payload(lease), lease.receipt_signature):
        reasons.append("Capability lease receipt signature is invalid.")
    if lease.agent_id and action.agent_id != lease.agent_id:
        reasons.append("Agent identity does not match the lease subject.")
    if action.provider != lease.provider:
        reasons.append("Agent provider does not match the lease provider.")
    if action.action_type not in lease.scope.allowed_actions:
        reasons.append(f"Action type {action.action_type} is outside the approved lease.")
    if action.repository and action.repository != lease.scope.repository:
        reasons.append("Repository does not match the approved capability scope.")
    if action.branch and action.branch != lease.scope.branch:
        reasons.append("Branch does not match the approved capability scope.")
    if action.branch in PROTECTED_BRANCHES:
        reasons.append("Protected branches cannot receive agent mutations.")
    if bool(action.metadata.get("auto_merge")) or lease.scope.allow_auto_merge:
        reasons.append("Auto-merge is prohibited by the Aixion trust policy.")

    invalid_paths = [
        path
        for path in action.paths
        if not _path_allowed(path, lease.scope.allowed_path_prefixes)
    ]
    if invalid_paths:
        reasons.append("Paths outside approved scope: " + ", ".join(sorted(invalid_paths)))

    if action.command and not _command_allowed(action.command, lease.scope.allowed_commands):
        reasons.append("Command is not covered by the approved command allowlist.")

    invalid_domains = [
        domain
        for domain in action.network_domains
        if not _domain_allowed(domain, lease.scope.allowed_network_domains)
    ]
    if invalid_domains:
        reasons.append("Network domains outside approved scope: " + ", ".join(invalid_domains))

    projected_runtime = lease.consumed_runtime_seconds + action.estimated_runtime_seconds
    if projected_runtime > lease.scope.max_runtime_seconds:
        reasons.append("Projected runtime exceeds the capability budget.")
    projected_cost = lease.consumed_cost_usd + action.estimated_cost_usd
    if projected_cost > lease.scope.max_cost_usd:
        reasons.append("Projected cost exceeds the capability budget.")
    if action.retry_number > lease.scope.max_retries:
        reasons.append("Retry number exceeds the capability retry budget.")
    if (
        action.action_type == CapabilityActionType.CREATE_PULL_REQUEST
        and lease.consumed_pull_requests >= lease.scope.max_pull_requests
    ):
        reasons.append("Pull-request capability budget is exhausted.")

    if reasons:
        return _decision(
            action,
            PolicyDecisionType.BLOCK,
            RiskLevel.CRITICAL,
            reasons,
            ["Revise the approval scope and issue a new capability lease."],
        )

    if lease.mode == CapabilityLeaseMode.STRICT:
        return _decision(
            action,
            PolicyDecisionType.REQUIRE_APPROVAL,
            RiskLevel.HIGH,
            ["Strict mode requires a human decision for every meaningful action."],
            ["Approve this exact action without broadening the lease."],
        )

    if lease.mode == CapabilityLeaseMode.SUPERVISED and action.action_type in HIGH_RISK_ACTIONS:
        return _decision(
            action,
            PolicyDecisionType.REQUIRE_APPROVAL,
            RiskLevel.CRITICAL,
            ["Supervised mode escalates high-risk actions to a human reviewer."],
            ["Require an owner or maintainer decision before execution."],
        )

    risk = RiskLevel.HIGH if action.action_type in HIGH_RISK_ACTIONS else RiskLevel.LOW
    return _decision(
        action,
        PolicyDecisionType.ALLOW,
        risk,
        ["Action is inside the signed, active and budgeted capability lease."],
        ["Record the executed payload and consumption against the lease."],
    )
