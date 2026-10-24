from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import psycopg
import pytest
from fastapi import HTTPException
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app.agent_auth_repository import AgentAuthenticationResult
from app.agent_auth_repository import SQLiteExternalAgentAuthRepository
from app.agent_auth import revoke_external_agent_token
from app.agent_credential_models import AgentCredentialRecord
from app.models import (
    AgentAuthType,
    AgentProvider,
    AuditEvent,
    ExternalAgent,
    User,
    UserRole,
    Workspace,
    now_utc,
)
from app.postgres_database import run_postgres_migrations
from app.postgres_external_agent_auth_repository import (
    PostgresExternalAgentConflictError,
    PostgresExternalAgentAuthRepository,
)
from app.database_migrations import run_migrations


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


def _agent_and_credential(
    workspace_id: str,
    owner_id: str,
    *,
    rate_limit: int = 60,
    expires_at=None,
    provider: AgentProvider = AgentProvider.ANTIGRAVITY,
) -> tuple[ExternalAgent, AgentCredentialRecord, str]:
    raw_token = f"aixion_agent_{uuid.uuid4().hex}"
    agent = ExternalAgent(
        provider=provider,
        display_name="Test local connector",
        auth_type=AgentAuthType.API_KEY,
        allowed_actions=[],
        created_by_user_id=owner_id,
        workspace_id=workspace_id,
        secret_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
    )
    credential = AgentCredentialRecord(
        agent_id=agent.id,
        rate_limit_per_minute=rate_limit,
        token_expires_at=expires_at,
    )
    return agent, credential, raw_token


def _registration_audit_details(agent: ExternalAgent) -> dict[str, object]:
    return {
        "provider": agent.provider,
        "display_name": agent.display_name,
        "auth_type": agent.auth_type,
        "allowed_project_ids": agent.allowed_project_ids,
        "allowed_repositories": agent.allowed_repositories,
        "allowed_actions": agent.allowed_actions,
    }


def _registration_audit_event(agent: ExternalAgent) -> AuditEvent:
    return AuditEvent(
        event_type="agent.registered",
        actor="owner@example.test",
        entity_id=agent.id,
        workspace_id=agent.workspace_id,
        details=_registration_audit_details(agent),
    )


def test_sqlite_external_agent_listing_is_workspace_scoped_and_requires_credentials(
    tmp_path,
) -> None:
    database = tmp_path / "sqlite-agent-listing.sqlite3"

    def connect() -> sqlite3.Connection:
        return sqlite3.connect(database, timeout=10)

    with connect() as connection:
        run_migrations(connection)

    repository = SQLiteExternalAgentAuthRepository(connect)
    in_scope, in_scope_credential, _ = _agent_and_credential(
        "workspace-listing-a", "owner-a"
    )
    out_of_scope, out_of_scope_credential, _ = _agent_and_credential(
        "workspace-listing-b", "owner-b"
    )
    with connect() as connection:
        connection.executemany(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            (
                ("external_agent", in_scope.id, in_scope.model_dump_json()),
                (
                    "external_agent_credential",
                    in_scope.id,
                    in_scope_credential.model_dump_json(),
                ),
                ("external_agent", out_of_scope.id, out_of_scope.model_dump_json()),
                (
                    "external_agent_credential",
                    out_of_scope.id,
                    out_of_scope_credential.model_dump_json(),
                ),
            ),
        )

    listed = repository.list_for_workspace("workspace-listing-a")
    assert [(agent.id, credential.agent_id) for agent, credential in listed] == [
        (in_scope.id, in_scope.id)
    ]

    with connect() as connection:
        connection.execute(
            "DELETE FROM kv_store WHERE entity_type=? AND entity_id=?",
            ("external_agent_credential", in_scope.id),
        )
    with pytest.raises(ValueError, match="missing its credential record"):
        repository.list_for_workspace("workspace-listing-a")


def test_stale_external_agent_token_revoke_maps_to_conflict() -> None:
    agent, credential, _ = _agent_and_credential("workspace-a", "owner-a")

    class StaleRepository:
        def update(self, **kwargs) -> None:
            assert kwargs["expected_agent"] != kwargs["agent"]
            assert kwargs["expected_credential"] != kwargs["credential"]
            raise PostgresExternalAgentConflictError("snapshot changed")

    with pytest.raises(HTTPException) as error:
        revoke_external_agent_token(
            agent,
            credential=credential,
            repository=StaleRepository(),
            actor="owner@example.test",
        )

    assert error.value.status_code == 409
    assert error.value.detail == "Agent credentials changed; reload and retry"


def _create_agent(
    repository: PostgresExternalAgentAuthRepository,
    agent: ExternalAgent,
    credential: AgentCredentialRecord,
) -> AuditEvent:
    event = _registration_audit_event(agent)
    repository.create(agent=agent, credential=credential, audit_event=event)
    return event


def test_create_get_and_workspace_list_round_trip(agent_repository) -> None:
    repository, _, workspace_id, owner_id = agent_repository
    agent, credential, _ = _agent_and_credential(workspace_id, owner_id)
    _create_agent(repository, agent, credential)

    assert repository.get(agent.id) == (agent, credential)
    assert repository.get_for_workspace(agent.id, workspace_id=workspace_id) == (agent, credential)
    assert repository.get_for_workspace(agent.id, workspace_id="another-workspace") is None
    assert repository.list_for_workspace(workspace_id) == [(agent, credential)]
    assert repository.list_for_workspace("another-workspace") == []


def test_create_commits_agent_credential_and_registration_audit_together(agent_repository) -> None:
    repository, connect, workspace_id, owner_id = agent_repository
    agent, credential, _ = _agent_and_credential(workspace_id, owner_id)
    audit_event = _registration_audit_event(agent)

    repository.create(agent=agent, credential=credential, audit_event=audit_event)

    assert repository.get(agent.id) == (agent, credential)
    with connect() as conn:
        row = conn.execute(
            "SELECT workspace_id,entity_type,entity_id,event_type,actor FROM audit_events "
            "WHERE event_id=%s",
            (audit_event.id,),
        ).fetchone()
    assert row == (workspace_id, "external_agent", agent.id, "agent.registered", audit_event.actor)


@pytest.mark.parametrize("event_type", ["agent.token_rotated", "agent.token_revoked"])
def test_token_mutation_rejects_stale_snapshot_after_authentication(
    agent_repository, event_type: str
) -> None:
    repository, connect, workspace_id, owner_id = agent_repository
    agent, credential, raw_token = _agent_and_credential(workspace_id, owner_id)
    _create_agent(repository, agent, credential)
    expected_agent = agent.model_copy(deep=True)
    expected_credential = credential.model_copy(deep=True)

    authenticated_at = now_utc()
    result = repository.authenticate_and_consume(
        agent_id=agent.id,
        token=raw_token,
        occurred_at=authenticated_at,
        window_seconds=60,
        require_workspace=True,
    )
    assert result.reason is None

    changed_agent = agent.model_copy(deep=True)
    changed_credential = credential.model_copy(deep=True)
    changed_agent.updated_at = authenticated_at + timedelta(seconds=1)
    changed_credential.updated_at = authenticated_at + timedelta(seconds=1)
    if event_type == "agent.token_rotated":
        changed_agent.secret_hash = hashlib.sha256(b"replacement-token").hexdigest()
        changed_credential.token_rotated_at = changed_agent.updated_at
    else:
        changed_agent.secret_hash = None
        changed_credential.token_revoked_at = changed_agent.updated_at
    event = AuditEvent(
        event_type=event_type,
        actor="owner@example.test",
        entity_id=agent.id,
        workspace_id=workspace_id,
        details={},
        created_at=changed_agent.updated_at,
    )

    with pytest.raises(PostgresExternalAgentConflictError, match="changed"):
        repository.update(
            agent=changed_agent,
            credential=changed_credential,
            audit_event=event,
            expected_agent=expected_agent,
            expected_credential=expected_credential,
        )

    current = repository.get(agent.id)
    assert current is not None
    current_agent, current_credential = current
    assert current_agent.secret_hash == agent.secret_hash
    assert current_credential.last_used_at == authenticated_at
    assert current_credential.token_revoked_at is None
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE entity_id=%s AND event_type=%s",
            (agent.id, event_type),
        ).fetchone()[0] == 0


def test_concurrent_token_rotations_from_same_snapshot_commit_once(agent_repository) -> None:
    repository, connect, workspace_id, owner_id = agent_repository
    agent, credential, _ = _agent_and_credential(workspace_id, owner_id)
    _create_agent(repository, agent, credential)
    expected_agent = agent.model_copy(deep=True)
    expected_credential = credential.model_copy(deep=True)
    start = Barrier(2)

    def rotate(index: int) -> str:
        rotated_at = now_utc() + timedelta(seconds=index + 1)
        updated_agent = expected_agent.model_copy(
            update={
                "secret_hash": hashlib.sha256(f"replacement-{index}".encode()).hexdigest(),
                "updated_at": rotated_at,
            }
        )
        updated_credential = expected_credential.model_copy(
            update={"token_rotated_at": rotated_at, "updated_at": rotated_at}
        )
        event = AuditEvent(
            event_type="agent.token_rotated",
            actor=f"owner-{index}@example.test",
            entity_id=agent.id,
            workspace_id=workspace_id,
            details={},
            created_at=rotated_at,
        )
        start.wait(timeout=10)
        try:
            repository.update(
                agent=updated_agent,
                credential=updated_credential,
                audit_event=event,
                expected_agent=expected_agent,
                expected_credential=expected_credential,
            )
        except PostgresExternalAgentConflictError:
            return "conflict"
        return "committed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(rotate, (0, 1)))

    assert sorted(outcomes) == ["committed", "conflict"]
    current = repository.get(agent.id)
    assert current is not None
    assert current[0].secret_hash in {
        hashlib.sha256(b"replacement-0").hexdigest(),
        hashlib.sha256(b"replacement-1").hexdigest(),
    }
    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE entity_id=%s "
            "AND event_type='agent.token_rotated'",
            (agent.id,),
        ).fetchone()[0] == 1


def test_create_rejects_registration_audit_with_mismatched_agent_details(agent_repository) -> None:
    repository, connect, workspace_id, owner_id = agent_repository
    agent, credential, _ = _agent_and_credential(workspace_id, owner_id)
    audit_event = AuditEvent(
        event_type="agent.registered",
        actor="owner@example.test",
        entity_id=agent.id,
        workspace_id=workspace_id,
        details={**_registration_audit_details(agent), "provider": AgentProvider.CODEX},
    )

    with pytest.raises(ValueError, match="details must match"):
        repository.create(agent=agent, credential=credential, audit_event=audit_event)

    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM external_agents WHERE agent_id=%s", (agent.id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM external_agent_credentials WHERE agent_id=%s", (agent.id,)
        ).fetchone()[0] == 0


def test_create_rolls_back_agent_and_credential_when_registration_audit_insert_fails(
    agent_repository,
) -> None:
    repository, connect, workspace_id, owner_id = agent_repository
    agent, credential, _ = _agent_and_credential(workspace_id, owner_id)
    existing_audit = AuditEvent(
        event_type="agent.registration_seed",
        actor="test-fixture",
        entity_id=agent.id,
        workspace_id=workspace_id,
        details={},
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO audit_events"
            "(event_id,workspace_id,approval_id,entity_type,entity_id,event_type,actor,details,created_at,payload) "
            "VALUES(%s,%s,NULL,'external_agent',%s,%s,%s,%s,%s,%s)",
            (
                existing_audit.id,
                workspace_id,
                agent.id,
                existing_audit.event_type,
                existing_audit.actor,
                Jsonb(existing_audit.details),
                existing_audit.created_at,
                Jsonb(existing_audit.model_dump(mode="json")),
            ),
        )
    duplicate_audit = _registration_audit_event(agent).model_copy(
        update={"id": existing_audit.id}
    )

    with pytest.raises(psycopg.errors.UniqueViolation):
        repository.create(agent=agent, credential=credential, audit_event=duplicate_audit)

    with connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM external_agents WHERE agent_id=%s", (agent.id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM external_agent_credentials WHERE agent_id=%s", (agent.id,)
        ).fetchone()[0] == 0


def test_valid_token_updates_durable_rate_limit_and_last_used(agent_repository) -> None:
    repository, connect, workspace_id, owner_id = agent_repository
    agent, credential, raw_token = _agent_and_credential(
        workspace_id, owner_id, rate_limit=1
    )
    _create_agent(repository, agent, credential)
    occurred_at = now_utc()

    first = repository.authenticate_and_consume(
        agent_id=agent.id,
        token=raw_token,
        occurred_at=occurred_at,
        window_seconds=60,
        require_workspace=True,
    )
    second = repository.authenticate_and_consume(
        agent_id=agent.id,
        token=raw_token,
        occurred_at=occurred_at + timedelta(seconds=1),
        window_seconds=60,
        require_workspace=True,
    )

    assert first.reason is None
    assert first.agent == agent
    assert first.credential is not None
    assert first.credential.rate_limit_request_count == 1
    assert first.credential.last_used_at == occurred_at
    assert second.reason == "rate_limited"
    assert second.retry_after_seconds == 59
    assert second.audit_event is not None
    with connect() as conn:
        row = conn.execute(
            "SELECT rate_limit_request_count FROM external_agent_credentials WHERE agent_id=%s",
            (agent.id,),
        ).fetchone()
        audit_count = conn.execute(
            "SELECT count(*) FROM audit_events WHERE entity_id=%s AND event_type='agent.rate_limited'",
            (agent.id,),
        ).fetchone()[0]
    assert row[0] == 1
    assert audit_count == 1


def test_concurrent_authentication_cannot_exceed_agent_rate_limit(agent_repository) -> None:
    repository, connect, workspace_id, owner_id = agent_repository
    limit = 3
    workers = 12
    agent, credential, raw_token = _agent_and_credential(
        workspace_id, owner_id, rate_limit=limit
    )
    _create_agent(repository, agent, credential)
    barrier = Barrier(workers)
    occurred_at = now_utc()

    def authenticate(_: int):
        barrier.wait(timeout=10)
        return repository.authenticate_and_consume(
            agent_id=agent.id,
            token=raw_token,
            occurred_at=occurred_at,
            window_seconds=60,
            require_workspace=True,
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(authenticate, range(workers)))

    allowed = [result for result in results if result.reason is None]
    limited = [result for result in results if result.reason == "rate_limited"]
    assert len(allowed) == limit
    assert len(limited) == workers - limit
    assert all(result.agent == agent for result in results)
    assert all(result.retry_after_seconds == 60 for result in limited)

    with connect() as conn:
        row = conn.execute(
            "SELECT rate_limit_request_count FROM external_agent_credentials WHERE agent_id=%s",
            (agent.id,),
        ).fetchone()
        audit_count = conn.execute(
            "SELECT count(*) FROM audit_events WHERE entity_id=%s "
            "AND event_type='agent.rate_limited'",
            (agent.id,),
        ).fetchone()[0]
    assert row[0] == limit
    assert audit_count == workers - limit


@pytest.mark.parametrize(
    ("token", "expires_delta", "revoked", "expected_reason"),
    [
        ("wrong-token", None, False, "invalid_token"),
        (None, None, False, "missing_credentials"),
        ("valid", -1, False, "token_expired"),
        ("valid", None, True, "token_revoked_or_missing"),
        ("valid", None, False, "agent_disabled"),
        ("valid", None, False, "manual_agent_token_auth"),
    ],
)
def test_invalid_or_unusable_credentials_fail_closed_and_record_known_agent_failure(
    agent_repository,
    token,
    expires_delta,
    revoked,
    expected_reason,
) -> None:
    repository, connect, workspace_id, owner_id = agent_repository
    expires_at = now_utc() + timedelta(seconds=expires_delta) if expires_delta else None
    agent, credential, raw_token = _agent_and_credential(
        workspace_id, owner_id, expires_at=expires_at
    )
    if revoked:
        credential.token_revoked_at = now_utc()
    if expected_reason == "agent_disabled":
        agent.enabled = False
    elif expected_reason == "manual_agent_token_auth":
        agent.auth_type = AgentAuthType.MANUAL
    _create_agent(repository, agent, credential)
    presented = raw_token if token == "valid" else token

    result = repository.authenticate_and_consume(
        agent_id=agent.id,
        token=presented,
        occurred_at=now_utc(),
        window_seconds=60,
        require_workspace=True,
    )

    assert isinstance(result, AgentAuthenticationResult)
    assert result.reason == expected_reason
    assert result.credential is not None
    assert result.credential.failed_auth_count == 1
    assert result.audit_event is not None
    with connect() as conn:
        row = conn.execute(
            "SELECT failed_auth_count FROM external_agent_credentials WHERE agent_id=%s",
            (agent.id,),
        ).fetchone()
        audit_count = conn.execute(
            "SELECT count(*) FROM audit_events WHERE entity_id=%s "
            "AND event_type='agent.auth_failed'",
            (agent.id,),
        ).fetchone()[0]
    assert row[0] == 1
    assert audit_count == 1


def test_postgres_auth_expires_legacy_token_without_explicit_expiry(
    agent_repository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIXION_EXTERNAL_AGENT_TOKEN_TTL_SECONDS", "300")
    repository, _, workspace_id, owner_id = agent_repository
    agent, credential, raw_token = _agent_and_credential(workspace_id, owner_id)
    credential.token_expires_at = None
    credential.token_rotated_at = None
    credential.created_at = now_utc() - timedelta(seconds=301)
    _create_agent(repository, agent, credential)

    result = repository.authenticate_and_consume(
        agent_id=agent.id,
        token=raw_token,
        occurred_at=now_utc(),
        window_seconds=60,
        require_workspace=True,
    )

    assert result.reason == "token_expired"
    assert result.credential is not None
    assert result.audit_event is not None
    assert result.audit_event.details["reason"] == "token_expired"


@pytest.mark.parametrize(
    ("failure", "expected_reason"),
    [
        ("invalid", "invalid_token"),
        ("expired", "token_expired"),
        ("revoked", "token_revoked_or_missing"),
        ("disabled", "agent_disabled"),
        ("manual", "manual_agent_token_auth"),
    ],
)
def test_sqlite_known_agent_credential_failures_are_audited(
    tmp_path,
    failure: str,
    expected_reason: str,
) -> None:
    database = tmp_path / f"sqlite-agent-auth-{failure}.sqlite3"

    def connect() -> sqlite3.Connection:
        return sqlite3.connect(database, timeout=10)

    with connect() as connection:
        run_migrations(connection)
        workspace = Workspace(
            id=f"sqlite-workspace-{uuid.uuid4().hex}",
            name="SQLite auth contract",
            owner_user_id="sqlite-owner",
        )
        connection.execute(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            ("workspace", workspace.id, workspace.model_dump_json()),
        )

    repository = SQLiteExternalAgentAuthRepository(connect)
    agent, credential, raw_token = _agent_and_credential(workspace.id, "sqlite-owner")
    occurred_at = now_utc()
    presented_token: str | None = raw_token
    if failure == "invalid":
        presented_token = "wrong-token"
    elif failure == "expired":
        credential.token_expires_at = occurred_at - timedelta(seconds=1)
    elif failure == "revoked":
        credential.token_revoked_at = occurred_at - timedelta(seconds=1)
    elif failure == "disabled":
        agent.enabled = False
    elif failure == "manual":
        agent.auth_type = AgentAuthType.MANUAL

    with connect() as connection:
        connection.executemany(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            (
                ("external_agent", agent.id, agent.model_dump_json()),
                ("external_agent_credential", agent.id, credential.model_dump_json()),
            ),
        )
    result = repository.authenticate_and_consume(
        agent_id=agent.id,
        token=presented_token,
        occurred_at=occurred_at,
        window_seconds=60,
        require_workspace=True,
    )

    assert result.reason == expected_reason
    assert result.credential is not None
    assert result.credential.failed_auth_count == 1
    assert result.audit_event is not None
    assert result.audit_event.event_type == "agent.auth_failed"
    assert result.audit_event.details["reason"] == expected_reason
    with connect() as connection:
        credential_payload = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type='external_agent_credential' "
            "AND entity_id=?",
            (agent.id,),
        ).fetchone()[0]
        rows = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type='audit_event'"
        ).fetchall()
    assert AgentCredentialRecord.model_validate_json(credential_payload).failed_auth_count == 1
    failed_events = [
        AuditEvent.model_validate_json(row[0])
        for row in rows
        if AuditEvent.model_validate_json(row[0]).event_type == "agent.auth_failed"
    ]
    assert len(failed_events) == 1
    assert failed_events[0].details["reason"] == expected_reason


def test_unknown_agent_does_not_invent_workspace_audit_scope(agent_repository) -> None:
    repository, connect, _, _ = agent_repository

    result = repository.authenticate_and_consume(
        agent_id="unknown-agent",
        token="untrusted",
        occurred_at=now_utc(),
        window_seconds=60,
        require_workspace=True,
    )

    assert result.reason == "unknown_agent"
    assert result.audit_event is None
    with connect() as conn:
        assert conn.execute("SELECT count(*) FROM audit_events").fetchone()[0] == 0


def test_missing_allowed_project_fails_closed(agent_repository) -> None:
    repository, _, workspace_id, owner_id = agent_repository
    agent, credential, raw_token = _agent_and_credential(workspace_id, owner_id)
    agent.allowed_project_ids = ["missing-project"]
    _create_agent(repository, agent, credential)

    result = repository.authenticate_and_consume(
        agent_id=agent.id,
        token=raw_token,
        occurred_at=now_utc(),
        window_seconds=60,
        require_workspace=True,
    )

    assert result.reason == "invalid_workspace_scope"
    assert result.agent == agent
    assert result.audit_event is not None
