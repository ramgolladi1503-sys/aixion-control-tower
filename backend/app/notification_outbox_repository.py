from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta

from psycopg import Connection
from psycopg.types.json import Jsonb

from .device_registration_repository import (
    DeviceRegistrationMutation,
    device_registration_audit,
)
from .models import AuditEvent, DeviceRegistration, Notification, NotificationStatus, now_utc

_FINAL_PUSH_STATES = {"SENT", "FAILED", "SKIPPED", "NO_DEVICE", "RETRY_WAIT"}
_PUSH_STATES = ("PENDING", "RETRY_WAIT", "SENDING", "SENT", "FAILED", "SKIPPED", "NO_DEVICE")
_QUEUED_PUSH_STATES = ("PENDING", "RETRY_WAIT", "SENDING")


@dataclass(frozen=True)
class NotificationOutboxMetricsSnapshot:
    """Bounded workspace aggregate; contains no IDs, tokens, or payload data."""

    status_counts: dict[str, int]
    queue_depth: int
    oldest_pending_age_seconds: float


class PostgresNotificationOutboxRepository:
    """Transactional outbox operations for a PostgreSQL-backed dispatcher.

    This repository deliberately does not select or send notifications itself.
    Provider I/O belongs outside its transactions; callers must persist the
    resulting status with the attempt number returned by ``claim_next_due``.
    """

    def __init__(self, connect: Callable[[], Connection]) -> None:
        self._connect = connect

    def metrics_snapshot(
        self,
        *,
        workspace_id: str,
        now: datetime | None = None,
        connection: Connection | None = None,
    ) -> NotificationOutboxMetricsSnapshot:
        """Return one-snapshot delivery aggregates for an authenticated workspace."""
        if not workspace_id or not workspace_id.strip():
            raise ValueError("Notification outbox metrics require a workspace")
        current_time = now or now_utc()
        if current_time.utcoffset() is None:
            raise ValueError("Notification outbox metric time must be timezone-aware")
        connection_context = nullcontext(connection) if connection is not None else self._connect()
        with connection_context as conn:
            rows = conn.execute(
                "WITH scoped AS MATERIALIZED ("
                "SELECT status,created_at FROM notifications WHERE workspace_id=%s"
                "), status_counts AS ("
                "SELECT status,COUNT(*)::bigint AS status_count FROM scoped GROUP BY status"
                "), queue AS ("
                "SELECT COUNT(*)::bigint AS queue_depth,MIN(created_at) AS oldest_pending_at "
                "FROM scoped WHERE status = ANY(%s)"
                ") SELECT status_counts.status,status_counts.status_count,queue.queue_depth,"
                "queue.oldest_pending_at FROM queue LEFT JOIN status_counts ON TRUE "
                "ORDER BY status_counts.status",
                (workspace_id, list(_QUEUED_PUSH_STATES)),
            ).fetchall()
        counts = {state: 0 for state in _PUSH_STATES}
        queue_depth = 0
        oldest_pending_at = None
        for status, count, depth, oldest in rows:
            if status is not None:
                if status not in counts:
                    raise RuntimeError("Notification outbox contains an unsupported delivery state")
                counts[status] = int(count)
            queue_depth = int(depth)
            oldest_pending_at = oldest
        oldest_age = (
            0.0
            if oldest_pending_at is None
            else max(0.0, (current_time - oldest_pending_at).total_seconds())
        )
        return NotificationOutboxMetricsSnapshot(
            status_counts=counts,
            queue_depth=queue_depth,
            oldest_pending_age_seconds=oldest_age,
        )

    def claim_next_due(
        self,
        *,
        now: datetime | None = None,
        max_attempts: int,
        lease_seconds: int,
    ) -> Notification | None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        current_time = now or now_utc()

        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload, notification_id, workspace_id, user_id, approval_id, "
                "task_id, entity_type, status, attempts, next_attempt_at, lease_expires_at "
                "FROM notifications WHERE "
                "(status IN ('PENDING','RETRY_WAIT') AND "
                "(next_attempt_at IS NULL OR next_attempt_at <= %s)) OR "
                "(status='SENDING' AND (lease_expires_at IS NULL OR lease_expires_at <= %s)) "
                "ORDER BY created_at, notification_id LIMIT 1 FOR UPDATE SKIP LOCKED",
                (current_time, current_time),
            ).fetchone()
            if row is None:
                return None

            notification = Notification.model_validate(row[0])
            self._validate_row(notification, row)
            if notification.push_attempts >= max_attempts:
                exhausted = notification.model_copy(
                    update={
                        "push_status": "FAILED",
                        "push_error": "FCM_RETRIES_EXHAUSTED",
                        "push_next_attempt_at": None,
                        "push_lease_expires_at": None,
                    }
                )
                self._write_claim(conn, exhausted)
                return exhausted

            claimed = notification.model_copy(
                update={
                    "push_status": "SENDING",
                    "push_attempts": notification.push_attempts + 1,
                    "push_error": None,
                    "push_next_attempt_at": None,
                    "push_lease_expires_at": current_time + timedelta(seconds=lease_seconds),
                }
            )
            self._write_claim(conn, claimed)
            return claimed

    def record_outcome(
        self,
        *,
        notification_id: str,
        expected_attempts: int,
        push_status: str,
        push_error: str | None,
        next_attempt_at: datetime | None,
        completed_at: datetime | None = None,
        invalid_devices: tuple[tuple[str, str], ...] = (),
    ) -> Notification | None:
        """Persist one claim result and matching dead-token cleanup atomically.

        ``invalid_devices`` contains device ID/token pairs reported unregistered
        by FCM during this attempt. Cleanup is conditional on the same active
        outbox claim, and each device is removed only if it still has that
        token. A stale claim performs neither mutation.
        """
        if expected_attempts < 1:
            raise ValueError("expected_attempts must be positive")
        if push_status not in _FINAL_PUSH_STATES:
            raise ValueError("Outbox outcome must be a terminal or retry-wait state")
        current_time = completed_at or now_utc()
        if push_status == "RETRY_WAIT":
            if next_attempt_at is None or next_attempt_at <= current_time:
                raise ValueError("Retry-wait outcome requires a future retry time")
            if not push_error:
                raise ValueError("Retry-wait outcome requires a bounded failure category")
        elif next_attempt_at is not None:
            raise ValueError("Only retry-wait outcomes may have a next attempt time")
        if push_error is not None and (
            len(push_error) > 160
            or any(ord(character) < 32 or ord(character) == 127 for character in push_error)
        ):
            raise ValueError("Push outcome error must be a bounded single-line diagnostic")
        if any(
            not device_id
            or not token
            or len(device_id) > 128
            or len(token) > 4096
            for device_id, token in invalid_devices
        ):
            raise ValueError("Invalid-device cleanup requires bounded device and token values")

        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload, notification_id, workspace_id, user_id, approval_id, "
                "task_id, entity_type, status, attempts, next_attempt_at, lease_expires_at "
                "FROM notifications WHERE notification_id=%s AND status='SENDING' "
                "AND attempts=%s AND lease_expires_at > %s FOR UPDATE",
                (notification_id, expected_attempts, current_time),
            ).fetchone()
            if row is None:
                return None
            notification = Notification.model_validate(row[0])
            self._validate_row(notification, row)
            for device_id, token in dict.fromkeys(invalid_devices):
                self._invalidate_device_if_token_matches(
                    conn,
                    device_id=device_id,
                    token=token,
                    occurred_at=current_time,
                )
            updated = notification.model_copy(
                update={
                    "push_status": push_status,
                    "push_error": push_error,
                    "push_next_attempt_at": next_attempt_at,
                    "push_lease_expires_at": None,
                }
            )
            update = conn.execute(
                "UPDATE notifications SET status=%s, next_attempt_at=%s, "
                "lease_expires_at=NULL, payload=%s WHERE notification_id=%s "
                "AND status='SENDING' AND attempts=%s AND lease_expires_at > %s",
                (
                    updated.push_status,
                    updated.push_next_attempt_at,
                    Jsonb(updated.model_dump(mode="json")),
                    notification_id,
                    expected_attempts,
                    current_time,
                ),
            )
            if update.rowcount != 1:
                raise RuntimeError("Notification claim changed during its locked outcome update")
            return updated

    def list_device_targets(self, notification: Notification) -> list[DeviceRegistration]:
        """Return enabled-user devices in exactly the notification's workspace."""
        if notification.workspace_id is None:
            raise ValueError("PostgreSQL push delivery requires a workspace-scoped notification")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT device.payload, device.device_id, device.user_id, "
                "device.workspace_id, device.token, device.platform, device.app_version "
                "FROM device_registrations AS device "
                "JOIN users AS account ON account.user_id=device.user_id "
                "AND account.workspace_id=device.workspace_id "
                "WHERE device.workspace_id=%s AND account.disabled=FALSE "
                "AND (%s::text IS NULL OR device.user_id=%s) "
                "ORDER BY device.device_id",
                (notification.workspace_id, notification.user_id, notification.user_id),
            ).fetchall()

        devices: list[DeviceRegistration] = []
        for row in rows:
            device = DeviceRegistration.model_validate(row[0])
            if (
                device.id != row[1]
                or device.user_id != row[2]
                or device.workspace_id != row[3]
                or device.token != row[4]
                or device.platform != row[5]
                or device.app_version != row[6]
                or device.workspace_id != notification.workspace_id
            ):
                raise ValueError("Device registration payload does not match its scoped row")
            devices.append(device)
        return devices

    def list_for_user(self, *, user_id: str, workspace_id: str) -> list[Notification]:
        """List only notifications visible to an active workspace member."""
        if not user_id or not workspace_id:
            raise ValueError("Notification listing requires user and workspace scope")
        with self._connect() as conn:
            account = conn.execute(
                "SELECT workspace_id,disabled,email_verified FROM users WHERE user_id=%s",
                (user_id,),
            ).fetchone()
            if (
                account is None
                or account[0] != workspace_id
                or account[1]
                or not account[2]
            ):
                raise PermissionError("Notification reader is unavailable outside its workspace")
            rows = conn.execute(
                "SELECT payload,notification_id,workspace_id,user_id,approval_id,task_id,"
                "entity_type,status,attempts,next_attempt_at,lease_expires_at "
                "FROM notifications WHERE workspace_id=%s AND (user_id IS NULL OR user_id=%s) "
                "ORDER BY created_at DESC,notification_id DESC",
                (workspace_id, user_id),
            ).fetchall()
        notifications: list[Notification] = []
        for row in rows:
            notification = Notification.model_validate(row[0])
            self._validate_row(notification, row)
            if notification.workspace_id != workspace_id or notification.user_id not in {
                None,
                user_id,
            }:
                raise ValueError("Notification payload crosses the authenticated workspace")
            notifications.append(notification)
        return notifications

    def mark_read(
        self,
        *,
        notification_id: str,
        user_id: str,
        workspace_id: str,
    ) -> Notification | None:
        """Persist a read state only for a notification visible to its owner."""
        if not notification_id or not user_id or not workspace_id:
            raise ValueError("Notification read requires notification, user, and workspace scope")
        with self._connect() as conn:
            account = conn.execute(
                "SELECT workspace_id,disabled,email_verified FROM users WHERE user_id=%s",
                (user_id,),
            ).fetchone()
            if (
                account is None
                or account[0] != workspace_id
                or account[1]
                or not account[2]
            ):
                raise PermissionError("Notification reader is unavailable outside its workspace")
            row = conn.execute(
                "SELECT payload,notification_id,workspace_id,user_id,approval_id,task_id,"
                "entity_type,status,attempts,next_attempt_at,lease_expires_at "
                "FROM notifications WHERE notification_id=%s FOR UPDATE",
                (notification_id,),
            ).fetchone()
            if row is None:
                return None
            notification = Notification.model_validate(row[0])
            self._validate_row(notification, row)
            if notification.workspace_id != workspace_id or notification.user_id not in {
                None,
                user_id,
            }:
                return None
            updated = notification.model_copy(update={"status": NotificationStatus.READ})
            result = conn.execute(
                "UPDATE notifications SET payload=%s WHERE notification_id=%s "
                "AND workspace_id=%s AND status=%s AND attempts=%s",
                (
                    Jsonb(updated.model_dump(mode="json")),
                    notification_id,
                    workspace_id,
                    notification.push_status,
                    notification.push_attempts,
                ),
            )
            if result.rowcount != 1:
                raise RuntimeError("Notification changed during its locked read-state update")
            return updated

    def register_device(self, device: DeviceRegistration) -> DeviceRegistrationMutation:
        """Persist an authenticated device owner and its audit record atomically."""
        if not device.user_id or not device.workspace_id:
            raise ValueError("PostgreSQL device registration requires user and workspace scope")
        with self._connect() as conn:
            # The unique token constraint is the final invariant. The advisory
            # lock makes owner reassignment and its audit event deterministic
            # when two accounts register the same token concurrently.
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (device.token,),
            )
            account = conn.execute(
                "SELECT workspace_id, disabled FROM users WHERE user_id=%s FOR SHARE",
                (device.user_id,),
            ).fetchone()
            if account is None or account[0] != device.workspace_id or account[1]:
                raise PermissionError("Device owner is disabled or outside its workspace")

            row = conn.execute(
                "SELECT payload, device_id, user_id, workspace_id, token, platform, app_version "
                "FROM device_registrations WHERE token=%s FOR UPDATE",
                (device.token,),
            ).fetchone()
            if row is None:
                persisted = device
                event = device_registration_audit("notification.device_registered", persisted)
                conn.execute(
                    "INSERT INTO device_registrations "
                    "(device_id,user_id,workspace_id,platform,token,app_version,created_at,updated_at,payload) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    self._device_columns(persisted),
                )
            else:
                existing = self._decode_device_row(row)
                persisted = existing.model_copy(
                    update={
                        "user_id": device.user_id,
                        "workspace_id": device.workspace_id,
                        "platform": device.platform,
                        "app_version": device.app_version,
                        "updated_at": device.updated_at,
                    }
                )
                changed_owner = (
                    existing.user_id != persisted.user_id
                    or existing.workspace_id != persisted.workspace_id
                )
                updated_row = conn.execute(
                    "UPDATE device_registrations SET user_id=%s,workspace_id=%s,platform=%s,"
                    "app_version=%s,updated_at=%s,payload=%s WHERE device_id=%s AND token=%s",
                    (
                        persisted.user_id,
                        persisted.workspace_id,
                        persisted.platform,
                        persisted.app_version,
                        persisted.updated_at,
                        Jsonb(persisted.model_dump(mode="json")),
                        persisted.id,
                        persisted.token,
                    ),
                )
                if updated_row.rowcount != 1:
                    raise RuntimeError("Device registration changed during its locked update")
                event = (
                    device_registration_audit("notification.device_reassigned", persisted)
                    if changed_owner
                    else None
                )

            if event is not None:
                self._insert_device_audit(conn, event)
            return DeviceRegistrationMutation(persisted, audit_event=event)

    def unregister_device(
        self,
        *,
        device_id: str,
        user_id: str,
    ) -> DeviceRegistrationMutation | None:
        """Delete only the exact owner's device and append audit in one transaction."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload, device_id, user_id, workspace_id, token, platform, app_version "
                "FROM device_registrations WHERE device_id=%s FOR UPDATE",
                (device_id,),
            ).fetchone()
            if row is None:
                return None
            device = self._decode_device_row(row)
            if device.user_id != user_id:
                return None
            deleted = conn.execute(
                "DELETE FROM device_registrations WHERE device_id=%s AND user_id=%s",
                (device_id, user_id),
            )
            if deleted.rowcount != 1:
                raise RuntimeError("Device registration changed during owner-scoped removal")
            event = device_registration_audit("notification.device_unregistered", device)
            self._insert_device_audit(conn, event)
            return DeviceRegistrationMutation(device, audit_event=event)

    @staticmethod
    def _device_columns(device: DeviceRegistration) -> tuple[object, ...]:
        return (
            device.id,
            device.user_id,
            device.workspace_id,
            device.platform,
            device.token,
            device.app_version,
            device.created_at,
            device.updated_at,
            Jsonb(device.model_dump(mode="json")),
        )

    @staticmethod
    def _decode_device_row(row: tuple[object, ...]) -> DeviceRegistration:
        device = DeviceRegistration.model_validate(row[0])
        if (
            device.id != row[1]
            or device.user_id != row[2]
            or device.workspace_id != row[3]
            or device.token != row[4]
            or device.platform != row[5]
            or device.app_version != row[6]
        ):
            raise ValueError("Device registration payload does not match its scoped row")
        return device

    @staticmethod
    def _insert_device_audit(conn: Connection, event: AuditEvent) -> None:
        conn.execute(
            "INSERT INTO audit_events "
            "(event_id,workspace_id,approval_id,entity_type,entity_id,event_type,"
            "actor,details,created_at,payload) "
            "VALUES(%s,%s,NULL,'device_registration',%s,%s,%s,%s,%s,%s)",
            (
                event.id,
                event.workspace_id,
                event.entity_id,
                event.event_type,
                event.actor,
                Jsonb(event.details),
                event.created_at,
                Jsonb(event.model_dump(mode="json")),
            ),
        )

    def invalidate_device_if_token_matches(
        self,
        *,
        device_id: str,
        token: str,
        occurred_at: datetime | None = None,
    ) -> bool:
        """Remove a still-matching dead token and append its audit event atomically."""
        current_time = occurred_at or now_utc()
        with self._connect() as conn:
            return self._invalidate_device_if_token_matches(
                conn,
                device_id=device_id,
                token=token,
                occurred_at=current_time,
            )

    @staticmethod
    def _invalidate_device_if_token_matches(
        conn: Connection,
        *,
        device_id: str,
        token: str,
        occurred_at: datetime,
    ) -> bool:
        row = conn.execute(
            "DELETE FROM device_registrations WHERE device_id=%s AND token=%s "
            "RETURNING payload, device_id, user_id, workspace_id, token, platform, app_version",
            (device_id, token),
        ).fetchone()
        if row is None:
            return False
        device = DeviceRegistration.model_validate(row[0])
        if (
            device.id != row[1]
            or device.user_id != row[2]
            or device.workspace_id != row[3]
            or device.token != row[4]
            or device.platform != row[5]
            or device.app_version != row[6]
            or device.workspace_id is None
        ):
            raise ValueError("Device registration payload does not match its scoped row")
        event = AuditEvent(
            event_type="notification.device_token_invalidated",
            actor="system:fcm",
            entity_id=device.id,
            workspace_id=device.workspace_id,
            details={"platform": device.platform},
            created_at=occurred_at,
        )
        conn.execute(
            "INSERT INTO audit_events "
            "(event_id,workspace_id,approval_id,entity_type,entity_id,event_type,"
            "actor,details,created_at,payload) "
            "VALUES(%s,%s,NULL,'device_registration',%s,%s,%s,%s,%s,%s)",
            (
                event.id,
                event.workspace_id,
                event.entity_id,
                event.event_type,
                event.actor,
                Jsonb(event.details),
                event.created_at,
                Jsonb(event.model_dump(mode="json")),
            ),
        )
        return True

    @staticmethod
    def _write_claim(conn: Connection, notification: Notification) -> None:
        update = conn.execute(
            "UPDATE notifications SET status=%s, attempts=%s, next_attempt_at=%s, "
            "lease_expires_at=%s, payload=%s WHERE notification_id=%s",
            (
                notification.push_status,
                notification.push_attempts,
                notification.push_next_attempt_at,
                notification.push_lease_expires_at,
                Jsonb(notification.model_dump(mode="json")),
                notification.id,
            ),
        )
        if update.rowcount != 1:
            raise RuntimeError("Notification disappeared during its locked outbox claim")

    @staticmethod
    def _validate_row(notification: Notification, row: tuple[object, ...]) -> None:
        if (
            notification.id != row[1]
            or notification.workspace_id != row[2]
            or notification.user_id != row[3]
            or notification.entity_id != (row[4] or row[5])
            or notification.entity_type != row[6]
            or notification.push_status != row[7]
            or notification.push_attempts != row[8]
            or notification.push_next_attempt_at != row[9]
            or notification.push_lease_expires_at != row[10]
        ):
            raise ValueError("Notification payload does not match its outbox row")
