from __future__ import annotations

import hashlib
import hmac
import secrets

from fastapi import Header, HTTPException

from .relay_models import RelayHost, RelayStatus
from .store import store

RELAY_TOKEN_PREFIX = "aixr_"


def issue_relay_token() -> tuple[str, str]:
    token = RELAY_TOKEN_PREFIX + secrets.token_urlsafe(36)
    return token, hash_relay_token(token)


def hash_relay_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_relay_token(token: str, token_hash: str) -> bool:
    return hmac.compare_digest(hash_relay_token(token), token_hash)


def authenticate_relay(
    relay_id: str,
    x_aixion_relay_token: str | None = Header(default=None),
) -> RelayHost:
    relay = store.relay_hosts.get(relay_id)
    if relay is None:
        raise HTTPException(status_code=404, detail="Relay not found")
    if relay.status == RelayStatus.DISABLED:
        raise HTTPException(status_code=403, detail="Relay is disabled")
    if not x_aixion_relay_token:
        raise HTTPException(status_code=401, detail="Missing relay token")
    if not verify_relay_token(x_aixion_relay_token, relay.token_hash):
        raise HTTPException(status_code=401, detail="Invalid relay token")
    return relay
