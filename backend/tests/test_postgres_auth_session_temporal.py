from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app.models import SessionToken, User, UserRole, Workspace, now_utc
from app.postgres_auth_repository import PostgresAuthRepository
from app.postgres_database import run_postgres_migrations


@pytest.fixture
def auth_repository() -> Iterator[tuple[PostgresAuthRepository, Callable[[], Connection]]]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL integration tests")
    schema = f"ct_session_time_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    try:
        run_postgres_migrations(connect)
        yield PostgresAuthRepository(connect), connect
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _insert_verified_owner(connect: Callable[[], Connection]) -> User:
    workspace = Workspace(name="Session time test", owner_user_id="owner")
    user = User(
        id="owner",
        email="owner@example.test",
        role=UserRole.OWNER,
        password_hash="a" * 64,
        password_salt="b" * 32,
        workspace_id=workspace.id,
        email_verified=True,
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
            (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
        )
        conn.execute(
            "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,email_verified,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                user.id,
                workspace.id,
                user.email,
                user.email,
                user.role.value,
                user.disabled,
                user.email_verified,
                Jsonb(user.model_dump(mode="json")),
            ),
        )
    return user


@pytest.mark.parametrize(
    ("created_at", "expires_at"),
    [
        (
            datetime(2026, 1, 1),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 2),
        ),
    ],
)
def test_session_creation_rejects_naive_timestamps(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
    created_at: datetime,
    expires_at: datetime,
) -> None:
    repository, _ = auth_repository
    session = SessionToken(
        user_id="owner",
        token_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        created_at=created_at,
        expires_at=expires_at,
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        repository.create_session(session)


def test_session_creation_rejects_future_creation_time(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
) -> None:
    repository, _ = auth_repository
    future = now_utc() + timedelta(minutes=1)
    session = SessionToken(
        user_id="owner",
        token_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        created_at=future,
        expires_at=future + timedelta(days=1),
    )

    with pytest.raises(ValueError, match="future"):
        repository.create_session(session)


def test_authentication_rejects_naive_current_time_without_query(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
) -> None:
    repository, _ = auth_repository

    with pytest.raises(ValueError, match="timezone-aware"):
        repository.authenticate_session(
            hashlib.sha256(b"valid-token").hexdigest(),
            now=datetime(2026, 1, 1),
        )


def test_authentication_rejects_future_dated_persisted_session(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
) -> None:
    repository, connect = auth_repository
    user = _insert_verified_owner(connect)
    current = now_utc()
    future = current + timedelta(minutes=1)
    session = SessionToken(
        user_id=user.id,
        token_hash=hashlib.sha256(b"future-session-token").hexdigest(),
        created_at=future,
        expires_at=future + timedelta(days=1),
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO sessions(session_id,user_id,token_hash,created_at,expires_at,revoked,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (
                session.id,
                session.user_id,
                session.token_hash,
                session.created_at,
                session.expires_at,
                session.revoked,
                Jsonb(session.model_dump(mode="json")),
            ),
        )

    assert repository.authenticate_session(session.token_hash, now=current) is None
