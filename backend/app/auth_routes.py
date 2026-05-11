from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from .auth import AuthResponse, LoginRequest, RegisterRequest, authenticate_user, create_session, create_user, require_user
from .invite_routes import record_invite_event, validate_invite_token
from .models import AuthUser, InviteStatus, now_utc
from .store import store

router = APIRouter(prefix="/auth", tags=["auth"])


def register_with_invite(payload: RegisterRequest) -> AuthResponse:
    normalized_email = str(payload.email).lower().strip()
    invite = validate_invite_token(payload.invite_token or "")
    if invite.email != normalized_email:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invite email does not match registration email")

    user = create_user(
        email=payload.email,
        password=payload.password,
        display_name=payload.display_name,
        role=invite.role,
    )
    invite.status = InviteStatus.ACCEPTED
    invite.accepted_by_user_id = user.id
    invite.updated_at = now_utc()
    record_invite_event("auth.invite_accepted", actor=AuthUser(id=user.id, email=user.email, display_name=user.display_name, role=user.role), invite=invite)
    store.persist()
    return create_session(user)


@router.post("/register", response_model=AuthResponse)
def register(payload: RegisterRequest) -> AuthResponse:
    if not store.users:
        user = create_user(
            email=payload.email,
            password=payload.password,
            display_name=payload.display_name,
        )
        return create_session(user)

    if not payload.invite_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invite token is required after bootstrap owner registration",
        )

    return register_with_invite(payload)


@router.post("/login", response_model=AuthResponse)
def login(payload: LoginRequest) -> AuthResponse:
    user = authenticate_user(email=payload.email, password=payload.password)
    return create_session(user)


@router.get("/me", response_model=AuthUser)
def me(user: AuthUser = Depends(require_user)) -> AuthUser:
    return user
