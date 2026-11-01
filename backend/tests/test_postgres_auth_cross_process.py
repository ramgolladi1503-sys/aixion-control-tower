from __future__ import annotations

import hashlib
import multiprocessing
import os
import uuid
from concurrent.futures import ProcessPoolExecutor
from datetime import timedelta
from collections.abc import Callable, Iterator

import psycopg
import pytest
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app.models import SessionToken, User, UserRole, Workspace, now_utc
from app.postgres_auth_repository import PostgresAuthRepository
from app.postgres_database import run_postgres_migrations


def _session_is_authenticated_in_fresh_process(
    dsn: str, schema: str, token_hash: str
) -> bool:
    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    return PostgresAuthRepository(connect).authenticate_session(token_hash) is not None


@pytest.fixture
def auth_database() -> Iterator[tuple[str, str, Callable[[], Connection]]]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL integration tests")

    schema = f"ct_auth_process_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    try:
        run_postgres_migrations(connect)
        yield dsn, schema, connect
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_committed_session_revocation_is_visible_in_a_fresh_process(
    auth_database: tuple[str, str, Callable[[], Connection]],
) -> None:
    dsn, schema, connect = auth_database
    workspace = Workspace(id=f"workspace_{uuid.uuid4().hex}", name="Process auth", owner_user_id="owner")
    owner = User(
        id="owner",
        email="owner@example.test",
        display_name="Owner",
        role=UserRole.OWNER,
        password_hash="owner-hash",
        password_salt="owner-salt",
        workspace_id=workspace.id,
        email_verified=True,
    )
    reviewer = User(
        id="reviewer",
        email="reviewer@example.test",
        display_name="Reviewer",
        role=UserRole.REVIEWER,
        password_hash="reviewer-hash",
        password_salt="reviewer-salt",
        workspace_id=workspace.id,
        email_verified=True,
    )
    raw_token = "process-visible-session-token"
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    session = SessionToken(
        user_id=reviewer.id,
        token_hash=token_hash,
        expires_at=now_utc() + timedelta(days=1),
    )

    with connect() as conn:
        conn.execute(
            "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
            (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
        )
        for user in (owner, reviewer):
            conn.execute(
                "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,email_verified,payload) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    user.id,
                    user.workspace_id,
                    user.email,
                    user.email.lower(),
                    user.role.value,
                    user.disabled,
                    user.email_verified,
                    Jsonb(user.model_dump(mode="json")),
                ),
            )
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

    result = PostgresAuthRepository(connect).revoke_user_sessions(
        actor_user_id=owner.id,
        target_user_id=reviewer.id,
    )
    assert result.revoked_count == 1

    with ProcessPoolExecutor(
        max_workers=1,
        mp_context=multiprocessing.get_context("spawn"),
    ) as pool:
        authenticated = pool.submit(
            _session_is_authenticated_in_fresh_process,
            dsn,
            schema,
            token_hash,
        ).result(timeout=15)

    assert authenticated is False
