from __future__ import annotations

import hashlib
import multiprocessing
import os
import uuid
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

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
    PostgresExternalAgentAuthRepository,
    PostgresExternalAgentConflictError,
)


def _rotate_in_process(
    dsn: str,
    schema: str,
    agent_data: dict[str, Any],
    credential_data: dict[str, Any],
    workspace_id: str,
    index: int,
    start_barrier: Any,
    outcomes: Any,
) -> None:
    def connect() -> psycopg.Connection:
        connection = psycopg.connect(dsn)
        connection.execute(
            sql.SQL("SET search_path TO {}").format(sql.Identifier(schema))
        )
        return connection

    expected_agent = ExternalAgent.model_validate(agent_data)
    expected_credential = AgentCredentialRecord.model_validate(credential_data)
    rotated_at = now_utc() + timedelta(seconds=index + 1)
    updated_agent = expected_agent.model_copy(
        update={
            "secret_hash": hashlib.sha256(
                f"process-replacement-{index}".encode()
            ).hexdigest(),
            "updated_at": rotated_at,
        }
    )
    updated_credential = expected_credential.model_copy(
        update={"token_rotated_at": rotated_at, "updated_at": rotated_at}
    )
    event = AuditEvent(
        event_type="agent.token_rotated",
        actor=f"process-{index}@example.test",
        entity_id=expected_agent.id,
        workspace_id=workspace_id,
        details={},
        created_at=rotated_at,
    )

    try:
        start_barrier.wait(timeout=20)
        PostgresExternalAgentAuthRepository(connect).update(
            agent=updated_agent,
            credential=updated_credential,
            audit_event=event,
            expected_agent=expected_agent,
            expected_credential=expected_credential,
        )
    except PostgresExternalAgentConflictError:
        outcomes.put("conflict")
    except BaseException as error:
        outcomes.put(f"error:{type(error).__name__}:{error}")
        raise
    else:
        outcomes.put("committed")


def _authenticate_rotated_token_in_process(
    dsn: str,
    schema: str,
    agent_id: str,
    old_token: str,
    replacement_token: str,
    outcomes: Any,
) -> None:
    """Observe token rotation and audit effects from a fresh OS process."""
    def connect() -> psycopg.Connection:
        connection = psycopg.connect(dsn)
        connection.execute(
            sql.SQL("SET search_path TO {}").format(sql.Identifier(schema))
        )
        return connection

    repository = PostgresExternalAgentAuthRepository(connect)
    old_result = repository.authenticate_and_consume(
        agent_id=agent_id,
        token=old_token,
        occurred_at=now_utc(),
        window_seconds=60,
        require_workspace=True,
    )
    replacement_result = repository.authenticate_and_consume(
        agent_id=agent_id,
        token=replacement_token,
        occurred_at=now_utc() + timedelta(seconds=1),
        window_seconds=60,
        require_workspace=True,
    )
    outcomes.put(
        {
            "old_reason": old_result.reason,
            "old_audit_type": (
                old_result.audit_event.event_type
                if old_result.audit_event is not None
                else None
            ),
            "replacement_reason": replacement_result.reason,
            "replacement_agent_id": (
                replacement_result.agent.id
                if replacement_result.agent is not None
                else None
            ),
        }
    )


def test_concurrent_token_rotations_from_separate_processes_commit_once() -> None:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip(
            "AIXION_TEST_POSTGRES_DSN is required for PostgreSQL integration tests"
        )

    schema = f"ct_agent_process_race_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> psycopg.Connection:
        connection = psycopg.connect(dsn)
        connection.execute(
            sql.SQL("SET search_path TO {}").format(sql.Identifier(schema))
        )
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
        name="Process-level credential race",
        owner_user_id=owner.id,
    )

    try:
        run_postgres_migrations(connect)
        with connect() as connection:
            connection.execute(
                "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
                (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
            )
            connection.execute(
                "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,"
                "email_verified,payload) VALUES(%s,%s,%s,%s,%s,FALSE,TRUE,%s)",
                (
                    owner.id,
                    workspace.id,
                    owner.email,
                    owner.email.lower().strip(),
                    owner.role.value,
                    Jsonb(owner.model_dump(mode="json")),
                ),
            )

        raw_token = f"aixion_agent_{uuid.uuid4().hex}"
        agent = ExternalAgent(
            provider=AgentProvider.ANTIGRAVITY,
            display_name="Process race connector",
            auth_type=AgentAuthType.API_KEY,
            allowed_actions=[],
            created_by_user_id=owner.id,
            workspace_id=workspace_id,
            secret_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
        )
        credential = AgentCredentialRecord(agent_id=agent.id, rate_limit_per_minute=60)
        registration_event = AuditEvent(
            event_type="agent.registered",
            actor=owner.email,
            entity_id=agent.id,
            workspace_id=workspace_id,
            details={
                "provider": agent.provider,
                "display_name": agent.display_name,
                "auth_type": agent.auth_type,
                "allowed_project_ids": agent.allowed_project_ids,
                "allowed_repositories": agent.allowed_repositories,
                "allowed_actions": agent.allowed_actions,
            },
        )
        repository = PostgresExternalAgentAuthRepository(connect)
        repository.create(
            agent=agent,
            credential=credential,
            audit_event=registration_event,
        )

        context = multiprocessing.get_context("spawn")
        start_barrier = context.Barrier(2)
        outcomes = context.Queue()
        agent_data = agent.model_dump(mode="json")
        credential_data = credential.model_dump(mode="json")
        workers = [
            context.Process(
                target=_rotate_in_process,
                args=(
                    dsn,
                    schema,
                    agent_data,
                    credential_data,
                    workspace_id,
                    index,
                    start_barrier,
                    outcomes,
                ),
            )
            for index in range(2)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=30)

        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
                pytest.fail("Credential rotation process did not finish before timeout.")
            assert worker.exitcode == 0

        observed = sorted(outcomes.get(timeout=5) for _ in workers)
        assert observed == ["committed", "conflict"]

        current = repository.get(agent.id)
        assert current is not None
        assert current[0].secret_hash in {
            hashlib.sha256(b"process-replacement-0").hexdigest(),
            hashlib.sha256(b"process-replacement-1").hexdigest(),
        }
        with connect() as connection:
            audit_count = connection.execute(
                "SELECT count(*) FROM audit_events WHERE entity_id=%s "
                "AND event_type='agent.token_rotated'",
                (agent.id,),
            ).fetchone()[0]
        assert audit_count == 1
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_committed_token_rotation_is_observed_by_fresh_process() -> None:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip(
            "AIXION_TEST_POSTGRES_DSN is required for PostgreSQL integration tests"
        )

    schema = f"ct_agent_rotation_process_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> psycopg.Connection:
        connection = psycopg.connect(dsn)
        connection.execute(
            sql.SQL("SET search_path TO {}").format(sql.Identifier(schema))
        )
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
        name="Cross-process token rotation",
        owner_user_id=owner.id,
    )

    try:
        run_postgres_migrations(connect)
        with connect() as connection:
            connection.execute(
                "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
                (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
            )
            connection.execute(
                "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,"
                "email_verified,payload) VALUES(%s,%s,%s,%s,%s,FALSE,TRUE,%s)",
                (
                    owner.id,
                    workspace.id,
                    owner.email,
                    owner.email.lower().strip(),
                    owner.role.value,
                    Jsonb(owner.model_dump(mode="json")),
                ),
            )

        old_token = f"aixion_agent_{uuid.uuid4().hex}"
        replacement_token = f"aixion_agent_{uuid.uuid4().hex}"
        agent = ExternalAgent(
            provider=AgentProvider.ANTIGRAVITY,
            display_name="Cross-process rotation connector",
            auth_type=AgentAuthType.API_KEY,
            allowed_actions=[],
            created_by_user_id=owner.id,
            workspace_id=workspace_id,
            secret_hash=hashlib.sha256(old_token.encode()).hexdigest(),
        )
        credential = AgentCredentialRecord(agent_id=agent.id, rate_limit_per_minute=60)
        repository = PostgresExternalAgentAuthRepository(connect)
        repository.create(
            agent=agent,
            credential=credential,
            audit_event=AuditEvent(
                event_type="agent.registered",
                actor=owner.email,
                entity_id=agent.id,
                workspace_id=workspace_id,
                details={
                    "provider": agent.provider,
                    "display_name": agent.display_name,
                    "auth_type": agent.auth_type,
                    "allowed_project_ids": agent.allowed_project_ids,
                    "allowed_repositories": agent.allowed_repositories,
                    "allowed_actions": agent.allowed_actions,
                },
            ),
        )

        rotated_at = now_utc()
        rotated_agent = agent.model_copy(
            update={
                "secret_hash": hashlib.sha256(replacement_token.encode()).hexdigest(),
                "updated_at": rotated_at,
            }
        )
        rotated_credential = credential.model_copy(
            update={"token_rotated_at": rotated_at, "updated_at": rotated_at}
        )
        repository.update(
            agent=rotated_agent,
            credential=rotated_credential,
            audit_event=AuditEvent(
                event_type="agent.token_rotated",
                actor=owner.email,
                entity_id=agent.id,
                workspace_id=workspace_id,
                details={},
                created_at=rotated_at,
            ),
            expected_agent=agent,
            expected_credential=credential,
        )

        context = multiprocessing.get_context("spawn")
        outcomes = context.Queue()
        process = context.Process(
            target=_authenticate_rotated_token_in_process,
            args=(dsn, schema, agent.id, old_token, replacement_token, outcomes),
        )
        process.start()
        process.join(timeout=30)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
            pytest.fail("Fresh token-authentication process did not finish before timeout.")
        assert process.exitcode == 0
        observed = outcomes.get(timeout=5)
        assert observed == {
            "old_reason": "invalid_token",
            "old_audit_type": "agent.auth_failed",
            "replacement_reason": None,
            "replacement_agent_id": agent.id,
        }

        current = repository.get(agent.id)
        assert current is not None
        assert current[0].secret_hash == hashlib.sha256(replacement_token.encode()).hexdigest()
        assert current[1].failed_auth_count == 1
        with connect() as connection:
            audit_rows = connection.execute(
                "SELECT event_type,details FROM audit_events WHERE entity_id=%s "
                "ORDER BY created_at",
                (agent.id,),
            ).fetchall()
        assert [row[0] for row in audit_rows] == [
            "agent.registered",
            "agent.token_rotated",
            "agent.auth_failed",
        ]
        assert audit_rows[-1][1]["reason"] == "invalid_token"
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
