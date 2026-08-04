from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import AgentProvider, RiskLevel, UserRole, new_id, now_utc


class CapabilityLeaseMode(StrEnum):
    STRICT = "STRICT"
    BOUNDED = "BOUNDED"
    SUPERVISED = "SUPERVISED"


class CapabilityLeaseStatus(StrEnum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    CONSUMED = "CONSUMED"


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


V1_ALLOWED_CAPABILITY_ACTIONS = {
    CapabilityActionType.READ_REPOSITORY,
    CapabilityActionType.CREATE_BRANCH,
    CapabilityActionType.MODIFY_FILES,
    CapabilityActionType.RUN_COMMAND,
    CapabilityActionType.ACCESS_NETWORK,
    CapabilityActionType.CREATE_PULL_REQUEST,
}


class CapabilityScope(BaseModel):
    repository: str
    branch: str
    allowed_actions: list[CapabilityActionType] = Field(min_length=1, max_length=10)
    allowed_path_prefixes: list[str] = Field(default_factory=list, max_length=100)
    allowed_commands: list[str] = Field(default_factory=list, max_length=50)
    allowed_network_domains: list[str] = Field(default_factory=list, max_length=20)
    max_runtime_seconds: int = Field(default=900, ge=1, le=3600)
    max_cost_usd: float = Field(default=5.0, ge=0.0, le=10.0)
    max_retries: int = Field(default=3, ge=0, le=3)
    max_pull_requests: int = Field(default=1, ge=0, le=1)
    allow_auto_merge: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        cleaned = value.strip()
        if "/" not in cleaned or cleaned.startswith("/") or cleaned.endswith("/"):
            raise ValueError("repository must use owner/name format")
        return cleaned

    @field_validator("branch")
    @classmethod
    def validate_branch(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("branch is required")
        if cleaned in {"main", "master"}:
            raise ValueError("protected branches cannot be leased for mutation")
        return cleaned

    @field_validator("allowed_actions")
    @classmethod
    def validate_v1_actions(
        cls,
        values: list[CapabilityActionType],
    ) -> list[CapabilityActionType]:
        normalized = list(dict.fromkeys(values))
        unsupported = sorted(
            value.value
            for value in normalized
            if value not in V1_ALLOWED_CAPABILITY_ACTIONS
        )
        if unsupported:
            raise ValueError(
                "V1 capability leases cannot grant high-risk or custom actions: "
                + ", ".join(unsupported)
            )
        return normalized

    @field_validator("allowed_path_prefixes", "allowed_commands", "allowed_network_domains")
    @classmethod
    def normalize_strings(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            cleaned = value.strip()
            if cleaned and cleaned not in normalized:
                normalized.append(cleaned)
        return normalized


class CapabilityLeaseCreate(BaseModel):
    approval_request_id: str
    agent_id: str | None = None
    provider: AgentProvider = AgentProvider.OTHER
    mode: CapabilityLeaseMode = CapabilityLeaseMode.STRICT
    scope: CapabilityScope
    expires_in_seconds: int = Field(default=900, ge=30, le=3600)
    required_reviewer_count: int = Field(default=1, ge=1, le=10)
    prevent_self_approval: bool = True


class CapabilityLease(BaseModel):
    id: str = Field(default_factory=lambda: new_id("lease"))
    approval_request_id: str
    approved_payload_hash: str
    agent_id: str | None = None
    provider: AgentProvider = AgentProvider.OTHER
    mode: CapabilityLeaseMode
    scope: CapabilityScope
    issued_by_user_id: str
    required_reviewer_count: int = 1
    prevent_self_approval: bool = True
    status: CapabilityLeaseStatus = CapabilityLeaseStatus.ACTIVE
    issued_at: datetime = Field(default_factory=now_utc)
    expires_at: datetime
    revoked_at: datetime | None = None
    revoked_by_user_id: str | None = None
    revocation_reason: str = ""
    receipt_nonce: str
    receipt_signature: str
    consumed_pull_requests: int = 0
    consumed_retries: int = 0
    consumed_runtime_seconds: int = 0
    consumed_cost_usd: float = 0.0
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class CapabilityLeasePublic(BaseModel):
    id: str
    approval_request_id: str
    agent_id: str | None = None
    provider: AgentProvider
    mode: CapabilityLeaseMode
    scope: CapabilityScope
    issued_by_user_id: str
    status: CapabilityLeaseStatus
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    revocation_reason: str = ""
    consumed_pull_requests: int
    consumed_retries: int
    consumed_runtime_seconds: int
    consumed_cost_usd: float
    receipt_signature: str


class LeaseRevocationRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class ReviewerAttestationDecision(StrEnum):
    APPROVE = "APPROVE"
    DENY = "DENY"


class ReviewerAttestationCreate(BaseModel):
    approval_request_id: str
    decision: ReviewerAttestationDecision
    reason: str = Field(default="", max_length=1000)


class ReviewerAttestation(BaseModel):
    id: str = Field(default_factory=lambda: new_id("attestation"))
    approval_request_id: str
    approval_payload_hash: str
    reviewer_user_id: str
    reviewer_role: UserRole
    decision: ReviewerAttestationDecision
    reason: str = ""
    signature: str
    created_at: datetime = Field(default_factory=now_utc)


class ProposedAction(BaseModel):
    id: str = Field(default_factory=lambda: new_id("action"))
    lease_id: str | None = None
    provider: AgentProvider = AgentProvider.OTHER
    agent_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    project_id: str | None = None
    action_type: CapabilityActionType
    repository: str | None = None
    branch: str | None = None
    paths: list[str] = Field(default_factory=list, max_length=100)
    command: str | None = Field(default=None, max_length=4000)
    network_domains: list[str] = Field(default_factory=list, max_length=20)
    estimated_runtime_seconds: int = Field(default=0, ge=0, le=3600)
    estimated_cost_usd: float = Field(default=0.0, ge=0.0, le=10.0)
    retry_number: int = Field(default=0, ge=0, le=3)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now_utc)


class ActionConsumption(BaseModel):
    actual_runtime_seconds: int = Field(default=0, ge=0, le=3600)
    actual_cost_usd: float = Field(default=0.0, ge=0.0, le=10.0)
    pull_requests_created: int = Field(default=0, ge=0, le=1)
    retry_consumed: bool = False
    output_reference: str | None = Field(default=None, max_length=4000)
    evidence: dict[str, Any] = Field(default_factory=dict)


class PolicyDecisionType(StrEnum):
    ALLOW = "ALLOW"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    BLOCK = "BLOCK"


class PolicyDecision(BaseModel):
    id: str = Field(default_factory=lambda: new_id("decision"))
    proposed_action_id: str
    lease_id: str | None = None
    decision: PolicyDecisionType
    risk_level: RiskLevel
    reasons: list[str] = Field(default_factory=list)
    obligations: list[str] = Field(default_factory=list)
    evaluated_policy_version: str = "trust-policy-v1"
    evaluated_at: datetime = Field(default_factory=now_utc)


class AgentGatewayResult(BaseModel):
    action: ProposedAction
    decision: PolicyDecision
    lease: CapabilityLeasePublic | None = None


class TrustEventType(StrEnum):
    ATTESTATION_RECORDED = "ATTESTATION_RECORDED"
    LEASE_ISSUED = "LEASE_ISSUED"
    LEASE_REVOKED = "LEASE_REVOKED"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    ACTION_RECEIVED = "ACTION_RECEIVED"
    ACTION_ALLOWED = "ACTION_ALLOWED"
    ACTION_REQUIRES_APPROVAL = "ACTION_REQUIRES_APPROVAL"
    ACTION_BLOCKED = "ACTION_BLOCKED"
    ACTION_CONSUMED = "ACTION_CONSUMED"
    CREDENTIAL_GRANT_ISSUED = "CREDENTIAL_GRANT_ISSUED"
    CREDENTIAL_GRANT_REVOKED = "CREDENTIAL_GRANT_REVOKED"
    RUN_EXCEPTION = "RUN_EXCEPTION"


class TrustEvent(BaseModel):
    id: str = Field(default_factory=lambda: new_id("trust_event"))
    sequence: int
    event_type: TrustEventType
    entity_type: str
    entity_id: str
    actor: str
    correlation_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    previous_hash: str | None = None
    event_hash: str
    created_at: datetime = Field(default_factory=now_utc)


class FlightRecorderVerification(BaseModel):
    valid: bool
    event_count: int
    first_invalid_sequence: int | None = None
    reason: str = ""


class CredentialGrantType(StrEnum):
    GITHUB_APP_INSTALLATION = "GITHUB_APP_INSTALLATION"
    OIDC_FEDERATION = "OIDC_FEDERATION"
    INTERNAL_CAPABILITY_TOKEN = "INTERNAL_CAPABILITY_TOKEN"


class CredentialGrant(BaseModel):
    id: str = Field(default_factory=lambda: new_id("credential_grant"))
    lease_id: str
    grant_type: CredentialGrantType
    audience: str
    subject: str
    scope: list[str] = Field(default_factory=list, max_length=50)
    issued_at: datetime = Field(default_factory=now_utc)
    expires_at: datetime
    revoked: bool = False
    revoked_at: datetime | None = None
    token_hash: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class CredentialGrantPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    lease_id: str
    grant_type: CredentialGrantType
    audience: str
    subject: str
    scope: list[str]
    issued_at: datetime
    expires_at: datetime
    revoked: bool
    revoked_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CredentialGrantCreate(BaseModel):
    lease_id: str
    grant_type: CredentialGrantType = CredentialGrantType.INTERNAL_CAPABILITY_TOKEN
    audience: str = Field(min_length=1, max_length=500)
    subject: str = Field(min_length=1, max_length=500)
    scope: list[str] = Field(default_factory=list, max_length=50)
    expires_in_seconds: int = Field(default=300, ge=30, le=3600)


class CredentialGrantResponse(BaseModel):
    grant: CredentialGrantPublic
    bearer_token: str


class AgentReliabilityScorecard(BaseModel):
    provider: AgentProvider
    agent_id: str | None = None
    evaluated_actions: int
    allowed_actions: int
    blocked_actions: int
    approval_escalations: int
    scope_adherence_rate: float
    first_attempt_success_rate: float
    recovery_success_rate: float
    human_intervention_rate: float
    policy_block_rate: float
    evidence_completion_rate: float
    average_runtime_seconds: float
    total_cost_usd: float
    confidence: float
    generated_at: datetime = Field(default_factory=now_utc)


class ExceptionQueueItem(BaseModel):
    id: str
    category: str
    severity: RiskLevel
    title: str
    summary: str
    run_id: str | None = None
    task_id: str | None = None
    action_id: str | None = None
    lease_id: str | None = None
    created_at: datetime
    requires_human: bool = True
