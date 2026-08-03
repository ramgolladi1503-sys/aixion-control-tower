from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from .models import AuthUser, now_utc
from .store import store
from .trust_crypto import verify_bearer_token
from .trust_flight_recorder import append_trust_event
from .trust_models import CredentialGrant, TrustEventType


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
    now = datetime.now(timezone.utc)
    for grant in store.credential_grants.values():
        if not verify_bearer_token(payload.bearer_token, grant.token_hash):
            continue
        lease = store.capability_leases.get(grant.lease_id)
        if grant.revoked:
            return CredentialIntrospectionResult(active=False, reason="Credential grant was revoked.")
        if grant.expires_at <= now:
            return CredentialIntrospectionResult(active=False, reason="Credential grant expired.")
        if lease is None or lease.status.value != "ACTIVE" or lease.expires_at <= now:
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
    return CredentialIntrospectionResult(active=False, reason="Credential token is invalid.")


def revoke_credential_grant(
    grant: CredentialGrant,
    *,
    user: AuthUser,
    reason: str,
) -> CredentialGrant:
    if grant.revoked:
        return grant
    grant.revoked = True
    grant.revoked_at = now_utc()
    grant.metadata = {
        **grant.metadata,
        "revoked_by_user_id": user.id,
        "revocation_reason": reason,
    }
    append_trust_event(
        TrustEventType.CREDENTIAL_GRANT_REVOKED,
        entity_type="credential_grant",
        entity_id=grant.id,
        actor=user.email,
        payload={"lease_id": grant.lease_id, "reason": reason},
        persist=False,
    )
    store.persist()
    return grant
