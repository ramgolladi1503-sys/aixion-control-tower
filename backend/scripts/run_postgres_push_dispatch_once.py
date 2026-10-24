#!/usr/bin/env python3
"""Dispatch at most one PostgreSQL push outbox item, then exit."""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from typing import Protocol

from app.application_repositories import ApplicationRepositories
from app.notification_outbox_repository import PostgresNotificationOutboxRepository
from app.postgres_notification_dispatcher import dispatch_one_postgres_push
from app.postgres_runtime import PostgresRuntime, PostgresRuntimeProbe
from app.settings import validate_outbox_worker_environment


class Runtime(Protocol):
    @property
    def repositories(self) -> ApplicationRepositories: ...

    def open(self, *, wait: bool = False) -> None: ...

    def probe(self) -> PostgresRuntimeProbe: ...

    def close(self) -> None: ...


RuntimeFactory = Callable[[], Runtime]
Dispatcher = Callable[[PostgresNotificationOutboxRepository], str | None]


def run_once(
    *,
    runtime_factory: RuntimeFactory = PostgresRuntime.from_environment,
    dispatcher: Dispatcher = dispatch_one_postgres_push,
) -> bool:
    """Validate, connect, dispatch no more than one item, and close the pool."""
    settings = validate_outbox_worker_environment("push")
    if not settings.push_dispatch_enabled:
        return False
    if settings.outbox_dispatch_mode != "scheduled":
        raise RuntimeError(
            "AIXION_OUTBOX_DISPATCH_MODE=scheduled is required for one-shot push dispatch"
        )

    runtime = runtime_factory()
    try:
        runtime.open(wait=True)
        if not runtime.probe().ready:
            raise RuntimeError("PostgreSQL schema is not ready for push dispatch")
        return dispatcher(runtime.repositories.notification_outbox) is not None
    finally:
        runtime.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        processed = run_once()
    except KeyboardInterrupt:
        return 130
    except Exception:
        # Connection strings, provider responses, and push tokens must never
        # appear in this one-shot worker's output.
        print("PostgreSQL push dispatch failed; inspect protected service logs.", file=sys.stderr)
        return 1

    print(json.dumps({"status": "processed" if processed else "idle"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
