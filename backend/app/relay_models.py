from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from .models import new_id, now_utc
from .trust_models import PolicyDecision, ProposedAction


class RelayProvider(StrEnum):
    CODEX = "CODEX"
    CHATGPT = "CHATGPT"
    CLAUDE = "CLAUDE"
    ANTIGRAVITY = "ANTIGRAVITY"
    OPENCLAW = "OPENCLAW"
    GEMINI = "GEMINI"
    CURSOR = "CURSOR"
    COPILOT = "COPILOT"
    AIDER = "AIDER"
    CLINE = "CLINE"
    CONTINUE = "CONTINUE"
    WINDSURF = "WINDSURF"
    GITHUB_ACTIONS = "GITHUB_ACTIONS"
    CUSTOM = "CUSTOM"


class RelayPlatform(StrEnum):
    MACOS = "MACOS"
    LINUX = "LINUX"
    WINDOWS = "WINDOWS"
    OTHER = "OTHER"


class RelayStatus(StrEnum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    DEGRADED = "DEGRADED"
    DISABLED = "DISABLED"


class RelayAdapterKind(StrEnum):
    CODEX_APP_SERVER = "CODEX_APP_SERVER"
    CLAUDE_AGENT_SDK = "CLAUDE_AGENT_SDK"
    ANTIGRAVITY_SDK = "ANTIGRAVITY_SDK"
    ANTIGRAVITY_HOOKS = "ANTIGRAVITY_HOOKS"
    OPENCLAW_GATEWAY = "OPENCLAW_GATEWAY"
    PROCESS_JSONL = "PROCESS_JSONL"
    MCP = "MCP"
    GENERIC = "GENERIC"


class RelayAdapterFeature(StrEnum):
    STRUCTURED_EVENTS = "STRUCTURED_EVENTS"
    RESUME = "RESUME"
    STEER = "STEER"
    CANCEL = "CANCEL"
    NATIVE_APPROVALS = "NATIVE_APPROVALS"
    MCP = "MCP"
    SUBAGENTS = "SUBAGENTS"
    FILE_DIFFS = "FILE_DIFFS"
    TEST_RESULTS = "TEST_RESULTS"
    TOKEN_USAGE = "TOKEN_USAGE"
    COST_USAGE = "COST_USAGE"


class RelaySessionStatus(StrEnum):
    QUEUED = "QUEUED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class RelaySessionOrigin(StrEnum):
    HOST_STARTED = "HOST_STARTED"
    MOBILE_STARTED = "MOBILE_STARTED"


class RelayApprovalSource(StrEnum):
    MAC = "MAC"
    ANDROID = "ANDROID"
    POLICY = "POLICY"
    SYSTEM = "SYSTEM"


class RelayCommandType(StrEnum):
    START_SESSION = "START_SESSION"
    SEND_MESSAGE = "SEND_MESSAGE"
    RESUME_SESSION = "RESUME_SESSION"
    PAUSE_SESSION = "PAUSE_SESSION"
    CANCEL_SESSION = "CANCEL_SESSION"
    SYNC_SESSION = "SYNC_SESSION"
    SHUTDOWN_RELAY = "SHUTDOWN_RELAY"


class RelayCommandStatus(StrEnum):
    PENDING = "PENDING"
    LEASED = "LEASED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class RelayEventType(StrEnum):
    SESSION_STARTING = "SESSION_STARTING"
    SESSION_STARTED = "SESSION_STARTED"
    SESSION_RESUMED = "SESSION_RESUMED"
    AGENT_MESSAGE = "AGENT_MESSAGE"
    USER_MESSAGE = "USER_MESSAGE"
    PLAN_UPDATED = "PLAN_UPDATED"
    REASONING_SUMMARY = "REASONING_SUMMARY"
    TOOL_PROPOSED = "TOOL_PROPOSED"
    TOOL_STARTED = "TOOL_STARTED"
    TOOL_OUTPUT = "TOOL_OUTPUT"
    FILE_CHANGE_PROPOSED = "FILE_CHANGE_PROPOSED"
    FILE_CHANGED = "FILE_CHANGED"
    COMMAND_PROPOSED = "COMMAND_PROPOSED"
    COMMAND_STARTED = "COMMAND_STARTED"
    COMMAND_OUTPUT = "COMMAND_OUTPUT"
    TEST_RESULT = "TEST_RESULT"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_RESOLVED = "APPROVAL_RESOLVED"
    USAGE = "USAGE"
    HEARTBEAT = "HEARTBEAT"
    SESSION_PAUSED = "SESSION_PAUSED"
    SESSION_COMPLETED = "SESSION_COMPLETED"
    SESSION_FAILED = "SESSION_FAILED"
    SESSION_CANCELLED = "SESSION_CANCELLED"
    RAW = "RAW"


class RelayAdapterManifest(BaseModel):
    adapter_id: str = Field(min_length=1, max_length=120)
    provider: RelayProvider
    adapter_kind: RelayAdapterKind
    display_name: str = Field(min_length=1, max_length=120)
    version: str = Field(default="unknown", max_length=120)
    executable: str | None = Field(default=None, max_length=500)
    available: bool = True
    features: list[RelayAdapterFeature] = Field(default_factory=list, max_length=32)
    external_agent_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("features")
    @classmethod
    def deduplicate_features(
        cls,
        values: list[RelayAdapterFeature],
    ) -> list[RelayAdapterFeature]:
        return list(dict.fromkeys(values))


class RelayRegistrationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    platform: RelayPlatform
    hostname: str = Field(min_length=1, max_length=255)
    machine_fingerprint: str = Field(min_length=16, max_length=256)
    workspace_roots: list[str] = Field(min_length=1, max_length=32)
    allowed_project_ids: list[str] = Field(default_factory=list, max_length=100)
    allowed_repositories: list[str] = Field(default_factory=list, max_length=100)
    adapters: list[RelayAdapterManifest] = Field(default_factory=list, max_length=64)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("workspace_roots")
    @classmethod
    def validate_workspace_roots(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw in values:
            value = raw.strip().rstrip("/") or "/"
            if not value.startswith("/") and not (
                len(value) >= 3 and value[1:3] in {":\\", ":/"}
            ):
                raise ValueError("workspace roots must be absolute paths")
            if value not in normalized:
                normalized.append(value)
        return normalized

    @field_validator("allowed_repositories")
    @classmethod
    def validate_repositories(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw in values:
            value = raw.strip()
            if not value:
                continue
            if "/" not in value or value.startswith("/") or value.endswith("/"):
                raise ValueError("allowed repositories must use owner/name format")
            if value not in normalized:
                normalized.append(value)
        return normalized


class RelayHost(BaseModel):
    id: str = Field(default_factory=lambda: new_id("relay"))
    name: str
    platform: RelayPlatform
    hostname: str
    machine_fingerprint: str
    token_hash: str
    status: RelayStatus = RelayStatus.OFFLINE
    workspace_roots: list[str] = Field(default_factory=list)
    allowed_project_ids: list[str] = Field(default_factory=list)
    allowed_repositories: list[str] = Field(default_factory=list)
    adapters: list[RelayAdapterManifest] = Field(default_factory=list)
    active_session_count: int = 0
    relay_version: str = "unknown"
    last_heartbeat_at: datetime | None = None
    disabled_at: datetime | None = None
    disabled_reason: str = ""
    created_by_user_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class RelayHostPublic(BaseModel):
    id: str
    name: str
    platform: RelayPlatform
    hostname: str
    machine_fingerprint: str
    status: RelayStatus
    workspace_roots: list[str]
    allowed_project_ids: list[str]
    allowed_repositories: list[str]
    adapters: list[RelayAdapterManifest]
    active_session_count: int
    relay_version: str
    last_heartbeat_at: datetime | None = None
    disabled_at: datetime | None = None
    disabled_reason: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class RelayRegistrationResponse(BaseModel):
    relay: RelayHostPublic
    relay_token: str


class RelayHeartbeatRequest(BaseModel):
    relay_version: str = Field(default="unknown", max_length=120)
    adapters: list[RelayAdapterManifest] = Field(default_factory=list, max_length=64)
    active_session_count: int = Field(default=0, ge=0, le=1000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RelaySessionCreate(BaseModel):
    relay_id: str
    provider: RelayProvider
    adapter_id: str = Field(min_length=1, max_length=120)
    objective: str = Field(min_length=1, max_length=20000)
    workspace_path: str = Field(min_length=1, max_length=2000)
    repository: str | None = Field(default=None, max_length=500)
    project_id: str | None = None
    task_id: str | None = None
    run_id: str | None = None
    approval_mode: str = Field(default="STRICT", max_length=40)
    model: str | None = Field(default=None, max_length=200)
    max_runtime_seconds: int = Field(default=3600, ge=30, le=86400)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("workspace_path")
    @classmethod
    def validate_workspace_path(cls, value: str) -> str:
        cleaned = value.strip().rstrip("/") or "/"
        if not cleaned.startswith("/") and not (
            len(cleaned) >= 3 and cleaned[1:3] in {":\\", ":/"}
        ):
            raise ValueError("workspace_path must be absolute")
        return cleaned

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if "/" not in cleaned or cleaned.startswith("/") or cleaned.endswith("/"):
            raise ValueError("repository must use owner/name format")
        return cleaned


class RelayLocalSessionCreate(BaseModel):
    provider: RelayProvider
    adapter_id: str = Field(min_length=1, max_length=120)
    workspace_path: str = Field(min_length=1, max_length=2000)
    repository: str | None = Field(default=None, max_length=500)
    project_id: str | None = None
    task_id: str | None = None
    run_id: str | None = None
    approval_mode: str = Field(default="STRICT", max_length=40)
    model: str | None = Field(default=None, max_length=200)
    max_runtime_seconds: int = Field(default=3600, ge=30, le=86400)
    host_process_id: int | None = Field(default=None, ge=1)
    provider_process_id: int | None = Field(default=None, ge=1)
    provider_thread_id: str | None = Field(default=None, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=300)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("workspace_path")
    @classmethod
    def validate_workspace_path(cls, value: str) -> str:
        return RelaySessionCreate.validate_workspace_path(value)

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str | None) -> str | None:
        return RelaySessionCreate.validate_repository(value)


class RelaySession(BaseModel):
    id: str = Field(default_factory=lambda: new_id("relay_session"))
    relay_id: str
    provider: RelayProvider
    adapter_id: str
    objective: str
    origin: RelaySessionOrigin = RelaySessionOrigin.MOBILE_STARTED
    workspace_path: str
    repository: str | None = None
    project_id: str | None = None
    task_id: str | None = None
    run_id: str | None = None
    approval_mode: str = "STRICT"
    model: str | None = None
    status: RelaySessionStatus = RelaySessionStatus.QUEUED
    remote_session_id: str | None = None
    current_turn_id: str | None = None
    latest_event_sequence: int = 0
    final_evidence_hash: str | None = None
    last_error: str | None = None
    max_runtime_seconds: int = 3600
    started_at: datetime | None = None
    completed_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class RelaySessionMessageCreate(BaseModel):
    message: str = Field(min_length=1, max_length=20000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RelaySessionControlRequest(BaseModel):
    reason: str = Field(default="", max_length=1000)


class RelayCommand(BaseModel):
    id: str = Field(default_factory=lambda: new_id("relay_command"))
    relay_id: str
    session_id: str | None = None
    command_type: RelayCommandType
    status: RelayCommandStatus = RelayCommandStatus.PENDING
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str
    attempt_count: int = 0
    max_attempts: int = Field(default=3, ge=1, le=10)
    lease_owner: str | None = None
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    acknowledged_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class RelayCommandClaimRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=200)
    lease_seconds: int = Field(default=120, ge=15, le=3600)


class RelayCommandClaimResponse(BaseModel):
    command: RelayCommand | None = None


class RelayCommandLeaseRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=200)
    lease_token: str = Field(min_length=16, max_length=500)
    lease_seconds: int = Field(default=120, ge=15, le=3600)


class RelayCommandAckRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=200)
    lease_token: str = Field(min_length=16, max_length=500)


class RelayCommandResultRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=200)
    lease_token: str = Field(min_length=16, max_length=500)
    success: bool
    remote_session_id: str | None = Field(default=None, max_length=500)
    error: str | None = Field(default=None, max_length=4000)
    result: dict[str, Any] = Field(default_factory=dict)


class RelayEventCreate(BaseModel):
    event_id: str = Field(min_length=1, max_length=300)
    sequence: int = Field(ge=1)
    event_type: RelayEventType
    message: str = Field(default="", max_length=20000)
    payload: dict[str, Any] = Field(default_factory=dict)
    remote_session_id: str | None = Field(default=None, max_length=500)
    remote_turn_id: str | None = Field(default=None, max_length=500)
    created_at: datetime = Field(default_factory=now_utc)

    @model_validator(mode="after")
    def validate_payload_size(self) -> RelayEventCreate:
        serialized = self.model_dump_json()
        if len(serialized.encode("utf-8")) > 262_144:
            raise ValueError("relay event payload exceeds 256 KiB")
        return self


class RelayEvent(BaseModel):
    id: str = Field(default_factory=lambda: new_id("relay_event"))
    relay_id: str
    session_id: str
    provider: RelayProvider
    adapter_id: str
    event_id: str
    sequence: int
    event_type: RelayEventType
    message: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    remote_session_id: str | None = None
    remote_turn_id: str | None = None
    previous_hash: str | None = None
    event_hash: str
    created_at: datetime = Field(default_factory=now_utc)
    received_at: datetime = Field(default_factory=now_utc)


class RelaySessionDetail(BaseModel):
    session: RelaySession
    relay: RelayHostPublic
    commands: list[RelayCommand] = Field(default_factory=list)
    events: list[RelayEvent] = Field(default_factory=list)


class RelayActionEvaluationRequest(BaseModel):
    action: ProposedAction


class RelayActionStatus(BaseModel):
    action: ProposedAction
    decision: PolicyDecision


class RelaySummary(BaseModel):
    total_relays: int = 0
    online_relays: int = 0
    offline_relays: int = 0
    degraded_relays: int = 0
    total_sessions: int = 0
    active_sessions: int = 0
    waiting_for_approval: int = 0
    failed_sessions: int = 0
    pending_commands: int = 0
    leased_commands: int = 0
