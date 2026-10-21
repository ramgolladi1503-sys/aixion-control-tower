from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from .application_repository_access import application_repositories_for_request
from .auth import (
    AuthResponse,
    LoginRequest,
    RegisterRequest,
    RegistrationResponse,
    ResendVerificationRequest,
    VerifyEmailRequest,
    VerifyEmailResponse,
    _to_auth_user,
    authenticate_user,
    create_registration_response,
    create_session,
    create_user,
    new_user_record,
    prepare_verification_state,
    postgres_auth_repository,
    require_user,
    resend_verification,
    verify_email,
)
from .auth_rate_limits import enforce_auth_rate_limit
from .email_delivery import prepare_verification_email
from .email_policy import validate_registration_email_deliverability
from .invite_routes import hash_invite_token, record_invite_event, validate_invite_token
from .models import AuditEvent, AuthUser, InviteStatus, UserRole, Workspace, now_utc
from .postgres_auth_repository import PostgresAccountDeletionError, PostgresAuthConflictError
from .password_policy import validate_password_policy
from .settings import get_settings
from .store import store

router = APIRouter(prefix="/auth", tags=["auth"])


class AccountDeletionRequestCreate(BaseModel):
    reason: str = Field(default="", max_length=1000)


class AccountDeletionRequestResponse(BaseModel):
    user_id: str
    email: str
    status: str
    requested_at: datetime
    active_sessions_revoked: int
    message: str


def _validate_registration_payload(payload: RegisterRequest) -> None:
    try:
        validate_registration_email_deliverability(str(payload.email))
        validate_password_policy(payload.password, str(payload.email))
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error


def _request_ip(request: Request) -> str:
    # Do not trust caller-supplied forwarding headers here. Deployments behind
    # a proxy must configure trusted-proxy handling at the ASGI server boundary.
    return request.client.host if request.client is not None else "unknown-client"


def _limit(request: Request, bucket: str, identifier: str, maximum: int) -> None:
    settings = get_settings()
    postgres_repository = None
    if settings.postgres_auth_rate_limits_enabled or settings.postgres_auth_enabled:
        postgres_repository = application_repositories_for_request(request).auth_rate_limits
    enforce_auth_rate_limit(
        bucket=bucket,
        identifier=identifier,
        limit=maximum,
        window_seconds=settings.auth_rate_limit_window_seconds,
        postgres_repository=postgres_repository,
    )


def _postgres_registration_email(user):
    prepared, code = prepare_verification_state(user)
    outbox_id, encrypted, created_at = prepare_verification_email(
        recipient=user.email,
        code=code,
    )
    return prepared, code, outbox_id, encrypted, datetime.fromisoformat(created_at)


def _registration_response(user, code: str) -> RegistrationResponse:
    settings = get_settings()
    return RegistrationResponse(
        user=_to_auth_user(user),
        message="Registration created. Verify your email before logging in.",
        dev_verification_code=code if settings.profile in {"local", "demo", "test"} else None,
    )


def _record_account_deletion_request(
    actor: AuthUser,
    target_user_id: str,
    target_email: str,
    revoked_count: int,
    reason: str,
) -> None:
    store.audit_events.append(
        AuditEvent(
            event_type="auth.account_deletion_requested",
            actor=actor.email,
            entity_id=target_user_id,
            details={
                "target_user_id": target_user_id,
                "target_email": target_email,
                "active_sessions_revoked": revoked_count,
                "status": "RECEIVED",
                "reason_provided": bool(reason.strip()),
            },
        )
    )


def register_with_invite(payload: RegisterRequest, *, postgres_repository=None) -> RegistrationResponse:
    _validate_registration_payload(payload)
    normalized_email = str(payload.email).lower().strip()
    if postgres_repository is not None:
        invite = postgres_repository.get_invite_by_token_hash(
            hash_invite_token(payload.invite_token or "")
        )
        if invite is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite not found")
        if invite.email != normalized_email:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invite email does not match registration email",
            )
        if not invite.workspace_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invite workspace is unavailable")
        user = new_user_record(
            email=normalized_email,
            password=payload.password,
            display_name=payload.display_name,
            role=invite.role,
            workspace_id=invite.workspace_id,
        )
        prepared, code, outbox_id, encrypted, created_at = _postgres_registration_email(user)
        try:
            registered, _, _ = postgres_repository.register_with_invite(
                user=prepared,
                token_hash=invite.token_hash,
                outbox_id=outbox_id,
                encrypted_code=encrypted,
                email_created_at=created_at,
            )
        except PostgresAuthConflictError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        return _registration_response(registered, code)
    with store.atomic():
        invite = validate_invite_token(payload.invite_token or "")
        if invite.email != normalized_email:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invite email does not match registration email",
            )
        if not invite.workspace_id or invite.workspace_id not in store.workspaces:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invite workspace is unavailable",
            )

        user = create_user(
            email=payload.email,
            password=payload.password,
            display_name=payload.display_name,
            role=invite.role,
            email_verified=False,
            workspace_id=invite.workspace_id,
            _persist=False,
        )
        invite.status = InviteStatus.ACCEPTED
        invite.accepted_by_user_id = user.id
        invite.updated_at = now_utc()
        actor = AuthUser(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            role=user.role,
            workspace_id=user.workspace_id,
        )
        record_invite_event("auth.invite_accepted", actor=actor, invite=invite)
        try:
            # Registration response creation persists the verification state
            # and optional email outbox together with the user and invite.
            return create_registration_response(user)
        except Exception:
            # Restore all compatibility maps from the last committed snapshot
            # before releasing the lock to another request.
            store.load()
            raise


@router.post("/register", response_model=RegistrationResponse)
def register(payload: RegisterRequest, request: Request) -> RegistrationResponse:
    settings = get_settings()
    _limit(request, "auth.register.ip", _request_ip(request), settings.auth_register_limit_per_ip)
    _validate_registration_payload(payload)
    postgres_repository = postgres_auth_repository(request)
    if postgres_repository is not None:
        if payload.invite_token:
            return register_with_invite(payload, postgres_repository=postgres_repository)
        user = new_user_record(
            email=str(payload.email),
            password=payload.password,
            display_name=payload.display_name,
            role=UserRole.OWNER,
            workspace_id=None,
        )
        workspace = Workspace(name=user.display_name, owner_user_id=user.id)
        user = user.model_copy(update={"workspace_id": workspace.id})
        prepared, code, outbox_id, encrypted, created_at = _postgres_registration_email(user)
        try:
            registered = postgres_repository.bootstrap_owner(
                user=prepared,
                workspace=workspace,
                outbox_id=outbox_id,
                encrypted_code=encrypted,
                email_created_at=created_at,
            )
        except PostgresAuthConflictError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        return _registration_response(registered, code)
    # Serialize account/workspace creation with user persistence. Every
    # no-invite registration creates its own owner account; invite registrations
    # join only the workspace named by the validated invite.
    with store.atomic():
        if payload.invite_token:
            return register_with_invite(payload)
        user = create_user(
            email=payload.email,
            password=payload.password,
            display_name=payload.display_name,
            role=UserRole.OWNER,
            email_verified=False,
            create_workspace=True,
        )
        return create_registration_response(user)


@router.post("/verify-email", response_model=VerifyEmailResponse)
def verify_registered_email(payload: VerifyEmailRequest, request: Request) -> VerifyEmailResponse:
    settings = get_settings()
    _limit(request, "auth.verify.ip", _request_ip(request), settings.auth_verify_limit_per_ip)
    _limit(request, "auth.verify.email", str(payload.email), settings.auth_verify_limit_per_email)
    return verify_email(
        payload.email,
        payload.code,
        postgres_repository=postgres_auth_repository(request),
    )


@router.post("/resend-verification", response_model=RegistrationResponse)
def resend_registered_email_verification(
    payload: ResendVerificationRequest, request: Request
) -> RegistrationResponse:
    settings = get_settings()
    _limit(request, "auth.resend.ip", _request_ip(request), settings.auth_resend_limit_per_ip)
    _limit(request, "auth.resend.email", str(payload.email), settings.auth_resend_limit_per_email)
    return resend_verification(
        payload.email,
        postgres_repository=postgres_auth_repository(request),
    )


@router.post("/login", response_model=AuthResponse)
def login(payload: LoginRequest, request: Request) -> AuthResponse:
    settings = get_settings()
    _limit(request, "auth.login.ip", _request_ip(request), settings.auth_login_limit_per_ip)
    postgres_repository = postgres_auth_repository(request)
    user = authenticate_user(
        email=payload.email,
        password=payload.password,
        postgres_repository=postgres_repository,
    )
    return create_session(user, postgres_repository=postgres_repository)


@router.get("/me", response_model=AuthUser)
def me(user: AuthUser = Depends(require_user)) -> AuthUser:
    return user


@router.post("/account-deletion-request", response_model=AccountDeletionRequestResponse)
def request_account_deletion(
    payload: AccountDeletionRequestCreate,
    request: Request,
    actor: AuthUser = Depends(require_user),
) -> AccountDeletionRequestResponse:
    """Record an authenticated account-deletion request and disable access.

    This is intentionally request-and-disable, not silent hard deletion. The product keeps
    audit/control evidence, so final deletion/anonymization rules must be handled by an
    operator according to the published retention policy.
    """
    postgres_repository = postgres_auth_repository(request)
    if postgres_repository is not None:
        try:
            result = postgres_repository.request_account_deletion(
                user_id=actor.id,
                reason_provided=bool(payload.reason.strip()),
            )
        except PostgresAccountDeletionError as error:
            code = status.HTTP_409_CONFLICT if "already disabled" in str(error).lower() else status.HTTP_404_NOT_FOUND
            raise HTTPException(status_code=code, detail=str(error)) from error
        return AccountDeletionRequestResponse(
            user_id=result.target.id,
            email=result.target.email,
            status="RECEIVED",
            requested_at=result.audit_event.created_at,
            active_sessions_revoked=result.revoked_sessions_count,
            message="Account deletion request received. Account access has been disabled while retention/anonymization is processed.",
        )
    target = store.users.get(actor.id)
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Authenticated user not found")
    if target.disabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Account is already disabled")

    requested_at = now_utc()
    active_sessions = [session for session in store.sessions.values() if session.user_id == target.id and not session.revoked and session.expires_at > requested_at]
    for session in active_sessions:
        session.revoked = True

    target.disabled = True
    target.updated_at = requested_at
    store.users[target.id] = target
    _record_account_deletion_request(
        actor=actor,
        target_user_id=target.id,
        target_email=target.email,
        revoked_count=len(active_sessions),
        reason=payload.reason,
    )
    store.persist()
    return AccountDeletionRequestResponse(
        user_id=target.id,
        email=target.email,
        status="RECEIVED",
        requested_at=requested_at,
        active_sessions_revoked=len(active_sessions),
        message="Account deletion request received. Account access has been disabled while retention/anonymization is processed.",
    )


@router.get("/account-deletion-info")
def account_deletion_info() -> dict[str, str]:
    return {
        "app": "Aixion Control Tower",
        "status": "available",
        "authenticated_request_endpoint": "/auth/account-deletion-request",
        "public_deletion_url_status": "TODO: publish a public HTTPS account deletion page before Play Store submission",
        "retention_note": "Audit/security records may be retained or anonymized according to the published retention policy.",
    }
