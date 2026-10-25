from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import Response
from fastapi.testclient import TestClient
import google.auth
import google.auth.transport.requests

from app import main, notifications
from app.models import (
    ApprovalRequest,
    ApprovalStatus,
    AuthUser,
    DeviceRegistration,
    FileChange,
    Project,
    RiskAssessment,
    RiskLevel,
    UserRole,
    Workspace,
    now_utc,
)
from app.store import store
from app.trust_metrics import prometheus_trust_metrics


class _FakeClient:
    def __init__(self, response: httpx.Response, captured: dict[str, Any]) -> None:
        self.response = response
        self.captured = captured

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.captured["url"] = url
        self.captured.update(kwargs)
        return self.response


def _configure_fcm(monkeypatch: Any, response: httpx.Response, captured: dict[str, Any]) -> None:
    monkeypatch.setenv("AIXION_FCM_PROJECT_ID", "control-tower-prod")
    monkeypatch.setattr(notifications, "_fcm_access_token", lambda: "short-lived-test-token")
    monkeypatch.setattr(
        notifications.httpx,
        "Client",
        lambda **_: _FakeClient(response, captured),
    )


def _dispatch_local_production_scope_policy_for_test(
    notification: notifications.Notification,
) -> notifications.Notification:
    """Exercise the SQLite target filter directly; production uses the PG outbox."""
    return notifications._dispatch_push_from_local_state(notification, production=True)


@pytest.mark.parametrize(
    "payload",
    [
        {"token": ""},
        {"token": " "},
        {"token": "x" * 4097},
        {"token": "bad\ntoken"},
        {"token": "valid-token", "platform": "ios"},
        {"token": "valid-token", "app_version": "v" * 65},
    ],
)
def test_device_registration_request_rejects_invalid_or_unbounded_values(
    payload: dict[str, str],
) -> None:
    with pytest.raises(ValueError):
        notifications.DeviceRegistrationRequest(**payload)


def test_device_registration_request_accepts_current_android_client_contract() -> None:
    payload = notifications.DeviceRegistrationRequest(
        token="opaque:firebase-token_123",
        platform="android",
        app_version="0.1.0",
    )

    assert payload.token == "opaque:firebase-token_123"
    assert payload.platform == "android"


def test_push_uses_oauth_v1_and_generic_lock_screen_content(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "https://fcm.googleapis.com/test"),
    )
    _configure_fcm(monkeypatch, response, captured)

    result = notifications._send_fcm_to_token(
        token="device-token",
        title="Confidential project and task name",
        body="Sensitive approval summary",
        data={
            "notification_id": "notification_opaque",
            "entity_type": "agent_task",
            "entity_id": "task_opaque",
        },
    )

    assert result is None
    assert captured["url"] == (
        "https://fcm.googleapis.com/v1/projects/control-tower-prod/messages:send"
    )
    assert captured["headers"]["Authorization"] == "Bearer short-lived-test-token"
    payload = captured["json"]["message"]
    assert payload["token"] == "device-token"
    assert payload["android"]["priority"] == "HIGH"
    assert payload["notification"] == {
        "title": "Aixion Control Tower",
        "body": "A request is waiting for your review. Open the app to view details.",
    }
    assert "Confidential project and task name" not in str(payload)
    assert "Sensitive approval summary" not in str(payload)
    assert payload["data"]["notification_id"] == "notification_opaque"
    assert payload["data"]["entity_id"] == "task_opaque"


def test_fcm_error_body_is_not_returned_for_persistence(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    response = httpx.Response(
        401,
        text="provider diagnostic includes sensitive registration detail",
        request=httpx.Request("POST", "https://fcm.googleapis.com/test"),
    )
    _configure_fcm(monkeypatch, response, captured)

    result = notifications._send_fcm_to_token(
        token="device-token",
        title="title",
        body="body",
        data={"notification_id": "notification_opaque"},
    )

    assert result == "FCM_ERROR_401"
    assert "sensitive registration detail" not in result


def test_unregistered_fcm_token_is_classified_without_retaining_provider_body(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    response = httpx.Response(
        404,
        json={"error": {"status": "UNREGISTERED", "message": "private token detail"}},
        request=httpx.Request("POST", "https://fcm.googleapis.com/test"),
    )
    _configure_fcm(monkeypatch, response, captured)

    result = notifications._send_fcm_to_token(
        token="device-token",
        title="title",
        body="body",
        data={"notification_id": "notification_opaque"},
    )

    assert result == "FCM_TOKEN_UNREGISTERED"
    assert "private token detail" not in result


def test_fcm_transport_exception_text_is_not_returned_for_persistence(
    monkeypatch: Any,
) -> None:
    class FailingClient(_FakeClient):
        def post(self, url: str, **kwargs: Any) -> httpx.Response:
            raise httpx.ConnectError("sensitive request details and device token")

    monkeypatch.setenv("AIXION_FCM_PROJECT_ID", "control-tower-prod")
    monkeypatch.setattr(notifications, "_fcm_access_token", lambda: "short-lived-test-token")
    monkeypatch.setattr(
        notifications.httpx,
        "Client",
        lambda **_: FailingClient(
            httpx.Response(200, request=httpx.Request("POST", "https://fcm.googleapis.com/test")),
            {},
        ),
    )

    result = notifications._send_fcm_to_token(
        token="device-token",
        title="title",
        body="body",
        data={"notification_id": "notification_opaque"},
    )

    assert result == "FCM_NETWORK_ERROR"
    assert "sensitive" not in result
    assert "device token" not in result


def test_fcm_credentials_failure_is_sanitized(monkeypatch: Any) -> None:
    monkeypatch.setenv("AIXION_FCM_PROJECT_ID", "control-tower-prod")
    monkeypatch.setattr(
        notifications,
        "_fcm_access_token",
        lambda: (_ for _ in ()).throw(RuntimeError("secret local credential path")),
    )

    result = notifications._send_fcm_to_token(
        token="device-token",
        title="title",
        body="body",
        data={"notification_id": "notification_opaque"},
    )

    assert result == "FCM_CREDENTIALS_UNAVAILABLE"


def test_fcm_oauth_refresh_timeout_is_bounded_sanitized_and_retryable(
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("AIXION_FCM_PROJECT_ID", "control-tower-prod")
    captured: dict[str, Any] = {}

    class FakeRequest:
        def __call__(self, url: str, **kwargs: Any) -> None:
            captured["url"] = url
            captured.update(kwargs)
            raise TimeoutError("sensitive credential endpoint details")

    class FakeCredentials:
        token = None

        def refresh(self, request: Any) -> None:
            request("https://oauth.example.test/token", method="POST")

    monkeypatch.setattr(google.auth, "default", lambda **_: (FakeCredentials(), None))
    monkeypatch.setattr(
        google.auth.transport.requests,
        "Request",
        lambda: FakeRequest(),
    )

    result = notifications._send_fcm_to_token(
        token="device-token",
        title="title",
        body="body",
        data={"notification_id": "notification_opaque"},
    )

    assert captured["timeout"] == notifications.FCM_TIMEOUT_SECONDS
    assert result == "FCM_CREDENTIALS_UNAVAILABLE"
    assert "sensitive" not in result
    assert notifications._is_retryable_push_failure(result)


def test_push_is_skipped_when_project_id_is_missing(monkeypatch: Any) -> None:
    monkeypatch.delenv("AIXION_FCM_PROJECT_ID", raising=False)

    result = notifications._send_fcm_to_token(
        token="device-token",
        title="title",
        body="body",
        data={"notification_id": "notification_opaque"},
    )

    assert result == "SKIPPED_FCM_PROJECT_ID_NOT_CONFIGURED"


def test_production_push_requires_workspace_scope_and_never_broadcasts_legacy_record(
    monkeypatch: Any,
) -> None:
    store.reset()
    monkeypatch.setenv("AIXION_PROFILE", "production")
    workspace_a = Workspace(name="Account A", owner_user_id="owner-a")
    workspace_b = Workspace(name="Account B", owner_user_id="owner-b")
    store.workspaces.update({workspace_a.id: workspace_a, workspace_b.id: workspace_b})
    store.device_registrations.update(
        {
            "device-a": DeviceRegistration(
                id="device-a", user_id="owner-a", workspace_id=workspace_a.id, token="token-a"
            ),
            "device-b": DeviceRegistration(
                id="device-b", user_id="owner-b", workspace_id=workspace_b.id, token="token-b"
            ),
        }
    )
    notification = notifications.Notification(
        title="Legacy approval",
        body="Review",
        entity_type="approval_request",
        entity_id="approval-legacy",
    )
    delivered: list[str] = []
    monkeypatch.setattr(
        notifications,
        "_send_fcm_to_token",
        lambda token, **_: delivered.append(token),
    )

    result = _dispatch_local_production_scope_policy_for_test(notification)

    assert result.push_status == "SKIPPED"
    assert result.push_error == "RECIPIENT_SCOPE_MISSING"
    assert delivered == []


def test_production_push_routes_only_to_matching_workspace_and_optional_user(
    monkeypatch: Any,
) -> None:
    store.reset()
    monkeypatch.setenv("AIXION_PROFILE", "production")
    workspace_a = Workspace(name="Account A", owner_user_id="owner-a")
    workspace_b = Workspace(name="Account B", owner_user_id="owner-b")
    store.workspaces.update({workspace_a.id: workspace_a, workspace_b.id: workspace_b})
    store.users.update(
        {
            "owner-a": AuthUser(
                id="owner-a", email="a@example.test", display_name="A", role=UserRole.OWNER,
                workspace_id=workspace_a.id,
            ),
            "reviewer-a": AuthUser(
                id="reviewer-a", email="reviewer@example.test", display_name="Reviewer",
                role=UserRole.REVIEWER, workspace_id=workspace_a.id,
            ),
            "owner-b": AuthUser(
                id="owner-b", email="b@example.test", display_name="B", role=UserRole.OWNER,
                workspace_id=workspace_b.id,
            ),
        }
    )
    store.device_registrations.update(
        {
            "device-a1": DeviceRegistration(
                id="device-a1", user_id="owner-a", workspace_id=workspace_a.id, token="token-a1"
            ),
            "device-a2": DeviceRegistration(
                id="device-a2", user_id="reviewer-a", workspace_id=workspace_a.id, token="token-a2"
            ),
            "device-b": DeviceRegistration(
                id="device-b", user_id="owner-b", workspace_id=workspace_b.id, token="token-b"
            ),
            "device-mismatch": DeviceRegistration(
                id="device-mismatch", user_id="owner-a", workspace_id=workspace_b.id,
                token="token-mismatch",
            ),
        }
    )
    delivered: list[str] = []
    monkeypatch.setattr(
        notifications,
        "_send_fcm_to_token",
        lambda token, **_: delivered.append(token),
    )
    notification = notifications.Notification(
        title="Approval",
        body="Review",
        entity_type="approval_request",
        entity_id="approval-a",
        workspace_id=workspace_a.id,
    )

    _dispatch_local_production_scope_policy_for_test(notification)

    assert set(delivered) == {"token-a1", "token-a2"}
    assert "token-b" not in delivered
    assert "token-mismatch" not in delivered

    delivered.clear()
    notification.user_id = "reviewer-a"
    _dispatch_local_production_scope_policy_for_test(notification)
    assert delivered == ["token-a2"]


def test_agent_task_approval_push_targets_the_linked_approval(monkeypatch: Any) -> None:
    store.reset()
    monkeypatch.setenv("AIXION_PROFILE", "production")
    workspace = Workspace(name="Approval workspace", owner_user_id="owner")
    owner = AuthUser(
        id="owner", email="owner@example.test", display_name="Owner",
        role=UserRole.OWNER, workspace_id=workspace.id,
    )
    store.workspaces[workspace.id] = workspace
    store.users[owner.id] = owner
    store.device_registrations["owner-device"] = DeviceRegistration(
        id="owner-device", user_id=owner.id, workspace_id=workspace.id, token="owner-token"
    )
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        notifications,
        "_send_fcm_to_token",
        lambda **kwargs: captured.append(kwargs),
    )
    notification = notifications.Notification(
        title="Approval needed",
        body="A connected agent needs your decision.",
        entity_type="agent_task",
        entity_id="task_123",
        workspace_id=workspace.id,
        user_id=owner.id,
        approval_request_id="approval_123",
    )

    result = _dispatch_local_production_scope_policy_for_test(notification)

    assert result.push_status == "SENT"
    assert captured[0]["data"] == {
        "notification_id": notification.id,
        "entity_type": "approval_request",
        "entity_id": "approval_123",
    }


def test_partial_delivery_does_not_retry_entire_workspace_fanout(monkeypatch: Any) -> None:
    store.reset()
    monkeypatch.setenv("AIXION_PROFILE", "production")
    workspace = Workspace(name="Account", owner_user_id="owner")
    store.workspaces[workspace.id] = workspace
    store.users["owner"] = AuthUser(
        id="owner", email="owner@example.test", display_name="Owner",
        role=UserRole.OWNER, workspace_id=workspace.id,
    )
    store.device_registrations.update(
        {
            "accepted": DeviceRegistration(
                id="accepted", user_id="owner", workspace_id=workspace.id, token="accepted"
            ),
            "transient": DeviceRegistration(
                id="transient", user_id="owner", workspace_id=workspace.id, token="transient"
            ),
        }
    )
    notification = notifications.Notification(
        title="Approval", body="Review", entity_type="approval_request",
        entity_id="approval", workspace_id=workspace.id,
    )
    monkeypatch.setattr(
        notifications,
        "_send_fcm_to_token",
        lambda token, **_: None if token == "accepted" else "FCM_NETWORK_ERROR",
    )

    result = _dispatch_local_production_scope_policy_for_test(notification)

    assert result.push_status == "SENT"
    assert result.push_error == "PARTIAL_DELIVERY"
    assert not notifications._is_retryable_push_failure(result.push_error)


def test_unregistered_token_is_removed_and_audited(monkeypatch: Any) -> None:
    store.reset()
    monkeypatch.setenv("AIXION_PROFILE", "production")
    workspace = Workspace(name="Account", owner_user_id="owner")
    store.workspaces[workspace.id] = workspace
    store.users["owner"] = AuthUser(
        id="owner", email="owner@example.test", display_name="Owner",
        role=UserRole.OWNER, workspace_id=workspace.id,
    )
    device = DeviceRegistration(
        id="stale-device", user_id="owner", workspace_id=workspace.id, token="stale-token"
    )
    store.device_registrations[device.id] = device
    notification = notifications.Notification(
        title="Approval", body="Review", entity_type="approval_request",
        entity_id="approval", workspace_id=workspace.id,
    )
    monkeypatch.setattr(
        notifications, "_send_fcm_to_token", lambda **_: "FCM_TOKEN_UNREGISTERED"
    )

    result = _dispatch_local_production_scope_policy_for_test(notification)

    assert result.push_status == "FAILED"
    assert result.push_error == "FCM_TOKEN_UNREGISTERED"
    assert device.id not in store.device_registrations
    assert any(
        event.event_type == "notification.device_token_invalidated"
        and event.entity_id == device.id
        for event in store.audit_events
    )


def test_notification_push_only_runs_after_approval_and_notification_are_durable(
    monkeypatch: Any,
) -> None:
    store.reset()
    workspace = Workspace(name="Account", owner_user_id="owner")
    project = Project(name="Project", workspace_id=workspace.id)
    approval = ApprovalRequest(
        project_id=project.id,
        workspace_id=workspace.id,
        title="Review change",
        summary="A safe change",
        target_branch="feature/safe-change",
        files=[FileChange(path="README.md", diff="+safe")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.REQUESTED,
    )
    store.workspaces[workspace.id] = workspace
    store.projects[project.id] = project
    store.approval_requests[approval.id] = approval
    observed: dict[str, Any] = {}
    monkeypatch.setenv("AIXION_PUSH_DISPATCH_ENABLED", "true")

    def inspect_durable_records(notification):
        observed["approval"] = store._load_entities("approval_request", ApprovalRequest).get(approval.id)
        observed["notification"] = store._load_entities("notification", type(notification)).get(notification.id)
        notification.push_status = "SENT"
        return notification

    monkeypatch.setattr(notifications, "dispatch_push", inspect_durable_records)

    notification = notifications.create_notification(
        title="Approval needed",
        body="Open the app to review.",
        entity_type="approval_request",
        entity_id=approval.id,
    )
    assert notification.push_status == "PENDING"
    notifications.dispatch_one_due_push()

    assert observed["approval"].status == ApprovalStatus.REQUESTED
    assert observed["notification"].id == notification.id


def test_notification_delivery_and_status_failures_do_not_undo_committed_request(
    monkeypatch: Any,
) -> None:
    store.reset()
    workspace = Workspace(name="Account", owner_user_id="owner")
    project = Project(name="Project", workspace_id=workspace.id)
    approval = ApprovalRequest(
        project_id=project.id,
        workspace_id=workspace.id,
        title="Review change",
        summary="A safe change",
        target_branch="feature/safe-change",
        files=[FileChange(path="README.md", diff="+safe")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.REQUESTED,
    )
    store.workspaces[workspace.id] = workspace
    store.projects[project.id] = project
    store.approval_requests[approval.id] = approval
    monkeypatch.setenv("AIXION_PUSH_DISPATCH_ENABLED", "true")
    monkeypatch.setattr(
        notifications,
        "dispatch_push",
        lambda _notification: (_ for _ in ()).throw(RuntimeError("secret provider detail")),
    )
    original_persist = store.persist
    persist_count = 0

    def fail_delivery_status_persist() -> None:
        nonlocal persist_count
        persist_count += 1
        if persist_count == 3:
            raise OSError("database detail must not escape")
        original_persist()

    monkeypatch.setattr(store, "persist", fail_delivery_status_persist)

    notification = notifications.create_notification(
        title="Approval needed",
        body="Open the app to review.",
        entity_type="approval_request",
        entity_id=approval.id,
    )
    notifications.dispatch_one_due_push()

    assert notification.push_status == "RETRY_WAIT"
    assert notification.push_error == "FCM_DISPATCH_FAILED"
    durable_approval = store._load_entities("approval_request", ApprovalRequest)[approval.id]
    assert durable_approval.status == ApprovalStatus.REQUESTED


def test_initial_persistence_failure_prevents_push_dispatch(monkeypatch: Any) -> None:
    monkeypatch.setenv("AIXION_PUSH_DISPATCH_ENABLED", "true")
    store.reset()
    workspace = Workspace(name="Account", owner_user_id="owner")
    project = Project(name="Project", workspace_id=workspace.id)
    approval = ApprovalRequest(
        project_id=project.id,
        workspace_id=workspace.id,
        title="Review change",
        summary="A safe change",
        target_branch="feature/safe-change",
        files=[FileChange(path="README.md", diff="+safe")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.REQUESTED,
    )
    store.workspaces[workspace.id] = workspace
    store.projects[project.id] = project
    store.approval_requests[approval.id] = approval
    dispatched: list[str] = []
    monkeypatch.setattr(
        notifications,
        "dispatch_push",
        lambda notification: dispatched.append(notification.id),
    )
    monkeypatch.setattr(
        store,
        "persist",
        lambda: (_ for _ in ()).throw(OSError("storage unavailable")),
    )

    try:
        notifications.create_notification(
            title="Approval needed",
            body="Open the app to review.",
            entity_type="approval_request",
            entity_id=approval.id,
        )
    except OSError as exc:
        assert str(exc) == "storage unavailable"
    else:
        raise AssertionError("notification dispatch continued without a durable commit")

    assert dispatched == []
    assert not store.notifications
    assert notifications.dispatch_one_due_push() is None
    assert dispatched == []


def test_push_retries_use_bounded_exponential_backoff(monkeypatch: Any) -> None:
    monkeypatch.setenv("AIXION_PUSH_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("AIXION_PUSH_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("AIXION_PUSH_RETRY_BASE_SECONDS", "5")
    monkeypatch.setenv("AIXION_PUSH_RETRY_MAX_SECONDS", "20")
    store.reset()
    workspace = Workspace(name="Account", owner_user_id="owner")
    project = Project(name="Project", workspace_id=workspace.id)
    approval = ApprovalRequest(
        project_id=project.id,
        workspace_id=workspace.id,
        title="Review change",
        summary="A safe change",
        target_branch="feature/safe-change",
        files=[FileChange(path="README.md", diff="+safe")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.REQUESTED,
    )
    store.workspaces[workspace.id] = workspace
    store.projects[project.id] = project
    store.approval_requests[approval.id] = approval
    notification = notifications.create_notification(
        title="Approval needed",
        body="Open the app to review.",
        entity_type="approval_request",
        entity_id=approval.id,
    )
    attempts: list[str] = []

    def fail_delivery(item):
        attempts.append(item.id)
        item.push_status = "FAILED"
        item.push_error = "FCM_NETWORK_ERROR"
        return item

    monkeypatch.setattr(notifications, "dispatch_push", fail_delivery)
    start = now_utc()

    assert notifications.dispatch_one_due_push(now=start) == notification.id
    assert notification.push_attempts == 1
    assert notification.push_status == "RETRY_WAIT"
    assert notification.push_next_attempt_at == start + timedelta(seconds=5)
    assert notifications.dispatch_one_due_push(now=start + timedelta(seconds=4)) is None

    assert notifications.dispatch_one_due_push(now=start + timedelta(seconds=5)) == notification.id
    assert notification.push_attempts == 2
    assert notification.push_status == "FAILED"
    assert notification.push_error == "FCM_RETRIES_EXHAUSTED"
    assert notification.push_next_attempt_at is None
    assert attempts == [notification.id, notification.id]


def test_expired_push_lease_recovers_interrupted_send(monkeypatch: Any) -> None:
    monkeypatch.setenv("AIXION_PUSH_DISPATCH_ENABLED", "true")
    store.reset()
    workspace = Workspace(name="Account", owner_user_id="owner")
    project = Project(name="Project", workspace_id=workspace.id)
    approval = ApprovalRequest(
        project_id=project.id,
        workspace_id=workspace.id,
        title="Review change",
        summary="A safe change",
        target_branch="feature/safe-change",
        files=[FileChange(path="README.md", diff="+safe")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.REQUESTED,
    )
    store.workspaces[workspace.id] = workspace
    store.projects[project.id] = project
    store.approval_requests[approval.id] = approval
    notification = notifications.create_notification(
        title="Approval needed",
        body="Open the app to review.",
        entity_type="approval_request",
        entity_id=approval.id,
    )
    now = now_utc()
    notification.push_status = "SENDING"
    notification.push_attempts = 1
    notification.push_lease_expires_at = now - timedelta(seconds=1)
    store.persist()

    def succeed(item):
        item.push_status = "SENT"
        return item

    monkeypatch.setattr(notifications, "dispatch_push", succeed)

    assert notifications.dispatch_one_due_push(now=now) == notification.id
    assert notification.push_attempts == 2
    assert notification.push_status == "SENT"
    assert notification.push_lease_expires_at is None


def test_permanent_fcm_failure_is_not_retried(monkeypatch: Any) -> None:
    monkeypatch.setenv("AIXION_PUSH_DISPATCH_ENABLED", "true")
    store.reset()
    workspace = Workspace(name="Account", owner_user_id="owner")
    project = Project(name="Project", workspace_id=workspace.id)
    approval = ApprovalRequest(
        project_id=project.id,
        workspace_id=workspace.id,
        title="Review change",
        summary="A safe change",
        target_branch="feature/safe-change",
        files=[FileChange(path="README.md", diff="+safe")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.REQUESTED,
    )
    store.workspaces[workspace.id] = workspace
    store.projects[project.id] = project
    store.approval_requests[approval.id] = approval
    notification = notifications.create_notification(
        title="Approval needed",
        body="Open the app to review.",
        entity_type="approval_request",
        entity_id=approval.id,
    )
    attempts: list[str] = []

    def fail_permanently(item):
        attempts.append(item.id)
        item.push_status = "FAILED"
        item.push_error = "FCM_ERROR_401"
        return item

    monkeypatch.setattr(notifications, "dispatch_push", fail_permanently)

    assert notifications.dispatch_one_due_push() == notification.id
    assert notification.push_status == "FAILED"
    assert notification.push_error == "FCM_ERROR_401"
    assert notification.push_next_attempt_at is None
    assert notifications.dispatch_one_due_push() is None
    assert attempts == [notification.id]


def test_provider_outcome_is_not_visible_until_dispatcher_reacquires_store_lock(
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("AIXION_PUSH_DISPATCH_ENABLED", "true")
    store.reset()
    workspace = Workspace(name="Account", owner_user_id="owner")
    project = Project(name="Project", workspace_id=workspace.id)
    approval = ApprovalRequest(
        project_id=project.id,
        workspace_id=workspace.id,
        title="Review change",
        summary="A safe change",
        target_branch="feature/safe-change",
        files=[FileChange(path="README.md", diff="+safe")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.REQUESTED,
    )
    store.workspaces[workspace.id] = workspace
    store.projects[project.id] = project
    store.approval_requests[approval.id] = approval
    notification = notifications.create_notification(
        title="Approval needed",
        body="Open the app to review.",
        entity_type="approval_request",
        entity_id=approval.id,
    )
    observed: list[str] = []

    def provider_result(item):
        item.push_status = "SENT"
        item.push_error = None
        observed.append(store.notifications[notification.id].push_status)
        return item

    monkeypatch.setattr(notifications, "dispatch_push", provider_result)

    assert notifications.dispatch_one_due_push() == notification.id
    assert observed == ["SENDING"]
    assert notification.push_status == "SENT"


@pytest.mark.parametrize("operation", ["dispatch_one_due_push", "dispatch_push"])
@pytest.mark.parametrize("mode", ["postgres", "production"])
def test_sqlite_notification_dispatch_helpers_refuse_postgres_authority(
    operation: str,
    mode: str,
    monkeypatch: Any,
) -> None:
    store.reset()
    notification = notifications.Notification(
        title="Approval needed",
        body="Review the request",
        entity_type="approval_request",
        entity_id="approval_pg_guard",
        workspace_id="workspace_pg_guard",
    )
    store.notifications[notification.id] = notification
    original_notification = notification.model_copy(deep=True)
    provider_calls: list[str] = []
    monkeypatch.setattr(
        notifications,
        "get_settings",
        lambda: SimpleNamespace(
            is_production=mode == "production",
            postgres_auth_enabled=mode == "postgres",
            push_dispatch_enabled=True,
        ),
    )
    monkeypatch.setattr(
        notifications,
        "_send_fcm_to_token",
        lambda **_: provider_calls.append("sent") or None,
    )
    monkeypatch.setattr(
        store,
        "atomic",
        lambda: (_ for _ in ()).throw(AssertionError("SQLite state must not be read")),
    )

    with pytest.raises(RuntimeError, match="SQLite notification operations"):
        if operation == "dispatch_one_due_push":
            notifications.dispatch_one_due_push()
        else:
            notifications.dispatch_push(notification)

    assert store.notifications == {notification.id: original_notification}
    assert provider_calls == []


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("get", "/notifications", {}),
        ("post", "/notifications/notification_prod_guard/read", {}),
        (
            "post",
            "/notifications/devices",
            {"json": {"token": "prod-guard-token"}},
        ),
        ("delete", "/notifications/devices/device_prod_guard", {}),
    ],
)
def test_production_notification_routes_fail_closed_without_postgres_runtime(
    method: str,
    path: str,
    kwargs: dict[str, object],
    monkeypatch: Any,
) -> None:
    """Production must not route notification or device calls into SQLite."""

    class ForbiddenLocalMap(dict):
        def values(self):
            raise AssertionError("SQLite notification state must not be read")

        def get(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("SQLite notification state must not be read")

    user = AuthUser(
        id="production_notification_owner",
        email="production-notification@example.test",
        display_name="Production owner",
        role=UserRole.OWNER,
        workspace_id="production_notification_workspace",
    )
    monkeypatch.setattr(
        notifications,
        "get_settings",
        lambda: SimpleNamespace(is_production=True, postgres_auth_enabled=False),
    )
    monkeypatch.setattr(main.app.state, "postgres_runtime", None, raising=False)
    monkeypatch.setattr(main.app.state, "postgres_repositories", None, raising=False)
    monkeypatch.setattr(main.app.state, "notification_outbox_repository", None, raising=False)
    monkeypatch.setattr(store, "notifications", ForbiddenLocalMap(), raising=False)
    monkeypatch.setattr(store, "device_registrations", ForbiddenLocalMap(), raising=False)
    monkeypatch.setattr(
        store,
        "atomic",
        lambda: (_ for _ in ()).throw(AssertionError("SQLite mutation must not run")),
    )
    monkeypatch.setitem(
        main.app.dependency_overrides,
        notifications.AuthDependency.dependency,
        lambda: user,
    )

    response = getattr(TestClient(main.app), method)(path, **kwargs)

    assert response.status_code == 503
    assert response.json()["detail"] == "PostgreSQL notification runtime is unavailable"


def test_api_lifespan_starts_and_stops_push_dispatcher(monkeypatch: Any) -> None:
    from app import main

    started = asyncio.Event()
    stopped = asyncio.Event()

    async def waiting_dispatcher() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(main, "notification_dispatch_loop", waiting_dispatcher)
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(
            profile="test",
            outbox_dispatch_mode="inline",
            push_dispatch_enabled=True,
            email_dispatch_enabled=False,
            postgres_runtime_probe_enabled=False,
            postgres_auth_rate_limits_enabled=False,
            postgres_auth_enabled=False,
        ),
    )

    async def exercise_lifespan() -> None:
        async with main.lifespan(main.app):
            await asyncio.wait_for(started.wait(), timeout=1)

    asyncio.run(exercise_lifespan())

    assert stopped.is_set()


def test_push_metrics_are_workspace_scoped_and_do_not_include_sensitive_content() -> None:
    store.reset()
    workspace_a = Workspace(name="Account A", owner_user_id="owner_a")
    workspace_b = Workspace(name="Account B", owner_user_id="owner_b")
    project_a = Project(name="Project A", workspace_id=workspace_a.id)
    project_b = Project(name="Project B", workspace_id=workspace_b.id)
    approval_a = ApprovalRequest(
        project_id=project_a.id,
        workspace_id=workspace_a.id,
        title="Sensitive approval title A",
        summary="Sensitive details A",
        target_branch="feature/a",
        files=[FileChange(path="a.txt", diff="+a")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.REQUESTED,
    )
    approval_b = ApprovalRequest(
        project_id=project_b.id,
        workspace_id=workspace_b.id,
        title="Sensitive approval title B",
        summary="Sensitive details B",
        target_branch="feature/b",
        files=[FileChange(path="b.txt", diff="+b")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.REQUESTED,
    )
    store.workspaces.update({workspace_a.id: workspace_a, workspace_b.id: workspace_b})
    store.projects.update({project_a.id: project_a, project_b.id: project_b})
    store.approval_requests.update({approval_a.id: approval_a, approval_b.id: approval_b})
    notifications.create_notification(
        title=approval_a.title,
        body=approval_a.summary,
        entity_type="approval_request",
        entity_id=approval_a.id,
    )
    notifications.create_notification(
        title=approval_b.title,
        body=approval_b.summary,
        entity_type="approval_request",
        entity_id=approval_b.id,
    )

    workspace_metrics = prometheus_trust_metrics(workspace_a.id)
    global_metrics = prometheus_trust_metrics()

    assert 'aixion_push_notifications_total{status="PENDING"} 1' in workspace_metrics
    assert "aixion_push_retry_queue_depth 1" in workspace_metrics
    assert "aixion_push_retry_queue_depth 2" in global_metrics
    assert "Sensitive approval" not in workspace_metrics
    assert "Sensitive details" not in global_metrics


def test_device_unregistration_is_idempotent_and_cannot_remove_another_users_device() -> None:
    store.reset()
    owner_a = AuthUser(
        id="owner-a",
        email="owner-a@example.test",
        display_name="Owner A",
        role=UserRole.OWNER,
        workspace_id="workspace-a",
    )
    device_a = DeviceRegistration(
        id="device-a",
        user_id=owner_a.id,
        workspace_id=owner_a.workspace_id,
        token="token-a",
    )
    device_b = DeviceRegistration(
        id="device-b",
        user_id="owner-b",
        workspace_id="workspace-b",
        token="token-b",
    )
    store.device_registrations.update({device_a.id: device_a, device_b.id: device_b})
    store.persist()

    removed = notifications.unregister_device(device_a.id, owner_a)
    repeated = notifications.unregister_device(device_a.id, owner_a)
    foreign = notifications.unregister_device(device_b.id, owner_a)

    assert isinstance(removed, Response) and removed.status_code == 204
    assert isinstance(repeated, Response) and repeated.status_code == 204
    assert isinstance(foreign, Response) and foreign.status_code == 204
    assert device_a.id not in store.device_registrations
    assert store.device_registrations[device_b.id] == device_b
    assert any(
        event.event_type == "notification.device_unregistered"
        and event.entity_id == device_a.id
        and event.actor == owner_a.id
        for event in store.audit_events
    )


def test_device_unregistration_returns_no_content_without_disclosing_foreign_registration() -> None:
    store.reset()
    device = DeviceRegistration(
        id="private-device-id",
        user_id="owner-b",
        workspace_id="workspace-b",
        token="private-token",
    )
    store.device_registrations[device.id] = device
    store.persist()
    owner_a = AuthUser(
        id="owner-a",
        email="owner-a@example.test",
        display_name="Owner A",
        role=UserRole.OWNER,
        workspace_id="workspace-a",
    )

    response = notifications.unregister_device(device.id, owner_a)

    assert isinstance(response, Response) and response.status_code == 204
    assert store.device_registrations[device.id] == device


def test_device_unregistration_does_not_remove_same_user_record_from_another_workspace() -> None:
    store.reset()
    workspace_a = Workspace(name="Account A", owner_user_id="owner-a")
    store.workspaces[workspace_a.id] = workspace_a
    owner_a = AuthUser(
        id="owner-a",
        email="owner-a@example.test",
        display_name="Owner A",
        role=UserRole.OWNER,
        workspace_id=workspace_a.id,
    )
    device = DeviceRegistration(
        id="mismatched-device",
        user_id=owner_a.id,
        workspace_id="workspace-b",
        token="mismatched-token",
    )
    store.device_registrations[device.id] = device
    store.persist()

    response = notifications.unregister_device(device.id, owner_a)

    assert isinstance(response, Response) and response.status_code == 204
    assert store.device_registrations[device.id] == device


def test_registering_existing_installation_moves_it_to_the_current_account() -> None:
    store.reset()
    workspace_a = Workspace(name="Account A", owner_user_id="owner-a")
    workspace_b = Workspace(name="Account B", owner_user_id="owner-b")
    store.workspaces.update({workspace_a.id: workspace_a, workspace_b.id: workspace_b})
    old_device = DeviceRegistration(
        id="shared-installation",
        user_id="owner-a",
        workspace_id=workspace_a.id,
        token="same-installation-token",
    )
    store.device_registrations[old_device.id] = old_device
    store.persist()
    owner_b = AuthUser(
        id="owner-b",
        email="owner-b@example.test",
        display_name="Owner B",
        role=UserRole.OWNER,
        workspace_id=workspace_b.id,
    )

    registration = notifications.register_device(
        notifications.DeviceRegistrationRequest(token="same-installation-token"),
        owner_b,
    )

    assert registration.id == old_device.id
    assert registration.user_id == owner_b.id
    assert registration.workspace_id == workspace_b.id
    assert len(store.device_registrations) == 1
    assert len(store.audit_events) == 1
    assert store.audit_events[0].event_type == "notification.device_reassigned"
    assert store.audit_events[0].actor == owner_b.id
    assert owner_b.id not in store.audit_events[0].details.values()


def test_device_registration_does_not_rewrite_unrelated_store_rows(monkeypatch: Any) -> None:
    store.reset()
    owner = AuthUser(
        id="owner",
        email="owner@example.test",
        display_name="Owner",
        role=UserRole.OWNER,
    )
    unrelated = DeviceRegistration(
        id="unrelated", user_id="other", token="unrelated-token"
    )
    store.persist_records(inserts=[("unrelated_entity", unrelated.id, unrelated)])

    def reject_full_rewrite() -> None:
        raise AssertionError("device registration must not rewrite the whole SQLite store")

    monkeypatch.setattr(store, "persist", reject_full_rewrite)
    registration = notifications.register_device(
        notifications.DeviceRegistrationRequest(token="new-device-token"), owner
    )

    assert store.device_registrations[registration.id] == registration
    assert store._load_entities("device_registration", DeviceRegistration) == {
        registration.id: registration
    }
    with store._connect() as connection:
        unrelated_row = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type='unrelated_entity' AND entity_id=?",
            (unrelated.id,),
        ).fetchone()
    assert unrelated_row == (unrelated.model_dump_json(),)


def test_failed_device_reassignment_restores_cache_to_committed_state(monkeypatch: Any) -> None:
    store.reset()
    workspace_a = Workspace(name="Account A", owner_user_id="owner-a")
    workspace_b = Workspace(name="Account B", owner_user_id="owner-b")
    store.workspaces.update({workspace_a.id: workspace_a, workspace_b.id: workspace_b})
    original = DeviceRegistration(
        id="shared-installation", user_id="owner-a", workspace_id=workspace_a.id,
        token="same-installation-token",
    )
    original_before = original.model_copy(deep=True)
    store.device_registrations[original.id] = original
    store.persist()
    owner_b = AuthUser(
        id="owner-b", email="owner-b@example.test", display_name="Owner B",
        role=UserRole.OWNER, workspace_id=workspace_b.id,
    )

    def fail_register(_: DeviceRegistration) -> Any:
        raise OSError("database write failed")

    monkeypatch.setattr(store.device_registration_repository, "register", fail_register)
    with pytest.raises(OSError, match="database write failed"):
        notifications.register_device(
            notifications.DeviceRegistrationRequest(token=original.token), owner_b
        )

    assert store.device_registrations[original.id] == original_before
    assert store._load_entities("device_registration", DeviceRegistration) == {
        original.id: original_before
    }
    assert store.audit_events == []


def test_failed_device_registration_removes_uncommitted_cache_entries(monkeypatch: Any) -> None:
    store.reset()
    owner = AuthUser(
        id="owner", email="owner@example.test", display_name="Owner",
        role=UserRole.OWNER, workspace_id="workspace",
    )

    def fail_register(_: DeviceRegistration) -> Any:
        raise OSError("database write failed")

    monkeypatch.setattr(store.device_registration_repository, "register", fail_register)
    with pytest.raises(OSError, match="database write failed"):
        notifications.register_device(
            notifications.DeviceRegistrationRequest(token="new-token"), owner
        )

    assert store.device_registrations == {}
    assert store.audit_events == []
    assert store._load_entities("device_registration", DeviceRegistration) == {}


def test_failed_device_unregistration_restores_committed_cache_state(monkeypatch: Any) -> None:
    store.reset()
    owner = AuthUser(
        id="owner", email="owner@example.test", display_name="Owner",
        role=UserRole.OWNER, workspace_id="workspace",
    )
    device = DeviceRegistration(
        id="device", user_id=owner.id, workspace_id=owner.workspace_id, token="token"
    )
    store.device_registrations[device.id] = device
    store.persist()

    def fail_unregister(**_: Any) -> Any:
        raise OSError("database write failed")

    monkeypatch.setattr(
        store.device_registration_repository,
        "unregister",
        fail_unregister,
    )
    with pytest.raises(OSError, match="database write failed"):
        notifications.unregister_device(device.id, owner)

    assert store.device_registrations[device.id] == device
    assert store._load_entities("device_registration", DeviceRegistration) == {
        device.id: device
    }
    assert store.audit_events == []


def test_failed_invalid_token_cleanup_restores_cache_and_audit(monkeypatch: Any) -> None:
    store.reset()
    workspace = Workspace(name="Account", owner_user_id="owner")
    store.workspaces[workspace.id] = workspace
    store.users["owner"] = AuthUser(
        id="owner", email="owner@example.test", display_name="Owner",
        role=UserRole.OWNER, workspace_id=workspace.id,
    )
    device = DeviceRegistration(
        id="device", user_id="owner", workspace_id=workspace.id, token="stale-token"
    )
    store.device_registrations[device.id] = device
    store.persist()
    notification = notifications.Notification(
        title="Approval", body="Review", entity_type="approval_request",
        entity_id="approval", workspace_id=workspace.id,
    )
    monkeypatch.setattr(
        notifications, "_send_fcm_to_token", lambda **_: "FCM_TOKEN_UNREGISTERED"
    )

    def fail_persist() -> None:
        raise OSError("database write failed")

    monkeypatch.setattr(store, "persist", fail_persist)
    result = notifications.dispatch_push(notification)

    assert result.push_status == "FAILED"
    assert store.device_registrations[device.id] == device
    assert store._load_entities("device_registration", DeviceRegistration) == {
        device.id: device
    }
    assert not any(event.event_type == "notification.device_token_invalidated" for event in store.audit_events)


def test_failed_mark_read_restores_unread_notification_cache(monkeypatch: Any) -> None:
    store.reset()
    notification = notifications.Notification(
        id="notification", title="Approval", body="Review",
        entity_type="approval_request", entity_id="approval", user_id="owner",
    )
    store.notifications[notification.id] = notification
    store.persist()
    owner = AuthUser(
        id="owner", email="owner@example.test", display_name="Owner",
        role=UserRole.OWNER,
    )

    def fail_persist() -> None:
        raise OSError("database write failed")

    monkeypatch.setattr(store, "persist", fail_persist)
    with pytest.raises(OSError, match="database write failed"):
        notifications.mark_notification_read(notification.id, owner)

    assert store.notifications[notification.id].status == notifications.NotificationStatus.UNREAD
    assert (
        store._load_entities("notification", notifications.Notification)[notification.id].status
        == notifications.NotificationStatus.UNREAD
    )


def test_device_delete_route_requires_authentication_and_removes_registration(monkeypatch: Any) -> None:
    store.reset()
    device = DeviceRegistration(
        id="route-device",
        user_id="dev_user",
        token="route-token",
    )
    store.device_registrations[device.id] = device
    client = TestClient(main.app)

    monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")
    unauthenticated = client.delete(f"/notifications/devices/{device.id}")
    assert unauthenticated.status_code == 401
    assert store.device_registrations[device.id] == device

    monkeypatch.setenv("AIXION_AUTH_ENABLED", "false")
    removed = client.delete(f"/notifications/devices/{device.id}")

    assert removed.status_code == 204
    assert device.id not in store.device_registrations
