from __future__ import annotations

import re
from dataclasses import replace
from typing import Protocol

from fastapi import HTTPException, Request

from .agent_run_models import AgentRun, AgentRunStep, AgentRunStepType
from .agent_run_supervisor import (
    AgentRunValidationBundle,
    PostgresAgentRunValidationCoordinator,
    PostgresRunValidationRequest,
    inspect_validation_bundle,
    postgres_run_validation_enabled,
)
from .agent_task_repository import PostgresAgentTaskRepository
from .approval_repository import PostgresApprovalDecisionRepository
from .application_repository_access import application_repositories_for_request
from .application_repositories import ApplicationRepositories
from .agent_run_repository import PostgresAgentRunRepository
from .agent_worker_container_validation import ContainerValidationPolicy, DockerContainerRuntime
from .models import ApprovalStatus
from .postgres_agent_run_apply_changes import ApplyChangeIntentStatus
from .postgres_agent_run_branch import BranchIntentStatus
from .postgres_runtime import PostgresRuntime
from .postgres_trust_repository import PostgresTrustRepository


class AgentRunValidationBundleProvider(Protocol):
    """Supplies a pinned immutable repository snapshot; it must not clone on request."""

    def resolve_bundle(
        self, *, run: AgentRun, step: AgentRunStep, task, approval
    ) -> AgentRunValidationBundle | None: ...


def postgres_validation_execution_dependencies(
    *, request: Request, run: AgentRun, step: AgentRunStep, actor: str,
) -> tuple[PostgresAgentRunValidationCoordinator, object, object]:
    """Bind PostgreSQL validation to lifespan-owned authority and an explicit bundle provider."""
    if not postgres_run_validation_enabled():
        raise HTTPException(
            status_code=503,
            detail=(
                "PostgreSQL RUN_VALIDATION is disabled by the default-off "
                "AIXION_POSTGRES_RUN_VALIDATION_ENABLED rollout gate."
            ),
        )
    state = request.app.state
    runtime = getattr(state, "postgres_runtime", None)
    try:
        repositories = application_repositories_for_request(request)
    except Exception as error:
        raise HTTPException(status_code=503, detail="PostgreSQL validation requires the lifespan-owned repository bundle.") from error
    if (
        not isinstance(runtime, PostgresRuntime)
        or not isinstance(repositories, ApplicationRepositories)
        or runtime.repositories is not repositories
        or getattr(state, "postgres_repositories", None) is not repositories
        or getattr(state, "agent_run_repository", None) is not repositories.agent_runs
        or getattr(state, "agent_task_repository", None) is not repositories.agent_tasks
        or getattr(state, "approval_decision_repository", None) is not repositories.approval_decisions
        or getattr(state, "trust_repository", None) is not repositories.trust
        or getattr(state, "agent_run_validation_intent_repository", None) is not repositories.agent_run_validation_intents
        or not isinstance(repositories.agent_runs, PostgresAgentRunRepository)
        or not isinstance(repositories.agent_tasks, PostgresAgentTaskRepository)
        or not isinstance(repositories.approval_decisions, PostgresApprovalDecisionRepository)
        or not isinstance(repositories.trust, PostgresTrustRepository)
        or step.step_type != AgentRunStepType.RUN_VALIDATION
        or step.run_id != run.id or step.task_id != run.task_id
        or not run.workspace_id or not run.approval_request_id or not run.capability_lease_id
    ):
        raise HTTPException(status_code=503, detail="PostgreSQL RUN_VALIDATION authority repositories are incomplete.")

    container_runtime = getattr(state, "agent_run_validation_container_runtime", None)
    if container_runtime is None:
        container_runtime = DockerContainerRuntime()
    if (
        not callable(getattr(container_runtime, "available", None))
        or not callable(getattr(container_runtime, "run_validation", None))
    ):
        raise HTTPException(status_code=503, detail="Container validation runtime is unavailable.")
    policy = ContainerValidationPolicy()
    if re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", policy.image) is None:
        raise HTTPException(
            status_code=503,
            detail="RUN_VALIDATION requires AIXION_VALIDATION_CONTAINER_IMAGE pinned by SHA-256 digest.",
        )
    provider = getattr(state, "agent_run_validation_bundle_provider", None)
    if provider is None or not callable(getattr(provider, "resolve_bundle", None)):
        raise HTTPException(
            status_code=503,
            detail="No trusted pre-materialized validation bundle provider is configured.",
        )
    task = repositories.agent_tasks.get(run.task_id)
    approval = repositories.approval_decisions.get(run.approval_request_id)
    with runtime.connect() as conn:
        lease = repositories.trust.get_capability_lease(
            conn, run.capability_lease_id, run.workspace_id, for_share=True
        )
    if (
        task is None or approval is None or lease is None
        or task.id != run.task_id or task.workspace_id != run.workspace_id
        or task.project_id != run.project_id or task.approval_request_id != approval.id
        or approval.id != run.approval_request_id or approval.workspace_id != run.workspace_id
        or approval.project_id != task.project_id or approval.status != ApprovalStatus.APPROVED
        or not task.repository or not task.branch_preference or lease.id != run.capability_lease_id
        or lease.scope.repository != task.repository
    ):
        raise HTTPException(status_code=409, detail="Durable RUN_VALIDATION task, approval, or lease scope is incomplete.")

    durable_steps = repositories.agent_runs.list_steps_for_run(run.id)
    branch_steps = [item for item in durable_steps if item.step_type == AgentRunStepType.CREATE_BRANCH]
    if len(branch_steps) != 1:
        raise HTTPException(status_code=409, detail="RUN_VALIDATION requires one completed durable CREATE_BRANCH intent.")
    branch_step = branch_steps[0]
    branch_intent = repositories.agent_run_branch_intents.get_for_step(
        workspace_id=run.workspace_id, run_id=run.id, step_id=branch_step.id,
        idempotency_key=branch_step.idempotency_key,
    )
    if (
        branch_intent is None or branch_intent.status != BranchIntentStatus.COMPLETED
        or branch_intent.repository != task.repository
        or branch_intent.target_ref != task.branch_preference
    ):
        raise HTTPException(status_code=409, detail="RUN_VALIDATION requires the completed run-bound branch intent.")
    expected_revision = branch_intent.source_sha
    apply_steps = [item for item in durable_steps if item.step_type == AgentRunStepType.APPLY_CHANGES]
    if len(apply_steps) > 1:
        raise HTTPException(status_code=409, detail="RUN_VALIDATION found ambiguous durable APPLY_CHANGES steps.")
    if apply_steps:
        apply_step = apply_steps[0]
        apply_intent = repositories.agent_run_apply_change_intents.get_for_step(
            workspace_id=run.workspace_id, run_id=run.id, step_id=apply_step.id,
            idempotency_key=apply_step.idempotency_key,
        )
        if (
            apply_step.status.value != "SUCCEEDED" or apply_intent is None
            or apply_intent.status != ApplyChangeIntentStatus.COMPLETED
            or not apply_intent.commit_sha
            or apply_intent.repository != task.repository
            or apply_intent.branch != task.branch_preference
        ):
            raise HTTPException(status_code=409, detail="RUN_VALIDATION requires the completed run-bound file change intent.")
        expected_revision = apply_intent.commit_sha

    try:
        bundle = provider.resolve_bundle(run=run, step=step, task=task, approval=approval)
    except Exception as error:
        raise HTTPException(status_code=503, detail="Trusted validation bundle resolution failed.") from error
    if (
        not isinstance(bundle, AgentRunValidationBundle)
        or not bundle.path.is_dir()
        or bundle.repository != task.repository
        or bundle.branch != task.branch_preference
        or bundle.revision != expected_revision
        or re.fullmatch(r"[0-9a-f]{40}", bundle.revision) is None
    ):
        raise HTTPException(status_code=409, detail="Validation bundle does not match the durable approved repository revision.")
    try:
        verified_tree_sha = inspect_validation_bundle(bundle)
        bundle = replace(bundle, git_tree_sha=verified_tree_sha)
    except Exception as error:
        raise HTTPException(status_code=409, detail="Validation bundle Git checkout is not clean and pinned.") from error

    coordinator = PostgresAgentRunValidationCoordinator()

    def request_factory(current_run, current_step, worker_lease_token: str, timeout_seconds: int):
        if current_run.id != run.id or current_step.id != step.id:
            raise ValueError("RUN_VALIDATION execution changed its durable run scope")
        return PostgresRunValidationRequest(
            expected_run=current_run,
            expected_step=current_step,
            task=task,
            approval=approval,
            worker_id=current_run.lease_owner or "",
            worker_lease_token=worker_lease_token,
            actor=actor,
            timeout_seconds=timeout_seconds,
            expected_bundle_revision=expected_revision,
            bundle=bundle,
            container_runtime=container_runtime,
            intent_repository=repositories.agent_run_validation_intents,
        )

    return coordinator, request_factory, repositories.agent_run_validation_intents
