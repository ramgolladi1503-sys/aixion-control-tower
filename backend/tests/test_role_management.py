from __future__ import annotations

import pytest
from fastapi import HTTPException
from types import SimpleNamespace

from app import auth, role_routes, session_routes
from app.auth import create_user
from app.models import AuthUser, UserRole
from app.role_routes import RoleUpdateRequest, active_owner_count, list_users, update_user_role
from app.store import store


PASSWORD = "valid-password-123"


def setup_function() -> None:
    store.reset()


def test_owner_can_list_users_sorted_by_email() -> None:
    owner = create_user("owner@example.com", PASSWORD, "Owner")
    create_user("z-reviewer@example.com", PASSWORD, "Z Reviewer", workspace_id=owner.workspace_id)
    create_user("a-reviewer@example.com", PASSWORD, "A Reviewer", workspace_id=owner.workspace_id)

    users = list_users(auth_user(owner.id, owner.email, owner.role))

    assert [user.email for user in users] == [
        "a-reviewer@example.com",
        "owner@example.com",
        "z-reviewer@example.com",
    ]
    assert all(isinstance(user, AuthUser) for user in users)


@pytest.mark.parametrize("authoritative_mode", ["production", "postgres_auth"])
@pytest.mark.parametrize(
    "invoke",
    [
        lambda actor: role_routes.list_users(actor),
        lambda actor: role_routes.update_user_role(
            "target-user",
            RoleUpdateRequest(role=UserRole.REVIEWER),
            actor,
        ),
        lambda actor: session_routes.list_sessions(actor),
        lambda actor: session_routes.revoke_user_sessions("target-user", actor),
    ],
    ids=["list-users", "update-role", "list-sessions", "revoke-sessions"],
)
def test_account_security_routes_fail_closed_without_request_context(
    monkeypatch: pytest.MonkeyPatch,
    authoritative_mode: str,
    invoke,
) -> None:
    monkeypatch.setattr(
        auth,
        "get_settings",
        lambda: SimpleNamespace(
            is_production=authoritative_mode == "production",
            postgres_auth_enabled=authoritative_mode == "postgres_auth",
        ),
    )

    class ForbiddenLocalMap(dict):
        def get(self, *_args, **_kwargs):
            pytest.fail("SQLite account state must not be accessed")

        def values(self):
            pytest.fail("SQLite account state must not be accessed")

    monkeypatch.setattr(store, "users", ForbiddenLocalMap())
    monkeypatch.setattr(store, "sessions", ForbiddenLocalMap())
    actor = AuthUser(
        id="owner",
        email="owner@example.test",
        display_name="Owner",
        role=UserRole.OWNER,
    )

    with pytest.raises(HTTPException) as error:
        invoke(actor)

    assert error.value.status_code == 503
    assert error.value.headers == {"Retry-After": "1"}
    assert "app-bound request" in error.value.detail


def test_owner_can_promote_reviewer_to_maintainer() -> None:
    owner = create_user("owner@example.com", PASSWORD, "Owner")
    reviewer = create_user("reviewer@example.com", PASSWORD, "Reviewer", workspace_id=owner.workspace_id)

    updated = update_user_role(
        reviewer.id,
        RoleUpdateRequest(role=UserRole.MAINTAINER),
        auth_user(owner.id, owner.email, owner.role),
    )

    assert updated.role == UserRole.MAINTAINER
    assert store.users[reviewer.id].role == UserRole.MAINTAINER
    assert store.audit_events[-1].event_type == "auth.role_updated"
    assert store.audit_events[-1].workspace_id == owner.workspace_id
    assert store.audit_events[-1].details["previous_role"] == UserRole.REVIEWER
    assert store.audit_events[-1].details["new_role"] == UserRole.MAINTAINER


def test_role_update_is_idempotent_when_role_is_unchanged() -> None:
    owner = create_user("owner@example.com", PASSWORD, "Owner")
    reviewer = create_user("reviewer@example.com", PASSWORD, "Reviewer", workspace_id=owner.workspace_id)

    updated = update_user_role(
        reviewer.id,
        RoleUpdateRequest(role=UserRole.REVIEWER),
        auth_user(owner.id, owner.email, owner.role),
    )

    assert updated.role == UserRole.REVIEWER
    assert store.audit_events == []


def test_owner_cannot_demote_last_active_owner() -> None:
    owner = create_user("owner@example.com", PASSWORD, "Owner")

    with pytest.raises(HTTPException) as exc_info:
        update_user_role(
            owner.id,
            RoleUpdateRequest(role=UserRole.REVIEWER),
            auth_user(owner.id, owner.email, owner.role),
        )

    assert exc_info.value.status_code == 409
    assert store.users[owner.id].role == UserRole.OWNER
    assert active_owner_count() == 1


def test_owner_can_demote_owner_when_another_active_owner_exists() -> None:
    owner = create_user("owner@example.com", PASSWORD, "Owner")
    second_owner = create_user(
        "second-owner@example.com",
        PASSWORD,
        "Second Owner",
        role=UserRole.OWNER,
        workspace_id=owner.workspace_id,
    )

    updated = update_user_role(
        second_owner.id,
        RoleUpdateRequest(role=UserRole.MAINTAINER),
        auth_user(owner.id, owner.email, owner.role),
    )

    assert updated.role == UserRole.MAINTAINER
    assert store.users[second_owner.id].role == UserRole.MAINTAINER
    assert active_owner_count() == 1


def test_role_update_for_missing_user_returns_404() -> None:
    owner = create_user("owner@example.com", PASSWORD, "Owner")

    with pytest.raises(HTTPException) as exc_info:
        update_user_role(
            "user_missing",
            RoleUpdateRequest(role=UserRole.REVIEWER),
            auth_user(owner.id, owner.email, owner.role),
        )

    assert exc_info.value.status_code == 404


def auth_user(user_id: str, email: str, role: UserRole) -> AuthUser:
    return AuthUser(
        id=user_id,
        email=email,
        display_name=email,
        role=role,
    )
