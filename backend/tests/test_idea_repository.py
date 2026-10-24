from __future__ import annotations

import os
import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from app.database_migrations import run_migrations
from app.idea_repository import (
    IdeaRepositoryScopeError,
    PostgresIdeaRepository,
    SQLiteIdeaRepository,
)
from app.models import AuditEvent, Idea, Project, User, UserRole, Workspace
from app.postgres_database import run_postgres_migrations


def _idea(workspace_id: str, project_id: str | None = None, *, idea_id: str = "idea_repo") -> Idea:
    return Idea(
        id=idea_id,
        workspace_id=workspace_id,
        project_id=project_id,
        title="Approval workflow",
        raw_text="Route exact native-agent decisions to mobile.",
    )


def _audit(idea: Idea, *, event_id: str = "audit_idea_repo") -> AuditEvent:
    return AuditEvent(
        id=event_id,
        event_type="idea.created",
        entity_id=idea.id,
        workspace_id=idea.workspace_id,
        actor="owner@example.test",
        details={"title": idea.title, "project_id": idea.project_id},
    )


def _sqlite_repository(path: Path) -> tuple[SQLiteIdeaRepository, Callable[[], sqlite3.Connection]]:
    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(path, timeout=10)
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    with connect() as connection:
        run_migrations(connection)
    return SQLiteIdeaRepository(connect), connect


def _sqlite_workspace(connect: Callable[[], sqlite3.Connection], workspace_id: str) -> None:
    workspace = Workspace(id=workspace_id, name="Idea workspace", owner_user_id="owner")
    with connect() as connection:
        connection.execute(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            ("workspace", workspace.id, workspace.model_dump_json()),
        )


def test_sqlite_idea_and_audit_commit_together_and_read_after_cache_loss(tmp_path: Path) -> None:
    repository, connect = _sqlite_repository(tmp_path / "ideas.sqlite3")
    _sqlite_workspace(connect, "workspace_ideas")
    idea = _idea("workspace_ideas")
    audit_event = _audit(idea)

    repository.create(idea=idea, audit_event=audit_event)

    assert repository.get(idea.id) == idea
    assert repository.list_for_workspace("workspace_ideas") == [idea]
    with connect() as connection:
        assert connection.execute(
            "SELECT entity_type FROM kv_store WHERE entity_id IN (?,?) ORDER BY entity_type",
            (idea.id, audit_event.id),
        ).fetchall() == [("audit_event",), ("idea",)]


def test_sqlite_idea_write_rolls_back_if_audit_insert_fails(tmp_path: Path) -> None:
    repository, connect = _sqlite_repository(tmp_path / "ideas_rollback.sqlite3")
    _sqlite_workspace(connect, "workspace_ideas")
    idea = _idea("workspace_ideas")
    audit_event = _audit(idea)
    with connect() as connection:
        connection.execute(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            ("audit_event", audit_event.id, audit_event.model_dump_json()),
        )

    with pytest.raises(sqlite3.IntegrityError):
        repository.create(idea=idea, audit_event=audit_event)

    assert repository.get(idea.id) is None


def test_sqlite_idea_requires_existing_same_workspace_project(tmp_path: Path) -> None:
    repository, connect = _sqlite_repository(tmp_path / "ideas_scope.sqlite3")
    _sqlite_workspace(connect, "workspace_ideas")
    idea = _idea("workspace_ideas", "missing_project")

    with pytest.raises(IdeaRepositoryScopeError, match="project does not exist"):
        repository.create(idea=idea, audit_event=_audit(idea))

    assert repository.get(idea.id) is None


@pytest.fixture
def postgres_idea_repository() -> Any:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is not configured")
    schema = "idea_repo_" + uuid.uuid4().hex

    def connect():
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    with psycopg.connect(dsn) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        run_postgres_migrations(connect)
        workspace = Workspace(id="workspace_idea_repo", name="Ideas", owner_user_id="owner_idea_repo")
        user = User(
            id=workspace.owner_user_id,
            email="owner@example.test",
            role=UserRole.OWNER,
            password_hash="test-hash",
            password_salt="test-salt",
            workspace_id=workspace.id,
        )
        project = Project(id="project_idea_repo", name="Ideas", workspace_id=workspace.id)
        with connect() as connection:
            connection.execute(
                "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
                (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
            )
            connection.execute(
                "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,"
                "email_verified,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (user.id, workspace.id, user.email, user.email.lower(), user.role.value,
                 user.disabled, user.email_verified, Jsonb(user.model_dump(mode="json"))),
            )
            connection.execute(
                "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
                (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))),
            )
        yield PostgresIdeaRepository(connect), connect, workspace.id, project.id
    finally:
        with psycopg.connect(dsn) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_postgres_idea_and_audit_commit_together(
    postgres_idea_repository: tuple[PostgresIdeaRepository, Callable[[], Any], str, str],
) -> None:
    repository, connect, workspace_id, project_id = postgres_idea_repository
    idea = _idea(workspace_id, project_id)
    audit_event = _audit(idea)

    repository.create(idea=idea, audit_event=audit_event)

    assert repository.get(idea.id) == idea
    assert repository.list_for_workspace(workspace_id) == [idea]
    with connect() as connection:
        assert connection.execute(
            "SELECT workspace_id,entity_type,entity_id,event_type FROM audit_events WHERE event_id=%s",
            (audit_event.id,),
        ).fetchone() == (workspace_id, "idea", idea.id, "idea.created")


def test_postgres_idea_listing_requires_workspace(
    postgres_idea_repository: tuple[PostgresIdeaRepository, Callable[[], Any], str, str],
) -> None:
    repository, _connect, _workspace_id, _project_id = postgres_idea_repository
    with pytest.raises(IdeaRepositoryScopeError, match="requires a workspace"):
        repository.list_for_workspace()


def test_postgres_idea_project_scope_mismatch_writes_nothing(
    postgres_idea_repository: tuple[PostgresIdeaRepository, Callable[[], Any], str, str],
) -> None:
    repository, connect, workspace_id, _project_id = postgres_idea_repository
    idea = _idea(workspace_id, "project_outside_workspace")

    with pytest.raises(IdeaRepositoryScopeError, match="project workspace does not match"):
        repository.create(idea=idea, audit_event=_audit(idea))

    with connect() as connection:
        assert connection.execute("SELECT count(*) FROM ideas").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM audit_events").fetchone()[0] == 0
