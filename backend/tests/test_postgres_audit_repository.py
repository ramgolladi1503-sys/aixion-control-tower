from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta

import psycopg
import pytest
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app.application_repositories import ApplicationRepositories
from app.models import AuditEvent, Workspace, now_utc
from app.postgres_audit_repository import (
    PostgresAuditEventRepository,
    PostgresAuditRepositoryError,
)
from app.postgres_database import run_postgres_migrations


@pytest.fixture
def postgres_audit_repository() -> Iterator[
    tuple[PostgresAuditEventRepository, Callable[[], Connection], str, str]
]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL integration tests")

    schema = f"ct_audit_test_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    try:
        run_postgres_migrations(connect)
        workspace_a = Workspace(id=f"workspace_a_{uuid.uuid4().hex}", name="A", owner_user_id="owner_a")
        workspace_b = Workspace(id=f"workspace_b_{uuid.uuid4().hex}", name="B", owner_user_id="owner_b")
        with connect() as conn:
            for workspace in (workspace_a, workspace_b):
                conn.execute(
                    "INSERT INTO workspaces(workspace_id,payload) VALUES (%s,%s)",
                    (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
                )
        yield PostgresAuditEventRepository(connect), connect, workspace_a.id, workspace_b.id
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _insert_event(
    connect: Callable[[], Connection],
    *,
    workspace_id: str,
    event_type: str,
    entity_id: str,
    actor: str,
    details: dict[str, object],
    created_at,
) -> AuditEvent:
    event = AuditEvent(
        workspace_id=workspace_id,
        event_type=event_type,
        entity_id=entity_id,
        actor=actor,
        details=details,
        created_at=created_at,
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO audit_events "
            "(event_id,workspace_id,approval_id,entity_type,entity_id,event_type,actor,"
            "details,created_at,payload) "
            "VALUES (%s,%s,NULL,'project',%s,%s,%s,%s,%s,%s)",
            (
                event.id,
                workspace_id,
                entity_id,
                event_type,
                actor,
                Jsonb(details),
                created_at,
                Jsonb(event.model_dump(mode="json")),
            ),
        )
    return event


def test_postgres_audit_reads_are_workspace_scoped_and_newest_first(
    postgres_audit_repository,
) -> None:
    repository, connect, workspace_a, workspace_b = postgres_audit_repository
    now = now_utc()
    older = _insert_event(
        connect,
        workspace_id=workspace_a,
        event_type="project.created",
        entity_id="project-old",
        actor="owner-a",
        details={"source": "manual"},
        created_at=now - timedelta(minutes=1),
    )
    newer = _insert_event(
        connect,
        workspace_id=workspace_a,
        event_type="project.updated",
        entity_id="project-new",
        actor="owner-a",
        details={"source": "connector"},
        created_at=now,
    )
    other = _insert_event(
        connect,
        workspace_id=workspace_b,
        event_type="project.created",
        entity_id="project-other",
        actor="owner-b",
        details={"source": "manual"},
        created_at=now + timedelta(minutes=1),
    )

    assert repository.list_for_workspace(workspace_a) == [newer, older]
    assert repository.list_for_workspace(workspace_b) == [other]


def test_postgres_audit_filters_and_limit_are_applied_in_database(
    postgres_audit_repository,
) -> None:
    repository, connect, workspace_a, _workspace_b = postgres_audit_repository
    now = now_utc()
    first = _insert_event(
        connect,
        workspace_id=workspace_a,
        event_type="approval.decision",
        entity_id="approval-1",
        actor="reviewer@example.test",
        details={"decision": "approve"},
        created_at=now - timedelta(seconds=2),
    )
    second = _insert_event(
        connect,
        workspace_id=workspace_a,
        event_type="approval.decision",
        entity_id="approval-2",
        actor="reviewer@example.test",
        details={"decision": "deny"},
        created_at=now - timedelta(seconds=1),
    )
    _insert_event(
        connect,
        workspace_id=workspace_a,
        event_type="agent.created",
        entity_id="agent-1",
        actor="owner@example.test",
        details={"decision": "approve"},
        created_at=now,
    )

    result = repository.list_for_workspace(
        workspace_a,
        event_type="approval.decision",
        actor="reviewer@example.test",
        detail_key="decision",
        detail_value="approve",
        created_after=now - timedelta(seconds=3),
        created_before=now,
        limit=1,
    )
    assert result == [first]
    assert second.id not in {event.id for event in result}


def test_postgres_audit_get_does_not_disclose_other_workspace_event(
    postgres_audit_repository,
) -> None:
    repository, connect, workspace_a, workspace_b = postgres_audit_repository
    event = _insert_event(
        connect,
        workspace_id=workspace_a,
        event_type="agent.created",
        entity_id="agent-1",
        actor="owner-a",
        details={},
        created_at=now_utc(),
    )

    assert repository.get_for_workspace(event.id, workspace_b) is None
    assert repository.get_for_workspace(event.id, workspace_a) == event


def test_postgres_audit_rejects_unscoped_or_invalid_queries(
    postgres_audit_repository,
) -> None:
    repository, _connect, workspace_a, _workspace_b = postgres_audit_repository

    with pytest.raises(ValueError, match="require a workspace"):
        repository.list_for_workspace("")
    with pytest.raises(ValueError, match="between 1 and 1001"):
        repository.list_for_workspace(workspace_a, limit=1_002)
    with pytest.raises(ValueError, match="requires a detail key"):
        repository.list_for_workspace(workspace_a, detail_value="approve")
    with pytest.raises(ValueError, match="created_after"):
        repository.list_for_workspace(
            workspace_a,
            created_after=now_utc(),
            created_before=now_utc() - timedelta(seconds=1),
        )


def test_repository_bundle_exposes_audit_adapter_over_supplied_connector() -> None:
    def connector() -> None:
        return None

    repositories = ApplicationRepositories.from_connector(connector)
    assert isinstance(repositories.audit_events, PostgresAuditEventRepository)
    assert repositories.audit_events._connect is connector


def test_postgres_audit_reader_fails_on_payload_column_mismatch(
    postgres_audit_repository,
) -> None:
    repository, connect, workspace_a, _workspace_b = postgres_audit_repository
    event = _insert_event(
        connect,
        workspace_id=workspace_a,
        event_type="project.created",
        entity_id="project-1",
        actor="owner-a",
        details={"name": "Project"},
        created_at=now_utc(),
    )
    with connect() as conn:
        conn.execute("ALTER TABLE audit_events DISABLE TRIGGER audit_event_immutable")
        conn.execute(
            "UPDATE audit_events SET actor='different-actor' WHERE event_id=%s",
            (event.id,),
        )
        conn.execute("ALTER TABLE audit_events ENABLE TRIGGER audit_event_immutable")

    with pytest.raises(PostgresAuditRepositoryError, match="does not match"):
        repository.get_for_workspace(event.id, workspace_a)
