#!/usr/bin/env python3
"""Dispatch at most one PostgreSQL verification email, then exit."""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from typing import Protocol

from app.application_repositories import ApplicationRepositories
from app.email_delivery import dispatch_one_due_email_postgres
from app.postgres_email_outbox_repository import PostgresEmailOutboxRepository
from app.postgres_runtime import PostgresRuntime, PostgresRuntimeProbe
from app.settings import validate_outbox_worker_environment


class Runtime(Protocol):
    @property
    def repositories(self) -> ApplicationRepositories: ...

    def open(self, *, wait: bool = False) -> None: ...

    def probe(self) -> PostgresRuntimeProbe: ...

    def close(self) -> None: ...


RuntimeFactory = Callable[[], Runtime]
Dispatcher = Callable[[PostgresEmailOutboxRepository], bool]


def run_once(
    *,
    runtime_factory: RuntimeFactory = PostgresRuntime.from_environment,
    dispatcher: Dispatcher = dispatch_one_due_email_postgres,
) -> bool:
    """Validate, connect, dispatch no more than one item, and close the pool."""
    settings = validate_outbox_worker_environment("email")
    if not settings.email_dispatch_enabled:
        return False
    if settings.outbox_dispatch_mode != "scheduled":
        raise RuntimeError(
            "AIXION_OUTBOX_DISPATCH_MODE=scheduled is required for one-shot email dispatch"
        )

    runtime = runtime_factory()
    try:
        runtime.open(wait=True)
        if not runtime.probe().ready:
            raise RuntimeError("PostgreSQL schema is not ready for email dispatch")
        return dispatcher(runtime.repositories.email_outbox)
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
        # SMTP responses, recipient addresses, verification codes, connection
        # strings, and secrets must never appear in this worker's output.
        print(
            "PostgreSQL email dispatch failed; inspect protected service logs.",
            file=sys.stderr,
        )
        return 1

    print(json.dumps({"status": "processed" if processed else "idle"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
