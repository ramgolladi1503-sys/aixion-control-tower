from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from typing import Any

from .settings import get_settings


def _signing_key() -> bytes:
    configured = os.getenv("AIXION_LEASE_SIGNING_KEY", "").strip()
    if configured:
        return configured.encode("utf-8")
    settings = get_settings()
    if settings.is_production:
        raise RuntimeError("AIXION_LEASE_SIGNING_KEY is required in production")
    return b"aixion-development-signing-key-change-me"


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def sha256_hex(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def sign_payload(payload: Any) -> str:
    return hmac.new(
        _signing_key(),
        canonical_json(payload).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_payload_signature(payload: Any, signature: str) -> bool:
    expected = sign_payload(payload)
    return hmac.compare_digest(expected.encode("utf-8"), signature.encode("utf-8"))


def new_nonce() -> str:
    return secrets.token_urlsafe(24)


def issue_bearer_token() -> tuple[str, str]:
    raw = secrets.token_urlsafe(48)
    return raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def verify_bearer_token(raw_token: str, expected_hash: str) -> bool:
    candidate = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    return hmac.compare_digest(candidate.encode("utf-8"), expected_hash.encode("utf-8"))
