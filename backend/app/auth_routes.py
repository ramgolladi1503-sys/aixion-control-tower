from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

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
from .models import AuthUser, InviteStatus, now_utc
from .password_policy import validate_password_policy
from .store import store

router = APIRouter(prefix="/auth", tags=["auth"])


def _validate_registration_payload(payload: RegisterRequest) -> None:
    try:
        validate_registration_email_deliverability(str(payload.email))
        validate_password_policy(payload.password, str(payload.email))
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error


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