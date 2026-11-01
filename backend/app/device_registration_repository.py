from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from .models import AuditEvent, DeviceRegistration, now_utc


@dataclass(frozen=True)
class DeviceRegistrationMutation:
    """Committed registration state and cache projection details."""

    device: DeviceRegistration
    removed_device_ids: tuple[str, ...] = ()
    audit_event: AuditEvent | None = None


def device_registration_audit(event_type: str, device: DeviceRegistration) -> AuditEvent:
    if not device.user_id:
        raise ValueError("Device registration audit requires an authenticated user")
    return AuditEvent(
        event_type=event_type,
        actor=device.user_id,
        entity_id=device.id,
        workspace_id=device.workspace_id,
        details={"platform": device.platform},
        created_at=now_utc(),
    )


class SQLiteDeviceRegistrationRepository:
    """Single-database transactional writes for the active SQLite runtime."""

    def __init__(self, connect: Callable[[], sqlite3.Connection]) -> None:
        self._connect = connect

    def register(self, device: DeviceRegistration) -> DeviceRegistrationMutation:
        if not device.user_id:
            raise ValueError("Device registration requires an authenticated user")

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT entity_id, payload FROM kv_store "
                "WHERE entity_type='device_registration' ORDER BY entity_id"
            ).fetchall()
            registrations = [self._decode_row(row) for row in rows]
            matches = [item for item in registrations if item.token == device.token]
            if matches:
                existing = matches[0]
                updated = existing.model_copy(
                    update={
                        "user_id": device.user_id,
                        "workspace_id": device.workspace_id,
                        "platform": device.platform,
                        "app_version": device.app_version,
                        "updated_at": device.updated_at,
                    }
                )
                updated_row = connection.execute(
                    "UPDATE kv_store SET payload=? "
                    "WHERE entity_type='device_registration' AND entity_id=?",
                    (updated.model_dump_json(), existing.id),
                )
                if updated_row.rowcount != 1:
                    raise RuntimeError("Device registration changed during its locked update")
                duplicate_ids = tuple(item.id for item in matches[1:])
                for duplicate_id in duplicate_ids:
                    deleted_row = connection.execute(
                        "DELETE FROM kv_store "
                        "WHERE entity_type='device_registration' AND entity_id=?",
                        (duplicate_id,),
                    )
                    if deleted_row.rowcount != 1:
                        raise RuntimeError("Duplicate device changed during its locked removal")
                changed_owner = (
                    existing.user_id != updated.user_id
                    or existing.workspace_id != updated.workspace_id
                )
                audit_event = (
                    device_registration_audit("notification.device_reassigned", updated)
                    if changed_owner
                    else None
                )
            else:
                updated = device
                duplicate_ids = ()
                connection.execute(
                    "INSERT INTO kv_store(entity_type, entity_id, payload) "
                    "VALUES ('device_registration', ?, ?)",
                    (updated.id, updated.model_dump_json()),
                )
                audit_event = device_registration_audit(
                    "notification.device_registered", updated
                )

            if audit_event is not None:
                self._insert_audit(connection, audit_event)
            connection.commit()
            return DeviceRegistrationMutation(updated, duplicate_ids, audit_event)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def unregister(
        self,
        *,
        device_id: str,
        user_id: str,
        workspace_id: str | None,
    ) -> DeviceRegistrationMutation | None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT entity_id, payload FROM kv_store "
                "WHERE entity_type='device_registration' AND entity_id=?",
                (device_id,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            device = self._decode_row(row)
            if device.user_id != user_id or device.workspace_id != workspace_id:
                connection.commit()
                return None
            deleted_row = connection.execute(
                "DELETE FROM kv_store "
                "WHERE entity_type='device_registration' AND entity_id=?",
                (device_id,),
            )
            if deleted_row.rowcount != 1:
                raise RuntimeError("Device registration changed during its locked removal")
            audit_event = device_registration_audit(
                "notification.device_unregistered", device
            )
            self._insert_audit(connection, audit_event)
            connection.commit()
            return DeviceRegistrationMutation(device, audit_event=audit_event)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _decode_row(row: tuple[str, str]) -> DeviceRegistration:
        entity_id, payload = row
        device = DeviceRegistration.model_validate_json(payload)
        if device.id != entity_id:
            raise ValueError("Device registration identity does not match its durable row")
        return device

    @staticmethod
    def _insert_audit(connection: sqlite3.Connection, event: AuditEvent) -> None:
        connection.execute(
            "INSERT INTO kv_store(entity_type, entity_id, payload) VALUES (?, ?, ?)",
            ("audit_event", event.id, event.model_dump_json()),
        )
