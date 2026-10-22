from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier

import psycopg
import pytest
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from app.approval_repository import PostgresApprovalDecisionRepository
from app.models import (
    ApprovalRequest,
    ApprovalStatus,
    AuditEvent,
    DeviceRegistration,
    FileChange,
    Notification,
    Project,
    RiskAssessment,
    RiskLevel,
    User,
    UserRole,
    Workspace,
    now_utc,
)
from app.notification_outbox_repository import PostgresNotificationOutboxRepository
from app.postgres_notification_dispatcher import dispatch_one_postgres_push
from app.postgres_database import run_postgres_migrations


@pytest.fixture
def outbox_repository() -> Iterator[
    tuple[PostgresNotificationOutboxRepository, Callable[[], Connection]]
]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL integration tests")
    schema = f"ct_outbox_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    try:
        run_postgres_migrations(connect)
        yield PostgresNotificationOutboxRepository(connect), connect
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _seed_notification(
    connect: Callable[[], Connection],
    *,
    user_id: str | None = None,
    created_at: datetime | None = None,
) -> tuple[Workspace, ApprovalRequest, Notification]:
    suffix = uuid.uuid4().hex
    workspace = Workspace(
        id=f"workspace_{suffix}", name="Outbox workspace", owner_user_id=f"owner_{suffix}"
    )
    project = Project(
        id=f"project_{suffix}", name="Outbox project", workspace_id=workspace.id
    )
    approval = ApprovalRequest(
        id=f"approval_{suffix}",
        project_id=project.id,
        workspace_id=workspace.id,
        title="Outbox approval",
        summary="Durably notify the reviewer.",
        target_branch="main",
        files=[FileChange(path="README.md", diff="+approved")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.REQUESTED,
    )
    notification = Notification(
        id=f"notification_{suffix}",
        title="Approval needed",
        body="Open the app to review.",
        entity_type="approval_request",
        entity_id=approval.id,
        workspace_id=workspace.id,
        user_id=user_id,
        created_at=created_at or now_utc(),
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
            (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
        )
        conn.execute(
            "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
            (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))),
        )
        if user_id:
            user = _insert_user(conn, workspace, user_id)
            assert user.id == user_id
    PostgresApprovalDecisionRepository(connect).create(
        approval=approval,
        audit_event=AuditEvent(
            event_type="approval.created",
            entity_id=approval.id,
            workspace_id=workspace.id,
            details={"project_id": project.id},
        ),
        notification=notification,
    )
    return workspace, approval, notification


def test_metrics_snapshot_is_workspace_scoped_and_reports_oldest_queue_age(
    outbox_repository: tuple[PostgresNotificationOutboxRepository, Callable[[], Connection]],
) -> None:
    repository, connect = outbox_repository
    now = now_utc()
    workspace, _, notification = _seed_notification(
        connect, created_at=now - timedelta(seconds=120)
    )
    other_workspace, _, _ = _seed_notification(
        connect, created_at=now - timedelta(seconds=600)
    )

    snapshot = repository.metrics_snapshot(workspace_id=workspace.id, now=now)
    empty = repository.metrics_snapshot(workspace_id="workspace-with-no-notifications", now=now)

    assert snapshot.status_counts["PENDING"] == 1
    assert snapshot.queue_depth == 1
    assert snapshot.oldest_pending_age_seconds == pytest.approx(120)
    assert empty.queue_depth == 0
    assert empty.oldest_pending_age_seconds == 0
    assert sum(empty.status_counts.values()) == 0
    assert notification.workspace_id == workspace.id
    assert other_workspace.id != workspace.id


@pytest.mark.parametrize("workspace_id", ["", "   "])
def test_metrics_snapshot_requires_explicit_workspace_scope(
    outbox_repository: tuple[PostgresNotificationOutboxRepository, Callable[[], Connection]],
    workspace_id: str,
) -> None:
    repository, _ = outbox_repository
    with pytest.raises(ValueError, match="require a workspace"):
        repository.metrics_snapshot(workspace_id=workspace_id)


def _insert_user(conn: Connection, workspace: Workspace, user_id: str, *, disabled: bool = False) -> User:
    user = User(
        id=user_id,
        email=f"{user_id}@example.test",
        display_name=user_id,
        role=UserRole.OWNER,
        password_hash="test-hash",
        password_salt="test-salt",
        workspace_id=workspace.id,
        disabled=disabled,
        email_verified=True,
    )
    conn.execute(
        "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,email_verified,payload) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
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
    return user


def _insert_device(conn: Connection, workspace: Workspace, user_id: str, token: str) -> DeviceRegistration:
    device = DeviceRegistration(
        id=f"device_{uuid.uuid4().hex}",
        user_id=user_id,
        workspace_id=workspace.id,
        token=token,
    )
    conn.execute(
        "INSERT INTO device_registrations "
        "(device_id,user_id,workspace_id,platform,token,app_version,created_at,updated_at,payload) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            device.id,
            device.user_id,
            device.workspace_id,
            device.platform,
            device.token,
            device.app_version,
            device.created_at,
            device.updated_at,
            Jsonb(device.model_dump(mode="json")),
        ),
    )
    return device


def _persisted_notification(connect: Callable[[], Connection], notification_id: str) -> Notification:
    with connect() as conn:
        row = conn.execute(
            "SELECT payload FROM notifications WHERE notification_id=%s", (notification_id,)
        ).fetchone()
    assert row is not None
    return Notification.model_validate(row[0])


def test_claim_uses_attempt_fencing_and_rejects_stale_worker_result(
    outbox_repository: tuple[PostgresNotificationOutboxRepository, Callable[[], Connection]],
) -> None:
    repository, _ = outbox_repository
    workspace, _, notification = _seed_notification(outbox_repository[1])
    with outbox_repository[1]() as conn:
        _insert_user(conn, workspace, "push-owner")
        device = _insert_device(conn, workspace, "push-owner", "dead-token")
    started_at = now_utc()
    first = repository.claim_next_due(now=started_at, max_attempts=3, lease_seconds=30)
    assert first is not None and first.id == notification.id
    assert first.push_attempts == 1 and first.push_status == "SENDING"

    second_started_at = started_at + timedelta(seconds=31)
    second = repository.claim_next_due(
        now=second_started_at, max_attempts=3, lease_seconds=30
    )
    assert second is not None and second.id == notification.id
    assert second.push_attempts == 2 and second.push_status == "SENDING"

    stale_result = repository.record_outcome(
        notification_id=notification.id,
        expected_attempts=first.push_attempts,
        push_status="SENT",
        push_error=None,
        next_attempt_at=None,
        completed_at=second_started_at + timedelta(seconds=1),
        invalid_devices=((device.id, "dead-token"),),
    )
    assert stale_result is None
    with outbox_repository[1]() as conn:
        assert conn.execute(
            "SELECT token FROM device_registrations WHERE device_id=%s", (device.id,)
        ).fetchone() == ("dead-token",)
        assert conn.execute(
            "SELECT 1 FROM audit_events WHERE entity_id=%s "
            "AND event_type='notification.device_token_invalidated'",
            (device.id,),
        ).fetchone() is None
    accepted_result = repository.record_outcome(
        notification_id=notification.id,
        expected_attempts=second.push_attempts,
        push_status="SENT",
        push_error=None,
        next_attempt_at=None,
        completed_at=second_started_at + timedelta(seconds=2),
    )
    assert accepted_result is not None and accepted_result.push_status == "SENT"
    assert accepted_result.push_lease_expires_at is None


def test_retry_outcome_requires_future_retry_time_and_bounded_error_category(
    outbox_repository: tuple[PostgresNotificationOutboxRepository, Callable[[], Connection]],
) -> None:
    repository, connect = outbox_repository
    _, _, notification = _seed_notification(connect)
    with pytest.raises(ValueError, match="future retry time"):
        repository.record_outcome(
            notification_id=notification.id,
            expected_attempts=1,
            push_status="RETRY_WAIT",
            push_error="FCM_NETWORK_ERROR",
            next_attempt_at=now_utc(),
        )
    with pytest.raises(ValueError, match="bounded single-line"):
        repository.record_outcome(
            notification_id=notification.id,
            expected_attempts=1,
            push_status="SENT",
            push_error="provider detail\ncontains extra text",
            next_attempt_at=None,
        )


def test_claim_marks_retry_exhaustion_without_sending(
    outbox_repository: tuple[PostgresNotificationOutboxRepository, Callable[[], Connection]],
) -> None:
    repository, connect = outbox_repository
    _, _, notification = _seed_notification(connect)
    with connect() as conn:
        conn.execute(
            "UPDATE notifications SET attempts=2, payload=jsonb_set(payload,'{push_attempts}','2') "
            "WHERE notification_id=%s",
            (notification.id,),
        )

    result = repository.claim_next_due(max_attempts=2, lease_seconds=30)

    assert result is not None
    assert result.push_status == "FAILED"
    assert result.push_error == "FCM_RETRIES_EXHAUSTED"
    assert result.push_attempts == 2
    assert result.push_lease_expires_at is None


def test_claim_recovers_sending_row_with_missing_lease(
    outbox_repository: tuple[PostgresNotificationOutboxRepository, Callable[[], Connection]],
) -> None:
    repository, connect = outbox_repository
    _, _, notification = _seed_notification(connect)
    with connect() as conn:
        conn.execute(
            "UPDATE notifications SET status='SENDING', lease_expires_at=NULL, "
            "payload=jsonb_set(payload,'{push_status}','\"SENDING\"') "
            "WHERE notification_id=%s",
            (notification.id,),
        )

    result = repository.claim_next_due(max_attempts=3, lease_seconds=30)

    assert result is not None and result.id == notification.id
    assert result.push_status == "SENDING"
    assert result.push_attempts == 1
    assert result.push_lease_expires_at is not None


def test_concurrent_claims_skip_locked_rows_and_claim_distinct_notifications(
    outbox_repository: tuple[PostgresNotificationOutboxRepository, Callable[[], Connection]],
) -> None:
    repository, connect = outbox_repository
    _, _, first_notification = _seed_notification(connect)
    _, _, second_notification = _seed_notification(connect)
    barrier = Barrier(2)

    def claim() -> str | None:
        barrier.wait(timeout=5)
        result = repository.claim_next_due(max_attempts=3, lease_seconds=30)
        return result.id if result is not None else None

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(claim) for _ in range(2)]
        claimed_ids = [future.result(timeout=10) for future in futures]

    assert set(claimed_ids) == {first_notification.id, second_notification.id}


def test_device_targets_enforce_user_workspace_and_disabled_account_scope(
    outbox_repository: tuple[PostgresNotificationOutboxRepository, Callable[[], Connection]],
) -> None:
    repository, connect = outbox_repository
    workspace, _, notification = _seed_notification(connect, user_id="reviewer-a")
    other_workspace = Workspace(
        id=f"workspace_{uuid.uuid4().hex}", name="Other workspace", owner_user_id="owner-b"
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
            (other_workspace.id, Jsonb(other_workspace.model_dump(mode="json"))),
        )
        _insert_user(conn, workspace, "reviewer-b")
        _insert_user(conn, workspace, "disabled-reviewer", disabled=True)
        _insert_user(conn, other_workspace, "reviewer-other")
        _insert_device(conn, workspace, "reviewer-a", "token-a")
        _insert_device(conn, workspace, "reviewer-b", "token-b")
        _insert_device(conn, workspace, "disabled-reviewer", "token-disabled")
        _insert_device(conn, other_workspace, "reviewer-other", "token-other")

    user_scoped = repository.list_device_targets(notification)
    assert [device.token for device in user_scoped] == ["token-a"]

    workspace_notification = notification.model_copy(update={"user_id": None})
    workspace_targets = repository.list_device_targets(workspace_notification)
    assert {device.token for device in workspace_targets} == {"token-a", "token-b"}


def test_inbox_listing_and_read_state_are_workspace_and_user_scoped(
    outbox_repository: tuple[PostgresNotificationOutboxRepository, Callable[[], Connection]],
) -> None:
    repository, connect = outbox_repository
    workspace, _, notification = _seed_notification(connect, user_id="reviewer-a")
    other_workspace = Workspace(
        id=f"workspace_{uuid.uuid4().hex}", name="Other workspace", owner_user_id="owner-other"
    )
    with connect() as conn:
        _insert_user(conn, workspace, "reviewer-b")
        conn.execute(
            "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
            (other_workspace.id, Jsonb(other_workspace.model_dump(mode="json"))),
        )
        _insert_user(conn, other_workspace, "reviewer-other")

    assert [item.id for item in repository.list_for_user(
        user_id="reviewer-a", workspace_id=workspace.id
    )] == [notification.id]
    assert repository.list_for_user(
        user_id="reviewer-b", workspace_id=workspace.id
    ) == []
    with pytest.raises(PermissionError, match="outside its workspace"):
        repository.list_for_user(user_id="reviewer-other", workspace_id=workspace.id)

    assert repository.mark_read(
        notification_id=notification.id,
        user_id="reviewer-b",
        workspace_id=workspace.id,
    ) is None
    updated = repository.mark_read(
        notification_id=notification.id,
        user_id="reviewer-a",
        workspace_id=workspace.id,
    )
    assert updated is not None
    assert updated.status.value == "READ"
    assert _persisted_notification(connect, notification.id).status.value == "READ"


def test_dead_token_removal_and_audit_are_atomic_and_do_not_store_token(
    outbox_repository: tuple[PostgresNotificationOutboxRepository, Callable[[], Connection]],
) -> None:
    repository, connect = outbox_repository
    workspace, _, _ = _seed_notification(connect)
    token = "secret-device-token"
    with connect() as conn:
        _insert_user(conn, workspace, "device-owner")
        device = _insert_device(conn, workspace, "device-owner", token)

    assert not repository.invalidate_device_if_token_matches(
        device_id=device.id, token="different-token"
    )
    assert repository.invalidate_device_if_token_matches(device_id=device.id, token=token)
    assert not repository.invalidate_device_if_token_matches(device_id=device.id, token=token)
    with connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM device_registrations WHERE device_id=%s", (device.id,)
        ).fetchone() is None
        audit = conn.execute(
            "SELECT payload::text FROM audit_events "
            "WHERE entity_type='device_registration' AND entity_id=%s",
            (device.id,),
        ).fetchone()
    assert audit is not None
    assert token not in audit[0]


def test_dead_token_deletion_rolls_back_when_audit_insert_fails(
    outbox_repository: tuple[PostgresNotificationOutboxRepository, Callable[[], Connection]],
) -> None:
    repository, connect = outbox_repository
    workspace, _, _ = _seed_notification(connect)
    token = "rollback-device-token"
    with connect() as conn:
        _insert_user(conn, workspace, "device-owner")
        device = _insert_device(conn, workspace, "device-owner", token)
        conn.execute(
            "CREATE FUNCTION reject_device_token_audit() RETURNS trigger AS $$ "
            "BEGIN IF NEW.event_type='notification.device_token_invalidated' THEN "
            "RAISE EXCEPTION 'audit unavailable'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql"
        )
        conn.execute(
            "CREATE TRIGGER reject_device_token_audit BEFORE INSERT ON audit_events "
            "FOR EACH ROW EXECUTE FUNCTION reject_device_token_audit()"
        )

    with pytest.raises(psycopg.Error, match="audit unavailable"):
        repository.invalidate_device_if_token_matches(device_id=device.id, token=token)
    with connect() as conn:
        row = conn.execute(
            "SELECT token FROM device_registrations WHERE device_id=%s", (device.id,)
        ).fetchone()
    assert row == (token,)


def test_postgres_dispatcher_sends_and_records_durable_success(
    outbox_repository: tuple[PostgresNotificationOutboxRepository, Callable[[], Connection]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, connect = outbox_repository
    workspace, _, notification = _seed_notification(connect, user_id="push-owner")
    with connect() as conn:
        _insert_device(conn, workspace, "push-owner", "push-token")
    monkeypatch.setenv("AIXION_PUSH_DISPATCH_ENABLED", "true")
    captured: list[dict[str, object]] = []

    def sender(**kwargs: object) -> None:
        captured.append(kwargs)
        return None

    result = dispatch_one_postgres_push(
        repository, now=now_utc(), sender=sender
    )

    assert result == notification.id
    assert len(captured) == 1
    assert captured[0]["token"] == "push-token"
    data = captured[0]["data"]
    assert data == {
        "notification_id": notification.id,
        "entity_type": "approval_request",
        "entity_id": notification.entity_id,
    }
    stored = _persisted_notification(connect, notification.id)
    assert stored.push_status == "SENT"
    assert stored.push_attempts == 1


def test_postgres_dispatcher_retries_transient_failure_with_backoff(
    outbox_repository: tuple[PostgresNotificationOutboxRepository, Callable[[], Connection]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, connect = outbox_repository
    workspace, _, notification = _seed_notification(connect, user_id="push-owner")
    with connect() as conn:
        _insert_device(conn, workspace, "push-owner", "push-token")
    monkeypatch.setenv("AIXION_PUSH_DISPATCH_ENABLED", "true")
    started_at = now_utc()

    dispatch_one_postgres_push(
        repository,
        now=started_at,
        sender=lambda **_: "FCM_NETWORK_ERROR",
    )

    stored = _persisted_notification(connect, notification.id)
    assert stored.push_status == "RETRY_WAIT"
    assert stored.push_error == "FCM_NETWORK_ERROR"
    assert stored.push_next_attempt_at == started_at + timedelta(seconds=15)
    assert stored.push_lease_expires_at is None


def test_postgres_dispatcher_does_not_retry_after_partial_acceptance(
    outbox_repository: tuple[PostgresNotificationOutboxRepository, Callable[[], Connection]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, connect = outbox_repository
    workspace, _, notification = _seed_notification(connect)
    with connect() as conn:
        _insert_user(conn, workspace, "push-owner-a")
        _insert_user(conn, workspace, "push-owner-b")
        _insert_device(conn, workspace, "push-owner-a", "accepted-token")
        _insert_device(conn, workspace, "push-owner-b", "transient-token")
    monkeypatch.setenv("AIXION_PUSH_DISPATCH_ENABLED", "true")

    def sender(*, token: str, **_: object) -> str | None:
        return None if token == "accepted-token" else "FCM_NETWORK_ERROR"

    dispatch_one_postgres_push(repository, now=now_utc(), sender=sender)

    stored = _persisted_notification(connect, notification.id)
    assert stored.push_status == "SENT"
    assert stored.push_error == "PARTIAL_DELIVERY"
    assert stored.push_next_attempt_at is None


def test_postgres_dispatcher_removes_unregistered_token_and_records_failure(
    outbox_repository: tuple[PostgresNotificationOutboxRepository, Callable[[], Connection]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, connect = outbox_repository
    workspace, _, notification = _seed_notification(connect, user_id="push-owner")
    with connect() as conn:
        device = _insert_device(conn, workspace, "push-owner", "expired-token")
    monkeypatch.setenv("AIXION_PUSH_DISPATCH_ENABLED", "true")

    dispatch_one_postgres_push(
        repository,
        now=now_utc(),
        sender=lambda **_: "FCM_TOKEN_UNREGISTERED",
    )

    stored = _persisted_notification(connect, notification.id)
    assert stored.push_status == "FAILED"
    assert stored.push_error == "FCM_TOKEN_UNREGISTERED"
    with connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM device_registrations WHERE device_id=%s", (device.id,)
        ).fetchone() is None
        audit = conn.execute(
            "SELECT event_type FROM audit_events WHERE entity_type='device_registration' "
            "AND entity_id=%s",
            (device.id,),
        ).fetchone()
    assert audit == ("notification.device_token_invalidated",)


def test_postgres_dispatcher_rolls_back_outcome_and_token_cleanup_together_on_audit_failure(
    outbox_repository: tuple[PostgresNotificationOutboxRepository, Callable[[], Connection]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, connect = outbox_repository
    workspace, _, notification = _seed_notification(connect, user_id="push-owner")
    with connect() as conn:
        device = _insert_device(conn, workspace, "push-owner", "expired-token")
        conn.execute(
            "CREATE FUNCTION reject_dispatch_cleanup_audit() RETURNS trigger AS $$ "
            "BEGIN IF NEW.event_type='notification.device_token_invalidated' THEN "
            "RAISE EXCEPTION 'audit unavailable'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql"
        )
        conn.execute(
            "CREATE TRIGGER reject_dispatch_cleanup_audit BEFORE INSERT ON audit_events "
            "FOR EACH ROW EXECUTE FUNCTION reject_dispatch_cleanup_audit()"
        )
    monkeypatch.setenv("AIXION_PUSH_DISPATCH_ENABLED", "true")

    with pytest.raises(psycopg.Error, match="audit unavailable"):
        dispatch_one_postgres_push(
            repository,
            now=now_utc(),
            sender=lambda **_: "FCM_TOKEN_UNREGISTERED",
        )

    stored = _persisted_notification(connect, notification.id)
    assert stored.push_status == "SENDING"
    with connect() as conn:
        assert conn.execute(
            "SELECT token FROM device_registrations WHERE device_id=%s", (device.id,)
        ).fetchone() == ("expired-token",)
        assert conn.execute(
            "SELECT 1 FROM audit_events WHERE entity_id=%s "
            "AND event_type='notification.device_token_invalidated'",
            (device.id,),
        ).fetchone() is None


def test_postgres_dispatcher_keeps_replaced_token_when_old_token_is_rejected(
    outbox_repository: tuple[PostgresNotificationOutboxRepository, Callable[[], Connection]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, connect = outbox_repository
    workspace, _, notification = _seed_notification(connect, user_id="push-owner")
    with connect() as conn:
        device = _insert_device(conn, workspace, "push-owner", "old-token")
    monkeypatch.setenv("AIXION_PUSH_DISPATCH_ENABLED", "true")

    def sender(**_: object) -> str:
        # Simulate re-registration while the provider request is in flight.
        with connect() as conn:
            conn.execute(
                "UPDATE device_registrations SET token=%s, "
                "payload=jsonb_set(payload,'{token}',to_jsonb(%s::text)) "
                "WHERE device_id=%s",
                ("replacement-token", "replacement-token", device.id),
            )
        return "FCM_TOKEN_UNREGISTERED"

    dispatch_one_postgres_push(repository, now=now_utc(), sender=sender)

    stored = _persisted_notification(connect, notification.id)
    assert stored.push_status == "FAILED"
    with connect() as conn:
        assert conn.execute(
            "SELECT token FROM device_registrations WHERE device_id=%s", (device.id,)
        ).fetchone() == ("replacement-token",)
        assert conn.execute(
            "SELECT 1 FROM audit_events WHERE entity_id=%s "
            "AND event_type='notification.device_token_invalidated'",
            (device.id,),
        ).fetchone() is None
