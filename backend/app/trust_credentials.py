from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from .models import AuthUser, now_utc
from .store import store
from .trust_crypto import verify_bearer_token, verify_payload_signature
from .trust_flight_recorder import append_trust_event
from .trust_models import (
    CapabilityLease,
    CredentialGrant,
    CredentialGrantPublic,
    TrustEventType,
)
from .trust_policy import lease_receipt_payload
from .workspace_access import (
    capability_lease_scope_is_consistent,
    credential_grant_scope_is_consistent,
)


class CredentialIntrospectionRequest(BaseModel):
    bearer_token: str = Field(min_length=20, max_length=4096)
    audience: str
    required_scope: list[str] = Field(default_factory=list)


class CredentialIntrospectionResult(BaseModel):
    active: bool
    grant_id: str | None = None
    lease_id: str | None = None
    subject: str | None = None
    audience: str | None = None
    scope: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None
    reason: str = ""


class CredentialRevocationRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


def introspect_credential(
    payload: CredentialIntrospectionRequest,
) -> CredentialIntrospectionResult:
    for grant in store.credential_grants.values():
        if not verify_bearer_token(payload.bearer_token, grant.token_hash):
            continue
        lease = store.capability_leases.get(grant.lease_id)
        if (
            not credential_grant_scope_is_consistent(grant)
            or lease is None
            or not capability_lease_scope_is_consistent(lease)
        ):
            return CredentialIntrospectionResult(
                active=False,
                reason="Credential ownership scope is inconsistent.",
            )
        return introspect_credential_record(payload, grant, lease)
    return CredentialIntrospectionResult(active=False, reason="Credential token is invalid.")


def introspect_credential_record(
    payload: CredentialIntrospectionRequest,
    grant: CredentialGrant | None,
    lease: CapabilityLease | None,
) -> CredentialIntrospectionResult:
    """Apply the shared credential, lease, audience, and scope policy."""
    if grant is None or not verify_bearer_token(payload.bearer_token, grant.token_hash):
        return CredentialIntrospectionResult(active=False, reason="Credential token is invalid.")
    now = datetime.now(timezone.utc)
    if (
        lease is None
        or grant.lease_id != lease.id
        or (
            grant.workspace_id is not None
            and lease.workspace_id is not None
            and grant.workspace_id != lease.workspace_id
        )
    ):
        return CredentialIntrospectionResult(
            active=False,
            reason="Credential ownership scope is inconsistent.",
        )
    if not verify_payload_signature(
        lease_receipt_payload(lease), lease.receipt_signature
    ):
        return CredentialIntrospectionResult(
            active=False,
            reason="Capability lease receipt is invalid.",
        )
    if grant.revoked:
        return CredentialIntrospectionResult(active=False, reason="Credential grant was revoked.")
    if grant.expires_at <= now:
        return CredentialIntrospectionResult(active=False, reason="Credential grant expired.")
    if (
        lease.status.value != "ACTIVE"
        or lease.expires_at <= now
    ):
        return CredentialIntrospectionResult(active=False, reason="Capability lease is not active.")
    if grant.audience != payload.audience:
        return CredentialIntrospectionResult(active=False, reason="Credential audience mismatch.")
    missing_scope = sorted(set(payload.required_scope) - set(grant.scope))
    if missing_scope:
        return CredentialIntrospectionResult(
            active=False,
            reason="Credential scope is missing: " + ", ".join(missing_scope),
        )
    return CredentialIntrospectionResult(
        active=True,
        grant_id=grant.id,
        lease_id=grant.lease_id,
        subject=grant.subject,
        audience=grant.audience,
        scope=grant.scope,
        expires_at=grant.expires_at,
    )


def revoke_credential_grant(
    grant: CredentialGrant | CredentialGrantPublic,
    *,
    user: AuthUser,
    reason: str,
) -> CredentialGrant:
    canonical = store.credential_grants.get(grant.id)
    if canonical is None:
        raise ValueError("Credential grant not found.")
    if not credential_grant_scope_is_consistent(canonical):
        raise ValueError("Credential grant not found.")
    if canonical.revoked:
        return canonical
    canonical.revoked = True
    canonical.revoked_at = now_utc()
    canonical.metadata = {
        **canonical.metadata,
        "revoked_by_user_id": user.id,
        "revocation_reason": reason,
    }
    append_trust_event(
        TrustEventType.CREDENTIAL_GRANT_REVOKED,
        entity_type="credential_grant",
        entity_id=canonical.id,
        actor=user.email,
        workspace_id=canonical.workspace_id,
        payload={"lease_id": canonical.lease_id, "reason": reason},
        persist=False,
    )
    store.persist()
    return canonical
