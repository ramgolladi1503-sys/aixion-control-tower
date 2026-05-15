from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from app.agent_task_models import AgentTask, AgentTaskStatus
from app.agent_worker_container_plan import (
    ContainerExecutionPolicy,
    run_agent_worker_container_execution_plan_dry_run,
    validate_container_execution_policy,
)
from app.models import AgentProvider, ApprovalRequest, FileChange, Project, RiskAssessment, RiskLevel
from app.store import store


def setup_function() -> None:
    store.reset()


def _project() -> Project:
    project = Project(name="Container Plan", description="demo")
    store.projects[project.id] = project
    return project


def _approval(project: Project) -> ApprovalRequest:
    approval = ApprovalRequest(
        project_id=project.id,
        title="Approved container plan",
        summary="Plan future container execution.",
        agent_name="container-plan-worker",
        target_branch="feature/container-plan",
        files=[FileChange(path="docs/example.md", change_type="update", diff="+updated", new_content="updated\n")],
        test_plan=["python -m pytest"],
        rollback_plan="Close PR.",
        risk=RiskAssessment(level=RiskLevel.LOW),
    )
    store.approval_requests[approval.id] = approval
    return approval


def _approved_task(approval: ApprovalRequest) -> AgentTask:
    task = AgentTask(
        provider=AgentProvider.CODEX,
        project_id=approval.project_id,
        title="Plan container execution",
        goal="Create a safe container execution plan.",
        repository="ramgolladi1503-sys/aixion-control-tower",
        status=AgentTaskStatus.APPROVED,
        approval_request_id=approval.id,
    )
    store.agent_tasks[task.id] = task
    store.persist()
    return task


def test_container_plan_records_safe_policy_without_starting_container() -> None:
    project = _project()
    approval = _approval(project)
    task = _approved_task(approval)

    result = run_agent_worker_container_execution_plan_dry_run(task_id=task.id, worker_id="container-plan")

    assert result.success is True
    assert result.task_id == task.id
    assert result.event_id in store.agent_task_events
    assert store.agent_tasks[task.id].status == AgentTaskStatus.APPROVED
    assert store.agent_tasks[task.id].worker_lease_owner is None
    event = store.agent_task_events[result.event_id]
    assert event.event_type == "NOTE"
    assert event.metadata["dry_run"] is True
    assert event.metadata["container_started"] is False
    assert event.metadata["container_policy"]["policy_version"] == "aixion-container-execution-policy-v1"
    assert event.metadata["container_policy"]["network_mode"] == "none"
    assert any(audit.event_type == "agent_worker.container_execution_plan_completed" for audit in store.audit_events)


def test_container_policy_rejects_unsafe_image() -> None:
    policy = ContainerExecutionPolicy(image="ubuntu:latest")

    assert validate_container_execution_policy(policy) == "Container image is not allowlisted: ubuntu:latest"


def test_container_policy_rejects_host_network() -> None:
    policy = ContainerExecutionPolicy(network_mode="host")

    assert validate_container_execution_policy(policy) == "Container network mode is not allowlisted: host"


def test_container_policy_rejects_privileged_mode() -> None:
    policy = ContainerExecutionPolicy(privileged_allowed=True)

    assert validate_container_execution_policy(policy) == "Container policy must not allow privileged mode."


def test_container_policy_rejects_docker_socket_mount() -> None:
    policy = ContainerExecutionPolicy(docker_socket_mount_allowed=True)

    assert validate_container_execution_policy(policy) == "Container policy must not allow Docker socket mounts."


def test_container_plan_refuses_invalid_policy_before_claiming_task() -> None:
    project = _project()
    approval = _approval(project)
    task = _approved_task(approval)
    policy = ContainerExecutionPolicy(image="ubuntu:latest")

    result = run_agent_worker_container_execution_plan_dry_run(task_id=task.id, policy=policy)

    assert result.success is False
    assert result.reason == "Container image is not allowlisted: ubuntu:latest"
    assert store.agent_tasks[task.id].worker_lease_owner is None
    assert store.agent_task_events == {}


def test_container_plan_requires_task_selector() -> None:
    result = run_agent_worker_container_execution_plan_dry_run()

    assert result.success is False
    assert result.reason == "Provide task_id or set first_approved=true."
