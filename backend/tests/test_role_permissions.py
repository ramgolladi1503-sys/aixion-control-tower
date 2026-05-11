from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.auth import create_user, require_maintainer, require_owner, require_reviewer
from app.models import AuthUser, UserRole
from app.store import store


PASSWORD = "valid-password-123"


def setup_function() -> None:
    store.reset()


def test_first_self_registered_user_becomes_owner() -> None:
    user = create_user("owner@example.com", PASSWORD, "Owner")

    assert user.role == UserRole.OWNER


def test_later_self_registered_users_default_to_reviewer() -> None:
    owner = create_user("owner@example.com", PASSWORD, "Owner")
    reviewer = create_user("reviewer@example.com", PASSWORD, "Reviewer")

    assert owner.role == UserRole.OWNER
    assert reviewer.role == UserRole.REVIEWER


def test_explicit_role_is_respected_for_internal_user_creation() -> None:
    maintainer = create_user(
        "maintainer@example.com",
        PASSWORD,
        "Maintainer",
        role=UserRole.MAINTAINER,
    )

    assert maintainer.role == UserRole.MAINTAINER


def test_owner_can_pass_owner_maintainer_and_reviewer_guards() -> None:
    user = auth_user(UserRole.OWNER)

    assert require_owner(user) == user
    assert require_maintainer(user) == user
    assert require_reviewer(user) == user


def test_maintainer_cannot_pass_owner_guard_but_can_pass_lower_guards() -> None:
    user = auth_user(UserRole.MAINTAINER)

    with pytest.raises(HTTPException) as exc_info:
        require_owner(user)

    assert exc_info.value.status_code == 403
    assert require_maintainer(user) == user
    assert require_reviewer(user) == user


def test_reviewer_can_only_pass_reviewer_guard() -> None:
    user = auth_user(UserRole.REVIEWER)

    with pytest.raises(HTTPException) as owner_exc:
        require_owner(user)
    with pytest.raises(HTTPException) as maintainer_exc:
        require_maintainer(user)

    assert owner_exc.value.status_code == 403
    assert maintainer_exc.value.status_code == 403
    assert require_reviewer(user) == user


def auth_user(role: UserRole) -> AuthUser:
    return AuthUser(
        id=f"user_{role.value.lower()}",
        email=f"{role.value.lower()}@example.com",
        display_name=role.value.title(),
        role=role,
    )
