from __future__ import annotations

from collections.abc import Callable
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from .trust_crypto import sha256_hex
from .trust_models import FlightRecorderVerification, TrustEvent, TrustEventType


class PostgresTrustEventRepositoryError(RuntimeError):
    """The PostgreSQL trust event ledger contains inconsistent data."""


class PostgresTrustEventRepository:
    """Globally ordered trust events persisted in PostgreSQL."""

    def __init__(self, connect: Callable[[], Connection]) -> None:
        self._connect = connect

    @staticmethod
    def _event_material(event: TrustEvent) -> dict[str, Any]:
        return {
            "sequence": event.sequence,
            "event_type": event.event_type,
            "entity_type": event.entity_type,
            "entity_id": event.entity_id,
            "actor": event.actor,
            "correlation_id": event.correlation_id,
            "payload": event.payload,
            "previous_hash": event.previous_hash,
            "created_at": event.created_at,
        }

    @staticmethod
    def _decode(row: tuple[Any, ...]) -> TrustEvent:
        event_id, sequence, workspace_id, event_type, entity_type, entity_id, actor, correlation_id, previous_hash, event_hash, created_at, payload = row
        try:
            event = TrustEvent.model_validate(payload)
        except Exception as error:
            raise PostgresTrustEventRepositoryError("Stored trust event payload is invalid") from error
        if (
            event.id != event_id
            or event.sequence != sequence
            or event.workspace_id != workspace_id
            or event.event_type != event_type
            or event.entity_type != entity_type
            or event.entity_id != entity_id
            or event.actor != actor
            or event.correlation_id != correlation_id
            or event.previous_hash != previous_hash
            or event.event_hash != event_hash
            or event.created_at != created_at
        ):
            raise PostgresTrustEventRepositoryError(
                "Stored trust event payload does not match its indexed columns"
            )
        return event

    def append(
        self,
        event_type: TrustEventType,
        *,
        entity_type: str,
        entity_id: str,
        actor: str,
        workspace_id: str | None = None,
        correlation_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> TrustEvent:
        with self._connect() as conn:
            with conn.transaction():
                return self.append_in_transaction(
                    conn,
                    event_type,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    actor=actor,
                    workspace_id=workspace_id,
                    correlation_id=correlation_id,
                    payload=payload,
                )

    def append_in_transaction(
        self,
        conn: Connection,
        event_type: TrustEventType,
        *,
        entity_type: str,
        entity_id: str,
        actor: str,
        workspace_id: str | None = None,
        correlation_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> TrustEvent:
        """Append using the caller's transaction for atomic domain commits."""
        if not entity_type or not entity_id or not actor:
            raise ValueError("Trust events require entity type, entity id, and actor")
        head = conn.execute(
            "SELECT sequence,event_hash FROM trust_event_heads "
            "WHERE singleton=TRUE FOR UPDATE"
        ).fetchone()
        if head is None:
            raise PostgresTrustEventRepositoryError("Trust event sequence head is missing")
        previous_sequence, previous_hash = head
        latest_row = conn.execute(
            "SELECT event_id,sequence,workspace_id,event_type,entity_type,entity_id,actor,"
            "correlation_id,previous_hash,event_hash,created_at,payload "
            "FROM trust_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        if latest_row is None:
            if (previous_sequence, previous_hash) != (0, None):
                raise PostgresTrustEventRepositoryError(
                    "Trust event sequence head does not match the empty ledger"
                )
        else:
            latest = self._decode(latest_row)
            if (
                (previous_sequence, previous_hash)
                != (latest.sequence, latest.event_hash)
                or sha256_hex(self._event_material(latest)) != latest.event_hash
            ):
                raise PostgresTrustEventRepositoryError(
                    "Trust event sequence head or ledger tail is inconsistent"
                )
        event = TrustEvent(
            sequence=previous_sequence + 1,
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            actor=actor,
            workspace_id=workspace_id,
            correlation_id=correlation_id,
            payload=payload or {},
            previous_hash=previous_hash,
            event_hash="pending",
        )
        event.event_hash = sha256_hex(self._event_material(event))
        conn.execute(
            "INSERT INTO trust_events "
            "(event_id,sequence,workspace_id,event_type,entity_type,entity_id,actor,"
            "correlation_id,previous_hash,event_hash,created_at,payload) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                event.id,
                event.sequence,
                event.workspace_id,
                event.event_type.value,
                event.entity_type,
                event.entity_id,
                event.actor,
                event.correlation_id,
                event.previous_hash,
                event.event_hash,
                event.created_at,
                Jsonb(event.model_dump(mode="json")),
            ),
        )
        conn.execute(
            "UPDATE trust_event_heads SET sequence=%s,event_hash=%s WHERE singleton=TRUE",
            (event.sequence, event.event_hash),
        )
        return event

    def verify(self) -> FlightRecorderVerification:
        with self._connect() as conn:
            return self.verify_in_transaction(conn)

    def verify_in_transaction(self, conn: Connection) -> FlightRecorderVerification:
        """Verify the ledger using the caller's snapshot and transaction."""
        rows = conn.execute(
            "SELECT event_id,sequence,workspace_id,event_type,entity_type,entity_id,actor,"
            "correlation_id,previous_hash,event_hash,created_at,payload "
            "FROM trust_events ORDER BY sequence"
        ).fetchall()
        head = conn.execute(
            "SELECT sequence,event_hash FROM trust_event_heads WHERE singleton=TRUE"
        ).fetchone()
        expected_sequence = 1
        expected_previous: str | None = None
        for row in rows:
            try:
                event = self._decode(row)
            except PostgresTrustEventRepositoryError as error:
                return FlightRecorderVerification(
                    valid=False,
                    event_count=len(rows),
                    first_invalid_sequence=row[1],
                    reason=str(error),
                )
            if event.sequence != expected_sequence:
                return FlightRecorderVerification(
                    valid=False,
                    event_count=len(rows),
                    first_invalid_sequence=event.sequence,
                    reason=f"Expected sequence {expected_sequence}, got {event.sequence}.",
                )
            if event.previous_hash != expected_previous:
                return FlightRecorderVerification(
                    valid=False,
                    event_count=len(rows),
                    first_invalid_sequence=event.sequence,
                    reason="Hash-chain predecessor mismatch.",
                )
            if sha256_hex(self._event_material(event)) != event.event_hash:
                return FlightRecorderVerification(
                    valid=False,
                    event_count=len(rows),
                    first_invalid_sequence=event.sequence,
                    reason="Event payload hash mismatch.",
                )
            expected_previous = event.event_hash
            expected_sequence += 1
        if head != (len(rows), expected_previous):
            return FlightRecorderVerification(
                valid=False,
                event_count=len(rows),
                first_invalid_sequence=None,
                reason="Trust event sequence head does not match the ledger.",
            )
        return FlightRecorderVerification(valid=True, event_count=len(rows))
