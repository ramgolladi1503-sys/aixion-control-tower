from __future__ import annotations

import os
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from psycopg import Connection
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from app.database_migrations import run_migrations
from app.application_repositories import ApplicationRepositories
from app.mcp_child_server_repository import (
    MCPChildServerConflict,
    MCPChildServerScopeError,
    PostgresMCPChildServerRepository,
    SQLiteMCPChildServerRepository,
)
from app.models import AuditEvent, MCPChildServer, Project, Workspace
from app.postgres_database import POSTGRES_MIGRATIONS, run_postgres_migrations


def _server(workspace_id: str | None = "workspace-mcp-test", *, name: str = "local") -> MCPChildServer:
    return MCPChildServer(
        id="mcp-server-test",
        project_id="project-mcp-test",
        workspace_id=workspace_id,
        name=name,
        transport="stdio",
        endpoint="local://test",
    )


def _audit(server: MCPChildServer, *, audit_id: str = "audit-mcp-test") -> AuditEvent:
    return AuditEvent(
        id=audit_id,
        event_type="mcp.child_server.registered",
        entity_id=server.id,
        workspace_id=server.workspace_id,
        actor="owner@example.test",
        details={"project_id": server.project_id, "name": server.name},
    )


def _sqlite_repository(path: Path) -> SQLiteMCPChildServerRepository:
    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(path, timeout=10)
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    with connect() as connection:
        run_migrations(connection)
    return SQLiteMCPChildServerRepository(connect)


def _seed_sqlite_project(path: Path, workspace_id: str) -> None:
    workspace = Workspace(id=workspace_id, name="MCP test", owner_user_id="owner-mcp-test")
    project = Project(id="project-mcp-test", name="MCP project", workspace_id=workspace_id)
    with sqlite3.connect(path) as connection:
        connection.executemany(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            [
                ("workspace", workspace.id, workspace.model_dump_json()),
                ("project", project.id, project.model_dump_json()),
            ],
        )


def test_sqlite_create_and_update_commit_audit_with_registry_row(tmp_path: Path) -> None:
    path = tmp_path / "mcp-registry.sqlite3"
    repository = _sqlite_repository(path)
    _seed_sqlite_project(path, "workspace-mcp-test")
    server = _server()
    repository.create(server=server, audit_event=_audit(server))

    updated = server.model_copy(update={"enabled": False, "updated_at": server.updated_at.replace(year=2030)})
    update_event = _audit(
        updated,
        audit_id="audit-mcp-update-test",
    ).model_copy(update={"event_type": "mcp.child_server.disabled"})
    repository.update(
        server=updated,
        audit_event=update_event,
        expected_updated_at=server.updated_at.isoformat(),
    )

    assert repository.get(server.id) == updated
    assert repository.list_for_workspace(server.workspace_id, enabled=False) == [updated]
    with sqlite3.connect(path) as connection:
        records = connection.execute(
            "SELECT entity_type,count(*) FROM kv_store "
            "WHERE entity_type IN ('mcp_child_server','audit_event') GROUP BY entity_type"
        ).fetchall()
    assert records == [("audit_event", 2), ("mcp_child_server", 1)]


def test_sqlite_rejects_cross_workspace_project_without_writing(tmp_path: Path) -> None:
    path = tmp_path / "mcp-scope.sqlite3"
    repository = _sqlite_repository(path)
    _seed_sqlite_project(path, "workspace-one")
    server = _server("workspace-two")

    with pytest.raises(MCPChildServerScopeError, match="workspace does not match"):
        repository.create(server=server, audit_event=_audit(server))

    assert repository.get(server.id) is None


def test_sqlite_rejects_stale_update_and_duplicate_project_name(tmp_path: Path) -> None:
    path = tmp_path / "mcp-conflict.sqlite3"
    repository = _sqlite_repository(path)
    _seed_sqlite_project(path, "workspace-mcp-test")
    server = _server()
    repository.create(server=server, audit_event=_audit(server))

    changed = server.model_copy(update={"enabled": False, "updated_at": server.updated_at.replace(year=2030)})
    with pytest.raises(MCPChildServerConflict, match="changed"):
        repository.update(
            server=changed,
            audit_event=_audit(changed, audit_id="audit-stale"),
            expected_updated_at="2000-01-01T00:00:00+00:00",
        )

    duplicate = _server(name="local")
    duplicate = duplicate.model_copy(update={"id": "mcp-server-duplicate"})
    with pytest.raises(MCPChildServerConflict, match="name already exists"):
        repository.create(
            server=duplicate,
            audit_event=_audit(duplicate, audit_id="audit-duplicate"),
        )


def test_application_bundle_exposes_mcp_child_server_repository() -> None:
    repositories = ApplicationRepositories.from_connector(
        lambda: pytest.fail("constructing repository bundle must not open a connection")
    )

    assert isinstance(repositories.mcp_child_servers, PostgresMCPChildServerRepository)


@pytest.fixture
def postgres_mcp_child_server_repository() -> Iterator[
    tuple[PostgresMCPChildServerRepository, Callable[[], Connection], str]
]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is not configured")
    from psycopg import sql

    schema = "mcp_repo_" + uuid.uuid4().hex

    def connect() -> Connection:
        connection = Connection.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    with Connection.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        run_postgres_migrations(connect)
        migration_ids = [migration.migration_id for migration in POSTGRES_MIGRATIONS]
        assert migration_ids.index("pg_0011_workspace_scoped_mcp_child_servers") < migration_ids.index(
            "pg_0012_workspace_scoped_mcp_pending_requests"
        )
        yield PostgresMCPChildServerRepository(connect), connect, schema
    finally:
        with Connection.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _seed_postgres_project(connect: Callable[[], Connection]) -> None:
    workspace = Workspace(id="workspace-mcp-test", name="MCP test", owner_user_id="owner-mcp-test")
    project = Project(id="project-mcp-test", name="MCP project", workspace_id=workspace.id)
    with connect() as connection:
        connection.execute(
            "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
            (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
        )
        connection.execute(
            "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
            (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))),
        )


def test_postgres_migration_is_registered_after_pg0010(
    postgres_mcp_child_server_repository,
) -> None:
    repository, connect, _ = postgres_mcp_child_server_repository
    _seed_postgres_project(connect)
    server = _server()

    repository.create(server=server, audit_event=_audit(server))

    assert repository.get(server.id) == server
    assert repository.list_for_workspace(server.workspace_id) == [server]


def test_postgres_rejects_cross_workspace_scope_and_audit_failure_rolls_back(
    postgres_mcp_child_server_repository,
) -> None:
    repository, connect, _ = postgres_mcp_child_server_repository
    _seed_postgres_project(connect)
    wrong_scope = _server("workspace-other")
    with pytest.raises(MCPChildServerScopeError, match="workspace does not match"):
        repository.create(server=wrong_scope, audit_event=_audit(wrong_scope))

    server = _server()
    repository.create(server=server, audit_event=_audit(server))
    changed = server.model_copy(update={"enabled": False, "updated_at": server.updated_at.replace(year=2030)})
    with pytest.raises(UniqueViolation):
        repository.update(
            server=changed,
            audit_event=_audit(changed),
            expected_updated_at=server.updated_at.isoformat(),
        )
    assert repository.get(server.id) == server
    with connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM audit_events WHERE entity_type='mcp_child_server'"
        ).fetchone()[0] == 1
