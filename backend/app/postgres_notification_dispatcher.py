from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from threading import Event, Thread

from .notification_outbox_repository import PostgresNotificationOutboxRepository
from .notifications import (
    _is_retryable_push_failure,
    _push_retry_delay_seconds,
    _send_fcm_to_token,
)
from .settings import get_settings

logger = logging.getLogger(__name__)
PushSender = Callable[..., str | None]


class _ClaimLeaseHeartbeat:
    """Renew a live PostgreSQL claim while sequential provider I/O is in flight."""

    def __init__(
        self,
        repository: PostgresNotificationOutboxRepository,
        *,
        notification_id: str,
        attempts: int,
        lease_seconds: int,
    ) -> None:
        self._repository = repository
        self._notification_id = notification_id
        self._attempts = attempts
        self._lease_seconds = lease_seconds
        self._interval = max(0.25, lease_seconds / 3)
        self._stop = Event()
        self.lost = False
        self._thread = Thread(
            target=self._run,
            name="postgres-push-lease-heartbeat",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                renewed = self._repository.renew_claim(
                    notification_id=self._notification_id,
                    expected_attempts=self._attempts,
                    lease_seconds=self._lease_seconds,
                )
            except Exception as error:
                self.lost = True
                logger.warning(
                    "PostgreSQL push claim renewal failed error_type=%s",
                    type(error).__name__,
                )
                return
            if renewed is None:
                self.lost = True
                logger.warning("PostgreSQL push claim is no longer current")
                return


async def _drain_postgres_dispatch_task(dispatch_task: asyncio.Task[object]) -> None:
    """Wait for a thread-backed dispatch attempt despite repeated cancellation."""
    while not dispatch_task.done():
        try:
            await asyncio.shield(dispatch_task)
        except asyncio.CancelledError:
            # A repeated cancellation targets this loop task. Shielding keeps
            # asyncio from cancelling the wrapper while its worker thread runs.
            if dispatch_task.done():
                break
        except Exception:
            logger.warning("PostgreSQL push dispatch iteration failed during shutdown")
            break

    if dispatch_task.done() and not dispatch_task.cancelled():
        try:
            dispatch_task.result()
        except Exception:
            logger.warning("PostgreSQL push dispatch iteration failed during shutdown")


def dispatch_one_postgres_push(
    repository: PostgresNotificationOutboxRepository,
    *,
    now: datetime | None = None,
    sender: PushSender = _send_fcm_to_token,
) -> str | None:
    """Claim and deliver one PostgreSQL outbox row using injected persistence.

    The FastAPI lifespan starts this worker only when the complete PostgreSQL
    application repository bundle is selected and inline push dispatch is
    enabled. The worker uses the injected outbox for claims, target lookup,
    outcome persistence, and invalid-token cleanup; it does not read the local
    SQLite notification or device maps.
    """
    settings = get_settings()
    if not settings.push_dispatch_enabled:
        return None

    notification = repository.claim_next_due(
        # Leave runtime time selection to PostgreSQL so a host clock offset
        # cannot shorten or extend the durable claim lease.
        now=now,
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
    invalid_devices: list[tuple[str, str, str, str, str]] = []
    skipped = False
    heartbeat = _ClaimLeaseHeartbeat(
        repository,
        notification_id=notification.id,
        attempts=notification.push_attempts,
        lease_seconds=settings.push_lease_seconds,
    )
    # Start renewing immediately after claim acquisition. Recipient lookup is
    # database I/O too and can outlive the initial lease; without the heartbeat
    # here, another worker could reclaim the row before provider fan-out begins.
    heartbeat.start()
    try:
        try:
            devices = repository.list_device_targets(notification)
        except Exception:
            # A target lookup failure means no recipient set was established.
            # Keep the claim retryable and avoid partial recipient snapshots.
            logger.warning(
                "PostgreSQL push target lookup failed for notification id=%s",
                notification.id,
            )
            errors.append("FCM_DISPATCH_FAILED")
            devices = []

        for device in devices:
            if heartbeat.lost:
                break
            try:
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
                        # Fence cleanup to the registration that received this
                        # response; the row may be rebound while provider I/O runs.
                        if not device.user_id or not device.workspace_id or not device.session_id:
                            logger.warning(
                                "PostgreSQL push target has incomplete owner scope notification_id=%s",
                                notification.id,
                            )
                            continue
                        invalid_devices.append(
                            (
                                device.id,
                                device.token,
                                device.user_id,
                                device.workspace_id,
                                device.session_id,
                            )
                        )
            except Exception:
                # Isolate provider failures per device so one broken request cannot
                # prevent attempts to later recipients. Persist only the bounded
                # category; provider exception text can contain secrets or tokens.
                logger.warning(
                    "PostgreSQL push target failed for notification id=%s",
                    notification.id,
                )
                errors.append("FCM_DISPATCH_FAILED")
    finally:
        heartbeat.stop()

    if heartbeat.lost:
        # A newer claim may already be delivering. Do not overwrite it or send
        # to additional devices; the current claim will recover through its lease.
        return notification.id

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

    next_attempt_at = None
    next_attempt_delay_seconds = None
    if outcome_status == "FAILED" and _is_retryable_push_failure(outcome_error):
        if notification.push_attempts < settings.push_max_attempts:
            delay = _push_retry_delay_seconds(
                attempts=notification.push_attempts,
                base_seconds=settings.push_retry_base_seconds,
                maximum_seconds=settings.push_retry_max_seconds,
            )
            outcome_status = "RETRY_WAIT"
            if now is not None:
                next_attempt_at = now + timedelta(seconds=delay)
            else:
                next_attempt_delay_seconds = delay
        else:
            outcome_error = "FCM_RETRIES_EXHAUSTED"

    # A None result means this lease expired or a newer claim replaced it. The
    # stale worker must not overwrite the newer attempt's durable state.
    try:
        persisted = repository.record_outcome(
            notification_id=notification.id,
            expected_attempts=notification.push_attempts,
            push_status=outcome_status,
            push_error=outcome_error,
            next_attempt_at=next_attempt_at,
            completed_at=now,
            next_attempt_delay_seconds=next_attempt_delay_seconds,
            invalid_devices=tuple(invalid_devices),
        )
    except Exception:
        # Provider sends may already have reached devices. Leave the claim
        # leased for recovery rather than attempting a second fan-out here.
        # The outcome and any invalid-token audits share one DB transaction.
        logger.warning(
            "PostgreSQL push outcome persistence failed notification_id=%s attempt=%s",
            notification.id,
            notification.push_attempts,
        )
        raise
    if persisted is None:
        # A lease may be replaced while the provider call is in flight. Record
        # the stale result as a correlation event without exposing payloads,
        # device tokens, or provider response text.
        logger.warning(
            "PostgreSQL push outcome discarded notification_id=%s attempt=%s state=STALE_CLAIM",
            notification.id,
            notification.push_attempts,
        )
    else:
        logger.info(
            "PostgreSQL push delivery outcome notification_id=%s attempt=%s state=%s "
            "accepted_targets=%s failed_targets=%s",
            notification.id,
            notification.push_attempts,
            outcome_status,
            accepted,
            len(errors),
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
            await _drain_postgres_dispatch_task(dispatch_task)
            raise
        except Exception:
            # Keep the worker alive while excluding secrets and provider details.
            logger.warning("PostgreSQL push dispatch iteration failed")
        await asyncio.sleep(settings.push_dispatch_poll_seconds)
