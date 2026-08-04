from __future__ import annotations

from .relay_models import RelayActionStatus, RelayHost, RelaySession
from .relay_service import RelayConflict, evaluate_relay_action
from .store import store
from .trust_models import ProposedAction


def evaluate_bound_relay_action(
    relay: RelayHost,
    session: RelaySession,
    action: ProposedAction,
) -> RelayActionStatus:
    """Bind provider tool requests to existing durable approval truth.

    Relays and provider adapters are not allowed to invent a lease, branch, repository,
    task, or run identity. When a session is linked to an AgentRun, this function derives
    those values from the durable store and rejects conflicting provider claims.
    """
    run = store.agent_runs.get(session.run_id or "")
    task_id = session.task_id or (run.task_id if run else None)
    task = store.agent_tasks.get(task_id or "")

    conflicts: list[str] = []
    if session.run_id and run is None:
        conflicts.append("Relay session references a missing AgentRun.")
    if task_id and task is None:
        conflicts.append("Relay session references a missing AgentTask.")
    if action.run_id and session.run_id and action.run_id != session.run_id:
        conflicts.append("Provider action run id conflicts with the relay session.")
    if action.task_id and task_id and action.task_id != task_id:
        conflicts.append("Provider action task id conflicts with durable task truth.")
    if action.project_id and session.project_id and action.project_id != session.project_id:
        conflicts.append("Provider action project conflicts with the relay session.")
    if action.repository and session.repository and action.repository != session.repository:
        conflicts.append("Provider action repository conflicts with the relay session.")
    if task is not None:
        if session.repository and task.repository != session.repository:
            conflicts.append("Relay session repository conflicts with the AgentTask.")
        if action.repository and task.repository != action.repository:
            conflicts.append("Provider action repository conflicts with the AgentTask.")
        if action.branch and task.branch_preference and action.branch != task.branch_preference:
            conflicts.append("Provider action branch conflicts with the approved task branch.")
    if run is not None:
        if action.lease_id and run.capability_lease_id and action.lease_id != run.capability_lease_id:
            conflicts.append("Provider action capability lease conflicts with the AgentRun.")
        if not run.capability_lease_id:
            conflicts.append("Linked AgentRun has no capability lease.")
    if conflicts:
        raise RelayConflict(" ".join(conflicts))

    normalized = action.model_copy(
        update={
            "lease_id": action.lease_id or (run.capability_lease_id if run else None),
            "run_id": session.run_id or action.run_id,
            "task_id": task_id or action.task_id,
            "project_id": session.project_id or (task.project_id if task else action.project_id),
            "repository": session.repository or (task.repository if task else action.repository),
            "branch": action.branch or (task.branch_preference if task else None),
            "metadata": {
                **action.metadata,
                "durable_run_bound": run is not None,
                "durable_task_bound": task is not None,
            },
        }
    )
    return evaluate_relay_action(relay, session, normalized)
