from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from app.agent_task_models import AgentTask, AgentTaskStatus
from app.agent_worker_validation_runner import CommandExecutionResult, run_agent_worker_validation_commands
from app.models import AgentProvider, ApprovalRequest, FileChange, Project, RiskAssessment, RiskLevel
from app.store import store


def setup_function() -> None:
    store.reset()


def _project() -> Project:
    project = Project(name="Validation Runner", description="demo")
    store.projects[project.id] = project
    return project


def _approval(project: Project, test_plan: list[str] | None = None) -> ApprovalRequest:
    approval = ApprovalRequest(
        project_id=project.id,
        title="Approved validation run",
        summary="Run validation commands safely.",
        agent_name="validation-runner",
        target_branch="feature/validation-runner",
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
        title="Run validation commands",
        goal="Execute allowlisted validation commands.",
        repository="ramgolladi1503-sys/aixion-control-tower",
        status=AgentTaskStatus.APPROVED,
        approval_request_id=approval.id,
    )
    store.agent_tasks[task.id] = task
    store.persist()
    return task


def test_validation_runner_records_passed_commands() -> None:
    project = _project()
    approval = _approval(project)
    task = _approved_task(approval)
    calls: list[str] = []

    def fake_executor(command: str) -> CommandExecutionResult:
        calls.append(command)
        return CommandExecutionResult(command=command, exit_code=0, output_summary=f"passed {command}", duration_ms=7)

    result = run_agent_worker_validation_commands(task_id=task.id, worker_id="runner", executor=fake_executor)

    assert result.success is True
    assert result.task_id == task.id
    assert result.approval_request_id == approval.id
    assert result.command_count == 2
    assert calls == ["python -m pytest", "ruff check backend"]
    assert result.started_event_id in store.agent_task_events
    assert result.finished_event_id in store.agent_task_events
    assert store.agent_tasks[task.id].status == AgentTaskStatus.APPROVED
    assert store.agent_tasks[task.id].worker_lease_owner is None

    started = store.agent_task_events[result.started_event_id]
    finished = store.agent_task_events[result.finished_event_id]
    assert started.event_type == "TESTS_STARTED"
    assert started.status == AgentTaskStatus.TESTING
    assert started.metadata["validation_policy"]["policy_version"] == "aixion-validation-execution-policy-v1"
    assert finished.event_type == "TESTS_PASSED"
    assert finished.status == AgentTaskStatus.APPROVED
    assert finished.metadata["commands_executed"] is True
    assert finished.metadata["results"][0]["output_summary"] == "passed python -m pytest"
    assert finished.metadata["results"][0]["allowed_prefix"] == "python -m pytest"
    assert finished.metadata["results"][0]["duration_ms"] == 7
    assert finished.metadata["artifact_summary"]["passed_count"] == 2
    assert finished.metadata["artifact_summary"]["failed_count"] == 0
    assert finished.metadata["artifact_summary"]["total_duration_ms"] == 14
    assert any(audit.event_type == "agent_worker.validation_run_started" for audit in store.audit_events)
    assert any(audit.event_type == "agent_worker.validation_run_completed" for audit in store.audit_events)


def test_validation_runner_stops_on_first_failure_and_marks_task_failed() -> None:
    project = _project()
    approval = _approval(project, test_plan=["python -m pytest", "ruff check backend"])
    task = _approved_task(approval)
    calls: list[str] = []

    def fake_executor(command: str) -> CommandExecutionResult:
        calls.append(command)
        return CommandExecutionResult(command=command, exit_code=1, output_summary="failed", duration_ms=5)

    result = run_agent_worker_validation_commands(task_id=task.id, worker_id="runner", executor=fake_executor)

    assert result.success is False
    assert result.reason == "Validation commands failed."
    assert result.command_count == 1
    assert calls == ["python -m pytest"]
    assert store.agent_tasks[task.id].status == AgentTaskStatus.FAILED
    assert store.agent_tasks[task.id].worker_lease_owner is None
    finished = store.agent_task_events[result.finished_event_id]
    assert finished.event_type == "TESTS_FAILED"
    assert finished.status == AgentTaskStatus.FAILED
    assert finished.metadata["results"][0]["exit_code"] == 1
    assert finished.metadata["artifact_summary"]["failed_count"] == 1


def test_validation_runner_records_timeout_failure() -> None:
    project = _project()
    approval = _approval(project, test_plan=["python -m pytest"])
    task = _approved_task(approval)

    def fake_executor(command: str) -> CommandExecutionResult:
        return CommandExecutionResult(command=command, exit_code=124, timed_out=True, output_summary="timed out", duration_ms=120000)

    result = run_agent_worker_validation_commands(task_id=task.id, worker_id="runner", executor=fake_executor)

    assert result.success is False
    assert result.results[0].timed_out is True
    assert store.agent_tasks[task.id].status == AgentTaskStatus.FAILED
    finished = store.agent_task_events[result.finished_event_id]
    assert finished.metadata["artifact_summary"]["timed_out_count"] == 1


def test_validation_runner_truncates_large_output_artifact() -> None:
    project = _project()
    approval = _approval(project, test_plan=["python -m pytest"])
    task = _approved_task(approval)
    huge_output = "x" * 5000

    def fake_executor(command: str) -> CommandExecutionResult:
        return CommandExecutionResult(command=command, exit_code=0, output_summary=huge_output)

    result = run_agent_worker_validation_commands(task_id=task.id, worker_id="runner", executor=fake_executor)

    assert result.success is True
    finished = store.agent_task_events[result.finished_event_id]
    assert len(finished.metadata["results"][0]["output_summary"]) == 4000
    assert finished.metadata["results"][0]["output_truncated"] is True
    assert finished.metadata["artifact_summary"]["output_truncated"] is True


def test_validation_runner_refuses_non_allowlisted_command_before_execution() -> None:
    project = _project()
    approval = _approval(project, test_plan=["python scripts/deploy.py"])
    task = _approved_task(approval)
    calls: list[str] = []

    def fake_executor(command: str) -> CommandExecutionResult:
        calls.append(command)
        return CommandExecutionResult(command=command, exit_code=0)

    result = run_agent_worker_validation_commands(task_id=task.id, executor=fake_executor)

    assert result.success is False
    assert result.reason == "Validation command is not allowlisted: python scripts/deploy.py"
    assert calls == []
    assert store.agent_task_events == {}
    assert store.agent_tasks[task.id].status == AgentTaskStatus.APPROVED
    assert store.agent_tasks[task.id].worker_lease_owner is None
    assert any(audit.event_type == "agent_worker.validation_run_refused" for audit in store.audit_events)
    refusal = next(audit for audit in store.audit_events if audit.event_type == "agent_worker.validation_run_refused")
    assert refusal.details["validation_policy"]["shell_execution_allowed"] is False


def test_validation_runner_refuses_missing_approval() -> None:
    project = _project()
    approval = _approval(project)
    task = _approved_task(approval)
    del store.approval_requests[approval.id]
    store.persist()

    result = run_agent_worker_validation_commands(task_id=task.id)

    assert result.success is False
    assert result.reason == "Linked approval request not found."
    assert store.agent_task_events == {}
    assert store.agent_tasks[task.id].worker_lease_owner is None


def test_validation_runner_requires_task_selector() -> None:
    result = run_agent_worker_validation_commands()

    assert result.success is False
    assert result.reason == "Provide task_id or set first_approved=true."
