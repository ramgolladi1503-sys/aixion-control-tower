from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import main, postgres_notification_dispatcher
from app.settings import get_settings
from app.store import store

pytest_plugins = ("test_postgres_lifespan_approval_inbox",)


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
    dispatch_settings = replace(get_settings(), push_dispatch_enabled=True)
    monkeypatch.setattr(
        postgres_notification_dispatcher,
        "get_settings",
        lambda: dispatch_settings,
    )

    def fake_sender(**kwargs: object) -> None:
        sender_calls.append(kwargs)
        return None

    headers = {
        "X-Aixion-Agent-Id": agent.id,
        "X-Aixion-Agent-Token": agent_token,
    }
    with TestClient(main.app) as client:
        runtime = client.app.state.postgres_runtime
        repositories = runtime.repositories
        assert client.app.state.postgres_repositories is repositories
        assert client.app.state.notification_outbox_repository is repositories.notification_outbox

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
        assert notification.approval_request_id is None
        assert notification.workspace_id == workspace.id
        assert notification.push_status == "PENDING"

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

        delivered_id = postgres_notification_dispatcher.dispatch_one_postgres_push(
            client.app.state.notification_outbox_repository,
            sender=fake_sender,
        )
        assert delivered_id == notification.id
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
    monkeypatch.setattr(
        postgres_notification_dispatcher,
        "get_settings",
        lambda: replace(get_settings(), push_dispatch_enabled=True),
    )

    def fake_sender(**kwargs: object) -> None:
        sender_calls.append(kwargs)
        return None

    headers = {
        "X-Aixion-Agent-Id": agent.id,
        "X-Aixion-Agent-Token": agent_token,
    }
    with TestClient(main.app, raise_server_exceptions=False) as client:
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

        assert postgres_notification_dispatcher.dispatch_one_postgres_push(
            repository,
            sender=fake_sender,
        ) is None
        assert sender_calls == []

