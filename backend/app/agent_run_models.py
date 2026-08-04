from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from .models import new_id, now_utc


class AgentRunStatus(StrEnum):
    SCHEDULED = "SCHEDULED"
    RUNNING = "RUNNING"
    EVALUATING = "EVALUATING"
    RETRY_WAIT = "RETRY_WAIT"
    PAUSED = "PAUSED"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    BLOCKED = "BLOCKED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AgentRunStepType(StrEnum):
    VALIDATE_SCOPE = "VALIDATE_SCOPE"
    CREATE_BRANCH = "CREATE_BRANCH"
    APPLY_CHANGES = "APPLY_CHANGES"
    RUN_VALIDATION = "RUN_VALIDATION"
    CREATE_PULL_REQUEST = "CREATE_PULL_REQUEST"
    VERIFY_RESULT = "VERIFY_RESULT"
    SEAL_EVIDENCE = "SEAL_EVIDENCE"


class AgentRunStepStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    PAUSED = "PAUSED"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    BLOCKED = "BLOCKED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"


class VerificationDecision(StrEnum):
    PASS = "PASS"
    RETRY_TRANSIENT = "RETRY_TRANSIENT"
    NEEDS_REVISION = "NEEDS_REVISION"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    BLOCK_POLICY = "BLOCK_POLICY"
    FAIL_PERMANENT = "FAIL_PERMANENT"


class AgentRunEventType(StrEnum):
    RUN_CREATED = "RUN_CREATED"
    RUN_STATE_CHANGED = "RUN_STATE_CHANGED"
    RUN_LEASE_ACQUIRED = "RUN_LEASE_ACQUIRED"
    RUN_HEARTBEAT = "RUN_HEARTBEAT"
    RUN_LEASE_RELEASED = "RUN_LEASE_RELEASED"
    CAPABILITY_LEASE_ATTACHED = "CAPABILITY_LEASE_ATTACHED"
    TRUST_POLICY_EVALUATED = "TRUST_POLICY_EVALUATED"
    STEP_READY = "STEP_READY"
    STEP_STARTED = "STEP_STARTED"
    STEP_HEARTBEAT = "STEP_HEARTBEAT"
    STEP_SUCCEEDED = "STEP_SUCCEEDED"
    STEP_RETRY_SCHEDULED = "STEP_RETRY_SCHEDULED"
    STEP_NEEDS_HUMAN = "STEP_NEEDS_HUMAN"
    STEP_BLOCKED = "STEP_BLOCKED"
    STEP_FAILED = "STEP_FAILED"
    STEP_CANCELLED = "STEP_CANCELLED"
    STALE_LEASE_RECOVERED = "STALE_LEASE_RECOVERED"
    DUPLICATE_DELIVERY_IGNORED = "DUPLICATE_DELIVERY_IGNORED"
    EVIDENCE_RECORDED = "EVIDENCE_RECORDED"
    FAULT_INJECTED = "FAULT_INJECTED"
    OPERATOR_ACTION = "OPERATOR_ACTION"


DEFAULT_AGENT_RUN_STEPS: tuple[AgentRunStepType, ...] = (
    AgentRunStepType.VALIDATE_SCOPE,
    AgentRunStepType.CREATE_BRANCH,
    AgentRunStepType.APPLY_CHANGES,
    AgentRunStepType.RUN_VALIDATION,
    AgentRunStepType.CREATE_PULL_REQUEST,
    AgentRunStepType.VERIFY_RESULT,
    AgentRunStepType.SEAL_EVIDENCE,
)


class AgentRunCreate(BaseModel):
    task_id: str
    capability_lease_id: str | None = None
    max_attempts_per_step: int = Field(default=3, ge=1, le=10)
    correlation_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentRunCapabilityLeaseRequest(BaseModel):
    capability_lease_id: str


class AgentRun(BaseModel):
    id: str = Field(default_factory=lambda: new_id("agent_run"))
    task_id: str
    approval_request_id: str | None = None
    capability_lease_id: str | None = None
    project_id: str | None = None
    repository: str | None = None
    objective: str = ""
    correlation_id: str = Field(default_factory=lambda: new_id("corr"))
    status: AgentRunStatus = AgentRunStatus.SCHEDULED
    current_step_id: str | None = None
    current_step_index: int = 0
    max_attempts_per_step: int = 3
    pause_requested: bool = False
    cancel_requested: bool = False
    lease_owner: str | None = None
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    final_evidence_hash: str | None = None
    last_error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class AgentRunStep(BaseModel):
    id: str = Field(default_factory=lambda: new_id("agent_run_step"))
    run_id: str
    task_id: str
    sequence: int
    step_type: AgentRunStepType
    status: AgentRunStepStatus = AgentRunStepStatus.PENDING
    attempt_count: int = 0
    max_attempts: int = 3
    idempotency_key: str
    lease_owner: str | None = None
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    next_retry_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = None
    decision: VerificationDecision | None = None
    reason: str = ""
    last_error: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    evidence_hash: str | None = None
    output_reference: str | None = None
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class AgentRunEvent(BaseModel):
    id: str = Field(default_factory=lambda: new_id("agent_run_event"))
    run_id: str
    task_id: str
    step_id: str | None = None
    correlation_id: str
    event_type: AgentRunEventType
    previous_status: str | None = None
    new_status: str | None = None
    message: str = ""
    reason: str = ""
    actor: str = "system"
    attempt_number: int | None = None
    input_evidence_hash: str | None = None
    output_evidence_hash: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now_utc)


class AgentRunControlRequest(BaseModel):
    reason: str = ""


class AgentRunRetryRequest(AgentRunControlRequest):
    step_id: str | None = None


class AgentRunExecuteRequest(BaseModel):
    worker_id: str = "mission-control-worker"
    lease_seconds: int = Field(default=300, ge=10, le=3600)
    timeout_seconds: int = Field(default=120, ge=1, le=3600)


class AgentRunHeartbeatRequest(BaseModel):
    worker_id: str
    lease_token: str
    lease_seconds: int = Field(default=300, ge=10, le=3600)


class AgentRunFaultKind(StrEnum):
    WORKER_CRASH = "WORKER_CRASH"
    GITHUB_TIMEOUT = "GITHUB_TIMEOUT"
    DUPLICATE_CALLBACK = "DUPLICATE_CALLBACK"
    LOST_ACKNOWLEDGEMENT = "LOST_ACKNOWLEDGEMENT"
    VALIDATION_TIMEOUT = "VALIDATION_TIMEOUT"
    INVALID_AGENT_OUTPUT = "INVALID_AGENT_OUTPUT"
    STALE_LEASE = "STALE_LEASE"
    DATABASE_DISCONNECT = "DATABASE_DISCONNECT"
    CONTAINER_FAILURE = "CONTAINER_FAILURE"
    FORBIDDEN_FILE_MUTATION = "FORBIDDEN_FILE_MUTATION"


class AgentRunFaultConfig(BaseModel):
    id: str = Field(default_factory=lambda: new_id("agent_run_fault"))
    run_id: str | None = None
    step_type: AgentRunStepType | None = None
    fault_kind: AgentRunFaultKind
    enabled: bool = True
    remaining_uses: int = Field(default=1, ge=0, le=100)
    message: str = ""
    created_by: str = "system"
    created_at: datetime = Field(default_factory=now_utc)


class AgentRunFaultCreate(BaseModel):
    run_id: str | None = None
    step_type: AgentRunStepType | None = None
    fault_kind: AgentRunFaultKind
    remaining_uses: int = Field(default=1, ge=1, le=100)
    message: str = ""


class AgentRunStepExecutionResult(BaseModel):
    success: bool
    decision: VerificationDecision
    reason: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    output_reference: str | None = Field(default=None, exclude=True)


class AgentRunDetail(BaseModel):
    run: AgentRun
    steps: list[AgentRunStep] = Field(default_factory=list)
    events: list[AgentRunEvent] = Field(default_factory=list)


class AgentRunSummary(BaseModel):
    total: int
    active: int
    retry_wait: int
    needs_human: int
    blocked: int
    failed: int
    succeeded: int
    cancelled: int
    queue_depth: int
