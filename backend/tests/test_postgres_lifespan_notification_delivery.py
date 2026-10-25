from __future__ import annotations

import os
from dataclasses import replace
from threading import Event
from typing import Any

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from app import main, postgres_notification_dispatcher
from app.postgres_notification_dispatcher import dispatch_one_postgres_push
from app.postgres_runtime import PostgresRuntime
from app.settings import get_settings
from app.store import store
from scripts import run_postgres_push_dispatch_once
from test_postgres_notification_outbox import _insert_device, _seed_notification

pytest_plugins = (
    "test_postgres_lifespan_approval_inbox",
    "test_postgres_notification_outbox",
)


def _approval_payload(project_id: str) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "title": "Dispatch approval from PostgreSQL",
        "summary": "The approval and notification must use the lifespan runtime.",
        "agent_name": "Antigravity lifespan integration",
        "target_branch": "feature/notification-delivery-test",
        "files": [
            {
                "path": "antigravity://tool/run_command",
                "change_type": "tool_call",
                "diff": '{"command":"echo notification-delivery-test"}',
            }
        ],
        "source_session_id": "notification-delivery-session",
        "source_invocation_id": "notification-delivery-invocation",
    }


def test_lifespan_approval_notification_is_dispatched_from_postgres_outbox(
    postgres_lifespan_approval_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, workspace, project, user, agent, agent_token = postgres_lifespan_approval_context
    before_approvals = set(store.approval_requests)
    before_notifications = set(store.notifications)
    before_devices = set(store.device_registrations)
    before_audits = {event.id for event in store.audit_events}
    sender_calls: list[dict[str, object]] = []
    dispatch_settings = replace(
        main.get_settings(),
        outbox_dispatch_mode="inline",
        push_dispatch_enabled=True,
        push_dispatch_poll_seconds=30,
    )
    monkeypatch.setattr(main, "get_settings", lambda: dispatch_settings)
    monkeypatch.setattr(
        postgres_notification_dispatcher,
        "get_settings",
        lambda: dispatch_settings,
    )
    dispatch_started = Event()
    release_dispatch = Event()
    dispatch_finished = Event()
    dispatched_repositories: list[object] = []
    dispatch_one = postgres_notification_dispatcher.dispatch_one_postgres_push

    def fake_sender(**kwargs: object) -> None:
        sender_calls.append(kwargs)
        return None

    def gated_dispatch(repository) -> str | None:
        dispatched_repositories.append(repository)
        dispatch_started.set()
        if not release_dispatch.wait(timeout=10):
            raise TimeoutError("test did not release the PostgreSQL dispatcher")
        result = dispatch_one(repository, sender=fake_sender)
        dispatch_finished.set()
        return result

    monkeypatch.setattr(
        postgres_notification_dispatcher,
        "dispatch_one_postgres_push",
        gated_dispatch,
    )

    headers = {
        "X-Aixion-Agent-Id": agent.id,
        "X-Aixion-Agent-Token": agent_token,
    }
    with TestClient(main.app) as client:
        runtime = client.app.state.postgres_runtime
        repositories = runtime.repositories
        assert client.app.state.postgres_repositories is repositories
        assert client.app.state.notification_outbox_repository is repositories.notification_outbox
        assert dispatch_started.wait(timeout=5), "lifespan did not start PostgreSQL push dispatch"

        device_response = client.post(
            "/notifications/devices",
            json={
                "token": "fake-device-token-for-postgres-dispatch",
                "platform": "android",
                "app_version": "integration-test",
            },
        )
        assert device_response.status_code == 200, device_response.text
        assert device_response.json()["workspace_id"] == workspace.id

        created = client.post("/agents/approvals", headers=headers, json=_approval_payload(project.id))
        assert created.status_code == 200, created.text
        approval = created.json()
        assert approval["workspace_id"] == workspace.id
        notification_row = repositories.notification_outbox.list_for_user(
            user_id=user.id,
            workspace_id=workspace.id,
        )
        assert len(notification_row) == 1
        notification = notification_row[0]
        assert notification.entity_type == "approval_request"
        assert notification.entity_id == approval["id"]
        assert notification.approval_request_id == approval["id"]
        assert notification.workspace_id == workspace.id
        assert notification.push_status == "PENDING"

        with connect() as connection:
            assert connection.execute(
                "SELECT status,workspace_id FROM approval_requests WHERE approval_id=%s",
                (approval["id"],),
            ).fetchone() == ("PENDING_REVIEW", workspace.id)
            assert connection.execute(
                "SELECT count(*) FROM audit_events WHERE entity_id=%s "
                "AND workspace_id=%s AND event_type='approval.created_by_agent'",
                (approval["id"], workspace.id),
            ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT status,workspace_id,approval_id FROM notifications WHERE notification_id=%s",
                (notification.id,),
            ).fetchone() == ("PENDING", workspace.id, approval["id"])

        release_dispatch.set()
        assert dispatch_finished.wait(timeout=10), "lifespan dispatcher did not finish the delivery"
        assert dispatched_repositories == [client.app.state.notification_outbox_repository]
        assert len(sender_calls) == 1
        assert sender_calls[0]["token"] == "fake-device-token-for-postgres-dispatch"
        assert sender_calls[0]["data"] == {
            "notification_id": notification.id,
            "entity_type": "approval_request",
            "entity_id": approval["id"],
        }

        dispatched = repositories.notification_outbox.list_for_user(
            user_id=user.id,
            workspace_id=workspace.id,
        )
        assert len(dispatched) == 1
        assert dispatched[0].id == notification.id
        assert dispatched[0].push_status == "SENT"
        assert dispatched[0].push_attempts == 1
    release_dispatch.set()

    assert set(store.approval_requests) == before_approvals
    assert set(store.notifications) == before_notifications
    assert set(store.device_registrations) == before_devices
    assert {event.id for event in store.audit_events} == before_audits


def test_lifespan_notification_insert_failure_rolls_back_and_sends_nothing(
    postgres_lifespan_approval_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect, workspace, project, _user, agent, agent_token = postgres_lifespan_approval_context
    with connect() as connection:
        connection.execute(
            """
            CREATE FUNCTION reject_approval_notification_insert() RETURNS trigger AS $$
            BEGIN
                IF NEW.approval_id IS NOT NULL THEN
                    RAISE EXCEPTION 'injected approval notification failure';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        connection.execute(
            """
            CREATE TRIGGER reject_approval_notification
            BEFORE INSERT ON notifications FOR EACH ROW
            EXECUTE FUNCTION reject_approval_notification_insert()
            """
        )

    sender_calls: list[dict[str, object]] = []
    dispatch_settings = replace(
        main.get_settings(),
        outbox_dispatch_mode="inline",
        push_dispatch_enabled=True,
        push_dispatch_poll_seconds=30,
    )
    monkeypatch.setattr(main, "get_settings", lambda: dispatch_settings)
    monkeypatch.setattr(
        postgres_notification_dispatcher,
        "get_settings",
        lambda: dispatch_settings,
    )
    dispatch_started = Event()
    release_dispatch = Event()
    dispatch_finished = Event()
    dispatched_repositories: list[object] = []
    dispatch_one = postgres_notification_dispatcher.dispatch_one_postgres_push

    def fake_sender(**kwargs: object) -> None:
        sender_calls.append(kwargs)
        return None

    def gated_dispatch(repository) -> str | None:
        dispatched_repositories.append(repository)
        dispatch_started.set()
        if not release_dispatch.wait(timeout=10):
            raise TimeoutError("test did not release the PostgreSQL dispatcher")
        result = dispatch_one(repository, sender=fake_sender)
        dispatch_finished.set()
        return result

    monkeypatch.setattr(
        postgres_notification_dispatcher,
        "dispatch_one_postgres_push",
        gated_dispatch,
    )

    headers = {
        "X-Aixion-Agent-Id": agent.id,
        "X-Aixion-Agent-Token": agent_token,
    }
    with TestClient(main.app, raise_server_exceptions=False) as client:
        assert dispatch_started.wait(timeout=5), "lifespan did not start PostgreSQL push dispatch"
        runtime = client.app.state.postgres_runtime
        repository = runtime.repositories.notification_outbox
        response = client.post(
            "/agents/approvals",
            headers=headers,
            json=_approval_payload(project.id),
        )
        assert response.status_code == 500

        with connect() as connection:
            assert connection.execute(
                "SELECT count(*) FROM approval_requests WHERE workspace_id=%s",
                (workspace.id,),
            ).fetchone()[0] == 0
            assert connection.execute(
                "SELECT count(*) FROM notifications WHERE workspace_id=%s",
                (workspace.id,),
            ).fetchone()[0] == 0
            assert connection.execute(
                "SELECT count(*) FROM audit_events WHERE workspace_id=%s "
                "AND event_type='approval.created_by_agent'",
                (workspace.id,),
            ).fetchone()[0] == 0

        release_dispatch.set()
        assert dispatch_finished.wait(timeout=10), "lifespan dispatcher did not finish its poll"
        assert dispatched_repositories == [repository]
        assert sender_calls == []
    release_dispatch.set()


def test_scheduled_push_entrypoint_dispatches_postgres_outbox_once(
    outbox_repository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scheduled entrypoint uses the real runtime and durable outbox claim."""
    _repository, connect = outbox_repository
    dsn = os.environ["AIXION_TEST_POSTGRES_DSN"]
    monkeypatch.setenv("AIXION_PROFILE", "production")
    monkeypatch.setenv("AIXION_POSTGRES_AUTH_ENABLED", "true")
    monkeypatch.setenv("AIXION_POSTGRES_RUNTIME_PROBE_ENABLED", "true")
    monkeypatch.setenv("AIXION_POSTGRES_AUTH_RATE_LIMITS_ENABLED", "true")
    monkeypatch.setenv("AIXION_EMAIL_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("AIXION_EMAIL_SMTP_HOST", "mail.example.invalid")
    monkeypatch.setenv("AIXION_EMAIL_FROM", "notifications@example.invalid")
    monkeypatch.setenv("AIXION_EMAIL_SMTP_USERNAME", "test-user")
    monkeypatch.setenv("AIXION_EMAIL_SMTP_PASSWORD", "test-password")
    monkeypatch.setenv("AIXION_EMAIL_OUTBOX_KEY", "0" * 64)
    monkeypatch.setenv(
        "AIXION_POSTGRES_DSN",
        make_conninfo(dsn, sslmode="verify-full"),
    )
    monkeypatch.setenv("AIXION_FCM_PROJECT_ID", "aixion-test-project")
    monkeypatch.setenv("AIXION_OUTBOX_DISPATCH_MODE", "scheduled")
    monkeypatch.setenv("AIXION_PUSH_DISPATCH_ENABLED", "true")
    monkeypatch.delenv("AIXION_DB_PATH", raising=False)

    push_user_id = f"scheduled-push-user-{os.urandom(8).hex()}"
    workspace, _approval, notification = _seed_notification(connect, user_id=push_user_id)
    token = f"scheduled-push-token-{push_user_id}"
    with connect() as connection:
        _insert_device(connection, workspace, push_user_id, token)

    settings = get_settings()
    monkeypatch.setattr(postgres_notification_dispatcher, "get_settings", lambda: settings)
    sender_calls: list[dict[str, object]] = []

    def fake_sender(**kwargs: object) -> None:
        sender_calls.append(kwargs)

    runtime_instances: list[PostgresRuntime] = []
    closed_runtimes: list[PostgresRuntime] = []
    original_close = PostgresRuntime.close

    def create_runtime() -> PostgresRuntime:
        runtime = PostgresRuntime.from_environment(connector=lambda *_args, **_kwargs: connect())
        runtime_instances.append(runtime)
        return runtime

    def tracked_close(runtime: PostgresRuntime) -> None:
        original_close(runtime)
        closed_runtimes.append(runtime)

    monkeypatch.setattr(PostgresRuntime, "close", tracked_close)

    def dispatch(repository) -> str | None:
        return dispatch_one_postgres_push(repository, sender=fake_sender)

    assert (
        run_postgres_push_dispatch_once.run_once(
            runtime_factory=create_runtime,
            dispatcher=dispatch,
        )
        is True
    )

    assert len(runtime_instances) == 1
    first_runtime = runtime_instances[0]
    assert first_runtime.profile == "production"
    assert first_runtime.probe().ready
    assert closed_runtimes == [first_runtime]
    assert sender_calls == [
        {
            "token": token,
            "title": notification.title,
            "body": notification.body,
            "data": {
                "notification_id": notification.id,
                "entity_type": "approval_request",
                "entity_id": notification.approval_request_id or notification.entity_id,
            },
        }
    ]
    with connect() as connection:
        assert connection.execute(
            "SELECT status,attempts FROM notifications WHERE notification_id=%s",
            (notification.id,),
        ).fetchone() == ("SENT", 1)

    assert (
        run_postgres_push_dispatch_once.run_once(
            runtime_factory=create_runtime,
            dispatcher=dispatch,
        )
        is False
    )
    assert len(runtime_instances) == 2
    assert closed_runtimes == runtime_instances
    assert len(sender_calls) == 1
    with connect() as connection:
        assert connection.execute(
            "SELECT status,attempts FROM notifications WHERE notification_id=%s",
            (notification.id,),
        ).fetchone() == ("SENT", 1)
