from __future__ import annotations

import multiprocessing
import os
import queue as queue_module
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any

import psycopg
import pytest
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app.approval_repository import PostgresApprovalDecisionRepository
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
from app.notification_outbox_repository import PostgresNotificationOutboxRepository
from app.postgres_database import run_postgres_migrations


def _claim_in_process(
    dsn: str,
    schema: str,
    barrier: Any,
    results: Any,
    now_iso: str,
) -> None:
    """Race one claim from an independently connected process."""
    try:
        barrier.wait(timeout=10)

        def connect() -> Connection:
            conn = psycopg.connect(dsn)
            conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            return conn

        claimed = PostgresNotificationOutboxRepository(connect).claim_next_due(
            now=datetime.fromisoformat(now_iso), max_attempts=3, lease_seconds=60
        )
        results.put(
            (
                "ok",
                os.getpid(),
                None
                if claimed is None
                else (
                    claimed.id,
                    claimed.push_status,
                    claimed.push_attempts,
                    claimed.push_lease_expires_at.isoformat()
                    if claimed.push_lease_expires_at is not None
                    else None,
                ),
            )
        )
    except BaseException as exc:
        results.put(("error", os.getpid(), f"{type(exc).__name__}: {exc}"))
        raise


@pytest.fixture
def postgres_schema() -> Iterator[tuple[str, str, Callable[[], Connection]]]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL integration tests")
    schema = f"ct_outbox_process_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        conn = psycopg.connect(dsn)
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return conn

    try:
        run_postgres_migrations(connect)
        yield dsn, schema, connect
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _seed_due_notification(
    connect: Callable[[], Connection], *, suffix: str
) -> Notification:
    workspace = Workspace(
        id=f"workspace_{suffix}", name="Process claim workspace", owner_user_id=f"owner_{suffix}"
    )
    project = Project(
        id=f"project_{suffix}", name="Process claim project", workspace_id=workspace.id
    )
    approval = ApprovalRequest(
        id=f"approval_{suffix}",
        project_id=project.id,
        workspace_id=workspace.id,
        title="Process claim approval",
        summary="Exercise durable outbox leasing across processes.",
        target_branch="main",
        files=[FileChange(path="README.md", diff="+claim")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.REQUESTED,
    )
    notification = Notification(
        id=f"notification_{suffix}",
        title="Approval needed",
        body="Review the pending approval.",
        entity_type="approval_request",
        entity_id=approval.id,
        workspace_id=workspace.id,
        created_at=now_utc(),
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
            (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
        )
        conn.execute(
            "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
            (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))),
        )
    PostgresApprovalDecisionRepository(connect).create(
        approval=approval,
        audit_event=AuditEvent(
            event_type="approval.created",
            entity_id=approval.id,
            workspace_id=workspace.id,
            details={"project_id": project.id},
        ),
        notification=notification,
    )
    return notification


def test_independent_processes_claim_distinct_due_notifications_and_persist_leases(
    postgres_schema: tuple[str, str, Callable[[], Connection]],
) -> None:
    dsn, schema, connect = postgres_schema
    notifications = [
        _seed_due_notification(connect, suffix=uuid.uuid4().hex) for _ in range(2)
    ]
    claim_time = now_utc()

    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(
            target=_claim_in_process,
            args=(dsn, schema, barrier, results, claim_time.isoformat()),
        )
        for _ in range(2)
    ]
    try:
        for process in processes:
            process.start()
        received: list[tuple[str, int, Any]] = []
        for _ in processes:
            try:
                received.append(results.get(timeout=20))
            except queue_module.Empty:
                pytest.fail("Timed out waiting for a PostgreSQL claim worker")
        for process in processes:
            process.join(timeout=10)
            if process.is_alive():
                pytest.fail("PostgreSQL claim worker did not exit before timeout")

        assert [process.exitcode for process in processes] == [0, 0]
        assert all(result[0] == "ok" for result in received), received
        assert len({result[1] for result in received}) == 2
        claims = [result[2] for result in received]
        assert all(claim is not None for claim in claims)
        claimed_ids = [claim[0] for claim in claims]
        assert len(set(claimed_ids)) == 2
        assert set(claimed_ids) == {notification.id for notification in notifications}

        for claim in claims:
            assert claim[1] == "SENDING"
            assert claim[2] == 1
            assert claim[3] is not None
        with connect() as conn:
            rows = conn.execute(
                "SELECT notification_id,status,attempts,lease_expires_at,payload "
                "FROM notifications WHERE notification_id = ANY(%s)",
                (claimed_ids,),
            ).fetchall()
        assert len(rows) == 2
        for notification_id, status, attempts, lease_expiry, payload in rows:
            assert status == "SENDING"
            assert attempts == 1
            assert lease_expiry is not None and lease_expiry > claim_time
            stored = Notification.model_validate(payload)
            assert stored.id == notification_id
            assert stored.push_status == status
            assert stored.push_attempts == attempts
            assert stored.push_lease_expires_at == lease_expiry
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
        results.close()
        results.join_thread()
