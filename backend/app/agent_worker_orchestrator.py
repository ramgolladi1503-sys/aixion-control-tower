from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from psycopg.types.json import Jsonb

from .agent_task_models import AgentTask, AgentTaskEvent, AgentTaskEventType
from .agent_task_repository import PostgresAgentTaskRepository
from .agent_worker_container_validation import ContainerRuntime, ContainerValidationExecutor, ContainerValidationPolicy, container_validation_policy_metadata
from .agent_worker_github_branch import AgentWorkerGitHubBranchResult, GitHubBranchClient, run_agent_worker_github_branch_creation
from .agent_worker_github_files import AgentWorkerGitHubFilesResult, GitHubFileClient, run_agent_worker_github_file_application
from .agent_worker_github_pr import AgentWorkerGitHubPRResult, GitHubPullRequestClient, run_agent_worker_github_pr_creation
from .agent_worker_validation_runner import CommandExecutionResult, run_agent_worker_validation_commands
from .agent_worker_workspace import AgentWorkerWorkspaceResult, WorkspaceCommandRunner, cleanup_agent_worker_workspace, prepare_agent_worker_workspace
from .approval_repository import PostgresApprovalDecisionRepository
from .models import AuditEvent
from .postgres_audit_repository import PostgresAuditEventRepository
from .postgres_runtime import PostgresRuntime
from .settings import get_settings, validate_startup_environment
from .store import store


@dataclass
class AgentWorkerOrchestrationResult:
    success: bool
    task_id: str | None = None
    worker_id: str = "agent-worker-orchestrator"
    branch_result: AgentWorkerGitHubBranchResult | None = None
    file_result: AgentWorkerGitHubFilesResult | None = None
    workspace_result: AgentWorkerWorkspaceResult | None = None
    validation_result: Any | None = None
    pr_result: AgentWorkerGitHubPRResult | None = None
    summary_event_id: str | None = None
    final_status: str | None = None
    reason: str = ""
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["generated_at"] = self.generated_at.isoformat()
        return payload


def _is_postgres_pair(task_repository, approval_repository) -> bool:
    settings = get_settings()
    task_is_postgres = isinstance(task_repository, PostgresAgentTaskRepository)
    approval_is_postgres = isinstance(approval_repository, PostgresApprovalDecisionRepository)
    if (
        settings.is_production or settings.postgres_auth_enabled
    ) and task_repository is None and approval_repository is None:
        raise ValueError(
            "PostgreSQL orchestration repositories are required in production or PostgreSQL auth mode."
        )
    if task_is_postgres != approval_is_postgres:
        raise ValueError("PostgreSQL orchestration requires paired task and approval repositories.")
    if task_repository is not None and not task_is_postgres:
        raise ValueError("Orchestration task repository is not supported.")
    if approval_repository is not None and not approval_is_postgres:
        raise ValueError("Orchestration approval repository is not supported.")
    return task_is_postgres


def _task(task_id: str | None, task_repository=None) -> AgentTask | None:
    if task_id is None:
        return None
    if isinstance(task_repository, PostgresAgentTaskRepository):
        return task_repository.get(task_id)
    return store.agent_tasks.get(task_id)


def _workspace_summary(workspace: AgentWorkerWorkspaceResult | None) -> dict[str, Any]:
    if workspace is None:
        return {"workspace_success": None, "workspace_isolated": None, "workspace_cleaned": None}
    return {
        "workspace_success": workspace.success,
        "workspace_isolated": workspace.workspace_root is not None,
        "workspace_cleaned": workspace.cleaned,
        "workspace_path_redacted": workspace.workspace_root is not None,
        "repository_path_redacted": workspace.repository_path is not None,
    }


def _append_summary_event(task: AgentTask, worker_id: str, success: bool, reason: str, details: dict[str, Any]) -> AgentTaskEvent:
    return AgentTaskEvent(
        task_id=task.id,
        event_type=AgentTaskEventType.DONE if success else AgentTaskEventType.FAILED,
        message=reason,
        status=task.status,
        actor=worker_id,
        metadata={"worker_id": worker_id, "orchestration_success": success, **details, "operation_type": "approved_agent_task_orchestration"},
    )


def _insert_postgres_audit(
    audit_repository: PostgresAuditEventRepository, audit: AuditEvent
) -> None:
    if not audit.workspace_id:
        raise ValueError("PostgreSQL orchestration audit requires workspace ownership.")
    audit_repository.append_agent_task_event(audit)


def _commit_postgres_orchestration_evidence(
    task_repository: PostgresAgentTaskRepository,
    *,
    expected_task: AgentTask,
    event: AgentTaskEvent,
    audit: AuditEvent,
) -> None:
    """Atomically persist the orchestration event and its matching audit record."""
    if (
        not expected_task.workspace_id
        or event.task_id != expected_task.id
        or event.status != expected_task.status
        or event.actor != audit.actor
        or audit.entity_id != expected_task.id
        or audit.workspace_id != expected_task.workspace_id
        or audit.details.get("event_id") != event.id
    ):
        raise ValueError("Orchestration event and audit do not match the durable task.")
    with task_repository._connect() as conn:
        row = conn.execute(
            "SELECT workspace_id,project_id,payload FROM agent_tasks WHERE task_id=%s FOR UPDATE",
            (expected_task.id,),
        ).fetchone()
        if (
            row is None
            or row[0] != expected_task.workspace_id
            or row[1] != expected_task.project_id
            or AgentTask.model_validate(row[2]) != expected_task
        ):
            raise RuntimeError("AgentTask changed before orchestration evidence could be committed.")
        conn.execute(
            "INSERT INTO agent_task_events(event_id,task_id,workspace_id,event_type,actor,created_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (
                event.id,
                event.task_id,
                expected_task.workspace_id,
                event.event_type.value,
                event.actor,
                event.created_at,
                Jsonb(event.model_dump(mode="json")),
            ),
        )
        conn.execute(
            "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,event_type,"
            "actor,details,created_at,payload) VALUES(%s,%s,NULL,'agent_task',%s,%s,%s,%s,%s,%s)",
            (
                audit.id,
                audit.workspace_id,
                audit.entity_id,
                audit.event_type,
                audit.actor,
                Jsonb(audit.details),
                audit.created_at,
                Jsonb(audit.model_dump(mode="json")),
            ),
        )


def _summary_audit(task: AgentTask, event: AgentTaskEvent, worker_id: str, audit_type: str, details: dict[str, Any]) -> AuditEvent:
    return AuditEvent(
        event_type=audit_type,
        entity_id=task.id,
        workspace_id=task.workspace_id,
        actor=worker_id,
        details={"event_id": event.id, "status": event.status, "message": event.message, **details},
    )


def _persist_summary(
    task: AgentTask,
    event: AgentTaskEvent,
    audit: AuditEvent,
    *,
    task_repository=None,
) -> None:
    if isinstance(task_repository, PostgresAgentTaskRepository):
        _commit_postgres_orchestration_evidence(
            task_repository, expected_task=task, event=event, audit=audit
        )
        return
    store.agent_task_events[event.id] = event
    store.audit_events.append(audit)
    store.persist()


def _failure_result(
    *, task_id: str | None, worker_id: str, reason: str,
    branch_result=None, file_result=None, workspace_result=None,
    validation_result=None, pr_result=None, task_repository=None,
) -> AgentWorkerOrchestrationResult:
    task = _task(task_id, task_repository)
    summary_event_id = None
    if task is not None:
        details = {
            "branch_success": branch_result.success if branch_result else None,
            "file_success": file_result.success if file_result else None,
            **_workspace_summary(workspace_result),
            "validation_success": validation_result.success if validation_result else None,
            "pr_success": pr_result.success if pr_result else None,
        }
        event = _append_summary_event(task, worker_id, False, reason, details)
        audit = _summary_audit(task, event, worker_id, "agent_worker.orchestration_failed", {"reason": reason, **_workspace_summary(workspace_result)})
        try:
            _persist_summary(task, event, audit, task_repository=task_repository)
        except Exception as error:  # noqa: BLE001 - expose uncertainty after a failed durable record.
            reason = f"{reason} Final orchestration evidence could not be committed; persisted outcome is ambiguous: {error}"
        else:
            summary_event_id = event.id
    return AgentWorkerOrchestrationResult(False, task_id, worker_id, branch_result, file_result, workspace_result, validation_result, pr_result, summary_event_id, task.status if task else None, reason)


def _should_prepare_workspace(use_isolated_workspace: bool, validation_executor, workspace_runner, cwd: Path | None) -> bool:
    if not use_isolated_workspace:
        return False
    return not (validation_executor is not None and workspace_runner is None and cwd is None)


def _build_container_validation_executor(
    *, validation_cwd: Path | None, timeout_seconds: int, worker_id: str,
    container_runtime: ContainerRuntime | None = None,
    task: AgentTask | None = None,
    task_repository=None,
    audit_repository: PostgresAuditEventRepository | None = None,
) -> Callable[[str], CommandExecutionResult] | None:
    if validation_cwd is None:
        return None
    policy = ContainerValidationPolicy()
    details = {"worker_id": worker_id, "container_validation_policy": container_validation_policy_metadata(policy)}
    if isinstance(task_repository, PostgresAgentTaskRepository):
        if task is None or not isinstance(audit_repository, PostgresAuditEventRepository):
            raise ValueError("PostgreSQL container validation audit requires its AgentTask.")
        _insert_postgres_audit(
            audit_repository,
            AuditEvent(
                event_type="agent_worker.container_validation_configured",
                entity_id=task.id,
                workspace_id=task.workspace_id,
                details=details,
                actor=worker_id,
            ),
        )
    else:
        event = AuditEvent(event_type="agent_worker.container_validation_configured", entity_id=str(validation_cwd), details=details, actor=worker_id)
        store.audit_events.append(event)
        store.persist()
    return ContainerValidationExecutor(cwd=validation_cwd, timeout_seconds=timeout_seconds, policy=policy, runtime=container_runtime)


def run_approved_agent_task_worker_flow(
    *,
    task_id: str,
    worker_id: str = "agent-worker-orchestrator",
    lease_seconds: int = 300,
    source_branch: str = "main",
    base_branch: str = "main",
    cwd: Path | None = None,
    timeout_seconds: int = 120,
    branch_client: GitHubBranchClient | None = None,
    file_client: GitHubFileClient | None = None,
    pr_client: GitHubPullRequestClient | None = None,
    workspace_runner: WorkspaceCommandRunner | None = None,
    use_isolated_workspace: bool = True,
    use_container_validation: bool = True,
    validation_executor: Callable[[str], CommandExecutionResult] | None = None,
    container_runtime: ContainerRuntime | None = None,
    task_repository=None,
    approval_repository=None,
    audit_repository: PostgresAuditEventRepository | None = None,
) -> AgentWorkerOrchestrationResult:
    postgres_mode = _is_postgres_pair(task_repository, approval_repository)
    if postgres_mode and not isinstance(audit_repository, PostgresAuditEventRepository):
        raise ValueError("PostgreSQL orchestration requires its runtime-bound audit repository.")
    if audit_repository is not None and not isinstance(audit_repository, PostgresAuditEventRepository):
        raise ValueError("Orchestration audit repository is not supported.")
    task = _task(task_id, task_repository)
    if task is None:
        return AgentWorkerOrchestrationResult(success=False, task_id=task_id, worker_id=worker_id, reason="Agent task not found.")

    branch_result = file_result = workspace_result = validation_result = pr_result = None
    if postgres_mode:
        start_event = AgentTaskEvent(
            task_id=task.id,
            event_type=AgentTaskEventType.NOTE,
            message="Approved AgentTask worker orchestration started.",
            status=task.status,
            actor=worker_id,
            metadata={"worker_id": worker_id, "container_validation_required": use_container_validation, "operation_type": "approved_agent_task_orchestration"},
        )
        start_audit = _summary_audit(
            task, start_event, worker_id, "agent_worker.orchestration_started",
            {"container_validation_required": use_container_validation},
        )
        try:
            _persist_summary(task, start_event, start_audit, task_repository=task_repository)
        except Exception as error:  # noqa: BLE001 - fail before any external side effects.
            return AgentWorkerOrchestrationResult(
                False, task.id, worker_id, reason=f"Orchestration was not started because start evidence could not be committed: {error}"
            )
    else:
        _audit = AuditEvent(event_type="agent_worker.orchestration_started", entity_id=task.id, details={"worker_id": worker_id, "container_validation_required": use_container_validation}, actor=worker_id)
        store.audit_events.append(_audit)

    try:
        branch_result = run_agent_worker_github_branch_creation(task_id=task.id, worker_id=f"{worker_id}:branch", lease_seconds=lease_seconds, source_branch=source_branch, client=branch_client, task_repository=task_repository)
        if not branch_result.success:
            return _failure_result(task_id=task.id, worker_id=worker_id, reason=f"Branch step failed: {branch_result.reason}", branch_result=branch_result, task_repository=task_repository)

        file_result = run_agent_worker_github_file_application(task_id=task.id, worker_id=f"{worker_id}:files", lease_seconds=lease_seconds, client=file_client, task_repository=task_repository, approval_repository=approval_repository)
        if not file_result.success:
            return _failure_result(task_id=task.id, worker_id=worker_id, reason=f"File application step failed: {file_result.reason}", branch_result=branch_result, file_result=file_result, task_repository=task_repository)

        validation_cwd = cwd
        if _should_prepare_workspace(use_isolated_workspace, validation_executor, workspace_runner, cwd):
            current_task = _task(task.id, task_repository) or task
            workspace_result = prepare_agent_worker_workspace(
                task=current_task,
                branch=branch_result.branch or current_task.branch_preference or "",
                worker_id=f"{worker_id}:workspace",
                workspace_parent=cwd,
                runner=workspace_runner,
                audit_writer=(
                    audit_repository.append_agent_task_event if postgres_mode else None
                ),
            )
            if not workspace_result.success:
                return _failure_result(task_id=task.id, worker_id=worker_id, reason=f"Workspace step failed: {workspace_result.reason}", branch_result=branch_result, file_result=file_result, workspace_result=workspace_result, task_repository=task_repository)
            validation_cwd = workspace_result.repository_path

        effective_executor = validation_executor
        if effective_executor is None and use_container_validation:
            current_task = _task(task.id, task_repository) or task
            effective_executor = _build_container_validation_executor(
                validation_cwd=validation_cwd,
                timeout_seconds=timeout_seconds,
                worker_id=f"{worker_id}:validation",
                container_runtime=container_runtime,
                task=current_task,
                task_repository=task_repository,
                audit_repository=audit_repository,
            )
            if effective_executor is None:
                return _failure_result(task_id=task.id, worker_id=worker_id, reason="Validation step failed: container validation requires an isolated workspace.", branch_result=branch_result, file_result=file_result, workspace_result=workspace_result, task_repository=task_repository)

        validation_result = run_agent_worker_validation_commands(task_id=task.id, worker_id=f"{worker_id}:validation", lease_seconds=lease_seconds, cwd=validation_cwd, timeout_seconds=timeout_seconds, executor=effective_executor, task_repository=task_repository, approval_repository=approval_repository)
        if not validation_result.success:
            return _failure_result(task_id=task.id, worker_id=worker_id, reason=f"Validation step failed: {validation_result.reason}", branch_result=branch_result, file_result=file_result, workspace_result=workspace_result, validation_result=validation_result, task_repository=task_repository)

        pr_result = run_agent_worker_github_pr_creation(task_id=task.id, worker_id=f"{worker_id}:pr", lease_seconds=lease_seconds, base_branch=base_branch, client=pr_client, task_repository=task_repository, approval_repository=approval_repository)
        if not pr_result.success:
            return _failure_result(task_id=task.id, worker_id=worker_id, reason=f"PR step failed: {pr_result.reason}", branch_result=branch_result, file_result=file_result, workspace_result=workspace_result, validation_result=validation_result, pr_result=pr_result, task_repository=task_repository)

        completed_task = _task(task.id, task_repository) or task
        reason = "Approved AgentTask worker flow completed and PR is ready for human review."
        details = {"branch": branch_result.branch, "files_written": file_result.files_written, "files_deleted": file_result.files_deleted, "commits_created": file_result.commits_created, **_workspace_summary(workspace_result), "validation_commands": validation_result.command_count, "container_validation_required": use_container_validation, "pull_request_url": pr_result.pull_request_url, "pull_request_number": pr_result.pull_request_number}
        event = _append_summary_event(completed_task, worker_id, True, reason, details)
        audit = _summary_audit(completed_task, event, worker_id, "agent_worker.orchestration_completed", {"pull_request_url": pr_result.pull_request_url, "pull_request_number": pr_result.pull_request_number, "container_validation_required": use_container_validation, **_workspace_summary(workspace_result)})
        try:
            _persist_summary(completed_task, event, audit, task_repository=task_repository)
        except Exception as error:  # noqa: BLE001 - external GitHub side effects may already exist.
            return AgentWorkerOrchestrationResult(False, completed_task.id, worker_id, branch_result, file_result, workspace_result, validation_result, pr_result, None, completed_task.status, f"All stages reported success, but final orchestration evidence could not be committed; persisted outcome is ambiguous: {error}")
        return AgentWorkerOrchestrationResult(True, completed_task.id, worker_id, branch_result, file_result, workspace_result, validation_result, pr_result, event.id, completed_task.status, reason)
    except Exception as error:  # noqa: BLE001 - preserve a durable failure record for unexpected stage errors.
        return _failure_result(task_id=task.id, worker_id=worker_id, reason=f"Orchestration failed unexpectedly: {error}", branch_result=branch_result, file_result=file_result, workspace_result=workspace_result, validation_result=validation_result, pr_result=pr_result, task_repository=task_repository)
    finally:
        if workspace_result is not None:
            cleanup_agent_worker_workspace(workspace_result)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run approved AgentTask worker flow: branch, files, isolated validation, PR.")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--worker-id", default="agent-worker-orchestrator")
    parser.add_argument("--lease-seconds", type=int, default=300)
    parser.add_argument("--source-branch", default="main")
    parser.add_argument("--base-branch", default="main")
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--no-isolated-workspace", action="store_true")
    parser.add_argument("--no-container-validation", action="store_true")
    parser.add_argument("--postgres", action="store_true", help="Use the configured PostgreSQL runtime; production profile selects PostgreSQL automatically.")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        settings = get_settings()
        postgres_mode = args.postgres or settings.profile == "production"
        if postgres_mode:
            if settings.profile == "production":
                settings = validate_startup_environment()
            runtime = PostgresRuntime.from_environment()
            try:
                runtime.open(wait=True)
                probe = runtime.probe()
                if not probe.ready:
                    raise RuntimeError(
                        "PostgreSQL schema is not ready; missing migrations: "
                        + ", ".join(probe.schema.missing_migration_ids)
                    )
                repositories = runtime.repositories
                result = run_approved_agent_task_worker_flow(
                    task_id=args.task_id,
                    worker_id=args.worker_id,
                    lease_seconds=args.lease_seconds,
                    source_branch=args.source_branch,
                    base_branch=args.base_branch,
                    cwd=Path(args.cwd) if args.cwd else None,
                    timeout_seconds=args.timeout_seconds,
                    use_isolated_workspace=not args.no_isolated_workspace,
                    use_container_validation=not args.no_container_validation,
                    task_repository=repositories.agent_tasks,
                    approval_repository=repositories.approval_decisions,
                    audit_repository=repositories.audit_events,
                )
            finally:
                runtime.close()
        else:
            result = run_approved_agent_task_worker_flow(task_id=args.task_id, worker_id=args.worker_id, lease_seconds=args.lease_seconds, source_branch=args.source_branch, base_branch=args.base_branch, cwd=Path(args.cwd) if args.cwd else None, timeout_seconds=args.timeout_seconds, use_isolated_workspace=not args.no_isolated_workspace, use_container_validation=not args.no_container_validation)
    except Exception as error:  # noqa: BLE001 - CLI must fail closed on invalid runtime configuration.
        result = AgentWorkerOrchestrationResult(success=False, task_id=args.task_id, worker_id=args.worker_id, reason=f"Worker runtime unavailable; no task work was started: {error}")
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True, default=str))
    else:
        print(f"Approved AgentTask worker flow: {'PASS' if result.success else 'FAILED'}")
        print(f"Task: {result.task_id or 'n/a'}")
        print(f"Final status: {result.final_status or 'n/a'}")
        print(f"Reason: {result.reason}")
    return 0 if result.success else 1
