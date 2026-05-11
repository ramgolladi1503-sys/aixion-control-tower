from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from .auth import require_owner, require_user
from .models import AuditEvent, AuthUser, User, UserRole, now_utc
from .store import store

router = APIRouter(prefix="/auth", tags=["auth"])


class RoleChoicesResponse(BaseModel):
    roles: list[UserRole]


class RoleUpdateRequest(BaseModel):
    role: UserRole


def to_public_user(user: User) -> AuthUser:
    return AuthUser(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
    )


def active_owner_count() -> int:
    return sum(1 for user in store.users.values() if user.role == UserRole.OWNER and not user.disabled)


def record_role_update(actor: AuthUser, target: User, previous_role: UserRole) -> None:
    store.audit_events.append(
        AuditEvent(
            event_type="auth.role_updated",
            actor=actor.email,
            entity_id=target.id,
            details={
                "target_user_id": target.id,
                "target_email": target.email,
                "previous_role": previous_role,
                "new_role": target.role,
            },
        )
    )


@router.get("/roles", response_model=RoleChoicesResponse)
def available_roles(_: AuthUser = Depends(require_user)) -> RoleChoicesResponse:
    return RoleChoicesResponse(roles=list(UserRole))


@router.get("/users", response_model=list[AuthUser])
def list_users(_: AuthUser = Depends(require_owner)) -> list[AuthUser]:
    return [to_public_user(user) for user in sorted(store.users.values(), key=lambda item: item.email)]


@router.patch("/users/{user_id}/role", response_model=AuthUser)
def update_user_role(
    user_id: str,
    payload: RoleUpdateRequest,
    actor: AuthUser = Depends(require_owner),
) -> AuthUser:
    target = store.users.get(user_id)
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    previous_role = target.role
    requested_role = payload.role

    if previous_role == requested_role:
        return to_public_user(target)

    if previous_role == UserRole.OWNER and requested_role != UserRole.OWNER and active_owner_count() <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot remove the last active OWNER.",
        )

    target.role = requested_role
    target.updated_at = now_utc()
    record_role_update(actor=actor, target=target, previous_role=previous_role)
    store.persist()
    return to_public_user(target)
