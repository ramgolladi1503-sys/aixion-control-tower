from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace

import psycopg
import pytest
from psycopg import Connection, sql

from app.models import now_utc
from app.email_delivery import dispatch_one_due_email_postgres, prepare_verification_email
from app.postgres_database import run_postgres_migrations
from app.postgres_email_outbox_repository import (
    ClaimedVerificationEmail,
    PostgresEmailOutboxRepository,
)


@pytest.fixture
def email_outbox() -> Iterator[tuple[PostgresEmailOutboxRepository, Callable[[], Connection]]]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL integration tests")
    schema = f"ct_email_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    try:
        run_postgres_migrations(connect)
        yield PostgresEmailOutboxRepository(connect), connect
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _insert_email(
    connect: Callable[[], Connection], *, outbox_id: str | None = None, created_at=None
) -> str:
    identifier = outbox_id or f"email_{uuid.uuid4().hex}"
    now = created_at or now_utc()
    with connect() as conn:
        conn.execute(
            "INSERT INTO auth_email_outbox(outbox_id,recipient,encrypted_code,status,attempts,created_at,"
            "next_attempt_at) VALUES(%s,%s,%s,'PENDING',0,%s,%s)",
            (identifier, f"{identifier}@example.test", b"e" * 29, now, now),
        )
    return identifier


def _claim(
    repository: PostgresEmailOutboxRepository,
    *,
    now,
    lease_seconds: int = 30,
    max_attempts: int = 5,
) -> ClaimedVerificationEmail | None:
    return repository.claim_one_due(
        max_attempts=max_attempts,
        now=now,
        lease_expires_at=now + timedelta(seconds=lease_seconds),
    )


def test_email_claim_success_clears_ciphertext(
    email_outbox: tuple[PostgresEmailOutboxRepository, Callable[[], Connection]],
) -> None:
    repository, connect = email_outbox
    identifier = _insert_email(connect)
    now = now_utc()

    claim = _claim(repository, now=now)
    assert claim is not None
    assert claim.outbox_id == identifier
    assert claim.attempt == 1
    assert claim.encrypted_code == b"e" * 29
    assert repository.mark_sent(claim, now=now + timedelta(seconds=1)) is True
    with connect() as conn:
        row = conn.execute(
            "SELECT status,encrypted_code,sent_at,lease_expires_at FROM auth_email_outbox WHERE outbox_id=%s",
            (identifier,),
        ).fetchone()
    assert row[0] == "SENT"
    assert row[1] == b""
    assert row[2] is not None
    assert row[3] is None


def test_email_expired_lease_can_be_reclaimed_and_stale_worker_is_fenced(
    email_outbox: tuple[PostgresEmailOutboxRepository, Callable[[], Connection]],
) -> None:
    repository, connect = email_outbox
    identifier = _insert_email(connect)
    now = now_utc()
    first = _claim(repository, now=now, lease_seconds=2)
    assert first is not None
    second = _claim(repository, now=now + timedelta(seconds=3))
    assert second is not None
    assert second.outbox_id == identifier
    assert second.attempt == first.attempt + 1
    assert repository.mark_sent(first, now=now + timedelta(seconds=4)) is False
    assert repository.mark_sent(second, now=now + timedelta(seconds=4)) is True
    with connect() as conn:
        row = conn.execute(
            "SELECT status,attempts,encrypted_code FROM auth_email_outbox WHERE outbox_id=%s",
            (identifier,),
        ).fetchone()
    assert row == ("SENT", 2, b"")


def test_retry_keeps_encrypted_payload_and_terminal_failure_erases_it(
    email_outbox: tuple[PostgresEmailOutboxRepository, Callable[[], Connection]],
) -> None:
    repository, connect = email_outbox
    identifier = _insert_email(connect)
    now = now_utc()
    claim = _claim(repository, now=now)
    assert claim is not None
    retry_at = now + timedelta(seconds=10)
    assert repository.record_failure(
        claim,
        now=now + timedelta(seconds=1),
        retry_at=retry_at,
        error_code="SMTP_TIMEOUT",
    ) is True
    with connect() as conn:
        row = conn.execute(
            "SELECT status,encrypted_code,next_attempt_at,last_error FROM auth_email_outbox WHERE outbox_id=%s",
            (identifier,),
        ).fetchone()
    assert row == ("PENDING", b"e" * 29, retry_at, "SMTP_TIMEOUT")

    terminal_claim = _claim(repository, now=retry_at)
    assert terminal_claim is not None
    assert repository.record_failure(
        terminal_claim,
        now=retry_at + timedelta(seconds=1),
        retry_at=None,
        error_code="SMTP_PERMANENT_FAILURE",
    ) is True
    with connect() as conn:
        row = conn.execute(
            "SELECT status,encrypted_code,lease_expires_at,last_error FROM auth_email_outbox WHERE outbox_id=%s",
            (identifier,),
        ).fetchone()
    assert row == ("DEAD", b"", None, "SMTP_PERMANENT_FAILURE")


def test_email_worker_claims_distinct_rows_concurrently(
    email_outbox: tuple[PostgresEmailOutboxRepository, Callable[[], Connection]],
) -> None:
    repository, connect = email_outbox
    identifiers = {_insert_email(connect), _insert_email(connect)}
    now = now_utc()

    def claim(_: int) -> ClaimedVerificationEmail | None:
        return _claim(repository, now=now)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, (0, 1)))
    assert all(claim is not None for claim in claims)
    assert {claim.outbox_id for claim in claims if claim is not None} == identifiers


def test_exhausted_email_is_terminal_and_ciphertext_is_erased(
    email_outbox: tuple[PostgresEmailOutboxRepository, Callable[[], Connection]],
) -> None:
    repository, connect = email_outbox
    identifier = _insert_email(connect)
    with connect() as conn:
        conn.execute(
            "UPDATE auth_email_outbox SET attempts=2 WHERE outbox_id=%s", (identifier,)
        )
    now = now_utc()

    assert _claim(repository, now=now, max_attempts=2) is None
    with connect() as conn:
        row = conn.execute(
            "SELECT status,encrypted_code,last_error FROM auth_email_outbox WHERE outbox_id=%s",
            (identifier,),
        ).fetchone()
    assert row == ("DEAD", b"", "DELIVERY_RETRIES_EXHAUSTED")


def test_email_failure_rejects_unbounded_error_categories_and_stale_claims(
    email_outbox: tuple[PostgresEmailOutboxRepository, Callable[[], Connection]],
) -> None:
    repository, connect = email_outbox
    _insert_email(connect)
    now = now_utc()
    claim = _claim(repository, now=now, lease_seconds=1)
    assert claim is not None
    with pytest.raises(ValueError, match="uppercase category"):
        repository.record_failure(
            claim,
            now=now,
            retry_at=now + timedelta(seconds=1),
            error_code="recipient=user@example.test",
        )
    assert repository.record_failure(
        claim,
        now=now + timedelta(seconds=2),
        retry_at=None,
        error_code="SMTP_ERROR",
    ) is False


def test_postgres_dispatch_decrypts_and_sends_then_clears_ciphertext(
    email_outbox: tuple[PostgresEmailOutboxRepository, Callable[[], Connection]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, connect = email_outbox
    monkeypatch.setenv("AIXION_EMAIL_OUTBOX_KEY", "ab" * 32)
    monkeypatch.setattr(
        "app.email_delivery.get_settings",
        lambda: SimpleNamespace(
            email_max_attempts=3,
            email_lease_seconds=30,
            email_retry_base_seconds=2,
            email_retry_max_seconds=20,
        ),
    )
    identifier, encrypted, _ = prepare_verification_email(
        recipient="verify@example.test", code="731904"
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO auth_email_outbox(outbox_id,recipient,encrypted_code,status,attempts,created_at,"
            "next_attempt_at) VALUES(%s,%s,%s,'PENDING',0,%s,%s)",
            (
                identifier,
                "verify@example.test",
                encrypted,
                now_utc(),
                now_utc(),
            ),
        )
    sent: list[tuple[str, str]] = []

    assert dispatch_one_due_email_postgres(
        repository=repository,
        sender=lambda recipient, code: sent.append((recipient, code)),
    ) is True
    assert sent == [("verify@example.test", "731904")]
    with connect() as conn:
        row = conn.execute(
            "SELECT status,encrypted_code,sent_at FROM auth_email_outbox WHERE outbox_id=%s",
            (identifier,),
        ).fetchone()
    assert row[0] == "SENT"
    assert row[1] == b""
    assert row[2] is not None


def test_postgres_dispatch_failure_schedules_retry_with_bounded_error_category(
    email_outbox: tuple[PostgresEmailOutboxRepository, Callable[[], Connection]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, connect = email_outbox
    monkeypatch.setenv("AIXION_EMAIL_OUTBOX_KEY", "cd" * 32)
    monkeypatch.setattr(
        "app.email_delivery.get_settings",
        lambda: SimpleNamespace(
            email_max_attempts=3,
            email_lease_seconds=30,
            email_retry_base_seconds=2,
            email_retry_max_seconds=20,
        ),
    )
    identifier, encrypted, _ = prepare_verification_email(
        recipient="retry@example.test", code="secret-code"
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO auth_email_outbox(outbox_id,recipient,encrypted_code,status,attempts,created_at,"
            "next_attempt_at) VALUES(%s,%s,%s,'PENDING',0,%s,%s)",
            (identifier, "retry@example.test", encrypted, now_utc(), now_utc()),
        )

    def fail_sender(_recipient: str, _code: str) -> None:
        raise TimeoutError("provider details must not be stored")

    assert dispatch_one_due_email_postgres(repository=repository, sender=fail_sender) is True
    with connect() as conn:
        row = conn.execute(
            "SELECT status,attempts,encrypted_code,last_error,next_attempt_at "
            "FROM auth_email_outbox WHERE outbox_id=%s",
            (identifier,),
        ).fetchone()
    assert row[0] == "PENDING"
    assert row[1] == 1
    assert row[2] == encrypted
    assert row[3] == "DELIVERY_TIMEOUTERROR"
    assert row[4] > now_utc()


def test_postgres_dispatch_terminal_failure_erases_payload(
    email_outbox: tuple[PostgresEmailOutboxRepository, Callable[[], Connection]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, connect = email_outbox
    monkeypatch.setenv("AIXION_EMAIL_OUTBOX_KEY", "ef" * 32)
    monkeypatch.setattr(
        "app.email_delivery.get_settings",
        lambda: SimpleNamespace(
            email_max_attempts=1,
            email_lease_seconds=30,
            email_retry_base_seconds=2,
            email_retry_max_seconds=20,
        ),
    )
    identifier, encrypted, _ = prepare_verification_email(
        recipient="terminal@example.test", code="terminal-code"
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO auth_email_outbox(outbox_id,recipient,encrypted_code,status,attempts,created_at,"
            "next_attempt_at) VALUES(%s,%s,%s,'PENDING',0,%s,%s)",
            (identifier, "terminal@example.test", encrypted, now_utc(), now_utc()),
        )

    def fail_sender(_recipient: str, _code: str) -> None:
        raise RuntimeError("SMTP failed")

    assert dispatch_one_due_email_postgres(repository=repository, sender=fail_sender) is True
    with connect() as conn:
        row = conn.execute(
            "SELECT status,attempts,encrypted_code,last_error FROM auth_email_outbox WHERE outbox_id=%s",
            (identifier,),
        ).fetchone()
    assert row == ("DEAD", 1, b"", "DELIVERY_RUNTIMEERROR")


def test_postgres_dispatch_recovers_claim_after_worker_restart(
    email_outbox: tuple[PostgresEmailOutboxRepository, Callable[[], Connection]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh lifespan worker can recover a claim left by a crashed process."""
    repository, connect = email_outbox
    monkeypatch.setenv("AIXION_EMAIL_OUTBOX_KEY", "12" * 32)
    monkeypatch.setattr(
        "app.email_delivery.get_settings",
        lambda: SimpleNamespace(
            email_max_attempts=3,
            email_lease_seconds=30,
            email_retry_base_seconds=2,
            email_retry_max_seconds=20,
        ),
    )
    identifier, encrypted, _ = prepare_verification_email(
        recipient="restart@example.test", code="restart-code"
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO auth_email_outbox(outbox_id,recipient,encrypted_code,status,attempts,created_at,"
            "next_attempt_at) VALUES(%s,%s,%s,'PENDING',0,%s,%s)",
            (identifier, "restart@example.test", encrypted, now_utc(), now_utc()),
        )

    crashed_worker_claim = _claim(repository, now=now_utc(), lease_seconds=30)
    assert crashed_worker_claim is not None
    assert crashed_worker_claim.outbox_id == identifier

    # Model process death after commit: there is no sender call and no outcome
    # write. Advance the durable lease deadline, then create the same repository
    # type the next application lifespan obtains from PostgresRuntime.
    expired_at = now_utc() - timedelta(seconds=1)
    with connect() as conn:
        conn.execute(
            "UPDATE auth_email_outbox SET lease_expires_at=%s WHERE outbox_id=%s",
            (expired_at, identifier),
        )
    restarted_worker_repository = PostgresEmailOutboxRepository(connect)
    sent: list[tuple[str, str]] = []

    assert dispatch_one_due_email_postgres(
        repository=restarted_worker_repository,
        sender=lambda recipient, code: sent.append((recipient, code)),
    ) is True
    assert sent == [("restart@example.test", "restart-code")]
    assert repository.mark_sent(crashed_worker_claim, now=now_utc()) is False
    with connect() as conn:
        row = conn.execute(
            "SELECT status,attempts,encrypted_code,sent_at,lease_expires_at "
            "FROM auth_email_outbox WHERE outbox_id=%s",
            (identifier,),
        ).fetchone()
    assert row[0] == "SENT"
    assert row[1] == 2
    assert row[2] == b""
    assert row[3] is not None
    assert row[4] is None
