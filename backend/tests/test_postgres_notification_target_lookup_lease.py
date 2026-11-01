from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
import time
from types import SimpleNamespace

import pytest

from app import postgres_notification_dispatcher
from app.postgres_notification_dispatcher import dispatch_one_postgres_push
from test_postgres_notification_outbox import (
    _insert_device,
    _persisted_notification,
    _seed_notification,
)

pytest_plugins = ("test_postgres_notification_outbox",)


def test_postgres_claim_renews_during_slow_device_target_lookup(
    outbox_repository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow target lookup cannot let a second PostgreSQL worker reclaim the row."""
    repository, connect = outbox_repository
    workspace, _, notification = _seed_notification(connect, user_id="lookup-lease-owner")
    with connect() as conn:
        _insert_device(conn, workspace, "lookup-lease-owner", "lookup-lease-device-token")

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

    first_lookup_entered = Event()
    release_first_lookup = Event()
    lookup_lock = Lock()
    lookup_count = 0
    original_list_device_targets = repository.list_device_targets
    first_delivery_tokens: list[str] = []
    second_delivery_tokens: list[str] = []

    def delayed_lookup(target_notification):
        nonlocal lookup_count
        with lookup_lock:
            lookup_count += 1
            is_first_lookup = lookup_count == 1
        if is_first_lookup:
            first_lookup_entered.set()
            if not release_first_lookup.wait(timeout=8):
                raise TimeoutError("test did not release the first target lookup")
        return original_list_device_targets(target_notification)

    repository.list_device_targets = delayed_lookup

    def first_sender(*, token: str, **_: object) -> None:
        first_delivery_tokens.append(token)

    def second_sender(*, token: str, **_: object) -> None:
        second_delivery_tokens.append(token)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            first_attempt = pool.submit(
                dispatch_one_postgres_push,
                repository,
                sender=first_sender,
            )
            try:
                assert first_lookup_entered.wait(
                    timeout=5
                ), "first worker never reached target lookup"

                # Let the original one-second lease elapse while lookup is still blocked.
                time.sleep(1.2)
                assert dispatch_one_postgres_push(repository, sender=second_sender) is None

                live = _persisted_notification(connect, notification.id)
                assert live.push_status == "SENDING"
                assert live.push_attempts == 1
                assert live.push_lease_expires_at is not None
            finally:
                release_first_lookup.set()

            assert first_attempt.result(timeout=10) == notification.id
    finally:
        release_first_lookup.set()

    final = _persisted_notification(connect, notification.id)
    assert final.push_status == "SENT"
    assert final.push_attempts == 1
    assert final.push_lease_expires_at is None
    assert first_delivery_tokens == ["lookup-lease-device-token"]
    assert second_delivery_tokens == []
