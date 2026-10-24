from __future__ import annotations

import sqlite3
import os
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from psycopg.types.json import Jsonb

from app.connector_models import AgentConnector
from app.models import AgentProvider, AuthUser, ExternalAgent, User, UserRole, Workspace
from app.postgres_database import run_postgres_migrations
from app.workspace_access import (
    connector_scope_is_consistent,
    external_agent_scope_is_consistent,
    workspace_id_for_user,
)
from app import workspace_access
from app.workspace_membership_repository import (
    PostgresWorkspaceMembershipRepository,
    SQLiteWorkspaceMembershipRepository,
    WorkspaceMembershipIntegrityError,
)
from app.store import store


@pytest.fixture(autouse=True)
def isolated_global_store():
    store.reset()
    yield
    store.reset()


def _repository(database_path) -> SQLiteWorkspaceMembershipRepository:
    def connect() -> sqlite3.Connection:
        return sqlite3.connect(database_path)

    with connect() as conn:
        conn.execute(
            "CREATE TABLE kv_store (entity_type TEXT, entity_id TEXT, payload TEXT, "
            "PRIMARY KEY(entity_type, entity_id))"
        )
    return SQLiteWorkspaceMembershipRepository(connect)


def test_sqlite_membership_snapshot_reads_user_and_workspace_from_one_store(
    tmp_path,
) -> None:
    repository = _repository(tmp_path / "membership.sqlite3")
    workspace = Workspace(name="Account workspace", owner_user_id="owner-1")
    user = User(
        id="owner-1",
        email="owner@example.test",
        password_hash="hash",
        password_salt="salt",
        workspace_id=workspace.id,
    )
    with repository._connect() as conn:
        conn.executemany(
            "INSERT INTO kv_store(entity_type, entity_id, payload) VALUES (?, ?, ?)",
            [
                ("workspace", workspace.id, workspace.model_dump_json()),
                ("user", user.id, user.model_dump_json()),
            ],
        )

    snapshot = repository.snapshot(user_id=user.id, claimed_workspace_id=workspace.id)

    assert snapshot.user == user
    assert snapshot.workspace_count == 1
    assert snapshot.resolved_workspace_exists is True


def test_sqlite_membership_snapshot_does_not_trust_unknown_claimed_workspace(
    tmp_path,
) -> None:
    repository = _repository(tmp_path / "missing-workspace.sqlite3")
    snapshot = repository.snapshot(
        user_id="missing-user",
        claimed_workspace_id="workspace-does-not-exist",
    )

    assert snapshot.user is None
    assert snapshot.workspace_count == 0
    assert snapshot.resolved_workspace_exists is False


def test_sqlite_workspace_lookup_reads_durable_row_after_cache_eviction() -> None:
    workspace = Workspace(name="Durable lookup workspace", owner_user_id="owner-lookup")
    store.workspaces[workspace.id] = workspace
    store.persist()
    store.workspaces.clear()

    repository = SQLiteWorkspaceMembershipRepository(store._connect)

    assert repository.get_workspace(workspace.id) == workspace
    assert repository.get_workspace("missing-workspace") is None


def test_sqlite_workspace_lookup_rejects_payload_identity_mismatch(tmp_path) -> None:
    repository = _repository(tmp_path / "mismatched-workspace.sqlite3")
    workspace = Workspace(
        id="workspace-payload-id", name="Mismatched", owner_user_id="owner"
    )
    with repository._connect() as conn:
        conn.execute(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            ("workspace", "workspace-index-id", workspace.model_dump_json()),
        )

    with pytest.raises(WorkspaceMembershipIntegrityError):
        repository.get_workspace("workspace-index-id")


def test_connector_and_agent_scope_do_not_depend_on_workspace_cache() -> None:
    workspace = Workspace(name="Scoped workspace", owner_user_id="owner-scoped")
    store.workspaces[workspace.id] = workspace
    store.persist()
    store.workspaces.clear()
    connector = AgentConnector(name="Scoped connector", workspace_id=workspace.id)
    agent = ExternalAgent(
        provider=AgentProvider.CODEX,
        display_name="Scoped Codex",
        workspace_id=workspace.id,
    )

    assert connector_scope_is_consistent(connector) is True
    assert external_agent_scope_is_consistent(agent) is True


def test_sqlite_membership_snapshot_rejects_mismatched_user_identity(tmp_path) -> None:
    repository = _repository(tmp_path / "mismatched-user.sqlite3")
    workspace = Workspace(name="Account workspace", owner_user_id="different-user")
    user = User(
        id="different-user",
        email="owner@example.test",
        password_hash="hash",
        password_salt="salt",
        workspace_id=workspace.id,
    )
    with repository._connect() as conn:
        conn.executemany(
            "INSERT INTO kv_store(entity_type, entity_id, payload) VALUES (?, ?, ?)",
            [
                ("workspace", workspace.id, workspace.model_dump_json()),
                ("user", "indexed-user", user.model_dump_json()),
            ],
        )

    with pytest.raises(WorkspaceMembershipIntegrityError):
        repository.snapshot(user_id="indexed-user", claimed_workspace_id=None)


def test_workspace_authorization_uses_durable_membership_after_cache_eviction() -> None:
    workspace = Workspace(name="Durable workspace", owner_user_id="owner-1")
    user = User(
        id="owner-1",
        email="owner@example.test",
        password_hash="hash",
        password_salt="salt",
        workspace_id=workspace.id,
    )
    store.workspaces[workspace.id] = workspace
    store.users[user.id] = user
    store.persist()
    store.workspaces.clear()
    store.users.clear()

    authenticated_user = AuthUser(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=UserRole.OWNER,
    )

    assert workspace_id_for_user(authenticated_user) == workspace.id


def test_production_workspace_authorization_rejects_claim_without_durable_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = Workspace(name="Claim-only workspace", owner_user_id="real-owner")
    store.workspaces[workspace.id] = workspace
    store.persist()
    repository = SQLiteWorkspaceMembershipRepository(store._connect)
    user_with_unverified_claim = AuthUser(
        id="missing-user",
        email="missing@example.test",
        display_name="Missing User",
        role=UserRole.OWNER,
        workspace_id=workspace.id,
    )
    monkeypatch.setattr(
        workspace_access,
        "get_settings",
        lambda: SimpleNamespace(is_production=True),
    )

    with pytest.raises(HTTPException) as error:
        workspace_id_for_user(
            user_with_unverified_claim,
            membership_repository=repository,
        )

    assert error.value.status_code == 403
    assert error.value.detail == "Workspace membership is required"


def test_postgres_membership_snapshot_reads_normalized_scope_atomically(
    postgres_schema,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _schema, connect = postgres_schema
    run_postgres_migrations(connect)
    workspace = Workspace(name="PostgreSQL workspace", owner_user_id="owner-pg")
    another_workspace = Workspace(name="Other workspace", owner_user_id="owner-other")
    user = User(
        id="owner-pg",
        email="owner-pg@example.test",
        password_hash="hash",
        password_salt="salt",
        workspace_id=workspace.id,
    )
    with connect() as conn:
        for item in (workspace, another_workspace):
            conn.execute(
                "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
                (item.id, Jsonb(item.model_dump(mode="json"))),
            )
        conn.execute(
            "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,"
            "email_verified,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                user.id,
                workspace.id,
                user.email,
                user.email.lower().strip(),
                user.role.value,
                user.disabled,
                user.email_verified,
                Jsonb(user.model_dump(mode="json")),
            ),
        )

    repository = PostgresWorkspaceMembershipRepository(connect)
    assert repository.get_workspace(workspace.id) == workspace
    assert repository.get_workspace("missing-workspace") is None
    member = repository.snapshot(user_id=user.id, claimed_workspace_id=workspace.id)
    unknown_claim = repository.snapshot(
        user_id="missing-user",
        claimed_workspace_id="missing-workspace",
    )

    assert member.user == user
    assert member.workspace_count == 2
    assert member.resolved_workspace_exists is True
    assert unknown_claim.user is None
    assert unknown_claim.workspace_count == 2
    assert unknown_claim.resolved_workspace_exists is False

    monkeypatch.setattr(
        workspace_access,
        "get_settings",
        lambda: SimpleNamespace(is_production=True),
    )
    authenticated_user = AuthUser(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        workspace_id=user.workspace_id,
    )
    assert (
        workspace_id_for_user(
            authenticated_user,
            membership_repository=repository,
        )
        == workspace.id
    )

    mismatched_claim = authenticated_user.model_copy(
        update={"workspace_id": another_workspace.id}
    )
    with pytest.raises(HTTPException, match="Workspace membership is invalid"):
        workspace_id_for_user(
            mismatched_claim,
            membership_repository=repository,
        )

    unknown_user = authenticated_user.model_copy(
        update={"id": "missing-pg-user", "workspace_id": workspace.id}
    )
    with pytest.raises(HTTPException, match="Workspace membership is required"):
        workspace_id_for_user(unknown_user, membership_repository=repository)


@pytest.fixture
def postgres_schema():
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is not configured")
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql

    schema = "workspace_membership_" + uuid.uuid4().hex

    def connect():
        conn = psycopg.connect(dsn)
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return conn

    with psycopg.connect(dsn) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        yield schema, connect
    finally:
        with psycopg.connect(dsn) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
