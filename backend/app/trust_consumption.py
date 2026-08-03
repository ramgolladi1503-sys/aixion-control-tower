from __future__ import annotations

from .models import now_utc
from .store import store
from .trust_crypto import sha256_hex
from .trust_decisions import latest_policy_decision
from .trust_flight_recorder import append_trust_event
from .trust_models import (
    ActionConsumption,
    CapabilityLeaseStatus,
    PolicyDecisionType,
    TrustEventType,
)
from .trust_service import TrustControlConflict, refresh_lease_status


def consume_effective_action(
    action_id: str,
    consumption: ActionConsumption,
    *,
    actor: str,
) -> ActionConsumption:
    action = store.proposed_actions.get(action_id)
    if action is None:
        raise ValueError("Proposed action not found.")
    decision = latest_policy_decision(action.id)
    if decision is None or decision.decision != PolicyDecisionType.ALLOW:
        raise TrustControlConflict("Only an effective ALLOW decision can be consumed.")
    existing = store.action_consumptions.get(action.id)
    if existing:
        if existing != consumption:
            raise TrustControlConflict("Action consumption is immutable once recorded.")
        return existing

    lease = store.capability_leases.get(action.lease_id or "")
    if lease is None:
        raise TrustControlConflict("Allowed action is missing its capability lease.")
    refresh_lease_status(lease)
    if lease.status != CapabilityLeaseStatus.ACTIVE:
        raise TrustControlConflict("Capability lease is no longer active.")

    projected_runtime = lease.consumed_runtime_seconds + consumption.actual_runtime_seconds
    projected_cost = lease.consumed_cost_usd + consumption.actual_cost_usd
    projected_prs = lease.consumed_pull_requests + consumption.pull_requests_created
    projected_retries = lease.consumed_retries + int(consumption.retry_consumed)
    if projected_runtime > lease.scope.max_runtime_seconds:
        raise TrustControlConflict("Actual runtime would exceed the capability budget.")
    if projected_cost > lease.scope.max_cost_usd:
        raise TrustControlConflict("Actual cost would exceed the capability budget.")
    if projected_prs > lease.scope.max_pull_requests:
        raise TrustControlConflict("Actual pull-request count exceeds the capability budget.")
    if projected_retries > lease.scope.max_retries:
        raise TrustControlConflict("Actual retry count exceeds the capability budget.")

    lease.consumed_runtime_seconds = projected_runtime
    lease.consumed_cost_usd = projected_cost
    lease.consumed_pull_requests = projected_prs
    lease.consumed_retries = projected_retries
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
            "effective_policy_decision_id": decision.id,
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
