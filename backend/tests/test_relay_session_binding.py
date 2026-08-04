from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")
os.environ.setdefault("AIXION_PROFILE", "test")
os.environ.setdefault("AIXION_LEASE_SIGNING_KEY", "relay-session-binding-test-key")

import pytest

from app.agent_run_models import AgentRunCreate, AgentRunStatus
from app.agent_run_supervisor import create_agent_run
from app.agent_task_models import AgentTask, AgentTaskStatus
from app.models import (
    AgentProvider,
    ApprovalRequest,
    ApprovalStatus,
    AuthUser,
    FileChange,
    Project,
    RiskAssessment,
    RiskLevel,
    UserRole,
)
from app.relay_models import RelayProvider, RelaySessionCreate
from app.relay_service import RelayConflict
from app.relay_session_binding import bind_relay_session_to_run
from app.store import store


def setup_function() -> None:
    store.reset()


def _owner() -> AuthUser:
    return AuthUser(
        id="relay-owner",
        email="relay-owner@example.com",
        display_name="Relay Owner",
        role=UserRole.OWNER,
        email_verified=True,
    )


def _approved_run() -> tuple[AgentTask, object]:
    project = Project(name="Relay session binding", description="test")
    store.projects[project.id] = project
    approval = ApprovalRequest(
        project_id=project.id,
        title="Approved provider session",
        summary="Modify one exact file and run one exact validation command.",
        agent_name="codex",
        target_branch="feature/relay-session-binding",
        files=[
            FileChange(
                path="backend/app/relay_bound.py",
                change_type="create",
                diff="+relay_bound",
                new_content="relay_bound = True\n",
            )
        ],
        test_plan=["python -m pytest backend/tests/test_relay_bound.py"],
        rollback_plan="Close the feature pull request.",
        risk=RiskAssessment(level=RiskLevel.HIGH),
        status=ApprovalStatus.APPROVED,
        created_by_user_id="approval-creator",
        approved_by_user_id="approval-reviewer",
        approved_payload_hash="sealed-relay-session-binding-payload",
    )
    store.approval_requests[approval.id] = approval
    task = AgentTask(
        provider=AgentProvider.CODEX,
        project_id=project.id,
        title="Bound provider task",
        goal="Execute the approved provider session.",
        repository="owner/repo",
        branch_preference="feature/relay-session-binding",
        approval_request_id=approval.id,
        status=AgentTaskStatus.APPROVED,
    )
    store.agent_tasks[task.id] = task
    run = create_agent_run(
        AgentRunCreate(task_id=task.id),
        actor="relay-owner@example.com",
    )
    return task, run


def _payload(run_id: str | None = None) -> RelaySessionCreate:
    return RelaySessionCreate(
        relay_id="relay-test",
        provider=RelayProvider.CODEX,
        adapter_id="codex-app-server",
        objective="Execute the approved provider session.",
        workspace_path="/Users/test/work/repo",
        repository="owner/repo",
        run_id=run_id,
    )


def test_trust_enforcement_requires_approved_agent_run(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_TRUST_ENFORCEMENT", "true")

    with pytest.raises(RelayConflict, match="require an approved durable AgentRun"):
        bind_relay_session_to_run(_payload(), user=_owner())


def test_bound_session_derives_task_project_repository_and_capability(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_TRUST_ENFORCEMENT", "true")
    task, run = _approved_run()

    bound = bind_relay_session_to_run(_payload(run.id), user=_owner())

    assert bound.run_id == run.id
    assert bound.task_id == task.id
    assert bound.project_id == task.project_id
    assert bound.repository == task.repository
    assert run.capability_lease_id is not None
    assert bound.metadata["bound_capability_lease_id"] == run.capability_lease_id
    assert bound.metadata["bound_agent_run_id"] == run.id


def test_terminal_agent_run_cannot_start_provider_session(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_TRUST_ENFORCEMENT", "true")
    _, run = _approved_run()
    run.status = AgentRunStatus.FAILED

    with pytest.raises(RelayConflict, match="Terminal AgentRun"):
        bind_relay_session_to_run(_payload(run.id), user=_owner())


def test_provider_must_match_agent_task_provider(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_TRUST_ENFORCEMENT", "true")
    _, run = _approved_run()
    payload = _payload(run.id).model_copy(update={"provider": RelayProvider.CLAUDE})

    with pytest.raises(RelayConflict, match="conflicts with AgentTask provider"):
        bind_relay_session_to_run(payload, user=_owner())


def test_non_enforced_profile_allows_unbound_observational_session(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_TRUST_ENFORCEMENT", "false")
    payload = _payload()

    assert bind_relay_session_to_run(payload, user=_owner()) is payload
