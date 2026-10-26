from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time

from fastapi import HTTPException, status

from .settings import get_settings
from .store import store

logger = logging.getLogger(__name__)
_LOCAL_HMAC_KEY = secrets.token_bytes(32)
_BUCKET_RETENTION_SECONDS = 24 * 60 * 60


def _hmac_key() -> bytes:
    configured = os.getenv("AIXION_AUTH_RATE_LIMIT_HMAC_KEY", "").strip()
    if configured:
        try:
            key = bytes.fromhex(configured)
        except ValueError as exc:
            raise RuntimeError("Authentication rate-limit key is invalid") from exc
        if len(key) != 32:
            raise RuntimeError("Authentication rate-limit key must be 64 hexadecimal characters")
        return key
    settings = get_settings()
    if settings.is_production:
        raise RuntimeError("Authentication rate-limit HMAC key is required in production")
    lease_key = os.getenv("AIXION_LEASE_SIGNING_KEY", "").encode("utf-8")
    return hashlib.sha256(lease_key).digest() if lease_key else _LOCAL_HMAC_KEY


def _bucket_hash(bucket: str, identifier: str) -> str:
    message = f"{bucket}\x00{identifier.strip().lower()}".encode("utf-8")
    return hmac.new(_hmac_key(), message, hashlib.sha256).hexdigest()


def consume_auth_rate_limit(
    *,
    bucket: str,
    identifier: str,
    limit: int,
    window_seconds: int,
    now_epoch: int | None = None,
    postgres_repository=None,
) -> int | None:
    """Consume one fixed-window slot and return Retry-After if the bucket is full.

    Only a keyed digest is persisted, so bucket rows do not contain email or IP
    values. SQLite write locking makes the count update atomic in this local
    single-process adapter. Production requires the equivalent PostgreSQL
    transaction. When a runtime-owned PostgreSQL repository is supplied, only
    the digest and fixed-window counter are written through that repository.
    """
    if limit < 1 or window_seconds < 1:
        raise ValueError("Rate-limit bounds must be positive")
    settings = get_settings()
    if (
        getattr(settings, "is_production", False)
        or settings.postgres_auth_enabled
        or settings.postgres_auth_rate_limits_enabled
    ) and postgres_repository is None:
        raise RuntimeError(
            "PostgreSQL auth rate-limit repository is required in PostgreSQL mode"
        )
    now = int(time.time()) if now_epoch is None else int(now_epoch)
    window_start = now - now % window_seconds
    digest = _bucket_hash(bucket, identifier)
    if postgres_repository is not None:
        return postgres_repository.consume(
            bucket_hash=digest,
            limit=limit,
            window_seconds=window_seconds,
            now_epoch=now,
        )
    with store._lock, store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM auth_rate_limit_buckets WHERE window_start < ?",
            (now - _BUCKET_RETENTION_SECONDS,),
        )
        row = conn.execute(
            "SELECT request_count FROM auth_rate_limit_buckets "
            "WHERE bucket_hash=? AND window_start=?",
            (digest, window_start),
        ).fetchone()
        if row is not None and row[0] >= limit:
            conn.commit()
            return max(1, window_start + window_seconds - now)
        if row is None:
            conn.execute(
                "INSERT INTO auth_rate_limit_buckets(bucket_hash, window_start, request_count) "
                "VALUES (?, ?, 1)",
                (digest, window_start),
            )
        else:
            conn.execute(
                "UPDATE auth_rate_limit_buckets SET request_count=request_count+1 "
                "WHERE bucket_hash=? AND window_start=?",
                (digest, window_start),
            )
        conn.commit()
    return None


def enforce_auth_rate_limit(
    *, bucket: str, identifier: str, limit: int, window_seconds: int, postgres_repository=None
) -> None:
    retry_after = consume_auth_rate_limit(
        bucket=bucket,
        identifier=identifier,
        limit=limit,
        window_seconds=window_seconds,
        postgres_repository=postgres_repository,
    )
    if retry_after is None:
        return
    logger.warning("Authentication request rate limited: bucket=%s", bucket)
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Authentication request rate limit exceeded. Try again later.",
        headers={"Retry-After": str(retry_after)},
    )
