from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta

import psycopg
import pytest
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app.models import Invite, InviteStatus, User, UserRole, Workspace, now_utc
from app.postgres_auth_repository import (
    PostgresAuthRepository,
)
from app.postgres_database import run_postgres_migrations


@pytest.fixture
def auth_repository() -> Iterator[tuple[PostgresAuthRepository, Callable[[], Connection]]]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL integration tests")
    schema = f"ct_invite_lookup_{uuid.uuid4().hex}"
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


def _insert_invite(
    connect: Callable[[], Connection],
    *,
    status: InviteStatus = InviteStatus.PENDING,
    expires_at=None,
) -> Invite:
    workspace = Workspace(name="Invite lookup test", owner_user_id="owner")
    invite = Invite(
        email="reviewer@example.test",
        role=UserRole.REVIEWER,
        token_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        status=status,
        expires_at=expires_at or now_utc() + timedelta(days=1),
        created_by_user_id="owner",
        workspace_id=workspace.id,
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
            (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
        )
        owner = User(
            id="owner",
            email="owner@example.test",
            display_name="Invite owner",
            role=UserRole.OWNER,
            password_hash="a" * 64,
            password_salt="b" * 32,
            workspace_id=workspace.id,
            email_verified=True,
        )
        conn.execute(
            "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,email_verified,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                owner.id,
                workspace.id,
                owner.email,
                owner.email,
                owner.role.value,
                owner.disabled,
                owner.email_verified,
                Jsonb(owner.model_dump(mode="json")),
            ),
        )
        conn.execute(
            "INSERT INTO invites(invite_id,workspace_id,email,normalized_email,role,token_hash,status,"
            "expires_at,created_by_user_id,accepted_by_user_id,created_at,updated_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                invite.id,
                invite.workspace_id,
                invite.email,
                invite.email,
                invite.role.value,
                invite.token_hash,
                invite.status.value,
                invite.expires_at,
                invite.created_by_user_id,
                invite.accepted_by_user_id,
                invite.created_at,
                invite.updated_at,
                Jsonb(invite.model_dump(mode="json")),
            ),
        )
    return invite


def test_invite_lookup_returns_matching_invite_without_consuming_it(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
) -> None:
    repository, connect = auth_repository
    invite = _insert_invite(connect)

    assert repository.get_invite_by_token_hash(invite.token_hash) == invite
    with connect() as conn:
        assert conn.execute(
            "SELECT status FROM invites WHERE invite_id=%s", (invite.id,)
        ).fetchone()[0] == "PENDING"


def test_invite_lookup_returns_none_for_missing_or_malformed_hash(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
) -> None:
    repository, _ = auth_repository

    assert repository.get_invite_by_token_hash("a" * 64) is None
    assert repository.get_invite_by_token_hash("not-a-token-hash") is None


@pytest.mark.parametrize(
    ("status", "expires_at"),
    [
        (InviteStatus.REVOKED, now_utc() + timedelta(days=1)),
        (InviteStatus.EXPIRED, now_utc() - timedelta(seconds=1)),
    ],
)
def test_invite_lookup_preserves_non_pending_state_for_caller_to_reject(
    auth_repository: tuple[PostgresAuthRepository, Callable[[], Connection]],
    status: InviteStatus,
    expires_at,
) -> None:
    repository, connect = auth_repository
    invite = _insert_invite(connect, status=status, expires_at=expires_at)

    result = repository.get_invite_by_token_hash(invite.token_hash)

    assert result == invite
    assert result.status == status
