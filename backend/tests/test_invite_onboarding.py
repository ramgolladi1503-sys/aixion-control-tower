from __future__ import annotations

from fastapi.testclient import TestClient

from app.auth import create_session, create_user
from app.invite_routes import (
    InviteCreateRequest,
    create_invite,
    hash_invite_token,
    revoke_invite,
    validate_invite_token,
)
from app.main import app
from app.models import AuthUser, InviteStatus, UserRole
from app.store import store


PASSWORD = "valid-password-123"


def setup_function() -> None:
    store.reset()


def test_owner_can_create_invite_with_raw_token_returned_once() -> None:
    owner = create_user("owner@example.com", PASSWORD, "Owner")

    response = create_invite(
        InviteCreateRequest(email="NewUser@Example.com"),
        auth_user(owner.id, owner.email, owner.role),
    )

    assert response.email == "newuser@example.com"
    assert response.role == UserRole.REVIEWER
    assert response.status == InviteStatus.PENDING
    assert response.token

    stored = store.invites[response.id]
    assert stored.token_hash == hash_invite_token(response.token)
    assert response.token not in stored.model_dump_json()


def test_non_owner_cannot_create_invite(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")
    create_user("owner@example.com", PASSWORD, "Owner")
    reviewer = create_user("reviewer@example.com", PASSWORD, "Reviewer")
    token = create_session(reviewer).access_token

    response = TestClient(app).post(
        "/auth/invites",
        headers={"Authorization": f"Bearer {token}"},
        json={"email": "new@example.com"},
    )

    assert response.status_code == 403
    assert store.invites == {}


def test_invite_defaults_to_reviewer_unless_role_specified() -> None:
    owner = create_user("owner@example.com", PASSWORD, "Owner")

    default_invite = create_invite(
        InviteCreateRequest(email="default@example.com"),
        auth_user(owner.id, owner.email, owner.role),
    )
    maintainer_invite = create_invite(
        InviteCreateRequest(email="maintainer@example.com", role=UserRole.MAINTAINER),
        auth_user(owner.id, owner.email, owner.role),
    )

    assert default_invite.role == UserRole.REVIEWER
    assert maintainer_invite.role == UserRole.MAINTAINER


def test_invalid_invite_role_is_rejected_by_model(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")
    owner = create_user("owner@example.com", PASSWORD, "Owner")
    token = create_session(owner).access_token

    response = TestClient(app).post(
        "/auth/invites",
        headers={"Authorization": f"Bearer {token}"},
        json={"email": "new@example.com", "role": "ADMIN"},
    )

    assert response.status_code == 422


def test_owner_can_list_invites(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")
    owner = create_user("owner@example.com", PASSWORD, "Owner")
    token = create_session(owner).access_token
    create_invite(InviteCreateRequest(email="b@example.com"), auth_user(owner.id, owner.email, owner.role))
    create_invite(InviteCreateRequest(email="a@example.com"), auth_user(owner.id, owner.email, owner.role))

    response = TestClient(app).get("/auth/invites", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 2
    assert {invite["email"] for invite in payload} == {"a@example.com", "b@example.com"}
    assert all("token" not in invite for invite in payload)


def test_owner_can_revoke_pending_invite() -> None:
    owner = create_user("owner@example.com", PASSWORD, "Owner")
    invite = create_invite(
        InviteCreateRequest(email="new@example.com"),
        auth_user(owner.id, owner.email, owner.role),
    )

    revoked = revoke_invite(invite.id, auth_user(owner.id, owner.email, owner.role))

    assert revoked.status == InviteStatus.REVOKED
    assert store.invites[invite.id].status == InviteStatus.REVOKED


def test_cannot_revoke_accepted_invite() -> None:
    owner = create_user("owner@example.com", PASSWORD, "Owner")
    invite = create_invite(
        InviteCreateRequest(email="new@example.com"),
        auth_user(owner.id, owner.email, owner.role),
    )
    store.invites[invite.id].status = InviteStatus.ACCEPTED

    response = TestClient(app).post(
        f"/auth/invites/{invite.id}/revoke",
        headers={"Authorization": f"Bearer {create_session(owner).access_token}"},
    )

    assert response.status_code == 409
    assert store.invites[invite.id].status == InviteStatus.ACCEPTED


def test_invite_creation_and_revocation_audit_events_exist() -> None:
    owner = create_user("owner@example.com", PASSWORD, "Owner")
    actor = auth_user(owner.id, owner.email, owner.role)

    invite = create_invite(InviteCreateRequest(email="new@example.com"), actor)
    revoke_invite(invite.id, actor)

    assert [event.event_type for event in store.audit_events] == [
        "auth.invite_created",
        "auth.invite_revoked",
    ]
    assert store.audit_events[0].details["email"] == "new@example.com"
    assert store.audit_events[1].details["status"] == InviteStatus.REVOKED


def test_invite_validation_helper_accepts_only_pending_valid_token() -> None:
    owner = create_user("owner@example.com", PASSWORD, "Owner")
    invite = create_invite(
        InviteCreateRequest(email="new@example.com"),
        auth_user(owner.id, owner.email, owner.role),
    )

    validated = validate_invite_token(invite.token)

    assert validated.id == invite.id
    assert validated.status == InviteStatus.PENDING


def test_first_registration_bootstraps_owner_without_invite(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")

    response = TestClient(app).post(
        "/auth/register",
        json={"email": "owner@example.com", "password": PASSWORD, "display_name": "Owner"},
    )

    assert response.status_code == 200
    assert response.json()["user"]["role"] == UserRole.OWNER


def test_later_registration_requires_invite_token(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")
    create_user("owner@example.com", PASSWORD, "Owner")

    response = TestClient(app).post(
        "/auth/register",
        json={"email": "new@example.com", "password": PASSWORD, "display_name": "New User"},
    )

    assert response.status_code == 403
    assert "new@example.com" not in {user.email for user in store.users.values()}


def test_registration_accepts_matching_pending_invite_and_assigns_invite_role(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")
    owner = create_user("owner@example.com", PASSWORD, "Owner")
    invite = create_invite(
        InviteCreateRequest(email="maintainer@example.com", role=UserRole.MAINTAINER),
        auth_user(owner.id, owner.email, owner.role),
    )

    response = TestClient(app).post(
        "/auth/register",
        json={
            "email": "maintainer@example.com",
            "password": PASSWORD,
            "display_name": "Maintainer",
            "invite_token": invite.token,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["access_token"]
    assert payload["user"]["email"] == "maintainer@example.com"
    assert payload["user"]["role"] == UserRole.MAINTAINER
    assert store.invites[invite.id].status == InviteStatus.ACCEPTED
    assert store.invites[invite.id].accepted_by_user_id == payload["user"]["id"]
    assert store.audit_events[-1].event_type == "auth.invite_accepted"


def test_registration_rejects_invite_email_mismatch(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")
    owner = create_user("owner@example.com", PASSWORD, "Owner")
    invite = create_invite(
        InviteCreateRequest(email="allowed@example.com"),
        auth_user(owner.id, owner.email, owner.role),
    )

    response = TestClient(app).post(
        "/auth/register",
        json={
            "email": "wrong@example.com",
            "password": PASSWORD,
            "display_name": "Wrong",
            "invite_token": invite.token,
        },
    )

    assert response.status_code == 403
    assert store.invites[invite.id].status == InviteStatus.PENDING
    assert "wrong@example.com" not in {user.email for user in store.users.values()}


def test_registration_rejects_reused_invite_token(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")
    owner = create_user("owner@example.com", PASSWORD, "Owner")
    invite = create_invite(
        InviteCreateRequest(email="new@example.com"),
        auth_user(owner.id, owner.email, owner.role),
    )

    first = TestClient(app).post(
        "/auth/register",
        json={"email": "new@example.com", "password": PASSWORD, "display_name": "New", "invite_token": invite.token},
    )
    second = TestClient(app).post(
        "/auth/register",
        json={"email": "new@example.com", "password": PASSWORD, "display_name": "New", "invite_token": invite.token},
    )

    assert first.status_code == 200
    assert second.status_code in {409, 422}
    assert store.invites[invite.id].status == InviteStatus.ACCEPTED


def test_registration_rejects_revoked_invite_token(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")
    owner = create_user("owner@example.com", PASSWORD, "Owner")
    actor = auth_user(owner.id, owner.email, owner.role)
    invite = create_invite(InviteCreateRequest(email="new@example.com"), actor)
    revoke_invite(invite.id, actor)

    response = TestClient(app).post(
        "/auth/register",
        json={"email": "new@example.com", "password": PASSWORD, "display_name": "New", "invite_token": invite.token},
    )

    assert response.status_code == 409
    assert "new@example.com" not in {user.email for user in store.users.values()}


def auth_user(user_id: str, email: str, role: UserRole) -> AuthUser:
    return AuthUser(
        id=user_id,
        email=email,
        display_name=email,
        role=role,
    )
