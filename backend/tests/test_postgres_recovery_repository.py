from __future__ import annotations

import os
import json
import uuid
from collections.abc import Callable, Iterator

import psycopg
import pytest
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app.agent_task_models import AgentTask
from app import models
from app.models import (
    ApprovalRequest,
    AuditEvent,
    Idea,
    MCPChildServer,
    MCPPendingRequest,
    Notification,
    Project,
    RiskAssessment,
    User,
    UserRole,
    WorkOrder,
    Workspace,
)
from app.postgres_database import run_postgres_migrations
from app.recovery_routes import SAFE_ENTITY_STORAGE_TYPES
from app.recovery_repository import PostgresRecoverySnapshotRepository


class _Result:
    def __init__(self, rows: list[tuple[object, ...]] = ()) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows

    def fetchone(self) -> tuple[object, ...] | None:
        return self._rows[0] if self._rows else None


class _RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[object, ...] | None]] = []

    def __enter__(self) -> _RecordingConnection:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, statement: str, params: tuple[object, ...] | None = None) -> _Result:
        self.statements.append((statement, params))
        if "FROM schema_migrations" in statement:
            return _Result([("pg_0001", "baseline", "2026-01-01 00:00:00+00")])
        if "FROM users" in statement:
            return _Result([("user-a", '{"id":"user-a"}')])
        if "FROM workspaces" in statement:
            return _Result([("workspace-a", '{"id":"workspace-a"}')])
        return _Result()


def test_postgres_recovery_snapshot_uses_one_read_only_repeatable_read_and_sql_tenant_scope() -> (
    None
):
    connection = _RecordingConnection()
    repository = PostgresRecoverySnapshotRepository(lambda: connection)  # type: ignore[arg-type]

    grouped, migrations, tasks, membership = repository.read_snapshot(
        SAFE_ENTITY_STORAGE_TYPES,
        scope_entity_type="agent_task",
        workspace_id="workspace-a",
        membership_user_id="user-a",
        membership_workspace_id="workspace-a",
    )

    assert grouped == {name: [] for name in SAFE_ENTITY_STORAGE_TYPES}
    assert migrations == [("pg_0001", "baseline", "2026-01-01 00:00:00+00")]
    assert tasks == []
    assert membership == {
        "user": [("user-a", '{"id":"user-a"}')],
        "workspace": [("workspace-a", '{"id":"workspace-a"}')],
    }
    assert connection.statements[0][0] == (
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    )
    tenant_queries = [
        (statement, params)
        for statement, params in connection.statements
        if "workspace_id=%s" in statement and "schema_migrations" not in statement
    ]
    collection_queries = [
        statement
        for statement, _ in tenant_queries
        if any(f"FROM {table} " in statement for table, _ in repository._COLLECTION_TABLES.values())
    ]
    assert len(collection_queries) == len(SAFE_ENTITY_STORAGE_TYPES)
    assert all("workspace-a" in (params or ()) for _, params in tenant_queries)


@pytest.fixture
def postgres_recovery_connect() -> Iterator[
    tuple[Callable[[], Connection], Workspace, User, Project, AgentTask]
]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL recovery integration")
    schema = f"ct_recovery_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    try:
        run_postgres_migrations(connect)
        user = User(
            id=f"user_{uuid.uuid4().hex}",
            email=f"owner-{uuid.uuid4().hex}@example.test",
            display_name="Owner",
            role=UserRole.OWNER,
            password_hash="hash",
            password_salt="salt",
            workspace_id=f"workspace_{uuid.uuid4().hex}",
            email_verified=True,
        )
        workspace = Workspace(
            id=user.workspace_id,
            name="Recovery workspace",
            owner_user_id=user.id,
        )
        project = Project(
            id=f"project_{uuid.uuid4().hex}",
            name="Recovery project",
            workspace_id=workspace.id,
        )
        other_workspace = Workspace(
            id=f"workspace_{uuid.uuid4().hex}",
            name="Other workspace",
            owner_user_id=f"user_{uuid.uuid4().hex}",
        )
        other_project = Project(
            id=f"project_{uuid.uuid4().hex}",
            name="Other tenant project",
            workspace_id=other_workspace.id,
        )
        task = AgentTask(
            id=f"task_{uuid.uuid4().hex}",
            project_id=project.id,
            workspace_id=workspace.id,
            title="Recovery task",
            goal="Prove recovery scope",
        )
        with connect() as conn:
            conn.execute(
                "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
                (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
            )
            conn.execute(
                "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,email_verified,payload) "
                "VALUES(%s,%s,%s,%s,%s,FALSE,TRUE,%s)",
                (
                    user.id,
                    workspace.id,
                    user.email,
                    user.email.lower(),
                    user.role.value,
                    Jsonb(user.model_dump(mode="json")),
                ),
            )
            conn.execute(
                "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
                (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))),
            )
            conn.execute(
                "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
                (
                    other_workspace.id,
                    Jsonb(other_workspace.model_dump(mode="json")),
                ),
            )
            conn.execute(
                "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
                (
                    other_project.id,
                    other_workspace.id,
                    Jsonb(other_project.model_dump(mode="json")),
                ),
            )
            conn.execute(
                "INSERT INTO agent_tasks(task_id,project_id,workspace_id,external_agent_id,approval_request_id,status,updated_at,payload) "
                "VALUES(%s,%s,%s,NULL,NULL,%s,%s,%s)",
                (
                    task.id,
                    project.id,
                    workspace.id,
                    task.status.value,
                    task.updated_at,
                    Jsonb(task.model_dump(mode="json")),
                ),
            )
        yield connect, workspace, user, project, task
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_postgres_recovery_snapshot_reads_workspace_rows_and_membership_in_same_store(
    postgres_recovery_connect,
) -> None:
    connect, workspace, user, project, task = postgres_recovery_connect
    repository = PostgresRecoverySnapshotRepository(connect)

    grouped, migration_rows, task_rows, memberships = repository.read_snapshot(
        SAFE_ENTITY_STORAGE_TYPES,
        scope_entity_type="agent_task",
        workspace_id=workspace.id,
        membership_user_id=user.id,
        membership_workspace_id=workspace.id,
    )

    assert [row_id for row_id, _ in grouped["projects"]] == [project.id]
    assert json.loads(grouped["projects"][0][1])["workspace_id"] == workspace.id
    assert migration_rows
    assert task_rows[0][0] == task.id
    assert memberships["user"][0][0] == user.id
    assert memberships["workspace"][0][0] == workspace.id


def test_postgres_recovery_snapshot_reads_all_nine_collections_and_excludes_other_workspace(
    postgres_recovery_connect,
) -> None:
    connect, workspace, user, project, _task = postgres_recovery_connect
    repository = PostgresRecoverySnapshotRepository(connect)
    now = "2026-10-24T00:00:00+00:00"

    with connect() as conn:
        other_workspace_id = conn.execute(
            "SELECT workspace_id FROM workspaces WHERE workspace_id <> %s",
            (workspace.id,),
        ).fetchone()[0]
        other_project_id = conn.execute(
            "SELECT project_id FROM projects WHERE workspace_id=%s",
            (other_workspace_id,),
        ).fetchone()[0]

        own_idea = Idea(
            id=f"idea_{uuid.uuid4().hex}",
            project_id=project.id,
            workspace_id=workspace.id,
            title="Own idea",
            raw_text="own",
        )
        foreign_idea = Idea(
            id=f"idea_{uuid.uuid4().hex}",
            project_id=other_project_id,
            workspace_id=other_workspace_id,
            title="Foreign idea",
            raw_text="foreign",
        )
        for item in (own_idea, foreign_idea):
            conn.execute(
                "INSERT INTO ideas(idea_id,project_id,workspace_id,created_at,payload) VALUES(%s,%s,%s,%s,%s)",
                (
                    item.id,
                    item.project_id,
                    item.workspace_id,
                    item.created_at,
                    Jsonb(item.model_dump(mode="json")),
                ),
            )

        own_order = WorkOrder(
            id=f"work_{uuid.uuid4().hex}",
            project_id=project.id,
            idea_id=own_idea.id,
            goal="Own work",
            workspace_id=workspace.id,
        )
        foreign_order = WorkOrder(
            id=f"work_{uuid.uuid4().hex}",
            project_id=other_project_id,
            idea_id=foreign_idea.id,
            goal="Foreign work",
            workspace_id=other_workspace_id,
        )
        for item in (own_order, foreign_order):
            conn.execute(
                "INSERT INTO work_orders(work_order_id,project_id,workspace_id,idea_id,source_agent_id,created_at,payload) VALUES(%s,%s,%s,%s,NULL,%s,%s)",
                (
                    item.id,
                    item.project_id,
                    item.workspace_id,
                    item.idea_id,
                    item.created_at,
                    Jsonb(item.model_dump(mode="json")),
                ),
            )

        own_approval = ApprovalRequest(
            id=f"approval_{uuid.uuid4().hex}",
            project_id=project.id,
            work_order_id=own_order.id,
            workspace_id=workspace.id,
            title="Own approval",
            summary="own",
            target_branch="main",
            files=[],
            risk=RiskAssessment(level="LOW"),
        )
        foreign_approval = ApprovalRequest(
            id=f"approval_{uuid.uuid4().hex}",
            project_id=other_project_id,
            work_order_id=foreign_order.id,
            workspace_id=other_workspace_id,
            title="Foreign approval",
            summary="foreign",
            target_branch="main",
            files=[],
            risk=RiskAssessment(level="LOW"),
        )
        for item in (own_approval, foreign_approval):
            conn.execute(
                "INSERT INTO approval_requests(approval_id,project_id,workspace_id,work_order_id,source_agent_id,status,expires_at,created_at,updated_at,version,payload) VALUES(%s,%s,%s,%s,NULL,%s,NULL,%s,%s,0,%s)",
                (
                    item.id,
                    item.project_id,
                    item.workspace_id,
                    item.work_order_id,
                    item.status.value,
                    item.created_at,
                    item.updated_at,
                    Jsonb(item.model_dump(mode="json")),
                ),
            )

        own_server = MCPChildServer(
            id=f"mcp_server_{uuid.uuid4().hex}",
            project_id=project.id,
            workspace_id=workspace.id,
            name="own",
            created_at=now,
            updated_at=now,
        )
        foreign_server = MCPChildServer(
            id=f"mcp_server_{uuid.uuid4().hex}",
            project_id=other_project_id,
            workspace_id=other_workspace_id,
            name="foreign",
            created_at=now,
            updated_at=now,
        )
        for item in (own_server, foreign_server):
            conn.execute(
                "INSERT INTO mcp_child_servers(server_id,project_id,workspace_id,name,enabled,created_at,updated_at,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    item.id,
                    item.project_id,
                    item.workspace_id,
                    item.name,
                    item.enabled,
                    item.created_at,
                    item.updated_at,
                    Jsonb(item.model_dump(mode="json")),
                ),
            )

        own_pending = MCPPendingRequest(
            id=f"mcp_pending_{uuid.uuid4().hex}",
            project_id=project.id,
            workspace_id=workspace.id,
            approval_request_id=own_approval.id,
            server_name="own",
            tool_name="read",
            created_at=now,
            updated_at=now,
        )
        foreign_pending = MCPPendingRequest(
            id=f"mcp_pending_{uuid.uuid4().hex}",
            project_id=other_project_id,
            workspace_id=other_workspace_id,
            approval_request_id=foreign_approval.id,
            server_name="foreign",
            tool_name="read",
            created_at=now,
            updated_at=now,
        )
        for item in (own_pending, foreign_pending):
            conn.execute(
                "INSERT INTO mcp_pending_requests(pending_id,approval_id,project_id,workspace_id,status,attempts,lease_owner,lease_expires_at,last_error,created_at,updated_at,payload) VALUES(%s,%s,%s,%s,%s,%s,NULL,NULL,NULL,%s,%s,%s)",
                (
                    item.id,
                    item.approval_request_id,
                    item.project_id,
                    item.workspace_id,
                    item.status.value,
                    item.attempts,
                    item.created_at,
                    item.updated_at,
                    Jsonb(item.model_dump(mode="json")),
                ),
            )

        own_test = models.TestRun(
            id=f"test_{uuid.uuid4().hex}",
            approval_request_id=own_approval.id,
            command="pytest",
            status="passed",
            created_at=now,
        )
        foreign_test = models.TestRun(
            id=f"test_{uuid.uuid4().hex}",
            approval_request_id=foreign_approval.id,
            command="pytest",
            status="passed",
            created_at=now,
        )
        for item, workspace_id, project_id in (
            (own_test, workspace.id, project.id),
            (foreign_test, other_workspace_id, other_project_id),
        ):
            conn.execute(
                "INSERT INTO test_runs(test_run_id,approval_id,project_id,workspace_id,created_at,payload) VALUES(%s,%s,%s,%s,%s,%s)",
                (
                    item.id,
                    item.approval_request_id,
                    project_id,
                    workspace_id,
                    item.created_at,
                    Jsonb(item.model_dump(mode="json")),
                ),
            )

        own_notification = Notification(
            id=f"notification_{uuid.uuid4().hex}",
            title="own",
            body="own",
            entity_type="approval_request",
            entity_id=own_approval.id,
            workspace_id=workspace.id,
            user_id=user.id,
            approval_request_id=own_approval.id,
            created_at=now,
        )
        foreign_notification = Notification(
            id=f"notification_{uuid.uuid4().hex}",
            title="foreign",
            body="foreign",
            entity_type="approval_request",
            entity_id=foreign_approval.id,
            workspace_id=other_workspace_id,
            approval_request_id=foreign_approval.id,
            created_at=now,
        )
        for item in (own_notification, foreign_notification):
            conn.execute(
                "INSERT INTO notifications(notification_id,workspace_id,user_id,approval_id,entity_type,status,attempts,next_attempt_at,lease_expires_at,created_at,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,NULL,NULL,%s,%s)",
                (
                    item.id,
                    item.workspace_id,
                    item.user_id,
                    item.approval_request_id,
                    item.entity_type,
                    item.push_status,
                    item.push_attempts,
                    item.created_at,
                    Jsonb(item.model_dump(mode="json")),
                ),
            )

        own_audit = AuditEvent(
            id=f"audit_{uuid.uuid4().hex}",
            event_type="test",
            entity_id=project.id,
            workspace_id=workspace.id,
            created_at=now,
        )
        foreign_audit = AuditEvent(
            id=f"audit_{uuid.uuid4().hex}",
            event_type="test",
            entity_id=other_project_id,
            workspace_id=other_workspace_id,
            created_at=now,
        )
        for item in (own_audit, foreign_audit):
            conn.execute(
                "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,event_type,actor,details,created_at,payload) VALUES(%s,%s,NULL,%s,%s,%s,%s,%s,%s,%s)",
                (
                    item.id,
                    item.workspace_id,
                    "project",
                    item.entity_id,
                    item.event_type,
                    item.actor,
                    Jsonb(item.details),
                    item.created_at,
                    Jsonb(item.model_dump(mode="json")),
                ),
            )

    grouped, _migrations, _tasks, _membership = repository.read_snapshot(
        SAFE_ENTITY_STORAGE_TYPES,
        scope_entity_type="agent_task",
        workspace_id=workspace.id,
    )
    expected_ids = {
        "projects": {project.id},
        "mcp_child_servers": {own_server.id},
        "ideas": {own_idea.id},
        "work_orders": {own_order.id},
        "approval_requests": {own_approval.id},
        "mcp_pending_requests": {own_pending.id},
        "test_runs": {own_test.id},
        "notifications": {own_notification.id},
        "audit_events": {own_audit.id},
    }
    foreign_ids = {
        "projects": {other_project_id},
        "mcp_child_servers": {foreign_server.id},
        "ideas": {foreign_idea.id},
        "work_orders": {foreign_order.id},
        "approval_requests": {foreign_approval.id},
        "mcp_pending_requests": {foreign_pending.id},
        "test_runs": {foreign_test.id},
        "notifications": {foreign_notification.id},
        "audit_events": {foreign_audit.id},
    }
    assert set(grouped) == set(SAFE_ENTITY_STORAGE_TYPES)
    for collection in SAFE_ENTITY_STORAGE_TYPES:
        returned_ids = {row_id for row_id, _payload in grouped[collection]}
        assert returned_ids == expected_ids[collection], collection
        assert returned_ids.isdisjoint(foreign_ids[collection]), collection
