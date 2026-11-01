from __future__ import annotations

import os
import pytest

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from app.agent_task_models import AgentTask, AgentTaskStatus
from app.agent_worker_branch_plan import run_agent_worker_branch_plan_dry_run, validate_branch_plan
from app import agent_worker_branch_plan as branch_plan_module
from app.models import ApprovalRequest, ApprovalStatus, AgentProvider, Project, RiskAssessment, RiskLevel
from app.store import store


def setup_function() -> None:
    store.reset()


def _approved_task(
    *,
    repository: str | None = "ramgolladi1503-sys/aixion-control-tower",
    branch_preference: str | None = None,
    title: str = "Build worker branch planner",
) -> AgentTask:
    project = Project(name="Branch Planner", description="demo", workspace_id="workspace-worker-test")
    store.projects[project.id] = project
    approval = ApprovalRequest(
        project_id=project.id,
        title=f"{title} authorization",
        summary="Approval required before worker claims this task.",
        target_branch="feature/branch-planner",
        files=[],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.APPROVED,
    )
    store.approval_requests[approval.id] = approval
    task = AgentTask(
        provider=AgentProvider.CODEX,
        project_id=project.id,
        title=title,
        goal="Plan a safe branch without creating it.",
        repository=repository,
        branch_preference=branch_preference,
        status=AgentTaskStatus.APPROVED,
        approval_request_id=approval.id,
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


@pytest.mark.parametrize("settings_values", [{"is_production": True}, {"postgres_auth_enabled": True}])
def test_branch_plan_fails_closed_before_claim_or_mutation(monkeypatch, settings_values) -> None:
    from types import SimpleNamespace

    task = _approved_task()
    before_events = dict(store.agent_task_events)
    before_audits = list(store.audit_events)
    before_task = task.model_copy(deep=True)
    monkeypatch.setattr(
        branch_plan_module,
        "get_settings",
        lambda: SimpleNamespace(
            is_production=settings_values.get("is_production", False),
            postgres_auth_enabled=settings_values.get("postgres_auth_enabled", False),
        ),
    )
    monkeypatch.setattr(branch_plan_module, "claim_agent_task_for_worker", lambda **_: pytest.fail("claim must not run"))
    monkeypatch.setattr(branch_plan_module, "claim_first_approved_agent_task_for_worker", lambda **_: pytest.fail("claim must not run"))
    monkeypatch.setattr(store, "persist", lambda: pytest.fail("persist must not run"))

    result = run_agent_worker_branch_plan_dry_run(task_id=task.id)

    assert not result.success
    assert "unavailable in production or PostgreSQL auth mode" in result.reason
    assert store.agent_tasks[task.id] == before_task
    assert store.agent_task_events == before_events
    assert store.audit_events == before_audits


def test_first_approved_branch_plan_uses_available_task() -> None:
    task = _approved_task(branch_preference="feature/first-approved-plan")

    result = run_agent_worker_branch_plan_dry_run(first_approved=True)

    assert result.success is True
    assert result.task_id == task.id
    assert result.planned_branch == "feature/first-approved-plan"
