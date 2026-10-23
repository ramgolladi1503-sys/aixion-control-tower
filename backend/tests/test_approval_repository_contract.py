from __future__ import annotations

import hashlib
import os
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from threading import Barrier, Event

import psycopg
import pytest
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app.agent_task_models import (
    AgentTask,
    AgentTaskEvent,
    AgentTaskEventType,
    AgentTaskStatus,
)
from app.agent_credential_models import AgentCredentialRecord
from app.agent_task_repository import PostgresAgentTaskRepository, SQLiteAgentTaskRepository
from app.agent_run_repository import PostgresAgentRunRepository
from app.approval_integrity import compute_approval_payload_hash
from app.approval_expiry import expire_approval
from app.approval_repository import (
    ApprovalDecisionConflict,
    ApprovalDecisionRepository,
    ApprovalDeliveryCredentialRejected,
    ApprovalInvocationLeaseRejected,
    PostgresApprovalDecisionRepository,
    SQLiteApprovalDecisionRepository,
)
from app.database_migrations import run_migrations
from app.models import (
    AgentAuthType,
    AgentProvider,
    ApprovalRequest,
    ApprovalStatus,
    AuditEvent,
    ExternalAgent,
    FileChange,
    Notification,
    Project,
    RiskAssessment,
    RiskLevel,
    Workspace,
    now_utc,
)
from app.postgres_database import run_postgres_migrations
from app.settings import get_settings


@dataclass(frozen=True)
class ApprovalRepositoryCase:
    backend: str
    connect: Callable[[], object]
    repository: ApprovalDecisionRepository
    task_repository: SQLiteAgentTaskRepository | PostgresAgentTaskRepository
    run_repository: PostgresAgentRunRepository | None
    workspace: Workspace
    project: Project


@pytest.fixture(params=("sqlite", "postgres"), ids=("sqlite", "postgres"))
def approval_case(request, tmp_path: Path) -> Iterator[ApprovalRepositoryCase]:
    backend = request.param
    suffix = uuid.uuid4().hex
    workspace = Workspace(
        id=f"workspace_contract_{suffix}",
        name="Approval repository contract workspace",
        owner_user_id=f"user_contract_{suffix}",
    )
    project = Project(
        id=f"project_contract_{suffix}",
        name="Approval repository contract project",
        workspace_id=workspace.id,
    )

    if backend == "sqlite":
        database = tmp_path / f"approval-contract-{suffix}.sqlite3"

        def connect() -> sqlite3.Connection:
            connection = sqlite3.connect(database, timeout=10)
            connection.execute("PRAGMA busy_timeout = 10000")
            return connection

        with connect() as connection:
            run_migrations(connection)
            connection.executemany(
                "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
                (
                    ("workspace", workspace.id, workspace.model_dump_json()),
                    ("project", project.id, project.model_dump_json()),
                ),
            )
        yield ApprovalRepositoryCase(
            backend=backend,
            connect=connect,
            repository=SQLiteApprovalDecisionRepository(connect),
            task_repository=SQLiteAgentTaskRepository(connect),
            run_repository=None,
            workspace=workspace,
            project=project,
        )
        return

    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL contract cases")
    schema = f"ct_approval_contract_{suffix}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    try:
        run_postgres_migrations(connect)
        with connect() as connection:
            connection.execute(
                "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
                (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
            )
            connection.execute(
                "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
                (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))),
            )
        yield ApprovalRepositoryCase(
            backend=backend,
            connect=connect,
            repository=PostgresApprovalDecisionRepository(connect),
            task_repository=PostgresAgentTaskRepository(connect),
            run_repository=PostgresAgentRunRepository(connect),
            workspace=workspace,
            project=project,
        )
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _approval(case: ApprovalRepositoryCase, **changes: object) -> ApprovalRequest:
    values: dict[str, object] = dict(
        id=f"approval_contract_{uuid.uuid4().hex}",
        project_id=case.project.id,
        workspace_id=case.workspace.id,
        title="Approval contract request",
        summary="Verify the shared decision repository contract.",
        target_branch="feature/approval-contract",
        files=[FileChange(path="README.md", diff="+contract")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.REQUESTED,
    )
    values.update(changes)
    return ApprovalRequest(**values)


def _create(case: ApprovalRepositoryCase, approval: ApprovalRequest) -> None:
    created = now_utc()
    case.repository.create(
        approval=approval,
        audit_event=AuditEvent(
            event_type="approval.created",
            entity_id=approval.id,
            workspace_id=approval.workspace_id,
            actor="contract-test",
            created_at=created,
        ),
        notification=Notification(
            title="Approval created",
            body="Review the request.",
            entity_type="approval_request",
            entity_id=approval.id,
            workspace_id=approval.workspace_id,
            created_at=created,
        ),
        invocation_lease_ttl_seconds=(60 if approval.source_invocation_id else None),
    )


def _store_task(case: ApprovalRepositoryCase, task: AgentTask) -> None:
    if case.backend == "sqlite":
        with case.connect() as connection:
            connection.execute(
                "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
                ("agent_task", task.id, task.model_dump_json()),
            )
        return
    with case.connect() as connection:
        connection.execute(
            "INSERT INTO agent_tasks(task_id,project_id,workspace_id,approval_request_id,status,updated_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (
                task.id,
                task.project_id,
                task.workspace_id,
                task.approval_request_id,
                task.status.value,
                task.updated_at,
                Jsonb(task.model_dump(mode="json")),
            ),
        )


def _store_external_agent(case: ApprovalRepositoryCase, agent: ExternalAgent) -> str:
    token = "contract-test-agent-token"
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    agent = agent.model_copy(update={"secret_hash": token_hash})
    credential = AgentCredentialRecord(agent_id=agent.id)
    if case.backend == "sqlite":
        with case.connect() as connection:
            connection.executemany(
                "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
                (
                    ("external_agent", agent.id, agent.model_dump_json()),
                    (
                        "external_agent_credential",
                        agent.id,
                        credential.model_dump_json(),
                    ),
                ),
            )
        return token_hash
    with case.connect() as connection:
        connection.execute(
            "INSERT INTO external_agents "
            "(agent_id,workspace_id,created_by_user_id,provider,auth_type,enabled,secret_hash,payload) "
            "VALUES(%s,%s,NULL,%s,%s,%s,%s,%s)",
            (
                agent.id,
                agent.workspace_id,
                agent.provider.value,
                agent.auth_type.value,
                agent.enabled,
                token_hash,
                Jsonb(agent.model_dump(mode="json")),
            ),
        )
        connection.execute(
            "INSERT INTO external_agent_credentials "
            "(credential_id,agent_id,workspace_id,token_expires_at,token_revoked_at,"
            "token_rotated_at,last_used_at,failed_auth_count,rate_limit_per_minute,"
            "rate_limit_window_started_at,rate_limit_request_count,created_at,updated_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                credential.id, agent.id, agent.workspace_id,
                credential.token_expires_at, credential.token_revoked_at,
                credential.token_rotated_at, credential.last_used_at,
                credential.failed_auth_count, credential.rate_limit_per_minute,
                credential.rate_limit_window_started_at,
                credential.rate_limit_request_count, credential.created_at,
                credential.updated_at, Jsonb(credential.model_dump(mode="json")),
            ),
        )
    return token_hash


def _linked_task_records(
    case: ApprovalRepositoryCase, approval: ApprovalRequest
) -> tuple[AgentTask, AgentTask, AgentTaskEvent, AuditEvent, Notification]:
    initial_task = AgentTask(
        id=f"task_create_contract_{uuid.uuid4().hex}",
        project_id=approval.project_id,
        workspace_id=approval.workspace_id,
        title="Task awaiting approval creation",
        goal="The task must only link after the durable approval transaction.",
        status=AgentTaskStatus.RECEIVED,
    )
    linked_task = initial_task.model_copy(
        update={
            "approval_request_id": approval.id,
            "status": AgentTaskStatus.WAITING_FOR_APPROVAL,
            "updated_at": now_utc(),
        }
    )
    actor = "approval-contract-test"
    task_event = AgentTaskEvent(
        task_id=initial_task.id,
        event_type=AgentTaskEventType.APPROVAL_CREATED,
        message="Approval request created.",
        status=AgentTaskStatus.WAITING_FOR_APPROVAL,
        actor=actor,
        metadata={"approval_request_id": approval.id},
    )
    task_audit = AuditEvent(
        id=f"audit_task_create_contract_{uuid.uuid4().hex}",
        event_type="agent_task.approval_created",
        entity_id=initial_task.id,
        workspace_id=approval.workspace_id,
        actor=actor,
        details={"approval_request_id": approval.id},
    )
    notification = Notification(
        id=f"notification_task_create_contract_{uuid.uuid4().hex}",
        title="Task approval created",
        body="Review the linked approval.",
        entity_type="agent_task",
        entity_id=initial_task.id,
        workspace_id=approval.workspace_id,
    )
    _store_task(case, initial_task)
    return initial_task, linked_task, task_event, task_audit, notification


def _store_notification(case: ApprovalRepositoryCase, notification: Notification) -> None:
    if case.backend == "sqlite":
        with case.connect() as connection:
            connection.execute(
                "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
                ("notification", notification.id, notification.model_dump_json()),
            )
        return
    with case.connect() as connection:
        connection.execute(
            "INSERT INTO notifications "
            "(notification_id,workspace_id,user_id,approval_id,task_id,entity_type,status,attempts,"
            "next_attempt_at,lease_expires_at,created_at,payload) "
            "VALUES(%s,%s,NULL,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                notification.id,
                notification.workspace_id,
                notification.entity_id
                if notification.entity_type == "approval_request"
                else None,
                notification.entity_id
                if notification.entity_type == "agent_task"
                else None,
                notification.entity_type,
                notification.push_status,
                notification.push_attempts,
                notification.push_next_attempt_at,
                notification.push_lease_expires_at,
                notification.created_at,
                Jsonb(notification.model_dump(mode="json")),
            ),
        )


def _count(case: ApprovalRepositoryCase, entity_type: str) -> int:
    if case.backend == "sqlite":
        with case.connect() as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM kv_store WHERE entity_type=?",
                (entity_type,),
            ).fetchone()[0]
    table = {
        "approval_request": "approval_requests",
        "audit_event": "audit_events",
        "notification": "notifications",
        "agent_task_event": "agent_task_events",
    }[entity_type]
    with case.connect() as connection:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _decision_audit(
    approval: ApprovalRequest, updated: ApprovalRequest, suffix: str
) -> AuditEvent:
    return AuditEvent(
        id=f"audit_contract_{suffix}",
        event_type="approval.decision",
        entity_id=approval.id,
        workspace_id=approval.workspace_id,
        actor="contract-reviewer",
        details={
            "previous_status": approval.status,
            "new_status": updated.status,
            "approved_payload_hash": updated.approved_payload_hash,
        },
    )


def test_create_and_read_are_workspace_scoped_across_adapters(approval_case) -> None:
    approval = _approval(approval_case)
    _create(approval_case, approval)

    assert approval_case.repository.get(approval.id) == approval
    assert approval_case.repository.list_for_workspace(approval_case.workspace.id) == [approval]
    assert approval_case.repository.list_for_workspace("workspace_not_this_one") == []


def test_agent_task_approval_creation_commits_all_records_across_adapters(
    approval_case,
) -> None:
    approval = _approval(approval_case)
    initial_task, linked_task, task_event, task_audit, task_notification = (
        _linked_task_records(approval_case, approval)
    )
    approval_audit = AuditEvent(
        event_type="approval.created_by_agent",
        entity_id=approval.id,
        workspace_id=approval.workspace_id,
        actor="agent:contract-installation",
    )

    approval_case.repository.create_for_agent_task(
        approval=approval,
        audit_event=approval_audit,
        task=linked_task,
        task_event=task_event,
        task_audit_event=task_audit,
        notification=task_notification,
    )

    assert approval_case.repository.get(approval.id) == approval
    assert approval_case.task_repository.get(initial_task.id) == linked_task
    assert approval_case.task_repository.list_events(
        initial_task.id, workspace_id=approval.workspace_id
    ) == [task_event]
    assert _count(approval_case, "audit_event") == 2
    assert _count(approval_case, "notification") == 1


def test_agent_task_approval_creation_rolls_back_every_write_on_outbox_conflict(
    approval_case,
) -> None:
    approval = _approval(approval_case)
    initial_task, linked_task, task_event, task_audit, notification = _linked_task_records(
        approval_case, approval
    )
    if approval_case.backend == "sqlite":
        with approval_case.connect() as connection:
            connection.execute(
                f"CREATE TRIGGER fail_contract_outbox BEFORE INSERT ON kv_store "
                f"WHEN NEW.entity_type='notification' AND NEW.entity_id='{notification.id}' "
                "BEGIN SELECT RAISE(ABORT,'forced contract outbox failure'); END"
            )
    else:
        conflict_notification = notification.model_copy(
            update={"id": "notification_forced_conflict"}
        )
        _store_notification(approval_case, conflict_notification)
    approval_audit = AuditEvent(
        event_type="approval.created_by_agent",
        entity_id=approval.id,
        workspace_id=approval.workspace_id,
        actor="agent:contract-installation",
    )
    failing_task_notification = notification.model_copy(
        update={"id": conflict_notification.id}
        if approval_case.backend == "postgres"
        else {}
    )

    expected_error = (
        sqlite3.IntegrityError if approval_case.backend == "sqlite" else ApprovalDecisionConflict
    )
    with pytest.raises(expected_error):
        approval_case.repository.create_for_agent_task(
            approval=approval,
            audit_event=approval_audit,
            task=linked_task,
            task_event=task_event,
            task_audit_event=task_audit,
            notification=failing_task_notification,
        )

    assert approval_case.repository.get(approval.id) is None
    assert approval_case.task_repository.get(initial_task.id) == initial_task
    assert approval_case.task_repository.list_events(
        initial_task.id, workspace_id=approval.workspace_id
    ) == []
    assert _count(approval_case, "audit_event") == 0
    assert _count(approval_case, "notification") == int(approval_case.backend == "postgres")


@pytest.mark.parametrize(
    ("decision_status", "task_status"),
    (
        (ApprovalStatus.APPROVED, AgentTaskStatus.APPROVED),
        (ApprovalStatus.DENIED, AgentTaskStatus.DENIED),
        (ApprovalStatus.REVISION_REQUESTED, AgentTaskStatus.PLANNING),
    ),
)
def test_decision_commits_linked_task_audit_and_notifications_across_adapters(
    approval_case, decision_status, task_status
) -> None:
    approval = _approval(approval_case)
    _create(approval_case, approval)
    task = AgentTask(
        id=f"task_contract_{uuid.uuid4().hex}",
        project_id=approval.project_id,
        workspace_id=approval.workspace_id,
        title="Wait for review",
        goal="Continue only after the approval decision.",
        approval_request_id=approval.id,
        status=AgentTaskStatus.WAITING_FOR_APPROVAL,
    )
    _store_task(approval_case, task)
    decision_updates = {"status": decision_status}
    if decision_status == ApprovalStatus.APPROVED:
        decision_updates.update(
            {"approved_payload_hash_version": 2, "approved_at": now_utc()}
        )
    updated = approval.model_copy(update=decision_updates)
    if decision_status == ApprovalStatus.APPROVED:
        updated = updated.model_copy(
            update={"approved_payload_hash": compute_approval_payload_hash(updated)}
        )

    result = approval_case.repository.commit_decision(
        approval_id=approval.id,
        expected_status=ApprovalStatus.REQUESTED,
        expected_approval=approval,
        updated_approval=updated,
        audit_event=_decision_audit(approval, updated, uuid.uuid4().hex),
        decision_notification=Notification(
            title="Decision recorded",
            body="The approval was approved.",
            entity_type="approval_request",
            entity_id=approval.id,
            workspace_id=approval.workspace_id,
        ),
        run_repository=approval_case.run_repository,
    )

    persisted_task = approval_case.task_repository.get(task.id)
    assert result.linked_task is not None
    assert result.linked_task.status == task_status
    assert persisted_task == result.linked_task
    assert len(approval_case.task_repository.list_events(task.id, workspace_id=task.workspace_id)) == 1
    assert approval_case.repository.get(approval.id) == updated
    expected_audits = (
        5
        if approval_case.backend == "postgres" and decision_status == ApprovalStatus.APPROVED
        else 3
    )
    assert _count(approval_case, "audit_event") == expected_audits
    assert _count(approval_case, "notification") == 3


def test_duplicate_stale_decision_cannot_replace_winner_or_duplicate_side_effects(
    approval_case,
) -> None:
    approval = _approval(approval_case)
    _create(approval_case, approval)
    winner = approval.model_copy(
        update={
            "status": ApprovalStatus.APPROVED,
            "approved_payload_hash_version": 2,
            "approved_at": now_utc(),
        }
    )
    winner = winner.model_copy(
        update={"approved_payload_hash": compute_approval_payload_hash(winner)}
    )
    approval_case.repository.commit_decision(
        approval_id=approval.id,
        expected_status=ApprovalStatus.REQUESTED,
        expected_approval=approval,
        updated_approval=winner,
        audit_event=_decision_audit(approval, winner, "winning-decision"),
        decision_notification=Notification(
            title="Decision recorded",
            body="The winning reviewer decision was recorded.",
            entity_type="approval_request",
            entity_id=approval.id,
            workspace_id=approval.workspace_id,
        ),
    )
    stale_loser = approval.model_copy(update={"status": ApprovalStatus.DENIED})

    with pytest.raises(ApprovalDecisionConflict):
        approval_case.repository.commit_decision(
            approval_id=approval.id,
            expected_status=ApprovalStatus.REQUESTED,
            expected_approval=approval,
            updated_approval=stale_loser,
            audit_event=_decision_audit(approval, stale_loser, "losing-decision"),
            decision_notification=Notification(
                title="Decision recorded",
                body="This stale reviewer decision must not be recorded.",
                entity_type="approval_request",
                entity_id=approval.id,
                workspace_id=approval.workspace_id,
            ),
        )

    assert approval_case.repository.get(approval.id) == winner
    assert _count(approval_case, "audit_event") == 2
    assert _count(approval_case, "notification") == 2


def test_decision_commit_rejects_deadline_elapsed_after_pending_snapshot_across_adapters(
    approval_case,
) -> None:
    approval = _approval(
        approval_case,
        expires_at=now_utc() - timedelta(seconds=1),
    )
    _create(approval_case, approval)
    approved = approval.model_copy(
        update={
            "status": ApprovalStatus.APPROVED,
            "approved_payload_hash_version": 2,
            "approved_at": now_utc(),
        }
    )
    approved = approved.model_copy(
        update={"approved_payload_hash": compute_approval_payload_hash(approved)}
    )

    with pytest.raises(ApprovalDecisionConflict, match="deadline elapsed"):
        approval_case.repository.commit_decision(
            approval_id=approval.id,
            expected_status=ApprovalStatus.REQUESTED,
            expected_approval=approval,
            updated_approval=approved,
            audit_event=_decision_audit(approval, approved, uuid.uuid4().hex),
            decision_notification=Notification(
                title="Decision recorded",
                body="A late approval must not commit.",
                entity_type="approval_request",
                entity_id=approval.id,
                workspace_id=approval.workspace_id,
            ),
        )

    assert approval_case.repository.get(approval.id) == approval
    assert _count(approval_case, "audit_event") == 1
    assert _count(approval_case, "notification") == 1


def test_expiry_wins_concurrent_late_decision_race_across_adapters(approval_case) -> None:
    approval = _approval(
        approval_case,
        expires_at=now_utc() - timedelta(seconds=1),
    )
    _create(approval_case, approval)
    expired = approval.model_copy(
        update={"status": ApprovalStatus.EXPIRED, "updated_at": now_utc()}
    )
    expiry_audit = AuditEvent(
        id=f"audit_expiry_race_{uuid.uuid4().hex}",
        event_type="approval.expired",
        entity_id=approval.id,
        workspace_id=approval.workspace_id,
        actor="expiry-race-test",
        details={
            "previous_status": approval.status,
            "new_status": ApprovalStatus.EXPIRED,
            "expires_at": approval.expires_at,
        },
    )
    approved = approval.model_copy(
        update={
            "status": ApprovalStatus.APPROVED,
            "approved_payload_hash_version": 2,
            "approved_at": now_utc(),
        }
    )
    approved = approved.model_copy(
        update={"approved_payload_hash": compute_approval_payload_hash(approved)}
    )
    start = Barrier(2)

    def expire() -> str:
        start.wait(timeout=5)
        try:
            approval_case.repository.expire_pending(
                approval_id=approval.id,
                expected_approval=approval,
                expired_approval=expired,
                audit_event=expiry_audit,
            )
        except ApprovalDecisionConflict:
            return "conflict"
        return "expired"

    def decide() -> str:
        start.wait(timeout=5)
        try:
            approval_case.repository.commit_decision(
                approval_id=approval.id,
                expected_status=ApprovalStatus.REQUESTED,
                expected_approval=approval,
                updated_approval=approved,
                audit_event=_decision_audit(approval, approved, uuid.uuid4().hex),
                decision_notification=Notification(
                    title="Decision recorded",
                    body="A stale late decision must not win expiry.",
                    entity_type="approval_request",
                    entity_id=approval.id,
                    workspace_id=approval.workspace_id,
                ),
            )
        except ApprovalDecisionConflict:
            return "conflict"
        return "approved"

    with ThreadPoolExecutor(max_workers=2) as executor:
        expiry_result = executor.submit(expire)
        decision_result = executor.submit(decide)
        outcomes = (expiry_result.result(timeout=15), decision_result.result(timeout=15))

    assert outcomes.count("expired") == 1
    assert outcomes.count("approved") == 0
    assert outcomes.count("conflict") == 1
    assert approval_case.repository.get(approval.id) == expired
    assert _count(approval_case, "audit_event") == 2
    assert _count(approval_case, "notification") == 1


def test_expiration_commits_terminal_approval_and_keeps_linked_task_blocked_across_adapters(
    approval_case,
) -> None:
    deadline = now_utc() - timedelta(seconds=2)
    approval = _approval(approval_case, expires_at=deadline)
    _create(approval_case, approval)
    task = AgentTask(
        id=f"task_expiry_contract_{uuid.uuid4().hex}",
        project_id=approval.project_id,
        workspace_id=approval.workspace_id,
        title="Expired request task",
        goal="Remain blocked when review expires.",
        approval_request_id=approval.id,
        status=AgentTaskStatus.WAITING_FOR_APPROVAL,
    )
    _store_task(approval_case, task)
    expired = approval.model_copy(
        update={"status": ApprovalStatus.EXPIRED, "updated_at": now_utc()}
    )
    audit = AuditEvent(
        id=f"audit_expiry_contract_{uuid.uuid4().hex}",
        event_type="approval.expired",
        entity_id=approval.id,
        workspace_id=approval.workspace_id,
        actor="contract-deadline",
        details={
            "previous_status": approval.status,
            "new_status": ApprovalStatus.EXPIRED,
            "expires_at": deadline,
        },
    )

    result = approval_case.repository.expire_pending(
        approval_id=approval.id,
        expected_approval=approval,
        expired_approval=expired,
        audit_event=audit,
    )

    assert result.approval == expired
    assert result.linked_task is not None
    assert result.linked_task.status == AgentTaskStatus.WAITING_FOR_APPROVAL
    assert approval_case.task_repository.get(task.id) == result.linked_task
    assert approval_case.repository.get(approval.id) == expired
    if approval_case.backend == "sqlite":
        with approval_case.connect() as connection:
            payload = connection.execute(
                "SELECT payload FROM kv_store WHERE entity_type='audit_event' "
                "AND json_extract(payload,'$.event_type')='approval.expired' "
                "AND json_extract(payload,'$.entity_id')=?",
                (approval.id,),
            ).fetchone()[0]
        persisted_deadline = AuditEvent.model_validate_json(payload).details["expires_at"]
    else:
        with approval_case.connect() as connection:
            persisted_deadline = connection.execute(
                "SELECT details->>'expires_at' FROM audit_events "
                "WHERE event_type='approval.expired' AND entity_id=%s",
                (approval.id,),
            ).fetchone()[0]
    assert datetime.fromisoformat(persisted_deadline.replace("Z", "+00:00")) == deadline


def test_legacy_pending_approval_backfills_audited_deadline_once_across_adapters(
    approval_case,
) -> None:
    created_at = now_utc()
    approval = _approval(
        approval_case,
        source_agent_id=None,
        expires_at=None,
        created_at=created_at,
        updated_at=created_at,
    )
    _create(approval_case, approval)
    backfill_time = created_at + timedelta(seconds=1)
    expected_deadline = created_at + timedelta(
        seconds=get_settings().agent_approval_ttl_seconds
    )

    expired = expire_approval(
        approval,
        actor="legacy-expiry-contract",
        reason="deadline_elapsed",
        now=backfill_time,
        repository=approval_case.repository,
    )

    persisted = approval_case.repository.get(approval.id)
    assert expired is False
    assert persisted is not None
    assert persisted.status == ApprovalStatus.REQUESTED
    assert persisted.expires_at == expected_deadline
    assert persisted.updated_at == backfill_time

    if approval_case.backend == "sqlite":
        with approval_case.connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM kv_store WHERE entity_type='audit_event' "
                "AND json_extract(payload,'$.event_type')='approval.expiry_backfilled' "
                "AND json_extract(payload,'$.entity_id')=?",
                (approval.id,),
            ).fetchall()
        events = [AuditEvent.model_validate_json(row[0]) for row in rows]
    else:
        with approval_case.connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM audit_events WHERE event_type='approval.expiry_backfilled' "
                "AND entity_id=%s",
                (approval.id,),
            ).fetchall()
        events = [AuditEvent.model_validate(row[0]) for row in rows]
    assert len(events) == 1
    recorded_deadline = events[0].details["expires_at"]
    if isinstance(recorded_deadline, str):
        recorded_deadline = datetime.fromisoformat(recorded_deadline.replace("Z", "+00:00"))
    assert recorded_deadline == expected_deadline

    expire_approval(
        persisted,
        actor="legacy-expiry-contract",
        reason="deadline_elapsed",
        now=backfill_time,
        repository=approval_case.repository,
    )
    assert approval_case.repository.get(approval.id) == persisted
    assert _count(approval_case, "audit_event") == 2


def test_legacy_manual_approval_past_derived_deadline_expires_across_adapters(
    approval_case,
) -> None:
    now = now_utc()
    created_at = now - timedelta(
        seconds=get_settings().agent_approval_ttl_seconds + 1
    )
    expected_deadline = created_at + timedelta(
        seconds=get_settings().agent_approval_ttl_seconds
    )
    approval = _approval(
        approval_case,
        source_agent_id=None,
        expires_at=None,
        created_at=created_at,
        updated_at=created_at,
    )
    _create(approval_case, approval)

    expired = expire_approval(
        approval,
        actor="legacy-expiry-contract",
        reason="deadline_elapsed",
        now=now,
        repository=approval_case.repository,
    )

    persisted = approval_case.repository.get(approval.id)
    assert expired is True
    assert persisted is not None
    assert persisted.status == ApprovalStatus.EXPIRED
    assert persisted.expires_at == expected_deadline
    assert _count(approval_case, "audit_event") == 2


def test_approved_connector_delivery_claim_is_single_use_across_adapters(approval_case) -> None:
    agent_id = f"agent_contract_{uuid.uuid4().hex}"
    invocation_id = f"connector-contract:{uuid.uuid4().hex}"
    agent = ExternalAgent(
        id=agent_id,
        provider=AgentProvider.ANTIGRAVITY,
        display_name="Contract-test connector",
        auth_type=AgentAuthType.API_KEY,
        workspace_id=approval_case.workspace.id,
    )
    presented_token_hash = _store_external_agent(approval_case, agent)
    approval = _approval(
        approval_case,
        source_provider=AgentProvider.ANTIGRAVITY,
        source_agent_id=agent.id,
        source_invocation_id=invocation_id,
        status=ApprovalStatus.PENDING_REVIEW,
        expires_at=now_utc() + timedelta(minutes=5),
    )
    _create(approval_case, approval)
    approved = approval.model_copy(
        update={"status": ApprovalStatus.APPROVED, "approved_payload_hash_version": 2}
    )
    approved = approved.model_copy(
        update={"approved_payload_hash": compute_approval_payload_hash(approved)}
    )
    approval_case.repository.commit_decision(
        approval_id=approval.id,
        expected_status=ApprovalStatus.PENDING_REVIEW,
        expected_approval=approval,
        updated_approval=approved,
        audit_event=_decision_audit(approval, approved, uuid.uuid4().hex),
        decision_notification=Notification(
            title="Decision recorded",
            body="The connector may claim this invocation once.",
            entity_type="approval_request",
            entity_id=approval.id,
            workspace_id=approval.workspace_id,
        ),
    )
    claim_audit = AuditEvent(
        id=f"audit_claim_contract_{uuid.uuid4().hex}",
        event_type="approval.delivery_claimed",
        entity_id=approval.id,
        workspace_id=approval.workspace_id,
        actor=f"agent:{agent.id}",
        details={"source_invocation_id": invocation_id},
    )
    claim = {
        "approval_id": approval.id,
        "workspace_id": approval.workspace_id,
        "source_agent_id": agent.id,
        "source_invocation_id": invocation_id,
        "expected_payload_hash": approved.approved_payload_hash,
        "presented_token_hash": presented_token_hash,
        "audit_event": claim_audit,
    }

    assert approval_case.repository.claim_approved_delivery(**claim) is True
    with approval_case.connect() as connection:
        if approval_case.backend == "sqlite":
            claimed_at_value = connection.execute(
                "SELECT claimed_at FROM approval_delivery_claims WHERE approval_id=?",
                (approval.id,),
            ).fetchone()[0]
            audit_created_at_value = connection.execute(
                "SELECT json_extract(payload,'$.created_at') FROM kv_store "
                "WHERE entity_type='audit_event' AND entity_id=?",
                (claim_audit.id,),
            ).fetchone()[0]
            claimed_at = datetime.fromisoformat(claimed_at_value)
            audit_created_at = datetime.fromisoformat(audit_created_at_value)
        else:
            claimed_at = connection.execute(
                "SELECT claimed_at FROM approval_delivery_claims WHERE approval_id=%s",
                (approval.id,),
            ).fetchone()[0]
            audit_created_at = connection.execute(
                "SELECT created_at FROM audit_events WHERE event_id=%s",
                (claim_audit.id,),
            ).fetchone()[0]
    assert claimed_at == audit_created_at
    assert claimed_at > claim_audit.created_at
    assert approval_case.repository.claim_approved_delivery(
        **{**claim, "audit_event": claim_audit.model_copy(update={"id": "duplicate_claim_audit"})}
    ) is False
    assert _count(approval_case, "audit_event") == 3


def test_invocation_heartbeat_racing_delivery_claim_cannot_revive_consumed_call(
    approval_case,
) -> None:
    agent = ExternalAgent(
        id=f"agent_heartbeat_claim_race_{uuid.uuid4().hex}",
        provider=AgentProvider.ANTIGRAVITY,
        display_name="Heartbeat and claim race connector",
        auth_type=AgentAuthType.API_KEY,
        workspace_id=approval_case.workspace.id,
    )
    presented_token_hash = _store_external_agent(approval_case, agent)
    invocation_id = f"heartbeat-claim-race:{uuid.uuid4().hex}"
    approval = _approval(
        approval_case,
        source_provider=AgentProvider.ANTIGRAVITY,
        source_agent_id=agent.id,
        source_invocation_id=invocation_id,
        status=ApprovalStatus.PENDING_REVIEW,
        expires_at=now_utc() + timedelta(minutes=5),
    )
    _create(approval_case, approval)
    approved = approval.model_copy(
        update={"status": ApprovalStatus.APPROVED, "approved_payload_hash_version": 2}
    )
    approved = approved.model_copy(
        update={"approved_payload_hash": compute_approval_payload_hash(approved)}
    )
    approval_case.repository.commit_decision(
        approval_id=approval.id,
        expected_status=ApprovalStatus.PENDING_REVIEW,
        expected_approval=approval,
        updated_approval=approved,
        audit_event=_decision_audit(approval, approved, uuid.uuid4().hex),
        decision_notification=Notification(
            title="Decision recorded",
            body="The connector may claim this invocation once.",
            entity_type="approval_request",
            entity_id=approval.id,
            workspace_id=approval.workspace_id,
        ),
    )
    barrier = Barrier(2)

    def heartbeat():
        barrier.wait(timeout=5)
        try:
            return approval_case.repository.heartbeat_invocation(
                approval_id=approval.id,
                workspace_id=approval.workspace_id,
                source_agent_id=agent.id,
                source_invocation_id=invocation_id,
                presented_token_hash=presented_token_hash,
                occurred_at=now_utc(),
                ttl_seconds=60,
            )
        except (ApprovalInvocationLeaseRejected, ApprovalDeliveryCredentialRejected):
            return None

    def claim() -> bool:
        barrier.wait(timeout=5)
        return approval_case.repository.claim_approved_delivery(
            approval_id=approval.id,
            workspace_id=approval.workspace_id,
            source_agent_id=agent.id,
            source_invocation_id=invocation_id,
            expected_payload_hash=approved.approved_payload_hash,
            presented_token_hash=presented_token_hash,
            audit_event=AuditEvent(
                id=f"audit_claim_race_{uuid.uuid4().hex}",
                event_type="approval.delivery_claimed",
                entity_id=approval.id,
                workspace_id=approval.workspace_id,
                actor=f"agent:{agent.id}",
                details={"source_invocation_id": invocation_id},
            ),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        heartbeat_future = executor.submit(heartbeat)
        claim_future = executor.submit(claim)
        heartbeat_result = heartbeat_future.result(timeout=10)
        claim_result = claim_future.result(timeout=10)

    assert claim_result is True
    assert heartbeat_result is None or heartbeat_result.approval_id == approval.id
    with approval_case.connect() as connection:
        if approval_case.backend == "sqlite":
            claim_count = connection.execute(
                "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=?",
                (approval.id,),
            ).fetchone()[0]
        else:
            claim_count = connection.execute(
                "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=%s",
                (approval.id,),
            ).fetchone()[0]
    assert claim_count == 1
    with pytest.raises(ApprovalInvocationLeaseRejected):
        approval_case.repository.heartbeat_invocation(
            approval_id=approval.id,
            workspace_id=approval.workspace_id,
            source_agent_id=agent.id,
            source_invocation_id=invocation_id,
            presented_token_hash=presented_token_hash,
            occurred_at=now_utc(),
            ttl_seconds=60,
        )


def test_connector_revoked_after_authentication_cannot_claim_delivery(approval_case) -> None:
    agent = ExternalAgent(
        id=f"agent_revocation_contract_{uuid.uuid4().hex}",
        provider=AgentProvider.ANTIGRAVITY,
        display_name="Revocation race connector",
        auth_type=AgentAuthType.API_KEY,
        workspace_id=approval_case.workspace.id,
    )
    presented_token_hash = _store_external_agent(approval_case, agent)
    approval = _approval(
        approval_case,
        source_provider=AgentProvider.ANTIGRAVITY,
        source_agent_id=agent.id,
        source_invocation_id=f"connector-revocation:{uuid.uuid4().hex}",
        status=ApprovalStatus.PENDING_REVIEW,
        expires_at=now_utc() + timedelta(minutes=5),
    )
    _create(approval_case, approval)
    approved = approval.model_copy(
        update={"status": ApprovalStatus.APPROVED, "approved_payload_hash_version": 2}
    )
    approved = approved.model_copy(
        update={"approved_payload_hash": compute_approval_payload_hash(approved)}
    )
    approval_case.repository.commit_decision(
        approval_id=approval.id,
        expected_status=ApprovalStatus.PENDING_REVIEW,
        expected_approval=approval,
        updated_approval=approved,
        audit_event=_decision_audit(approval, approved, uuid.uuid4().hex),
        decision_notification=Notification(
            title="Decision recorded",
            body="The connector may claim this invocation once.",
            entity_type="approval_request",
            entity_id=approval.id,
            workspace_id=approval.workspace_id,
        ),
    )

    with approval_case.connect() as connection:
        if approval_case.backend == "sqlite":
            row = connection.execute(
                "SELECT payload FROM kv_store "
                "WHERE entity_type='external_agent_credential' AND entity_id=?",
                (agent.id,),
            ).fetchone()
            credential = AgentCredentialRecord.model_validate_json(row[0]).model_copy(
                update={"token_revoked_at": now_utc(), "updated_at": now_utc()}
            )
            connection.execute(
                "UPDATE kv_store SET payload=? "
                "WHERE entity_type='external_agent_credential' AND entity_id=?",
                (credential.model_dump_json(), agent.id),
            )
        else:
            row = connection.execute(
                "SELECT payload FROM external_agent_credentials WHERE agent_id=%s",
                (agent.id,),
            ).fetchone()
            credential = AgentCredentialRecord.model_validate(row[0]).model_copy(
                update={"token_revoked_at": now_utc(), "updated_at": now_utc()}
            )
            connection.execute(
                "UPDATE external_agent_credentials SET token_revoked_at=%s,updated_at=%s,payload=%s "
                "WHERE agent_id=%s",
                (
                    credential.token_revoked_at,
                    credential.updated_at,
                    Jsonb(credential.model_dump(mode="json")),
                    agent.id,
                ),
            )

    claim_event = AuditEvent(
        event_type="approval.delivery_claimed",
        entity_id=approval.id,
        workspace_id=approval.workspace_id,
        actor=f"agent:{agent.id}",
        details={"source_invocation_id": approval.source_invocation_id},
    )
    audit_count_before_claim = _count(approval_case, "audit_event")
    with pytest.raises(ApprovalDeliveryCredentialRejected):
        approval_case.repository.claim_approved_delivery(
            approval_id=approval.id,
            workspace_id=approval.workspace_id,
            source_agent_id=agent.id,
            source_invocation_id=approval.source_invocation_id,
            expected_payload_hash=approved.approved_payload_hash,
            presented_token_hash=presented_token_hash,
            audit_event=claim_event,
        )

    with approval_case.connect() as connection:
        if approval_case.backend == "sqlite":
            claim_count = connection.execute(
                "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=?",
                (approval.id,),
            ).fetchone()[0]
        else:
            claim_count = connection.execute(
                "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=%s",
                (approval.id,),
            ).fetchone()[0]
    assert claim_count == 0
    assert _count(approval_case, "audit_event") == audit_count_before_claim


def test_invocation_heartbeat_is_scoped_and_capped_at_approval_expiry(approval_case) -> None:
    agent = ExternalAgent(
        id=f"agent_heartbeat_contract_{uuid.uuid4().hex}",
        provider=AgentProvider.ANTIGRAVITY,
        display_name="Invocation heartbeat connector",
        auth_type=AgentAuthType.API_KEY,
        workspace_id=approval_case.workspace.id,
    )
    presented_token_hash = _store_external_agent(approval_case, agent)
    invocation_id = f"heartbeat-contract:{uuid.uuid4().hex}"
    approval_created = now_utc() - timedelta(seconds=5)
    approval = _approval(
        approval_case,
        source_provider=AgentProvider.ANTIGRAVITY,
        source_agent_id=agent.id,
        source_invocation_id=invocation_id,
        status=ApprovalStatus.PENDING_REVIEW,
        created_at=approval_created,
        expires_at=approval_created + timedelta(seconds=45),
    )
    _create(approval_case, approval)
    with approval_case.connect() as connection:
        if approval_case.backend == "sqlite":
            initial = connection.execute(
                "SELECT workspace_id,source_agent_id,source_invocation_id,heartbeat_at,expires_at "
                "FROM approval_invocation_leases WHERE approval_id=?",
                (approval.id,),
            ).fetchone()
        else:
            initial = connection.execute(
                "SELECT workspace_id,source_agent_id,source_invocation_id,heartbeat_at,expires_at "
                "FROM approval_invocation_leases WHERE approval_id=%s",
                (approval.id,),
            ).fetchone()
    assert initial is not None
    assert initial[:3] == (approval_case.workspace.id, agent.id, invocation_id)
    initial_heartbeat_at = (
        initial[3]
        if isinstance(initial[3], datetime)
        else datetime.fromisoformat(initial[3])
    )
    initial_expiry = (
        initial[4]
        if isinstance(initial[4], datetime)
        else datetime.fromisoformat(initial[4])
    )
    assert initial_heartbeat_at == approval.created_at
    assert initial_expiry <= approval.expires_at
    heartbeat_at = now_utc()
    lease = approval_case.repository.heartbeat_invocation(
        approval_id=approval.id,
        workspace_id=approval_case.workspace.id,
        source_agent_id=agent.id,
        source_invocation_id=invocation_id,
        presented_token_hash=presented_token_hash,
        occurred_at=heartbeat_at,
        ttl_seconds=60,
    )
    assert lease.approval_id == approval.id
    assert lease.source_agent_id == agent.id
    assert lease.source_invocation_id == invocation_id
    assert lease.expires_at == approval.expires_at

    with pytest.raises((ApprovalInvocationLeaseRejected, ApprovalDeliveryCredentialRejected)):
        approval_case.repository.heartbeat_invocation(
            approval_id=approval.id,
            workspace_id=f"{approval_case.workspace.id}-other",
            source_agent_id=agent.id,
            source_invocation_id=invocation_id,
            presented_token_hash=presented_token_hash,
            occurred_at=heartbeat_at,
            ttl_seconds=60,
        )


@pytest.mark.parametrize("lease_state", ("expired", "missing"))
def test_expired_or_missing_invocation_lease_blocks_heartbeat_and_decision_without_side_effects(
    approval_case, lease_state: str
) -> None:
    agent = ExternalAgent(
        id=f"agent_expired_lease_fence_{uuid.uuid4().hex}",
        provider=AgentProvider.ANTIGRAVITY,
        display_name="Expired invocation lease fence connector",
        auth_type=AgentAuthType.API_KEY,
        workspace_id=approval_case.workspace.id,
    )
    presented_token_hash = _store_external_agent(approval_case, agent)
    invocation_id = f"expired-lease-fence:{uuid.uuid4().hex}"
    approval = _approval(
        approval_case,
        source_provider=AgentProvider.ANTIGRAVITY,
        source_agent_id=agent.id,
        source_invocation_id=invocation_id,
        status=ApprovalStatus.PENDING_REVIEW,
        expires_at=now_utc() + timedelta(minutes=5),
    )
    _create(approval_case, approval)
    with approval_case.connect() as connection:
        if lease_state == "expired":
            expired_at = now_utc() - timedelta(seconds=2)
            heartbeat_at = expired_at - timedelta(seconds=1)
            if approval_case.backend == "sqlite":
                connection.execute(
                    "UPDATE approval_invocation_leases SET heartbeat_at=?,expires_at=? WHERE approval_id=?",
                    (heartbeat_at.isoformat(), expired_at.isoformat(), approval.id),
                )
            else:
                connection.execute(
                    "UPDATE approval_invocation_leases SET heartbeat_at=%s,expires_at=%s WHERE approval_id=%s",
                    (heartbeat_at, expired_at, approval.id),
                )
        else:
            if approval_case.backend == "sqlite":
                connection.execute(
                    "DELETE FROM approval_invocation_leases WHERE approval_id=?", (approval.id,)
                )
            else:
                connection.execute(
                    "DELETE FROM approval_invocation_leases WHERE approval_id=%s", (approval.id,)
                )

    audit_count_before = _count(approval_case, "audit_event")
    notification_count_before = _count(approval_case, "notification")
    with pytest.raises(ApprovalInvocationLeaseRejected):
        approval_case.repository.heartbeat_invocation(
            approval_id=approval.id,
            workspace_id=approval_case.workspace.id,
            source_agent_id=agent.id,
            source_invocation_id=invocation_id,
            presented_token_hash=presented_token_hash,
            occurred_at=now_utc(),
            ttl_seconds=60,
        )

    approved = approval.model_copy(
        update={"status": ApprovalStatus.APPROVED, "approved_payload_hash_version": 2}
    )
    approved = approved.model_copy(
        update={"approved_payload_hash": compute_approval_payload_hash(approved)}
    )
    with pytest.raises(ApprovalDecisionConflict, match="invocation is no longer live"):
        approval_case.repository.commit_decision(
            approval_id=approval.id,
            expected_status=ApprovalStatus.PENDING_REVIEW,
            expected_approval=approval,
            updated_approval=approved,
            audit_event=_decision_audit(approval, approved, uuid.uuid4().hex),
            decision_notification=Notification(
                title="Decision recorded",
                body="This must not be persisted for a stale invocation.",
                entity_type="approval_request",
                entity_id=approval.id,
                workspace_id=approval.workspace_id,
            ),
        )

    persisted = approval_case.repository.get(approval.id)
    assert persisted == approval
    assert _count(approval_case, "audit_event") == audit_count_before
    assert _count(approval_case, "notification") == notification_count_before


def test_invocation_heartbeat_without_matching_lease_identity_is_rejected(approval_case) -> None:
    agent = ExternalAgent(
        id=f"agent_heartbeat_mismatch_{uuid.uuid4().hex}",
        provider=AgentProvider.ANTIGRAVITY,
        display_name="Invocation heartbeat mismatch connector",
        auth_type=AgentAuthType.API_KEY,
        workspace_id=approval_case.workspace.id,
    )
    presented_token_hash = _store_external_agent(approval_case, agent)
    invocation_id = f"heartbeat-mismatch:{uuid.uuid4().hex}"
    approval = _approval(
        approval_case,
        source_provider=AgentProvider.ANTIGRAVITY,
        source_agent_id=agent.id,
        source_invocation_id=invocation_id,
    )
    _create(approval_case, approval)
    with pytest.raises(ApprovalInvocationLeaseRejected):
        approval_case.repository.heartbeat_invocation(
            approval_id=approval.id,
            workspace_id=approval_case.workspace.id,
            source_agent_id=agent.id,
            source_invocation_id=f"{invocation_id}-wrong",
            presented_token_hash=presented_token_hash,
            occurred_at=now_utc(),
            ttl_seconds=60,
        )


def test_expired_invocation_lease_cannot_be_consumed(approval_case) -> None:
    agent = ExternalAgent(
        id=f"agent_lease_expiry_{uuid.uuid4().hex}",
        provider=AgentProvider.ANTIGRAVITY,
        display_name="Invocation lease expiry connector",
        auth_type=AgentAuthType.API_KEY,
        workspace_id=approval_case.workspace.id,
    )
    presented_token_hash = _store_external_agent(approval_case, agent)
    invocation_id = f"lease-expiry:{uuid.uuid4().hex}"
    approval = _approval(
        approval_case,
        source_provider=AgentProvider.ANTIGRAVITY,
        source_agent_id=agent.id,
        source_invocation_id=invocation_id,
        status=ApprovalStatus.PENDING_REVIEW,
        expires_at=now_utc() + timedelta(minutes=5),
    )
    _create(approval_case, approval)
    approved = approval.model_copy(
        update={"status": ApprovalStatus.APPROVED, "approved_payload_hash_version": 2}
    )
    approved = approved.model_copy(
        update={"approved_payload_hash": compute_approval_payload_hash(approved)}
    )
    approval_case.repository.commit_decision(
        approval_id=approval.id,
        expected_status=ApprovalStatus.PENDING_REVIEW,
        expected_approval=approval,
        updated_approval=approved,
        audit_event=_decision_audit(approval, approved, uuid.uuid4().hex),
        decision_notification=Notification(
            title="Decision recorded",
            body="The connector may claim this invocation once.",
            entity_type="approval_request",
            entity_id=approval.id,
            workspace_id=approval.workspace_id,
        ),
    )
    with approval_case.connect() as connection:
        expired_at = now_utc() - timedelta(seconds=1)
        heartbeat_at = expired_at - timedelta(seconds=1)
        if approval_case.backend == "sqlite":
            connection.execute(
                "UPDATE approval_invocation_leases SET heartbeat_at=?, expires_at=? WHERE approval_id=?",
                (heartbeat_at.isoformat(), expired_at.isoformat(), approval.id),
            )
        else:
            connection.execute(
                "UPDATE approval_invocation_leases SET heartbeat_at=%s, expires_at=%s WHERE approval_id=%s",
                (heartbeat_at, expired_at, approval.id),
            )
    claim_event = AuditEvent(
        event_type="approval.delivery_claimed",
        entity_id=approval.id,
        workspace_id=approval.workspace_id,
        actor=f"agent:{agent.id}",
        details={"source_invocation_id": invocation_id},
    )
    with pytest.raises(ApprovalInvocationLeaseRejected):
        approval_case.repository.claim_approved_delivery(
            approval_id=approval.id,
            workspace_id=approval_case.workspace.id,
            source_agent_id=agent.id,
            source_invocation_id=invocation_id,
            expected_payload_hash=approved.approved_payload_hash,
            presented_token_hash=presented_token_hash,
            audit_event=claim_event,
        )


@pytest.mark.parametrize("expiry_target", ("approval", "lease"))
def test_sqlite_claim_waiting_on_database_lock_rechecks_approval_and_lease_expiry(
    approval_case, expiry_target: str
) -> None:
    if approval_case.backend != "sqlite":
        pytest.skip("Controlled database-lock wait is exercised by the PostgreSQL parity test")

    agent = ExternalAgent(
        id=f"agent_claim_lock_expiry_{uuid.uuid4().hex}",
        provider=AgentProvider.ANTIGRAVITY,
        display_name="Claim lock-expiry connector",
        auth_type=AgentAuthType.API_KEY,
        workspace_id=approval_case.workspace.id,
    )
    presented_token_hash = _store_external_agent(approval_case, agent)
    invocation_id = f"claim-lock-expiry:{uuid.uuid4().hex}"
    approval = _approval(
        approval_case,
        source_provider=AgentProvider.ANTIGRAVITY,
        source_agent_id=agent.id,
        source_invocation_id=invocation_id,
        status=ApprovalStatus.PENDING_REVIEW,
        expires_at=now_utc() + timedelta(minutes=5),
    )
    _create(approval_case, approval)
    approved = approval.model_copy(
        update={"status": ApprovalStatus.APPROVED, "approved_payload_hash_version": 2}
    )
    approved = approved.model_copy(
        update={"approved_payload_hash": compute_approval_payload_hash(approved)}
    )
    approval_case.repository.commit_decision(
        approval_id=approval.id,
        expected_status=ApprovalStatus.PENDING_REVIEW,
        expected_approval=approval,
        updated_approval=approved,
        audit_event=_decision_audit(approval, approved, f"claim-lock-decision-{uuid.uuid4().hex}"),
        decision_notification=Notification(
            title="Decision recorded",
            body="The connector may claim this invocation once.",
            entity_type="approval_request",
            entity_id=approval.id,
            workspace_id=approval.workspace_id,
        ),
    )
    deadline = now_utc() + timedelta(seconds=2)
    approval_expiry = (
        deadline
        if expiry_target == "approval"
        else now_utc() + timedelta(minutes=5)
    )
    approved = approved.model_copy(update={"expires_at": approval_expiry})
    with approval_case.connect() as connection:
        connection.execute(
            "UPDATE kv_store SET payload=? WHERE entity_type='approval_request' AND entity_id=?",
            (approved.model_dump_json(), approval.id),
        )
        connection.execute(
            "UPDATE approval_invocation_leases SET expires_at=? WHERE approval_id=?",
            (deadline.isoformat(), approval.id),
        )

    claim_started_at = now_utc()
    assert claim_started_at < deadline
    claim_event = AuditEvent(
        id=f"audit_claim_lock_expiry_{uuid.uuid4().hex}",
        event_type="approval.delivery_claimed",
        entity_id=approval.id,
        workspace_id=approval.workspace_id,
        actor=f"agent:{agent.id}",
        created_at=claim_started_at,
        details={"source_invocation_id": invocation_id},
    )
    begin_requested = Event()

    class BeginObservedConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection

        def execute(self, statement: str, parameters=()):
            if statement == "BEGIN IMMEDIATE":
                begin_requested.set()
            return self._connection.execute(statement, parameters)

        def __enter__(self):
            self._connection.__enter__()
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return self._connection.__exit__(exc_type, exc_value, traceback)

        def __getattr__(self, name: str):
            return getattr(self._connection, name)

    waiting_repository = SQLiteApprovalDecisionRepository(
        lambda: BeginObservedConnection(approval_case.connect())
    )
    blocker = approval_case.connect()
    blocker.execute("BEGIN IMMEDIATE")
    before_claims = _count(approval_case, "audit_event")
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                waiting_repository.claim_approved_delivery,
                approval_id=approval.id,
                workspace_id=approval.workspace_id,
                source_agent_id=agent.id,
                source_invocation_id=invocation_id,
                expected_payload_hash=approved.approved_payload_hash,
                presented_token_hash=presented_token_hash,
                audit_event=claim_event,
            )
            assert begin_requested.wait(timeout=2)
            assert now_utc() < deadline
            assert not future.done()
            time.sleep(max(0.0, (deadline - now_utc()).total_seconds()) + 0.05)
            assert now_utc() >= deadline
            blocker.rollback()
            with pytest.raises(ApprovalInvocationLeaseRejected, match="expired"):
                future.result(timeout=5)
    finally:
        blocker.close()

    assert _count(approval_case, "audit_event") == before_claims
    with approval_case.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM approval_delivery_claims WHERE approval_id=?",
            (approval.id,),
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "decision_status",
    (
        ApprovalStatus.APPROVED,
        ApprovalStatus.DENIED,
        ApprovalStatus.EXPIRED,
        ApprovalStatus.CANCELLED,
    ),
)
def test_legacy_pending_review_transitions_across_adapters(
    approval_case, decision_status
) -> None:
    """Preserve the legacy state as the expected snapshot for every allowed transition."""
    approval = _approval(approval_case).model_copy(
        update={"status": ApprovalStatus.PENDING_REVIEW}
    )
    _create(approval_case, approval)

    decision_updates = {"status": decision_status}
    if decision_status == ApprovalStatus.APPROVED:
        decision_updates.update(
            {"approved_payload_hash_version": 2, "approved_at": now_utc()}
        )
    updated = approval.model_copy(update=decision_updates)
    if decision_status == ApprovalStatus.APPROVED:
        updated = updated.model_copy(
            update={"approved_payload_hash": compute_approval_payload_hash(updated)}
        )
    approval_case.repository.commit_decision(
        approval_id=approval.id,
        expected_status=ApprovalStatus.PENDING_REVIEW,
        expected_approval=approval,
        updated_approval=updated,
        audit_event=_decision_audit(approval, updated, uuid.uuid4().hex),
        decision_notification=Notification(
            title="Decision recorded",
            body=f"The legacy pending request moved to {decision_status}.",
            entity_type="approval_request",
            entity_id=approval.id,
            workspace_id=approval.workspace_id,
        ),
    )

    assert approval_case.repository.get(approval.id) == updated
    assert _count(approval_case, "audit_event") == 2
    assert _count(approval_case, "notification") == 2


def test_legacy_rejected_approval_remains_terminal_across_adapters(approval_case) -> None:
    """The legacy REJECTED representation must not become approvable on read."""
    requested = _approval(approval_case)
    _create(approval_case, requested)
    rejected = requested.model_copy(
        update={"status": ApprovalStatus.REJECTED}
    )
    with approval_case.connect() as connection:
        if approval_case.backend == "sqlite":
            connection.execute(
                "UPDATE kv_store SET payload=? WHERE entity_type=? AND entity_id=?",
                (rejected.model_dump_json(), "approval_request", rejected.id),
            )
        else:
            from psycopg.types.json import Jsonb

            connection.execute(
                "UPDATE approval_requests SET status=%s,payload=%s WHERE approval_id=%s",
                (rejected.status.value, Jsonb(rejected.model_dump(mode="json")), rejected.id),
            )
    attempted_approval = rejected.model_copy(
        update={
            "status": ApprovalStatus.APPROVED,
            "approved_payload_hash_version": 2,
            "approved_at": now_utc(),
        }
    )
    attempted_approval = attempted_approval.model_copy(
        update={"approved_payload_hash": compute_approval_payload_hash(attempted_approval)}
    )

    with pytest.raises(ValueError, match="Invalid approval transition"):
        approval_case.repository.commit_decision(
            approval_id=rejected.id,
            expected_status=ApprovalStatus.REJECTED,
            expected_approval=rejected,
            updated_approval=attempted_approval,
            audit_event=_decision_audit(rejected, attempted_approval, "legacy-rejected"),
            decision_notification=Notification(
                title="Decision recorded",
                body="A rejected legacy approval cannot be reopened by a decision call.",
                entity_type="approval_request",
                entity_id=rejected.id,
                workspace_id=rejected.workspace_id,
            ),
        )

    assert approval_case.repository.get(rejected.id) == rejected
    assert _count(approval_case, "audit_event") == 1
    assert _count(approval_case, "notification") == 1


def test_decision_outbox_conflict_rolls_back_decision_and_audit_across_adapters(
    approval_case,
) -> None:
    approval = _approval(approval_case)
    _create(approval_case, approval)
    approved = approval.model_copy(
        update={
            "status": ApprovalStatus.APPROVED,
            "approved_payload_hash_version": 2,
            "approved_at": now_utc(),
        }
    )
    approved = approved.model_copy(
        update={"approved_payload_hash": compute_approval_payload_hash(approved)}
    )
    decision_notification = Notification(
        id=f"notification_decision_conflict_{uuid.uuid4().hex}",
        title="Decision recorded",
        body="This write must conflict with the pre-existing outbox identity.",
        entity_type="approval_request",
        entity_id=approval.id,
        workspace_id=approval.workspace_id,
    )
    _store_notification(approval_case, decision_notification)

    expected_error = (
        sqlite3.IntegrityError
        if approval_case.backend == "sqlite"
        else psycopg.errors.UniqueViolation
    )
    with pytest.raises(expected_error):
        approval_case.repository.commit_decision(
            approval_id=approval.id,
            expected_status=ApprovalStatus.REQUESTED,
            expected_approval=approval,
            updated_approval=approved,
            audit_event=_decision_audit(approval, approved, uuid.uuid4().hex),
            decision_notification=decision_notification,
        )

    assert approval_case.repository.get(approval.id) == approval
    assert _count(approval_case, "audit_event") == 1
    assert _count(approval_case, "notification") == 2
