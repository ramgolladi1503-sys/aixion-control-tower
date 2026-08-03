from __future__ import annotations

from .agent_run_models import (
    AgentRunEvent,
    AgentRunEventType,
    AgentRunStatus,
    AgentRunStepStatus,
)
from .agent_run_state_machine import transition_run, transition_step
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


def _apply_durable_run_decision(
    action_id: str,
    decision: ActionAuthorizationDecision,
    *,
    actor: str,
    reason: str,
) -> None:
    action = store.proposed_actions.get(action_id)
    if action is None or not action.run_id:
        return
    run = store.agent_runs.get(action.run_id)
    if run is None:
        return
    step_id = str(action.metadata.get("step_id") or "")
    step = store.agent_run_steps.get(step_id)
    if step is None or step.run_id != run.id:
        return

    if decision == ActionAuthorizationDecision.ALLOW:
        if step.status == AgentRunStepStatus.NEEDS_HUMAN:
            transition_step(
                step,
                AgentRunStepStatus.READY,
                reason="Exact trust action approval recorded.",
            )
        if run.status == AgentRunStatus.NEEDS_HUMAN:
            transition_run(
                run,
                AgentRunStatus.SCHEDULED,
                reason="Exact trust action approval recorded.",
            )
        message = "Exact human action approval returned the run to the scheduler."
    else:
        if step.status == AgentRunStepStatus.NEEDS_HUMAN:
            transition_step(step, AgentRunStepStatus.BLOCKED, reason=reason)
        if run.status == AgentRunStatus.NEEDS_HUMAN:
            transition_run(run, AgentRunStatus.BLOCKED, reason=reason)
        message = "Exact human action denial permanently blocked the run."

    event = AgentRunEvent(
        run_id=run.id,
        task_id=run.task_id,
        step_id=step.id,
        correlation_id=run.correlation_id,
        event_type=AgentRunEventType.OPERATOR_ACTION,
        message=message,
        reason=reason,
        actor=actor,
        attempt_number=step.attempt_count,
        metadata={
            "action_id": action.id,
            "authorization_decision": decision,
        },
    )
    store.agent_run_events[event.id] = event


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
            _apply_durable_run_decision(
                action_id,
                existing.decision,
                actor=user.email,
                reason=existing.reason,
            )
            store.persist()
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
        _apply_durable_run_decision(
            action_id,
            existing.decision,
            actor=user.email,
            reason=existing.reason,
        )
        store.persist()
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
    _apply_durable_run_decision(
        action_id,
        payload.decision,
        actor=user.email,
        reason=payload.reason,
    )
    store.persist()
    return authorization
