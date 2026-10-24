from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from types import SimpleNamespace

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app import main
from app.application_repositories import ApplicationRepositories
from app.auth import require_maintainer, require_owner, require_reviewer
from app.models import (
    AuthUser,
    Idea,
    Project,
    User,
    UserRole,
    Workspace,
)
from app.postgres_database import run_postgres_migrations
from app.store import store
from app.work_order_repository import PostgresWorkOrderRepository, WorkOrderRepositoryScopeError


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
        monkeypatch.setattr(
            "app.application_repository_access.get_settings",
            lambda: SimpleNamespace(postgres_auth_enabled=True),
        )
        for name, value in {
            "postgres_runtime": SimpleNamespace(repositories=repositories),
            "postgres_repositories": repositories,
            "project_repository": repositories.projects,
            "idea_repository": repositories.ideas,
            "work_order_repository": repositories.work_orders,
            "workspace_membership_repository": repositories.workspace_membership,
        }.items():
            monkeypatch.setattr(main.app.state, name, value, raising=False)
        user = AuthUser(id=owner.id, email=owner.email, display_name=owner.display_name,
                        role=owner.role, workspace_id=workspace.id)
        monkeypatch.setitem(main.app.dependency_overrides, require_maintainer, lambda: user)
        monkeypatch.setitem(main.app.dependency_overrides, require_owner, lambda: user)
        monkeypatch.setitem(main.app.dependency_overrides, require_reviewer, lambda: user)
        yield connect, workspace, project
    finally:
        main.app.dependency_overrides.pop(require_maintainer, None)
        main.app.dependency_overrides.pop(require_owner, None)
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


def test_approval_route_links_postgres_only_work_order_without_sqlite_fallback(
    postgres_idea_work_order_runtime,
) -> None:
    connect, workspace, project = postgres_idea_work_order_runtime
    client = TestClient(main.app)
    assert project.id not in store.projects
    created_work_order = client.post(
        "/work-orders",
        json={
            "project_id": project.id,
            "goal": "Prepare a change for approval",
            "tasks": ["Review exact diff", "Approve from mobile"],
        },
    )
    assert created_work_order.status_code == 200, created_work_order.text
    work_order = created_work_order.json()
    assert work_order["id"] not in store.work_orders

    before_approvals = set(store.approval_requests)
    before_notifications = set(store.notifications)
    before_audits = {event.id for event in store.audit_events}
    response = client.post(
        "/approvals",
        json={
            "project_id": project.id,
            "work_order_id": work_order["id"],
            "title": "Review the prepared change",
            "summary": "Approve the exact work order from the mobile console.",
            "target_branch": "main",
            "files": [{"path": "README.md", "diff": "+approved change"}],
        },
    )

    assert response.status_code == 200, response.text
    approval = response.json()
    assert approval["workspace_id"] == workspace.id
    assert approval["work_order_id"] == work_order["id"]
    with connect() as connection:
        persisted = connection.execute(
            "SELECT project_id,workspace_id,work_order_id FROM approval_requests "
            "WHERE approval_id=%s",
            (approval["id"],),
        ).fetchone()
        assert persisted == (project.id, workspace.id, work_order["id"])
        assert connection.execute(
            "SELECT count(*) FROM audit_events WHERE entity_id=%s AND workspace_id=%s "
            "AND event_type='approval.created'",
            (approval["id"], workspace.id),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM notifications WHERE approval_id=%s AND workspace_id=%s",
            (approval["id"], workspace.id),
        ).fetchone()[0] == 1
    assert approval["id"] not in store.approval_requests
    assert set(store.approval_requests) == before_approvals
    assert set(store.notifications) == before_notifications
    assert {event.id for event in store.audit_events} == before_audits


@pytest.mark.parametrize(
    ("failed_insert_table", "trigger_condition"),
    [
        ("audit_events", "NEW.event_type = 'approval.created'"),
        ("notifications", "TRUE"),
    ],
    ids=("audit-write-fails", "notification-outbox-write-fails"),
)
def test_approval_route_rolls_back_postgres_and_local_projections_on_side_effect_failure(
    postgres_idea_work_order_runtime,
    failed_insert_table: str,
    trigger_condition: str,
) -> None:
    connect, workspace, project = postgres_idea_work_order_runtime
    with connect() as connection:
        connection.execute(
            "CREATE FUNCTION fail_approval_side_effect_insert() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'injected approval side-effect failure'; END; "
            "$$ LANGUAGE plpgsql"
        )
        connection.execute(
            f"CREATE TRIGGER fail_approval_side_effect BEFORE INSERT ON {failed_insert_table} "
            f"FOR EACH ROW WHEN ({trigger_condition}) "
            "EXECUTE FUNCTION fail_approval_side_effect_insert()"
        )
        before_approval_count = connection.execute(
            "SELECT count(*) FROM approval_requests WHERE workspace_id=%s", (workspace.id,)
        ).fetchone()[0]
        before_creation_audit_count = connection.execute(
            "SELECT count(*) FROM audit_events WHERE workspace_id=%s "
            "AND event_type='approval.created'",
            (workspace.id,),
        ).fetchone()[0]
        before_notification_count = connection.execute(
            "SELECT count(*) FROM notifications WHERE workspace_id=%s "
            "AND entity_type='approval_request'",
            (workspace.id,),
        ).fetchone()[0]

    before_approvals = set(store.approval_requests)
    before_notifications = set(store.notifications)
    before_audits = {event.id for event in store.audit_events}
    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.post(
        "/approvals",
        json={
            "project_id": project.id,
            "title": "Failure injection must not leave a partial approval",
            "summary": "The approval, audit, and outbox must commit or roll back together.",
            "target_branch": "main",
            "files": [{"path": "README.md", "diff": "+atomic write"}],
        },
    )

    assert response.status_code == 500, response.text
    with connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM approval_requests WHERE workspace_id=%s", (workspace.id,)
        ).fetchone()[0] == before_approval_count
        assert connection.execute(
            "SELECT count(*) FROM audit_events WHERE workspace_id=%s "
            "AND event_type='approval.created'",
            (workspace.id,),
        ).fetchone()[0] == before_creation_audit_count
        assert connection.execute(
            "SELECT count(*) FROM notifications WHERE workspace_id=%s "
            "AND entity_type='approval_request'",
            (workspace.id,),
        ).fetchone()[0] == before_notification_count
    assert set(store.approval_requests) == before_approvals
    assert set(store.notifications) == before_notifications
    assert {event.id for event in store.audit_events} == before_audits


def test_external_agent_work_order_uses_postgres_project_and_idea_scope(
    postgres_idea_work_order_runtime, monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, workspace, project = postgres_idea_work_order_runtime
    idea = Idea(
        project_id=project.id,
        title="PostgreSQL only idea",
        raw_text="External agent can link this scoped idea.",
        workspace_id=workspace.id,
    )
    with connect() as connection:
        connection.execute(
            "INSERT INTO ideas(idea_id,project_id,workspace_id,created_at,payload) "
            "VALUES(%s,%s,%s,%s,%s)",
            (
                idea.id,
                project.id,
                workspace.id,
                idea.created_at,
                Jsonb(idea.model_dump(mode="json")),
            ),
        )

    client = TestClient(main.app)
    registration = client.post("/agents", json={
        "provider": "ANTIGRAVITY",
        "display_name": "PostgreSQL work-order agent",
        "auth_type": "API_KEY",
        "allowed_project_ids": [project.id],
        "allowed_actions": ["CREATE_WORK_ORDER"],
        "enabled": True,
    })
    assert registration.status_code == 200, registration.text
    agent = registration.json()["agent"]
    headers = {
        "X-Aixion-Agent-Id": agent["id"],
        "X-Aixion-Agent-Token": registration.json()["agent_token"],
    }
    before_orders = set(store.work_orders)
    before_audits = {event.id for event in store.audit_events}

    created = client.post(
        "/agents/work-orders",
        headers=headers,
        json={
            "project_id": project.id,
            "idea_id": idea.id,
            "goal": "Create the scoped PostgreSQL work order",
            "tasks": ["Persist work order", "Keep its audit paired"],
        },
    )
    assert created.status_code == 200, created.text
    work_order = created.json()
    assert work_order["workspace_id"] == workspace.id
    assert work_order["idea_id"] == idea.id
    assert work_order["source_agent_id"] == agent["id"]
    assert work_order["source_type"] == "AGENT_TASK"
    with connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM work_orders WHERE work_order_id=%s AND workspace_id=%s",
            (work_order["id"], workspace.id),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM audit_events WHERE entity_id=%s AND workspace_id=%s "
            "AND event_type='work_order.created_by_agent'",
            (work_order["id"], workspace.id),
        ).fetchone()[0] == 1
    assert work_order["id"] not in store.work_orders
    assert {event.id for event in store.audit_events} == before_audits
    assert set(store.work_orders) == before_orders

    foreign_workspace = Workspace(
        id=f"workspace_{uuid.uuid4().hex}",
        name="Foreign idea workspace",
        owner_user_id=f"owner_{uuid.uuid4().hex}",
    )
    foreign_project = Project(
        id=f"project_{uuid.uuid4().hex}",
        name="Foreign project",
        workspace_id=foreign_workspace.id,
    )
    foreign_idea = Idea(
        project_id=foreign_project.id,
        title="Foreign idea",
        raw_text="Must not be linked",
        workspace_id=foreign_workspace.id,
    )
    with connect() as connection:
        connection.execute(
            "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
            (foreign_workspace.id, Jsonb(foreign_workspace.model_dump(mode="json"))),
        )
        connection.execute(
            "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
            (
                foreign_project.id,
                foreign_workspace.id,
                Jsonb(foreign_project.model_dump(mode="json")),
            ),
        )
        connection.execute(
            "INSERT INTO ideas(idea_id,project_id,workspace_id,created_at,payload) "
            "VALUES(%s,%s,%s,%s,%s)",
            (
                foreign_idea.id,
                foreign_project.id,
                foreign_workspace.id,
                foreign_idea.created_at,
                Jsonb(foreign_idea.model_dump(mode="json")),
            ),
        )
    rejected = client.post(
        "/agents/work-orders",
        headers=headers,
        json={
            "project_id": project.id,
            "idea_id": foreign_idea.id,
            "goal": "Must reject a foreign-workspace idea",
        },
    )
    assert rejected.status_code == 404
    assert rejected.json()["detail"] == "Idea not found"
    assert set(store.work_orders) == before_orders
    with connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM work_orders WHERE workspace_id=%s",
            (workspace.id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM audit_events WHERE workspace_id=%s "
            "AND event_type='work_order.created_by_agent'",
            (workspace.id,),
        ).fetchone()[0] == 1

    def reject_changed_scope(self, *, work_order, audit_event) -> None:
        raise WorkOrderRepositoryScopeError("WorkOrder project scope changed before commit")

    monkeypatch.setattr(PostgresWorkOrderRepository, "create", reject_changed_scope)
    conflicted = client.post(
        "/agents/work-orders",
        headers=headers,
        json={
            "project_id": project.id,
            "goal": "Must return conflict when repository scope changes",
        },
    )
    assert conflicted.status_code == 409
    with connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM work_orders WHERE workspace_id=%s",
            (workspace.id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM audit_events WHERE workspace_id=%s "
            "AND event_type='work_order.created_by_agent'",
            (workspace.id,),
        ).fetchone()[0] == 1
