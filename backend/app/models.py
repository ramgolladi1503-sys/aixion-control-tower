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


class WorkOrderSourceType(StrEnum):
    MANUAL = "MANUAL"
    AGENT_TASK = "AGENT_TASK"
    CONNECTOR = "CONNECTOR"
    MCP = "MCP"


class User(BaseModel):
    id: str = Field(default_factory=lambda: new_id("user"))
    email: str
    display_name: str = ""
    role: UserRole = UserRole.OWNER
    password_hash: str
    password_salt: str
    disabled: bool = False
    email_verified: bool = False
    email_verification_hash: str | None = None
    email_verification_salt: str | None = None
    email_verification_expires_at: datetime | None = None
    email_verification_sent_at: datetime | None = None
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
    email_verified: bool = False


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
    updated_at: datetime = Field(default_factory=now_utc)


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
    rollback_plan: str = ""


class AgentWorkOrderCreate(WorkOrderCreate):
    source_task_id: str | None = None
    source_session_id: str | None = None


class WorkOrder(WorkOrderCreate):
    id: str = Field(default_factory=lambda: new_id("work"))
    risk_level: RiskLevel = RiskLevel.MEDIUM
    source_type: WorkOrderSourceType = WorkOrderSourceType.MANUAL
    source_provider: AgentProvider = AgentProvider.MANUAL
    source_agent_id: str | None = None
    source_agent_name: str | None = None
    source_task_id: str | None = None
    source_session_id: str | None = None
    created_by_user_id: str | None = None
    verified_source: bool = False
    created_at: datetime = Field(default_factory=now_utc)

    @field_validator("source_provider", mode="before")
    @classmethod
    def default_manual_source_provider(cls, value: AgentProvider | str | None) -> AgentProvider | str:
        return AgentProvider.MANUAL if value is None else value


class FileChange(BaseModel):
    path: str
    change_type: str = "update"
    diff: str
    new_content: str | None = None


class ApprovalRequestCreate(BaseModel):
    project_id: str
    work_order_id: str | None = None
    title: str
    summary: str
    agent_name: str = "manual-agent"
    target_branch: str
    files: list[FileChange]
    test_plan: list[str] = Field(default_factory=list)
    rollback_plan: str = ""
    source_provider: AgentProvider | None = None
    source_agent_id: str | None = None
    source_agent_name: str | None = None
    source_session_id: str | None = None
    source_task_url: str | None = None


class RiskAssessment(BaseModel):
    level: RiskLevel
    reasons: list[str] = Field(default_factory=list)
    blocked: bool = False
    required_actions: list[str] = Field(default_factory=list)


class ApprovalRequest(ApprovalRequestCreate):
    id: str = Field(default_factory=lambda: new_id("approval"))
    status: ApprovalStatus = ApprovalStatus.REQUESTED
    risk: RiskAssessment
    source_provider: AgentProvider = AgentProvider.MANUAL
    source_agent_id: str | None = None
    source_agent_name: str | None = None
    source_session_id: str | None = None
    source_task_url: str | None = None
    created_by_user_id: str | None = None
    verified_source: bool = False
    approved_payload_hash: str | None = None
    approved_at: datetime | None = None
    approved_by_user_id: str | None = None
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)

    @field_validator("source_provider", mode="before")
    @classmethod
    def default_manual_source_provider(cls, value: AgentProvider | str | None) -> AgentProvider | str:
        return AgentProvider.MANUAL if value is None else value


class MCPPendingRequest(BaseModel):
    id: str = Field(default_factory=lambda: new_id("mcp_pending"))
    project_id: str
    approval_request_id: str
    server_name: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = None
    requested_by: str = "mcp-client"
    status: MCPPendingStatus = MCPPendingStatus.WAITING_FOR_APPROVAL
    attempts: int = 0
    max_attempts: int = 3
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class DecisionCreate(BaseModel):
    decision: str
    reason: str = ""


class ApprovalGroups(BaseModel):
    action_required: list[ApprovalRequest] = Field(default_factory=list)
    approved_waiting: list[ApprovalRequest] = Field(default_factory=list)
    executing: list[ApprovalRequest] = Field(default_factory=list)
    ready_for_pr: list[ApprovalRequest] = Field(default_factory=list)
    failed: list[ApprovalRequest] = Field(default_factory=list)
    completed: list[ApprovalRequest] = Field(default_factory=list)
    history: list[ApprovalRequest] = Field(default_factory=list)


class TestRunCreate(BaseModel):
    approval_request_id: str
    command: str
    status: str
    output_summary: str = ""


class TestRun(TestRunCreate):
    id: str = Field(default_factory=lambda: new_id("test"))
    created_at: datetime = Field(default_factory=now_utc)


class Notification(BaseModel):
    id: str = Field(default_factory=lambda: new_id("notification"))
    title: str
    body: str
    entity_type: str
    entity_id: str
    user_id: str | None = None
    status: NotificationStatus = NotificationStatus.UNREAD
    push_status: str = "PENDING"
    push_error: str | None = None
    created_at: datetime = Field(default_factory=now_utc)


class DeviceRegistration(BaseModel):
    id: str = Field(default_factory=lambda: new_id("device"))
    user_id: str | None = None
    platform: str = "android"
    token: str
    app_version: str = ""
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class AuditEvent(BaseModel):
    id: str = Field(default_factory=lambda: new_id("audit"))
    event_type: str
    actor: str = "system"
    entity_id: str
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now_utc)
