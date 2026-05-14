from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from app.agent_task_models import AgentTask, AgentTaskStatus
from app.agent_worker_branch_plan import run_agent_worker_branch_plan_dry_run, validate_branch_plan
from app.models import AgentProvider, Project
from app.store import store


def setup_function() -> None:
    store.reset()


def _approved_task(
    *,
    repository: str | None = "ramgolladi1503-sys/aixion-control-tower",
    branch_preference: str | None = None,
    title: str = "Build worker branch planner",
) -> AgentTask:
    project = Project(name="Branch Planner", description="demo")
    store.projects[project.id] = project
    task = AgentTask(
        provider=AgentProvider.CODEX,
        project_id=project.id,
        title=title,
        goal="Plan a safe branch without creating it.",
        repository=repository,
        branch_preference=branch_preference,
        status=AgentTaskStatus.APPROVED,
        approval_request_id="approval_branch_plan",
    )
    store.agent_tasks[task.id] = task
    store.persist()
    return task


def test_branch_plan_generates_safe_feature_branch_without_mutating_repository() -> None:
    task = _approved_task()

    result = run_agent_worker_branch_plan_dry_run(task_id=task.id, worker_id="branch-worker")

    assert result.success is True
    assert result.task_id == task.id
    assert result.repository == "ramgolladi1503-sys/aixion-control-tower"
    assert result.planned_branch is not None
    assert result.planned_branch.startswith("feature/agent-task-")
    assert result.event_id in store.agent_task_events
    assert result.final_status == AgentTaskStatus.APPROVED
    assert store.agent_tasks[task.id].status == AgentTaskStatus.APPROVED
    assert store.agent_tasks[task.id].worker_lease_owner is None

    event = store.agent_task_events[result.event_id]
    assert event.event_type == "NOTE"
    assert event.metadata["branch_created"] is False
    assert event.metadata["repository_mutated"] is False
    assert event.metadata["planned_branch"] == result.planned_branch
    assert any(audit.event_type == "agent_worker.branch_plan_dry_run_completed" for audit in store.audit_events)


def test_branch_plan_accepts_safe_branch_preference() -> None:
    task = _approved_task(branch_preference="feature/custom-safe-branch")

    result = run_agent_worker_branch_plan_dry_run(task_id=task.id)

    assert result.success is True
    assert result.planned_branch == "feature/custom-safe-branch"


def test_branch_plan_refuses_missing_repository() -> None:
    task = _approved_task(repository=None)

    result = run_agent_worker_branch_plan_dry_run(task_id=task.id)

    assert result.success is False
    assert result.reason == "Agent task is missing repository."
    assert result.planned_branch is not None
    assert store.agent_tasks[task.id].status == AgentTaskStatus.APPROVED
    assert store.agent_tasks[task.id].worker_lease_owner is None
    assert store.agent_task_events == {}
    assert any(audit.event_type == "agent_worker.branch_plan_refused" for audit in store.audit_events)


def test_branch_plan_refuses_invalid_repository_format() -> None:
    task = _approved_task(repository="not-a-full-repo")

    result = run_agent_worker_branch_plan_dry_run(task_id=task.id)

    assert result.success is False
    assert result.reason == "Agent task repository is not in owner/repo format: not-a-full-repo"


def test_branch_plan_refuses_protected_branch_preference() -> None:
    task = _approved_task(branch_preference="main")

    result = run_agent_worker_branch_plan_dry_run(task_id=task.id)

    assert result.success is False
    assert result.reason == "Planned branch is unsafe or protected: main"
    assert store.agent_tasks[task.id].worker_lease_owner is None


def test_branch_plan_refuses_unsafe_branch_prefix() -> None:
    task = _approved_task(branch_preference="hotfix/direct-prod")

    result = run_agent_worker_branch_plan_dry_run(task_id=task.id)

    assert result.success is False
    assert result.reason == "Planned branch is unsafe or protected: hotfix/direct-prod"


def test_branch_plan_refuses_path_traversal_branch() -> None:
    task = _approved_task(branch_preference="feature/../main")

    result = run_agent_worker_branch_plan_dry_run(task_id=task.id)

    assert result.success is False
    assert result.reason == "Planned branch is unsafe or protected: feature/../main"


def test_validate_branch_plan_reports_none_for_safe_task() -> None:
    task = _approved_task(branch_preference="fix/safe-branch")

    assert validate_branch_plan(task) is None


def test_first_approved_branch_plan_uses_available_task() -> None:
    task = _approved_task(branch_preference="feature/first-approved-plan")

    result = run_agent_worker_branch_plan_dry_run(first_approved=True)

    assert result.success is True
    assert result.task_id == task.id
    assert result.planned_branch == "feature/first-approved-plan"
}
