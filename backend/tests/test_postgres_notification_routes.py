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
        active_runtime = SimpleNamespace(repositories=repositories)
        monkeypatch.setattr(main.app.state, "postgres_runtime", active_runtime, raising=False)
        monkeypatch.setattr(main.app.state, "postgres_repositories", repositories, raising=False)
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
        device_events = connection.execute(
            "SELECT event_type FROM audit_events WHERE entity_type='device_registration' "
            "AND entity_id=%s ORDER BY created_at,event_id",
            (device_id,),
        ).fetchall()
        assert [row[0] for row in device_events] == [
            "notification.device_registered",
            "notification.device_unregistered",
        ]

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


def test_postgres_device_unregister_does_not_delete_on_workspace_claim_mismatch(
    postgres_notification_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, workspace, user, _notification = postgres_notification_route_runtime
    device = main.app.state.notification_outbox_repository.register_device(
        notifications.DeviceRegistration(
            user_id=user.id,
            workspace_id=workspace.id,
            token=f"fcm-token-{uuid.uuid4().hex}",
        )
    ).device
    foreign_claim = user.model_copy(update={"workspace_id": f"workspace_{uuid.uuid4().hex}"})
    monkeypatch.setitem(
        main.app.dependency_overrides,
        AuthDependency.dependency,
        lambda: foreign_claim,
    )

    response = TestClient(main.app).delete(f"/notifications/devices/{device.id}")

    # Unknown/foreign registrations are an idempotent no-op. A mismatched
    # workspace claim must not delete the existing registration.
    assert response.status_code == 204
    with connect() as connection:
        assert connection.execute(
            "SELECT user_id,workspace_id FROM device_registrations WHERE device_id=%s",
            (device.id,),
        ).fetchone() == (user.id, workspace.id)


@pytest.mark.parametrize(
    "postgres_mode",
    ["postgres_auth_enabled", "postgres_runtime_probe_enabled"],
)
def test_notification_routes_fail_closed_without_postgres_repository_when_postgres_is_active(
    monkeypatch: pytest.MonkeyPatch,
    postgres_mode: str,
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
        lambda: SimpleNamespace(**{postgres_mode: True}),
    )
    monkeypatch.setitem(main.app.dependency_overrides, AuthDependency.dependency, lambda: user)
    before_notifications = set(store.notifications)
    before_devices = set(store.device_registrations)
    before_audits = {event.id for event in store.audit_events}

    response = TestClient(main.app).get("/notifications")

    assert response.status_code == 503
    assert response.json()["detail"] == "PostgreSQL notification runtime is unavailable"
    assert set(store.notifications) == before_notifications
    assert set(store.device_registrations) == before_devices
    assert {event.id for event in store.audit_events} == before_audits


@pytest.mark.parametrize(
    "postgres_mode",
    ["postgres_auth_enabled", "postgres_runtime_probe_enabled"],
)
def test_device_registration_fails_closed_without_postgres_repository_in_postgres_mode(
    monkeypatch: pytest.MonkeyPatch,
    postgres_mode: str,
) -> None:
    user = AuthUser(
        id=f"user_{uuid.uuid4().hex}",
        email="postgres-auth@example.test",
        display_name="PostgreSQL auth user",
        role=UserRole.OWNER,
        workspace_id=f"workspace_{uuid.uuid4().hex}",
    )
    monkeypatch.setattr(
        notifications,
        "get_settings",
        lambda: SimpleNamespace(**{postgres_mode: True}),
    )
    monkeypatch.setattr(main.app.state, "postgres_runtime", None, raising=False)
    monkeypatch.setattr(main.app.state, "postgres_repositories", None, raising=False)
    monkeypatch.setattr(main.app.state, "notification_outbox_repository", None, raising=False)
    monkeypatch.setitem(main.app.dependency_overrides, AuthDependency.dependency, lambda: user)
    before_notifications = set(store.notifications)
    before_devices = set(store.device_registrations)
    before_audits = {event.id for event in store.audit_events}

    response = TestClient(main.app).post(
        "/notifications/devices",
        json={"token": f"fcm-token-{uuid.uuid4().hex}"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "PostgreSQL notification runtime is unavailable"
    assert set(store.notifications) == before_notifications
    assert set(store.device_registrations) == before_devices
    assert {event.id for event in store.audit_events} == before_audits
    main.app.dependency_overrides.pop(AuthDependency.dependency, None)


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        (
            "post",
            "/notifications/devices",
            {"json": {"token": f"fcm-token-{uuid.uuid4().hex}"}},
        ),
        ("delete", f"/notifications/devices/device_{uuid.uuid4().hex}", {}),
    ],
)
def test_postgres_device_mutations_fail_closed_without_runtime_bundle(
    method: str,
    path: str,
    kwargs: dict[str, object],
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
        notifications,
        "get_settings",
        lambda: SimpleNamespace(postgres_auth_enabled=True),
    )
    monkeypatch.setattr(main.app.state, "postgres_runtime", None, raising=False)
    monkeypatch.setattr(main.app.state, "postgres_repositories", None, raising=False)
    monkeypatch.setattr(main.app.state, "notification_outbox_repository", None, raising=False)
    monkeypatch.setitem(main.app.dependency_overrides, AuthDependency.dependency, lambda: user)
    before_devices = set(store.device_registrations)
    before_audits = {event.id for event in store.audit_events}

    response = getattr(TestClient(main.app), method)(path, **kwargs)

    assert response.status_code == 503
    assert response.json()["detail"] == "PostgreSQL notification runtime is unavailable"
    assert set(store.device_registrations) == before_devices
    assert {event.id for event in store.audit_events} == before_audits
    main.app.dependency_overrides.pop(AuthDependency.dependency, None)


def test_notification_route_rejects_repository_from_another_runtime(
    postgres_notification_route_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect, _workspace, _user, _notification = postgres_notification_route_runtime
    monkeypatch.setattr(
        notifications,
        "get_settings",
        lambda: SimpleNamespace(postgres_auth_enabled=True),
    )
    main.app.state.postgres_runtime = SimpleNamespace(repositories=object())

    response = TestClient(main.app).get("/notifications")

    assert response.status_code == 503
    assert response.json()["detail"] == "PostgreSQL notification runtime is unavailable"
