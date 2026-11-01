from __future__ import annotations

import re
import time
from collections.abc import Callable

from psycopg import Connection

_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_BUCKET_RETENTION_SECONDS = 24 * 60 * 60


class PostgresAuthRateLimitRepository:
    """Atomic fixed-window counters; only keyed identifier digests are stored."""

    def __init__(self, connect: Callable[[], Connection]) -> None:
        self._connect = connect

    def consume(
        self,
        *,
        bucket_hash: str,
        limit: int,
        window_seconds: int,
        now_epoch: int | None = None,
    ) -> int | None:
        """Consume one slot, returning Retry-After when the window is full."""
        if not _DIGEST_PATTERN.fullmatch(bucket_hash):
            raise ValueError("Rate-limit bucket must be a lowercase SHA-256 digest")
        if limit < 1 or window_seconds < 1:
            raise ValueError("Rate-limit bounds must be positive")

        now = int(time.time()) if now_epoch is None else int(now_epoch)
        if now < 0:
            raise ValueError("Rate-limit timestamp must be non-negative")
        window_start = now - now % window_seconds
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM auth_rate_limit_buckets WHERE window_start < %s",
                (now - _BUCKET_RETENTION_SECONDS,),
            )
            row = conn.execute(
                "INSERT INTO auth_rate_limit_buckets(bucket_hash,window_start,request_count) "
                "VALUES(%s,%s,1) ON CONFLICT(bucket_hash,window_start) DO UPDATE "
                "SET request_count=auth_rate_limit_buckets.request_count+1 "
                "WHERE auth_rate_limit_buckets.request_count < %s "
                "RETURNING request_count",
                (bucket_hash, window_start, limit),
            ).fetchone()
            if row is not None:
                return None
        return max(1, window_start + window_seconds - now)
