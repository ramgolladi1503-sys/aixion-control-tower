from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from app import main


def _production_settings(*, postgres_runtime_enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(
        profile="production",
        is_production=True,
        outbox_dispatch_mode="scheduled",
        push_dispatch_enabled=False,
        email_dispatch_enabled=False,
        postgres_runtime_probe_enabled=postgres_runtime_enabled,
        postgres_auth_rate_limits_enabled=False,
        postgres_auth_enabled=False,
    )


def test_production_lifespan_rejects_missing_postgres_authority_before_yield(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_factory_calls: list[None] = []
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: _production_settings(postgres_runtime_enabled=False),
    )
    monkeypatch.setattr(
        main.PostgresRuntime,
        "from_environment",
        staticmethod(lambda: runtime_factory_calls.append(None)),
    )
    application = FastAPI(lifespan=main.lifespan)
    yielded = False

    async def exercise() -> None:
        nonlocal yielded
        with pytest.raises(
            RuntimeError,
            match="lifespan-owned runtime repository bundle",
        ):
            async with main.lifespan(application):
                yielded = True
        assert yielded is False

    asyncio.run(exercise())
    assert runtime_factory_calls == []


def test_production_lifespan_rejects_incomplete_postgres_bundle_before_yield(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class IncompleteRuntime:
        repositories = object()

        def __init__(self) -> None:
            self.opened = False
            self.closed = False

        def open(self, *, wait: bool = False) -> None:
            assert wait is True
            self.opened = True

        def probe(self) -> SimpleNamespace:
            return SimpleNamespace(ready=True)

        def close(self) -> None:
            self.closed = True

    runtime = IncompleteRuntime()
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: _production_settings(postgres_runtime_enabled=True),
    )
    monkeypatch.setattr(
        main.PostgresRuntime,
        "from_environment",
        staticmethod(lambda: runtime),
    )
    application = FastAPI(lifespan=main.lifespan)
    yielded = False

    async def exercise() -> None:
        nonlocal yielded
        with pytest.raises(
            RuntimeError,
            match="PostgreSQL application repository bundle is unavailable or mismatched",
        ):
            async with main.lifespan(application):
                yielded = True
        assert yielded is False

    asyncio.run(exercise())
    assert runtime.opened is True
    assert runtime.closed is True
