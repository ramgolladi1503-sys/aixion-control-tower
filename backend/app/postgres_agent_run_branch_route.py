from __future__ import annotations

from fastapi import HTTPException, Request

from .agent_run_models import AgentRun, AgentRunStep, AgentRunStepType
from .agent_run_supervisor import build_postgres_branch_completion
from .agent_task_models import AgentTaskStatus
from .agent_task_repository import PostgresAgentTaskRepository
from .agent_worker_github_branch import DEFAULT_SOURCE_BRANCH, _default_client
from .application_repository_access import application_repositories_for_request
from .application_repositories import ApplicationRepositories
from .approval_repository import PostgresApprovalDecisionRepository
from .models import AuthUser, now_utc
from .postgres_agent_run_branch import AgentRunBranchIntent, PostgresAgentRunBranchIntentRepository
from .postgres_agent_run_branch_executor import BranchExecutionRequest, PostgresCreateBranchCoordinator
from .postgres_runtime import PostgresRuntime
from .trust_crypto import sha256_hex
from .trust_models import CapabilityActionType, ProposedAction
from .agent_run_repository import PostgresAgentRunRepository
from .postgres_trust_repository import PostgresTrustRepository

SOURCE_BRANCH = DEFAULT_SOURCE_BRANCH


def postgres_branch_execution_dependencies(
    *, request: Request, run: AgentRun, step: AgentRunStep, actor: AuthUser
) -> tuple[PostgresCreateBranchCoordinator, object]:
    """Bind CREATE_BRANCH to the exact PostgreSQL authority owned by app lifespan."""
    state = request.app.state
    runtime = getattr(state, "postgres_runtime", None)
    try:
        repositories = application_repositories_for_request(request)
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail="PostgreSQL branch execution requires the complete lifespan-owned repository bundle.",
        ) from error
    if (
        not isinstance(runtime, PostgresRuntime)
        or not isinstance(repositories, ApplicationRepositories)
        or runtime.repositories is not repositories
        or getattr(state, "postgres_repositories", None) is not repositories
        or getattr(state, "agent_run_repository", None) is not repositories.agent_runs
        or getattr(state, "agent_task_repository", None) is not repositories.agent_tasks
        or getattr(state, "approval_decision_repository", None) is not repositories.approval_decisions
        or getattr(state, "trust_repository", None) is not repositories.trust
        or getattr(state, "trust_event_repository", None) is not repositories.trust_events
        or getattr(state, "agent_run_branch_intent_repository", None) is not repositories.agent_run_branch_intents
        or not isinstance(repositories.agent_runs, PostgresAgentRunRepository)
        or not isinstance(repositories.agent_tasks, PostgresAgentTaskRepository)
        or not isinstance(repositories.approval_decisions, PostgresApprovalDecisionRepository)
        or not isinstance(repositories.trust, PostgresTrustRepository)
        or not isinstance(repositories.agent_run_branch_intents, PostgresAgentRunBranchIntentRepository)
        or not run.workspace_id
        or not run.capability_lease_id
        or not run.approval_request_id
        or step.step_type != AgentRunStepType.CREATE_BRANCH
        or step.run_id != run.id
        or step.task_id != run.task_id
    ):
        raise HTTPException(status_code=503, detail="PostgreSQL branch execution requires the complete lifespan-owned run, task, approval, trust, and intent repositories.")

    task = repositories.agent_tasks.get(run.task_id)
    approval = repositories.approval_decisions.get(run.approval_request_id)
    with runtime.connect() as conn:
        lease = repositories.trust.get_capability_lease(
            conn, run.capability_lease_id, run.workspace_id, for_share=True
        )
    if (
        task is None or approval is None or lease is None
        or task.id != run.task_id or task.workspace_id != run.workspace_id
        or task.approval_request_id != approval.id
        or approval.id != run.approval_request_id
        or approval.workspace_id != run.workspace_id
        or task.project_id != approval.project_id
        or run.project_id != task.project_id
        or task.status != AgentTaskStatus.APPROVED
        or not task.repository or not task.branch_preference
        or lease.id != run.capability_lease_id
    ):
        raise HTTPException(status_code=409, detail="Durable CREATE_BRANCH task, approval, or lease scope is incomplete.")

    repository = task.repository
    target_ref = task.branch_preference
    try:
        provider = _default_client()
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail="GitHub branch provider is not configured for PostgreSQL CREATE_BRANCH.",
        ) from error
    coordinator = PostgresCreateBranchCoordinator(
        intent_repository=repositories.agent_run_branch_intents,
        run_repository=repositories.agent_runs,
        provider=provider,
        clock=now_utc,
    )

    def request_factory(current_run, current_step, worker_lease_token: str, timeout_seconds: int):
        if current_run.id != run.id or current_step.id != step.id or current_run.capability_lease_id != lease.id:
            raise ValueError("CREATE_BRANCH execution changed its durable run scope")

        def action_factory(source_sha: str) -> ProposedAction:
            identity = sha256_hex(f"{run.id}:{step.id}:{step.idempotency_key}:{lease.id}:{source_sha}")
            return ProposedAction(
                id=f"action_{identity}", lease_id=lease.id, provider=task.provider,
                agent_id=task.external_agent_id, run_id=run.id, task_id=task.id,
                project_id=task.project_id, workspace_id=run.workspace_id,
                action_type=CapabilityActionType.CREATE_BRANCH, repository=repository,
                branch=target_ref,
                metadata={
                    "source_sha": source_sha, "source_branch": SOURCE_BRANCH,
                    "target_ref": target_ref, "step_id": step.id,
                    "idempotency_key": step.idempotency_key, "task_id": task.id,
                    "approval_id": approval.id, "lease_id": lease.id,
                    "repository": repository,
                },
            )

        def intent_factory(source_sha: str, action: ProposedAction) -> AgentRunBranchIntent:
            stamp = now_utc()
            return AgentRunBranchIntent(
                id=f"branch_intent_{sha256_hex(f'{run.id}:{step.id}:{step.idempotency_key}')}",
                workspace_id=run.workspace_id, run_id=run.id, task_id=task.id,
                approval_id=approval.id, step_id=step.id, action_id=action.id,
                lease_id=lease.id, idempotency_key=step.idempotency_key,
                repository=repository, source_branch=SOURCE_BRANCH, target_ref=target_ref,
                source_sha=source_sha, lease_token_hash=sha256_hex(worker_lease_token),
                initial_lease_token_hash=sha256_hex(worker_lease_token),
                run_revision=current_run.updated_at, step_revision=current_step.updated_at,
                created_at=stamp, updated_at=stamp,
            )

        return BranchExecutionRequest(
            workspace_id=run.workspace_id, actor=actor.email,
            worker_lease_token=worker_lease_token, expected_run=current_run,
            expected_step=current_step, task=task, approval=approval, lease=lease,
            repository=repository, source_branch=SOURCE_BRANCH, target_ref=target_ref,
            timeout_seconds=timeout_seconds, action_factory=action_factory,
            intent_factory=intent_factory,
            completion_factory=lambda intent, url: build_postgres_branch_completion(
                run=current_run, steps=repositories.agent_runs.list_steps_for_run(run.id),
                intent=intent, branch_url=url, actor=actor.email,
                worker_lease_token=worker_lease_token,
            ),
        )

    return coordinator, request_factory
