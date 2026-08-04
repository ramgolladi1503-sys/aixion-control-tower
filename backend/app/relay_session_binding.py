from __future__ import annotations

from .agent_run_models import AgentRunStatus
from .models import AgentProvider, AuthUser
from .relay_models import RelayProvider, RelaySessionCreate
from .relay_service import RelayConflict
from .settings import get_settings
from .store import store
from .trust_run_capability import RunCapabilityConflict, ensure_run_capability


TERMINAL_RUN_STATUSES = {
    AgentRunStatus.BLOCKED,
    AgentRunStatus.SUCCEEDED,
    AgentRunStatus.FAILED,
    AgentRunStatus.CANCELLED,
}

PROVIDER_BINDINGS: dict[AgentProvider, set[RelayProvider]] = {
    AgentProvider.CODEX: {RelayProvider.CODEX, RelayProvider.CHATGPT},
    AgentProvider.CHATGPT: {RelayProvider.CHATGPT, RelayProvider.CODEX},
    AgentProvider.CLAUDE: {RelayProvider.CLAUDE},
    AgentProvider.CURSOR: {RelayProvider.CURSOR},
    AgentProvider.GITHUB_ACTIONS: {
        RelayProvider.GITHUB_ACTIONS,
        RelayProvider.COPILOT,
    },
}


def bind_relay_session_to_run(
    payload: RelaySessionCreate,
    *,
    user: AuthUser,
) -> RelaySessionCreate:
    settings = get_settings()
    if not payload.run_id:
        if settings.trust_enforcement:
            raise RelayConflict(
                "Production relay sessions require an approved durable AgentRun."
            )
        return payload

    run = store.agent_runs.get(payload.run_id)
    if run is None:
        raise ValueError("Agent run not found.")
    if run.status in TERMINAL_RUN_STATUSES:
        raise RelayConflict(
            f"Terminal AgentRun {run.status} cannot start a provider session."
        )
    task = store.agent_tasks.get(run.task_id)
    if task is None:
        raise RelayConflict("AgentRun is missing its AgentTask.")
    if run.approval_request_id is None:
        raise RelayConflict("AgentRun is missing its approved request.")

    allowed_providers = PROVIDER_BINDINGS.get(task.provider)
    if allowed_providers and payload.provider not in allowed_providers:
        raise RelayConflict(
            f"Relay provider {payload.provider} conflicts with AgentTask provider "
            f"{task.provider}."
        )

    try:
        ensure_run_capability(run, user=user)
    except RunCapabilityConflict as error:
        raise RelayConflict(str(error)) from error
    if not run.capability_lease_id:
        raise RelayConflict("AgentRun capability lease could not be established.")

    if payload.task_id and payload.task_id != task.id:
        raise RelayConflict("Relay session task conflicts with AgentRun task truth.")
    if payload.project_id and payload.project_id != task.project_id:
        raise RelayConflict("Relay session project conflicts with AgentRun project truth.")
    if payload.repository and payload.repository != task.repository:
        raise RelayConflict("Relay session repository conflicts with AgentRun repository truth.")

    return payload.model_copy(
        update={
            "task_id": task.id,
            "project_id": task.project_id,
            "repository": task.repository,
            "metadata": {
                **payload.metadata,
                "bound_agent_run_id": run.id,
                "bound_capability_lease_id": run.capability_lease_id,
                "bound_task_provider": task.provider.value,
            },
        }
    )
