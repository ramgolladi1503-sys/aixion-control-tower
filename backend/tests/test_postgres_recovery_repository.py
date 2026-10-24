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
from app.models import Project, User, UserRole, Workspace
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

    def execute(
        self, statement: str, params: tuple[object, ...] | None = None
    ) -> _Result:
        self.statements.append((statement, params))
        if "FROM schema_migrations" in statement:
            return _Result([("pg_0001", "baseline", "2026-01-01 00:00:00+00")])
        if "FROM users" in statement:
            return _Result([("user-a", '{"id":"user-a"}')])
        if "FROM workspaces" in statement:
            return _Result([("workspace-a", '{"id":"workspace-a"}')])
        return _Result()


def test_postgres_recovery_snapshot_uses_one_read_only_repeatable_read_and_sql_tenant_scope() -> None:
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
        statement for statement, _ in tenant_queries
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
