from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta

from fastapi import Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field

from .application_repository_access import application_repositories_for_request
from .models import AuthUser, SessionToken, User, UserRole, Workspace, now_utc
from .settings import get_settings
from .store import store
from .email_delivery import insert_verification_email, prepare_verification_email

PBKDF2_ITERATIONS = 210_000
SESSION_DAYS = 30
VERIFICATION_MINUTES = 30


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12)
    display_name: str = ""
    invite_token: str | None = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class VerifyEmailRequest(BaseModel):
    email: EmailStr
    code: str = Field(min_length=6, max_length=64)


class ResendVerificationRequest(BaseModel):
    email: EmailStr


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: AuthUser


class RegistrationResponse(BaseModel):
    user: AuthUser
    verification_required: bool = True
    message: str
    dev_verification_code: str | None = None


class VerifyEmailResponse(BaseModel):
    user: AuthUser
    verified: bool = True
    message: str


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


def _require_postgres_repository(postgres_repository, operation: str) -> None:
    """Prevent service-level direct calls from falling back to SQLite in authoritative modes."""
    settings = get_settings()
    if postgres_repository is None and (
        getattr(settings, "is_production", False)
        or getattr(settings, "postgres_auth_enabled", False)
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{operation} requires the PostgreSQL auth repository in production mode",
            headers={"Retry-After": "30"},
        )


def _default_role_for_new_user() -> UserRole:
    if not store.users:
        return UserRole.OWNER
    return UserRole.REVIEWER


def _to_auth_user(user: User) -> AuthUser:
    return AuthUser(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        workspace_id=user.workspace_id,
        email_verified=user.email_verified,
    )


def new_user_record(
    email: str,
    password: str,
    display_name: str,
    *,
    role: UserRole,
    workspace_id: str | None,
) -> User:
    normalized_email = email.lower().strip()
    salt = secrets.token_hex(16)
    return User(
        email=normalized_email,
        display_name=display_name.strip() or normalized_email,
        role=role,
        password_salt=salt,
        password_hash=_hash_secret(password, salt),
        workspace_id=workspace_id,
        email_verified=False,
    )


def prepare_verification_state(user: User) -> tuple[User, str]:
    code = f"{secrets.randbelow(1_000_000):06d}"
    salt = secrets.token_hex(16)
    now = now_utc()
    return user.model_copy(
        update={
            "email_verification_salt": salt,
            "email_verification_hash": _hash_secret(code, salt),
            "email_verification_expires_at": now + timedelta(minutes=VERIFICATION_MINUTES),
            "email_verification_sent_at": now,
            "updated_at": now,
        }
    ), code


def _request_from_context(request: Request) -> Request:
    return request


def postgres_auth_repository(request: Request | None):
    settings = get_settings()
    if request is None and (
        getattr(settings, "is_production", False)
        or getattr(settings, "postgres_auth_enabled", False)
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PostgreSQL auth repository requires an app-bound request",
            headers={"Retry-After": "1"},
        )
    if not settings.postgres_auth_enabled:
        return None
    return application_repositories_for_request(request).auth


def postgres_workspace_membership_repository(request: Request | None):
    """Return the membership adapter paired with PostgreSQL authentication."""
    if not get_settings().postgres_auth_enabled:
        return None
    repositories = application_repositories_for_request(request)
    repository = repositories.workspace_membership
    if repository is None:
        raise RuntimeError("PostgreSQL auth is enabled but workspace membership is unavailable")
    return repository


def _persist_verification_fields(user: User, prepared: User) -> None:
    for field_name in (
        "email_verification_salt",
        "email_verification_hash",
        "email_verification_expires_at",
        "email_verification_sent_at",
        "updated_at",
    ):
        setattr(user, field_name, getattr(prepared, field_name))


def _issue_email_verification(user: User, *, persist: bool = True) -> str:
    code = f"{secrets.randbelow(1_000_000):06d}"
    salt = secrets.token_hex(16)
    now = now_utc()
    user.email_verification_salt = salt
    user.email_verification_hash = _hash_secret(code, salt)
    user.email_verification_expires_at = now + timedelta(minutes=VERIFICATION_MINUTES)
    user.email_verification_sent_at = now
    user.updated_at = now
    store.users[user.id] = user
    if persist:
        store.persist()
    return code


def _dev_verification_code(code: str) -> str | None:
    return code if get_settings().profile in {"local", "demo", "test"} else None


def create_user(
    email: str,
    password: str,
    display_name: str = "",
    role: UserRole | None = None,
    *,
    email_verified: bool = True,
    workspace_id: str | None = None,
    create_workspace: bool = False,
    _persist: bool = True,
) -> User:
    _require_postgres_repository(None, "Account creation")
    normalized_email = email.lower().strip()
    salt = secrets.token_hex(16)
    password_hash = _hash_secret(password, salt)
    display_name = display_name.strip() or normalized_email

    # The in-memory bootstrap check and its persisted user/workspace creation
    # must be one process-local critical section. Without this, concurrent first
    # registrations can create multiple owners or an unassigned second user.
    with store.atomic():
        if any(user.email.lower() == normalized_email for user in store.users.values()):
            raise HTTPException(status_code=409, detail="User already exists")

        is_first_user = not store.users
        if create_workspace and (workspace_id is not None or role not in (None, UserRole.OWNER)):
            raise ValueError("A new account workspace requires a new OWNER account")
        if not create_workspace and not is_first_user and workspace_id is None:
            if len(store.workspaces) != 1:
                raise HTTPException(
                    status_code=403,
                    detail="An unambiguous workspace membership is required",
                )
            workspace_id = next(iter(store.workspaces))

        if workspace_id is not None and workspace_id not in store.workspaces:
            raise HTTPException(status_code=403, detail="Workspace is not available")

        user = User(
            email=normalized_email,
            display_name=display_name,
            role=role or (UserRole.OWNER if create_workspace else _default_role_for_new_user()),
            password_salt=salt,
            password_hash=password_hash,
            email_verified=email_verified,
            workspace_id=workspace_id,
        )
        if (create_workspace or is_first_user) and workspace_id is None:
            workspace = Workspace(name=display_name, owner_user_id=user.id)
            user.workspace_id = workspace.id
            store.workspaces[workspace.id] = workspace

        store.users[user.id] = user
        if _persist:
            store.persist()
        return user


def create_registration_response(user: User, *, postgres_repository=None) -> RegistrationResponse:
    _require_postgres_repository(postgres_repository, "Registration verification")
    settings = get_settings()
    if postgres_repository is not None:
        prepared, code = prepare_verification_state(user)
        outbox_id, encrypted, created_at = prepare_verification_email(
            recipient=user.email, code=code
        )
        updated = postgres_repository.rotate_email_verification(
            email=user.email,
            verification_hash=prepared.email_verification_hash,
            verification_salt=prepared.email_verification_salt,
            expires_at=prepared.email_verification_expires_at,
            sent_at=prepared.email_verification_sent_at,
            outbox_id=outbox_id,
            encrypted_code=encrypted,
            email_created_at=datetime.fromisoformat(created_at),
        )
        return RegistrationResponse(
            user=_to_auth_user(updated),
            message="Registration created. Verify your email before logging in.",
            dev_verification_code=_dev_verification_code(code),
        )
    if settings.email_dispatch_enabled:
        code = _issue_email_verification(user, persist=False)
        outbox_id, encrypted, created_at = prepare_verification_email(
            recipient=user.email, code=code
        )

        def persist_verification_and_email(conn) -> None:
            insert_verification_email(
                conn,
                outbox_id=outbox_id,
                recipient=user.email,
                encrypted_code=encrypted,
                created_at=created_at,
            )

        try:
            store.persist(extra_write=persist_verification_and_email)
        except Exception:
            # The SQLite transaction rolled back. Restore process-local state
            # before another request can observe the uncommitted verification.
            store.load()
            raise
    else:
        code = _issue_email_verification(user)
    return RegistrationResponse(
        user=_to_auth_user(user),
        message="Registration created. Verify your email before logging in.",
        dev_verification_code=_dev_verification_code(code),
    )


def resend_verification(email: str, *, postgres_repository=None) -> RegistrationResponse:
    _require_postgres_repository(postgres_repository, "Verification resend")
    normalized_email = email.lower().strip()
    user = (
        postgres_repository.get_user_by_email(normalized_email)
        if postgres_repository is not None
        else next((item for item in store.users.values() if item.email.lower() == normalized_email), None)
    )
    if not user or user.disabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.email_verified:
        return RegistrationResponse(
            user=_to_auth_user(user),
            verification_required=False,
            message="Email is already verified.",
        )
    return create_registration_response(user, postgres_repository=postgres_repository)


def verify_email(email: str, code: str, *, postgres_repository=None) -> VerifyEmailResponse:
    _require_postgres_repository(postgres_repository, "Email verification")
    normalized_email = email.lower().strip()
    if postgres_repository is not None:
        from .postgres_auth_repository import PostgresEmailVerificationError

        user = postgres_repository.get_user_by_email(normalized_email)
        if not user or user.disabled:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        if user.email_verified:
            return VerifyEmailResponse(user=_to_auth_user(user), message="Email already verified.")
        if not user.email_verification_salt:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No active verification code")
        candidate = _hash_secret(code.strip(), user.email_verification_salt)
        try:
            verified = postgres_repository.verify_email(
                email=normalized_email,
                candidate_hash=candidate,
            )
        except PostgresEmailVerificationError as error:
            if error.reason == "user_unavailable":
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found") from error
            details = {
                "no_active_code": "No active verification code",
                "expired": "Verification code expired",
                "invalid": "Invalid verification code",
            }
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=details.get(error.reason, "Verification failed"),
            ) from error
        return VerifyEmailResponse(
            user=_to_auth_user(verified),
            message="Email verified. You can now log in.",
        )
    user = next((item for item in store.users.values() if item.email.lower() == normalized_email), None)
    if not user or user.disabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.email_verified:
        return VerifyEmailResponse(user=_to_auth_user(user), message="Email already verified.")
    if not user.email_verification_hash or not user.email_verification_salt or not user.email_verification_expires_at:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No active verification code")
    if user.email_verification_expires_at <= now_utc():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Verification code expired")
    candidate = _hash_secret(code.strip(), user.email_verification_salt)
    if not _safe_equal(candidate, user.email_verification_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid verification code")

    user.email_verified = True
    user.email_verification_hash = None
    user.email_verification_salt = None
    user.email_verification_expires_at = None
    user.updated_at = now_utc()
    store.users[user.id] = user
    store.persist()
    return VerifyEmailResponse(user=_to_auth_user(user), message="Email verified. You can now log in.")


def authenticate_user(email: str, password: str, *, postgres_repository=None) -> User:
    _require_postgres_repository(postgres_repository, "Authentication")
    normalized_email = email.lower().strip()
    user = (
        postgres_repository.get_user_by_email(normalized_email)
        if postgres_repository is not None
        else next((item for item in store.users.values() if item.email.lower() == normalized_email), None)
    )
    if not user or user.disabled:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    candidate = _hash_secret(password, user.password_salt)
    if not _safe_equal(candidate, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    if not user.email_verified:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Email verification required")
    return user


def create_session(user: User, *, postgres_repository=None) -> AuthResponse:
    _require_postgres_repository(postgres_repository, "Session creation")
    raw_token = secrets.token_urlsafe(48)
    session = SessionToken(
        user_id=user.id,
        token_hash=_hash_token(raw_token),
        expires_at=now_utc() + timedelta(days=SESSION_DAYS),
    )
    if postgres_repository is not None:
        try:
            user = postgres_repository.create_session(session)
        except PermissionError as error:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not active") from error
    else:
        store.sessions[session.id] = session
        store.persist()
    return AuthResponse(access_token=raw_token, user=_to_auth_user(user))


def require_user(
    authorization: str | None = Header(default=None),
    request: Request = Depends(_request_from_context),
) -> AuthUser:
    """Require a bearer session token.

    This replaces the API-key MVP guard. Local dev can still disable auth with
    AIXION_AUTH_ENABLED=false or AIXION_PROFILE=demo, but production defaults to enabled.
    """
    if not auth_enabled():
        # A local auth bypass is scoped only when exactly one workspace exists.
        # Ambiguous multi-workspace state must not turn the dev identity into a
        # cross-workspace principal.
        workspace_id = next(iter(store.workspaces)) if len(store.workspaces) == 1 else None
        return AuthUser(
            id="dev_user",
            email="dev@local",
            display_name="Local Dev",
            role=UserRole.OWNER,
            workspace_id=workspace_id,
            email_verified=True,
        )

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")

    raw_token = authorization.split(" ", 1)[1].strip()
    token_hash = _hash_token(raw_token)
    postgres_repository = postgres_auth_repository(request)
    if postgres_repository is not None:
        authenticated = postgres_repository.authenticate_session(token_hash)
        if authenticated is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session")
        user = _to_auth_user(authenticated.user)
        # Auth and workspace ownership must be read from the same durable
        # runtime. Never let a PostgreSQL session fall back to the SQLite
        # membership adapter or trust only the workspace claim in its payload.
        from .workspace_access import workspace_id_for_user

        membership_repository = postgres_workspace_membership_repository(request)
        if membership_repository is None:
            raise RuntimeError("PostgreSQL workspace membership repository is unavailable")
        workspace_id_for_user(user, membership_repository=membership_repository)
        return user
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
    if not user.email_verified:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Email verification required")
    return _to_auth_user(user)


def require_roles(*allowed_roles: UserRole) -> Callable[[AuthUser], AuthUser]:
    allowed = set(allowed_roles)

    def dependency(user: AuthUser = Depends(require_user)) -> AuthUser:
        if user.role not in allowed:
            allowed_names = ", ".join(role.value for role in sorted(allowed, key=lambda role: role.value))
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires one of roles: {allowed_names}",
            )
        return user

    return dependency


require_owner = require_roles(UserRole.OWNER)
require_maintainer = require_roles(UserRole.OWNER, UserRole.MAINTAINER)
require_reviewer = require_roles(UserRole.OWNER, UserRole.MAINTAINER, UserRole.REVIEWER)

# Backward-compatible dependency name while endpoints are migrated.
require_api_key = require_user
