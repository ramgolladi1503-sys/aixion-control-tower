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


def test_live_multidevice_dispatch_renews_lease_and_fences_second_worker(
    outbox_repository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live provider fan-out cannot be reclaimed while its claim heartbeat works."""
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

            # The first worker is blocked in provider I/O beyond the initial lease.
            # Its PostgreSQL heartbeat must keep another process from reclaiming it.
            time.sleep(1.1)
            assert dispatch_one_postgres_push(repository, sender=second_sender) is None
            live = _persisted_notification(connect, notification.id)
            assert live.push_status == "SENDING"
            assert live.push_attempts == 1
            assert live.push_lease_expires_at is not None

            release_first_sender.set()
            assert first_attempt.result(timeout=10) == notification.id
    finally:
        release_first_sender.set()

    final = _persisted_notification(connect, notification.id)
    assert final.push_status == "SENT"
    assert final.push_attempts == 1
    assert final.push_error is None
    assert final.push_lease_expires_at is None
    assert Counter(first_delivery_tokens) == Counter(
        {"device-token-a": 1, "device-token-b": 1}
    )
    assert second_delivery_tokens == []


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


def test_outcome_commit_response_loss_does_not_redeliver(
    outbox_repository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost DB response after commit must not replay an already-sent push."""
    repository, connect = outbox_repository
    workspace, _, notification = _seed_notification(connect, user_id="commit-owner")
    with connect() as conn:
        _insert_device(conn, workspace, "commit-owner", "commit-device-token")

    monkeypatch.setattr(
        postgres_notification_dispatcher,
        "get_settings",
        lambda: SimpleNamespace(
            push_dispatch_enabled=True,
            push_lease_seconds=10,
            push_max_attempts=4,
            push_retry_base_seconds=15,
            push_retry_max_seconds=900,
        ),
    )
    delivered_tokens: list[str] = []

    def sender(*, token: str, **_: object) -> None:
        delivered_tokens.append(token)
        return None

    commit_outcome = repository.record_outcome
    committed_then_response_lost = False

    def lose_response_after_commit(**kwargs: object) -> object:
        nonlocal committed_then_response_lost
        result = commit_outcome(**kwargs)
        if not committed_then_response_lost:
            committed_then_response_lost = True
            # The repository transaction has returned (and committed), but the
            # worker cannot distinguish a lost response from a failed commit.
            raise RuntimeError("simulated lost response after outcome commit")
        return result

    monkeypatch.setattr(repository, "record_outcome", lose_response_after_commit)
    with pytest.raises(RuntimeError, match="lost response after outcome commit"):
        dispatch_one_postgres_push(repository, sender=sender)

    committed = _persisted_notification(connect, notification.id)
    assert committed.push_status == "SENT"
    assert committed.push_attempts == 1
    assert committed.push_lease_expires_at is None
    assert delivered_tokens == ["commit-device-token"]

    # Simulate a restarted dispatcher after the ambiguous worker failure. The
    # terminal row is durable, so it cannot be claimed and sent a second time.
    restarted_repository = type(repository)(connect)
    assert dispatch_one_postgres_push(restarted_repository, sender=sender) is None
    assert delivered_tokens == ["commit-device-token"]
    final = _persisted_notification(connect, notification.id)
    assert final.push_status == "SENT"
    assert final.push_attempts == 1
