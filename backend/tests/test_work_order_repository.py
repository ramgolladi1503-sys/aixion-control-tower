from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.database_migrations import run_migrations
from app.models import AuditEvent, Project, WorkOrder, Workspace
from app.work_order_repository import (
    PostgresWorkOrderRepository,
    SQLiteWorkOrderRepository,
    WorkOrderRepositoryScopeError,
)


def _work_order(workspace_id: str | None, project_id: str = "project-work-order") -> WorkOrder:
    return WorkOrder(
        id="work-order-repository-test",
        project_id=project_id,
        workspace_id=workspace_id,
        goal="Persist a scoped work order",
        created_by_user_id=None,
    )


def _audit(order: WorkOrder, *, audit_id: str = "audit-work-order-test") -> AuditEvent:
    return AuditEvent(
        id=audit_id,
        event_type="work_order.created",
        entity_id=order.id,
        workspace_id=order.workspace_id,
        actor="owner@example.test",
        details={
            "project_id": order.project_id,
            "risk_level": order.risk_level,
            "source_type": order.source_type,
            "source_provider": order.source_provider,
            "source_agent_id": order.source_agent_id,
            "source_agent_name": order.source_agent_name,
            "created_by_user_id": order.created_by_user_id,
            "verified_source": order.verified_source,
        },
    )


def _sqlite_repository(path: Path) -> SQLiteWorkOrderRepository:
    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(path, timeout=10)
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    with connect() as connection:
        run_migrations(connection)
    return SQLiteWorkOrderRepository(connect)


def _seed_sqlite_project(path: Path, workspace_id: str) -> None:
    with sqlite3.connect(path) as connection:
        workspace = Workspace(
            id=workspace_id, name="WorkOrder test", owner_user_id="owner-work-order-test"
        )
        project = Project(id="project-work-order", name="WorkOrder project", workspace_id=workspace_id)
        connection.execute(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            ("workspace", workspace.id, workspace.model_dump_json()),
        )
        connection.execute(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            ("project", project.id, project.model_dump_json()),
        )


def test_sqlite_work_order_and_audit_commit_together_and_are_readable(tmp_path: Path) -> None:
    path = tmp_path / "work-orders.sqlite3"
    repository = _sqlite_repository(path)
    _seed_sqlite_project(path, "workspace-work-order")
    order = _work_order("workspace-work-order")
    event = _audit(order)

    repository.create(work_order=order, audit_event=event)

    assert repository.get(order.id) == order
    assert repository.list_for_workspace(order.workspace_id) == [order]
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT entity_type,entity_id FROM kv_store WHERE entity_type IN ('work_order','audit_event') "
            "ORDER BY entity_type"
        ).fetchall()
    assert rows == [("audit_event", event.id), ("work_order", order.id)]


def test_sqlite_scope_failure_writes_neither_order_nor_audit(tmp_path: Path) -> None:
    path = tmp_path / "work-order-scope.sqlite3"
    repository = _sqlite_repository(path)
    _seed_sqlite_project(path, "workspace-one")
    order = _work_order("workspace-other")

    with pytest.raises(WorkOrderRepositoryScopeError, match="workspace does not match"):
        repository.create(work_order=order, audit_event=_audit(order))

    with sqlite3.connect(path) as connection:
        count = connection.execute(
            "SELECT count(*) FROM kv_store WHERE entity_type IN ('work_order','audit_event')"
        ).fetchone()[0]
    assert count == 0


def test_sqlite_audit_identity_mismatch_is_rejected_before_write(tmp_path: Path) -> None:
    path = tmp_path / "work-order-audit.sqlite3"
    repository = _sqlite_repository(path)
    _seed_sqlite_project(path, "workspace-work-order")
    order = _work_order("workspace-work-order")
    bad_event = _audit(order).model_copy(update={"entity_id": "different-work-order"})

    with pytest.raises(ValueError, match="audit event must match"):
        repository.create(work_order=order, audit_event=bad_event)

    assert repository.get(order.id) is None


@pytest.fixture
def postgres_work_order_repository():
    import os
    import uuid

    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is not configured")
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql
    from psycopg.types.json import Jsonb

    from app.postgres_database import run_postgres_migrations

    schema = "work_order_repo_" + uuid.uuid4().hex

    def connect():
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    with psycopg.connect(dsn) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        run_postgres_migrations(connect)
        workspace = Workspace(
            id="workspace-work-order-pg",
            name="WorkOrder PG test",
            owner_user_id="owner-work-order-pg-test",
        )
        project = Project(
            id="project-work-order", name="WorkOrder project", workspace_id=workspace.id
        )
        with connect() as connection:
            connection.execute(
                "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
                (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
            )
            connection.execute(
                "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
                (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))),
            )
        yield PostgresWorkOrderRepository(connect), connect, workspace.id
    finally:
        with psycopg.connect(dsn) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_postgres_work_order_and_audit_commit_together(postgres_work_order_repository) -> None:
    repository, connect, workspace_id = postgres_work_order_repository
    order = _work_order(workspace_id)
    event = _audit(order)

    repository.create(work_order=order, audit_event=event)

    assert repository.get(order.id) == order
    assert repository.list_for_workspace(workspace_id) == [order]
    with connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM audit_events WHERE event_id=%s", (event.id,)
        ).fetchone()[0] == 1


def test_postgres_work_order_listing_requires_workspace(postgres_work_order_repository) -> None:
    repository, _connect, _workspace_id = postgres_work_order_repository

    with pytest.raises(WorkOrderRepositoryScopeError, match="requires a workspace"):
        repository.list_for_workspace()
