from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from .auth import (
    AuthResponse,
    LoginRequest,
    RegisterRequest,
    RegistrationResponse,
    ResendVerificationRequest,
    VerifyEmailRequest,
    VerifyEmailResponse,
    authenticate_user,
    create_registration_response,
    create_session,
    create_user,
    require_user,
    resend_verification,
    verify_email,
)
from .email_policy import validate_registration_email_deliverability
from .invite_routes import record_invite_event, validate_invite_token
from .models import AuditEvent, AuthUser, InviteStatus, now_utc
from .password_policy import validate_password_policy
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


def register_with_invite(payload: RegisterRequest) -> RegistrationResponse:
    _validate_registration_payload(payload)
    normalized_email = str(payload.email).lower().strip()
    invite = validate_invite_token(payload.invite_token or "")
    if invite.email != normalized_email:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invite email does not match registration email")

    user = create_user(
        email=payload.email,
        password=payload.password,
        display_name=payload.display_name,
        role=invite.role,
        email_verified=False,
    )
    invite.status = InviteStatus.ACCEPTED
    invite.accepted_by_user_id = user.id
    invite.updated_at = now_utc()
    record_invite_event("auth.invite_accepted", actor=AuthUser(id=user.id, email=user.email, display_name=user.display_name, role=user.role), invite=invite)
    store.persist()
    return create_registration_response(user)


@router.post("/register", response_model=RegistrationResponse)
def register(payload: RegisterRequest) -> RegistrationResponse:
    _validate_registration_payload(payload)
    if not store.users:
        user = create_user(
            email=payload.email,
            password=payload.password,
            display_name=payload.display_name,
            email_verified=False,
        )
        return create_registration_response(user)

    if not payload.invite_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invite token is required after bootstrap owner registration",
        )

    return register_with_invite(payload)


@router.post("/verify-email", response_model=VerifyEmailResponse)
def verify_registered_email(payload: VerifyEmailRequest) -> VerifyEmailResponse:
    return verify_email(payload.email, payload.code)


@router.post("/resend-verification", response_model=RegistrationResponse)
def resend_registered_email_verification(payload: ResendVerificationRequest) -> RegistrationResponse:
    return resend_verification(payload.email)


@router.post("/login", response_model=AuthResponse)
def login(payload: LoginRequest) -> AuthResponse:
    user = authenticate_user(email=payload.email, password=payload.password)
    return create_session(user)


@router.get("/me", response_model=AuthUser)
def me(user: AuthUser = Depends(require_user)) -> AuthUser:
    return user


@router.post("/account-deletion-request", response_model=AccountDeletionRequestResponse)
def request_account_deletion(
    payload: AccountDeletionRequestCreate,
    actor: AuthUser = Depends(require_user),
) -> AccountDeletionRequestResponse:
    """Record an authenticated account-deletion request and disable access.

    This is intentionally request-and-disable, not silent hard deletion. The product keeps
    audit/control evidence, so final deletion/anonymization rules must be handled by an
    operator according to the published retention policy.
    """
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
