#!/usr/bin/env python3
"""Check or explicitly apply the versioned PostgreSQL schema migrations.

The default operation is read-only. Applying migrations requires --apply and
exact --expected-database and --expected-schema matches. Remote application
additionally requires --allow-remote; this command never runs automatically
during API startup.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import sys
from typing import Sequence

import psycopg
from psycopg.conninfo import conninfo_to_dict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.postgres_database import run_postgres_migrations  # noqa: E402
from app.postgres_runtime import PostgresRuntime  # noqa: E402


class MigrationCommandError(RuntimeError):
    """A migration request did not satisfy its explicit safety preconditions."""


def _is_loopback_target(dsn: str) -> bool:
    try:
        host = conninfo_to_dict(dsn).get("host", "").strip().lower()
    except (psycopg.Error, ValueError):
        raise MigrationCommandError("AIXION_POSTGRES_DSN is invalid") from None
    if not host or host == "localhost":
        return True
    if host.startswith("/"):
        # PostgreSQL Cloud SQL sockets live under /cloudsql and reach a remote
        # service; do not treat every Unix-domain socket as a local database.
        local_socket_roots = ("/tmp", "/private/tmp", "/var/run/postgresql", "/run/postgresql")
        return any(
            host == root or host.startswith(root.rstrip("/") + "/")
            for root in local_socket_roots
        )
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def inspect_or_apply(
    runtime: PostgresRuntime,
    *,
    dsn: str,
    apply: bool,
    expected_database: str | None,
    expected_schema: str | None,
    allow_remote: bool,
) -> dict[str, object]:
    """Run the read-only schema check or explicitly apply migrations, then verify."""
    try:
        connection_info = conninfo_to_dict(dsn)
    except (psycopg.Error, ValueError):
        raise MigrationCommandError("AIXION_POSTGRES_DSN is invalid") from None
    actual_database = connection_info.get("dbname", "")
    remote = not _is_loopback_target(dsn)
    if remote and connection_info.get("sslmode") != "verify-full":
        raise MigrationCommandError(
            "Remote PostgreSQL requires sslmode=verify-full"
        )

    if apply:
        if not expected_database:
            raise MigrationCommandError("--apply requires --expected-database")
        if not expected_schema:
            raise MigrationCommandError("--apply requires --expected-schema")
        if actual_database != expected_database:
            raise MigrationCommandError(
                "Configured database name does not match --expected-database"
            )
        if remote and not allow_remote:
            raise MigrationCommandError(
                "Remote migration requires explicit --allow-remote authorization"
            )
    elif expected_database is not None or expected_schema is not None or allow_remote:
        raise MigrationCommandError(
            "target confirmations and --allow-remote are valid only with --apply"
        )

    with runtime.connect() as connection:
        connected_database, connected_schema = connection.execute(
            "SELECT current_database(), current_schema()"
        ).fetchone()

    if apply:
        if connected_database != expected_database:
            raise MigrationCommandError(
                "Connected database does not match --expected-database"
            )
        if connected_schema != expected_schema:
            raise MigrationCommandError(
                "Connected schema does not match --expected-schema"
            )
        run_postgres_migrations(runtime.connect)

    status = runtime.verify_schema()
    return {
        "status": "READY" if status.ready else "NOT_READY",
        "database": connected_database,
        "schema": connected_schema,
        "migration_ledger_present": status.migration_ledger_present,
        "applied_migration_ids": list(status.applied_migration_ids),
        "missing_migration_ids": list(status.missing_migration_ids),
        "unknown_migration_ids": list(status.unknown_migration_ids),
        "mismatched_migration_ids": list(status.mismatched_migration_ids),
        "apply_requested": apply,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="read and report schema migration state without modifying it (default)",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="apply pending versioned migrations under the PostgreSQL advisory lock",
    )
    parser.add_argument(
        "--expected-database",
        help="exact database name required with --apply as a target confirmation",
    )
    parser.add_argument(
        "--expected-schema",
        help="exact PostgreSQL schema required with --apply as a target confirmation",
    )
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="allow --apply to connect to a non-loopback PostgreSQL host",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    dsn = os.getenv("AIXION_POSTGRES_DSN", "").strip()
    try:
        runtime = PostgresRuntime.from_environment(connector=psycopg.connect)
        report = inspect_or_apply(
            runtime,
            dsn=dsn,
            apply=args.apply,
            expected_database=args.expected_database,
            expected_schema=args.expected_schema,
            allow_remote=args.allow_remote,
        )
    except (MigrationCommandError, RuntimeError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    except psycopg.Error as exc:
        # Driver diagnostics can contain connection details or database values.
        # Emit only stable, non-secret classification fields to stdout/stderr.
        print(
            json.dumps(
                {"status": "ERROR", "error_type": type(exc).__name__, "sqlstate": exc.sqlstate},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2

    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
