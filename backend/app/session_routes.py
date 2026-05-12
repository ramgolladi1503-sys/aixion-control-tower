from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from .auth import require_owner
from .models import AuditEvent, AuthUser, SessionToken, UserRole, now_utc
from .store import store

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


def to_session_response(session: SessionToken) -> SessionResponse:
    user = store.users.get(session.user_id)
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


def record_session_revocation(actor: AuthUser, target_user_id: str, target_email: str, revoked_count: int) -> None:
    store.audit_events.append(
        AuditEvent(
            event_type="auth.sessions_revoked",
            actor=actor.email,
            entity_id=target_user_id,
            details={
                "target_user_id": target_user_id,
                "target_email": target_email,
                "revoked_sessions_count": revoked_count,
            },
        )
    )


@router.get("/sessions", response_model=list[SessionResponse])
def list_sessions(_: AuthUser = Depends(require_owner)) -> list[SessionResponse]:
    return [
        to_session_response(session)
        for session in sorted(store.sessions.values(), key=lambda item: item.created_at, reverse=True)
    ]


@router.post("/users/{user_id}/sessions/revoke", response_model=SessionRevokeResponse)
def revoke_user_sessions(user_id: str, actor: AuthUser = Depends(require_owner)) -> SessionRevokeResponse:
    target = store.users.get(user_id)
    if not target:
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
    )
    store.persist()
    return SessionRevokeResponse(
        target_user_id=target.id,
        target_email=target.email,
        revoked_sessions_count=len(active_sessions),
    )
