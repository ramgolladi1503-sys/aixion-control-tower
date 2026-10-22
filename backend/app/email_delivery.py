from __future__ import annotations

import asyncio
import logging
import os
import re
import smtplib
import ssl
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .settings import get_settings
from .postgres_email_outbox_repository import PostgresEmailOutboxRepository
from .store import store


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _key() -> bytes:
    raw = os.getenv("AIXION_EMAIL_OUTBOX_KEY", "").strip()
    try:
        key = bytes.fromhex(raw)
    except ValueError as exc:
        raise RuntimeError("Email outbox encryption key is invalid") from exc
    if len(key) != 32:
        raise RuntimeError("Email outbox encryption key must be 64 hexadecimal characters")
    return key


def _encrypt_code(*, outbox_id: str, recipient: str, code: str) -> bytes:
    nonce = os.urandom(12)
    aad = f"{outbox_id}:{recipient}".encode("utf-8")
    return nonce + AESGCM(_key()).encrypt(nonce, code.encode("utf-8"), aad)


def _decrypt_code(*, outbox_id: str, recipient: str, encrypted: bytes) -> str:
    if len(encrypted) < 29:
        raise RuntimeError("Email outbox payload is invalid")
    nonce, ciphertext = encrypted[:12], encrypted[12:]
    aad = f"{outbox_id}:{recipient}".encode("utf-8")
    try:
        return AESGCM(_key()).decrypt(nonce, ciphertext, aad).decode("utf-8")
    except Exception as exc:
        raise RuntimeError("Email outbox payload authentication failed") from exc


def prepare_verification_email(*, recipient: str, code: str) -> tuple[str, bytes, str]:
    """Prepare encrypted queue data for insertion with the verification hash."""
    outbox_id = f"email_{uuid.uuid4().hex}"
    encrypted = _encrypt_code(outbox_id=outbox_id, recipient=recipient, code=code)
    now = _now().isoformat()
    return outbox_id, encrypted, now


def insert_verification_email(
    conn, *, outbox_id: str, recipient: str, encrypted_code: bytes, created_at: str
) -> None:
    # Superseded codes must not remain eligible for a later retry.
    conn.execute(
        """UPDATE auth_email_outbox SET status='DEAD', encrypted_code=X'',
               last_error='VERIFICATION_CODE_SUPERSEDED', lease_expires_at=NULL
           WHERE recipient=? AND status='PENDING'""",
        (recipient,),
    )
    conn.execute(
        """INSERT INTO auth_email_outbox
           (id, recipient, encrypted_code, status, attempts, created_at, next_attempt_at)
           VALUES (?, ?, ?, 'PENDING', 0, ?, ?)""",
        (outbox_id, recipient, encrypted_code, created_at, created_at),
    )


def enqueue_verification_email(*, recipient: str, code: str) -> str:
    """Persist encrypted verification material; test/operator utility."""
    outbox_id, encrypted, created_at = prepare_verification_email(recipient=recipient, code=code)
    with store._lock, store._connect() as conn:
        insert_verification_email(
            conn,
            outbox_id=outbox_id,
            recipient=recipient,
            encrypted_code=encrypted,
            created_at=created_at,
        )
    return outbox_id


def _send_smtp(recipient: str, code: str) -> None:
    settings = get_settings()
    if not settings.email_smtp_host or not settings.email_from:
        raise RuntimeError("Email SMTP delivery is not configured")
    message = EmailMessage()
    message["From"] = settings.email_from
    message["To"] = recipient
    message["Subject"] = "Verify your Aixion Control Tower account"
    message.set_content(
        "Your Aixion Control Tower verification code is: "
        f"{code}\n\nThis code expires in 30 minutes. If you did not request it, ignore this email."
    )
    with smtplib.SMTP(
        settings.email_smtp_host,
        settings.email_smtp_port,
        timeout=settings.email_smtp_timeout_seconds,
    ) as client:
        client.ehlo()
        client.starttls(context=ssl.create_default_context())
        client.ehlo()
        username = os.getenv("AIXION_EMAIL_SMTP_USERNAME", "").strip()
        password = os.getenv("AIXION_EMAIL_SMTP_PASSWORD", "")
        if username and password:
            client.login(username, password)
        client.send_message(message)


def dispatch_one_due_email(*, sender=None) -> bool:
    """Claim and attempt one item. All durable state is kept in SQLite for local use."""
    settings = get_settings()
    now = _now()
    lease_until = (now + timedelta(seconds=settings.email_lease_seconds)).isoformat()
    now_text = now.isoformat()
    with store._lock, store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """UPDATE auth_email_outbox SET status='DEAD', last_error='DELIVERY_RETRIES_EXHAUSTED'
               WHERE attempts >= ? AND status IN ('PENDING', 'SENDING')
                 AND (status='PENDING' OR lease_expires_at <= ?)""",
            (settings.email_max_attempts, now_text),
        )
        row = conn.execute(
            """SELECT id, recipient, encrypted_code, attempts FROM auth_email_outbox
               WHERE attempts < ? AND (
                 (status = 'PENDING' AND next_attempt_at <= ?) OR
                 (status = 'SENDING' AND lease_expires_at <= ?)
               ) ORDER BY created_at LIMIT 1""",
            (settings.email_max_attempts, now_text, now_text),
        ).fetchone()
        if row is None:
            conn.commit()
            return False
        outbox_id, recipient, encrypted, attempts = row
        claimed_attempt = attempts + 1
        conn.execute(
            """UPDATE auth_email_outbox SET status='SENDING', attempts=?, lease_expires_at=?
               WHERE id=?""",
            (claimed_attempt, lease_until, outbox_id),
        )
        conn.commit()

    try:
        code = _decrypt_code(outbox_id=outbox_id, recipient=recipient, encrypted=encrypted)
        (sender or _send_smtp)(recipient, code)
    except Exception as exc:
        completed = _now()
        if claimed_attempt >= settings.email_max_attempts:
            status, next_attempt = "DEAD", None
        else:
            delay = min(
                settings.email_retry_max_seconds,
                settings.email_retry_base_seconds * 2 ** (claimed_attempt - 1),
            )
            status = "PENDING"
            next_attempt = (completed + timedelta(seconds=delay)).isoformat()
        with store._lock, store._connect() as conn:
            conn.execute(
                """UPDATE auth_email_outbox
                   SET status=?, next_attempt_at=COALESCE(?, next_attempt_at),
                       lease_expires_at=NULL, last_error=? WHERE id=? AND status='SENDING'""",
                (status, next_attempt, f"DELIVERY_{exc.__class__.__name__}", outbox_id),
            )
        return True

    with store._lock, store._connect() as conn:
        conn.execute(
            """UPDATE auth_email_outbox
               SET status='SENT', encrypted_code=X'', sent_at=?, lease_expires_at=NULL,
                   next_attempt_at=?, last_error=NULL WHERE id=? AND status='SENDING'""",
            (_now().isoformat(), _now().isoformat(), outbox_id),
        )
    return True


def dispatch_one_due_email_postgres(
    *, repository: PostgresEmailOutboxRepository, sender=None, now: datetime | None = None
) -> bool:
    """Claim and attempt one PostgreSQL email without holding a DB transaction over SMTP."""
    settings = get_settings()
    claimed_at = now or _now()
    claim = repository.claim_one_due(
        max_attempts=settings.email_max_attempts,
        now=claimed_at,
        lease_expires_at=claimed_at + timedelta(seconds=settings.email_lease_seconds),
    )
    if claim is None:
        return False

    try:
        code = _decrypt_code(
            outbox_id=claim.outbox_id,
            recipient=claim.recipient,
            encrypted=claim.encrypted_code,
        )
        (sender or _send_smtp)(claim.recipient, code)
    except Exception as exc:
        completed_at = _now()
        if claim.attempt >= settings.email_max_attempts:
            retry_at = None
        else:
            delay = min(
                settings.email_retry_max_seconds,
                settings.email_retry_base_seconds * 2 ** (claim.attempt - 1),
            )
            retry_at = completed_at + timedelta(seconds=delay)
        error_suffix = re.sub(r"[^A-Z0-9_]", "_", type(exc).__name__.upper())[:52] or "ERROR"
        repository.record_failure(
            claim,
            now=completed_at,
            retry_at=retry_at,
            error_code=f"DELIVERY_{error_suffix}",
        )
        return True

    repository.mark_sent(claim, now=_now())
    return True


async def _drain_email_dispatch_task(dispatch_task: asyncio.Task[object]) -> None:
    """Wait for the thread-backed dispatch task despite repeated cancellation."""
    while not dispatch_task.done():
        try:
            await asyncio.shield(dispatch_task)
        except asyncio.CancelledError:
            # A repeated cancellation targets this loop task. Shielding keeps
            # asyncio from cancelling the to_thread task while its worker runs.
            if dispatch_task.done():
                break
        except Exception:
            logging.getLogger(__name__).warning(
                "Email outbox dispatch iteration failed during shutdown"
            )
            break

    if dispatch_task.done() and not dispatch_task.cancelled():
        try:
            dispatch_task.result()
        except Exception:
            logging.getLogger(__name__).warning(
                "Email outbox dispatch iteration failed during shutdown"
            )


async def email_dispatch_loop(
    *, postgres_repository: PostgresEmailOutboxRepository | None = None
) -> None:
    """Dispatch using the explicitly selected store; SQLite remains the default."""
    settings = get_settings()
    if not settings.email_dispatch_enabled:
        return
    if settings.postgres_auth_enabled and postgres_repository is None:
        logging.getLogger(__name__).error(
            "PostgreSQL email outbox repository is required in PostgreSQL auth mode"
        )
        raise RuntimeError(
            "PostgreSQL email outbox repository is required in PostgreSQL auth mode"
        )
    while True:
        if postgres_repository is None:
            dispatch_task = asyncio.create_task(asyncio.to_thread(dispatch_one_due_email))
        else:
            dispatch_task = asyncio.create_task(
                asyncio.to_thread(
                    dispatch_one_due_email_postgres,
                    repository=postgres_repository,
                )
            )
        try:
            await asyncio.shield(dispatch_task)
        except asyncio.CancelledError:
            # Cancelling asyncio.to_thread does not stop its worker thread.
            # Drain it before lifespan closes the shared PostgreSQL pool.
            await _drain_email_dispatch_task(dispatch_task)
            raise
        except Exception:
            # Do not log recipient, verification code, SMTP response, or secrets.
            logging.getLogger(__name__).warning("Email outbox dispatch iteration failed")
        await asyncio.sleep(settings.email_dispatch_poll_seconds)
