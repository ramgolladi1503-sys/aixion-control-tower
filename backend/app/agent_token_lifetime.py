from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .agent_credential_models import AgentCredentialRecord
from .models import now_utc
from .settings import get_settings


def resolve_agent_token_expiry(
    requested_expiry: datetime | None,
    *,
    issued_at: datetime | None = None,
) -> datetime:
    """Resolve a requested expiry within the configured maximum token lifetime."""
    now = _as_utc(issued_at or now_utc(), require_timezone=False)
    max_lifetime = timedelta(seconds=get_settings().external_agent_token_ttl_seconds)
    maximum_expiry = now + max_lifetime
    if requested_expiry is None:
        return maximum_expiry
    if requested_expiry.tzinfo is None or requested_expiry.utcoffset() is None:
        raise ValueError("token_expires_at must include a timezone")
    expiry = requested_expiry.astimezone(timezone.utc)
    if expiry <= now:
        raise ValueError("token_expires_at must be in the future")
    if expiry > maximum_expiry:
        raise ValueError(
            "token_expires_at must not exceed AIXION_EXTERNAL_AGENT_TOKEN_TTL_SECONDS"
        )
    return expiry


def effective_agent_token_expiry(
    credential: AgentCredentialRecord,
    *,
    ttl_seconds: int | None = None,
) -> datetime:
    """Return expiry capped by the credential's issue/rotation time and policy."""
    lifetime = (
        ttl_seconds
        if ttl_seconds is not None
        else get_settings().external_agent_token_ttl_seconds
    )
    issued_at = credential.token_rotated_at or credential.created_at
    maximum_expiry = _as_utc(issued_at, require_timezone=False) + timedelta(seconds=lifetime)
    if credential.token_expires_at is None:
        return maximum_expiry
    explicit_expiry = _as_utc(credential.token_expires_at, require_timezone=False)
    return min(explicit_expiry, maximum_expiry)


def _as_utc(value: datetime, *, require_timezone: bool) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        if require_timezone:
            raise ValueError("timestamp must include a timezone")
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
