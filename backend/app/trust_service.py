from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .models import ApprovalStatus, AuthUser, RiskLevel, now_utc
from .store import store
from .trust_crypto import issue_bearer_token, new_nonce, sha256_hex, sign_payload
from .trust_flight_recorder import append_trust_event
from .trust_models import (
    ActionConsumption,
    AgentGatewayResult,
    AgentReliabilityScorecard,
    CapabilityLease,
    CapabilityLeaseCreate,
    CapabilityLeasePublic,
    CapabilityLeaseStatus,
    CredentialGrant,
    CredentialGrantCreate,
    CredentialGrantResponse,
    CredentialGrantType,
    ExceptionQueueItem,
    PolicyDecisionType,
    ProposedAction,
    ReviewerAttestation,
    ReviewerAttestationCreate,
    ReviewerAttestationDecision,
    TrustEventType,
)
from .trust_policy import evaluate_proposed_action, lease_receipt_payload


class TrustControlConflict(RuntimeError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _lease_public(lease: CapabilityLease) -> CapabilityLeasePublic:
    return CapabilityLeasePublic.model_validate(lease.model_dump())


def _linked_task_for_approval(approval_request_id: str):
    return next(
        (
            task
            for task in store.agent_tasks.values()
            if task.approval_request_id == approval_request_id
        ),
        None,
    )


def _approval_reviewer_ids(approval_request_id: str, payload_hash: str) -> set[str]:
    approval = store.approval_requests[approval_request_id]
    reviewer_ids: set[str] = set()
    if approval.approved_by_user_id:
        reviewer_ids.add(approval.approved_by_user_id)
    for attestation in store.reviewer_attestations.values():
        if (
            attestation.approval_request_id == approval_request_id
            and attestation.approval_payload_hash == payload_hash
            and attestation.decision == ReviewerAttestationDecision.APPROVE
        ):
            reviewer_ids.add(attestation.reviewer_user_id)
    return reviewer_ids


def _has_denial(approval_request_id: str, payload_hash: str) -> bool:
    return any(
        attestation.approval_request_id == approval_request_id
        and attestation.approval_payload_hash == payload_hash
        and attestation.decision == ReviewerAttestationDecision.DENY
        for attestation in store.reviewer_attestations.values()
    )


def record_reviewer_attestation(
    payload: ReviewerAttestationCreate,
    *,
    user: AuthUser,
) -> ReviewerAttestation:
    approval = store.approval_requests.get(payload.approval_request_id)
    if approval is None:
        raise ValueError("Approval request not found.")
    if not approval.approved_payload_hash:
        raise TrustControlConflict("Approval payload must be sealed before attestation.")
    if approval.created_by_user_id == user.id:
        raise TrustControlConflict("The approval creator cannot attest their own request.")

    existing = next(
        (
            item
            for item in store.reviewer_attestations.values()
            if item.approval_request_id == approval.id
            and item.reviewer_user_id == user.id
            and item.approval_payload_hash == approval.approved_payload_hash
        ),
        None,
    )
    material = {
        "approval_request_id": approval.id,
        "approval_payload_hash": approval.approved_payload_hash,
        "reviewer_user_id": user.id,
        "reviewer_role": user.role,
        "decision": payload.decision,
        "reason": payload.reason,
    }
    if existing:
        if existing.decision != payload.decision or existing.reason != payload.reason:
            raise TrustControlConflict("Reviewer attestation is immutable once recorded.")
        return existing

    attestation = ReviewerAttestation(
        approval_request_id=approval.id,
        approval_payload_hash=approval.approved_payload_hash,
        reviewer_user_id=user.id,
        reviewer_role=user.role,
        decision=payload.decision,
        reason=payload.reason,
        signature=sign_payload(material),
    )
    store.reviewer_attestations[attestation.id] = attestation
    append_trust_event(
        TrustEventType.ATTESTATION_RECORDED,
        entity_type="approval_request",
        entity_id=approval.id,
        actor=user.email,
        payload={
            "attestation_id": attestation.id,
            "decision": attestation.decision,
            "reviewer_role": attestation.reviewer_role,
            "approval_payload_hash": approval.approved_payload_hash,
        },
        persist=False,
    )
    store.persist()
    return attestation


def _validate_scope_against_approval(payload: CapabilityLeaseCreate) -> None:
    approval = store.approval_requests[payload.approval_request_id]
    task = _linked_task_for_approval(approval.id)
    if task is None:
        raise TrustControlConflict("Capability leases require a linked AgentTask.")
    if payload.scope.repository != task.repository:
        raise TrustControlConflict("Lease repository must match the approved AgentTask repository.")
    if payload.scope.branch != approval.target_branch or payload.scope.branch != task.branch_preference:
        raise TrustControlConflict("Lease branch must match the approved target branch.")

    approved_paths = {item.path.strip().replace("\\", "/") for item in approval.files}
    requested_paths = {
        item.strip().replace("\\", "/").rstrip("/")
        for item in payload.scope.allowed_path_prefixes
    }
    outside_paths = sorted(path for path in requested_paths if path not in approved_paths)
    if outside_paths:
        raise TrustControlConflict(
            "Lease path scope exceeds approved files: " + ", ".join(outside_paths)
        )

    approved_commands = {item.strip() for item in approval.test_plan if item.strip()}
    outside_commands = sorted(
        command
        for command in payload.scope.allowed_commands
        if command.strip() not in approved_commands
    )
    if outside_commands:
        raise TrustControlConflict(
            "Lease command scope exceeds the approved test plan: "
            + ", ".join(outside_commands)
        )
    if payload.scope.allow_auto_merge:
        raise TrustControlConflict("Auto-merge cannot be granted by a capability lease.")


def issue_capability_lease(
    payload: CapabilityLeaseCreate,
    *,
    user: AuthUser,
) -> CapabilityLease:
    approval = store.approval_requests.get(payload.approval_request_id)
    if approval is None:
        raise ValueError("Approval request not found.")
    if approval.status != ApprovalStatus.APPROVED:
        raise TrustControlConflict("Capability leases require an APPROVED request.")
    if not approval.approved_payload_hash:
        raise TrustControlConflict("Approved payload hash is required before lease issuance.")
    if payload.agent_id and payload.agent_id not in store.external_agents:
        raise ValueError("External agent not found.")
    if _has_denial(approval.id, approval.approved_payload_hash):
        raise TrustControlConflict("A reviewer denied this exact approval payload.")

    _validate_scope_against_approval(payload)
    reviewers = _approval_reviewer_ids(approval.id, approval.approved_payload_hash)
    if payload.prevent_self_approval and approval.created_by_user_id in reviewers:
        raise TrustControlConflict("Self-approved work cannot receive a capability lease.")
    if len(reviewers) < payload.required_reviewer_count:
        raise TrustControlConflict(
            f"Lease requires {payload.required_reviewer_count} distinct reviewers; "
            f"only {len(reviewers)} are recorded."
        )

    active = next(
        (
            lease
            for lease in store.capability_leases.values()
            if lease.approval_request_id == approval.id
            and lease.agent_id == payload.agent_id
            and lease.status == CapabilityLeaseStatus.ACTIVE
            and lease.expires_at > _utcnow()
        ),
        None,
    )
    if active:
        requested_fingerprint = sha256_hex(payload.model_dump(mode="json"))
        existing_fingerprint = active.scope.metadata.get("request_fingerprint")
        if existing_fingerprint == requested_fingerprint:
            return active
        raise TrustControlConflict("An active lease already exists for this approval and agent.")

    issued_at = _utcnow()
    scope = payload.scope.model_copy(deep=True)
    scope.metadata = {
        **scope.metadata,
        "request_fingerprint": sha256_hex(payload.model_dump(mode="json")),
    }
    lease = CapabilityLease(
        approval_request_id=approval.id,
        approved_payload_hash=approval.approved_payload_hash,
        agent_id=payload.agent_id,
        provider=payload.provider,
        mode=payload.mode,
        scope=scope,
        issued_by_user_id=user.id,
        required_reviewer_count=payload.required_reviewer_count,
        prevent_self_approval=payload.prevent_self_approval,
        issued_at=issued_at,
        expires_at=issued_at + timedelta(seconds=payload.expires_in_seconds),
        receipt_nonce=new_nonce(),
        receipt_signature="pending",
    )
    lease.receipt_signature = sign_payload(lease_receipt_payload(lease))
    store.capability_leases[lease.id] = lease
    append_trust_event(
        TrustEventType.LEASE_ISSUED,
        entity_type="capability_lease",
        entity_id=lease.id,
        actor=user.email,
        correlation_id=scope.metadata.get("correlation_id"),
        payload={
            "approval_request_id": approval.id,
            "provider": lease.provider,
            "agent_id": lease.agent_id,
            "mode": lease.mode,
            "repository": scope.repository,
            "branch": scope.branch,
            "expires_at": lease.expires_at,
            "required_reviewer_count": lease.required_reviewer_count,
        },
        persist=False,
    )
    store.persist()
    return lease


def refresh_lease_status(lease: CapabilityLease) -> CapabilityLease:
    if lease.status == CapabilityLeaseStatus.ACTIVE and lease.expires_at <= _utcnow():
        lease.status = CapabilityLeaseStatus.EXPIRED
        lease.updated_at = now_utc()
        append_trust_event(
            TrustEventType.LEASE_EXPIRED,
            entity_type="capability_lease",
            entity_id=lease.id,
            actor="system",
            payload={"expires_at": lease.expires_at},
            persist=False,
        )
        store.persist()
    return lease


def revoke_capability_lease(
    lease: CapabilityLease,
    *,
    user: AuthUser,
    reason: str,
) -> CapabilityLease:
    refresh_lease_status(lease)
    if lease.status in {CapabilityLeaseStatus.REVOKED, CapabilityLeaseStatus.EXPIRED}:
        return lease
    if lease.status != CapabilityLeaseStatus.ACTIVE:
        raise TrustControlConflict(f"Lease cannot be revoked from {lease.status}.")
    lease.status = CapabilityLeaseStatus.REVOKED
    lease.revoked_at = now_utc()
    lease.revoked_by_user_id = user.id
    lease.revocation_reason = reason
    lease.updated_at = now_utc()
    for grant in store.credential_grants.values():
        if grant.lease_id == lease.id and not grant.revoked:
            grant.revoked = True
            grant.revoked_at = now_utc()
    append_trust_event(
        TrustEventType.LEASE_REVOKED,
        entity_type="capability_lease",
        entity_id=lease.id,
        actor=user.email,
        payload={"reason": reason},
        persist=False,
    )
    store.persist()
    return lease


def evaluate_gateway_action(action: ProposedAction, *, actor: str) -> AgentGatewayResult:
    lease = store.capability_leases.get(action.lease_id or "")
    if lease:
        refresh_lease_status(lease)
    store.proposed_actions[action.id] = action
    append_trust_event(
        TrustEventType.ACTION_RECEIVED,
        entity_type="proposed_action",
        entity_id=action.id,
        actor=actor,
        correlation_id=action.run_id or action.task_id,
        payload={
            "provider": action.provider,
            "agent_id": action.agent_id,
            "action_type": action.action_type,
            "lease_id": action.lease_id,
        },
        persist=False,
    )
    decision = evaluate_proposed_action(action, lease)
    store.policy_decisions[decision.id] = decision
    event_type = {
        PolicyDecisionType.ALLOW: TrustEventType.ACTION_ALLOWED,
        PolicyDecisionType.REQUIRE_APPROVAL: TrustEventType.ACTION_REQUIRES_APPROVAL,
        PolicyDecisionType.BLOCK: TrustEventType.ACTION_BLOCKED,
    }[decision.decision]
    append_trust_event(
        event_type,
        entity_type="proposed_action",
        entity_id=action.id,
        actor=actor,
        correlation_id=action.run_id or action.task_id,
        payload={
            "decision_id": decision.id,
            "decision": decision.decision,
            "risk_level": decision.risk_level,
            "reasons": decision.reasons,
            "obligations": decision.obligations,
        },
        persist=False,
    )
    store.persist()
    return AgentGatewayResult(
        action=action,
        decision=decision,
        lease=_lease_public(lease) if lease else None,
    )


def consume_gateway_action(
    action_id: str,
    consumption: ActionConsumption,
    *,
    actor: str,
) -> ActionConsumption:
    action = store.proposed_actions.get(action_id)
    if action is None:
        raise ValueError("Proposed action not found.")
    decision = next(
        (
            item
            for item in store.policy_decisions.values()
            if item.proposed_action_id == action.id
        ),
        None,
    )
    if decision is None or decision.decision != PolicyDecisionType.ALLOW:
        raise TrustControlConflict("Only an ALLOW decision can be consumed.")
    if action.id in store.action_consumptions:
        existing = store.action_consumptions[action.id]
        if existing != consumption:
            raise TrustControlConflict("Action consumption is immutable once recorded.")
        return existing
    lease = store.capability_leases.get(action.lease_id or "")
    if lease is None:
        raise TrustControlConflict("Allowed action is missing its capability lease.")
    refresh_lease_status(lease)
    if lease.status != CapabilityLeaseStatus.ACTIVE:
        raise TrustControlConflict("Capability lease is no longer active.")

    lease.consumed_runtime_seconds += consumption.actual_runtime_seconds
    lease.consumed_cost_usd += consumption.actual_cost_usd
    lease.consumed_pull_requests += consumption.pull_requests_created
    if consumption.retry_consumed:
        lease.consumed_retries += 1
    lease.updated_at = now_utc()
    store.action_consumptions[action.id] = consumption
    append_trust_event(
        TrustEventType.ACTION_CONSUMED,
        entity_type="proposed_action",
        entity_id=action.id,
        actor=actor,
        correlation_id=action.run_id or action.task_id,
        payload={
            "lease_id": lease.id,
            "actual_runtime_seconds": consumption.actual_runtime_seconds,
            "actual_cost_usd": consumption.actual_cost_usd,
            "pull_requests_created": consumption.pull_requests_created,
            "retry_consumed": consumption.retry_consumed,
            "output_reference": consumption.output_reference,
            "evidence_hash": sha256_hex(consumption.evidence),
        },
        persist=False,
    )
    store.persist()
    return consumption


def issue_credential_grant(
    payload: CredentialGrantCreate,
    *,
    user: AuthUser,
) -> CredentialGrantResponse:
    lease = store.capability_leases.get(payload.lease_id)
    if lease is None:
        raise ValueError("Capability lease not found.")
    refresh_lease_status(lease)
    if lease.status != CapabilityLeaseStatus.ACTIVE:
        raise TrustControlConflict("Credentials require an active capability lease.")
    if payload.grant_type != CredentialGrantType.INTERNAL_CAPABILITY_TOKEN:
        raise NotImplementedError(
            "External GitHub App and OIDC brokers require deployment-specific issuer integration."
        )
    remaining_seconds = int((lease.expires_at - _utcnow()).total_seconds())
    if payload.expires_in_seconds > remaining_seconds:
        raise TrustControlConflict("Credential grant cannot outlive its capability lease.")

    raw_token, token_hash = issue_bearer_token()
    grant = CredentialGrant(
        lease_id=lease.id,
        grant_type=payload.grant_type,
        audience=payload.audience,
        subject=payload.subject,
        scope=payload.scope,
        expires_at=_utcnow() + timedelta(seconds=payload.expires_in_seconds),
        token_hash=token_hash,
        metadata={"issued_by_user_id": user.id},
    )
    store.credential_grants[grant.id] = grant
    append_trust_event(
        TrustEventType.CREDENTIAL_GRANT_ISSUED,
        entity_type="credential_grant",
        entity_id=grant.id,
        actor=user.email,
        payload={
            "lease_id": lease.id,
            "grant_type": grant.grant_type,
            "audience": grant.audience,
            "subject": grant.subject,
            "scope": grant.scope,
            "expires_at": grant.expires_at,
        },
        persist=False,
    )
    store.persist()
    return CredentialGrantResponse(grant=grant, bearer_token=raw_token)


def build_reliability_scorecards() -> list[AgentReliabilityScorecard]:
    keys = {
        (action.provider, action.agent_id)
        for action in store.proposed_actions.values()
    }
    for task in store.agent_tasks.values():
        keys.add((task.provider, task.external_agent_id))

    results: list[AgentReliabilityScorecard] = []
    for provider, agent_id in sorted(keys, key=lambda item: (item[0].value, item[1] or "")):
        actions = [
            action
            for action in store.proposed_actions.values()
            if action.provider == provider and action.agent_id == agent_id
        ]
        action_ids = {action.id for action in actions}
        decisions = [
            decision
            for decision in store.policy_decisions.values()
            if decision.proposed_action_id in action_ids
        ]
        allowed = sum(item.decision == PolicyDecisionType.ALLOW for item in decisions)
        blocked = sum(item.decision == PolicyDecisionType.BLOCK for item in decisions)
        escalated = sum(
            item.decision == PolicyDecisionType.REQUIRE_APPROVAL for item in decisions
        )
        tasks = [
            task
            for task in store.agent_tasks.values()
            if task.provider == provider and task.external_agent_id == agent_id
        ]
        task_ids = {task.id for task in tasks}
        runs = [run for run in store.agent_runs.values() if run.task_id in task_ids]
        run_ids = {run.id for run in runs}
        first_attempt = 0
        for run in runs:
            steps = [step for step in store.agent_run_steps.values() if step.run_id == run.id]
            if steps and all(step.attempt_count <= 1 for step in steps):
                first_attempt += 1
        recovered_runs = {
            event.run_id
            for event in store.agent_run_events.values()
            if event.event_type.value == "STALE_LEASE_RECOVERED"
            and event.run_id in run_ids
        }
        recovered_successes = sum(
            run.id in recovered_runs and run.status.value == "SUCCEEDED" for run in runs
        )
        interventions = sum(run.status.value == "NEEDS_HUMAN" for run in runs)
        completed_with_evidence = sum(bool(run.final_evidence_hash) for run in runs)
        durations = [
            (run.completed_at - run.started_at).total_seconds()
            for run in runs
            if run.started_at and run.completed_at
        ]
        total_cost = sum(
            consumption.actual_cost_usd
            for action_id, consumption in store.action_consumptions.items()
            if action_id in action_ids
        )
        evaluated = len(decisions)
        scope_rate = allowed / evaluated if evaluated else 0.0
        results.append(
            AgentReliabilityScorecard(
                provider=provider,
                agent_id=agent_id,
                evaluated_actions=evaluated,
                allowed_actions=allowed,
                blocked_actions=blocked,
                approval_escalations=escalated,
                scope_adherence_rate=scope_rate,
                first_attempt_success_rate=(first_attempt / len(runs) if runs else 0.0),
                recovery_success_rate=(
                    recovered_successes / len(recovered_runs) if recovered_runs else 0.0
                ),
                human_intervention_rate=(interventions / len(runs) if runs else 0.0),
                policy_block_rate=(blocked / evaluated if evaluated else 0.0),
                evidence_completion_rate=(
                    completed_with_evidence / len(runs) if runs else 0.0
                ),
                average_runtime_seconds=(
                    sum(durations) / len(durations) if durations else 0.0
                ),
                total_cost_usd=total_cost,
                confidence=min(1.0, (evaluated + len(runs)) / 30.0),
            )
        )
    return results


def build_exception_queue() -> list[ExceptionQueueItem]:
    items: list[ExceptionQueueItem] = []
    for run in store.agent_runs.values():
        if run.status.value not in {"NEEDS_HUMAN", "BLOCKED", "FAILED"}:
            continue
        severity = RiskLevel.CRITICAL if run.status.value == "BLOCKED" else RiskLevel.HIGH
        items.append(
            ExceptionQueueItem(
                id=f"run:{run.id}",
                category=run.status.value,
                severity=severity,
                title=f"Agent run {run.status.value.replace('_', ' ').lower()}",
                summary=run.last_error or run.objective or "Operator review is required.",
                run_id=run.id,
                task_id=run.task_id,
                created_at=run.updated_at,
            )
        )
    action_by_id = store.proposed_actions
    for decision in store.policy_decisions.values():
        if decision.decision == PolicyDecisionType.ALLOW:
            continue
        action = action_by_id.get(decision.proposed_action_id)
        if action is None:
            continue
        items.append(
            ExceptionQueueItem(
                id=f"action:{action.id}",
                category=decision.decision.value,
                severity=decision.risk_level,
                title=(
                    "Agent action blocked"
                    if decision.decision == PolicyDecisionType.BLOCK
                    else "Agent action needs approval"
                ),
                summary=" ".join(decision.reasons),
                run_id=action.run_id,
                task_id=action.task_id,
                action_id=action.id,
                lease_id=action.lease_id,
                created_at=decision.evaluated_at,
            )
        )
    return sorted(items, key=lambda item: item.created_at, reverse=True)
