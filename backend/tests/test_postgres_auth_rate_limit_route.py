from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from types import SimpleNamespace

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import Connection, sql

from app.main import app
from app.application_repositories import ApplicationRepositories
from app.postgres_auth_rate_limits import PostgresAuthRateLimitRepository
from app.postgres_database import run_postgres_migrations
from app.store import store


@pytest.fixture
def postgres_rate_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[PostgresAuthRateLimitRepository, Callable[[], Connection]]]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL route tests")

    schema = f"ct_auth_route_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    prior_runtime = getattr(app.state, "postgres_runtime", None)
    prior_repositories = getattr(app.state, "postgres_repositories", None)
    try:
        run_postgres_migrations(connect)
        repositories = ApplicationRepositories.from_connector(connect)
        repository = repositories.auth_rate_limits
        monkeypatch.setenv("AIXION_PROFILE", "test")
        monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")
        monkeypatch.setenv("AIXION_POSTGRES_RUNTIME_PROBE_ENABLED", "true")
        monkeypatch.setenv("AIXION_POSTGRES_AUTH_RATE_LIMITS_ENABLED", "true")
        monkeypatch.setenv("AIXION_AUTH_RATE_LIMIT_HMAC_KEY", "ab" * 32)
        monkeypatch.setenv("AIXION_AUTH_LOGIN_LIMIT_PER_IP", "1")
        monkeypatch.setattr(
            app.state,
            "postgres_runtime",
            SimpleNamespace(repositories=repositories),
            raising=False,
        )
        monkeypatch.setattr(
            app.state,
            "postgres_repositories",
            repositories,
            raising=False,
        )
        yield repository, connect
    finally:
        monkeypatch.setattr(app.state, "postgres_runtime", prior_runtime, raising=False)
        monkeypatch.setattr(
            app.state, "postgres_repositories", prior_repositories, raising=False
        )
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_login_rate_limit_route_uses_postgres_repository(
    postgres_rate_limits: tuple[
        PostgresAuthRateLimitRepository, Callable[[], Connection]
    ],
) -> None:
    _, connect = postgres_rate_limits
    client = TestClient(app)
    payload = {
        "email": f"missing-{uuid.uuid4().hex}@example.com",
        "password": "not-a-real-password",
    }
    with store._connect() as connection:
        sqlite_rows_before = connection.execute(
            "SELECT COUNT(*) FROM auth_rate_limit_buckets"
        ).fetchone()[0]

    first = client.post("/auth/login", json=payload)
    second = client.post("/auth/login", json=payload)

    with connect() as connection:
        postgres_rows = connection.execute(
            "SELECT request_count FROM auth_rate_limit_buckets"
        ).fetchall()
    with store._connect() as connection:
        sqlite_rows_after = connection.execute(
            "SELECT COUNT(*) FROM auth_rate_limit_buckets"
        ).fetchone()[0]

    assert first.status_code == 401
    assert second.status_code == 429
    assert 1 <= int(second.headers["Retry-After"]) <= 60
    assert postgres_rows == [(1,)]
    assert sqlite_rows_after == sqlite_rows_before
