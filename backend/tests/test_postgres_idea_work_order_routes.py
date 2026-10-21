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
from app.models import AuthUser, Idea, Project, User, UserRole, Workspace
from app.postgres_database import run_postgres_migrations
from app.store import store


@pytest.fixture
def postgres_idea_work_order_runtime(monkeypatch: pytest.MonkeyPatch) -> Iterator[
    tuple[Callable[[], Connection], Workspace, Project]
]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL idea/work-order route tests")
    schema = f"ct_idea_work_order_routes_{uuid.uuid4().hex}"
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
        workspace = Workspace(id=owner.workspace_id, name="Idea/work-order routes", owner_user_id=owner.id)
        project = Project(id=f"project_{uuid.uuid4().hex}", name="Project", workspace_id=workspace.id)
        with connect() as conn:
            conn.execute("INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
                         (workspace.id, Jsonb(workspace.model_dump(mode="json"))))
            conn.execute(
                "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,email_verified,payload) "
                "VALUES(%s,%s,%s,%s,%s,FALSE,TRUE,%s)",
                (owner.id, workspace.id, owner.email, owner.email.lower(), owner.role.value,
                 Jsonb(owner.model_dump(mode="json"))),
            )
            conn.execute("INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
                         (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))))
        repositories = ApplicationRepositories.from_connector(connect)
        for name, value in {
            "project_repository": repositories.projects,
            "idea_repository": repositories.ideas,
            "work_order_repository": repositories.work_orders,
            "workspace_membership_repository": repositories.workspace_membership,
        }.items():
            monkeypatch.setattr(main.app.state, name, value, raising=False)
        user = AuthUser(id=owner.id, email=owner.email, display_name=owner.display_name,
                        role=owner.role, workspace_id=workspace.id)
        monkeypatch.setitem(main.app.dependency_overrides, require_maintainer, lambda: user)
        monkeypatch.setitem(main.app.dependency_overrides, require_reviewer, lambda: user)
        yield connect, workspace, project
    finally:
        main.app.dependency_overrides.pop(require_maintainer, None)
        main.app.dependency_overrides.pop(require_reviewer, None)
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_idea_and_work_order_http_flow_uses_postgres_only(postgres_idea_work_order_runtime) -> None:
    connect, workspace, project = postgres_idea_work_order_runtime
    before_ideas, before_orders = set(store.ideas), set(store.work_orders)
    before_audits = {event.id for event in store.audit_events}
    client = TestClient(main.app)
    idea_response = client.post("/ideas", json={
        "project_id": project.id, "title": "Approval workflow", "raw_text": "Use the phone gate."
    })
    assert idea_response.status_code == 200, idea_response.text
    idea = idea_response.json()
    order_response = client.post("/work-orders", json={
        "project_id": project.id, "idea_id": idea["id"],
        "goal": "Implement a scoped approval workflow", "tasks": ["Persist request", "Audit decision"],
    })
    assert order_response.status_code == 200, order_response.text
    order = order_response.json()
    assert order["workspace_id"] == workspace.id
    assert client.get("/ideas").json() == [idea]
    assert client.get("/work-orders").json() == [order]
    with connect() as conn:
        assert conn.execute("SELECT count(*) FROM ideas WHERE idea_id=%s AND workspace_id=%s",
                            (idea["id"], workspace.id)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM work_orders WHERE work_order_id=%s AND workspace_id=%s",
                            (order["id"], workspace.id)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM audit_events WHERE workspace_id=%s AND entity_id IN (%s,%s)",
                            (workspace.id, idea["id"], order["id"])).fetchone()[0] == 2
    assert set(store.ideas) == before_ideas
    assert set(store.work_orders) == before_orders
    assert {event.id for event in store.audit_events} == before_audits


def test_work_order_cannot_link_an_idea_from_another_workspace(postgres_idea_work_order_runtime) -> None:
    connect, workspace, project = postgres_idea_work_order_runtime
    other_id = f"workspace_other_{uuid.uuid4().hex}"
    other = Workspace(id=other_id, name="Other", owner_user_id=f"owner_{uuid.uuid4().hex}")
    with connect() as conn:
        conn.execute("INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
                     (other.id, Jsonb(other.model_dump(mode="json"))))
        other_project = Project(id=f"project_{uuid.uuid4().hex}", name="Other", workspace_id=other.id)
        conn.execute("INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
                     (other_project.id, other.id, Jsonb(other_project.model_dump(mode="json"))))
        other_idea = Idea(
            project_id=other_project.id, title="Other workspace", raw_text="Never link across workspaces.",
            workspace_id=other.id,
        )
        conn.execute(
            "INSERT INTO ideas(idea_id,project_id,workspace_id,created_at,payload) VALUES(%s,%s,%s,%s,%s)",
            (other_idea.id, other_project.id, other.id, other_idea.created_at,
             Jsonb(other_idea.model_dump(mode="json"))),
        )
    client = TestClient(main.app)
    response = client.post("/work-orders", json={
        "project_id": project.id, "idea_id": other_idea.id,
        "goal": "Must not consume another workspace idea",
    })
    assert response.status_code == 404
    assert response.json()["detail"] == "Idea not found"
