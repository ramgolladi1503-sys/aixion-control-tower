from __future__ import annotations

import asyncio
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
