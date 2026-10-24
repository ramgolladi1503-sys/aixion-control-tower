from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping

from psycopg import Connection


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


class PostgresRecoverySnapshotRepository:
    """Read one workspace's recovery collections from a stable PostgreSQL snapshot."""

    _COLLECTION_TABLES = {
        "project": ("projects", "project_id"),
        "mcp_child_server": ("mcp_child_servers", "server_id"),
        "idea": ("ideas", "idea_id"),
        "work_order": ("work_orders", "work_order_id"),
        "approval_request": ("approval_requests", "approval_id"),
        "mcp_pending_request": ("mcp_pending_requests", "pending_id"),
        "test_run": ("test_runs", "test_run_id"),
        "notification": ("notifications", "notification_id"),
        "audit_event": ("audit_events", "event_id"),
    }

    def __init__(self, connect: Callable[[], Connection]) -> None:
        self._connect = connect

    def read_snapshot(
        self,
        entity_types: Mapping[str, str],
        *,
        scope_entity_type: str,
        workspace_id: str,
        membership_user_id: str | None = None,
        membership_workspace_id: str | None = None,
    ) -> tuple[
        dict[str, list[tuple[str, str]]],
        list[tuple[str, str, str]],
        list[tuple[str, str]],
        dict[str, list[tuple[str, str]]],
    ]:
        """Return rows and membership from one read-only repeatable-read transaction.

        Table and key names come from this module's fixed allowlist. Workspace
        filtering is performed in SQL for every tenant-owned table; payloads
        are converted to JSON text to preserve the route's existing decoder.
        """
        if not isinstance(workspace_id, str) or not workspace_id:
            raise ValueError("A workspace is required for a PostgreSQL recovery snapshot")
        if scope_entity_type != "agent_task":
            raise ValueError("Unsupported PostgreSQL recovery scope collection")
        if any(storage not in self._COLLECTION_TABLES for storage in entity_types.values()):
            raise ValueError("Unsupported PostgreSQL recovery collection")

        grouped: dict[str, list[tuple[str, str]]] = {name: [] for name in entity_types}
        migration_rows: list[tuple[str, str, str]] = []
        scope_rows: list[tuple[str, str]] = []
        membership_rows: dict[str, list[tuple[str, str]]] = {
            "user": [],
            "workspace": [],
        }

        with self._connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            migration_rows = conn.execute(
                "SELECT migration_id,name,applied_at::text "
                "FROM schema_migrations ORDER BY migration_id"
            ).fetchall()

            for collection, storage_type in entity_types.items():
                table, key_column = self._COLLECTION_TABLES[storage_type]
                rows = conn.execute(
                    f"SELECT {key_column},payload::text FROM {table} "
                    f"WHERE workspace_id=%s ORDER BY {key_column}",
                    (workspace_id,),
                ).fetchall()
                grouped[collection] = [(row[0], row[1]) for row in rows]

            if membership_user_id is not None:
                if membership_workspace_id is None:
                    row = conn.execute(
                        "SELECT user_id,payload::text FROM users WHERE user_id=%s",
                        (membership_user_id,),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT user_id,payload::text FROM users "
                        "WHERE user_id=%s AND workspace_id=%s",
                        (membership_user_id, membership_workspace_id),
                    ).fetchone()
                if row is not None:
                    membership_rows["user"].append((row[0], row[1]))

            if membership_workspace_id is not None:
                row = conn.execute(
                    "SELECT workspace_id,payload::text FROM workspaces "
                    "WHERE workspace_id=%s",
                    (membership_workspace_id,),
                ).fetchone()
                if row is not None:
                    membership_rows["workspace"].append((row[0], row[1]))

            task_rows = conn.execute(
                "SELECT task_id,payload::text FROM agent_tasks "
                "WHERE workspace_id=%s ORDER BY task_id",
                (workspace_id,),
            ).fetchall()
            scope_rows = [(row[0], row[1]) for row in task_rows]

        return grouped, migration_rows, scope_rows, membership_rows
