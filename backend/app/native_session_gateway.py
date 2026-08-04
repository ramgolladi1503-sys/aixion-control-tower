from __future__ import annotations

from threading import Lock
from typing import Any

from pydantic import BaseModel, Field, field_validator

from .models import now_utc
from .trust_crypto import sha256_hex


class NativeApprovalConflict(RuntimeError):
    """Raised when a native approval cannot be resolved without ambiguity."""


class NativeSessionIdentity(BaseModel):
    """Stable identity of the provider-owned execution context."""

    provider: str = Field(min_length=1, max_length=120)
    host_id: str = Field(min_length=1, max_length=300)
    connector_id: str = Field(min_length=1, max_length=300)
    native_session_id: str = Field(min_length=1, max_length=500)
    native_thread_id: str = Field(min_length=1, max_length=500)
    native_turn_id: str = Field(min_length=1, max_length=500)
    provider_process_id: int | None = Field(default=None, ge=1)

    @field_validator("provider")
    @classmethod
    def normalize_provider(cls, value: str) -> str:
        return value.strip().upper()


class NativeApprovalRequest(BaseModel):
    """Canonical provider approval request displayed by Aixion."""

    provider_request_id: str = Field(min_length=1, max_length=500)
    provider_item_id: str | None = Field(default=None, max_length=500)
    identity: NativeSessionIdentity
    action_kind: str = Field(min_length=1, max_length=120)
    command: str | None = Field(default=None, max_length=20000)
    cwd: str | None = Field(default=None, max_length=2000)
    reason: str | None = Field(default=None, max_length=4000)
    sandbox_scope: str | None = Field(default=None, max_length=500)
    network_scope: str | None = Field(default=None, max_length=500)
    permission_amendment: dict[str, Any] | None = None
    available_decisions: list[str] = Field(min_length=1, max_length=20)
    provider_payload: dict[str, Any] = Field(default_factory=dict)
    payload_hash: str = Field(min_length=64, max_length=64)
    expires_at: str | None = None

    @field_validator("available_decisions")
    @classmethod
    def normalize_decisions(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw in values:
            value = raw.strip()
            if not value:
                raise ValueError("provider decisions must not be blank")
            if value not in normalized:
                normalized.append(value)
        return normalized


class NativeApprovalResolution(BaseModel):
    """A mobile or local decision bound to the exact provider request."""

    provider_request_id: str = Field(min_length=1, max_length=500)
    payload_hash: str = Field(min_length=64, max_length=64)
    decision: str = Field(min_length=1, max_length=300)
    source_device_id: str = Field(min_length=1, max_length=300)
    actor_user_id: str = Field(min_length=1, max_length=300)
    resolved_at: str = Field(default_factory=lambda: now_utc().isoformat())


class NativeContinuationReceipt(BaseModel):
    """Provider acknowledgement that the original context received a decision."""

    provider_request_id: str
    payload_hash: str
    decision: str
    identity: NativeSessionIdentity
    provider_acknowledged: bool
    same_execution_context: bool
    provider_receipt_id: str | None = None
    acknowledged_at: str = Field(default_factory=lambda: now_utc().isoformat())


def native_approval_hash_payload(
    *,
    provider_request_id: str,
    provider_item_id: str | None,
    identity: NativeSessionIdentity,
    action_kind: str,
    command: str | None,
    cwd: str | None,
    reason: str | None,
    sandbox_scope: str | None,
    network_scope: str | None,
    permission_amendment: dict[str, Any] | None,
    available_decisions: list[str],
    provider_payload: dict[str, Any],
) -> dict[str, Any]:
    """Return the complete immutable payload covered by the action hash."""

    return {
        "provider_request_id": provider_request_id,
        "provider_item_id": provider_item_id,
        "identity": identity.model_dump(mode="json"),
        "action_kind": action_kind,
        "command": command,
        "cwd": cwd,
        "reason": reason,
        "sandbox_scope": sandbox_scope,
        "network_scope": network_scope,
        "permission_amendment": permission_amendment,
        "available_decisions": available_decisions,
        "provider_payload": provider_payload,
    }


def build_native_approval_request(
    *,
    provider_request_id: str,
    identity: NativeSessionIdentity,
    action_kind: str,
    available_decisions: list[str],
    provider_item_id: str | None = None,
    command: str | None = None,
    cwd: str | None = None,
    reason: str | None = None,
    sandbox_scope: str | None = None,
    network_scope: str | None = None,
    permission_amendment: dict[str, Any] | None = None,
    provider_payload: dict[str, Any] | None = None,
    expires_at: str | None = None,
) -> NativeApprovalRequest:
    """Build a canonical request whose exact provider semantics are hashed."""

    normalized_decisions = NativeApprovalRequest.normalize_decisions(
        available_decisions
    )
    payload = native_approval_hash_payload(
        provider_request_id=provider_request_id,
        provider_item_id=provider_item_id,
        identity=identity,
        action_kind=action_kind,
        command=command,
        cwd=cwd,
        reason=reason,
        sandbox_scope=sandbox_scope,
        network_scope=network_scope,
        permission_amendment=permission_amendment,
        available_decisions=normalized_decisions,
        provider_payload=provider_payload or {},
    )
    return NativeApprovalRequest(
        **payload,
        payload_hash=sha256_hex(payload),
        expires_at=expires_at,
    )


def validate_native_resolution(
    request: NativeApprovalRequest,
    resolution: NativeApprovalResolution,
) -> None:
    """Fail closed unless the decision matches the exact provider request."""

    if resolution.provider_request_id != request.provider_request_id:
        raise NativeApprovalConflict("provider request identity mismatch")
    if resolution.payload_hash != request.payload_hash:
        raise NativeApprovalConflict("approval payload hash mismatch")
    if resolution.decision not in request.available_decisions:
        raise NativeApprovalConflict(
            "decision is not available for the provider request"
        )


class NativeApprovalDecisionLedger:
    """Process-local atomic first-valid-decision-wins primitive.

    Durable production storage must provide the same compare-and-set semantics.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._resolutions: dict[str, NativeApprovalResolution] = {}

    def resolve(
        self,
        request: NativeApprovalRequest,
        resolution: NativeApprovalResolution,
    ) -> NativeApprovalResolution:
        validate_native_resolution(request, resolution)
        with self._lock:
            existing = self._resolutions.get(request.provider_request_id)
            if existing is None:
                self._resolutions[request.provider_request_id] = resolution
                return resolution
            if existing == resolution:
                return existing
            raise NativeApprovalConflict(
                "provider request was already resolved by another decision"
            )

    def get(self, provider_request_id: str) -> NativeApprovalResolution | None:
        with self._lock:
            return self._resolutions.get(provider_request_id)


def verify_native_continuation(
    request: NativeApprovalRequest,
    resolution: NativeApprovalResolution,
    receipt: NativeContinuationReceipt,
) -> None:
    """Verify that the provider acknowledged the original execution context."""

    validate_native_resolution(request, resolution)
    if receipt.provider_request_id != request.provider_request_id:
        raise NativeApprovalConflict("provider receipt request identity mismatch")
    if receipt.payload_hash != request.payload_hash:
        raise NativeApprovalConflict("provider receipt payload hash mismatch")
    if receipt.decision != resolution.decision:
        raise NativeApprovalConflict("provider receipt decision mismatch")
    if receipt.identity != request.identity:
        raise NativeApprovalConflict("native execution context changed")
    if not receipt.provider_acknowledged:
        raise NativeApprovalConflict("provider did not acknowledge the decision")
    if not receipt.same_execution_context:
        raise NativeApprovalConflict("provider did not continue the same context")
