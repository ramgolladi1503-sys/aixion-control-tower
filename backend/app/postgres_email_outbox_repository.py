from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from psycopg import Connection

_ERROR_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


@dataclass(frozen=True)
class ClaimedVerificationEmail:
    outbox_id: str
    recipient: str
    encrypted_code: bytes
    attempt: int
    lease_expires_at: datetime


class PostgresEmailOutboxIntegrityError(RuntimeError):
    """A delivery worker attempted a stale or malformed outbox transition."""


class PostgresEmailOutboxRepository:
    """Leased PostgreSQL claims for encrypted verification email delivery."""

    def __init__(self, connect: Callable[[], Connection]) -> None:
        self._connect = connect

    def claim_one_due(
        self,
        *,
        max_attempts: int,
        now: datetime,
        lease_expires_at: datetime,
    ) -> ClaimedVerificationEmail | None:
        if max_attempts < 1 or lease_expires_at <= now:
            raise ValueError("Email retry limit and lease deadline must be valid")
        with self._connect() as conn:
            conn.execute(
                "UPDATE auth_email_outbox SET status='DEAD',encrypted_code=%s,"
                "lease_expires_at=NULL,last_error='DELIVERY_RETRIES_EXHAUSTED' "
                "WHERE attempts >= %s AND (status='PENDING' OR "
                "(status='SENDING' AND lease_expires_at <= %s))",
                (b"", max_attempts, now),
            )
            row = conn.execute(
                "SELECT outbox_id,recipient,encrypted_code,attempts FROM auth_email_outbox "
                "WHERE attempts < %s AND ((status='PENDING' AND next_attempt_at <= %s) OR "
                "(status='SENDING' AND lease_expires_at <= %s)) "
                "ORDER BY next_attempt_at,created_at,outbox_id LIMIT 1 FOR UPDATE SKIP LOCKED",
                (max_attempts, now, now),
            ).fetchone()
            if row is None:
                return None
            outbox_id, recipient, encrypted_code, attempts = row
            attempt = attempts + 1
            result = conn.execute(
                "UPDATE auth_email_outbox SET status='SENDING',attempts=%s,lease_expires_at=%s "
                "WHERE outbox_id=%s AND attempts=%s AND status IN ('PENDING','SENDING')",
                (attempt, lease_expires_at, outbox_id, attempts),
            )
            if result.rowcount != 1:
                raise PostgresEmailOutboxIntegrityError("Email outbox claim changed while locked")
        return ClaimedVerificationEmail(
            outbox_id=outbox_id,
            recipient=recipient,
            encrypted_code=bytes(encrypted_code),
            attempt=attempt,
            lease_expires_at=lease_expires_at,
        )

    def mark_sent(self, claim: ClaimedVerificationEmail, *, now: datetime) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                "UPDATE auth_email_outbox SET status='SENT',encrypted_code=%s,sent_at=%s,"
                "lease_expires_at=NULL,next_attempt_at=%s,last_error=NULL "
                "WHERE outbox_id=%s AND status='SENDING' AND attempts=%s AND lease_expires_at>%s",
                (b"", now, now, claim.outbox_id, claim.attempt, now),
            )
        return result.rowcount == 1

    def record_failure(
        self,
        claim: ClaimedVerificationEmail,
        *,
        now: datetime,
        retry_at: datetime | None,
        error_code: str,
    ) -> bool:
        if not _ERROR_CODE_PATTERN.fullmatch(error_code):
            raise ValueError("Email delivery error must be a bounded uppercase category")
        if retry_at is not None and retry_at <= now:
            raise ValueError("Email retry must be scheduled after the failure time")
        terminal = retry_at is None
        status = "DEAD" if terminal else "PENDING"
        encrypted_code = b"" if terminal else None
        with self._connect() as conn:
            result = conn.execute(
                "UPDATE auth_email_outbox SET status=%s,encrypted_code=COALESCE(%s,encrypted_code),"
                "next_attempt_at=COALESCE(%s,next_attempt_at),lease_expires_at=NULL,last_error=%s "
                "WHERE outbox_id=%s AND status='SENDING' AND attempts=%s AND lease_expires_at>%s",
                (
                    status,
                    encrypted_code,
                    retry_at,
                    error_code,
                    claim.outbox_id,
                    claim.attempt,
                    now,
                ),
            )
        return result.rowcount == 1
