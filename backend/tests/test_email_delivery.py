from __future__ import annotations

import pytest
import smtplib

from app import email_delivery
from app.store import store


@pytest.fixture(autouse=True)
def isolated_email_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIXION_PROFILE", "test")
    monkeypatch.setenv("AIXION_EMAIL_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("AIXION_EMAIL_SMTP_HOST", "smtp.test.invalid")
    monkeypatch.setenv("AIXION_EMAIL_FROM", "verify@test.invalid")
    monkeypatch.setenv("AIXION_EMAIL_OUTBOX_KEY", bytes(range(32)).hex())
    monkeypatch.setenv("AIXION_EMAIL_SMTP_USERNAME", "test-user")
    monkeypatch.setenv("AIXION_EMAIL_SMTP_PASSWORD", "test-password")
    with store._connect() as conn:
        conn.execute("DELETE FROM auth_email_outbox")


def _row(outbox_id: str) -> tuple:
    with store._connect() as conn:
        return conn.execute(
            "SELECT status, attempts, encrypted_code, last_error FROM auth_email_outbox WHERE id=?",
            (outbox_id,),
        ).fetchone()


def test_outbox_encrypts_code_and_erases_ciphertext_after_send() -> None:
    outbox_id = email_delivery.enqueue_verification_email(
        recipient="owner@example.com", code="123456"
    )
    pending = _row(outbox_id)
    assert pending[0] == "PENDING"
    assert b"123456" not in pending[2]

    sent: list[tuple[str, str]] = []
    assert email_delivery.dispatch_one_due_email(sender=lambda recipient, code: sent.append((recipient, code)))

    assert sent == [("owner@example.com", "123456")]
    status, attempts, encrypted, last_error = _row(outbox_id)
    assert (status, attempts, encrypted, last_error) == ("SENT", 1, b"", None)


def test_delivery_failure_is_retried_and_provider_detail_is_not_persisted() -> None:
    outbox_id = email_delivery.enqueue_verification_email(
        recipient="owner@example.com", code="123456"
    )

    assert email_delivery.dispatch_one_due_email(
        sender=lambda *_: (_ for _ in ()).throw(smtplib_error())
    )
    status, attempts, _, last_error = _row(outbox_id)
    assert status == "PENDING"
    assert attempts == 1
    assert last_error == "DELIVERY_RuntimeError"

    with store._connect() as conn:
        conn.execute(
            "UPDATE auth_email_outbox SET next_attempt_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (outbox_id,),
        )
    assert email_delivery.dispatch_one_due_email(sender=lambda *_: None)
    assert _row(outbox_id)[0:2] == ("SENT", 2)


def test_item_reaches_dead_state_after_bounded_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIXION_EMAIL_MAX_ATTEMPTS", "1")
    outbox_id = email_delivery.enqueue_verification_email(
        recipient="owner@example.com", code="123456"
    )

    assert email_delivery.dispatch_one_due_email(
        sender=lambda *_: (_ for _ in ()).throw(RuntimeError("provider body must not persist"))
    )

    status, attempts, _, last_error = _row(outbox_id)
    assert (status, attempts, last_error) == ("DEAD", 1, "DELIVERY_RuntimeError")


def test_outbox_ciphertext_cannot_be_moved_to_another_recipient() -> None:
    outbox_id = email_delivery.enqueue_verification_email(
        recipient="owner@example.com", code="123456"
    )
    encrypted = _row(outbox_id)[2]

    with pytest.raises(RuntimeError, match="authentication failed"):
        email_delivery._decrypt_code(
            outbox_id=outbox_id, recipient="attacker@example.com", encrypted=encrypted
        )


def test_smtp_transport_uses_starttls_and_does_not_contact_external_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: int) -> None:
            calls.append((host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def ehlo(self) -> None:
            calls.append("ehlo")

        def starttls(self, *, context) -> None:
            calls.append(("starttls", context))

        def login(self, username: str, password: str) -> None:
            calls.append(("login", username, password))

        def send_message(self, message) -> None:
            calls.append(("send", message["To"], message["From"], message.get_content()))

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)

    email_delivery._send_smtp("owner@example.com", "654321")

    assert calls[0] == ("smtp.test.invalid", 587, 10)
    assert [call for call in calls if isinstance(call, tuple) and call[0] == "starttls"]
    assert ("login", "test-user", "test-password") in calls
    sent = next(call for call in calls if isinstance(call, tuple) and call[0] == "send")
    assert sent[1] == "owner@example.com"
    assert "654321" in sent[3]


def smtplib_error() -> RuntimeError:
    return RuntimeError("provider response may contain sensitive data")
