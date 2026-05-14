from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from .models import AgentProvider, RiskLevel, new_id, now_utc


class AgentTaskStatus(StrEnum):
    RECEIVED = "RECEIVED"
    PLANNING = "PLANNING"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    EXECUTING = "EXECUTING"
    TESTING = "TESTING"
    READY_FOR_PR = "READY_FOR_PR"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    DONE = "DONE"


class AgentTaskRequestedAction(StrEnum):
    CREATE_WORK_ORDER = "CREATE_WORK_ORDER"
    CREATE_APPROVAL = "CREATE_APPROVAL"
    REVIEW_ARCHITECTURE = "REVIEW_ARCHITECTURE"
    GENERATE_DOCS = "GENERATE_DOCS"
    OTHER = "OTHER"


class AgentTaskEventType(StrEnum):
    TASK_CREATED = "TASK_CREATED"
    PLAN_RECEIVED = "PLAN_RECEIVED"
    APPROVAL_CREATED = "APPROVAL_CREATED"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    EXECUTION_STARTED = "EXECUTION_STARTED"
    TESTS_STARTED = "TESTS_STARTED"
    TESTS_PASSED = "TESTS_PASSED"
    TESTS_FAILED = "TESTS_FAILED"
    PR_CREATED = "PR_CREATED"
    RESULT_READY = "RESULT_READY"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    DONE = "DONE"
    NOTE = "NOTE"


class AgentTaskCreate(BaseModel):
    provider: AgentProvider = AgentProvider.MANUAL
    project_id: str | None = None
    title: str
    goal: str
    context: str = ""
    source_url: str | None = None
    source_session_id: str | None = None
    source_task_id: str | None = None
    requested_action: AgentTaskRequestedAction = AgentTaskRequestedAction.CREATE_WORK_ORDER
    repository: str | None = None
    branch_preference: str | None = None
    risk_hint: RiskLevel | None = None
    requires_approval: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentTask(AgentTaskCreate):
    id: str = Field(default_factory=lambda: new_id("agent_task"))
    status: AgentTaskStatus = AgentTaskStatus.RECEIVED
    external_agent_id: str | None = None
    external_agent_name: str | None = None
    approval_request_id: str | None = None
    created_by_user_id: str | None = None
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class AgentTaskEventCreate(BaseModel):
    event_type: AgentTaskEventType = AgentTaskEventType.NOTE
    message: str = ""
    status: AgentTaskStatus | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentTaskEvent(AgentTaskEventCreate):
    id: str = Field(default_factory=lambda: new_id("agent_task_event"))
    task_id: str
    actor: str = "system"
    created_at: datetime = Field(default_factory=now_utc)
