from __future__ import annotations

from fastapi.testclient import TestClient

from app.auth import create_session, create_user
from app.main import app
from app.models import UserRole
from app.store import store


PASSWORD = "valid-password-123"


def setup_function() -> None:
    store.reset()


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_owner_can_list_sessions(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")
    owner = create_user("owner@example.com", PASSWORD, "Owner")
    reviewer = create_user("reviewer@example.com", PASSWORD, "Reviewer")
    owner_auth = create_session(owner)
    create_session(reviewer)

    response = TestClient(app).get("/auth/sessions", headers=auth_header(owner_auth.access_token))

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 2
    assert {session["user_email"] for session in payload} == {"owner@example.com", "reviewer@example.com"}
    assert all("token_hash" not in session for session in payload)


def test_non_owner_cannot_list_sessions(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")
    create_user("owner@example.com", PASSWORD, "Owner")
    reviewer = create_user("reviewer@example.com", PASSWORD, "Reviewer")
    reviewer_auth = create_session(reviewer)

    response = TestClient(app).get("/auth/sessions", headers=auth_header(reviewer_auth.access_token))

    assert response.status_code == 403


def test_owner_can_revoke_target_user_sessions(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")
    owner = create_user("owner@example.com", PASSWORD, "Owner")
    reviewer = create_user("reviewer@example.com", PASSWORD, "Reviewer")
    owner_auth = create_session(owner)
    reviewer_auth = create_session(reviewer)
    create_session(reviewer)

    response = TestClient(app).post(
        f"/auth/users/{reviewer.id}/sessions/revoke",
        headers=auth_header(owner_auth.access_token),
    )

    assert response.status_code == 200
    assert response.json()["revoked_sessions_count"] == 2

    rejected = TestClient(app).get("/auth/me", headers=auth_header(reviewer_auth.access_token))
    assert rejected.status_code == 401


def test_owner_cannot_revoke_own_sessions(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")
    owner = create_user("owner@example.com", PASSWORD, "Owner")
    owner_auth = create_session(owner)

    response = TestClient(app).post(
        f"/auth/users/{owner.id}/sessions/revoke",
        headers=auth_header(owner_auth.access_token),
    )

    assert response.status_code == 409
    assert any(not session.revoked for session in store.sessions.values() if session.user_id == owner.id)


def test_non_owner_cannot_revoke_sessions(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")
    create_user("owner@example.com", PASSWORD, "Owner")
    reviewer = create_user("reviewer@example.com", PASSWORD, "Reviewer")
    maintainer = create_user("maintainer@example.com", PASSWORD, "Maintainer", role=UserRole.MAINTAINER)
    reviewer_auth = create_session(reviewer)
    create_session(maintainer)

    response = TestClient(app).post(
        f"/auth/users/{maintainer.id}/sessions/revoke",
        headers=auth_header(reviewer_auth.access_token),
    )

    assert response.status_code == 403
    assert any(session.user_id == maintainer.id and not session.revoked for session in store.sessions.values())


def test_revoke_missing_user_returns_404(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")
    owner = create_user("owner@example.com", PASSWORD, "Owner")
    owner_auth = create_session(owner)

    response = TestClient(app).post(
        "/auth/users/user_missing/sessions/revoke",
        headers=auth_header(owner_auth.access_token),
    )

    assert response.status_code == 404


def test_session_revocation_audit_event_exists(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")
    owner = create_user("owner@example.com", PASSWORD, "Owner")
    reviewer = create_user("reviewer@example.com", PASSWORD, "Reviewer")
    owner_auth = create_session(owner)
    create_session(reviewer)

    response = TestClient(app).post(
        f"/auth/users/{reviewer.id}/sessions/revoke",
        headers=auth_header(owner_auth.access_token),
    )

    assert response.status_code == 200
    assert store.audit_events[-1].event_type == "auth.sessions_revoked"
    assert store.audit_events[-1].actor == "owner@example.com"
    assert store.audit_events[-1].entity_id == reviewer.id
    assert store.audit_events[-1].details["revoked_sessions_count"] == 1
