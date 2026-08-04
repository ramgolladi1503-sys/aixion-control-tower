from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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


class AdapterKind(StrEnum):
    CODEX_APP_SERVER = "CODEX_APP_SERVER"
    CLAUDE_AGENT_SDK = "CLAUDE_AGENT_SDK"
    ANTIGRAVITY_SDK = "ANTIGRAVITY_SDK"
    ANTIGRAVITY_HOOKS = "ANTIGRAVITY_HOOKS"
    OPENCLAW_GATEWAY = "OPENCLAW_GATEWAY"
    PROCESS_JSONL = "PROCESS_JSONL"
    MCP = "MCP"
    GENERIC = "GENERIC"


class AdapterFeature(StrEnum):
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


class EventType(StrEnum):
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


class CommandType(StrEnum):
    START_SESSION = "START_SESSION"
    SEND_MESSAGE = "SEND_MESSAGE"
    RESUME_SESSION = "RESUME_SESSION"
    PAUSE_SESSION = "PAUSE_SESSION"
    CANCEL_SESSION = "CANCEL_SESSION"
    SYNC_SESSION = "SYNC_SESSION"
    SHUTDOWN_RELAY = "SHUTDOWN_RELAY"


class CapabilityActionType(StrEnum):
    READ_REPOSITORY = "READ_REPOSITORY"
    CREATE_BRANCH = "CREATE_BRANCH"
    MODIFY_FILES = "MODIFY_FILES"
    RUN_COMMAND = "RUN_COMMAND"
    ACCESS_NETWORK = "ACCESS_NETWORK"
    CREATE_PULL_REQUEST = "CREATE_PULL_REQUEST"
    DEPLOY = "DEPLOY"
    READ_SECRET = "READ_SECRET"
    WRITE_DATABASE = "WRITE_DATABASE"
    CUSTOM = "CUSTOM"


class PolicyDecision(StrEnum):
    ALLOW = "ALLOW"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    BLOCK = "BLOCK"


class AdapterManifest(BaseModel):
    adapter_id: str
    provider: RelayProvider
    adapter_kind: AdapterKind
    display_name: str
    version: str = "unknown"
    executable: str | None = None
    available: bool = True
    features: list[AdapterFeature] = Field(default_factory=list)
    external_agent_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RelayCommand(BaseModel):
    id: str
    relay_id: str
    session_id: str | None = None
    command_type: CommandType
    status: str
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str
    attempt_count: int = 0
    max_attempts: int = 3
    lease_owner: str | None = None
    lease_token: str | None = None
    lease_expires_at: datetime | None = None


class NormalizedEvent(BaseModel):
    event_type: EventType
    message: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    remote_session_id: str | None = None
    remote_turn_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class ActionProposal(BaseModel):
    action_type: CapabilityActionType
    lease_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    project_id: str | None = None
    repository: str | None = None
    branch: str | None = None
    paths: list[str] = Field(default_factory=list)
    command: str | None = None
    network_domains: list[str] = Field(default_factory=list)
    estimated_runtime_seconds: int = 0
    estimated_cost_usd: float = 0.0
    retry_number: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class ActionDecision(BaseModel):
    action_id: str
    decision: PolicyDecision
    reasons: list[str] = Field(default_factory=list)
    obligations: list[str] = Field(default_factory=list)


class SessionStartRequest(BaseModel):
    session_id: str
    objective: str
    workspace_path: str
    repository: str | None = None
    approval_mode: str = "STRICT"
    model: str | None = None
    max_runtime_seconds: int = 3600
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionResult(BaseModel):
    success: bool
    remote_session_id: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
