from __future__ import annotations

import asyncio
from threading import Event
from types import SimpleNamespace

import pytest

from app import email_delivery


@pytest.mark.parametrize(
    ("repository", "expected_dispatcher"),
    [
        (None, email_delivery.dispatch_one_due_email),
        (object(), email_delivery.dispatch_one_due_email_postgres),
    ],
)
def test_email_loop_uses_only_the_explicitly_selected_dispatcher(
    monkeypatch: pytest.MonkeyPatch,
    repository: object | None,
    expected_dispatcher,
) -> None:
    monkeypatch.setattr(
        email_delivery,
        "get_settings",
        lambda: SimpleNamespace(email_dispatch_enabled=True, email_dispatch_poll_seconds=1),
    )
    calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

    async def stop_after_dispatch(function, *args, **kwargs):
        calls.append((function, args, kwargs))
        raise asyncio.CancelledError

    monkeypatch.setattr(email_delivery.asyncio, "to_thread", stop_after_dispatch)

    async def run_loop() -> None:
        await email_delivery.email_dispatch_loop(postgres_repository=repository)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_loop())

    assert len(calls) == 1
    function, args, kwargs = calls[0]
    assert function is expected_dispatcher
    if repository is None:
        assert args == ()
        assert kwargs == {}
    else:
        assert args == ()
        assert kwargs == {"repository": repository}


def test_disabled_email_loop_returns_without_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        email_delivery,
        "get_settings",
        lambda: SimpleNamespace(email_dispatch_enabled=False, email_dispatch_poll_seconds=1),
    )

    asyncio.run(email_delivery.email_dispatch_loop(postgres_repository=object()))


def test_email_dispatch_loop_drains_inflight_thread_on_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = Event()
    release = Event()
    finished = Event()

    def dispatch(*, repository: object) -> None:
        assert repository is not None
        started.set()
        release.wait(timeout=2)
        finished.set()

    monkeypatch.setattr(email_delivery, "dispatch_one_due_email_postgres", dispatch)
    monkeypatch.setattr(
        email_delivery,
        "get_settings",
        lambda: SimpleNamespace(email_dispatch_enabled=True, email_dispatch_poll_seconds=30),
    )

    async def exercise() -> None:
        task = asyncio.create_task(email_delivery.email_dispatch_loop(postgres_repository=object()))
        started_ready = await asyncio.to_thread(started.wait, 2)
        assert started_ready
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("Cancelled email worker did not propagate cancellation")
        assert finished.is_set()

    asyncio.run(exercise())
