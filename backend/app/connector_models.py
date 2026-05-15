from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from .models import AgentAction, new_id, now_utc


class ConnectorType(StrEnum):
    GENERIC_HTTP = "GENERIC_HTTP"
    WEBHOOK = "WEBHOOK"
    MCP = "MCP"
    LOCAL_BRIDGE = "LOCAL_BRIDGE"
    GPT_ACTIONS = "GPT_ACTIONS"
    CUSTOM = "CUSTOM"


class ConnectorProviderLabel(StrEnum):
    OPENCLAW = "OPENCLAW"
    ANTIGRAVITY = "ANTIGRAVITY"
    GEMINI = "GEMINI"
    CLAUDE = "CLAUDE"
    CURSOR = "CURSOR"
    CHATGPT = "CHATGPT"
    CODEX = "CODEX"
    CUSTOM = "CUSTOM"


class ConnectorAuthType(StrEnum):
    NONE = "NONE"
    API_KEY = "API_KEY"
    BEARER = "BEARER"
    HMAC = "HMAC"


class ConnectorStatus(StrEnum):
    ENABLED = "ENABLED"
    DISABLED = "DISABLED"


class ConnectorHealthStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILING = "FAILING"
    DISABLED = "DISABLED"


class ConnectorCreate(BaseModel):
    name: str
    connector_type: ConnectorType = ConnectorType.GENERIC_HTTP
    provider_label: ConnectorProviderLabel = ConnectorProviderLabel.CUSTOM
    endpoint_url: str | None = None
    callback_url: str | None = None
    auth_type: ConnectorAuthType = ConnectorAuthType.BEARER
    allowed_project_ids: list[str] = Field(default_factory=list)
    allowed_repositories: list[str] = Field(default_factory=list)
    allowed_actions: list[AgentAction] = Field(default_factory=lambda: [AgentAction.CREATE_AGENT_TASK])
    rate_limit_per_minute: int = Field(default=60, ge=1, le=1000)
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Connector name is required")
        if len(cleaned) > 120:
            raise ValueError("Connector name is too long")
        return cleaned

    @field_validator("endpoint_url", "callback_url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if len(cleaned) > 500:
            raise ValueError("Connector URL is too long")
        if not (cleaned.startswith("https://") or cleaned.startswith("http://localhost") or cleaned.startswith("http://127.0.0.1")):
            raise ValueError("Connector URL must be https or local development HTTP")
        return cleaned


class ConnectorUpdate(BaseModel):
    name: str | None = None
    connector_type: ConnectorType | None = None
    provider_label: ConnectorProviderLabel | None = None
    endpoint_url: str | None = None
    callback_url: str | None = None
    auth_type: ConnectorAuthType | None = None
    allowed_project_ids: list[str] | None = None
    allowed_repositories: list[str] | None = None
    allowed_actions: list[AgentAction] | None = None
    rate_limit_per_minute: int | None = Field(default=None, ge=1, le=1000)
    enabled: bool | None = None
    config: dict[str, Any] | None = None


class AgentConnector(BaseModel):
    id: str = Field(default_factory=lambda: new_id("connector"))
    name: str
    connector_type: ConnectorType = ConnectorType.GENERIC_HTTP
    provider_label: ConnectorProviderLabel = ConnectorProviderLabel.CUSTOM
    endpoint_url: str | None = None
    callback_url: str | None = None
    auth_type: ConnectorAuthType = ConnectorAuthType.BEARER
    status: ConnectorStatus = ConnectorStatus.ENABLED
    health_status: ConnectorHealthStatus = ConnectorHealthStatus.UNKNOWN
    allowed_project_ids: list[str] = Field(default_factory=list)
    allowed_repositories: list[str] = Field(default_factory=list)
    allowed_actions: list[AgentAction] = Field(default_factory=list)
    rate_limit_per_minute: int = 60
    secret_ref: str | None = None
    last_used_at: datetime | None = None
    last_health_check_at: datetime | None = None
    failed_auth_count: int = 0
    last_error: str | None = None
    created_by_user_id: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class ConnectorPublic(BaseModel):
    id: str
    name: str
    connector_type: ConnectorType
    provider_label: ConnectorProviderLabel
    endpoint_url: str | None = None
    callback_url: str | None = None
    auth_type: ConnectorAuthType
    status: ConnectorStatus
    health_status: ConnectorHealthStatus
    allowed_project_ids: list[str] = Field(default_factory=list)
    allowed_repositories: list[str] = Field(default_factory=list)
    allowed_actions: list[AgentAction] = Field(default_factory=list)
    rate_limit_per_minute: int
    secret_configured: bool = False
    last_used_at: datetime | None = None
    last_health_check_at: datetime | None = None
    failed_auth_count: int = 0
    last_error: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
