from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from psycopg import sql

from app.application_repositories import ApplicationRepositories
from app.postgres_database import run_postgres_migrations
from app.postgres_trust_event_repository import PostgresTrustEventRepository
from app.trust_models import TrustEventType


@pytest.fixture
def trust_event_repository():
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL integration tests")
    schema = f"ct_trust_event_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect():
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    try:
        run_postgres_migrations(connect)
        yield PostgresTrustEventRepository(connect), connect
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_postgres_trust_events_append_and_verify_hash_chain(trust_event_repository):
    repository, _ = trust_event_repository
    first = repository.append(
        TrustEventType.ACTION_RECEIVED,
        entity_type="proposed_action",
        entity_id="action-1",
        actor="connector-1",
        payload={"command": "safe command"},
    )
    second = repository.append(
        TrustEventType.ACTION_ALLOWED,
        entity_type="proposed_action",
        entity_id="action-1",
        actor="reviewer-1",
        workspace_id=None,
    )

    assert first.sequence == 1
    assert second.sequence == 2
    assert second.previous_hash == first.event_hash
    assert repository.verify().model_dump() == {
        "valid": True,
        "event_count": 2,
        "first_invalid_sequence": None,
        "reason": "",
    }


def test_postgres_trust_event_sequence_is_serialized_across_concurrent_writers(
    trust_event_repository,
):
    repository, _ = trust_event_repository

    def append(index: int) -> int:
        return repository.append(
            TrustEventType.ACTION_RECEIVED,
            entity_type="proposed_action",
            entity_id=f"action-{index}",
            actor="connector",
        ).sequence

    with ThreadPoolExecutor(max_workers=8) as executor:
        sequences = list(executor.map(append, range(24)))

    assert sorted(sequences) == list(range(1, 25))
    verification = repository.verify()
    assert verification.valid is True
    assert verification.event_count == 24


def test_postgres_trust_event_verifier_detects_payload_tampering(trust_event_repository):
    repository, connect = trust_event_repository
    event = repository.append(
        TrustEventType.ACTION_RECEIVED,
        entity_type="proposed_action",
        entity_id="action-1",
        actor="connector",
        payload={"source": "original"},
    )
    with connect() as conn:
        with conn.transaction():
            # The fixture owns this isolated schema; bypass the guard only to
            # simulate offline/storage-level corruption for verifier coverage.
            conn.execute("ALTER TABLE trust_events DISABLE TRIGGER trust_events_reject_mutation")
            conn.execute(
                "UPDATE trust_events SET payload=jsonb_set(payload,'{payload,source}',%s::jsonb) "
                "WHERE event_id=%s",
                ('"tampered"', event.id),
            )
            conn.execute("ALTER TABLE trust_events ENABLE TRIGGER trust_events_reject_mutation")

    verification = repository.verify()
    assert verification.valid is False
    assert verification.first_invalid_sequence == 1
    assert verification.reason == "Event payload hash mismatch."


@pytest.mark.parametrize(
    "statement",
    (
        "UPDATE trust_events SET actor='rewritten' WHERE sequence=1",
        "DELETE FROM trust_events WHERE sequence=1",
    ),
)
def test_postgres_trust_events_reject_update_and_delete(trust_event_repository, statement):
    repository, connect = trust_event_repository
    repository.append(
        TrustEventType.ACTION_RECEIVED,
        entity_type="proposed_action",
        entity_id="action-1",
        actor="connector",
    )

    with pytest.raises(psycopg.errors.RaiseException, match="trust events are immutable"):
        with connect() as conn:
            conn.execute(statement)
    assert repository.verify().valid is True


def test_postgres_trust_event_append_fails_closed_on_inconsistent_head(
    trust_event_repository,
):
    repository, connect = trust_event_repository
    repository.append(
        TrustEventType.ACTION_RECEIVED,
        entity_type="proposed_action",
        entity_id="action-1",
        actor="connector",
    )
    with connect() as conn:
        conn.execute(
            "UPDATE trust_event_heads SET event_hash=%s WHERE singleton=TRUE",
            ("0" * 64,),
        )

    with pytest.raises(RuntimeError, match="head or ledger tail is inconsistent"):
        repository.append(
            TrustEventType.ACTION_ALLOWED,
            entity_type="proposed_action",
            entity_id="action-1",
            actor="reviewer",
        )
    assert repository.verify().valid is False


def test_postgres_trust_event_append_participates_in_callers_transaction(
    trust_event_repository,
):
    repository, connect = trust_event_repository
    with pytest.raises(RuntimeError, match="rollback outer trust transaction"):
        with connect() as conn:
            with conn.transaction():
                event = repository.append_in_transaction(
                    conn,
                    TrustEventType.ACTION_ALLOWED,
                    entity_type="proposed_action",
                    entity_id="action-rollback",
                    actor="reviewer",
                )
                assert event.sequence == 1
                raise RuntimeError("rollback outer trust transaction")

    with connect() as conn:
        assert conn.execute("SELECT count(*) FROM trust_events").fetchone() == (0,)
        assert conn.execute(
            "SELECT sequence,event_hash FROM trust_event_heads WHERE singleton=TRUE"
        ).fetchone() == (0, None)
    assert repository.verify().valid is True


def test_postgres_trust_event_requires_attribution(trust_event_repository):
    repository, _ = trust_event_repository
    with pytest.raises(ValueError, match="entity type, entity id, and actor"):
        repository.append(
            TrustEventType.ACTION_RECEIVED,
            entity_type="",
            entity_id="action-1",
            actor="connector",
        )


def test_application_repository_bundle_exposes_postgres_trust_event_adapter():
    def connector():
        raise AssertionError("Constructing the repository bundle must not connect")

    repositories = ApplicationRepositories.from_connector(connector)
    assert isinstance(repositories.trust_events, PostgresTrustEventRepository)
