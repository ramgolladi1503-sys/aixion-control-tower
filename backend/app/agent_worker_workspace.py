from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .agent_task_models import AgentTask
from .models import AuditEvent
from .store import store


@dataclass
class WorkspaceCommandResult:
    command: list[str]
    exit_code: int
    output_summary: str = ""

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


class WorkspaceCommandRunner(Protocol):
    def run(self, command: list[str], *, cwd: Path | None = None) -> WorkspaceCommandResult: ...


class SubprocessWorkspaceCommandRunner:
    def run(self, command: list[str], *, cwd: Path | None = None) -> WorkspaceCommandResult:
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
        )
        output = "\n".join(part for part in [completed.stdout, completed.stderr] if part).strip()
        return WorkspaceCommandResult(
            command=command,
            exit_code=completed.returncode,
            output_summary=output[-4000:],
        )


@dataclass
class AgentWorkerWorkspaceResult:
    success: bool
    task_id: str | None = None
    repository: str | None = None
    branch: str | None = None
    workspace_root: Path | None = None
    repository_path: Path | None = None
    cleaned: bool = False
    reason: str = ""
    commands: list[WorkspaceCommandResult] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["generated_at"] = self.generated_at.isoformat()
        if self.workspace_root is not None:
            payload["workspace_root"] = str(self.workspace_root)
        if self.repository_path is not None:
            payload["repository_path"] = str(self.repository_path)
        return payload


def _audit(event_type: str, entity_id: str, details: dict[str, Any], actor: str) -> AuditEvent:
    event = AuditEvent(event_type=event_type, entity_id=entity_id, details=details, actor=actor)
    store.audit_events.append(event)
    return event


def _planned_branch_for_task(task: AgentTask) -> str | None:
    return task.branch_preference or None


def _safe_repo_dir_name(repository: str) -> str:
    return repository.replace("/", "__").replace(":", "_")


def _redacted_workspace_metadata(result: AgentWorkerWorkspaceResult) -> dict[str, Any]:
    return {
        "repository": result.repository,
        "branch": result.branch,
        "workspace_isolated": True,
        "workspace_path_redacted": result.workspace_root is not None,
        "repository_path_redacted": result.repository_path is not None,
        "command_count": len(result.commands),
    }


def cleanup_agent_worker_workspace(result: AgentWorkerWorkspaceResult) -> AgentWorkerWorkspaceResult:
    if result.workspace_root is not None and result.workspace_root.exists():
        shutil.rmtree(result.workspace_root, ignore_errors=True)
    result.cleaned = True
    return result


def prepare_agent_worker_workspace(
    *,
    task: AgentTask,
    branch: str,
    worker_id: str = "agent-worker-orchestrator:workspace",
    workspace_parent: Path | None = None,
    runner: WorkspaceCommandRunner | None = None,
    cleanup_on_failure: bool = True,
) -> AgentWorkerWorkspaceResult:
    if not task.repository:
        return AgentWorkerWorkspaceResult(
            success=False,
            task_id=task.id,
            branch=branch,
            reason="Agent task repository is required for isolated workspace preparation.",
        )
    if not branch:
        return AgentWorkerWorkspaceResult(
            success=False,
            task_id=task.id,
            repository=task.repository,
            reason="Agent task branch is required for isolated workspace preparation.",
        )

    command_runner = runner or SubprocessWorkspaceCommandRunner()
    workspace_root = Path(tempfile.mkdtemp(prefix=f"aixion-agent-task-{task.id}-", dir=str(workspace_parent) if workspace_parent else None))
    repository_path = workspace_root / _safe_repo_dir_name(task.repository)
    result = AgentWorkerWorkspaceResult(
        success=False,
        task_id=task.id,
        repository=task.repository,
        branch=branch,
        workspace_root=workspace_root,
        repository_path=repository_path,
    )

    clone_url = f"https://github.com/{task.repository}.git"
    commands = [
        ["git", "clone", "--depth", "1", "--branch", branch, clone_url, str(repository_path)],
        ["git", "checkout", branch],
    ]

    try:
        clone_result = command_runner.run(commands[0], cwd=workspace_root)
        result.commands.append(clone_result)
        if not clone_result.passed:
            result.reason = f"Workspace clone failed for branch {branch}."
            _audit(
                "agent_worker.workspace_prepare_failed",
                task.id,
                {**_redacted_workspace_metadata(result), "reason": result.reason},
                actor=worker_id,
            )
            if cleanup_on_failure:
                cleanup_agent_worker_workspace(result)
            return result

        checkout_result = command_runner.run(commands[1], cwd=repository_path)
        result.commands.append(checkout_result)
        if not checkout_result.passed:
            result.reason = f"Workspace checkout failed for branch {branch}."
            _audit(
                "agent_worker.workspace_prepare_failed",
                task.id,
                {**_redacted_workspace_metadata(result), "reason": result.reason},
                actor=worker_id,
            )
            if cleanup_on_failure:
                cleanup_agent_worker_workspace(result)
            return result
    except Exception as error:  # noqa: BLE001 - return controlled worker failure.
        result.reason = str(error)
        _audit(
            "agent_worker.workspace_prepare_failed",
            task.id,
            {**_redacted_workspace_metadata(result), "reason": result.reason},
            actor=worker_id,
        )
        if cleanup_on_failure:
            cleanup_agent_worker_workspace(result)
        return result

    result.success = True
    result.reason = "Isolated workspace prepared and target branch checked out."
    _audit(
        "agent_worker.workspace_prepared",
        task.id,
        _redacted_workspace_metadata(result),
        actor=worker_id,
    )
    return result
