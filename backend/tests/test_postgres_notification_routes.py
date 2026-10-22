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

from app import main, notifications
from app.application_repositories import ApplicationRepositories
from app.models import (
    ApprovalRequest,
    ApprovalStatus,
    AuditEvent,
    AuthUser,
    FileChange,
    Notification,
    Project,
    RiskAssessment,
    RiskLevel,
    User,
    UserRole,
    Workspace,
)
from app.notifications import AuthDependency
from app.postgres_database import run_postgres_migrations
from app.store import store


@pytest.fixture
def postgres_notification_route_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Callable[[], Connection], Workspace, AuthUser, Notification]]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL notification route tests")
    schema = f"ct_notification_routes_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    try:
        run_postgres_migrations(connect)
        owner = User(
            id=f"user_{uuid.uuid4().hex}",
            email=f"owner-{uuid.uuid4().hex}@example.test",
            display_name="Notification owner",
            role=UserRole.OWNER,
            password_hash="hash",
            password_salt="salt",
            workspace_id=f"workspace_{uuid.uuid4().hex}",
            email_verified=True,
        )
        workspace = Workspace(
            id=owner.workspace_id,
            name="Notification routes",
            owner_user_id=owner.id,
        )
        project = Project(
            id=f"project_{uuid.uuid4().hex}",
            name="Notification project",
            workspace_id=workspace.id,
        )
        with connect() as connection:
            connection.execute(
                "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
                (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
            )
            connection.execute(
                "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,email_verified,payload) "
                "VALUES(%s,%s,%s,%s,%s,FALSE,TRUE,%s)",
                (
                    owner.id,
                    workspace.id,
                    owner.email,
                    owner.email.lower(),
                    owner.role.value,
                    Jsonb(owner.model_dump(mode="json")),
                ),
            )
            connection.execute(
                "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
                (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))),
            )

        repositories = ApplicationRepositories.from_connector(connect)
        monkeypatch.setattr(
            main.app.state,
            "notification_outbox_repository",
            repositories.notification_outbox,
            raising=False,
        )
        monkeypatch.setattr(
            main.app.state,
            "workspace_membership_repository",
            repositories.workspace_membership,
            raising=False,
        )
        user = AuthUser(
            id=owner.id,
            email=owner.email,
            display_name=owner.display_name,
            role=owner.role,
            workspace_id=workspace.id,
        )
        monkeypatch.setitem(main.app.dependency_overrides, AuthDependency.dependency, lambda: user)
        approval = ApprovalRequest(
            id=f"approval_{uuid.uuid4().hex}",
            project_id=project.id,
            workspace_id=workspace.id,
            title="Pending approval",
            summary="Notification route fixture",
            target_branch="main",
            files=[FileChange(path="README.md", diff="+review")],
            risk=RiskAssessment(level=RiskLevel.LOW),
            status=ApprovalStatus.REQUESTED,
        )
        notification = Notification(
            title="Approval needed",
            body="Open Control Tower to review.",
            entity_type="approval_request",
            entity_id=approval.id,
            workspace_id=workspace.id,
        )
        repositories.approval_decisions.create(
            approval=approval,
            audit_event=AuditEvent(
                event_type="approval.created",
                entity_id=approval.id,
                workspace_id=workspace.id,
                details={"project_id": project.id},
            ),
            notification=notification,
        )
        yield connect, workspace, user, notification
    finally:
        main.app.dependency_overrides.pop(AuthDependency.dependency, None)
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_postgres_notification_routes_use_scoped_repository_without_sqlite_projection(
    postgres_notification_route_runtime,
) -> None:
    connect, _workspace, _user, notification = postgres_notification_route_runtime
    client = TestClient(main.app)
    before_notifications = set(store.notifications)
    before_devices = set(store.device_registrations)
    before_audits = {event.id for event in store.audit_events}

    listed = client.get("/notifications")
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()] == [notification.id]

    marked = client.post(f"/notifications/{notification.id}/read")
    assert marked.status_code == 200, marked.text
    assert marked.json()["status"] == "READ"
    with connect() as connection:
        row = connection.execute(
            "SELECT payload->>'status' FROM notifications WHERE notification_id=%s",
            (notification.id,),
        ).fetchone()
        assert row == ("READ",)

    registered = client.post(
        "/notifications/devices",
        json={"token": f"fcm-token-{uuid.uuid4().hex}", "app_version": "1.0.0"},
    )
    assert registered.status_code == 200, registered.text
    device_id = registered.json()["id"]
    assert registered.json()["workspace_id"] == notification.workspace_id
    removed = client.delete(f"/notifications/devices/{device_id}")
    assert removed.status_code == 204
    with connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM device_registrations WHERE device_id=%s", (device_id,)
        ).fetchone() is None

    assert set(store.notifications) == before_notifications
    assert set(store.device_registrations) == before_devices
    assert {event.id for event in store.audit_events} == before_audits


def test_postgres_notification_routes_reject_claimed_foreign_workspace(
    postgres_notification_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect, workspace, user, _notification = postgres_notification_route_runtime
    foreign_claim = user.model_copy(update={"workspace_id": f"workspace_{uuid.uuid4().hex}"})
    monkeypatch.setitem(
        main.app.dependency_overrides,
        AuthDependency.dependency,
        lambda: foreign_claim,
    )

    response = TestClient(main.app).get("/notifications")

    assert response.status_code == 403
    assert workspace.id != foreign_claim.workspace_id


def test_notification_routes_fail_closed_without_postgres_repository_when_auth_uses_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = AuthUser(
        id=f"user_{uuid.uuid4().hex}",
        email="postgres-auth@example.test",
        display_name="PostgreSQL auth user",
        role=UserRole.OWNER,
        workspace_id=f"workspace_{uuid.uuid4().hex}",
    )
    monkeypatch.setattr(
        main.app.state,
        "notification_outbox_repository",
        None,
        raising=False,
    )
    monkeypatch.setattr(
        notifications,
        "get_settings",
        lambda: SimpleNamespace(postgres_auth_enabled=True),
    )
    monkeypatch.setitem(main.app.dependency_overrides, AuthDependency.dependency, lambda: user)
    before_notifications = set(store.notifications)
    before_devices = set(store.device_registrations)
    before_audits = {event.id for event in store.audit_events}

    response = TestClient(main.app).get("/notifications")

    assert response.status_code == 503
    assert response.json()["detail"] == "PostgreSQL notification repository is unavailable"
    assert set(store.notifications) == before_notifications
    assert set(store.device_registrations) == before_devices
    assert {event.id for event in store.audit_events} == before_audits
