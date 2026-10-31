from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from .auth import postgres_auth_repository, require_owner
from .models import AuditEvent, AuthUser, SessionToken, User, UserRole, now_utc
from .store import store
from .workspace_access import workspace_id_for_user
from .postgres_auth_repository import PostgresSessionAuthorizationError

router = APIRouter(prefix="/auth", tags=["auth"])


class SessionResponse(BaseModel):
    id: str
    user_id: str
    user_email: str
    user_role: UserRole
    created_at: datetime
    expires_at: datetime
    revoked: bool
    active: bool


class SessionRevokeResponse(BaseModel):
    target_user_id: str
    target_email: str
    revoked_sessions_count: int


def session_is_active(session: SessionToken) -> bool:
    return not session.revoked and session.expires_at > now_utc()


def to_session_response(session: SessionToken, user: User | None = None) -> SessionResponse:
    user = user or store.users.get(session.user_id)
    return SessionResponse(
        id=session.id,
        user_id=session.user_id,
        user_email=user.email if user else "unknown",
        user_role=user.role if user else UserRole.REVIEWER,
        created_at=session.created_at,
        expires_at=session.expires_at,
        revoked=session.revoked,
        active=session_is_active(session),
    )


def record_session_revocation(
    actor: AuthUser,
    target_user_id: str,
    target_email: str,
    revoked_count: int,
    workspace_id: str | None,
) -> None:
    store.audit_events.append(
        AuditEvent(
            event_type="auth.sessions_revoked",
            actor=actor.email,
            entity_id=target_user_id,
            workspace_id=workspace_id,
            details={
                "target_user_id": target_user_id,
                "target_email": target_email,
                "revoked_sessions_count": revoked_count,
            },
        )
    )


@router.get("/sessions", response_model=list[SessionResponse])
def list_sessions(
    actor: AuthUser = Depends(require_owner), request: Request = None  # type: ignore[assignment]
) -> list[SessionResponse]:
    postgres_repository = postgres_auth_repository(request)
    if postgres_repository is not None:
        try:
            return [
                to_session_response(session, user)
                for session, user in postgres_repository.list_sessions(actor.id)
            ]
        except PostgresSessionAuthorizationError as error:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    workspace_id = workspace_id_for_user(actor)
    return [
        to_session_response(session)
        for session in sorted(store.sessions.values(), key=lambda item: item.created_at, reverse=True)
        if (account := store.users.get(session.user_id)) is not None
        and (workspace_id is None or account.workspace_id == workspace_id)
    ]


@router.post("/users/{user_id}/sessions/revoke", response_model=SessionRevokeResponse)
def revoke_user_sessions(
    user_id: str,
    actor: AuthUser = Depends(require_owner),
    request: Request = None,  # type: ignore[assignment]
) -> SessionRevokeResponse:
    postgres_repository = postgres_auth_repository(request)
    if postgres_repository is not None:
        try:
            result = postgres_repository.revoke_user_sessions(
                actor_user_id=actor.id,
                target_user_id=user_id,
            )
        except PostgresSessionAuthorizationError as error:
            message = str(error)
            code = status.HTTP_409_CONFLICT if "own sessions" in message.lower() else status.HTTP_404_NOT_FOUND if "unavailable" in message.lower() else status.HTTP_403_FORBIDDEN
            raise HTTPException(status_code=code, detail=message) from error
        return SessionRevokeResponse(
            target_user_id=result.target.id,
            target_email=result.target.email,
            revoked_sessions_count=result.revoked_count,
        )
    target = store.users.get(user_id)
    workspace_id = workspace_id_for_user(actor)
    if not target or (workspace_id is not None and target.workspace_id != workspace_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if target.id == actor.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Owners cannot revoke their own sessions through this endpoint.",
        )

    active_sessions = [session for session in store.sessions.values() if session.user_id == target.id and session_is_active(session)]
    for session in active_sessions:
        session.revoked = True

    record_session_revocation(
        actor=actor,
        target_user_id=target.id,
        target_email=target.email,
        revoked_count=len(active_sessions),
        workspace_id=target.workspace_id,
    )
    store.persist()
    return SessionRevokeResponse(
        target_user_id=target.id,
        target_email=target.email,
        revoked_sessions_count=len(active_sessions),
    )
