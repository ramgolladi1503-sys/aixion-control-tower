from __future__ import annotations

from .agent_run_models import (
    AgentRun,
    AgentRunEvent,
    AgentRunEventType,
    AgentRunStatus,
    AgentRunStep,
    AgentRunStepStatus,
    AgentRunStepType,
)
from .models import now_utc
from .settings import get_settings
from .store import store
from .trust_consumption import consume_effective_action
from .trust_crypto import sha256_hex
from .trust_decisions import latest_policy_decision
from .trust_models import (
    ActionConsumption,
    CapabilityActionType,
    CapabilityLease,
    CapabilityLeaseStatus,
    PolicyDecisionType,
    ProposedAction,
)
from .trust_service import evaluate_gateway_action, refresh_lease_status


class AgentRunTrustConflict(RuntimeError):
    pass


SIDE_EFFECT_STEPS = {
    AgentRunStepType.CREATE_BRANCH,
    AgentRunStepType.APPLY_CHANGES,
    AgentRunStepType.RUN_VALIDATION,
    AgentRunStepType.CREATE_PULL_REQUEST,
}


def _append_run_event(
    run: AgentRun,
    step: AgentRunStep | None,
    *,
    event_type: AgentRunEventType,
    actor: str,
    message: str,
    metadata: dict,
) -> AgentRunEvent:
    event = AgentRunEvent(
        run_id=run.id,
        task_id=run.task_id,
        step_id=step.id if step else None,
        correlation_id=run.correlation_id,
        event_type=event_type,
        message=message,
        actor=actor,
        attempt_number=step.attempt_count if step else None,
        metadata=metadata,
    )
    store.agent_run_events[event.id] = event
    return event


def _validate_lease_for_run(run: AgentRun, lease: CapabilityLease) -> None:
    task = store.agent_tasks.get(run.task_id)
    approval = store.approval_requests.get(run.approval_request_id or "")
    refresh_lease_status(lease)
    reasons: list[str] = []
    if task is None:
        reasons.append("AgentTask is missing.")
    if approval is None:
        reasons.append("ApprovalRequest is missing.")
    if lease.status != CapabilityLeaseStatus.ACTIVE:
        reasons.append(f"Capability lease is not active: {lease.status}.")
    if lease.approval_request_id != run.approval_request_id:
        reasons.append("Capability lease approval does not match the run approval.")
    if lease.scope.repository != run.repository:
        reasons.append("Capability lease repository does not match the run repository.")
    if task is not None:
        if lease.provider != task.provider:
            reasons.append("Capability lease provider does not match the AgentTask provider.")
        if lease.agent_id and lease.agent_id != task.external_agent_id:
            reasons.append("Capability lease agent identity does not match the AgentTask agent.")
        if lease.scope.branch != task.branch_preference:
            reasons.append("Capability lease branch does not match the AgentTask branch.")
    if approval is not None and lease.approved_payload_hash != approval.approved_payload_hash:
        reasons.append("Capability lease is not bound to the current approved payload hash.")
    if reasons:
        raise AgentRunTrustConflict(" ".join(reasons))


def attach_capability_lease(
    run: AgentRun,
    lease_id: str,
    *,
    actor: str,
) -> AgentRun:
    if run.status in {
        AgentRunStatus.RUNNING,
        AgentRunStatus.EVALUATING,
        AgentRunStatus.BLOCKED,
        AgentRunStatus.SUCCEEDED,
        AgentRunStatus.FAILED,
        AgentRunStatus.CANCELLED,
    }:
        raise AgentRunTrustConflict(
            f"Capability lease cannot be attached while run is {run.status}."
        )
    current_step = store.agent_run_steps.get(run.current_step_id or "")
    if current_step and current_step.status == AgentRunStepStatus.RUNNING:
        raise AgentRunTrustConflict("Capability lease cannot change while a step is running.")
    lease = store.capability_leases.get(lease_id)
    if lease is None:
        raise ValueError("Capability lease not found.")
    _validate_lease_for_run(run, lease)
    if run.capability_lease_id:
        if run.capability_lease_id == lease.id:
            return run
        raise AgentRunTrustConflict(
            "A durable run cannot be rebound to a different capability lease."
        )
    run.capability_lease_id = lease.id
    run.updated_at = now_utc()
    _append_run_event(
        run,
        None,
        event_type=AgentRunEventType.CAPABILITY_LEASE_ATTACHED,
        actor=actor,
        message="Signed capability lease attached to the durable run.",
        metadata={
            "capability_lease_id": lease.id,
            "mode": lease.mode,
            "expires_at": lease.expires_at,
            "approved_payload_hash": lease.approved_payload_hash,
        },
    )
    store.persist()
    return run


def _action_id(run: AgentRun, step: AgentRunStep, suffix: str) -> str:
    material = {
        "run_id": run.id,
        "step_id": step.id,
        "next_attempt": step.attempt_count + 1,
        "suffix": suffix,
        "capability_lease_id": run.capability_lease_id,
    }
    return "trust_action_" + sha256_hex(material)[:32]


def _step_actions(
    run: AgentRun,
    step: AgentRunStep,
    *,
    timeout_seconds: int,
) -> list[ProposedAction]:
    task = store.agent_tasks.get(run.task_id)
    approval = store.approval_requests.get(run.approval_request_id or "")
    if task is None or approval is None:
        raise AgentRunTrustConflict("Run trust evaluation requires task and approval truth.")
    common = {
        "lease_id": run.capability_lease_id,
        "provider": task.provider,
        "agent_id": task.external_agent_id,
        "run_id": run.id,
        "task_id": task.id,
        "project_id": task.project_id,
        "repository": task.repository,
        "branch": task.branch_preference,
        "retry_number": step.attempt_count,
        "created_at": step.created_at,
        "metadata": {
            "step_id": step.id,
            "step_type": step.step_type.value,
            "idempotency_key": step.idempotency_key,
        },
    }
    if step.step_type == AgentRunStepType.CREATE_BRANCH:
        return [
            ProposedAction(
                id=_action_id(run, step, "branch"),
                action_type=CapabilityActionType.CREATE_BRANCH,
                estimated_runtime_seconds=min(timeout_seconds, 60),
                **common,
            )
        ]
    if step.step_type == AgentRunStepType.APPLY_CHANGES:
        return [
            ProposedAction(
                id=_action_id(run, step, "files"),
                action_type=CapabilityActionType.MODIFY_FILES,
                paths=[item.path for item in approval.files],
                estimated_runtime_seconds=min(timeout_seconds, 300),
                **common,
            )
        ]
    if step.step_type == AgentRunStepType.RUN_VALIDATION:
        return [
            ProposedAction(
                id=_action_id(run, step, f"command:{index}"),
                action_type=CapabilityActionType.RUN_COMMAND,
                command=command,
                estimated_runtime_seconds=timeout_seconds,
                **common,
            )
            for index, command in enumerate(approval.test_plan)
        ]
    if step.step_type == AgentRunStepType.CREATE_PULL_REQUEST:
        return [
            ProposedAction(
                id=_action_id(run, step, "pull-request"),
                action_type=CapabilityActionType.CREATE_PULL_REQUEST,
                estimated_runtime_seconds=min(timeout_seconds, 60),
                **common,
            )
        ]
    return []


def authorize_run_step(
    run: AgentRun,
    step: AgentRunStep,
    *,
    actor: str,
    timeout_seconds: int,
) -> list[ProposedAction]:
    if not get_settings().trust_enforcement or step.step_type not in SIDE_EFFECT_STEPS:
        return []
    if not run.capability_lease_id:
        raise AgentRunTrustConflict(
            "Production trust enforcement requires a signed capability lease before side effects."
        )
    lease = store.capability_leases.get(run.capability_lease_id)
    if lease is None:
        raise AgentRunTrustConflict("Run capability lease cannot be found.")
    _validate_lease_for_run(run, lease)

    actions = _step_actions(run, step, timeout_seconds=timeout_seconds)
    for action in actions:
        existing = store.proposed_actions.get(action.id)
        if existing is None:
            result = evaluate_gateway_action(action, actor=actor)
            decision = result.decision
        else:
            existing_payload = existing.model_dump(mode="json", exclude={"created_at"})
            action_payload = action.model_dump(mode="json", exclude={"created_at"})
            if existing_payload != action_payload:
                raise AgentRunTrustConflict(
                    "Deterministic trust action identifier resolved to a different payload."
                )
            decision = latest_policy_decision(action.id)
            if decision is None:
                raise AgentRunTrustConflict("Existing trust action has no policy decision.")
        _append_run_event(
            run,
            step,
            event_type=AgentRunEventType.TRUST_POLICY_EVALUATED,
            actor=actor,
            message="Trust policy evaluated the next governed run step.",
            metadata={
                "action_id": action.id,
                "decision_id": decision.id,
                "decision": decision.decision,
                "risk_level": decision.risk_level,
                "reasons": decision.reasons,
            },
        )
        if decision.decision == PolicyDecisionType.BLOCK:
            store.persist()
            raise AgentRunTrustConflict(
                "Trust policy blocked execution: " + " ".join(decision.reasons)
            )
        if decision.decision == PolicyDecisionType.REQUIRE_APPROVAL:
            store.persist()
            raise AgentRunTrustConflict(
                "Exact action approval is required before execution: " + action.id
            )
    store.persist()
    return actions


def consume_run_step_actions(
    actions: list[ProposedAction],
    step: AgentRunStep,
    *,
    actor: str,
) -> None:
    if not actions or step.status != AgentRunStepStatus.SUCCEEDED:
        return
    duration_seconds = max(0, int((step.duration_ms or 0) / 1000))
    per_action_runtime = duration_seconds // len(actions)
    remainder = duration_seconds - per_action_runtime * len(actions)
    for index, action in enumerate(actions):
        consumption = ActionConsumption(
            actual_runtime_seconds=per_action_runtime + (1 if index < remainder else 0),
            actual_cost_usd=0.0,
            pull_requests_created=(
                1 if step.step_type == AgentRunStepType.CREATE_PULL_REQUEST else 0
            ),
            retry_consumed=step.attempt_count > 1 and index == 0,
            output_reference=step.output_reference,
            evidence={
                "run_id": step.run_id,
                "step_id": step.id,
                "step_type": step.step_type,
                "step_evidence_hash": step.evidence_hash,
                "attempt_count": step.attempt_count,
            },
        )
        consume_effective_action(action.id, consumption, actor=actor)
