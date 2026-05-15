from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from .models import AgentAuthType, new_id, now_utc


class AgentCredentialState(StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    EXPIRED = "EXPIRED"
    MANUAL = "MANUAL"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    REVOKED = "REVOKED"


class AgentCredentialRecord(BaseModel):
    id: str = Field(default_factory=lambda: new_id("agent_credential"))
    agent_id: str
    token_expires_at: datetime | None = None
    token_revoked_at: datetime | None = None
    token_rotated_at: datetime | None = None
    last_used_at: datetime | None = None
    failed_auth_count: int = 0
    last_auth_failed_at: datetime | None = None
    rate_limit_per_minute: int = 60
    rate_limit_window_started_at: datetime | None = None
    rate_limit_request_count: int = 0
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class AgentCredentialStatus(BaseModel):
    agent_id: str
    auth_type: AgentAuthType
    credential_state: AgentCredentialState
    token_present: bool
    token_expires_at: datetime | None = None
    token_revoked_at: datetime | None = None
    token_rotated_at: datetime | None = None
    last_used_at: datetime | None = None
    failed_auth_count: int = 0
    last_auth_failed_at: datetime | None = None
    rate_limit_per_minute: int
    rate_limit_window_started_at: datetime | None = None
    rate_limit_request_count: int = 0


class AgentTokenRotateRequest(BaseModel):
    token_expires_at: datetime | None = None
    rate_limit_per_minute: int | None = Field(default=None, ge=1, le=1000)
