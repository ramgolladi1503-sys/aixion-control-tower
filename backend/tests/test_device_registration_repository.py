from __future__ import annotations

import os
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from threading import Barrier

import psycopg
import pytest
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app.database_migrations import run_migrations
from app.device_registration_repository import SQLiteDeviceRegistrationRepository
from app.models import DeviceRegistration, SessionToken, Workspace, now_utc
from app.notification_outbox_repository import PostgresNotificationOutboxRepository
from app.postgres_database import run_postgres_migrations


@pytest.fixture
def sqlite_device_repository(
    tmp_path: Path,
) -> tuple[SQLiteDeviceRegistrationRepository, Callable[[], sqlite3.Connection]]:
    database_path = tmp_path / "device-registration.sqlite3"

    def connect() -> sqlite3.Connection:
        return sqlite3.connect(database_path, timeout=5.0)

    with connect() as connection:
        run_migrations(connection)
    return SQLiteDeviceRegistrationRepository(connect), connect


@pytest.fixture
def postgres_device_repository() -> Iterator[
    tuple[PostgresNotificationOutboxRepository, Callable[[], Connection], tuple[str, str]]
]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL integration tests")
    schema = f"ct_devices_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    workspaces = (
        Workspace(name="Account A", owner_user_id="owner-a"),
        Workspace(name="Account B", owner_user_id="owner-b"),
    )
    try:
        run_postgres_migrations(connect)
        with connect() as connection:
            for workspace, user_id in zip(workspaces, ("owner-a", "owner-b"), strict=True):
                connection.execute(
                    "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
                    (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
                )
                payload = {
                    "id": user_id,
                    "workspace_id": workspace.id,
                    "email": f"{user_id}@example.test",
                    "role": "OWNER",
                    "disabled": False,
                    "email_verified": True,
                }
                connection.execute(
                    "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,email_verified,payload) "
                    "VALUES(%s,%s,%s,%s,'OWNER',FALSE,TRUE,%s)",
                    (
                        user_id,
                        workspace.id,
                        payload["email"],
                        payload["email"],
                        Jsonb(payload),
                    ),
                )
                session = _session_for_test_user(user_id)
                connection.execute(
                    "INSERT INTO sessions(session_id,user_id,token_hash,created_at,expires_at,revoked,payload) "
                    "VALUES(%s,%s,%s,%s,%s,FALSE,%s)",
                    (
                        session.id,
                        session.user_id,
                        session.token_hash,
                        session.created_at,
                        session.expires_at,
                        Jsonb(session.model_dump(mode="json")),
                    ),
                )
            disabled_id = "disabled-reviewer"
            disabled_email = f"{disabled_id}@example.test"
            connection.execute(
                "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,email_verified,payload) "
                "VALUES(%s,%s,%s,%s,'REVIEWER',TRUE,TRUE,%s)",
                (
                    disabled_id,
                    workspaces[1].id,
                    disabled_email,
                    disabled_email,
                    Jsonb(
                        {
                            "id": disabled_id,
                            "workspace_id": workspaces[1].id,
                            "email": disabled_email,
                            "role": "REVIEWER",
                            "disabled": True,
                            "email_verified": True,
                        }
                    ),
                ),
            )
            disabled_session = _session_for_test_user(disabled_id)
            connection.execute(
                "INSERT INTO sessions(session_id,user_id,token_hash,created_at,expires_at,revoked,payload) "
                "VALUES(%s,%s,%s,%s,%s,FALSE,%s)",
                (
                    disabled_session.id,
                    disabled_session.user_id,
                    disabled_session.token_hash,
                    disabled_session.created_at,
                    disabled_session.expires_at,
                    Jsonb(disabled_session.model_dump(mode="json")),
                ),
            )
        yield (
            PostgresNotificationOutboxRepository(connect),
            connect,
            (workspaces[0].id, workspaces[1].id),
        )
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _session_for_test_user(user_id: str) -> SessionToken:
    return SessionToken(
        id=f"session-{user_id}",
        user_id=user_id,
        token_hash=f"test-session-token-hash-{user_id}",
        expires_at=now_utc() + timedelta(days=1),
    )


def _persisted_sqlite_devices(connect: Callable[[], sqlite3.Connection]) -> dict[str, str]:
    with connect() as connection:
        return dict(
            connection.execute(
                "SELECT entity_id,payload FROM kv_store WHERE entity_type='device_registration'"
            ).fetchall()
        )


def test_sqlite_registration_and_audit_commit_without_rewriting_other_rows(
    sqlite_device_repository: tuple[
        SQLiteDeviceRegistrationRepository, Callable[[], sqlite3.Connection]
    ],
) -> None:
    repository, connect = sqlite_device_repository
    with connect() as connection:
        connection.execute(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES('project','unrelated','{}')"
        )
    device = DeviceRegistration(
        id="device-a",
        user_id="owner-a",
        workspace_id="workspace-a",
        token="opaque-token-a",
        app_version="1.2.3",
    )

    mutation = repository.register(device)

    assert mutation.device == device
    assert mutation.audit_event is not None
    assert mutation.audit_event.event_type == "notification.device_registered"
    assert mutation.audit_event.actor == "owner-a"
    persisted = _persisted_sqlite_devices(connect)
    assert set(persisted) == {device.id}
    with connect() as connection:
        assert connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type='project' AND entity_id='unrelated'"
        ).fetchone() == ("{}",)
        audit_count = connection.execute(
            "SELECT count(*) FROM kv_store WHERE entity_type='audit_event'"
        ).fetchone()[0]
    assert audit_count == 1


def test_sqlite_registration_reassigns_token_and_removes_legacy_duplicates(
    sqlite_device_repository: tuple[
        SQLiteDeviceRegistrationRepository, Callable[[], sqlite3.Connection]
    ],
) -> None:
    repository, connect = sqlite_device_repository
    first = DeviceRegistration(
        id="device-a", user_id="owner-a", workspace_id="workspace-a", token="shared-token"
    )
    duplicate = DeviceRegistration(
        id="device-b", user_id="owner-a", workspace_id="workspace-a", token="shared-token"
    )
    with connect() as connection:
        for device in (first, duplicate):
            connection.execute(
                "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
                ("device_registration", device.id, device.model_dump_json()),
            )
    requested = DeviceRegistration(
        user_id="owner-b",
        workspace_id="workspace-b",
        token="shared-token",
        platform="android",
        app_version="2.0",
    )

    mutation = repository.register(requested)

    assert mutation.device.id == first.id
    assert mutation.device.user_id == "owner-b"
    assert mutation.device.workspace_id == "workspace-b"
    assert mutation.device.created_at == first.created_at
    assert mutation.removed_device_ids == (duplicate.id,)
    assert mutation.audit_event is not None
    assert mutation.audit_event.event_type == "notification.device_reassigned"
    persisted = _persisted_sqlite_devices(connect)
    assert set(persisted) == {first.id}
    assert DeviceRegistration.model_validate_json(persisted[first.id]).user_id == "owner-b"


def test_sqlite_registration_rolls_back_device_when_audit_insert_fails(
    sqlite_device_repository: tuple[
        SQLiteDeviceRegistrationRepository, Callable[[], sqlite3.Connection]
    ],
) -> None:
    repository, connect = sqlite_device_repository
    with connect() as connection:
        connection.execute(
            "CREATE TRIGGER reject_device_audit BEFORE INSERT ON kv_store "
            "WHEN NEW.entity_type='audit_event' BEGIN SELECT RAISE(ABORT,'audit unavailable'); END"
        )
    device = DeviceRegistration(
        user_id="owner-a", workspace_id="workspace-a", token="token-a"
    )

    with pytest.raises(sqlite3.IntegrityError, match="audit unavailable"):
        repository.register(device)

    assert _persisted_sqlite_devices(connect) == {}
    with connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM kv_store WHERE entity_type='audit_event'"
        ).fetchone()[0] == 0


def test_sqlite_unregister_is_exact_owner_scoped_and_audited(
    sqlite_device_repository: tuple[
        SQLiteDeviceRegistrationRepository, Callable[[], sqlite3.Connection]
    ],
) -> None:
    repository, connect = sqlite_device_repository
    device = DeviceRegistration(
        id="device-a", user_id="owner-a", workspace_id="workspace-a", token="token-a"
    )
    repository.register(device)

    assert repository.unregister(
        device_id=device.id, user_id="owner-a", workspace_id="workspace-b"
    ) is None
    assert repository.unregister(
        device_id=device.id, user_id="owner-b", workspace_id="workspace-a"
    ) is None
    assert set(_persisted_sqlite_devices(connect)) == {device.id}
    mutation = repository.unregister(
        device_id=device.id, user_id="owner-a", workspace_id="workspace-a"
    )

    assert mutation is not None and mutation.device.id == device.id
    assert mutation.audit_event is not None
    assert mutation.audit_event.event_type == "notification.device_unregistered"
    assert _persisted_sqlite_devices(connect) == {}


def test_sqlite_concurrent_registration_serializes_same_token(
    sqlite_device_repository: tuple[
        SQLiteDeviceRegistrationRepository, Callable[[], sqlite3.Connection]
    ],
) -> None:
    repository, connect = sqlite_device_repository
    barrier = Barrier(2)

    def register(user_id: str) -> str:
        barrier.wait(timeout=5)
        return repository.register(
            DeviceRegistration(
                user_id=user_id,
                workspace_id=f"workspace-{user_id}",
                token="one-concurrently-registered-token",
            )
        ).device.id

    with ThreadPoolExecutor(max_workers=2) as pool:
        resulting_ids = list(pool.map(register, ("owner-a", "owner-b")))

    assert resulting_ids[0] == resulting_ids[1]
    assert len(_persisted_sqlite_devices(connect)) == 1
    with connect() as connection:
        events = connection.execute(
            "SELECT json_extract(payload,'$.event_type') FROM kv_store "
            "WHERE entity_type='audit_event' ORDER BY rowid"
        ).fetchall()
    assert {event[0] for event in events} == {
        "notification.device_registered",
        "notification.device_reassigned",
    }
    assert len(events) == 2


def test_postgres_registration_reassigns_and_unregisters_with_atomic_audit(
    postgres_device_repository: tuple[
        PostgresNotificationOutboxRepository,
        Callable[[], Connection],
        tuple[str, str],
    ],
) -> None:
    repository, connect, (workspace_a, workspace_b) = postgres_device_repository
    device = DeviceRegistration(
        user_id="owner-a",
        workspace_id=workspace_a,
        session_id=_session_for_test_user("owner-a").id,
        token=f"token-{uuid.uuid4().hex}",
    )

    created = repository.register_device(device)
    same_owner = repository.register_device(
        device.model_copy(update={"app_version": "1.1"})
    )
    reassigned = repository.register_device(
        DeviceRegistration(
            user_id="owner-b",
            workspace_id=workspace_b,
            session_id=_session_for_test_user("owner-b").id,
            token=device.token,
            app_version="2.0",
        )
    )

    assert created.audit_event is not None
    assert created.audit_event.event_type == "notification.device_registered"
    assert same_owner.device.id == created.device.id
    assert same_owner.device.app_version == "1.1"
    assert same_owner.audit_event is None
    assert reassigned.device.id == created.device.id
    assert reassigned.device.user_id == "owner-b"
    assert reassigned.device.workspace_id == workspace_b
    assert reassigned.audit_event is not None
    assert reassigned.audit_event.event_type == "notification.device_reassigned"
    assert repository.unregister_device(
        device_id=device.id, user_id="owner-b", workspace_id=workspace_a
    ) is None
    assert repository.unregister_device(
        device_id=device.id, user_id="owner-a", workspace_id=workspace_b
    ) is None
    removed = repository.unregister_device(
        device_id=device.id, user_id="owner-b", workspace_id=workspace_b
    )
    assert removed is not None and removed.audit_event is not None
    assert removed.audit_event.event_type == "notification.device_unregistered"
    with connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM device_registrations WHERE device_id=%s", (device.id,)
        ).fetchone()[0] == 0
        events = connection.execute(
            "SELECT event_type FROM audit_events WHERE entity_type='device_registration' "
            "AND entity_id=%s ORDER BY created_at,event_id",
            (device.id,),
        ).fetchall()
    assert [row[0] for row in events] == [
        "notification.device_registered",
        "notification.device_reassigned",
        "notification.device_unregistered",
    ]


def test_postgres_registration_rejects_disabled_or_wrong_workspace_owner(
    postgres_device_repository: tuple[
        PostgresNotificationOutboxRepository,
        Callable[[], Connection],
        tuple[str, str],
    ],
) -> None:
    repository, _, (workspace_a, workspace_b) = postgres_device_repository
    with pytest.raises(PermissionError, match="outside its workspace"):
        repository.register_device(
            DeviceRegistration(
                user_id="owner-b",
                workspace_id=workspace_a,
                session_id=_session_for_test_user("owner-b").id,
                token=f"token-{uuid.uuid4().hex}",
            )
        )
    with pytest.raises(PermissionError, match="disabled or outside"):
        repository.register_device(
            DeviceRegistration(
                user_id="disabled-reviewer",
                workspace_id=workspace_b,
                session_id=_session_for_test_user("disabled-reviewer").id,
                token=f"token-{uuid.uuid4().hex}",
            )
        )


def test_postgres_concurrent_registration_serializes_same_token(
    postgres_device_repository: tuple[
        PostgresNotificationOutboxRepository,
        Callable[[], Connection],
        tuple[str, str],
    ],
) -> None:
    repository, connect, (workspace_a, workspace_b) = postgres_device_repository
    token = f"token-{uuid.uuid4().hex}"
    barrier = Barrier(2)

    def register(user_id: str, workspace_id: str) -> str:
        barrier.wait(timeout=5)
        return repository.register_device(
            DeviceRegistration(
                user_id=user_id,
                workspace_id=workspace_id,
                session_id=_session_for_test_user(user_id).id,
                token=token,
            )
        ).device.id

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(register, "owner-a", workspace_a)
        second = pool.submit(register, "owner-b", workspace_b)
        resulting_ids = (first.result(timeout=10), second.result(timeout=10))

    assert resulting_ids[0] == resulting_ids[1]
    with connect() as connection:
        devices = connection.execute(
            "SELECT device_id FROM device_registrations WHERE token=%s", (token,)
        ).fetchall()
        events = connection.execute(
            "SELECT event_type FROM audit_events WHERE entity_id=%s "
            "AND entity_type='device_registration' ORDER BY created_at,event_id",
            (resulting_ids[0],),
        ).fetchall()
    assert devices == [(resulting_ids[0],)]
    assert {event[0] for event in events} == {
        "notification.device_registered",
        "notification.device_reassigned",
    }
    assert len(events) == 2


def test_postgres_registration_rolls_back_when_audit_insert_fails(
    postgres_device_repository: tuple[
        PostgresNotificationOutboxRepository,
        Callable[[], Connection],
        tuple[str, str],
    ],
) -> None:
    repository, connect, (workspace_a, _) = postgres_device_repository
    with connect() as connection:
        connection.execute(
            "CREATE FUNCTION reject_device_audit() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN IF NEW.entity_type='device_registration' THEN RAISE EXCEPTION 'audit unavailable'; "
            "END IF; RETURN NEW; END $$"
        )
        connection.execute(
            "CREATE TRIGGER reject_device_audit BEFORE INSERT ON audit_events "
            "FOR EACH ROW EXECUTE FUNCTION reject_device_audit()"
        )
    device = DeviceRegistration(
        user_id="owner-a",
        workspace_id=workspace_a,
        session_id=_session_for_test_user("owner-a").id,
        token=f"token-{uuid.uuid4().hex}",
    )

    with pytest.raises(psycopg.Error, match="audit unavailable"):
        repository.register_device(device)

    with connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM device_registrations WHERE token=%s", (device.token,)
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM audit_events WHERE entity_id=%s", (device.id,)
        ).fetchone()[0] == 0
