from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .agent_task_models import AgentTask, AgentTaskEvent, AgentTaskEventType
from .agent_worker_container_validation import ContainerValidationExecutor, ContainerValidationPolicy, container_validation_policy_metadata
from .agent_worker_github_branch import AgentWorkerGitHubBranchResult, GitHubBranchClient, run_agent_worker_github_branch_creation
from .agent_worker_github_files import AgentWorkerGitHubFilesResult, GitHubFileClient, run_agent_worker_github_file_application
from .agent_worker_github_pr import AgentWorkerGitHubPRResult, GitHubPullRequestClient, run_agent_worker_github_pr_creation
from .agent_worker_validation_runner import CommandExecutionResult, run_agent_worker_validation_commands
from .agent_worker_workspace import AgentWorkerWorkspaceResult, WorkspaceCommandRunner, cleanup_agent_worker_workspace, prepare_agent_worker_workspace
from .models import AuditEvent
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


def _audit(event_type: str, entity_id: str, details: dict[str, Any], actor: str) -> AuditEvent:
    event = AuditEvent(event_type=event_type, entity_id=entity_id, details=details, actor=actor)
    store.audit_events.append(event)
    return event


def _task(task_id: str | None) -> AgentTask | None:
    return store.agent_tasks.get(task_id or "")


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
    event = AgentTaskEvent(
        task_id=task.id,
        event_type=AgentTaskEventType.DONE if success else AgentTaskEventType.FAILED,
        message=reason,
        status=task.status,
        actor=worker_id,
        metadata={"worker_id": worker_id, "orchestration_success": success, **details, "operation_type": "approved_agent_task_orchestration"},
    )
    store.agent_task_events[event.id] = event
    return event


def _failure_result(*, task_id: str | None, worker_id: str, reason: str, branch_result=None, file_result=None, workspace_result=None, validation_result=None, pr_result=None) -> AgentWorkerOrchestrationResult:
    task = _task(task_id)
    summary_event_id = None
    if task is not None:
        event = _append_summary_event(
            task,
            worker_id,
            False,
            reason,
            {
                "branch_success": branch_result.success if branch_result else None,
                "file_success": file_result.success if file_result else None,
                **_workspace_summary(workspace_result),
                "validation_success": validation_result.success if validation_result else None,
                "pr_success": pr_result.success if pr_result else None,
            },
        )
        summary_event_id = event.id
        _audit("agent_worker.orchestration_failed", task.id, {"worker_id": worker_id, "reason": reason, **_workspace_summary(workspace_result)}, actor=worker_id)
        store.persist()
    return AgentWorkerOrchestrationResult(False, task_id, worker_id, branch_result, file_result, workspace_result, validation_result, pr_result, summary_event_id, task.status if task else None, reason)


def _should_prepare_workspace(use_isolated_workspace: bool, validation_executor, workspace_runner, cwd: Path | None) -> bool:
    if not use_isolated_workspace:
        return False
    return not (validation_executor is not None and workspace_runner is None and cwd is None)


def _build_container_validation_executor(
    *,
    validation_cwd: Path | None,
    timeout_seconds: int,
    worker_id: str,
) -> Callable[[str], CommandExecutionResult] | None:
    if validation_cwd is None:
        return None
    policy = ContainerValidationPolicy()
    _audit(
        "agent_worker.container_validation_configured",
        str(validation_cwd),
        {"worker_id": worker_id, "container_validation_policy": container_validation_policy_metadata(policy)},
        actor=worker_id,
    )
    return ContainerValidationExecutor(cwd=validation_cwd, timeout_seconds=timeout_seconds, policy=policy)


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
) -> AgentWorkerOrchestrationResult:
    task = _task(task_id)
    if task is None:
        return AgentWorkerOrchestrationResult(success=False, task_id=task_id, worker_id=worker_id, reason="Agent task not found.")

    _audit("agent_worker.orchestration_started", task.id, {"worker_id": worker_id, "container_validation_required": use_container_validation}, actor=worker_id)
    branch_result = run_agent_worker_github_branch_creation(task_id=task.id, worker_id=f"{worker_id}:branch", lease_seconds=lease_seconds, source_branch=source_branch, client=branch_client)
    if not branch_result.success:
        return _failure_result(task_id=task.id, worker_id=worker_id, reason=f"Branch step failed: {branch_result.reason}", branch_result=branch_result)

    file_result = run_agent_worker_github_file_application(task_id=task.id, worker_id=f"{worker_id}:files", lease_seconds=lease_seconds, client=file_client)
    if not file_result.success:
        return _failure_result(task_id=task.id, worker_id=worker_id, reason=f"File application step failed: {file_result.reason}", branch_result=branch_result, file_result=file_result)

    workspace_result: AgentWorkerWorkspaceResult | None = None
    validation_cwd = cwd
    try:
        if _should_prepare_workspace(use_isolated_workspace, validation_executor, workspace_runner, cwd):
            workspace_result = prepare_agent_worker_workspace(task=task, branch=branch_result.branch or task.branch_preference or "", worker_id=f"{worker_id}:workspace", workspace_parent=cwd, runner=workspace_runner)
            if not workspace_result.success:
                return _failure_result(task_id=task.id, worker_id=worker_id, reason=f"Workspace step failed: {workspace_result.reason}", branch_result=branch_result, file_result=file_result, workspace_result=workspace_result)
            validation_cwd = workspace_result.repository_path

        effective_executor = validation_executor
        if effective_executor is None and use_container_validation:
            effective_executor = _build_container_validation_executor(validation_cwd=validation_cwd, timeout_seconds=timeout_seconds, worker_id=f"{worker_id}:validation")
            if effective_executor is None:
                return _failure_result(task_id=task.id, worker_id=worker_id, reason="Validation step failed: container validation requires an isolated workspace.", branch_result=branch_result, file_result=file_result, workspace_result=workspace_result)

        validation_result = run_agent_worker_validation_commands(task_id=task.id, worker_id=f"{worker_id}:validation", lease_seconds=lease_seconds, cwd=validation_cwd, timeout_seconds=timeout_seconds, executor=effective_executor)
        if not validation_result.success:
            return _failure_result(task_id=task.id, worker_id=worker_id, reason=f"Validation step failed: {validation_result.reason}", branch_result=branch_result, file_result=file_result, workspace_result=workspace_result, validation_result=validation_result)

        pr_result = run_agent_worker_github_pr_creation(task_id=task.id, worker_id=f"{worker_id}:pr", lease_seconds=lease_seconds, base_branch=base_branch, client=pr_client)
        if not pr_result.success:
            return _failure_result(task_id=task.id, worker_id=worker_id, reason=f"PR step failed: {pr_result.reason}", branch_result=branch_result, file_result=file_result, workspace_result=workspace_result, validation_result=validation_result, pr_result=pr_result)

        completed_task = _task(task.id) or task
        event = _append_summary_event(completed_task, worker_id, True, "Approved AgentTask worker flow completed and PR is ready for human review.", {"branch": branch_result.branch, "files_written": file_result.files_written, "files_deleted": file_result.files_deleted, "commits_created": file_result.commits_created, **_workspace_summary(workspace_result), "validation_commands": validation_result.command_count, "container_validation_required": use_container_validation, "pull_request_url": pr_result.pull_request_url, "pull_request_number": pr_result.pull_request_number})
        _audit("agent_worker.orchestration_completed", completed_task.id, {"worker_id": worker_id, "pull_request_url": pr_result.pull_request_url, "pull_request_number": pr_result.pull_request_number, "container_validation_required": use_container_validation, **_workspace_summary(workspace_result)}, actor=worker_id)
        store.persist()
        return AgentWorkerOrchestrationResult(True, completed_task.id, worker_id, branch_result, file_result, workspace_result, validation_result, pr_result, event.id, completed_task.status, "Approved AgentTask worker flow completed and PR is ready for human review.")
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
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    result = run_approved_agent_task_worker_flow(task_id=args.task_id, worker_id=args.worker_id, lease_seconds=args.lease_seconds, source_branch=args.source_branch, base_branch=args.base_branch, cwd=Path(args.cwd) if args.cwd else None, timeout_seconds=args.timeout_seconds, use_isolated_workspace=not args.no_isolated_workspace, use_container_validation=not args.no_container_validation)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True, default=str))
    else:
        print(f"Approved AgentTask worker flow: {'PASS' if result.success else 'FAILED'}")
        print(f"Task: {result.task_id or 'n/a'}")
        print(f"Final status: {result.final_status or 'n/a'}")
        print(f"Reason: {result.reason}")
    return 0 if result.success else 1
