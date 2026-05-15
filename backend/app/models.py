from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"
    BLOCKED = "BLOCKED"


class ApprovalStatus(StrEnum):
    REQUESTED = "REQUESTED"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"
    EXECUTING = "EXECUTING"
    READY_FOR_PR = "READY_FOR_PR"
    FAILED = "FAILED"
    MERGED = "MERGED"
    CANCELLED = "CANCELLED"

    PENDING_REVIEW = "PENDING_REVIEW"
    REJECTED = "REJECTED"
    REVISION_REQUESTED = "REVISION_REQUESTED"
    BLOCKED = "BLOCKED"
    APPLIED = "APPLIED"
    TESTS_RUNNING = "TESTS_RUNNING"
    TESTS_PASSED = "TESTS_PASSED"
    TESTS_FAILED = "TESTS_FAILED"


class MCPPendingStatus(StrEnum):
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    FORWARDING = "FORWARDING"
    FORWARDED = "FORWARDED"
    BLOCKED_BY_DECISION = "BLOCKED_BY_DECISION"
    ORPHANED = "ORPHANED"
    DEAD_LETTER = "DEAD_LETTER"


class ProjectMode(StrEnum):
    STRICT = "STRICT"
    MANUAL = "MANUAL"
    ASSISTED = "ASSISTED"
    LOW_RISK_AUTO = "LOW_RISK_AUTO"
    LOCKDOWN = "LOCKDOWN"


class NotificationStatus(StrEnum):
    UNREAD = "UNREAD"
    READ = "READ"


class UserRole(StrEnum):
    OWNER = "OWNER"
    MAINTAINER = "MAINTAINER"
    REVIEWER = "REVIEWER"


class InviteStatus(StrEnum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class AgentProvider(StrEnum):
    CODEX = "CODEX"
    CHATGPT = "CHATGPT"
    CLAUDE = "CLAUDE"
    CURSOR = "CURSOR"
    GITHUB_ACTIONS = "GITHUB_ACTIONS"
    MANUAL = "MANUAL"
    OTHER = "OTHER"


class AgentAuthType(StrEnum):
    API_KEY = "API_KEY"
    WEBHOOK_SECRET = "WEBHOOK_SECRET"
    MANUAL = "MANUAL"


class AgentAction(StrEnum):
    CREATE_APPROVAL = "CREATE_APPROVAL"
    CREATE_WORK_ORDER = "CREATE_WORK_ORDER"
    CREATE_AGENT_TASK = "CREATE_AGENT_TASK"
    APPEND_AGENT_TASK_EVENT = "APPEND_AGENT_TASK_EVENT"
    READ_AGENT_TASK = "READ_AGENT_TASK"
    EXECUTE_GITHUB = "EXECUTE_GITHUB"


class User(BaseModel):
    id: str = Field(default_factory=lambda: new_id("user"))
    email: str
    display_name: str = ""
    role: UserRole = UserRole.OWNER
    password_hash: str
    password_salt: str
    disabled: bool = False
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class SessionToken(BaseModel):
    id: str = Field(default_factory=lambda: new_id("session"))
    user_id: str
    token_hash: str
    created_at: datetime = Field(default_factory=now_utc)
    expires_at: datetime
    revoked: bool = False


class Invite(BaseModel):
    id: str = Field(default_factory=lambda: new_id("invite"))
    email: str
    role: UserRole = UserRole.REVIEWER
    token_hash: str
    status: InviteStatus = InviteStatus.PENDING
    expires_at: datetime
    created_by_user_id: str
    accepted_by_user_id: str | None = None
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class AuthUser(BaseModel):
    id: str
    email: str
    display_name: str
    role: UserRole


class AgentCreate(BaseModel):
    provider: AgentProvider
    display_name: str
    auth_type: AgentAuthType = AgentAuthType.API_KEY
    allowed_project_ids: list[str] = Field(default_factory=list)
    allowed_repositories: list[str] = Field(default_factory=list)
    allowed_actions: list[AgentAction] = Field(default_factory=lambda: [AgentAction.CREATE_APPROVAL])
    enabled: bool = True


class ExternalAgent(BaseModel):
    id: str = Field(default_factory=lambda: new_id("agent"))
    provider: AgentProvider
    display_name: str
    auth_type: AgentAuthType = AgentAuthType.API_KEY
    allowed_project_ids: list[str] = Field(default_factory=list)
    allowed_repositories: list[str] = Field(default_factory=list)
    allowed_actions: list[AgentAction] = Field(default_factory=list)
    created_by_user_id: str | None = None
    enabled: bool = True
    secret_hash: str | None = None
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class ExternalAgentPublic(BaseModel):
    id: str
    provider: AgentProvider
    display_name: str
    auth_type: AgentAuthType
    allowed_project_ids: list[str] = Field(default_factory=list)
    allowed_repositories: list[str] = Field(default_factory=list)
    allowed_actions: list[AgentAction] = Field(default_factory=list)
    created_by_user_id: str | None = None
    enabled: bool
    created_at: datetime
    updated_at: datetime


class AgentRegistrationResponse(BaseModel):
    agent: ExternalAgentPublic
    agent_token: str | None = None


class ProjectCreate(BaseModel):
    name: str
    description: str = ""
    mode: ProjectMode = ProjectMode.STRICT
    rules: list[str] = Field(default_factory=list)


class Project(ProjectCreate):
    id: str = Field(default_factory=lambda: new_id("project"))
    created_at: datetime = Field(default_factory=now_utc)


class MCPToolDefinition(BaseModel):
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    mutating: bool = False


class MCPChildServerCreate(BaseModel):
    project_id: str
    name: str
    description: str = ""
    transport: str = "test"
    endpoint: str = ""
    enabled: bool = True
    tools: list[MCPToolDefinition] = Field(default_factory=list)


class MCPChildServer(MCPChildServerCreate):
    id: str = Field(default_factory=lambda: new_id("mcp_server"))
    created_at: datetime = Field(default_factory=now_utc)


class MCPCallRequest(BaseModel):
    project_id: str
    server_id: str | None = None
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    agent_name: str = "mcp-client"
    session_id: str | None = None
    source_url: str | None = None


class MCPCallResponse(BaseModel):
    status: str
    pending_request_id: str | None = None
    approval_request_id: str | None = None
    result: dict[str, Any] | None = None


class MCPApprovalDecision(BaseModel):
    approval_request_id: str
    status: ApprovalStatus


class MCPPendingRequest(BaseModel):
    id: str = Field(default_factory=lambda: new_id("mcp_pending"))
    project_id: str
    server_id: str | None = None
    approval_request_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    agent_name: str
    session_id: str | None = None
    source_url: str | None = None
    status: MCPPendingStatus = MCPPendingStatus.WAITING_FOR_APPROVAL
    attempts: int = 0
    max_attempts: int = 3
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    last_error: str | None = None
    dead_letter_reason: str | None = None
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class MCPPendingSummary(BaseModel):
    pending: int
    forwarding: int
    dead_letter: int
    orphaned: int


class MCPRecoveryRequest(BaseModel):
    reason: str = "operator-triggered"
    lease_owner: str = "operator"
    lease_seconds: int = 60


class IdeaCreate(BaseModel):
    project_id: str | None = None
    title: str
    raw_text: str


class Idea(IdeaCreate):
    id: str = Field(default_factory=lambda: new_id("idea"))
    created_at: datetime = Field(default_factory=now_utc)


class WorkOrderCreate(BaseModel):
    project_id: str
    idea_id: str | None = None
    goal: str
    context: str = ""
    tasks: list[str] = Field(default_factory=list)
    affected_areas: list[str] = Field(default_factory=list)
    required_tests: list[str] = Field(default_factory=list)
    rollback_plan: str


class WorkOrder(WorkOrderCreate):
    id: str = Field(default_factory=lambda: new_id("work_order"))
    status: str = "DRAFT"
    risk_level: RiskLevel = RiskLevel.LOW
    created_at: datetime = Field(default_factory=now_utc)


class FileChange(BaseModel):
    path: str
    change_type: str
    diff: str
    new_content: str | None = None


class RiskAssessment(BaseModel):
    level: RiskLevel
    blocked: bool = False
    reasons: list[str] = Field(default_factory=list)


class ApprovalRequestCreate(BaseModel):
    project_id: str
    work_order_id: str | None = None
    title: str
    summary: str
    agent_name: str
    target_branch: str
    files: list[FileChange] = Field(default_factory=list)
    test_plan: list[str] = Field(default_factory=list)
    rollback_plan: str
    source_provider: AgentProvider | None = None
    source_agent_id: str | None = None
    source_agent_name: str | None = None
    source_session_id: str | None = None
    source_task_url: str | None = None

    @field_validator("files")
    @classmethod
    def require_file_paths(cls, files: list[FileChange]) -> list[FileChange]:
        for file in files:
            if not file.path.strip():
                raise ValueError("File path cannot be empty")
        return files


class ApprovalRequest(ApprovalRequestCreate):
    id: str = Field(default_factory=lambda: new_id("approval"))
    status: ApprovalStatus = ApprovalStatus.REQUESTED
    risk: RiskAssessment
    approved_payload_hash: str | None = None
    created_by_user_id: str | None = None
    verified_source: bool = False
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class DecisionCreate(BaseModel):
    status: ApprovalStatus
    decided_by: str = "operator"
    notes: str = ""


class TestRunCreate(BaseModel):
    approval_request_id: str
    command: str
    status: str
    output_summary: str


class TestRun(TestRunCreate):
    id: str = Field(default_factory=lambda: new_id("test"))
    created_at: datetime = Field(default_factory=now_utc)


class Notification(BaseModel):
    id: str = Field(default_factory=lambda: new_id("notification"))
    title: str
    body: str
    entity_type: str
    entity_id: str
    status: NotificationStatus = NotificationStatus.UNREAD
    user_id: str | None = None
    channel: str = "IN_APP"
    push_status: str = "PENDING"
    created_at: datetime = Field(default_factory=now_utc)
    read_at: datetime | None = None


class DeviceRegistration(BaseModel):
    id: str = Field(default_factory=lambda: new_id("device"))
    user_id: str | None = None
    platform: str = "ANDROID"
    fcm_token_hash: str
    fcm_token_preview: str
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class ApprovalGroups(BaseModel):
    action_required: list[ApprovalRequest]
    ready_for_pr: list[ApprovalRequest]
    completed: list[ApprovalRequest]
    blocked: list[ApprovalRequest]


class AuditEvent(BaseModel):
    id: str = Field(default_factory=lambda: new_id("audit"))
    event_type: str
    entity_id: str
    actor: str
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now_utc)
