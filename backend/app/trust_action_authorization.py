from __future__ import annotations

from .models import AuthUser
from .store import store
from .trust_action_authorization_models import (
    ActionAuthorization,
    ActionAuthorizationCreate,
    ActionAuthorizationDecision,
)
from .trust_crypto import sign_payload
from .trust_decisions import latest_policy_decision
from .trust_flight_recorder import append_trust_event
from .trust_models import PolicyDecision, PolicyDecisionType, TrustEventType


class ActionAuthorizationConflict(RuntimeError):
    pass


def record_action_authorization(
    action_id: str,
    payload: ActionAuthorizationCreate,
    *,
    user: AuthUser,
) -> ActionAuthorization:
    action = store.proposed_actions.get(action_id)
    if action is None:
        raise ValueError("Proposed action not found.")
    current = latest_policy_decision(action_id)
    if current is None:
        raise ActionAuthorizationConflict("Action has no policy decision.")
    if current.decision == PolicyDecisionType.BLOCK:
        raise ActionAuthorizationConflict(
            "A policy-blocked action cannot be overridden; revise scope and create a new action."
        )
    if current.decision == PolicyDecisionType.ALLOW:
        existing = next(
            (
                item
                for item in store.action_authorizations.values()
                if item.action_id == action_id
            ),
            None,
        )
        if existing:
            return existing
        raise ActionAuthorizationConflict("Action is already allowed by policy.")

    existing = next(
        (
            item
            for item in store.action_authorizations.values()
            if item.action_id == action_id
        ),
        None,
    )
    if existing:
        if existing.decision != payload.decision or existing.reason != payload.reason:
            raise ActionAuthorizationConflict(
                "Action authorization is immutable once recorded."
            )
        return existing

    material = {
        "action_id": action.id,
        "policy_decision_id": current.id,
        "reviewer_user_id": user.id,
        "reviewer_role": user.role,
        "decision": payload.decision,
        "reason": payload.reason,
    }
    authorization = ActionAuthorization(
        action_id=action.id,
        policy_decision_id=current.id,
        reviewer_user_id=user.id,
        reviewer_role=user.role,
        decision=payload.decision,
        reason=payload.reason,
        signature=sign_payload(material),
    )
    store.action_authorizations[authorization.id] = authorization

    effective_decision = (
        PolicyDecisionType.ALLOW
        if payload.decision == ActionAuthorizationDecision.ALLOW
        else PolicyDecisionType.BLOCK
    )
    effective = PolicyDecision(
        proposed_action_id=action.id,
        lease_id=action.lease_id,
        decision=effective_decision,
        risk_level=current.risk_level,
        reasons=[
            "A human reviewer allowed this exact proposed action."
            if effective_decision == PolicyDecisionType.ALLOW
            else "A human reviewer denied this exact proposed action."
        ],
        obligations=(
            ["Execute only the immutable action payload and record consumption evidence."]
            if effective_decision == PolicyDecisionType.ALLOW
            else ["Do not execute; revise the action or capability lease."]
        ),
        evaluated_policy_version=current.evaluated_policy_version + "+human-decision",
    )
    store.policy_decisions[effective.id] = effective
    append_trust_event(
        (
            TrustEventType.ACTION_ALLOWED
            if effective_decision == PolicyDecisionType.ALLOW
            else TrustEventType.ACTION_BLOCKED
        ),
        entity_type="proposed_action",
        entity_id=action.id,
        actor=user.email,
        correlation_id=action.run_id or action.task_id,
        payload={
            "authorization_id": authorization.id,
            "prior_policy_decision_id": current.id,
            "effective_policy_decision_id": effective.id,
            "decision": effective_decision,
            "reason": payload.reason,
        },
        persist=False,
    )
    store.persist()
    return authorization
