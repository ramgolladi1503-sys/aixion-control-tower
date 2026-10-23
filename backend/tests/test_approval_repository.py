from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest

from app.agent_task_models import (
    AgentTask,
    AgentTaskEvent,
    AgentTaskEventType,
    AgentTaskStatus,
)
from app.approval_integrity import compute_approval_payload_hash
from app.approval_expiry import expire_due_approvals
from app.approval_repository import (
    ApprovalDecisionConflict,
    ApprovalDeliveryEvidenceIntegrityError,
    ApprovalDeliveryEvidenceUnavailable,
    SQLiteApprovalDecisionRepository,
)
from app.database_migrations import run_migrations
from app.models import (
    ApprovalRequest,
    ApprovalStatus,
    AuditEvent,
    FileChange,
    Notification,
    Project,
    RiskAssessment,
    RiskLevel,
    Workspace,
    now_utc,
)


def _approval() -> ApprovalRequest:
    return ApprovalRequest(
        project_id="project_repo_test",
        title="Repository test approval",
        summary="Test conditional decision persistence",
        target_branch="feature/repository-test",
        files=[FileChange(path="README.md", diff="+safe")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.REQUESTED,
    )


def _repository(database: Path) -> SQLiteApprovalDecisionRepository:
    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(database, timeout=10)
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    return SQLiteApprovalDecisionRepository(connect)


def test_approved_payload_hash_binds_connector_invocation_identity() -> None:
    first = _approval().model_copy(update={"source_invocation_id": "antigravity:v1:call-a"})
    second = first.model_copy(update={"source_invocation_id": "antigravity:v1:call-b"})

    assert compute_approval_payload_hash(first) != compute_approval_payload_hash(second)


def test_hash_v1_remains_compatible_and_hash_v2_binds_workspace_and_risk() -> None:
    legacy = _approval().model_copy(update={"workspace_id": "workspace-risk-hash"})
    legacy_risk_changed = legacy.model_copy(
        update={"risk": RiskAssessment(level=RiskLevel.HIGH, reasons=["changed risk"])}
    )
    assert compute_approval_payload_hash(legacy) == compute_approval_payload_hash(legacy_risk_changed)

    v2 = legacy.model_copy(update={"approved_payload_hash_version": 2})
    changed_risk = v2.model_copy(
        update={"risk": RiskAssessment(level=RiskLevel.HIGH, reasons=["changed risk"])}
    )
    changed_workspace = v2.model_copy(update={"workspace_id": "another-workspace"})
    assert compute_approval_payload_hash(v2).startswith("sha256:v2:")
    assert compute_approval_payload_hash(v2) != compute_approval_payload_hash(changed_risk)
    assert compute_approval_payload_hash(v2) != compute_approval_payload_hash(changed_workspace)


def test_approval_payload_hash_version_rejects_unknown_values() -> None:
    with pytest.raises(ValueError):
        ApprovalRequest.model_validate(
            _approval().model_dump() | {"approved_payload_hash_version": 3}
        )


def _seed_database(database: Path, approval: ApprovalRequest) -> None:
    with sqlite3.connect(database) as connection:
        run_migrations(connection)
        project = Project(
            id=approval.project_id,
            name="Repository test project",
            workspace_id=approval.workspace_id,
        )
        connection.execute(
            "INSERT INTO kv_store(entity_type, entity_id, payload) VALUES (?, ?, ?)",
            ("project", project.id, project.model_dump_json()),
        )
        if project.workspace_id is not None:
            workspace = Workspace(
                id=project.workspace_id,
                name="Repository test workspace",
                owner_user_id="user_repo_test",
            )
            connection.execute(
                "INSERT INTO kv_store(entity_type, entity_id, payload) VALUES (?, ?, ?)",
                ("workspace", workspace.id, workspace.model_dump_json()),
            )
        connection.execute(
            "INSERT INTO kv_store(entity_type, entity_id, payload) VALUES (?, ?, ?)",
            ("approval_request", approval.id, approval.model_dump_json()),
        )


def test_get_and_list_reject_orphaned_or_mismatched_project_scope(tmp_path: Path) -> None:
    database = tmp_path / "approval_read_scope.sqlite3"
    valid = _approval().model_copy(
        update={"id": "approval_read_scope", "workspace_id": "workspace_repo_test"}
    )
    _seed_database(database, valid)
    orphan = valid.model_copy(
        update={"id": "approval_orphaned", "project_id": "project_missing"}
    )
    mismatched = valid.model_copy(
        update={"id": "approval_mismatched", "workspace_id": "workspace_other"}
    )
    with sqlite3.connect(database) as connection:
        connection.executemany(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            (
                ("approval_request", orphan.id, orphan.model_dump_json()),
                ("approval_request", mismatched.id, mismatched.model_dump_json()),
            ),
        )

    repository = _repository(database)
    assert repository.get(orphan.id) is None
    assert repository.get(mismatched.id) is None
    assert {item.id for item in repository.list_for_workspace()} == {valid.id}
    assert repository.list_for_workspace("workspace_other") == []


def test_list_due_pending_is_bounded_scoped_and_includes_legacy_deadlines(
    tmp_path: Path,
) -> None:
    database = tmp_path / "approval_due_selection.sqlite3"
    now = datetime.now(timezone.utc)
    due_explicit = _approval().model_copy(
        update={
            "id": "approval_due_explicit",
            "project_id": "project_due_explicit",
            "workspace_id": "workspace_due_a",
            "status": ApprovalStatus.PENDING_REVIEW,
            "created_at": now - timedelta(minutes=10),
            "expires_at": now - timedelta(seconds=2),
        }
    )
    due_legacy = _approval().model_copy(
        update={
            "id": "approval_due_legacy",
            "project_id": "project_due_legacy",
            "workspace_id": "workspace_due_a",
            "status": ApprovalStatus.REQUESTED,
            "created_at": now - timedelta(minutes=10),
            "expires_at": None,
        }
    )
    other_workspace = _approval().model_copy(
        update={
            "id": "approval_due_other_workspace",
            "project_id": "project_due_other",
            "workspace_id": "workspace_due_b",
            "status": ApprovalStatus.REQUESTED,
            "created_at": now - timedelta(minutes=10),
            "expires_at": now - timedelta(seconds=1),
        }
    )
    not_due = _approval().model_copy(
        update={
            "id": "approval_not_due",
            "project_id": "project_not_due",
            "workspace_id": "workspace_due_a",
            "status": ApprovalStatus.REQUESTED,
            "created_at": now - timedelta(minutes=1),
            "expires_at": now + timedelta(minutes=1),
        }
    )
    with sqlite3.connect(database) as connection:
        run_migrations(connection)
        for approval in (due_explicit, due_legacy, other_workspace, not_due):
            project = Project(
                id=approval.project_id,
                name="Due query project",
                workspace_id=approval.workspace_id,
            )
            connection.execute(
                "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
                ("project", project.id, project.model_dump_json()),
            )
            connection.execute(
                "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
                ("approval_request", approval.id, approval.model_dump_json()),
            )

    repository = _repository(database)
    selected = repository.list_due_pending(
        now=now,
        legacy_ttl_seconds=600,
        limit=10,
        workspace_id="workspace_due_a",
    )
    assert {approval.id for approval in selected} == {
        due_explicit.id,
        due_legacy.id,
    }
    assert repository.list_due_pending(
        now=now,
        legacy_ttl_seconds=600,
        limit=1,
    ) == [due_explicit]


def test_list_due_pending_rejects_invalid_bounds(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "approval_due_bounds.sqlite3")
    with pytest.raises(ValueError, match="bounds"):
        repository.list_due_pending(
            now=datetime.now(timezone.utc),
            legacy_ttl_seconds=600,
            limit=0,
        )


def test_list_due_pending_preserves_unscoped_local_approvals_without_cross_scope_leakage(
    tmp_path: Path,
) -> None:
    database = tmp_path / "approval_due_unscoped.sqlite3"
    now = datetime.now(timezone.utc)
    approval = _approval().model_copy(
        update={
            "id": "approval_due_local_unscoped",
            "project_id": "project_due_local_unscoped",
            "workspace_id": None,
            "status": ApprovalStatus.PENDING_REVIEW,
            "created_at": now - timedelta(minutes=5),
            "expires_at": now - timedelta(minutes=1),
        }
    )
    _seed_database(database, approval)
    repository = _repository(database)

    assert [item.id for item in repository.list_due_pending(
        now=now,
        legacy_ttl_seconds=600,
        limit=250,
    )] == [approval.id]
    assert repository.list_due_pending(
        now=now,
        legacy_ttl_seconds=600,
        limit=250,
        workspace_id="workspace-not-local",
    ) == []


def test_list_due_pending_validates_before_limit_to_prevent_corrupt_row_starvation(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database = tmp_path / "approval_due_invalid_rows.sqlite3"
    now = datetime.now(timezone.utc)
    valid = _approval().model_copy(
        update={
            "id": "approval_valid_after_invalid_rows",
            "project_id": "project_due_invalid_rows",
            "workspace_id": "workspace_due_invalid_rows",
            "status": ApprovalStatus.PENDING_REVIEW,
            "created_at": now - timedelta(minutes=5),
            "expires_at": now - timedelta(minutes=1),
        }
    )
    _seed_database(database, valid)
    with sqlite3.connect(database) as connection:
        invalid_rows = []
        for index in range(260):
            entity_id = f"approval_invalid_{index:03d}"
            payload = valid.model_dump(mode="json") | {
                "id": f"payload_identity_mismatch_{index:03d}",
                "expires_at": (now - timedelta(minutes=2)).isoformat(),
            }
            invalid_rows.append(
                ("approval_request", entity_id, json.dumps(payload, separators=(",", ":")))
            )
        connection.executemany(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            invalid_rows,
        )

    repository = _repository(database)
    with caplog.at_level("WARNING", logger="app.approval_repository"):
        selected = repository.list_due_pending(
            now=now,
            legacy_ttl_seconds=600,
            limit=250,
            workspace_id="workspace_due_invalid_rows",
        )

    assert [approval.id for approval in selected] == [valid.id]
    assert "invalid persisted approval identity" in caplog.text
    assert "approval_invalid_000" in caplog.text


def test_list_due_pending_skips_malformed_payload_and_returns_valid_due_approval(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database = tmp_path / "approval_due_malformed_payload.sqlite3"
    now = datetime.now(timezone.utc)
    valid = _approval().model_copy(
        update={
            "id": "approval_valid_malformed_neighbor",
            "project_id": "project_malformed_neighbor",
            "workspace_id": "workspace_malformed_neighbor",
            "status": ApprovalStatus.PENDING_REVIEW,
            "created_at": now - timedelta(minutes=5),
            "expires_at": now - timedelta(minutes=1),
        }
    )
    _seed_database(database, valid)
    malformed_id = "approval_malformed_neighbor"
    malformed_payload = {
        "id": malformed_id,
        "project_id": valid.project_id,
        "workspace_id": valid.workspace_id,
        "status": ApprovalStatus.PENDING_REVIEW.value,
        "created_at": (now - timedelta(minutes=4)).isoformat(),
        "expires_at": (now - timedelta(minutes=2)).isoformat(),
    }
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            (
                "approval_request",
                malformed_id,
                json.dumps(malformed_payload, separators=(",", ":")),
            ),
        )

    with caplog.at_level("WARNING", logger="app.approval_repository"):
        selected = _repository(database).list_due_pending(
            now=now,
            legacy_ttl_seconds=600,
            limit=250,
            workspace_id=valid.workspace_id,
        )

    assert [approval.id for approval in selected] == [valid.id]
    assert "skipped malformed persisted payload" in caplog.text
    assert malformed_id in caplog.text
    assert "Repository test approval" not in caplog.text


def test_recent_null_expiry_approvals_are_backfilled_in_bounded_batches(
    tmp_path: Path,
) -> None:
    database = tmp_path / "approval_due_null_expiry.sqlite3"
    now = datetime.now(timezone.utc)
    first = _approval().model_copy(
        update={
            "id": "approval_null_expiry_000",
            "project_id": "project_null_expiry_batch",
            "workspace_id": "workspace_null_expiry_batch",
            "created_at": now,
            "expires_at": None,
        }
    )
    project = Project(
        id=first.project_id,
        name="Legacy approvals project",
        workspace_id=first.workspace_id,
    )
    with sqlite3.connect(database) as connection:
        run_migrations(connection)
        connection.execute(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            ("project", project.id, project.model_dump_json()),
        )
        for index in range(260):
            approval = first.model_copy(update={"id": f"approval_null_expiry_{index:03d}"})
            connection.execute(
                "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
                ("approval_request", approval.id, approval.model_dump_json()),
            )

    repository = _repository(database)
    first_batch = repository.list_due_pending(
        now=now,
        legacy_ttl_seconds=600,
        limit=250,
        workspace_id=first.workspace_id,
    )
    assert len(first_batch) == 250
    assert all(approval.expires_at is None for approval in first_batch)

    assert expire_due_approvals(repository=repository, workspace_id=first.workspace_id) == 0
    remaining = repository.list_due_pending(
        now=now_utc(),
        legacy_ttl_seconds=600,
        limit=250,
        workspace_id=first.workspace_id,
    )
    assert len(remaining) == 10
    assert all(approval.expires_at is None for approval in remaining)

    assert expire_due_approvals(repository=repository, workspace_id=first.workspace_id) == 0
    assert repository.list_due_pending(
        now=now_utc(),
        legacy_ttl_seconds=600,
        limit=250,
        workspace_id=first.workspace_id,
    ) == []
    assert all(
        repository.get(f"approval_null_expiry_{index:03d}").expires_at is not None
        for index in range(260)
    )


def _audit(
    approval: ApprovalRequest,
    suffix: str,
    *,
    previous_status: ApprovalStatus = ApprovalStatus.REQUESTED,
    new_status: ApprovalStatus = ApprovalStatus.APPROVED,
) -> AuditEvent:
    approved_payload_hash = (
        compute_approval_payload_hash(approval)
        if new_status == ApprovalStatus.APPROVED
        else None
    )
    return AuditEvent(
        id=f"audit_{suffix}",
        event_type="approval.decision",
        entity_id=approval.id,
        details={
            "previous_status": previous_status,
            "new_status": new_status,
            "approved_payload_hash": approved_payload_hash,
        },
    )


def _approved_decision(approval: ApprovalRequest, **changes: object) -> ApprovalRequest:
    updated = approval.model_copy(update={"status": ApprovalStatus.APPROVED, **changes})
    return updated.model_copy(
        update={"approved_payload_hash": compute_approval_payload_hash(updated)}
    )


def _notification(approval: ApprovalRequest, suffix: str) -> Notification:
    return Notification(
        id=f"notification_{suffix}",
        title="Approval decision recorded",
        body="Open the app to view the decision.",
        entity_type="approval_request",
        entity_id=approval.id,
        workspace_id=approval.workspace_id,
    )


def test_decision_and_audit_commit_together(tmp_path: Path) -> None:
    database = tmp_path / "approval.sqlite3"
    approval = _approval()
    _seed_database(database, approval)
    task = AgentTask(
        project_id=approval.project_id,
        title="Linked task",
        goal="Run only after approval",
        approval_request_id=approval.id,
        status=AgentTaskStatus.WAITING_FOR_APPROVAL,
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO kv_store(entity_type, entity_id, payload) VALUES (?, ?, ?)",
            ("agent_task", task.id, task.model_dump_json()),
        )
    updated = _approved_decision(approval)
    decision_audit = _audit(approval, "one")

    _repository(database).commit_decision(
        approval_id=approval.id,
        expected_status=ApprovalStatus.REQUESTED,
        expected_approval=approval,
        updated_approval=updated,
        audit_event=decision_audit,
        decision_notification=_notification(approval, "one"),
    )

    repository = _repository(database)
    assert repository.latest_decision_audit(
        approval_id=approval.id,
        workspace_id=approval.workspace_id,
        new_status=ApprovalStatus.APPROVED,
    ) == decision_audit
    assert repository.latest_decision_audit(
        approval_id=approval.id,
        workspace_id="other_workspace",
        new_status=ApprovalStatus.APPROVED,
    ) is None

    with sqlite3.connect(database) as connection:
        persisted = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type = ? AND entity_id = ?",
            ("approval_request", approval.id),
        ).fetchone()
        audit_rows = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type = ? AND entity_id = ?",
            ("audit_event", "audit_one"),
        ).fetchall()
        task_payload = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type = ? AND entity_id = ?",
            ("agent_task", task.id),
        ).fetchone()[0]
        notification_payload = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type = ? AND entity_id = ?",
            ("notification", "notification_one"),
        ).fetchone()[0]
        task_events = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type='agent_task_event'"
        ).fetchall()
        task_notifications = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type='notification' "
            "AND entity_id != 'notification_one'"
        ).fetchall()
        task_audits = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type='audit_event' "
            "AND entity_id != 'audit_one'"
        ).fetchall()

    assert ApprovalRequest.model_validate_json(persisted[0]).status == ApprovalStatus.APPROVED
    assert len(audit_rows) == 1
    assert AgentTask.model_validate_json(task_payload).status == AgentTaskStatus.APPROVED
    assert Notification.model_validate_json(notification_payload).push_status == "PENDING"
    assert len(task_events) == 1
    assert AgentTaskEvent.model_validate_json(task_events[0][0]).event_type == AgentTaskEventType.APPROVED
    assert Notification.model_validate_json(task_notifications[0][0]).entity_type == "agent_task"
    assert AuditEvent.model_validate_json(task_audits[0][0]).event_type == "agent_task.approval_decision_propagated"


def test_creation_persists_approval_audit_and_notification_together(tmp_path: Path) -> None:
    database = tmp_path / "approval_create.sqlite3"
    existing = _approval().model_copy(update={"id": "approval_existing", "workspace_id": "workspace_repo_test"})
    _seed_database(database, existing)
    approval = existing.model_copy(update={"id": "approval_created"})
    audit_event = AuditEvent(
        id="audit_created",
        event_type="approval.created",
        entity_id=approval.id,
        workspace_id=approval.workspace_id,
        details={"project_id": approval.project_id},
    )
    notification = Notification(
        id="notification_created",
        title="Approval requested",
        body="Open the app to review.",
        entity_type="approval_request",
        entity_id=approval.id,
        workspace_id=approval.workspace_id,
    )

    _repository(database).create(
        approval=approval,
        audit_event=audit_event,
        notification=notification,
    )

    assert _repository(database).get(approval.id) == approval
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM kv_store WHERE entity_type='audit_event' AND entity_id='audit_created'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM kv_store WHERE entity_type='notification' AND entity_id='notification_created'"
        ).fetchone()[0] == 1


def test_list_for_workspace_reads_only_durable_workspace_approvals(tmp_path: Path) -> None:
    database = tmp_path / "approval_workspace_list.sqlite3"
    first = _approval().model_copy(
        update={"id": "approval_workspace_first", "workspace_id": "workspace_first"}
    )
    _seed_database(database, first)
    second_workspace = Workspace(
        id="workspace_second", name="Second", owner_user_id="owner_second"
    )
    second_project = Project(
        id="project_second", name="Second project", workspace_id=second_workspace.id
    )
    second = first.model_copy(
        update={
            "id": "approval_workspace_second",
            "project_id": second_project.id,
            "workspace_id": second_workspace.id,
        }
    )
    with sqlite3.connect(database) as connection:
        connection.executemany(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            (
                ("workspace", second_workspace.id, second_workspace.model_dump_json()),
                ("project", second_project.id, second_project.model_dump_json()),
                ("approval_request", second.id, second.model_dump_json()),
            ),
        )

    repository = _repository(database)
    assert [item.id for item in repository.list_for_workspace(first.workspace_id)] == [first.id]
    assert {item.id for item in repository.list_for_workspace()} == {first.id, second.id}


def test_creation_rolls_back_approval_when_notification_insert_fails(tmp_path: Path) -> None:
    database = tmp_path / "approval_create_rollback.sqlite3"
    existing = _approval().model_copy(update={"id": "approval_existing", "workspace_id": "workspace_repo_test"})
    _seed_database(database, existing)
    approval = existing.model_copy(update={"id": "approval_rollback"})
    audit_event = AuditEvent(
        id="audit_rollback",
        event_type="approval.created",
        entity_id=approval.id,
        workspace_id=approval.workspace_id,
    )
    notification = Notification(
        id="notification_rollback",
        title="Approval requested",
        body="Open the app to review.",
        entity_type="approval_request",
        entity_id=approval.id,
        workspace_id=approval.workspace_id,
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TRIGGER reject_creation_notification BEFORE INSERT ON kv_store "
            "WHEN NEW.entity_type='notification' "
            "BEGIN SELECT RAISE(ABORT, 'notification unavailable'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="notification unavailable"):
        _repository(database).create(
            approval=approval,
            audit_event=audit_event,
            notification=notification,
        )

    assert _repository(database).get(approval.id) is None
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM kv_store WHERE entity_type='audit_event' AND entity_id='audit_rollback'"
        ).fetchone()[0] == 0


def _agent_task_approval_records(database: Path) -> tuple[ApprovalRequest, AgentTask, AuditEvent, AgentTaskEvent, AuditEvent, Notification]:
    existing = _approval().model_copy(
        update={"id": "approval_task_seed", "workspace_id": "workspace_repo_test"}
    )
    _seed_database(database, existing)
    task = AgentTask(
        id="task_approval_seed",
        project_id=existing.project_id,
        workspace_id=existing.workspace_id,
        title="Task needing approval",
        goal="Wait for a reviewer decision",
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            ("agent_task", task.id, task.model_dump_json()),
        )
    approval = existing.model_copy(update={"id": "approval_task_created"})
    linked_task = task.model_copy(
        update={
            "approval_request_id": approval.id,
            "status": AgentTaskStatus.WAITING_FOR_APPROVAL,
        }
    )
    approval_audit = AuditEvent(
        id="audit_task_approval_created",
        event_type="approval.created",
        entity_id=approval.id,
        workspace_id=approval.workspace_id,
    )
    task_event = AgentTaskEvent(
        id="event_task_approval_created",
        task_id=task.id,
        event_type=AgentTaskEventType.APPROVAL_CREATED,
        status=AgentTaskStatus.WAITING_FOR_APPROVAL,
        actor="reviewer@example.test",
    )
    task_audit = AuditEvent(
        id="audit_agent_task_approval_created",
        event_type="agent_task.approval_created",
        entity_id=task.id,
        workspace_id=approval.workspace_id,
    )
    notification = Notification(
        id="notification_task_approval_created",
        title="Agent task approval needed",
        body="Review the approval request.",
        entity_type="agent_task",
        entity_id=task.id,
        workspace_id=approval.workspace_id,
    )
    return approval, linked_task, approval_audit, task_event, task_audit, notification


def test_agent_task_approval_creation_commits_task_link_timeline_and_audit_together(
    tmp_path: Path,
) -> None:
    database = tmp_path / "agent_task_approval_create.sqlite3"
    _seed_approval, task, approval_audit, task_event, task_audit, notification = (
        _agent_task_approval_records(database)
    )
    approval = _seed_approval.model_copy(update={"id": "approval_task_created"})

    _repository(database).create_for_agent_task(
        approval=approval,
        audit_event=approval_audit,
        task=task,
        task_event=task_event,
        task_audit_event=task_audit,
        notification=notification,
    )

    with sqlite3.connect(database) as connection:
        durable_task = AgentTask.model_validate_json(
            connection.execute(
                "SELECT payload FROM kv_store WHERE entity_type='agent_task' AND entity_id=?",
                (task.id,),
            ).fetchone()[0]
        )
        assert durable_task.approval_request_id == approval.id
        assert durable_task.status == AgentTaskStatus.WAITING_FOR_APPROVAL
        assert connection.execute(
            "SELECT count(*) FROM kv_store WHERE entity_type='agent_task_event' AND entity_id=?",
            (task_event.id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM kv_store WHERE entity_type='audit_event' AND entity_id IN (?,?)",
            (approval_audit.id, task_audit.id),
        ).fetchone()[0] == 2


def test_agent_task_approval_creation_rolls_back_every_record_on_timeline_failure(
    tmp_path: Path,
) -> None:
    database = tmp_path / "agent_task_approval_rollback.sqlite3"
    approval, task, approval_audit, task_event, task_audit, notification = (
        _agent_task_approval_records(database)
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TRIGGER reject_task_timeline BEFORE INSERT ON kv_store "
            "WHEN NEW.entity_type='agent_task_event' "
            "BEGIN SELECT RAISE(ABORT, 'timeline unavailable'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="timeline unavailable"):
        _repository(database).create_for_agent_task(
            approval=approval,
            audit_event=approval_audit,
            task=task,
            task_event=task_event,
            task_audit_event=task_audit,
            notification=notification,
        )

    with sqlite3.connect(database) as connection:
        durable_task = AgentTask.model_validate_json(
            connection.execute(
                "SELECT payload FROM kv_store WHERE entity_type='agent_task' AND entity_id=?",
                (task.id,),
            ).fetchone()[0]
        )
        assert durable_task.approval_request_id is None
        assert connection.execute(
            "SELECT count(*) FROM kv_store WHERE entity_type='approval_request' AND entity_id=?",
            (approval.id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM kv_store WHERE entity_type='audit_event' AND entity_id IN (?,?)",
            (approval_audit.id, task_audit.id),
        ).fetchone()[0] == 0


def test_audit_insert_failure_rolls_back_approval_transition(tmp_path: Path) -> None:
    database = tmp_path / "approval_rollback.sqlite3"
    approval = _approval()
    _seed_database(database, approval)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TRIGGER reject_audit BEFORE INSERT ON kv_store "
            "WHEN NEW.entity_type = 'audit_event' "
            "BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END"
        )

    updated = _approved_decision(approval)
    with pytest.raises(sqlite3.IntegrityError, match="audit unavailable"):
        _repository(database).commit_decision(
            approval_id=approval.id,
            expected_status=ApprovalStatus.REQUESTED,
            expected_approval=approval,
            updated_approval=updated,
            audit_event=_audit(approval, "rollback"),
            decision_notification=_notification(approval, "rollback"),
        )

    with sqlite3.connect(database) as connection:
        payload = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type = ? AND entity_id = ?",
            ("approval_request", approval.id),
        ).fetchone()[0]
        audit_count = connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type = 'audit_event'"
        ).fetchone()[0]
    assert ApprovalRequest.model_validate_json(payload).status == ApprovalStatus.REQUESTED
    assert audit_count == 0


def test_decision_rejects_audit_event_with_mismatched_approved_payload_hash(
    tmp_path: Path,
) -> None:
    database = tmp_path / "approval_audit_hash_mismatch.sqlite3"
    approval = _approval()
    _seed_database(database, approval)
    updated = _approved_decision(approval)
    audit_event = _audit(approval, "audit-hash-mismatch")
    audit_event.details["approved_payload_hash"] = "different-payload-hash"

    with pytest.raises(ValueError, match="audit payload hash"):
        _repository(database).commit_decision(
            approval_id=approval.id,
            expected_status=ApprovalStatus.REQUESTED,
            expected_approval=approval,
            updated_approval=updated,
            audit_event=audit_event,
            decision_notification=_notification(approval, "audit-hash-mismatch"),
        )

    with sqlite3.connect(database) as connection:
        approval_payload = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type = ? AND entity_id = ?",
            ("approval_request", approval.id),
        ).fetchone()[0]
        audit_count = connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type = 'audit_event'"
        ).fetchone()[0]
        notification_count = connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type = 'notification'"
        ).fetchone()[0]

    assert ApprovalRequest.model_validate_json(approval_payload) == approval
    assert audit_count == 0
    assert notification_count == 0


def test_approved_decision_requires_a_frozen_payload_hash(tmp_path: Path) -> None:
    database = tmp_path / "approval_missing_payload_hash.sqlite3"
    approval = _approval()
    _seed_database(database, approval)
    updated = approval.model_copy(update={"status": ApprovalStatus.APPROVED})
    audit_event = _audit(approval, "missing-payload-hash")
    audit_event.details["approved_payload_hash"] = None

    with pytest.raises(ValueError, match="must include a frozen payload hash"):
        _repository(database).commit_decision(
            approval_id=approval.id,
            expected_status=ApprovalStatus.REQUESTED,
            expected_approval=approval,
            updated_approval=updated,
            audit_event=audit_event,
            decision_notification=_notification(approval, "missing-payload-hash"),
        )

    with sqlite3.connect(database) as connection:
        approval_payload = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type = ? AND entity_id = ?",
            ("approval_request", approval.id),
        ).fetchone()[0]
        audit_count = connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type = 'audit_event'"
        ).fetchone()[0]
        notification_count = connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type = 'notification'"
        ).fetchone()[0]

    assert ApprovalRequest.model_validate_json(approval_payload) == approval
    assert audit_count == 0
    assert notification_count == 0


def test_approved_decision_rejects_payload_changed_after_hashing(tmp_path: Path) -> None:
    database = tmp_path / "approval_changed_payload_hash.sqlite3"
    approval = _approval()
    _seed_database(database, approval)
    updated = _approved_decision(approval).model_copy(
        update={"summary": "Changed after payload hash was calculated"}
    )

    with pytest.raises(ValueError, match="does not match its frozen payload hash"):
        _repository(database).commit_decision(
            approval_id=approval.id,
            expected_status=ApprovalStatus.REQUESTED,
            expected_approval=approval,
            updated_approval=updated,
            audit_event=_audit(approval, "changed-after-hashing"),
            decision_notification=_notification(approval, "changed-after-hashing"),
        )

    with sqlite3.connect(database) as connection:
        approval_payload = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type = ? AND entity_id = ?",
            ("approval_request", approval.id),
        ).fetchone()[0]
        audit_count = connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type = 'audit_event'"
        ).fetchone()[0]
        notification_count = connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type = 'notification'"
        ).fetchone()[0]

    assert ApprovalRequest.model_validate_json(approval_payload) == approval
    assert audit_count == 0
    assert notification_count == 0


def test_linked_task_write_failure_rolls_back_approval_and_audit(tmp_path: Path) -> None:
    database = tmp_path / "approval_task_rollback.sqlite3"
    approval = _approval()
    _seed_database(database, approval)
    task = AgentTask(
        project_id=approval.project_id,
        title="Linked task",
        goal="Run only after approval",
        approval_request_id=approval.id,
        status=AgentTaskStatus.WAITING_FOR_APPROVAL,
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO kv_store(entity_type, entity_id, payload) VALUES (?, ?, ?)",
            ("agent_task", task.id, task.model_dump_json()),
        )
        connection.execute(
            "CREATE TRIGGER reject_task_update BEFORE UPDATE ON kv_store "
            "WHEN OLD.entity_type = 'agent_task' "
            "BEGIN SELECT RAISE(ABORT, 'task persistence unavailable'); END"
        )

    updated = _approved_decision(approval)
    with pytest.raises(sqlite3.IntegrityError, match="task persistence unavailable"):
        _repository(database).commit_decision(
            approval_id=approval.id,
            expected_status=ApprovalStatus.REQUESTED,
            expected_approval=approval,
            updated_approval=updated,
            audit_event=_audit(approval, "task-rollback"),
            decision_notification=_notification(approval, "task-rollback"),
        )

    with sqlite3.connect(database) as connection:
        approval_payload = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type = ? AND entity_id = ?",
            ("approval_request", approval.id),
        ).fetchone()[0]
        task_payload = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type = ? AND entity_id = ?",
            ("agent_task", task.id),
        ).fetchone()[0]
        audit_count = connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type = 'audit_event'"
        ).fetchone()[0]
    assert ApprovalRequest.model_validate_json(approval_payload).status == ApprovalStatus.REQUESTED
    assert AgentTask.model_validate_json(task_payload).status == AgentTaskStatus.WAITING_FOR_APPROVAL
    assert audit_count == 0


def test_expiration_audit_failure_rolls_back_durable_expiration(tmp_path: Path) -> None:
    database = tmp_path / "approval_expiration_rollback.sqlite3"
    approval = _approval().model_copy(
        update={
            "status": ApprovalStatus.PENDING_REVIEW,
            "source_agent_id": "agent-expiration-test",
        }
    )
    _seed_database(database, approval)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TRIGGER reject_expiration_audit BEFORE INSERT ON kv_store "
            "WHEN NEW.entity_type = 'audit_event' "
            "BEGIN SELECT RAISE(ABORT, 'expiration audit unavailable'); END"
        )
    expired = approval.model_copy(update={"status": ApprovalStatus.EXPIRED})
    audit_event = AuditEvent(
        event_type="approval.expired",
        entity_id=approval.id,
        details={
            "previous_status": ApprovalStatus.PENDING_REVIEW,
            "new_status": ApprovalStatus.EXPIRED,
        },
    )

    with pytest.raises(sqlite3.IntegrityError, match="expiration audit unavailable"):
        _repository(database).expire_pending(
            approval_id=approval.id,
            expected_approval=approval,
            expired_approval=expired,
            audit_event=audit_event,
        )

    with sqlite3.connect(database) as connection:
        approval_payload = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type = ? AND entity_id = ?",
            ("approval_request", approval.id),
        ).fetchone()[0]
        audit_count = connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type = 'audit_event'"
        ).fetchone()[0]

    assert ApprovalRequest.model_validate_json(approval_payload) == approval
    assert audit_count == 0


def test_linked_task_expiration_commits_timeline_audit_and_notification_atomically(
    tmp_path: Path,
) -> None:
    database = tmp_path / "linked_approval_expiration.sqlite3"
    approval, task, approval_audit, task_event, task_audit, notification = (
        _agent_task_approval_records(database)
    )
    repository = _repository(database)
    repository.create_for_agent_task(
        approval=approval,
        audit_event=approval_audit,
        task=task,
        task_event=task_event,
        task_audit_event=task_audit,
        notification=notification,
    )
    expired = approval.model_copy(
        update={"status": ApprovalStatus.EXPIRED, "updated_at": now_utc()}
    )
    expiry_audit = AuditEvent(
        id="audit_linked_approval_expired",
        event_type="approval.expired",
        actor="expiry-test",
        entity_id=approval.id,
        workspace_id=approval.workspace_id,
        details={
            "previous_status": approval.status,
            "new_status": ApprovalStatus.EXPIRED,
            "expires_at": expired.expires_at,
        },
        created_at=expired.updated_at,
    )

    commit = repository.expire_pending(
        approval_id=approval.id,
        expected_approval=approval,
        expired_approval=expired,
        audit_event=expiry_audit,
    )

    assert commit.linked_task is not None
    assert commit.linked_task.status == AgentTaskStatus.WAITING_FOR_APPROVAL
    assert commit.linked_task_event is not None
    assert commit.linked_task_event.message == "Linked approval expired; task remains blocked."
    assert commit.linked_task_audit_event is not None
    assert commit.linked_task_notification is not None
    with sqlite3.connect(database) as connection:
        stored_task = AgentTask.model_validate_json(
            connection.execute(
                "SELECT payload FROM kv_store WHERE entity_type='agent_task' AND entity_id=?",
                (task.id,),
            ).fetchone()[0]
        )
        stored_approval = ApprovalRequest.model_validate_json(
            connection.execute(
                "SELECT payload FROM kv_store WHERE entity_type='approval_request' AND entity_id=?",
                (approval.id,),
            ).fetchone()[0]
        )
        assert stored_task.status == AgentTaskStatus.WAITING_FOR_APPROVAL
        assert stored_approval.status == ApprovalStatus.EXPIRED
        assert connection.execute(
            "SELECT count(*) FROM kv_store WHERE entity_type IN "
            "('agent_task_event','notification') AND entity_id IN (?,?)",
            (commit.linked_task_event.id, commit.linked_task_notification.id),
        ).fetchone()[0] == 2


def test_linked_task_expiration_rolls_back_all_records_when_notification_fails(
    tmp_path: Path,
) -> None:
    database = tmp_path / "linked_approval_expiration_rollback.sqlite3"
    approval, task, approval_audit, task_event, task_audit, notification = (
        _agent_task_approval_records(database)
    )
    repository = _repository(database)
    repository.create_for_agent_task(
        approval=approval,
        audit_event=approval_audit,
        task=task,
        task_event=task_event,
        task_audit_event=task_audit,
        notification=notification,
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TRIGGER reject_expiry_notification BEFORE INSERT ON kv_store "
            "WHEN NEW.entity_type='notification' AND NEW.entity_id!='notification_task_approval_created' "
            "BEGIN SELECT RAISE(ABORT, 'expiry notification unavailable'); END"
        )
    expired = approval.model_copy(
        update={"status": ApprovalStatus.EXPIRED, "updated_at": now_utc()}
    )
    expiry_audit = AuditEvent(
        id="audit_linked_approval_expired_rollback",
        event_type="approval.expired",
        actor="expiry-test",
        entity_id=approval.id,
        workspace_id=approval.workspace_id,
        details={
            "previous_status": approval.status,
            "new_status": ApprovalStatus.EXPIRED,
            "expires_at": expired.expires_at,
        },
        created_at=expired.updated_at,
    )

    with pytest.raises(sqlite3.IntegrityError, match="expiry notification unavailable"):
        repository.expire_pending(
            approval_id=approval.id,
            expected_approval=approval,
            expired_approval=expired,
            audit_event=expiry_audit,
        )

    assert repository.get(approval.id) == approval
    with sqlite3.connect(database) as connection:
        stored_task = AgentTask.model_validate_json(
            connection.execute(
                "SELECT payload FROM kv_store WHERE entity_type='agent_task' AND entity_id=?",
                (task.id,),
            ).fetchone()[0]
        )
        assert stored_task.updated_at == task.updated_at
        assert stored_task.status == AgentTaskStatus.WAITING_FOR_APPROVAL
        assert connection.execute(
            "SELECT count(*) FROM kv_store WHERE entity_type='agent_task_event' "
            "AND entity_id=?",
            (task_event.id,),
        ).fetchone()[0] == 1


@pytest.mark.parametrize("tamper", ["payload", "audit_workspace", "audit_deadline"])
def test_expiration_rejects_payload_or_audit_scope_tampering(
    tmp_path: Path,
    tamper: str,
) -> None:
    database = tmp_path / f"approval_expiration_{tamper}.sqlite3"
    approval = _approval().model_copy(update={"status": ApprovalStatus.PENDING_REVIEW})
    _seed_database(database, approval)
    expired = approval.model_copy(
        update={"status": ApprovalStatus.EXPIRED, "updated_at": now_utc()}
    )
    audit_event = AuditEvent(
        id=f"audit_expiration_{tamper}",
        event_type="approval.expired",
        entity_id=approval.id,
        workspace_id=approval.workspace_id,
        details={
            "previous_status": ApprovalStatus.PENDING_REVIEW,
            "new_status": ApprovalStatus.EXPIRED,
            "expires_at": expired.expires_at,
        },
    )
    expected_message = "preserve the approval request payload"
    if tamper == "payload":
        expired.summary = "tampered during expiration"
    elif tamper == "audit_workspace":
        audit_event.workspace_id = "another-workspace"
        expected_message = "audit workspace"
    else:
        audit_event.details["expires_at"] = "incorrect-deadline"
        expected_message = "audit deadline"

    with pytest.raises(ValueError, match=expected_message):
        _repository(database).expire_pending(
            approval_id=approval.id,
            expected_approval=approval,
            expired_approval=expired,
            audit_event=audit_event,
        )

    with sqlite3.connect(database) as connection:
        approval_payload = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type = ? AND entity_id = ?",
            ("approval_request", approval.id),
        ).fetchone()[0]
        audit_count = connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type = 'audit_event'"
        ).fetchone()[0]
    assert ApprovalRequest.model_validate_json(approval_payload) == approval
    assert audit_count == 0


def test_ambiguous_multiple_linked_tasks_refuse_decision(tmp_path: Path) -> None:
    database = tmp_path / "approval_multiple_tasks.sqlite3"
    approval = _approval()
    _seed_database(database, approval)
    tasks = [
        AgentTask(
            project_id=approval.project_id,
            title=f"Linked task {index}",
            goal="Run only after approval",
            approval_request_id=approval.id,
            status=AgentTaskStatus.WAITING_FOR_APPROVAL,
        )
        for index in range(2)
    ]
    with sqlite3.connect(database) as connection:
        connection.executemany(
            "INSERT INTO kv_store(entity_type, entity_id, payload) VALUES (?, ?, ?)",
            [("agent_task", task.id, task.model_dump_json()) for task in tasks],
        )
    updated = _approved_decision(approval)

    with pytest.raises(ApprovalDecisionConflict, match="multiple AgentTasks"):
        _repository(database).commit_decision(
            approval_id=approval.id,
            expected_status=ApprovalStatus.REQUESTED,
            expected_approval=approval,
            updated_approval=updated,
            audit_event=_audit(approval, "multiple-tasks"),
            decision_notification=_notification(approval, "multiple-tasks"),
        )

    with sqlite3.connect(database) as connection:
        approval_payload = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type = ? AND entity_id = ?",
            ("approval_request", approval.id),
        ).fetchone()[0]
        task_payloads = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type = 'agent_task'"
        ).fetchall()
        audit_count = connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type = 'audit_event'"
        ).fetchone()[0]
    assert ApprovalRequest.model_validate_json(approval_payload).status == ApprovalStatus.REQUESTED
    assert all(
        AgentTask.model_validate_json(row[0]).status == AgentTaskStatus.WAITING_FOR_APPROVAL
        for row in task_payloads
    )
    assert audit_count == 0


def test_decision_refuses_to_overwrite_a_linked_task_that_left_approval_wait(
    tmp_path: Path,
) -> None:
    database = tmp_path / "approval_task_already_cancelled.sqlite3"
    approval = _approval()
    _seed_database(database, approval)
    task = AgentTask(
        project_id=approval.project_id,
        title="Linked task",
        goal="Run only after approval",
        approval_request_id=approval.id,
        status=AgentTaskStatus.CANCELLED,
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO kv_store(entity_type, entity_id, payload) VALUES (?, ?, ?)",
            ("agent_task", task.id, task.model_dump_json()),
        )

    with pytest.raises(ApprovalDecisionConflict, match="no longer waiting"):
        _repository(database).commit_decision(
            approval_id=approval.id,
            expected_status=ApprovalStatus.REQUESTED,
            expected_approval=approval,
            updated_approval=_approved_decision(approval),
            audit_event=_audit(approval, "task-already-cancelled"),
            decision_notification=_notification(approval, "task-already-cancelled"),
        )

    with sqlite3.connect(database) as connection:
        persisted_approval = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type='approval_request' AND entity_id=?",
            (approval.id,),
        ).fetchone()[0]
        persisted_task = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type='agent_task' AND entity_id=?",
            (task.id,),
        ).fetchone()[0]
        assert connection.execute(
            "SELECT count(*) FROM kv_store WHERE entity_type IN ('audit_event','notification','agent_task_event')"
        ).fetchone()[0] == 0
    assert ApprovalRequest.model_validate_json(persisted_approval).status == ApprovalStatus.REQUESTED
    assert AgentTask.model_validate_json(persisted_task).status == AgentTaskStatus.CANCELLED


def test_linked_task_workspace_is_checked_against_project_inferred_approval_scope(
    tmp_path: Path,
) -> None:
    database = tmp_path / "approval_inferred_workspace_mismatch.sqlite3"
    approval = _approval()
    _seed_database(database, approval)
    project = Project(
        id=approval.project_id,
        name="Scoped project",
        workspace_id="workspace-a",
    )
    workspace = Workspace(
        id="workspace-a",
        name="Repository test workspace",
        owner_user_id="user_repo_test",
    )
    task = AgentTask(
        project_id=approval.project_id,
        workspace_id="workspace-b",
        title="Cross-workspace linked task",
        goal="Run only after approval",
        approval_request_id=approval.id,
        status=AgentTaskStatus.WAITING_FOR_APPROVAL,
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE kv_store SET payload = ? WHERE entity_type = ? AND entity_id = ?",
            (project.model_dump_json(), "project", project.id),
        )
        connection.execute(
            "INSERT INTO kv_store(entity_type, entity_id, payload) VALUES (?, ?, ?)",
            ("workspace", workspace.id, workspace.model_dump_json()),
        )
        connection.execute(
            "INSERT INTO kv_store(entity_type, entity_id, payload) VALUES (?, ?, ?)",
            ("agent_task", task.id, task.model_dump_json()),
        )
    updated = _approved_decision(approval)
    decision_notification = Notification(
        id="notification-inferred-workspace-mismatch",
        title="Approval decision recorded",
        body="Review the decision.",
        entity_type="approval_request",
        entity_id=approval.id,
        workspace_id="workspace-a",
    )

    with pytest.raises(ApprovalDecisionConflict, match="Linked AgentTask workspace"):
        _repository(database).commit_decision(
            approval_id=approval.id,
            expected_status=ApprovalStatus.REQUESTED,
            expected_approval=approval,
            updated_approval=updated,
            audit_event=_audit(approval, "inferred-workspace-mismatch"),
            decision_notification=decision_notification,
        )

    with sqlite3.connect(database) as connection:
        approval_payload = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type = ? AND entity_id = ?",
            ("approval_request", approval.id),
        ).fetchone()[0]
        task_payload = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type = ? AND entity_id = ?",
            ("agent_task", task.id),
        ).fetchone()[0]
        audit_count = connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type = 'audit_event'"
        ).fetchone()[0]
        notification_count = connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type = 'notification'"
        ).fetchone()[0]

    assert ApprovalRequest.model_validate_json(approval_payload).status == ApprovalStatus.REQUESTED
    assert AgentTask.model_validate_json(task_payload).status == AgentTaskStatus.WAITING_FOR_APPROVAL
    assert audit_count == 0
    assert notification_count == 0


def test_decision_cannot_change_durable_approval_workspace(tmp_path: Path) -> None:
    database = tmp_path / "approval_workspace_immutable.sqlite3"
    approval = _approval().model_copy(update={"workspace_id": "workspace-a"})
    _seed_database(database, approval)
    updated = _approved_decision(approval, workspace_id="workspace-b")

    with pytest.raises(ApprovalDecisionConflict, match="workspace cannot change"):
        _repository(database).commit_decision(
            approval_id=approval.id,
            expected_status=ApprovalStatus.REQUESTED,
            expected_approval=approval,
            updated_approval=updated,
            audit_event=_audit(approval, "workspace-immutable"),
            decision_notification=_notification(approval, "workspace-immutable"),
        )

    with sqlite3.connect(database) as connection:
        approval_payload = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type = ? AND entity_id = ?",
            ("approval_request", approval.id),
        ).fetchone()[0]
        audit_count = connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type = 'audit_event'"
        ).fetchone()[0]
        notification_count = connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type = 'notification'"
        ).fetchone()[0]

    persisted = ApprovalRequest.model_validate_json(approval_payload)
    assert persisted.status == ApprovalStatus.REQUESTED
    assert persisted.workspace_id == "workspace-a"
    assert audit_count == 0
    assert notification_count == 0


def test_decision_refuses_approval_with_missing_project(tmp_path: Path) -> None:
    database = tmp_path / "approval_missing_project.sqlite3"
    approval = _approval()
    _seed_database(database, approval)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "DELETE FROM kv_store WHERE entity_type = ? AND entity_id = ?",
            ("project", approval.project_id),
        )
    updated = _approved_decision(approval)

    with pytest.raises(ApprovalDecisionConflict, match="project is missing"):
        _repository(database).commit_decision(
            approval_id=approval.id,
            expected_status=ApprovalStatus.REQUESTED,
            expected_approval=approval,
            updated_approval=updated,
            audit_event=_audit(approval, "missing-project"),
            decision_notification=_notification(approval, "missing-project"),
        )

    with sqlite3.connect(database) as connection:
        approval_payload = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type = ? AND entity_id = ?",
            ("approval_request", approval.id),
        ).fetchone()[0]
        audit_count = connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type = 'audit_event'"
        ).fetchone()[0]
        notification_count = connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type = 'notification'"
        ).fetchone()[0]

    assert ApprovalRequest.model_validate_json(approval_payload).status == ApprovalStatus.REQUESTED
    assert audit_count == 0
    assert notification_count == 0


@pytest.mark.parametrize("project_workspace_id", [None, "workspace-b"])
def test_decision_refuses_explicit_approval_workspace_mismatch_with_project(
    tmp_path: Path,
    project_workspace_id: str | None,
) -> None:
    database = tmp_path / f"approval_project_workspace_{project_workspace_id}.sqlite3"
    approval = _approval().model_copy(update={"workspace_id": "workspace-a"})
    _seed_database(database, approval)
    with sqlite3.connect(database) as connection:
        project = Project(
            id=approval.project_id,
            name="Repository test project",
            workspace_id=project_workspace_id,
        )
        connection.execute(
            "UPDATE kv_store SET payload = ? WHERE entity_type = ? AND entity_id = ?",
            (project.model_dump_json(), "project", project.id),
        )
        if project_workspace_id is not None:
            workspace = Workspace(
                id=project_workspace_id,
                name="Repository test workspace",
                owner_user_id="user_repo_test",
            )
            connection.execute(
                "INSERT INTO kv_store(entity_type, entity_id, payload) VALUES (?, ?, ?)",
                ("workspace", workspace.id, workspace.model_dump_json()),
            )
    updated = _approved_decision(approval)

    with pytest.raises(ApprovalDecisionConflict, match="workspace does not match"):
        _repository(database).commit_decision(
            approval_id=approval.id,
            expected_status=ApprovalStatus.REQUESTED,
            expected_approval=approval,
            updated_approval=updated,
            audit_event=_audit(approval, f"project-workspace-{project_workspace_id}"),
            decision_notification=_notification(approval, f"project-workspace-{project_workspace_id}"),
        )

    with sqlite3.connect(database) as connection:
        approval_payload = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type = ? AND entity_id = ?",
            ("approval_request", approval.id),
        ).fetchone()[0]
        audit_count = connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type = 'audit_event'"
        ).fetchone()[0]

    assert ApprovalRequest.model_validate_json(approval_payload).status == ApprovalStatus.REQUESTED
    assert audit_count == 0


def test_decision_refuses_project_with_missing_workspace_record(tmp_path: Path) -> None:
    database = tmp_path / "approval_missing_workspace.sqlite3"
    approval = _approval()
    _seed_database(database, approval)
    project = Project(
        id=approval.project_id,
        name="Dangling workspace project",
        workspace_id="workspace-missing",
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE kv_store SET payload = ? WHERE entity_type = ? AND entity_id = ?",
            (project.model_dump_json(), "project", project.id),
        )
    updated = _approved_decision(approval)

    with pytest.raises(ApprovalDecisionConflict, match="project workspace is missing"):
        _repository(database).commit_decision(
            approval_id=approval.id,
            expected_status=ApprovalStatus.REQUESTED,
            expected_approval=approval,
            updated_approval=updated,
            audit_event=_audit(approval, "missing-workspace"),
            decision_notification=Notification(
                id="notification-missing-workspace",
                title="Approval decision recorded",
                body="Review the decision.",
                entity_type="approval_request",
                entity_id=approval.id,
                workspace_id="workspace-missing",
            ),
        )

    with sqlite3.connect(database) as connection:
        approval_payload = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type = ? AND entity_id = ?",
            ("approval_request", approval.id),
        ).fetchone()[0]
        audit_count = connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type = 'audit_event'"
        ).fetchone()[0]
        notification_count = connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type = 'notification'"
        ).fetchone()[0]

    assert ApprovalRequest.model_validate_json(approval_payload).status == ApprovalStatus.REQUESTED
    assert audit_count == 0
    assert notification_count == 0


def test_notification_enqueue_failure_rolls_back_decision_audit_and_task(tmp_path: Path) -> None:
    database = tmp_path / "approval_notification_rollback.sqlite3"
    approval = _approval()
    _seed_database(database, approval)
    task = AgentTask(
        project_id=approval.project_id,
        title="Linked task",
        goal="Run only after approval",
        approval_request_id=approval.id,
        status=AgentTaskStatus.WAITING_FOR_APPROVAL,
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO kv_store(entity_type, entity_id, payload) VALUES (?, ?, ?)",
            ("agent_task", task.id, task.model_dump_json()),
        )
        connection.execute(
            "CREATE TRIGGER reject_decision_notification BEFORE INSERT ON kv_store "
            "WHEN NEW.entity_type = 'notification' "
            "BEGIN SELECT RAISE(ABORT, 'notification enqueue unavailable'); END"
        )
    updated = _approved_decision(approval)

    with pytest.raises(sqlite3.IntegrityError, match="notification enqueue unavailable"):
        _repository(database).commit_decision(
            approval_id=approval.id,
            expected_status=ApprovalStatus.REQUESTED,
            expected_approval=approval,
            updated_approval=updated,
            audit_event=_audit(approval, "notification-rollback"),
            decision_notification=_notification(approval, "notification-rollback"),
        )

    with sqlite3.connect(database) as connection:
        approval_payload = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type = ? AND entity_id = ?",
            ("approval_request", approval.id),
        ).fetchone()[0]
        task_payload = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type = ? AND entity_id = ?",
            ("agent_task", task.id),
        ).fetchone()[0]
        audit_count = connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type = 'audit_event'"
        ).fetchone()[0]
        notification_count = connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type = 'notification'"
        ).fetchone()[0]
    assert ApprovalRequest.model_validate_json(approval_payload).status == ApprovalStatus.REQUESTED
    assert AgentTask.model_validate_json(task_payload).status == AgentTaskStatus.WAITING_FOR_APPROVAL
    assert audit_count == 0
    assert notification_count == 0


def test_stale_snapshot_cannot_overwrite_durable_approval_fields(tmp_path: Path) -> None:
    database = tmp_path / "approval_stale_snapshot.sqlite3"
    approval = _approval()
    _seed_database(database, approval)
    concurrent_update = approval.model_copy(update={"summary": "Concurrent policy update"})
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE kv_store SET payload = ? WHERE entity_type = ? AND entity_id = ?",
            (concurrent_update.model_dump_json(), "approval_request", approval.id),
        )
    stale_decision = _approved_decision(approval)

    with pytest.raises(ApprovalDecisionConflict, match="changed since the decision snapshot"):
        _repository(database).commit_decision(
            approval_id=approval.id,
            expected_status=ApprovalStatus.REQUESTED,
            expected_approval=approval,
            updated_approval=stale_decision,
            audit_event=_audit(approval, "stale"),
            decision_notification=_notification(approval, "stale"),
        )

    with sqlite3.connect(database) as connection:
        payload = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type = ? AND entity_id = ?",
            ("approval_request", approval.id),
        ).fetchone()[0]
        audit_count = connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type = 'audit_event'"
        ).fetchone()[0]
    durable = ApprovalRequest.model_validate_json(payload)
    assert durable.status == ApprovalStatus.REQUESTED
    assert durable.summary == "Concurrent policy update"
    assert audit_count == 0


@pytest.mark.parametrize(
    ("audit_previous", "audit_new"),
    [
        (ApprovalStatus.APPROVED, ApprovalStatus.APPROVED),
        (ApprovalStatus.REQUESTED, ApprovalStatus.DENIED),
    ],
)
def test_repository_rejects_audit_statuses_that_do_not_match_transition(
    tmp_path: Path,
    audit_previous: ApprovalStatus,
    audit_new: ApprovalStatus,
) -> None:
    database = tmp_path / f"audit_mismatch_{audit_previous}_{audit_new}.sqlite3"
    approval = _approval()
    _seed_database(database, approval)
    updated = _approved_decision(approval)

    with pytest.raises(ValueError, match="Approval audit statuses must match"):
        _repository(database).commit_decision(
            approval_id=approval.id,
            expected_status=ApprovalStatus.REQUESTED,
            expected_approval=approval,
            updated_approval=updated,
            audit_event=_audit(
                approval,
                "mismatch",
                previous_status=audit_previous,
                new_status=audit_new,
            ),
            decision_notification=_notification(approval, "mismatch"),
        )

    with sqlite3.connect(database) as connection:
        payload = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type = ? AND entity_id = ?",
            ("approval_request", approval.id),
        ).fetchone()[0]
        audit_count = connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type = 'audit_event'"
        ).fetchone()[0]
    assert ApprovalRequest.model_validate_json(payload).status == ApprovalStatus.REQUESTED
    assert audit_count == 0


@pytest.mark.parametrize(
    ("expected_status", "updated_status"),
    [
        (ApprovalStatus.REQUESTED, ApprovalStatus.REQUESTED),
        (ApprovalStatus.APPROVED, ApprovalStatus.DENIED),
    ],
)
def test_repository_rejects_invalid_or_noop_lifecycle_transitions(
    tmp_path: Path,
    expected_status: ApprovalStatus,
    updated_status: ApprovalStatus,
) -> None:
    database = tmp_path / f"invalid_transition_{expected_status}_{updated_status}.sqlite3"
    approval = _approval().model_copy(update={"status": expected_status})
    _seed_database(database, approval)
    updated = approval.model_copy(update={"status": updated_status})

    with pytest.raises(ValueError, match="Invalid approval transition"):
        _repository(database).commit_decision(
            approval_id=approval.id,
            expected_status=expected_status,
            expected_approval=approval,
            updated_approval=updated,
            audit_event=_audit(
                approval,
                "invalid",
                previous_status=expected_status,
                new_status=updated_status,
            ),
            decision_notification=_notification(approval, "invalid"),
        )

    with sqlite3.connect(database) as connection:
        payload = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type = ? AND entity_id = ?",
            ("approval_request", approval.id),
        ).fetchone()[0]
        audit_count = connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type = 'audit_event'"
        ).fetchone()[0]
    assert ApprovalRequest.model_validate_json(payload).status == expected_status
    assert audit_count == 0


def test_separate_connections_allow_only_one_terminal_decision(tmp_path: Path) -> None:
    database = tmp_path / "approval_race.sqlite3"
    approval = _approval()
    _seed_database(database, approval)
    barrier = Barrier(2)

    def commit(status: ApprovalStatus, suffix: str) -> str:
        updated = (
            _approved_decision(approval)
            if status == ApprovalStatus.APPROVED
            else approval.model_copy(update={"status": status})
        )
        barrier.wait(timeout=3)
        try:
            _repository(database).commit_decision(
                approval_id=approval.id,
                expected_status=ApprovalStatus.REQUESTED,
                expected_approval=approval,
                updated_approval=updated,
                audit_event=_audit(approval, suffix, new_status=status),
                decision_notification=_notification(approval, suffix),
            )
        except ApprovalDecisionConflict:
            return "conflict"
        return "committed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            pool.submit(commit, ApprovalStatus.APPROVED, "approve"),
            pool.submit(commit, ApprovalStatus.DENIED, "deny"),
        ]
        outcomes = [result.result(timeout=5) for result in results]

    assert sorted(outcomes) == ["committed", "conflict"]
    with sqlite3.connect(database) as connection:
        payload = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type = ? AND entity_id = ?",
            ("approval_request", approval.id),
        ).fetchone()[0]
        audit_count = connection.execute(
            "SELECT COUNT(*) FROM kv_store WHERE entity_type = 'audit_event'"
        ).fetchone()[0]
    assert ApprovalRequest.model_validate_json(payload).status in {
        ApprovalStatus.APPROVED,
        ApprovalStatus.DENIED,
    }
    assert audit_count == 1


def _delivery_approval(
    approval_id: str,
    workspace_id: str,
    agent_id: str,
    invocation_id: str,
    **changes: object,
) -> ApprovalRequest:
    approval = _approval().model_copy(
        update={
            "id": approval_id,
            "project_id": f"project-{approval_id}",
            "workspace_id": workspace_id,
            "source_agent_id": agent_id,
            "source_invocation_id": invocation_id,
            **changes,
        }
    )
    return _approved_decision(approval)


def test_sqlite_delivery_outcomes_are_batched_scoped_and_explicit(tmp_path: Path) -> None:
    database = tmp_path / "approval_delivery_evidence.sqlite3"
    claimed = _delivery_approval(
        "approval-claim", "workspace-a", "agent-a", "invocation-a"
    )
    failed = _delivery_approval(
        "approval-failed", "workspace-a", "agent-a", "invocation-b"
    )
    foreign = _delivery_approval(
        "approval-foreign", "workspace-b", "agent-b", "invocation-c"
    )
    _seed_database(database, claimed)
    with sqlite3.connect(database) as connection:
        connection.executemany(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            (
                ("approval_request", approval.id, approval.model_dump_json())
                for approval in (failed, foreign)
            ),
        )
    claimed_at = now_utc()
    with sqlite3.connect(database) as connection:
        for approval, agent_id, invocation_id in (
            (claimed, "agent-a", "invocation-a"),
            (failed, "agent-a", "invocation-b"),
            (foreign, "agent-b", "invocation-c"),
        ):
            connection.execute(
                "INSERT INTO approval_delivery_claims "
                "(approval_id,workspace_id,source_agent_id,source_invocation_id,claimed_at) "
                "VALUES(?,?,?,?,?)",
                (
                    approval.id,
                    approval.workspace_id,
                    agent_id,
                    invocation_id,
                    claimed_at.isoformat(),
                ),
            )
        connection.execute(
            "INSERT INTO approval_delivery_failures "
            "(approval_id,workspace_id,source_agent_id,source_invocation_id,reason_code,reported_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                failed.id,
                "workspace-a",
                "agent-a",
                "invocation-b",
                "CLAIM_RESPONSE_AMBIGUOUS",
                (claimed_at + timedelta(seconds=1)).isoformat(),
            ),
        )

    outcomes = _repository(database).list_delivery_outcomes(
        workspace_id="workspace-a",
        approval_ids=[claimed.id, failed.id, "approval-without-evidence"],
    )

    assert outcomes == {
        claimed.id: "CONSUMED",
        failed.id: "DELIVERY_UNCONFIRMED",
        "approval-without-evidence": "NOT_CONFIRMED",
    }
    with pytest.raises(ApprovalDeliveryEvidenceIntegrityError, match="workspace"):
        _repository(database).list_delivery_outcomes(
            workspace_id="workspace-a", approval_ids=[foreign.id]
        )


def test_sqlite_delivery_outcomes_distinguish_waiting_expired_and_non_applicable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "approval_delivery_states.sqlite3"
    waiting = _delivery_approval(
        "approval-waiting", "workspace-a", "agent-a", "invocation-w",
        expires_at=now_utc() + timedelta(minutes=5),
    )
    expired = _delivery_approval(
        "approval-expired", "workspace-a", "agent-a", "invocation-e",
        expires_at=now_utc() - timedelta(seconds=1),
    )
    denied = _delivery_approval(
        "approval-denied", "workspace-a", "agent-a", "invocation-d",
        expires_at=now_utc() + timedelta(minutes=5),
    ).model_copy(
        update={"status": ApprovalStatus.DENIED}
    )
    _seed_database(database, waiting)
    with sqlite3.connect(database) as connection:
        connection.executemany(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            (("approval_request", item.id, item.model_dump_json()) for item in (expired, denied)),
        )

    outcomes = _repository(database).list_delivery_outcomes(
        workspace_id="workspace-a", approval_ids=[waiting.id, expired.id, denied.id]
    )

    assert outcomes == {
        waiting.id: "AWAITING_DELIVERY",
        expired.id: "EXPIRED",
        denied.id: "NOT_APPLICABLE",
    }


def test_sqlite_delivery_outcomes_reject_detached_claim_rows(tmp_path: Path) -> None:
    database = tmp_path / "approval_detached_delivery_evidence.sqlite3"
    approval = _delivery_approval(
        "approval-detached", "workspace-detached", "agent-detached", "invocation-original"
    )
    _seed_database(database, approval)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO approval_delivery_claims "
            "(approval_id,workspace_id,source_agent_id,source_invocation_id,claimed_at) "
            "VALUES(?,?,?,?,?)",
            (
                approval.id,
                approval.workspace_id,
                approval.source_agent_id,
                approval.source_invocation_id,
                now_utc().isoformat(),
            ),
        )
        connection.execute(
            "UPDATE approval_delivery_claims SET source_invocation_id=? WHERE approval_id=?",
            ("detached-invocation", approval.id),
        )

    with pytest.raises(ApprovalDeliveryEvidenceIntegrityError, match="does not match its approval"):
        _repository(database).list_delivery_outcomes(
            workspace_id="workspace-detached", approval_ids=[approval.id]
        )


@pytest.mark.parametrize(
    "payload_changes",
    (
        {"approved_payload_hash": "0" * 64},
        {"status": ApprovalStatus.REQUESTED},
    ),
)
def test_sqlite_delivery_outcomes_reject_inconsistent_approved_claim(
    tmp_path: Path, payload_changes: dict[str, object]
) -> None:
    database = tmp_path / "approval_changed_delivery_payload.sqlite3"
    approval = _delivery_approval(
        "approval-changed-payload", "workspace-payload", "agent-payload", "invocation-payload"
    )
    _seed_database(database, approval)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO approval_delivery_claims "
            "(approval_id,workspace_id,source_agent_id,source_invocation_id,claimed_at) "
            "VALUES(?,?,?,?,?)",
            (
                approval.id,
                approval.workspace_id,
                approval.source_agent_id,
                approval.source_invocation_id,
                now_utc().isoformat(),
            ),
        )
        connection.execute(
            "UPDATE kv_store SET payload=? WHERE entity_type='approval_request' AND entity_id=?",
            (
                approval.model_copy(update=payload_changes).model_dump_json(),
                approval.id,
            ),
        )

    with pytest.raises(ApprovalDeliveryEvidenceIntegrityError, match="does not match its approval"):
        _repository(database).list_delivery_outcomes(
            workspace_id=approval.workspace_id, approval_ids=[approval.id]
        )


def test_sqlite_delivery_outcomes_map_storage_errors_to_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "approval_delivery_evidence_unavailable.sqlite3"
    repository = _repository(database)

    def fail_connect():
        raise sqlite3.OperationalError("private database detail")

    monkeypatch.setattr(repository, "_connect", fail_connect)

    with pytest.raises(ApprovalDeliveryEvidenceUnavailable) as error:
        repository.list_delivery_outcomes(
            workspace_id="workspace-one", approval_ids=["approval-one"]
        )

    assert "private database detail" not in str(error.value)
