from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from .models import now_utc
from .notification_outbox_repository import PostgresNotificationOutboxRepository
from .notifications import (
    _is_retryable_push_failure,
    _push_retry_delay_seconds,
    _send_fcm_to_token,
)
from .settings import get_settings

logger = logging.getLogger(__name__)
PushSender = Callable[..., str | None]


def dispatch_one_postgres_push(
    repository: PostgresNotificationOutboxRepository,
    *,
    now: datetime | None = None,
    sender: PushSender = _send_fcm_to_token,
) -> str | None:
    """Claim and deliver one PostgreSQL outbox row using injected persistence.

    The service is intentionally not started by the current SQLite-backed API.
    A future PostgreSQL application binding can inject the repository without
    changing the provider transport or duplicating retry policy.
    """
    settings = get_settings()
    if not settings.push_dispatch_enabled:
        return None

    claimed_at = now or now_utc()
    notification = repository.claim_next_due(
        now=claimed_at,
        max_attempts=settings.push_max_attempts,
        lease_seconds=settings.push_lease_seconds,
    )
    if notification is None:
        return None
    if notification.push_status != "SENDING":
        # The repository may have transitioned an exhausted row to FAILED.
        return notification.id

    accepted = 0
    errors: list[str] = []
    skipped = False
    try:
        devices = repository.list_device_targets(notification)
        for device in devices:
            error = sender(
                token=device.token,
                title=notification.title,
                body=notification.body,
                data={
                    "notification_id": notification.id,
                    "entity_type": notification.push_entity_type,
                    "entity_id": notification.push_entity_id,
                },
            )
            if error == "SKIPPED_FCM_PROJECT_ID_NOT_CONFIGURED":
                skipped = True
            elif error is None:
                accepted += 1
            else:
                errors.append(error)
                if error == "FCM_TOKEN_UNREGISTERED":
                    try:
                        repository.invalidate_device_if_token_matches(
                            device_id=device.id,
                            token=device.token,
                            occurred_at=claimed_at,
                        )
                    except Exception:
                        # Cleanup is independent of the delivery result. The
                        # durable token remains eligible for later cleanup.
                        logger.warning(
                            "PostgreSQL invalid push token cleanup failed for device id=%s",
                            device.id,
                        )
    except Exception:
        # Do not persist provider exception text or payload data. The outbox
        # records a bounded retry category instead.
        logger.warning(
            "PostgreSQL push dispatch failed for notification id=%s", notification.id
        )
        errors.append("FCM_DISPATCH_FAILED")

    if accepted:
        outcome_status = "SENT"
        outcome_error = "PARTIAL_DELIVERY" if errors or skipped else None
    elif errors:
        # Retain a retryable category if any target failed transiently; avoid
        # persisting an unbounded list of per-device provider responses.
        outcome_error = next(
            (item for item in errors if _is_retryable_push_failure(item)), errors[0]
        )
        outcome_status = "FAILED"
    elif skipped:
        outcome_status = "SKIPPED"
        outcome_error = "AIXION_FCM_PROJECT_ID is not configured"
    else:
        outcome_status = "NO_DEVICE"
        outcome_error = None

    completed_at = now or now_utc()
    next_attempt_at = None
    if outcome_status == "FAILED" and _is_retryable_push_failure(outcome_error):
        if notification.push_attempts < settings.push_max_attempts:
            delay = _push_retry_delay_seconds(
                attempts=notification.push_attempts,
                base_seconds=settings.push_retry_base_seconds,
                maximum_seconds=settings.push_retry_max_seconds,
            )
            outcome_status = "RETRY_WAIT"
            next_attempt_at = completed_at + timedelta(seconds=delay)
        else:
            outcome_error = "FCM_RETRIES_EXHAUSTED"

    # A None result means this lease expired or a newer claim replaced it. The
    # stale worker must not overwrite the newer attempt's durable state.
    repository.record_outcome(
        notification_id=notification.id,
        expected_attempts=notification.push_attempts,
        push_status=outcome_status,
        push_error=outcome_error,
        next_attempt_at=next_attempt_at,
        completed_at=completed_at,
    )
    return notification.id


async def postgres_notification_dispatch_loop(
    repository: PostgresNotificationOutboxRepository,
) -> None:
    """Poll the PostgreSQL outbox, draining an in-flight attempt on shutdown."""
    settings = get_settings()
    if not settings.push_dispatch_enabled:
        logger.info("PostgreSQL push dispatch loop disabled by configuration")
        return
    while True:
        dispatch_task = asyncio.create_task(
            asyncio.to_thread(dispatch_one_postgres_push, repository)
        )
        try:
            await asyncio.shield(dispatch_task)
        except asyncio.CancelledError:
            # The thread cannot be cancelled safely. Wait for its bounded DB and
            # FCM operations to finish before lifespan closes the shared pool.
            try:
                await dispatch_task
            except Exception:
                logger.warning("PostgreSQL push dispatch iteration failed during shutdown")
            raise
        except Exception:
            # Keep the worker alive while excluding secrets and provider details.
            logger.warning("PostgreSQL push dispatch iteration failed")
        await asyncio.sleep(settings.push_dispatch_poll_seconds)
