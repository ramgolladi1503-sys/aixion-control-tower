from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from app.agent_task_models import AgentTask, AgentTaskStatus
from app.agent_worker_file_plan import run_agent_worker_file_plan_dry_run, validate_file_patch_plan
from app.models import AgentProvider, ApprovalRequest, FileChange, Project, RiskAssessment, RiskLevel
from app.store import store


def setup_function() -> None:
    store.reset()


def _project() -> Project:
    project = Project(name="File Planner", description="demo")
    store.projects[project.id] = project
    return project


def _approval(project: Project, files: list[FileChange] | None = None) -> ApprovalRequest:
    approval = ApprovalRequest(
        project_id=project.id,
        title="Approved file patch plan",
        summary="Plan file patches safely.",
        agent_name="file-plan-worker",
        target_branch="feature/file-plan",
        files=files
        if files is not None
        else [
            FileChange(
                path="docs/example.md",
                change_type="update",
                diff="+updated",
                new_content="updated content\n",
            )
        ],
        test_plan=["python -m pytest"],
        rollback_plan="Revert generated branch.",
        risk=RiskAssessment(level=RiskLevel.LOW),
    )
    store.approval_requests[approval.id] = approval
    return approval


def _approved_task(approval: ApprovalRequest) -> AgentTask:
    task = AgentTask(
        provider=AgentProvider.CODEX,
        project_id=approval.project_id,
        title="Plan file patches",
        goal="Validate file patch plan without writing files.",
        repository="ramgolladi1503-sys/aixion-control-tower",
        status=AgentTaskStatus.APPROVED,
        approval_request_id=approval.id,
    )
    store.agent_tasks[task.id] = task
    store.persist()
    return task


def test_file_plan_records_safe_file_patch_evidence_without_mutation() -> None:
    project = _project()
    approval = _approval(project)
    task = _approved_task(approval)

    result = run_agent_worker_file_plan_dry_run(task_id=task.id, worker_id="file-worker")

    assert result.success is True
    assert result.task_id == task.id
    assert result.approval_request_id == approval.id
    assert result.file_count == 1
    assert result.files[0].path == "docs/example.md"
    assert result.files[0].change_type == "update"
    assert result.files[0].has_new_content is True
    assert result.event_id in store.agent_task_events
    assert store.agent_tasks[task.id].status == AgentTaskStatus.APPROVED
    assert store.agent_tasks[task.id].worker_lease_owner is None

    event = store.agent_task_events[result.event_id]
    assert event.event_type == "NOTE"
    assert event.metadata["plan_type"] == "file_patch_dry_run"
    assert event.metadata["repository_mutated"] is False
    assert event.metadata["files_written"] is False
    assert event.metadata["file_count"] == 1
    assert any(audit.event_type == "agent_worker.file_plan_dry_run_completed" for audit in store.audit_events)


def test_file_plan_allows_delete_without_new_content() -> None:
    project = _project()
    approval = _approval(
        project,
        files=[FileChange(path="docs/old.md", change_type="delete", diff="-old", new_content=None)],
    )
    task = _approved_task(approval)

    result = run_agent_worker_file_plan_dry_run(task_id=task.id)

    assert result.success is True
    assert result.files[0].change_type == "delete"
    assert result.files[0].has_new_content is False


def test_file_plan_refuses_missing_approval() -> None:
    project = _project()
    approval = _approval(project)
    task = _approved_task(approval)
    del store.approval_requests[approval.id]
    store.persist()

    result = run_agent_worker_file_plan_dry_run(task_id=task.id)

    assert result.success is False
    assert result.reason == "Linked approval request not found."
    assert store.agent_task_events == {}
    assert any(audit.event_type == "agent_worker.file_plan_refused" for audit in store.audit_events)


def test_file_plan_refuses_missing_new_content_for_update() -> None:
    project = _project()
    approval = _approval(
        project,
        files=[FileChange(path="docs/example.md", change_type="update", diff="+updated", new_content=None)],
    )
    task = _approved_task(approval)

    result = run_agent_worker_file_plan_dry_run(task_id=task.id)

    assert result.success is False
    assert result.reason == "File change requires new_content for docs/example.md"
    assert store.agent_tasks[task.id].worker_lease_owner is None


def test_file_plan_refuses_delete_with_new_content() -> None:
    project = _project()
    approval = _approval(
        project,
        files=[FileChange(path="docs/old.md", change_type="delete", diff="-old", new_content="bad")],
    )
    task = _approved_task(approval)

    result = run_agent_worker_file_plan_dry_run(task_id=task.id)

    assert result.success is False
    assert result.reason == "Delete change must not include new_content for docs/old.md"


def test_file_plan_refuses_path_traversal() -> None:
    project = _project()
    approval = _approval(
        project,
        files=[FileChange(path="../secrets.txt", change_type="create", diff="+secret", new_content="secret")],
    )
    task = _approved_task(approval)

    result = run_agent_worker_file_plan_dry_run(task_id=task.id)

    assert result.success is False
    assert result.reason == "Unsafe file path: ../secrets.txt"


def test_file_plan_refuses_env_file() -> None:
    project = _project()
    approval = _approval(
        project,
        files=[FileChange(path=".env", change_type="create", diff="+secret", new_content="TOKEN=bad")],
    )
    task = _approved_task(approval)

    result = run_agent_worker_file_plan_dry_run(task_id=task.id)

    assert result.success is False
    assert result.reason == "Unsafe file path: .env"


def test_file_plan_refuses_github_workflow_path() -> None:
    project = _project()
    approval = _approval(
        project,
        files=[
            FileChange(
                path=".github/workflows/unsafe.yml",
                change_type="create",
                diff="+workflow",
                new_content="name: unsafe\n",
            )
        ],
    )
    task = _approved_task(approval)

    result = run_agent_worker_file_plan_dry_run(task_id=task.id)

    assert result.success is False
    assert result.reason == "Unsafe file path: .github/workflows/unsafe.yml"


def test_file_plan_refuses_duplicate_paths() -> None:
    project = _project()
    approval = _approval(
        project,
        files=[
            FileChange(path="docs/example.md", change_type="update", diff="+a", new_content="a"),
            FileChange(path="docs/example.md", change_type="update", diff="+b", new_content="b"),
        ],
    )
    task = _approved_task(approval)

    result = run_agent_worker_file_plan_dry_run(task_id=task.id)

    assert result.success is False
    assert result.reason == "Duplicate file change path: docs/example.md"


def test_validate_file_patch_plan_refuses_empty_files() -> None:
    project = _project()
    approval = _approval(project, files=[])

    assert validate_file_patch_plan(approval) == "Linked approval has no file changes."
