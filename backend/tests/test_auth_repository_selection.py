from __future__ import annotations

from types import SimpleNamespace
from typing import Callable

import pytest
from fastapi import HTTPException

from app import auth
from app.models import User, UserRole
from app.store import store


class _SQLiteAccessForbidden(dict):
    def _fail(self, *_args, **_kwargs):
        pytest.fail("SQLite-backed auth state was accessed without its authoritative repository")

    __iter__ = _fail
    values = _fail
    get = _fail
    __getitem__ = _fail
    __setitem__ = _fail
    __len__ = _fail
    __bool__ = _fail


def _user() -> User:
    return User(
        id="user_test",
        email="owner@example.com",
        display_name="Owner",
        role=UserRole.OWNER,
        password_hash="hash",
        password_salt="salt",
        email_verified=True,
    )


@pytest.mark.parametrize("mode", ["production", "postgres_auth"])
@pytest.mark.parametrize(
    "operation",
    [
        "create_user",
        "create_registration_response",
        "resend_verification",
        "verify_email",
        "authenticate_user",
        "create_session",
    ],
)
def test_auth_services_fail_closed_before_sqlite_access_without_postgres_repository(
    monkeypatch: pytest.MonkeyPatch, mode: str, operation: str
) -> None:
    monkeypatch.setattr(
        auth,
        "get_settings",
        lambda: SimpleNamespace(
            is_production=mode == "production",
            postgres_auth_enabled=mode == "postgres_auth",
            email_dispatch_enabled=False,
            profile="production" if mode == "production" else "test",
        ),
    )
    monkeypatch.setattr(store, "users", _SQLiteAccessForbidden())
    monkeypatch.setattr(store, "sessions", _SQLiteAccessForbidden())
    monkeypatch.setattr(
        store,
        "persist",
        lambda *_args, **_kwargs: pytest.fail("SQLite persistence ran before repository guard"),
    )
    monkeypatch.setattr(
        store,
        "_connect",
        lambda *_args, **_kwargs: pytest.fail("SQLite connection opened before repository guard"),
    )

    calls: dict[str, Callable[[], object]] = {
        "create_user": lambda: auth.create_user(
            "owner@example.com", "password", _persist=False
        ),
        "create_registration_response": lambda: auth.create_registration_response(_user()),
        "resend_verification": lambda: auth.resend_verification("owner@example.com"),
        "verify_email": lambda: auth.verify_email("owner@example.com", "123456"),
        "authenticate_user": lambda: auth.authenticate_user("owner@example.com", "password"),
        "create_session": lambda: auth.create_session(_user()),
    }

    with pytest.raises(HTTPException) as error:
        calls[operation]()

    assert error.value.status_code == 503
    assert error.value.headers == {"Retry-After": "30"}


def test_auth_services_keep_local_sqlite_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        auth,
        "get_settings",
        lambda: SimpleNamespace(
            is_production=False,
            postgres_auth_enabled=False,
            email_dispatch_enabled=False,
            profile="test",
        ),
    )
    monkeypatch.setattr(auth, "_hash_secret", lambda secret, _salt: f"hash:{secret}")
    monkeypatch.setattr(store, "users", {})
    monkeypatch.setattr(store, "workspaces", {})
    monkeypatch.setattr(store, "sessions", {})
    persisted: list[bool] = []
    monkeypatch.setattr(store, "persist", lambda: persisted.append(True))

    user = auth.create_user(
        "owner@example.com",
        "correct-password",
        create_workspace=True,
        email_verified=True,
        _persist=False,
    )

    assert auth.authenticate_user("OWNER@example.com", "correct-password").id == user.id
    session = auth.create_session(user)

    assert session.user.id == user.id
    assert store.users[user.id] == user
    assert user.workspace_id in store.workspaces
    assert len(store.sessions) == 1
    assert persisted == [True]
