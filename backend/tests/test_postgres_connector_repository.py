from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
import multiprocessing

import psycopg
import pytest
from psycopg import Connection, sql
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from app.connector_models import AgentConnector, ConnectorStatus
from app.connector_repository import (
    ConnectorRepositoryConflict,
    ConnectorRepositoryScopeError,
    PostgresConnectorRepository,
)
from app.models import AuditEvent, Project, User, UserRole, Workspace, now_utc
from app.postgres_database import run_postgres_migrations


_CONNECTOR_UPDATE_BARRIER: multiprocessing.synchronize.Barrier | None = None


def _initialize_connector_update_race(
    barrier: multiprocessing.synchronize.Barrier,
) -> None:
    global _CONNECTOR_UPDATE_BARRIER
    _CONNECTOR_UPDATE_BARRIER = barrier


def _update_connector_in_process(
    dsn: str,
    schema: str,
    connector_payload: dict[str, object],
    expected_updated_at: datetime,
    updated_at: datetime,
    status: str,
    audit_id: str,
) -> str:
    barrier = _CONNECTOR_UPDATE_BARRIER
    if barrier is None:
        raise RuntimeError("Connector update race barrier was not initialized")

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    connector = AgentConnector.model_validate(connector_payload).model_copy(
        update={"status": ConnectorStatus(status), "updated_at": updated_at}
    )
    audit_event = AuditEvent(
        id=audit_id,
        event_type="connector.updated",
        entity_id=connector.id,
        workspace_id=connector.workspace_id,
        actor="connector-owner@example.test",
        details={"status": status},
    )
    repository = PostgresConnectorRepository(connect)
    barrier.wait(timeout=10)
    try:
        repository.update(
            connector=connector,
            expected_updated_at=expected_updated_at,
            audit_event=audit_event,
        )
        return "updated"
    except ConnectorRepositoryConflict:
        return "conflict"


@pytest.fixture
def postgres_connector_repository() -> Iterator[
    tuple[PostgresConnectorRepository, Callable[[], Connection], str, str, str]
]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is not configured")
    schema = "connector_repo_" + uuid.uuid4().hex

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        run_postgres_migrations(connect)
        workspace = Workspace(
            id="workspace_connector_repository",
            name="Connector workspace",
            owner_user_id="user_connector_repository",
        )
        user = User(
            id=workspace.owner_user_id,
            email="connector-owner@example.test",
            role=UserRole.OWNER,
            password_hash="test-hash",
            password_salt="test-salt",
            workspace_id=workspace.id,
        )
        project = Project(
            id="project_connector_repository",
            name="Connector project",
            workspace_id=workspace.id,
            created_by_user_id=user.id,
        )
        with connect() as connection:
            connection.execute(
                "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
                (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
            )
            connection.execute(
                "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,"
                "email_verified,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    user.id,
                    workspace.id,
                    user.email,
                    user.email.lower(),
                    user.role.value,
                    user.disabled,
                    user.email_verified,
                    Jsonb(user.model_dump(mode="json")),
                ),
            )
            connection.execute(
                "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
                (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))),
            )
        yield PostgresConnectorRepository(connect), connect, workspace.id, user.id, project.id
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _connector(workspace_id: str, user_id: str, project_id: str) -> AgentConnector:
    return AgentConnector(
        id="connector_repository_test",
        name="Antigravity local hook",
        provider_label="ANTIGRAVITY",
        allowed_project_ids=[project_id],
        created_by_user_id=user_id,
        workspace_id=workspace_id,
    )


def _audit(connector: AgentConnector, *, event_id: str | None = None) -> AuditEvent:
    return AuditEvent(
        id=event_id or "audit_" + connector.id,
        event_type="connector.created",
        entity_id=connector.id,
        workspace_id=connector.workspace_id,
        actor="connector-owner@example.test",
        details={"provider_label": connector.provider_label.value},
    )


def test_postgres_connector_create_and_audit_are_durable_and_workspace_scoped(
    postgres_connector_repository: tuple[
        PostgresConnectorRepository, Callable[[], Connection], str, str, str
    ],
) -> None:
    repository, connect, workspace_id, user_id, project_id = postgres_connector_repository
    connector = _connector(workspace_id, user_id, project_id)
    audit_event = _audit(connector)

    repository.create(connector=connector, audit_event=audit_event)

    assert repository.get_for_workspace(connector.id, workspace_id=workspace_id) == connector
    assert repository.get_for_workspace(connector.id, workspace_id="another_workspace") is None
    assert repository.get_for_authentication(connector.id) == connector
    assert repository.list_for_workspace(workspace_id) == [connector]
    with connect() as connection:
        assert connection.execute(
            "SELECT workspace_id,entity_type,entity_id,event_type FROM audit_events WHERE event_id=%s",
            (audit_event.id,),
        ).fetchone() == (workspace_id, "connector", connector.id, "connector.created")


def test_postgres_connector_listing_requires_workspace(
    postgres_connector_repository: tuple[
        PostgresConnectorRepository, Callable[[], Connection], str, str, str
    ],
) -> None:
    repository, _connect, _workspace_id, _user_id, _project_id = postgres_connector_repository
    with pytest.raises(ConnectorRepositoryScopeError, match="requires a workspace"):
        repository.list_for_workspace("")


def test_postgres_connector_rejects_project_outside_workspace_without_writes(
    postgres_connector_repository: tuple[
        PostgresConnectorRepository, Callable[[], Connection], str, str, str
    ],
) -> None:
    repository, connect, workspace_id, user_id, _project_id = postgres_connector_repository
    connector = _connector(workspace_id, user_id, "project_from_another_workspace")

    with pytest.raises(ConnectorRepositoryScopeError, match="allowed projects"):
        repository.create(connector=connector, audit_event=_audit(connector))

    with connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM agent_connectors WHERE connector_id=%s", (connector.id,)
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM audit_events WHERE entity_id=%s", (connector.id,)
        ).fetchone()[0] == 0


def test_postgres_connector_audit_failure_rolls_back_connector_insert(
    postgres_connector_repository: tuple[
        PostgresConnectorRepository, Callable[[], Connection], str, str, str
    ],
) -> None:
    repository, connect, workspace_id, user_id, project_id = postgres_connector_repository
    original = _connector(workspace_id, user_id, project_id)
    repository.create(connector=original, audit_event=_audit(original, event_id="audit_duplicate"))
    second = _connector(workspace_id, user_id, project_id).model_copy(
        update={"id": "connector_duplicate_audit"}
    )

    with pytest.raises(UniqueViolation):
        repository.create(connector=second, audit_event=_audit(second, event_id="audit_duplicate"))

    assert repository.get_for_authentication(second.id) is None
    with connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM audit_events WHERE entity_id=%s", (second.id,)
        ).fetchone()[0] == 0


def test_postgres_connector_update_uses_optimistic_timestamp_and_atomic_audit(
    postgres_connector_repository: tuple[
        PostgresConnectorRepository, Callable[[], Connection], str, str, str
    ],
) -> None:
    repository, connect, workspace_id, user_id, project_id = postgres_connector_repository
    original = _connector(workspace_id, user_id, project_id)
    repository.create(connector=original, audit_event=_audit(original))
    updated_at = now_utc() + timedelta(seconds=1)
    updated = original.model_copy(
        update={"status": ConnectorStatus.DISABLED, "updated_at": updated_at}
    )
    update_event = AuditEvent(
        id="audit_connector_disabled",
        event_type="connector.disabled",
        entity_id=updated.id,
        workspace_id=workspace_id,
        actor="connector-owner@example.test",
        details={"new_status": updated.status.value},
    )

    repository.update(
        connector=updated,
        expected_updated_at=original.updated_at,
        audit_event=update_event,
    )

    assert repository.get_for_workspace(original.id, workspace_id=workspace_id) == updated
    stale = updated.model_copy(update={"updated_at": updated_at + timedelta(seconds=1)})
    with pytest.raises(ConnectorRepositoryConflict, match="changed before update"):
        repository.update(
            connector=stale,
            expected_updated_at=original.updated_at,
            audit_event=AuditEvent(
                id="audit_connector_stale",
                event_type="connector.updated",
                entity_id=stale.id,
                workspace_id=workspace_id,
                actor="connector-owner@example.test",
            ),
        )
    with connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM audit_events WHERE event_id='audit_connector_stale'"
        ).fetchone()[0] == 0


def test_postgres_competing_connector_updates_have_one_winner_across_processes(
    postgres_connector_repository: tuple[
        PostgresConnectorRepository, Callable[[], Connection], str, str, str
    ],
) -> None:
    repository, connect, workspace_id, user_id, project_id = postgres_connector_repository
    original = _connector(workspace_id, user_id, project_id)
    repository.create(connector=original, audit_event=_audit(original))
    with connect() as connection:
        schema = connection.execute("SELECT current_schema()").fetchone()[0]

    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    assert dsn, "fixture requires AIXION_TEST_POSTGRES_DSN"
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    with ProcessPoolExecutor(
        max_workers=2,
        mp_context=context,
        initializer=_initialize_connector_update_race,
        initargs=(barrier,),
    ) as executor:
        futures = [
            executor.submit(
                _update_connector_in_process,
                dsn,
                schema,
                original.model_dump(mode="json"),
                original.updated_at,
                original.updated_at + timedelta(seconds=increment),
                status,
                audit_id,
            )
            for increment, status, audit_id in (
                (1, ConnectorStatus.DISABLED.value, "audit_process_update_disabled"),
                (2, ConnectorStatus.ENABLED.value, "audit_process_update_enabled"),
            )
        ]
        outcomes = [future.result(timeout=30) for future in futures]

    assert sorted(outcomes) == ["conflict", "updated"]
    updated = repository.get_for_workspace(original.id, workspace_id=workspace_id)
    assert updated is not None
    assert updated.status in {ConnectorStatus.DISABLED, ConnectorStatus.ENABLED}
    assert updated.updated_at in {
        original.updated_at + timedelta(seconds=1),
        original.updated_at + timedelta(seconds=2),
    }
    with connect() as connection:
        audit_count = connection.execute(
            "SELECT count(*) FROM audit_events WHERE entity_id=%s "
            "AND event_type='connector.updated'",
            (original.id,),
        ).fetchone()[0]
    assert audit_count == 1


def test_postgres_connector_update_requires_full_audit_identity(
    postgres_connector_repository: tuple[
        PostgresConnectorRepository, Callable[[], Connection], str, str, str
    ],
) -> None:
    repository, _connect, workspace_id, user_id, project_id = postgres_connector_repository
    connector = _connector(workspace_id, user_id, project_id)
    with pytest.raises(ConnectorRepositoryScopeError, match="audit event"):
        repository.create(
            connector=connector,
            audit_event=AuditEvent(
                event_type="connector.created",
                entity_id=connector.id,
                workspace_id="wrong_workspace",
                actor="connector-owner@example.test",
            ),
        )


def test_postgres_connector_migration_upgrades_a_pg0009_schema(
    postgres_connector_repository: tuple[
        PostgresConnectorRepository, Callable[[], Connection], str, str, str
    ],
) -> None:
    _repository, connect, _workspace_id, _user_id, _project_id = (
        postgres_connector_repository
    )
    # The fixture first provisions the current schema. Removing only the last
    # migration's objects inside this disposable schema recreates its pg_0009
    # shape; the runner must then apply pg_0010 as a forward upgrade.
    with connect() as connection:
        connection.execute("DROP TABLE agent_connectors")
        connection.execute(
            "DELETE FROM schema_migrations WHERE migration_id=%s",
            ("pg_0010_workspace_scoped_connectors",),
        )

    applied = run_postgres_migrations(connect)

    migration_ids = [migration["migration_id"] for migration in applied]
    assert "pg_0010_workspace_scoped_connectors" in migration_ids
    assert "pg_0012_workspace_scoped_mcp_pending_requests" in migration_ids
    with connect() as connection:
        assert connection.execute(
            "SELECT to_regclass('agent_connectors')"
        ).fetchone() == ("agent_connectors",)
