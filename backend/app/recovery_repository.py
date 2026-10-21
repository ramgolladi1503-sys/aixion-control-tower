from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping


class SQLiteRecoverySnapshotRepository:
    """Read recovery collections and schema metadata from one SQLite snapshot."""

    def __init__(self, connect: Callable[[], sqlite3.Connection]) -> None:
        self._connect = connect

    def read_snapshot(
        self,
        entity_types: Mapping[str, str],
        *,
        scope_entity_type: str,
        membership_user_id: str | None = None,
        membership_workspace_id: str | None = None,
    ) -> tuple[
        dict[str, list[tuple[str, str]]],
        list[tuple[str, str, str]],
        list[tuple[str, str]],
        dict[str, list[tuple[str, str]]],
    ]:
        storage_types = tuple(dict.fromkeys((*entity_types.values(), scope_entity_type)))
        placeholders = ",".join("?" for _ in storage_types)
        membership_ids = {
            "user": membership_user_id,
            "workspace": membership_workspace_id,
        }
        with self._connect() as conn:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            rows = conn.execute(
                "SELECT entity_type,entity_id,payload FROM kv_store "
                f"WHERE entity_type IN ({placeholders}) ORDER BY entity_type,entity_id",
                storage_types,
            ).fetchall()
            migration_rows = conn.execute(
                "SELECT migration_id,name,applied_at FROM schema_migrations ORDER BY migration_id"
            ).fetchall()
            membership_rows: dict[str, list[tuple[str, str]]] = {
                "user": [],
                "workspace": [],
            }
            for entity_type, entity_id in membership_ids.items():
                if entity_id is None:
                    continue
                row = conn.execute(
                    "SELECT entity_id,payload FROM kv_store "
                    "WHERE entity_type = ? AND entity_id = ?",
                    (entity_type, entity_id),
                ).fetchone()
                if row is not None:
                    membership_rows[entity_type].append((row[0], row[1]))
            grouped: dict[str, list[tuple[str, str]]] = {name: [] for name in entity_types}
            scope_rows: list[tuple[str, str]] = []
            storage_to_collection = {storage: name for name, storage in entity_types.items()}
            for entity_type, entity_id, payload in rows:
                collection = storage_to_collection.get(entity_type)
                if collection is None:
                    if entity_type == scope_entity_type:
                        scope_rows.append((entity_id, payload))
                    continue
                grouped[collection].append((entity_id, payload))
            conn.commit()
        return grouped, migration_rows, scope_rows, membership_rows
