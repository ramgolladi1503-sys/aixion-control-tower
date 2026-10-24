from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from psycopg import Connection

from .agent_run_models import (
    AgentRunEvent,
    AgentRunEventType,
    AgentRunStatus,
    AgentRunStepStatus,
)
from .agent_run_state_machine import transition_run, transition_step
from .models import AuthUser, UserRole
from .store import store
from .trust_action_authorization_models import (
    ActionAuthorization,
    ActionAuthorizationCreate,
    ActionAuthorizationDecision,
)
from .trust_crypto import sha256_hex, sign_payload, verify_payload_signature
from .postgres_trust_event_repository import PostgresTrustEventRepository
from .postgres_trust_repository import PostgresTrustRepository
from .trust_decisions import latest_policy_decision
from .trust_flight_recorder import append_trust_event
from .trust_models import PolicyDecision, PolicyDecisionType, ProposedAction, TrustEventType
from .workspace_access import (
    action_authorization_scope_is_consistent,
    agent_run_scope_is_consistent,
    proposed_action_scope_is_consistent,
    proposed_action_workspace_id,
)


class ActionAuthorizationConflict(RuntimeError):
    pass


REVIEWER_ROLES = {UserRole.REVIEWER, UserRole.MAINTAINER, UserRole.OWNER}


def action_payload_hash(action: ProposedAction) -> str:
    return sha256_hex(action.model_dump(mode="json"))


def authorization_signature_payload(
    authorization: ActionAuthorization,
) -> dict:
    return {
        "action_id": authorization.action_id,
        "action_payload_hash": authorization.action_payload_hash,
        "policy_decision_id": authorization.policy_decision_id,
        "reviewer_user_id": authorization.reviewer_user_id,
        "reviewer_role": authorization.reviewer_role,
        "decision": authorization.decision,
        "reason": authorization.reason,
    }


def verify_action_authorization(
    authorization: ActionAuthorization,
    action: ProposedAction,
) -> bool:
    return (
        action_authorization_scope_is_consistent(authorization)
        and verify_action_authorization_payload(authorization, action)
    )


def verify_action_authorization_payload(
    authorization: ActionAuthorization,
    action: ProposedAction,
) -> bool:
    """Verify the signed action binding without consulting process-local scope."""
    return (
        authorization.action_id == action.id
        and authorization.action_payload_hash == action_payload_hash(action)
        and verify_payload_signature(
            authorization_signature_payload(authorization), authorization.signature
        )
    )


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
    if run is None or not agent_run_scope_is_consistent(run):
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
    if user.role not in REVIEWER_ROLES:
        raise ActionAuthorizationConflict("A reviewer role is required for action decisions.")
    action = store.proposed_actions.get(action_id)
    if action is None:
        raise ValueError("Proposed action not found.")
    if not proposed_action_scope_is_consistent(action):
        raise ValueError("Proposed action not found.")
    stored_workspace_id = store.users.get(user.id).workspace_id if user.id in store.users else user.workspace_id
    if stored_workspace_id is not None and proposed_action_workspace_id(action) != stored_workspace_id:
        raise ValueError("Proposed action not found.")
    current = latest_policy_decision(action_id)
    if current is None:
        raise ActionAuthorizationConflict("Action has no policy decision.")
    if current.decision == PolicyDecisionType.BLOCK:
        raise ActionAuthorizationConflict(
            "A policy-blocked action cannot be overridden; revise scope and create a new action."
        )
    existing = next(
        (
            item
            for item in store.action_authorizations.values()
            if item.action_id == action_id
        ),
        None,
    )
    if current.decision == PolicyDecisionType.ALLOW:
        if existing:
            if not verify_action_authorization(existing, action):
                raise ActionAuthorizationConflict(
                    "Stored action authorization no longer matches the immutable action payload."
                )
            _apply_durable_run_decision(
                action_id,
                existing.decision,
                actor=user.email,
                reason=existing.reason,
            )
            store.persist()
            return existing
        raise ActionAuthorizationConflict("Action is already allowed by policy.")

    if existing:
        if existing.decision != payload.decision or existing.reason != payload.reason:
            raise ActionAuthorizationConflict(
                "Action authorization is immutable once recorded."
            )
        if not verify_action_authorization(existing, action):
            raise ActionAuthorizationConflict(
                "Stored action authorization no longer matches the immutable action payload."
            )
        _apply_durable_run_decision(
            action_id,
            existing.decision,
            actor=user.email,
            reason=existing.reason,
        )
        store.persist()
        return existing

    payload_hash = action_payload_hash(action)
    authorization = ActionAuthorization(
        action_id=action.id,
        workspace_id=action.workspace_id,
        action_payload_hash=payload_hash,
        policy_decision_id=current.id,
        reviewer_user_id=user.id,
        reviewer_role=user.role,
        decision=payload.decision,
        reason=payload.reason,
        signature="pending",
    )
    authorization.signature = sign_payload(
        authorization_signature_payload(authorization)
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
        workspace_id=action.workspace_id,
        correlation_id=action.run_id or action.task_id,
        payload={
            "authorization_id": authorization.id,
            "action_payload_hash": payload_hash,
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


def record_action_authorization_postgres(
    action_id: str,
    payload: ActionAuthorizationCreate,
    *,
    user: AuthUser,
    workspace_id: str,
    connect: Callable[[], Connection],
    trust_repository: PostgresTrustRepository,
    trust_event_repository: PostgresTrustEventRepository,
    now: datetime,
) -> ActionAuthorization:
    """Persist one exact-action reviewer decision and policy event atomically."""
    if not workspace_id:
        raise ValueError("Action authorization workspace is required.")
    if user.role not in REVIEWER_ROLES:
        raise ActionAuthorizationConflict("A reviewer role is required for action decisions.")

    with connect() as conn:
        with conn.transaction():
            action = trust_repository.get_proposed_action(conn, action_id, workspace_id)
            if action is None:
                raise ValueError("Proposed action not found.")
            if action.run_id:
                raise ActionAuthorizationConflict(
                    "Run-linked action decisions require the durable AgentRun execution transaction."
                )
            if action.task_id:
                action, _task, _approval = trust_repository.lock_task_action_binding(
                    conn,
                    action_id=action_id,
                    workspace_id=workspace_id,
                    task_id=action.task_id,
                )
            else:
                action = trust_repository.get_proposed_action(
                    conn, action_id, workspace_id, for_update=True
                )
                if action is None:
                    raise ValueError("Proposed action not found.")

            current = trust_repository.get_latest_policy_decision_for_action(
                conn, action_id, workspace_id, for_update=True
            )
            if current is None:
                raise ActionAuthorizationConflict("Action has no policy decision.")
            existing = trust_repository.get_action_authorization_for_action(
                conn, action_id, workspace_id, for_update=True
            )
            if action.task_id:
                _task_action, task, approval = trust_repository.lock_task_action_binding(
                    conn,
                    action_id=action_id,
                    workspace_id=workspace_id,
                    task_id=action.task_id,
                )
                trust_repository.lock_task_action_lease(
                    conn, action=action, task=task, approval=approval
                )

            def verify_existing() -> ActionAuthorization:
                if (
                    existing is None
                    or existing.workspace_id != workspace_id
                    or not verify_action_authorization_payload(existing, action)
                ):
                    raise ActionAuthorizationConflict(
                        "Stored action authorization no longer matches the immutable action payload."
                    )
                prior = trust_repository.get_policy_decision(
                    conn, existing.policy_decision_id, workspace_id
                )
                expected_effective = (
                    PolicyDecisionType.ALLOW
                    if existing.decision == ActionAuthorizationDecision.ALLOW
                    else PolicyDecisionType.BLOCK
                )
                if (
                    prior is None
                    or prior.proposed_action_id != action.id
                    or prior.decision != PolicyDecisionType.REQUIRE_APPROVAL
                    or not current.evaluated_policy_version.endswith("+human-decision")
                    or current.decision != expected_effective
                ):
                    raise ActionAuthorizationConflict(
                        "Stored action authorization is inconsistent with the effective policy decision."
                    )
                if (
                    existing.decision != payload.decision
                    or existing.reason != payload.reason
                ):
                    raise ActionAuthorizationConflict(
                        "Action authorization is immutable once recorded."
                    )
                return existing

            if existing is not None:
                return verify_existing()
            if current.decision == PolicyDecisionType.BLOCK:
                raise ActionAuthorizationConflict(
                    "A policy-blocked action cannot be overridden; revise scope and create a new action."
                )
            if current.decision == PolicyDecisionType.ALLOW:
                raise ActionAuthorizationConflict("Action is already allowed by policy.")
            if current.decision != PolicyDecisionType.REQUIRE_APPROVAL:
                raise ActionAuthorizationConflict("Action policy decision is not reviewable.")

            action_hash = action_payload_hash(action)
            authorization = ActionAuthorization(
                action_id=action.id,
                workspace_id=workspace_id,
                action_payload_hash=action_hash,
                policy_decision_id=current.id,
                reviewer_user_id=user.id,
                reviewer_role=user.role,
                decision=payload.decision,
                reason=payload.reason,
                signature="pending",
            )
            authorization.signature = sign_payload(
                authorization_signature_payload(authorization)
            )
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
                evaluated_policy_version=current.evaluated_policy_version
                + "+human-decision",
                evaluated_at=now,
            )
            trust_repository.insert_action_authorization(conn, authorization)
            trust_repository.insert_policy_decision(
                conn, effective, workspace_id=workspace_id
            )
            trust_event_repository.append_in_transaction(
                conn,
                (
                    TrustEventType.ACTION_ALLOWED
                    if effective_decision == PolicyDecisionType.ALLOW
                    else TrustEventType.ACTION_BLOCKED
                ),
                entity_type="proposed_action",
                entity_id=action.id,
                actor=user.email,
                workspace_id=workspace_id,
                correlation_id=action.run_id or action.task_id,
                payload={
                    "authorization_id": authorization.id,
                    "action_payload_hash": action_hash,
                    "prior_policy_decision_id": current.id,
                    "effective_policy_decision_id": effective.id,
                    "decision": effective_decision,
                    "reason": payload.reason,
                },
            )
            return authorization


def record_agent_run_action_authorization_postgres(
    action_id: str,
    payload: ActionAuthorizationCreate,
    *,
    expected_run_id: str,
    expected_step_id: str,
    user: AuthUser,
    workspace_id: str,
    connect: Callable[[], Connection],
    trust_repository: PostgresTrustRepository,
    trust_event_repository: PostgresTrustEventRepository,
    now: datetime,
) -> ActionAuthorization:
    """Record a reviewer decision for one exact run-linked action.

    This persists trust state only. It intentionally does not update the run or
    make a worker executable; the production AgentRun route remains fail-closed
    until those writes can share one durable execution transaction.
    The authenticated route must first verify reviewer workspace membership.
    """
    if not workspace_id or not expected_run_id or not expected_step_id:
        raise ValueError("AgentRun action authorization requires full scope")
    if user.role not in REVIEWER_ROLES:
        raise ActionAuthorizationConflict("A reviewer role is required for action decisions.")

    with connect() as conn:
        with conn.transaction():
            action = trust_repository.lock_agent_run_action_binding(
                conn,
                action_id=action_id,
                workspace_id=workspace_id,
                run_id=expected_run_id,
                step_id=expected_step_id,
            )
            if (
                action.workspace_id != workspace_id
                or action.run_id != expected_run_id
                or str(action.metadata.get("step_id") or "") != expected_step_id
                or not action.lease_id
            ):
                raise ActionAuthorizationConflict(
                    "Proposed action is not bound to the requested AgentRun step."
                )

            current = trust_repository.get_latest_policy_decision_for_action(
                conn, action_id, workspace_id, for_update=True
            )
            if current is None:
                raise ActionAuthorizationConflict("Action has no policy decision.")
            existing = trust_repository.get_action_authorization_for_action(
                conn, action_id, workspace_id, for_update=True
            )

            if existing is not None:
                prior = trust_repository.get_policy_decision(
                    conn, existing.policy_decision_id, workspace_id
                )
                effective = (
                    PolicyDecisionType.ALLOW
                    if existing.decision == ActionAuthorizationDecision.ALLOW
                    else PolicyDecisionType.BLOCK
                )
                if (
                    existing.workspace_id != workspace_id
                    or existing.reviewer_user_id != user.id
                    or existing.reviewer_role != user.role
                    or existing.decision != payload.decision
                    or existing.reason != payload.reason
                    or not verify_action_authorization_payload(existing, action)
                    or prior is None
                    or prior.proposed_action_id != action.id
                    or prior.lease_id != action.lease_id
                    or prior.decision != PolicyDecisionType.REQUIRE_APPROVAL
                    or current.proposed_action_id != action.id
                    or current.lease_id != action.lease_id
                    or current.evaluated_policy_version
                    != prior.evaluated_policy_version + "+human-decision"
                    or current.decision != effective
                ):
                    raise ActionAuthorizationConflict(
                        "Existing run action authorization does not match this exact reviewer decision."
                    )
                return existing

            if (
                current.proposed_action_id != action.id
                or current.lease_id != action.lease_id
                or current.decision != PolicyDecisionType.REQUIRE_APPROVAL
            ):
                raise ActionAuthorizationConflict(
                    "Run-linked action is not awaiting a human decision."
                )

            action_hash = action_payload_hash(action)
            authorization = ActionAuthorization(
                action_id=action.id,
                workspace_id=workspace_id,
                action_payload_hash=action_hash,
                policy_decision_id=current.id,
                reviewer_user_id=user.id,
                reviewer_role=user.role,
                decision=payload.decision,
                reason=payload.reason,
                signature="pending",
            )
            authorization.signature = sign_payload(
                authorization_signature_payload(authorization)
            )
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
                    "A human reviewer allowed this exact AgentRun action."
                    if effective_decision == PolicyDecisionType.ALLOW
                    else "A human reviewer denied this exact AgentRun action."
                ],
                obligations=(
                    ["Execute only the immutable action payload and record consumption evidence."]
                    if effective_decision == PolicyDecisionType.ALLOW
                    else ["Do not execute; revise the action or capability lease."]
                ),
                evaluated_policy_version=current.evaluated_policy_version
                + "+human-decision",
                evaluated_at=now,
            )
            trust_repository.insert_action_authorization(conn, authorization)
            trust_repository.insert_policy_decision(
                conn, effective, workspace_id=workspace_id
            )
            trust_event_repository.append_in_transaction(
                conn,
                (
                    TrustEventType.ACTION_ALLOWED
                    if effective_decision == PolicyDecisionType.ALLOW
                    else TrustEventType.ACTION_BLOCKED
                ),
                entity_type="proposed_action",
                entity_id=action.id,
                actor=user.email,
                workspace_id=workspace_id,
                correlation_id=expected_run_id,
                payload={
                    "authorization_id": authorization.id,
                    "action_payload_hash": action_hash,
                    "prior_policy_decision_id": current.id,
                    "effective_policy_decision_id": effective.id,
                    "decision": effective_decision,
                    "reason": payload.reason,
                    "run_id": expected_run_id,
                    "step_id": expected_step_id,
                },
            )
            return authorization
