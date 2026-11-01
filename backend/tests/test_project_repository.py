from __future__ import annotations

import os
import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from app.database_migrations import run_migrations
from app.models import AuditEvent, AuthUser, Project, ProjectCreate, User, UserRole, Workspace
from app.project_repository import (
    PostgresProjectRepository,
    ProjectRepositoryScopeError,
    SQLiteProjectRepository,
)
from app.postgres_database import run_postgres_migrations
from app.workspace_membership_repository import SQLiteWorkspaceMembershipRepository


def _project(workspace_id: str | None, user_id: str | None = None) -> Project:
    return Project(
        id="project_repository_test",
        name="Repository project",
        workspace_id=workspace_id,
        created_by_user_id=user_id,
    )


def _audit(project: Project, *, event_id: str = "audit_project_repository_test") -> AuditEvent:
    return AuditEvent(
        id=event_id,
        event_type="project.created",
        entity_id=project.id,
        workspace_id=project.workspace_id,
        actor="owner@example.test",
        details={"name": project.name, "mode": project.mode},
    )


def _sqlite_repository(path: Path) -> SQLiteProjectRepository:
    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(path, timeout=10)
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    with connect() as connection:
        run_migrations(connection)
    return SQLiteProjectRepository(connect)


def test_sqlite_project_and_audit_commit_together(tmp_path: Path) -> None:
    database = tmp_path / "project_repository.sqlite3"
    repository = _sqlite_repository(database)
    project = _project(None)
    audit_event = _audit(project)

    repository.create(project=project, audit_event=audit_event)

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT entity_type, entity_id FROM kv_store ORDER BY entity_type"
        ).fetchall()
    assert rows == [
        ("audit_event", audit_event.id),
        ("project", project.id),
    ]


def test_sqlite_get_reads_persisted_project_after_cache_is_empty(tmp_path: Path) -> None:
    database = tmp_path / "project_repository_get.sqlite3"
    repository = _sqlite_repository(database)
    project = _project(None)
    repository.create(project=project, audit_event=_audit(project))

    assert repository.get(project.id) == project
    assert repository.get("missing_project") is None


def test_sqlite_workspace_listing_reads_persisted_projects_after_cache_is_empty(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project_repository_list.sqlite3"
    repository = _sqlite_repository(database)
    first = _project("workspace_project_list")
    second = Project(
        id="project_repository_other_workspace",
        name="Other workspace",
        workspace_id="workspace_other",
    )
    with sqlite3.connect(database) as connection:
        for workspace_id in (first.workspace_id, second.workspace_id):
            workspace = Workspace(
                id=workspace_id,
                name=workspace_id,
                owner_user_id="workspace_owner_" + str(workspace_id),
            )
            connection.execute(
                "INSERT INTO kv_store(entity_type, entity_id, payload) VALUES (?, ?, ?)",
                ("workspace", workspace.id, workspace.model_dump_json()),
            )
    repository.create(project=first, audit_event=_audit(first))
    repository.create(project=second, audit_event=_audit(second, event_id="audit_other_project"))

    assert repository.list_for_workspace("workspace_project_list") == [first]
    assert repository.list_for_workspace() == sorted(
        [first, second], key=lambda project: project.id
    )


def test_project_routes_read_durable_projects_without_process_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import main

    database = tmp_path / "project_repository_route.sqlite3"
    repository = _sqlite_repository(database)
    project = _project(None)
    repository.create(project=project, audit_event=_audit(project))
    monkeypatch.setattr(main, "project_repository", repository)
    monkeypatch.setattr(main, "_workspace_id_for_user", lambda _user: None)

    result = main.list_projects(
        AuthUser(
            id="project-list-user",
            email="reviewer@example.test",
            display_name="Reviewer",
            role=UserRole.REVIEWER,
        )
    )

    assert result == [project]


def test_project_routes_use_repository_bound_to_request_app(
    tmp_path: Path,
) -> None:
    from app import main

    database = tmp_path / "project_repository_app_binding.sqlite3"
    repository = _sqlite_repository(database)
    workspace = Workspace(id="workspace-app-binding", name="App bound", owner_user_id="app-binding-user")
    owner = User(
        id="app-binding-user",
        email="owner@example.test",
        display_name="Owner",
        role=UserRole.OWNER,
        password_hash="hash",
        password_salt="salt",
        workspace_id=workspace.id,
    )
    with repository._connect() as connection:
        connection.executemany(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            [
                ("workspace", workspace.id, workspace.model_dump_json()),
                ("user", owner.id, owner.model_dump_json()),
            ],
        )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                project_repository=repository,
                workspace_membership_repository=SQLiteWorkspaceMembershipRepository(
                    repository._connect
                ),
            )
        )
    )
    user = AuthUser(
        id=owner.id,
        email="owner@example.test",
        display_name="Owner",
        role=UserRole.OWNER,
        workspace_id=workspace.id,
    )

    created = main.create_project(
        ProjectCreate(name="App bound project"),
        user,
        request,
    )

    assert repository.get(created.id) == created
    assert created.workspace_id == workspace.id
    assert main.list_projects(user, request) == [created]


def test_sqlite_get_rejects_payload_identity_mismatch(tmp_path: Path) -> None:
    database = tmp_path / "project_repository_identity.sqlite3"
    repository = _sqlite_repository(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO kv_store(entity_type, entity_id, payload) VALUES (?, ?, ?)",
            ("project", "indexed_project", _project(None).model_dump_json()),
        )

    with pytest.raises(RuntimeError, match="identity is inconsistent"):
        repository.get("indexed_project")


def test_sqlite_audit_failure_rolls_back_project_insert(tmp_path: Path) -> None:
    database = tmp_path / "project_repository_rollback.sqlite3"
    repository = _sqlite_repository(database)
    project = _project(None)
    audit_event = _audit(project)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO kv_store(entity_type, entity_id, payload) VALUES (?, ?, ?)",
            ("audit_event", audit_event.id, audit_event.model_dump_json()),
        )

    with pytest.raises(sqlite3.IntegrityError):
        repository.create(project=project, audit_event=audit_event)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT 1 FROM kv_store WHERE entity_type='project' AND entity_id=?",
            (project.id,),
        ).fetchone() is None


def test_sqlite_rejects_missing_workspace_without_writing(tmp_path: Path) -> None:
    database = tmp_path / "project_repository_scope.sqlite3"
    repository = _sqlite_repository(database)
    project = _project("workspace_missing")

    with pytest.raises(ProjectRepositoryScopeError, match="workspace does not exist"):
        repository.create(project=project, audit_event=_audit(project))

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM kv_store WHERE entity_type IN ('project', 'audit_event')"
        ).fetchone()[0] == 0


def test_project_api_does_not_publish_cache_when_repository_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main
    from app.store import store

    class FailingProjectRepository:
        def create(self, *, project: Project, audit_event: AuditEvent) -> None:
            raise RuntimeError("simulated durable write failure")

    monkeypatch.setattr(main, "project_repository", FailingProjectRepository())
    monkeypatch.setattr(main, "_workspace_id_for_user", lambda _user: None)
    project_ids_before = set(store.projects)
    audit_ids_before = {event.id for event in store.audit_events}

    with pytest.raises(RuntimeError, match="simulated durable write failure"):
        main.create_project(
            ProjectCreate(name="Must not publish"),
            AuthUser(
                id="project-write-failure-user",
                email="owner@example.test",
                display_name="Owner",
                role=UserRole.OWNER,
            ),
        )

    assert set(store.projects) == project_ids_before
    assert {event.id for event in store.audit_events} == audit_ids_before


@pytest.fixture
def postgres_project_repository() -> Any:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is not configured")
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql

    schema = "project_repo_" + uuid.uuid4().hex

    def connect():
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    with psycopg.connect(dsn) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        run_postgres_migrations(connect)
        workspace = Workspace(id="workspace_project_repository", name="Project workspace", owner_user_id="user_project_repository")
        user = User(
            id=workspace.owner_user_id,
            email="owner@example.test",
            role=UserRole.OWNER,
            password_hash="test-hash",
            password_salt="test-salt",
            workspace_id=workspace.id,
            email_verified=True,
        )
        with connect() as connection:
            connection.execute(
                "INSERT INTO workspaces(workspace_id, payload) VALUES (%s, %s)",
                (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
            )
            connection.execute(
                "INSERT INTO users(user_id, workspace_id, email, normalized_email, role, "
                "disabled, email_verified, payload) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    user.id,
                    workspace.id,
                    user.email,
                    user.email.lower(),
                    user.role.value,
                    user.disabled,
                    user.email_verified,
                    Jsonb(user.model_dump(mode="json")),
                ),
            )
        yield PostgresProjectRepository(connect), connect, workspace.id, user.id
    finally:
        with psycopg.connect(dsn) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_postgres_project_and_audit_commit_together(
    postgres_project_repository: tuple[PostgresProjectRepository, Callable[[], Any], str, str],
) -> None:
    repository, connect, workspace_id, user_id = postgres_project_repository
    project = _project(workspace_id, user_id)
    audit_event = _audit(project)

    repository.create(project=project, audit_event=audit_event)

    with connect() as connection:
        project_row = connection.execute(
            "SELECT workspace_id, payload->>'id' FROM projects WHERE project_id=%s",
            (project.id,),
        ).fetchone()
        audit_row = connection.execute(
            "SELECT workspace_id, entity_type, entity_id, event_type FROM audit_events WHERE event_id=%s",
            (audit_event.id,),
        ).fetchone()
    assert project_row == (workspace_id, project.id)
    assert audit_row == (workspace_id, "project", project.id, "project.created")
    assert repository.get(project.id) == project
    assert repository.list_for_workspace(workspace_id) == [project]


def test_postgres_project_listing_requires_workspace(
    postgres_project_repository: tuple[PostgresProjectRepository, Callable[[], Any], str, str],
) -> None:
    repository, _connect, _workspace_id, _user_id = postgres_project_repository

    with pytest.raises(ProjectRepositoryScopeError, match="requires a workspace"):
        repository.list_for_workspace()
    assert repository.get("missing_project") is None


def test_postgres_audit_failure_rolls_back_project_insert(
    postgres_project_repository: tuple[PostgresProjectRepository, Callable[[], Any], str, str],
) -> None:
    repository, connect, workspace_id, user_id = postgres_project_repository
    project = _project(workspace_id, user_id)
    audit_event = _audit(project)
    with connect() as connection:
        connection.execute(
            "INSERT INTO audit_events(event_id, workspace_id, approval_id, entity_type, "
            "entity_id, event_type, actor, details, created_at, payload) "
            "VALUES (%s,%s,NULL,'project',%s,%s,%s,%s,%s,%s)",
            (
                audit_event.id,
                workspace_id,
                project.id,
                audit_event.event_type,
                audit_event.actor,
                Jsonb(audit_event.details),
                audit_event.created_at,
                Jsonb(audit_event.model_dump(mode="json")),
            ),
        )

    with pytest.raises(psycopg.IntegrityError):
        repository.create(project=project, audit_event=audit_event)

    with connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM projects WHERE project_id=%s",
            (project.id,),
        ).fetchone() is None


def test_postgres_rejects_cross_workspace_creator(
    postgres_project_repository: tuple[PostgresProjectRepository, Callable[[], Any], str, str],
) -> None:
    repository, connect, workspace_id, _user_id = postgres_project_repository
    project = _project(workspace_id, "user_from_another_workspace")

    with pytest.raises(ProjectRepositoryScopeError, match="creator does not belong"):
        repository.create(project=project, audit_event=_audit(project))

    with connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM projects WHERE project_id=%s",
            (project.id,),
        ).fetchone() is None
