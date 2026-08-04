from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from .models import UserRole, new_id, now_utc


class ActionAuthorizationDecision(StrEnum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"


class ActionAuthorizationCreate(BaseModel):
    decision: ActionAuthorizationDecision
    reason: str = Field(min_length=3, max_length=1000)


class ActionAuthorization(BaseModel):
    id: str = Field(default_factory=lambda: new_id("action_authorization"))
    action_id: str
    action_payload_hash: str = ""
    policy_decision_id: str
    reviewer_user_id: str
    reviewer_role: UserRole
    decision: ActionAuthorizationDecision
    reason: str
    signature: str
    created_at: datetime = Field(default_factory=now_utc)
