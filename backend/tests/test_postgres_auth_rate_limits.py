from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from psycopg import Connection, sql

from app.postgres_auth_rate_limits import PostgresAuthRateLimitRepository
from app.postgres_database import run_postgres_migrations


@pytest.fixture
def rate_limits() -> Iterator[tuple[PostgresAuthRateLimitRepository, Callable[[], Connection]]]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL integration tests")
    schema = f"ct_auth_rl_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    try:
        run_postgres_migrations(connect)
        yield PostgresAuthRateLimitRepository(connect), connect
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_fixed_window_rate_limit_allows_limit_then_returns_retry_after(
    rate_limits: tuple[PostgresAuthRateLimitRepository, Callable[[], Connection]],
) -> None:
    repository, _ = rate_limits
    digest = "a" * 64

    assert repository.consume(bucket_hash=digest, limit=2, window_seconds=60, now_epoch=121) is None
    assert repository.consume(bucket_hash=digest, limit=2, window_seconds=60, now_epoch=122) is None
    assert repository.consume(bucket_hash=digest, limit=2, window_seconds=60, now_epoch=123) == 57
    assert repository.consume(bucket_hash=digest, limit=2, window_seconds=60, now_epoch=180) is None


def test_rate_limit_concurrent_consumption_never_exceeds_configured_limit(
    rate_limits: tuple[PostgresAuthRateLimitRepository, Callable[[], Connection]],
) -> None:
    repository, connect = rate_limits
    digest = "b" * 64

    def consume(_: int) -> int | None:
        return repository.consume(
            bucket_hash=digest,
            limit=7,
            window_seconds=60,
            now_epoch=120,
        )

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(consume, range(20)))
    assert results.count(None) == 7
    assert all(result == 60 for result in results if result is not None)
    with connect() as conn:
        count = conn.execute(
            "SELECT request_count FROM auth_rate_limit_buckets WHERE bucket_hash=%s AND window_start=120",
            (digest,),
        ).fetchone()[0]
    assert count == 7


@pytest.mark.parametrize(
    ("digest", "limit", "window_seconds", "now_epoch", "message"),
    [
        ("plaintext", 1, 60, 120, "SHA-256"),
        ("c" * 64, 0, 60, 120, "positive"),
        ("c" * 64, 1, 0, 120, "positive"),
        ("c" * 64, 1, 60, -1, "non-negative"),
    ],
)
def test_rate_limit_rejects_invalid_inputs(
    rate_limits: tuple[PostgresAuthRateLimitRepository, Callable[[], Connection]],
    digest: str,
    limit: int,
    window_seconds: int,
    now_epoch: int,
    message: str,
) -> None:
    repository, _ = rate_limits
    with pytest.raises(ValueError, match=message):
        repository.consume(
            bucket_hash=digest,
            limit=limit,
            window_seconds=window_seconds,
            now_epoch=now_epoch,
        )
