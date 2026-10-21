from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from .auth import postgres_auth_repository, require_owner, require_user
from .models import AuditEvent, AuthUser, User, UserRole, now_utc
from .store import store
from .workspace_access import workspace_id_for_user
from .postgres_auth_repository import PostgresRoleManagementError

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
        workspace_id=user.workspace_id,
        email_verified=user.email_verified,
    )


def active_owner_count(workspace_id: str | None = None) -> int:
    return sum(
        1 for user in store.users.values()
        if user.role == UserRole.OWNER and not user.disabled
        and (workspace_id is None or user.workspace_id == workspace_id)
    )


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
def list_users(
    actor: AuthUser = Depends(require_owner), request: Request = None  # type: ignore[assignment]
) -> list[AuthUser]:
    postgres_repository = postgres_auth_repository(request)
    if postgres_repository is not None:
        try:
            return [to_public_user(user) for user in postgres_repository.list_workspace_users(actor_user_id=actor.id)]
        except PostgresRoleManagementError as error:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    workspace_id = workspace_id_for_user(actor)
    users = [user for user in store.users.values() if workspace_id is None or user.workspace_id == workspace_id]
    return [to_public_user(user) for user in sorted(users, key=lambda item: item.email)]


@router.patch("/users/{user_id}/role", response_model=AuthUser)
def update_user_role(
    user_id: str,
    payload: RoleUpdateRequest,
    actor: AuthUser = Depends(require_owner),
    request: Request = None,  # type: ignore[assignment]
) -> AuthUser:
    postgres_repository = postgres_auth_repository(request)
    if postgres_repository is not None:
        try:
            result = postgres_repository.update_user_role(
                actor_user_id=actor.id,
                target_user_id=user_id,
                role=payload.role,
            )
        except PostgresRoleManagementError as error:
            message = str(error)
            code = status.HTTP_404_NOT_FOUND if "unavailable" in message.lower() else status.HTTP_409_CONFLICT
            raise HTTPException(status_code=code, detail=message) from error
        return to_public_user(result.target)
    target = store.users.get(user_id)
    workspace_id = workspace_id_for_user(actor)
    if not target or (workspace_id is not None and target.workspace_id != workspace_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    previous_role = target.role
    requested_role = payload.role

    if previous_role == requested_role:
        return to_public_user(target)

    if previous_role == UserRole.OWNER and requested_role != UserRole.OWNER and active_owner_count(workspace_id) <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot remove the last active OWNER.",
        )

    target.role = requested_role
    target.updated_at = now_utc()
    record_role_update(actor=actor, target=target, previous_role=previous_role)
    store.persist()
    return to_public_user(target)
