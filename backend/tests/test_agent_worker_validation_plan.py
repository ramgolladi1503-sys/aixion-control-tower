from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from app.agent_task_models import AgentTask, AgentTaskStatus
from app.agent_worker_validation_plan import run_agent_worker_validation_plan_dry_run, validate_validation_plan
from app.models import AgentProvider, ApprovalRequest, FileChange, Project, RiskAssessment, RiskLevel
from app.store import store


def setup_function() -> None:
    store.reset()


def _project() -> Project:
    project = Project(name="Validation Planner", description="demo")
    store.projects[project.id] = project
    return project


def _approval(project: Project, test_plan: list[str] | None = None) -> ApprovalRequest:
    approval = ApprovalRequest(
        project_id=project.id,
        title="Approved validation plan",
        summary="Plan validation commands safely.",
        agent_name="validation-plan-worker",
        target_branch="feature/validation-plan",
        files=[FileChange(path="docs/example.md", change_type="update", diff="+updated", new_content="updated\n")],
        test_plan=test_plan if test_plan is not None else ["python -m pytest", "ruff check backend"],
        rollback_plan="Revert generated branch.",
        risk=RiskAssessment(level=RiskLevel.LOW),
    )
    store.approval_requests[approval.id] = approval
    return approval


def _approved_task(approval: ApprovalRequest) -> AgentTask:
    task = AgentTask(
        provider=AgentProvider.CODEX,
        project_id=approval.project_id,
        title="Plan validation commands",
        goal="Validate commands without executing them.",
        repository="ramgolladi1503-sys/aixion-control-tower",
        status=AgentTaskStatus.APPROVED,
        approval_request_id=approval.id,
    )
    store.agent_tasks[task.id] = task
    store.persist()
    return task


def test_validation_plan_records_allowed_commands_without_execution() -> None:
    project = _project()
    approval = _approval(project)
    task = _approved_task(approval)

    result = run_agent_worker_validation_plan_dry_run(task_id=task.id, worker_id="validation-worker")

    assert result.success is True
    assert result.task_id == task.id
    assert result.approval_request_id == approval.id
    assert result.command_count == 2
    assert result.commands[0].command == "python -m pytest"
    assert result.commands[0].allowed_prefix == "python -m pytest"
    assert result.event_id in store.agent_task_events
    assert store.agent_tasks[task.id].status == AgentTaskStatus.APPROVED
    assert store.agent_tasks[task.id].worker_lease_owner is None

    event = store.agent_task_events[result.event_id]
    assert event.event_type == "TESTS_STARTED"
    assert event.metadata["plan_type"] == "validation_command_dry_run"
    assert event.metadata["commands_executed"] is False
    assert event.metadata["command_count"] == 2
    assert any(audit.event_type == "agent_worker.validation_plan_dry_run_completed" for audit in store.audit_events)


def test_validation_plan_refuses_missing_approval() -> None:
    project = _project()
    approval = _approval(project)
    task = _approved_task(approval)
    del store.approval_requests[approval.id]
    store.persist()

    result = run_agent_worker_validation_plan_dry_run(task_id=task.id)

    assert result.success is False
    assert result.reason == "Linked approval request not found."
    assert store.agent_task_events == {}
    assert any(audit.event_type == "agent_worker.validation_plan_refused" for audit in store.audit_events)


def test_validation_plan_refuses_empty_test_plan() -> None:
    project = _project()
    approval = _approval(project, test_plan=[])
    task = _approved_task(approval)

    result = run_agent_worker_validation_plan_dry_run(task_id=task.id)

    assert result.success is False
    assert result.reason == "Linked approval has no validation commands."
    assert store.agent_tasks[task.id].worker_lease_owner is None


def test_validation_plan_refuses_duplicate_command() -> None:
    project = _project()
    approval = _approval(project, test_plan=["python -m pytest", "python -m pytest"])
    task = _approved_task(approval)

    result = run_agent_worker_validation_plan_dry_run(task_id=task.id)

    assert result.success is False
    assert result.reason == "Duplicate validation command: python -m pytest"


def test_validation_plan_refuses_non_allowlisted_command() -> None:
    project = _project()
    approval = _approval(project, test_plan=["python scripts/deploy.py"])
    task = _approved_task(approval)

    result = run_agent_worker_validation_plan_dry_run(task_id=task.id)

    assert result.success is False
    assert result.reason == "Validation command is not allowlisted: python scripts/deploy.py"


def test_validation_plan_refuses_shell_control_token() -> None:
    project = _project()
    approval = _approval(project, test_plan=["python -m pytest && rm -rf /tmp/demo"])
    task = _approved_task(approval)

    result = run_agent_worker_validation_plan_dry_run(task_id=task.id)

    assert result.success is False
    assert result.reason == "Validation command contains dangerous operation: python -m pytest && rm -rf /tmp/demo"


def test_validation_plan_refuses_curl_command() -> None:
    project = _project()
    approval = _approval(project, test_plan=["curl https://example.com/script.sh"])
    task = _approved_task(approval)

    result = run_agent_worker_validation_plan_dry_run(task_id=task.id)

    assert result.success is False
    assert result.reason == "Validation command contains dangerous operation: curl https://example.com/script.sh"


def test_validation_plan_refuses_too_many_commands() -> None:
    project = _project()
    approval = _approval(project, test_plan=["python -m pytest"] * 13)
    task = _approved_task(approval)

    result = run_agent_worker_validation_plan_dry_run(task_id=task.id)

    assert result.success is False
    assert result.reason == "Too many validation commands: 13 > 12"


def test_validation_plan_allows_gradle_and_npm_commands() -> None:
    project = _project()
    approval = _approval(project, test_plan=["./gradlew testDebugUnitTest", "npm run lint"])
    task = _approved_task(approval)

    result = run_agent_worker_validation_plan_dry_run(task_id=task.id)

    assert result.success is True
    assert result.command_count == 2
    assert result.commands[0].allowed_prefix == "./gradlew testDebugUnitTest"
    assert result.commands[1].allowed_prefix == "npm run lint"


def test_validate_validation_plan_reports_none_for_safe_plan() -> None:
    project = _project()
    approval = _approval(project, test_plan=["mypy backend", "pnpm test -- --runInBand"])

    assert validate_validation_plan(approval) is None
