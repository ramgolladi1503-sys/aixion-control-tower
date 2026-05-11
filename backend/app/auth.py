from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta

from fastapi import Header, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from .models import AuthUser, SessionToken, User, now_utc
from .settings import get_settings
from .store import store

PBKDF2_ITERATIONS = 210_000
SESSION_DAYS = 30


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12)
    display_name: str = ""


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: AuthUser


def auth_enabled() -> bool:
    return get_settings().auth_enabled


def _hash_secret(secret: str, salt: str) -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        secret.encode("utf-8"),
        salt.encode("utf-8"),
        PBKDF2_ITERATIONS,
    )
    return digest.hex()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _safe_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _to_auth_user(user: User) -> AuthUser:
    return AuthUser(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
    )


def create_user(email: str, password: str, display_name: str = "") -> User:
    normalized_email = email.lower().strip()
    if any(user.email.lower() == normalized_email for user in store.users.values()):
        raise HTTPException(status_code=409, detail="User already exists")

    salt = secrets.token_hex(16)
    user = User(
        email=normalized_email,
        display_name=display_name.strip() or normalized_email,
        password_salt=salt,
        password_hash=_hash_secret(password, salt),
    )
    store.users[user.id] = user
    store.persist()
    return user


def authenticate_user(email: str, password: str) -> User:
    normalized_email = email.lower().strip()
    user = next((item for item in store.users.values() if item.email.lower() == normalized_email), None)
    if not user or user.disabled:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    candidate = _hash_secret(password, user.password_salt)
    if not _safe_equal(candidate, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    return user


def create_session(user: User) -> AuthResponse:
    raw_token = secrets.token_urlsafe(48)
    session = SessionToken(
        user_id=user.id,
        token_hash=_hash_token(raw_token),
        expires_at=now_utc() + timedelta(days=SESSION_DAYS),
    )
    store.sessions[session.id] = session
    store.persist()
    return AuthResponse(access_token=raw_token, user=_to_auth_user(user))


def require_user(authorization: str | None = Header(default=None)) -> AuthUser:
    """Require a bearer session token.

    This replaces the API-key MVP guard. Local dev can still disable auth with
    AIXION_AUTH_ENABLED=false or AIXION_PROFILE=demo, but production defaults to enabled.
    """
    if not auth_enabled():
        return AuthUser(id="dev_user", email="dev@local", display_name="Local Dev", role="OWNER")

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")

    raw_token = authorization.split(" ", 1)[1].strip()
    token_hash = _hash_token(raw_token)
    now = now_utc()
    session = next(
        (
            item
            for item in store.sessions.values()
            if _safe_equal(item.token_hash, token_hash) and not item.revoked and item.expires_at > now
        ),
        None,
    )
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session")

    user = store.users.get(session.user_id)
    if not user or user.disabled:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not active")
    return _to_auth_user(user)


# Backward-compatible dependency name while endpoints are migrated.
require_api_key = require_user
