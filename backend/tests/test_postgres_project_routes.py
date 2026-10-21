from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app import main
from app.application_repositories import ApplicationRepositories
from app.auth import require_maintainer, require_reviewer
from app.models import AuthUser, User, UserRole, Workspace
from app.postgres_database import run_postgres_migrations
from app.store import store


@pytest.fixture
def postgres_project_route_runtime(monkeypatch: pytest.MonkeyPatch) -> Iterator[
    tuple[Callable[[], Connection], Workspace]
]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL project route tests")
    schema = f"ct_project_routes_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    try:
        run_postgres_migrations(connect)
        owner = User(
            id=f"user_{uuid.uuid4().hex}", email=f"owner-{uuid.uuid4().hex}@example.test",
            display_name="Owner", role=UserRole.OWNER, password_hash="hash",
            password_salt="salt", workspace_id=f"workspace_{uuid.uuid4().hex}",
            email_verified=True,
        )
        workspace = Workspace(id=owner.workspace_id, name="Project routes", owner_user_id=owner.id)
        with connect() as conn:
            conn.execute("INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
                         (workspace.id, Jsonb(workspace.model_dump(mode="json"))))
            conn.execute(
                "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,email_verified,payload) "
                "VALUES(%s,%s,%s,%s,%s,FALSE,TRUE,%s)",
                (owner.id, workspace.id, owner.email, owner.email.lower(), owner.role.value,
                 Jsonb(owner.model_dump(mode="json"))),
            )
        repositories = ApplicationRepositories.from_connector(connect)
        monkeypatch.setattr(main.app.state, "project_repository", repositories.projects, raising=False)
        monkeypatch.setattr(main.app.state, "workspace_membership_repository",
                            repositories.workspace_membership, raising=False)
        user = AuthUser(id=owner.id, email=owner.email, display_name=owner.display_name,
                        role=owner.role, workspace_id=workspace.id)
        monkeypatch.setitem(main.app.dependency_overrides, require_maintainer, lambda: user)
        monkeypatch.setitem(main.app.dependency_overrides, require_reviewer, lambda: user)
        yield connect, workspace
    finally:
        main.app.dependency_overrides.pop(require_maintainer, None)
        main.app.dependency_overrides.pop(require_reviewer, None)
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_project_http_create_and_list_use_postgres_without_sqlite_projection(
    postgres_project_route_runtime,
) -> None:
    connect, workspace = postgres_project_route_runtime
    before_projects = set(store.projects)
    before_audits = {event.id for event in store.audit_events}
    client = TestClient(main.app)
    response = client.post("/projects", json={"name": "Persisted project", "description": "test"})
    assert response.status_code == 200, response.text
    project = response.json()
    assert project["workspace_id"] == workspace.id
    assert client.get("/projects").json() == [project]
    with connect() as conn:
        assert conn.execute("SELECT workspace_id FROM projects WHERE project_id=%s",
                            (project["id"],)).fetchone() == (workspace.id,)
        audit_row = conn.execute(
            "SELECT event_type,entity_id FROM audit_events WHERE workspace_id=%s AND entity_id=%s",
            (workspace.id, project["id"]),
        ).fetchone()
    assert audit_row == ("project.created", project["id"])
    assert set(store.projects) == before_projects
    assert {event.id for event in store.audit_events} == before_audits
