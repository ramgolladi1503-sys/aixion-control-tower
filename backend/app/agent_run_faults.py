from __future__ import annotations

from .agent_run_models import AgentRunFaultConfig, AgentRunFaultKind, AgentRunStepType
from .store import store


class InjectedAgentRunFault(RuntimeError):
    def __init__(self, fault: AgentRunFaultConfig) -> None:
        self.fault = fault
        message = fault.message or fault.fault_kind.value.replace("_", " ").title()
        super().__init__(message)


def matching_fault(run_id: str, step_type: AgentRunStepType) -> AgentRunFaultConfig | None:
    candidates = [
        fault
        for fault in store.agent_run_faults.values()
        if fault.enabled
        and fault.remaining_uses > 0
        and (fault.run_id is None or fault.run_id == run_id)
        and (fault.step_type is None or fault.step_type == step_type)
    ]
    return sorted(candidates, key=lambda fault: fault.created_at)[0] if candidates else None


def consume_fault(run_id: str, step_type: AgentRunStepType) -> AgentRunFaultConfig | None:
    fault = matching_fault(run_id, step_type)
    if fault is None:
        return None
    fault.remaining_uses -= 1
    if fault.remaining_uses <= 0:
        fault.enabled = False
    store.persist()
    return fault


def raise_if_fault_configured(run_id: str, step_type: AgentRunStepType) -> None:
    fault = consume_fault(run_id, step_type)
    if fault is None:
        return
    raise InjectedAgentRunFault(fault)


def default_fault_reason(fault_kind: AgentRunFaultKind) -> str:
    return {
        AgentRunFaultKind.WORKER_CRASH: "Worker lost during execution; lease expired.",
        AgentRunFaultKind.GITHUB_TIMEOUT: "GitHub request timed out temporarily.",
        AgentRunFaultKind.DUPLICATE_CALLBACK: "Duplicate callback delivered for the same idempotency key.",
        AgentRunFaultKind.LOST_ACKNOWLEDGEMENT: "Lost acknowledgement after a possible side effect; manual reconciliation required.",
        AgentRunFaultKind.VALIDATION_TIMEOUT: "Validation timed out.",
        AgentRunFaultKind.INVALID_AGENT_OUTPUT: "Invalid agent output requires revision.",
        AgentRunFaultKind.STALE_LEASE: "Worker lease expired before completion.",
        AgentRunFaultKind.DATABASE_DISCONNECT: "Database connection temporarily unavailable.",
        AgentRunFaultKind.CONTAINER_FAILURE: "Container startup temporarily unavailable.",
        AgentRunFaultKind.FORBIDDEN_FILE_MUTATION: "Forbidden file mutation outside approved scope.",
    }[fault_kind]
