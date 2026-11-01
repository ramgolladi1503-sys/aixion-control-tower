from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator

import psycopg
import pytest
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app.models import User, UserRole, Workspace
from app.postgres_database import run_postgres_migrations
from app.postgres_external_agent_auth_repository import PostgresExternalAgentAuthRepository


@pytest.fixture
def agent_repository() -> Iterator[
    tuple[PostgresExternalAgentAuthRepository, Callable[[], Connection], str, str]
]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL integration tests")
    schema = f"ct_agent_auth_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    workspace_id = f"workspace_{uuid.uuid4().hex}"
    owner = User(
        id=f"user_{uuid.uuid4().hex}",
        email=f"{uuid.uuid4().hex}@example.test",
        display_name="Workspace owner",
        role=UserRole.OWNER,
        password_hash="test-hash",
        password_salt="test-salt",
        workspace_id=workspace_id,
        email_verified=True,
    )
    workspace = Workspace(
        id=workspace_id,
        name="External agent auth test",
        owner_user_id=owner.id,
    )
    try:
        run_postgres_migrations(connect)
        with connect() as conn:
            conn.execute(
                "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
                (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
            )
            conn.execute(
                "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,email_verified,payload) "
                "VALUES(%s,%s,%s,%s,%s,FALSE,TRUE,%s)",
                (
                    owner.id,
                    workspace.id,
                    owner.email,
                    owner.email.lower().strip(),
                    owner.role.value,
                    Jsonb(owner.model_dump(mode="json")),
                ),
            )
        yield PostgresExternalAgentAuthRepository(connect), connect, workspace.id, owner.id
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
