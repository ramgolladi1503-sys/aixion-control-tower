from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from .auth import require_owner
from .models import AuditEvent, AuthUser, Invite, InviteStatus, UserRole, now_utc
from .store import store

INVITE_DAYS = 7

router = APIRouter(prefix="/auth/invites", tags=["auth"])


class InviteCreateRequest(BaseModel):
    email: EmailStr
    role: UserRole = UserRole.REVIEWER
    expires_in_days: int = Field(default=INVITE_DAYS, ge=1, le=30)


class InviteResponse(BaseModel):
    id: str
    email: str
    role: UserRole
    status: InviteStatus
    expires_at: object
    created_by_user_id: str
    accepted_by_user_id: str | None = None
    created_at: object
    updated_at: object


class InviteCreateResponse(InviteResponse):
    token: str


def hash_invite_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalize_email(email: str) -> str:
    return email.lower().strip()


def public_invite(invite: Invite) -> InviteResponse:
    return InviteResponse(
        id=invite.id,
        email=invite.email,
        role=invite.role,
        status=effective_invite_status(invite),
        expires_at=invite.expires_at,
        created_by_user_id=invite.created_by_user_id,
        accepted_by_user_id=invite.accepted_by_user_id,
        created_at=invite.created_at,
        updated_at=invite.updated_at,
    )


def effective_invite_status(invite: Invite) -> InviteStatus:
    if invite.status == InviteStatus.PENDING and invite.expires_at <= now_utc():
        return InviteStatus.EXPIRED
    return invite.status


def ensure_no_pending_invite(email: str) -> None:
    for invite in store.invites.values():
        if invite.email == email and effective_invite_status(invite) == InviteStatus.PENDING:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Pending invite already exists")


def record_invite_event(event_type: str, actor: AuthUser, invite: Invite) -> None:
    store.audit_events.append(
        AuditEvent(
            event_type=event_type,
            actor=actor.email,
            entity_id=invite.id,
            details={
                "invite_id": invite.id,
                "email": invite.email,
                "role": invite.role,
                "status": invite.status,
                "expires_at": invite.expires_at.isoformat(),
            },
        )
    )


def validate_invite_token(token: str) -> Invite:
    token_hash = hash_invite_token(token)
    invite = next((item for item in store.invites.values() if item.token_hash == token_hash), None)
    if not invite:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite not found")

    invite.status = effective_invite_status(invite)
    if invite.status != InviteStatus.PENDING:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Invite is {invite.status}")
    return invite


@router.post("", response_model=InviteCreateResponse)
def create_invite(payload: InviteCreateRequest, actor: AuthUser = Depends(require_owner)) -> InviteCreateResponse:
    email = normalize_email(str(payload.email))
    ensure_no_pending_invite(email)

    raw_token = secrets.token_urlsafe(32)
    invite = Invite(
        email=email,
        role=payload.role,
        token_hash=hash_invite_token(raw_token),
        expires_at=now_utc() + timedelta(days=payload.expires_in_days),
        created_by_user_id=actor.id,
    )
    store.invites[invite.id] = invite
    record_invite_event("auth.invite_created", actor=actor, invite=invite)
    store.persist()

    public = public_invite(invite)
    return InviteCreateResponse(**public.model_dump(), token=raw_token)


@router.get("", response_model=list[InviteResponse])
def list_invites(_: AuthUser = Depends(require_owner)) -> list[InviteResponse]:
    changed = False
    for invite in store.invites.values():
        status_value = effective_invite_status(invite)
        if invite.status != status_value:
            invite.status = status_value
            invite.updated_at = now_utc()
            changed = True
    if changed:
        store.persist()

    return [
        public_invite(invite)
        for invite in sorted(store.invites.values(), key=lambda item: item.created_at, reverse=True)
    ]


@router.post("/{invite_id}/revoke", response_model=InviteResponse)
def revoke_invite(invite_id: str, actor: AuthUser = Depends(require_owner)) -> InviteResponse:
    invite = store.invites.get(invite_id)
    if not invite:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite not found")

    invite.status = effective_invite_status(invite)
    if invite.status == InviteStatus.ACCEPTED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Accepted invites cannot be revoked")
    if invite.status == InviteStatus.REVOKED:
        return public_invite(invite)
    if invite.status == InviteStatus.EXPIRED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Expired invites cannot be revoked")

    invite.status = InviteStatus.REVOKED
    invite.updated_at = now_utc()
    record_invite_event("auth.invite_revoked", actor=actor, invite=invite)
    store.persist()
    return public_invite(invite)
