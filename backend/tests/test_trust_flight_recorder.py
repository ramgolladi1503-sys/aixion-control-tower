from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")
os.environ.setdefault("AIXION_PROFILE", "test")

from app.store import store
from app.trust_flight_recorder import append_trust_event, verify_flight_recorder
from app.trust_models import TrustEventType


def setup_function() -> None:
    store.reset()


def test_flight_recorder_hash_chain_detects_payload_tampering() -> None:
    first = append_trust_event(
        TrustEventType.ACTION_RECEIVED,
        entity_type="proposed_action",
        entity_id="action_1",
        actor="agent:one",
        payload={"command": "pytest"},
    )
    second = append_trust_event(
        TrustEventType.ACTION_ALLOWED,
        entity_type="proposed_action",
        entity_id="action_1",
        actor="policy",
        payload={"decision": "ALLOW"},
    )

    assert second.previous_hash == first.event_hash
    assert verify_flight_recorder().valid is True

    first.payload["command"] = "rm -rf /"
    verification = verify_flight_recorder()
    assert verification.valid is False
    assert verification.first_invalid_sequence == 1
    assert "payload hash" in verification.reason.lower()


def test_flight_recorder_detects_sequence_gap() -> None:
    event = append_trust_event(
        TrustEventType.LEASE_ISSUED,
        entity_type="capability_lease",
        entity_id="lease_1",
        actor="owner@example.com",
    )
    event.sequence = 3
    verification = verify_flight_recorder()
    assert verification.valid is False
    assert "expected sequence" in verification.reason.lower()
