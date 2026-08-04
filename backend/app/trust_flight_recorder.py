from __future__ import annotations

from .store import store
from .trust_crypto import sha256_hex
from .trust_models import FlightRecorderVerification, TrustEvent, TrustEventType


def _event_material(event: TrustEvent) -> dict:
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


def append_trust_event(
    event_type: TrustEventType,
    *,
    entity_type: str,
    entity_id: str,
    actor: str,
    correlation_id: str | None = None,
    payload: dict | None = None,
    persist: bool = True,
) -> TrustEvent:
    with store.atomic():
        ordered = sorted(store.trust_events.values(), key=lambda item: item.sequence)
        previous = ordered[-1] if ordered else None
        event = TrustEvent(
            sequence=(previous.sequence + 1) if previous else 1,
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            actor=actor,
            correlation_id=correlation_id,
            payload=payload or {},
            previous_hash=previous.event_hash if previous else None,
            event_hash="pending",
        )
        event.event_hash = sha256_hex(_event_material(event))
        store.trust_events[event.id] = event
        if persist:
            store.persist()
        return event


def verify_flight_recorder() -> FlightRecorderVerification:
    with store.atomic():
        ordered = sorted(store.trust_events.values(), key=lambda item: item.sequence)
        expected_previous: str | None = None
        expected_sequence = 1
        for event in ordered:
            if event.sequence != expected_sequence:
                return FlightRecorderVerification(
                    valid=False,
                    event_count=len(ordered),
                    first_invalid_sequence=event.sequence,
                    reason=f"Expected sequence {expected_sequence}, got {event.sequence}.",
                )
            if event.previous_hash != expected_previous:
                return FlightRecorderVerification(
                    valid=False,
                    event_count=len(ordered),
                    first_invalid_sequence=event.sequence,
                    reason="Hash-chain predecessor mismatch.",
                )
            expected_hash = sha256_hex(_event_material(event))
            if expected_hash != event.event_hash:
                return FlightRecorderVerification(
                    valid=False,
                    event_count=len(ordered),
                    first_invalid_sequence=event.sequence,
                    reason="Event payload hash mismatch.",
                )
            expected_previous = event.event_hash
            expected_sequence += 1
        return FlightRecorderVerification(valid=True, event_count=len(ordered))
