from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from app import agent_worker_claims
from app.agent_task_models import AgentTask, AgentTaskStatus
from app.agent_worker_claims import claim_agent_task_for_worker, claim_first_approved_agent_task_for_worker
from app.models import (
    AgentProvider,
    ApprovalRequest,
    ApprovalStatus,
    FileChange,
    Project,
    RiskAssessment,
    RiskLevel,
)
from app.store import store


def setup_function() -> None:
    with store._connect() as conn:
        conn.execute("DROP TRIGGER IF EXISTS reject_worker_refusal_audit")
        conn.execute("DROP TRIGGER IF EXISTS reject_worker_lease")
        conn.execute("DROP TRIGGER IF EXISTS reject_worker_claim_audit")
    store.reset()


@pytest.mark.parametrize(
    "authoritative_mode",
    ["production", "postgres_auth", "postgres_runtime_probe"],
)
@pytest.mark.parametrize(
    "claim",
    [
        agent_worker_claims.claim_agent_task_for_worker,
        agent_worker_claims.claim_first_approved_agent_task_for_worker,
    ],
)
def test_worker_claims_fail_closed_without_postgres_repository(
    monkeypatch: pytest.MonkeyPatch,
    authoritative_mode: str,
    claim,
) -> None:
    monkeypatch.setattr(
        agent_worker_claims,
        "get_settings",
        lambda: SimpleNamespace(
            is_production=authoritative_mode == "production",
            postgres_auth_enabled=authoritative_mode == "postgres_auth",
            postgres_runtime_probe_enabled=authoritative_mode == "postgres_runtime_probe",
        ),
    )

    with pytest.raises(RuntimeError, match="require a PostgreSQL task repository"):
        if claim is agent_worker_claims.claim_agent_task_for_worker:
            claim(task_id="must-not-claim-from-sqlite", worker_id="worker")
        else:
            claim(worker_id="worker")


def _approved_task(title: str = "Approved task") -> AgentTask:
    project = Project(name="Transactional Claim", description="demo", workspace_id="workspace-worker-test")
    store.projects[project.id] = project
    approval = ApprovalRequest(
        project_id=project.id,
        title=f"{title} authorization",
        summary="Approval required before worker claims this task.",
        target_branch="feature/worker-claim",
        files=[FileChange(path="README.md", diff="+approved")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.APPROVED,
    )
    store.approval_requests[approval.id] = approval
    task = AgentTask(
        provider=AgentProvider.CODEX,
        project_id=project.id,
        title=title,
        goal="Claim this task safely.",
        repository="ramgolladi1503-sys/aixion-control-tower",
        status=AgentTaskStatus.APPROVED,
        approval_request_id=approval.id,
    )
    store.agent_tasks[task.id] = task
    store.persist()
    return task


def test_claim_refuses_task_when_linked_approval_is_missing() -> None:
    task = _approved_task()
    store.approval_requests.pop(task.approval_request_id or "")
    store.persist()

    result = claim_agent_task_for_worker(task_id=task.id, worker_id="worker-a")

    assert result.success is False
    assert result.reason == "Linked approval request not found."
    assert store.agent_tasks[task.id].worker_lease_owner is None
    assert any(
        event.event_type == "agent_worker.task_claim_refused"
        and event.entity_id == task.id
        and event.workspace_id == "workspace-worker-test"
        for event in store.audit_events
    )


def test_refusal_audit_is_not_added_to_memory_when_sqlite_insert_fails() -> None:
    task = _approved_task()
    store.approval_requests.pop(task.approval_request_id or "")
    store.persist()
    with store._connect() as conn:
        conn.execute(
            "CREATE TRIGGER reject_worker_refusal_audit BEFORE INSERT ON kv_store "
            "WHEN NEW.entity_type = 'audit_event' "
            "BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END"
        )
    audit_ids_before = {event.id for event in store.audit_events}

    try:
        claim_agent_task_for_worker(task_id=task.id, worker_id="worker-a")
    except sqlite3.IntegrityError as exc:
        assert "audit unavailable" in str(exc)
    else:
        raise AssertionError("refusal audit failure must abort the claim transaction")

    assert {event.id for event in store.audit_events} == audit_ids_before
    store.load()
    assert not any(
        event.event_type == "agent_worker.task_claim_refused"
        and event.entity_id == task.id
        for event in store.audit_events
    )


def test_claim_refuses_task_when_linked_approval_is_denied() -> None:
    task = _approved_task()
    approval = store.approval_requests[task.approval_request_id or ""]
    approval.status = ApprovalStatus.DENIED
    store.persist()

    result = claim_agent_task_for_worker(task_id=task.id, worker_id="worker-a")

    assert result.success is False
    assert result.reason == "Linked approval is not approved: DENIED"
    assert store.agent_tasks[task.id].worker_lease_owner is None


def test_claim_refuses_task_when_approval_project_does_not_match() -> None:
    task = _approved_task()
    approval = store.approval_requests[task.approval_request_id or ""]
    approval.project_id = "another-project"
    store.persist()

    result = claim_agent_task_for_worker(task_id=task.id, worker_id="worker-a")

    assert result.success is False
    assert result.reason == "Linked approval project does not match the AgentTask project"
    assert store.agent_tasks[task.id].worker_lease_owner is None


def test_claim_refuses_task_when_approval_workspace_does_not_match() -> None:
    task = _approved_task()
    task.workspace_id = "workspace-task"
    approval = store.approval_requests[task.approval_request_id or ""]
    approval.workspace_id = "workspace-approval"
    store.persist()

    result = claim_agent_task_for_worker(task_id=task.id, worker_id="worker-a")

    assert result.success is False
    assert result.reason == "AgentTask workspace does not match its project workspace"
    assert store.agent_tasks[task.id].worker_lease_owner is None


def test_claim_refuses_when_approval_has_no_workspace_but_task_does() -> None:
    task = _approved_task()
    task.workspace_id = "workspace-task"
    store.approval_requests[task.approval_request_id or ""].workspace_id = None
    store.persist()

    result = claim_agent_task_for_worker(task_id=task.id, worker_id="worker-a")

    assert result.success is False
    assert result.reason == "AgentTask workspace does not match its project workspace"
    assert store.agent_tasks[task.id].worker_lease_owner is None


def test_claim_refuses_when_task_has_no_workspace_but_approval_does() -> None:
    task = _approved_task()
    task.workspace_id = "workspace-approval"
    approval = store.approval_requests[task.approval_request_id or ""]
    approval.workspace_id = "workspace-approval"
    task.workspace_id = None
    store.persist()

    result = claim_agent_task_for_worker(task_id=task.id, worker_id="worker-a")

    assert result.success is False
    assert result.reason == "Linked approval workspace does not match its project workspace"
    assert store.agent_tasks[task.id].worker_lease_owner is None


def test_claim_refuses_project_without_workspace() -> None:
    task = _approved_task()
    project = store.projects[task.project_id or ""]
    project.workspace_id = None
    store.approval_requests[task.approval_request_id or ""].workspace_id = None
    task.workspace_id = None
    store.persist()

    result = claim_agent_task_for_worker(task_id=task.id, worker_id="worker-a")

    assert result.success is False
    assert result.reason == "AgentTask project workspace is missing"
    assert store.agent_tasks[task.id].worker_lease_owner is None


def test_claim_refuses_task_workspace_that_disagrees_with_project() -> None:
    task = _approved_task()
    task.workspace_id = "workspace-other"
    store.persist()

    result = claim_agent_task_for_worker(task_id=task.id, worker_id="worker-a")

    assert result.success is False
    assert result.reason == "AgentTask workspace does not match its project workspace"
    assert store.agent_tasks[task.id].worker_lease_owner is None


def test_claim_refuses_approval_workspace_that_disagrees_with_project() -> None:
    task = _approved_task()
    store.approval_requests[task.approval_request_id or ""].workspace_id = "workspace-other"
    store.persist()

    result = claim_agent_task_for_worker(task_id=task.id, worker_id="worker-a")

    assert result.success is False
    assert result.reason == "Linked approval workspace does not match its project workspace"
    assert store.agent_tasks[task.id].worker_lease_owner is None


def test_claim_agent_task_sets_lease_fields_transactionally() -> None:
    task = _approved_task()

    result = claim_agent_task_for_worker(task_id=task.id, worker_id="worker-a", lease_seconds=120)

    assert result.success is True
    assert result.task_id == task.id
    assert result.lease_token is not None
    assert result.lease_expires_at is not None
    assert result.task is not None
    assert result.task.worker_lease_owner == "worker-a"
    assert result.task.worker_lease_token == result.lease_token
    assert store.agent_tasks[task.id].worker_lease_owner == "worker-a"
    assert store.agent_tasks[task.id].worker_lease_token == result.lease_token
    assert any(
        event.event_type == "agent_worker.task_claimed"
        and event.entity_id == task.id
        and event.workspace_id == "workspace-worker-test"
        for event in store.audit_events
    )

    store.load()
    persisted = store.agent_tasks[task.id]
    assert persisted.worker_lease_owner == "worker-a"
    assert persisted.worker_lease_token == result.lease_token


def test_failed_lease_persistence_does_not_update_in_memory_task() -> None:
    task = _approved_task()
    with store._connect() as conn:
        conn.execute(
            "CREATE TRIGGER reject_worker_lease BEFORE UPDATE ON kv_store "
            "WHEN OLD.entity_type = 'agent_task' "
            "BEGIN SELECT RAISE(ABORT, 'lease persistence unavailable'); END"
        )

    try:
        claim_agent_task_for_worker(task_id=task.id, worker_id="worker-a")
    except sqlite3.IntegrityError as exc:
        assert "lease persistence unavailable" in str(exc)
    else:
        raise AssertionError("lease persistence failure must abort the claim")

    assert store.agent_tasks[task.id].worker_lease_owner is None
    store.load()
    assert store.agent_tasks[task.id].worker_lease_owner is None


def test_failed_claim_audit_rolls_back_durable_lease() -> None:
    task = _approved_task()
    with store._connect() as conn:
        conn.execute(
            "CREATE TRIGGER reject_worker_claim_audit BEFORE INSERT ON kv_store "
            "WHEN NEW.entity_type = 'audit_event' "
            "BEGIN SELECT RAISE(ABORT, 'claim audit unavailable'); END"
        )

    try:
        claim_agent_task_for_worker(task_id=task.id, worker_id="worker-a")
    except sqlite3.IntegrityError as exc:
        assert "claim audit unavailable" in str(exc)
    else:
        raise AssertionError("claim audit failure must abort lease persistence")

    assert store.agent_tasks[task.id].worker_lease_owner is None
    store.load()
    assert store.agent_tasks[task.id].worker_lease_owner is None


def test_claim_agent_task_refuses_active_lease() -> None:
    task = _approved_task()
    task.worker_lease_owner = "worker-a"
    task.worker_lease_token = "lease_active"
    task.worker_lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    store.persist()

    result = claim_agent_task_for_worker(task_id=task.id, worker_id="worker-b")

    assert result.success is False
    assert "already leased by worker-a" in result.reason
    assert store.agent_tasks[task.id].worker_lease_owner == "worker-a"
    assert store.agent_tasks[task.id].worker_lease_token == "lease_active"


def test_claim_agent_task_takes_expired_lease() -> None:
    task = _approved_task()
    task.worker_lease_owner = "old-worker"
    task.worker_lease_token = "lease_expired"
    task.worker_lease_expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    store.persist()

    result = claim_agent_task_for_worker(task_id=task.id, worker_id="new-worker")

    assert result.success is True
    assert result.lease_token is not None
    assert result.lease_token != "lease_expired"
    assert store.agent_tasks[task.id].worker_lease_owner == "new-worker"
    assert store.agent_tasks[task.id].worker_lease_token == result.lease_token


def test_claim_agent_task_refuses_non_approved_task() -> None:
    task = _approved_task()
    task.status = AgentTaskStatus.WAITING_FOR_APPROVAL
    store.persist()

    result = claim_agent_task_for_worker(task_id=task.id, worker_id="worker-a")

    assert result.success is False
    assert result.reason == "Agent task is not approved: WAITING_FOR_APPROVAL"
    assert store.agent_tasks[task.id].worker_lease_owner is None


def test_claim_first_approved_skips_active_lease_and_claims_next_available() -> None:
    busy = _approved_task("Busy approved task")
    busy.worker_lease_owner = "busy-worker"
    busy.worker_lease_token = "lease_busy"
    busy.worker_lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    available = _approved_task("Available approved task")
    store.persist()

    result = claim_first_approved_agent_task_for_worker(worker_id="worker-a")

    assert result.success is True
    assert result.task_id == available.id
    assert store.agent_tasks[busy.id].worker_lease_owner == "busy-worker"
    assert store.agent_tasks[available.id].worker_lease_owner == "worker-a"


def test_claim_first_approved_reports_when_no_available_task_exists() -> None:
    result = claim_first_approved_agent_task_for_worker(worker_id="worker-a")

    assert result.success is False
    assert result.reason == "No approved, linked, lease-available agent task found."
