from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

MIGRATION_TABLE = "schema_migrations"


@dataclass(frozen=True)
class Migration:
    migration_id: str
    name: str
    apply: Callable[[sqlite3.Connection], None]


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_migration_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MIGRATION_TABLE} (
            migration_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )


def baseline_kv_store(conn: sqlite3.Connection) -> None:
    """Baseline current MVP persistence without rewriting existing state."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kv_store (
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            payload TEXT NOT NULL,
            PRIMARY KEY (entity_type, entity_id)
        )
        """
    )


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        migration_id="0001_baseline_kv_store",
        name="Create baseline key-value persistence table",
        apply=baseline_kv_store,
    ),
)


def known_migration_ids() -> set[str]:
    return {migration.migration_id for migration in MIGRATIONS}


def get_applied_migrations(conn: sqlite3.Connection) -> list[dict[str, str]]:
    ensure_migration_table(conn)
    rows = conn.execute(
        f"SELECT migration_id, name, applied_at FROM {MIGRATION_TABLE} ORDER BY migration_id"
    ).fetchall()
    return [
        {"migration_id": migration_id, "name": name, "applied_at": applied_at}
        for migration_id, name, applied_at in rows
    ]


def fail_on_unknown_migrations(applied_ids: set[str]) -> None:
    unknown = sorted(applied_ids - known_migration_ids())
    if not unknown:
        return

    unknown_list = ", ".join(unknown)
    raise RuntimeError(
        "Database schema contains migrations newer than this backend understands: "
        f"{unknown_list}. Refusing to start because running old code against a newer database can corrupt state."
    )


def run_migrations(conn: sqlite3.Connection) -> None:
    """Apply pending database migrations safely and idempotently."""
    ensure_migration_table(conn)
    applied_ids = {
        row[0]
        for row in conn.execute(f"SELECT migration_id FROM {MIGRATION_TABLE}").fetchall()
    }
    fail_on_unknown_migrations(applied_ids)

    for migration in MIGRATIONS:
        if migration.migration_id in applied_ids:
            continue
        migration.apply(conn)
        conn.execute(
            f"INSERT INTO {MIGRATION_TABLE}(migration_id, name, applied_at) VALUES (?, ?, ?)",
            (migration.migration_id, migration.name, utc_timestamp()),
        )

    conn.commit()
