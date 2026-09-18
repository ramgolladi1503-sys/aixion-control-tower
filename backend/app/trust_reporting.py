from __future__ import annotations

from .models import RiskLevel
from .store import store
from .trust_action_authorization import action_payload_hash
from .trust_decisions import latest_policy_decision
from .trust_models import (
    AgentReliabilityScorecard,
    ExceptionQueueItem,
    PolicyDecisionType,
)


def _effective_decisions():
    return [
        decision
        for action in store.proposed_actions.values()
        if (decision := latest_policy_decision(action.id)) is not None
    ]


def build_reliability_scorecards() -> list[AgentReliabilityScorecard]:
    keys = {
        (action.provider, action.agent_id)
        for action in store.proposed_actions.values()
    }
    keys.update(
        (task.provider, task.external_agent_id)
        for task in store.agent_tasks.values()
    )
    effective = _effective_decisions()

    results: list[AgentReliabilityScorecard] = []
    for provider, agent_id in sorted(
        keys,
        key=lambda item: (item[0].value, item[1] or ""),
    ):
        actions = [
            action
            for action in store.proposed_actions.values()
            if action.provider == provider and action.agent_id == agent_id
        ]
        action_ids = {action.id for action in actions}
        decisions = [
            decision
            for decision in effective
            if decision.proposed_action_id in action_ids
        ]
        allowed = sum(
            decision.decision == PolicyDecisionType.ALLOW
            for decision in decisions
        )
        blocked = sum(
            decision.decision == PolicyDecisionType.BLOCK
            for decision in decisions
        )
        escalated = sum(
            decision.decision == PolicyDecisionType.REQUIRE_APPROVAL
            for decision in decisions
        )

        tasks = [
            task
            for task in store.agent_tasks.values()
            if task.provider == provider and task.external_agent_id == agent_id
        ]
        task_ids = {task.id for task in tasks}
        runs = [
            run
            for run in store.agent_runs.values()
            if run.task_id in task_ids
        ]
        run_ids = {run.id for run in runs}
        first_attempt_successes = 0
        for run in runs:
            steps = [
                step
                for step in store.agent_run_steps.values()
                if step.run_id == run.id
            ]
            if (
                run.status.value == "SUCCEEDED"
                and steps
                and all(step.attempt_count <= 1 for step in steps)
            ):
                first_attempt_successes += 1

        recovered_runs = {
            event.run_id
            for event in store.agent_run_events.values()
            if event.event_type.value == "STALE_LEASE_RECOVERED"
            and event.run_id in run_ids
        }
        recovered_successes = sum(
            run.id in recovered_runs and run.status.value == "SUCCEEDED"
            for run in runs
        )
        interventions = sum(
            run.status.value == "NEEDS_HUMAN"
            for run in runs
        )
        completed_with_evidence = sum(
            bool(run.final_evidence_hash)
            for run in runs
        )
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
        results.append(
            AgentReliabilityScorecard(
                provider=provider,
                agent_id=agent_id,
                evaluated_actions=evaluated,
                allowed_actions=allowed,
                blocked_actions=blocked,
                approval_escalations=escalated,
                scope_adherence_rate=(allowed / evaluated if evaluated else 0.0),
                first_attempt_success_rate=(
                    first_attempt_successes / len(runs) if runs else 0.0
                ),
                recovery_success_rate=(
                    recovered_successes / len(recovered_runs)
                    if recovered_runs
                    else 0.0
                ),
                human_intervention_rate=(
                    interventions / len(runs) if runs else 0.0
                ),
                policy_block_rate=(
                    blocked / evaluated if evaluated else 0.0
                ),
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
        severity = (
            RiskLevel.CRITICAL
            if run.status.value == "BLOCKED"
            else RiskLevel.HIGH
        )
        items.append(
            ExceptionQueueItem(
                id=f"run:{run.id}",
                category=run.status.value,
                severity=severity,
                title=f"Agent run {run.status.value.replace('_', ' ').lower()}",
                summary=(
                    run.last_error
                    or run.objective
                    or "Operator review is required."
                ),
                run_id=run.id,
                task_id=run.task_id,
                created_at=run.updated_at,
            )
        )

    for action in store.proposed_actions.values():
        decision = latest_policy_decision(action.id)
        if decision is None or decision.decision == PolicyDecisionType.ALLOW:
            continue
        metadata = action.metadata
        items.append(
            ExceptionQueueItem(
                id=f"action:{action.id}",
                category=decision.decision.value,
                severity=decision.risk_level,
                title=(
                    "Agent action blocked"
                    if decision.decision == PolicyDecisionType.BLOCK
                    else "Agent action needs exact approval"
                ),
                summary=" ".join(decision.reasons),
                run_id=action.run_id,
                task_id=action.task_id,
                action_id=action.id,
                lease_id=action.lease_id,
                provider=action.provider,
                action_type=action.action_type,
                command=action.command,
                paths=action.paths,
                network_domains=action.network_domains,
                repository=action.repository,
                branch=action.branch,
                cwd=str(metadata.get("cwd") or "") or None,
                relay_session_id=(
                    str(metadata.get("relay_session_id") or "") or None
                ),
                adapter_id=str(metadata.get("adapter_id") or "") or None,
                native_conversation_id=(
                    str(metadata.get("conversation_id") or "") or None
                ),
                native_step_index=(
                    int(metadata["step_index"])
                    if metadata.get("step_index") is not None
                    else None
                ),
                provider_payload_sha256=(
                    str(metadata.get("provider_payload_sha256") or "") or None
                ),
                action_payload_sha256=action_payload_hash(action),
                created_at=decision.evaluated_at,
            )
        )
    return sorted(items, key=lambda item: item.created_at, reverse=True)
