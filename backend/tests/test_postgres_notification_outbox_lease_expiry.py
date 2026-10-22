from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time
from types import SimpleNamespace

import pytest

from app import postgres_notification_dispatcher
from app.postgres_notification_dispatcher import dispatch_one_postgres_push
from test_postgres_notification_outbox import _insert_device, _persisted_notification, _seed_notification

pytest_plugins = ("test_postgres_notification_outbox",)


def test_expired_multidevice_dispatch_lease_fences_stale_worker_outcome(
    outbox_repository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reclaimed claim remains authoritative after an older fan-out unblocks."""
    repository, connect = outbox_repository
    workspace, _, notification = _seed_notification(connect, user_id="lease-owner")
    with connect() as conn:
        _insert_device(conn, workspace, "lease-owner", "device-token-a")
        _insert_device(conn, workspace, "lease-owner", "device-token-b")

    # Keep this test bounded while exercising real elapsed-time lease expiry.
    monkeypatch.setattr(
        postgres_notification_dispatcher,
        "get_settings",
        lambda: SimpleNamespace(
            push_dispatch_enabled=True,
            push_lease_seconds=1,
            push_max_attempts=4,
            push_retry_base_seconds=15,
            push_retry_max_seconds=900,
        ),
    )
    first_sender_entered = Event()
    release_first_sender = Event()
    first_delivery_tokens: list[str] = []
    second_delivery_tokens: list[str] = []

    def blocked_first_sender(*, token: str, **_: object) -> None:
        first_delivery_tokens.append(token)
        if len(first_delivery_tokens) == 1:
            first_sender_entered.set()
            if not release_first_sender.wait(timeout=10):
                raise TimeoutError("test did not release the first push sender")
        return None

    def second_sender(*, token: str, **_: object) -> None:
        second_delivery_tokens.append(token)
        return None

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            first_attempt = pool.submit(
                dispatch_one_postgres_push,
                repository,
                sender=blocked_first_sender,
            )
            assert first_sender_entered.wait(timeout=5), "first worker never reached sender"

            # The first worker is blocked in provider I/O. Waiting beyond the one-second
            # lease lets a second dispatcher reclaim attempt 2 from PostgreSQL.
            time.sleep(1.1)
            assert dispatch_one_postgres_push(repository, sender=second_sender) == notification.id
            reclaimed = _persisted_notification(connect, notification.id)
            assert reclaimed.push_status == "SENT"
            assert reclaimed.push_attempts == 2

            release_first_sender.set()
            assert first_attempt.result(timeout=10) == notification.id
    finally:
        release_first_sender.set()

    final = _persisted_notification(connect, notification.id)
    assert final.push_status == "SENT"
    assert final.push_attempts == 2
    assert final.push_error is None
    assert final.push_lease_expires_at is None
    # At-least-once delivery is expected: the reclaimed worker may duplicate sends
    # while the stale worker is still blocked, but its outcome cannot replace attempt 2.
    assert Counter(first_delivery_tokens) == Counter(
        {"device-token-a": 1, "device-token-b": 1}
    )
    assert Counter(second_delivery_tokens) == Counter(
        {"device-token-a": 1, "device-token-b": 1}
    )


def test_outbox_claim_recovers_after_worker_restart_before_outcome_persistence(
    outbox_repository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh dispatcher reclaims a durable claim after the prior worker is lost."""
    repository, connect = outbox_repository
    workspace, _, notification = _seed_notification(connect, user_id="restart-owner")
    with connect() as conn:
        _insert_device(conn, workspace, "restart-owner", "restart-device-token")

    monkeypatch.setattr(
        postgres_notification_dispatcher,
        "get_settings",
        lambda: SimpleNamespace(
            push_dispatch_enabled=True,
            push_lease_seconds=1,
            push_max_attempts=4,
            push_retry_base_seconds=15,
            push_retry_max_seconds=900,
        ),
    )
    delivered_tokens: list[str] = []

    def sender(*, token: str, **_: object) -> None:
        delivered_tokens.append(token)
        return None

    def lose_worker_before_outcome(**_: object) -> None:
        # Model a worker/process failure after provider acceptance and before the
        # durable outcome commit. The claim must stay leased for bounded recovery.
        raise RuntimeError("simulated worker loss before outcome persistence")

    monkeypatch.setattr(repository, "record_outcome", lose_worker_before_outcome)
    with pytest.raises(RuntimeError, match="simulated worker loss"):
        dispatch_one_postgres_push(repository, sender=sender)

    interrupted = _persisted_notification(connect, notification.id)
    assert interrupted.push_status == "SENDING"
    assert interrupted.push_attempts == 1
    assert interrupted.push_lease_expires_at is not None
    assert delivered_tokens == ["restart-device-token"]

    # Recreate the repository as a restarted dispatcher would, and recover only
    # after the durable claim expires. Duplicate external delivery is at-least-once.
    time.sleep(1.1)
    restarted_repository = type(repository)(connect)
    assert dispatch_one_postgres_push(restarted_repository, sender=sender) == notification.id

    recovered = _persisted_notification(connect, notification.id)
    assert recovered.push_status == "SENT"
    assert recovered.push_attempts == 2
    assert recovered.push_error is None
    assert recovered.push_lease_expires_at is None
    assert delivered_tokens == ["restart-device-token", "restart-device-token"]
