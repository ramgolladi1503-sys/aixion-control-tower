from __future__ import annotations

from datetime import timedelta
import uuid

import pytest
from fastapi.testclient import TestClient

from app import main
from app.models import (
    ApprovalRequest,
    ApprovalStatus,
    AuditEvent,
    FileChange,
    Notification,
    RiskAssessment,
    RiskLevel,
    now_utc,
)
from app.store import store

pytest_plugins = ("test_postgres_lifespan_approval_inbox",)


def _create_due_approval(repository, *, project_id: str, workspace_id: str) -> ApprovalRequest:
    now = now_utc()
    approval = ApprovalRequest(
        id=f"approval_expiry_{uuid.uuid4().hex}",
        project_id=project_id,
        workspace_id=workspace_id,
        title="Approval expiry integration",
        summary="A durable sweeper must expire and audit this exact request.",
        agent_name="Antigravity expiry integration",
        target_branch="feature/approval-expiry-integration",
        files=[FileChange(path="antigravity://tool/run_command", diff='{"command":"echo test"}')],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.PENDING_REVIEW,
        source_provider="ANTIGRAVITY",
        expires_at=now - timedelta(seconds=2),
        created_at=now - timedelta(minutes=5),
        updated_at=now - timedelta(minutes=5),
    )
    repository.create(
        approval=approval,
        audit_event=AuditEvent(
            event_type="approval.created_by_agent",
            entity_id=approval.id,
            workspace_id=workspace_id,
            actor="agent:expiry-test",
            created_at=approval.created_at,
            details={"source_provider": approval.source_provider},
        ),
        notification=Notification(
            title="Approval waiting",
            body="Review the requested action.",
            entity_type="approval_request",
            entity_id=approval.id,
            approval_request_id=approval.id,
            workspace_id=workspace_id,
        ),
    )
    return approval


def test_postgres_lifespan_expiry_sweeper_is_durable_bounded_and_fail_closed(
    postgres_lifespan_approval_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, workspace, project, _user, _agent, _agent_token = postgres_lifespan_approval_context
    runtime = main.PostgresRuntime.from_environment()
    repository = runtime.repositories.approval_decisions
    rollback_candidate = _create_due_approval(
        repository, project_id=project.id, workspace_id=workspace.id
    )

    assert [item.id for item in repository.list_due_pending(
        now=now_utc(),
        legacy_ttl_seconds=600,
        limit=1,
        workspace_id=workspace.id,
    )] == [rollback_candidate.id]
    assert repository.list_due_pending(
        now=now_utc(),
        legacy_ttl_seconds=600,
        limit=10,
        workspace_id="workspace-not-owned-by-this-test",
    ) == []

    # An audit write failure must roll back the state transition in the same
    # repository transaction, leaving the approval available for safe retry.
    with connect() as connection:
        connection.execute(
            "CREATE FUNCTION reject_expiry_audit() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN IF NEW.event_type='approval.expired' THEN RAISE EXCEPTION 'injected audit failure'; "
            "END IF; RETURN NEW; END $$"
        )
        connection.execute(
            "CREATE TRIGGER reject_expiry_audit BEFORE INSERT ON audit_events "
            "FOR EACH ROW EXECUTE FUNCTION reject_expiry_audit()"
        )
    with pytest.raises(Exception, match="injected audit failure"):
        main.expire_due_approvals(repository=repository, workspace_id=workspace.id, limit=1)
    assert repository.get(rollback_candidate.id).status == ApprovalStatus.PENDING_REVIEW
    with connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM audit_events WHERE entity_id=%s AND event_type='approval.expired'",
            (rollback_candidate.id,),
        ).fetchone()[0] == 0
        connection.execute("DROP TRIGGER reject_expiry_audit ON audit_events")
        connection.execute("DROP FUNCTION reject_expiry_audit()")

    assert main.expire_due_approvals(
        repository=repository, workspace_id=workspace.id, limit=1
    ) == 1
    assert main.expire_due_approvals(
        repository=repository, workspace_id=workspace.id, limit=1
    ) == 0
    assert repository.get(rollback_candidate.id).status == ApprovalStatus.EXPIRED
    with connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM audit_events WHERE entity_id=%s AND event_type='approval.expired'",
            (rollback_candidate.id,),
        ).fetchone()[0] == 1

    lifespan_candidate = _create_due_approval(
        repository, project_id=project.id, workspace_id=workspace.id
    )
    store_snapshot = {
        "approvals": set(store.approval_requests),
        "audits": {event.id for event in store.audit_events},
    }

    def reject_sqlite_access():
        pytest.fail("PostgreSQL lifespan expiry must not access SQLite store locking")

    monkeypatch.setattr(store, "atomic", reject_sqlite_access)
    with TestClient(main.app) as client:
        assert client.app.state.approval_decision_repository is repository
        assert client.app.state.approval_expiry_status == "healthy"
        assert client.app.state.approval_expiry_last_changed_count == 1
    assert repository.get(lifespan_candidate.id).status == ApprovalStatus.EXPIRED
    assert set(store.approval_requests) == store_snapshot["approvals"]
    assert {event.id for event in store.audit_events} == store_snapshot["audits"]
