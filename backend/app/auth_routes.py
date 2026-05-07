from __future__ import annotations

from fastapi import APIRouter, Depends

from .auth import AuthResponse, LoginRequest, RegisterRequest, authenticate_user, create_session, create_user, require_user
from .models import AuthUser

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=AuthResponse)
def register(payload: RegisterRequest) -> AuthResponse:
    user = create_user(
        email=payload.email,
        password=payload.password,
        display_name=payload.display_name,
    )
    return create_session(user)


@router.post("/login", response_model=AuthResponse)
def login(payload: LoginRequest) -> AuthResponse:
    user = authenticate_user(email=payload.email, password=payload.password)
    return create_session(user)


@router.get("/me", response_model=AuthUser)
def me(user: AuthUser = Depends(require_user)) -> AuthUser:
    return user
