from __future__ import annotations

from fastapi.testclient import TestClient

from app.auth import create_session, create_user
from app.main import app
from app.store import store

PASSWORD = "valid-password-123"


def setup_function() -> None:
    store.reset()


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_account_deletion_info_is_public() -> None:
    response = TestClient(app).get("/auth/account-deletion-info")

    assert response.status_code == 200
    payload = response.json()
    assert payload["app"] == "Aixion Control Tower"
    assert payload["authenticated_request_endpoint"] == "/auth/account-deletion-request"
    assert "retained" in payload["retention_note"]


def test_authenticated_user_can_request_account_deletion(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")
    user = create_user("owner@example.com", PASSWORD, "Owner")
    auth = create_session(user)
    create_session(user)

    response = TestClient(app).post(
        "/auth/account-deletion-request",
        headers=auth_header(auth.access_token),
        json={"reason": "No longer using the app"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["user_id"] == user.id
    assert payload["email"] == "owner@example.com"
    assert payload["status"] == "RECEIVED"
    assert payload["active_sessions_revoked"] == 2
    assert store.users[user.id].disabled is True
    assert all(session.revoked for session in store.sessions.values() if session.user_id == user.id)


def test_account_deletion_request_writes_audit_event(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")
    user = create_user("owner@example.com", PASSWORD, "Owner")
    auth = create_session(user)

    response = TestClient(app).post(
        "/auth/account-deletion-request",
        headers=auth_header(auth.access_token),
        json={"reason": "Please remove my account"},
    )

    assert response.status_code == 200
    event = store.audit_events[-1]
    assert event.event_type == "auth.account_deletion_requested"
    assert event.actor == "owner@example.com"
    assert event.entity_id == user.id
    assert event.details["target_email"] == "owner@example.com"
    assert event.details["status"] == "RECEIVED"
    assert event.details["reason_provided"] is True


def test_account_deletion_request_requires_auth(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")

    response = TestClient(app).post("/auth/account-deletion-request", json={"reason": "remove"})

    assert response.status_code == 401


def test_disabled_user_cannot_request_deletion_again(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")
    user = create_user("owner@example.com", PASSWORD, "Owner")
    auth = create_session(user)

    first = TestClient(app).post(
        "/auth/account-deletion-request",
        headers=auth_header(auth.access_token),
        json={"reason": "remove"},
    )
    assert first.status_code == 200

    second = TestClient(app).post(
        "/auth/account-deletion-request",
        headers=auth_header(auth.access_token),
        json={"reason": "remove again"},
    )

    assert second.status_code == 401
