from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


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
    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    REVISION_REQUESTED = "REVISION_REQUESTED"
    BLOCKED = "BLOCKED"
    APPLIED = "APPLIED"
    TESTS_RUNNING = "TESTS_RUNNING"
    TESTS_PASSED = "TESTS_PASSED"
    TESTS_FAILED = "TESTS_FAILED"
    READY_FOR_PR = "READY_FOR_PR"


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


class AuthUser(BaseModel):
    id: str
    email: str
    display_name: str
    role: UserRole


class ProjectCreate(BaseModel):
    name: str
    description: str = ""
    mode: ProjectMode = ProjectMode.STRICT
    rules: list[str] = Field(default_factory=list)


class Project(ProjectCreate):
    id: str = Field(default_factory=lambda: new_id("project"))
    created_at: datetime = Field(default_factory=now_utc)


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


class WorkOrder(WorkOrderCreate):
    id: str = Field(default_factory=lambda: new_id("work"))
    risk_level: RiskLevel = RiskLevel.MEDIUM
    created_at: datetime = Field(default_factory=now_utc)


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


class RiskAssessment(BaseModel):
    level: RiskLevel
    reasons: list[str] = Field(default_factory=list)
    blocked: bool = False
    required_actions: list[str] = Field(default_factory=list)


class ApprovalRequest(ApprovalRequestCreate):
    id: str = Field(default_factory=lambda: new_id("approval"))
    status: ApprovalStatus = ApprovalStatus.PENDING_REVIEW
    risk: RiskAssessment
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class DecisionCreate(BaseModel):
    decision: str
    reason: str = ""


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
