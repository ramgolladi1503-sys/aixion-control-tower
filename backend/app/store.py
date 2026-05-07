from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from .models import ApprovalRequest, AuditEvent, Idea, Notification, Project, TestRun, WorkOrder

T = TypeVar("T", bound=BaseModel)


class SQLiteBackedStore:
    """Small persistent MVP store backed by SQLite.

    This is intentionally lightweight. It keeps the current service code simple while
    making data survive backend restarts. A normalized relational schema can replace
    this once the API shape stabilizes.
    """

    def __init__(self) -> None:
        db_path = os.getenv("AIXION_DB_PATH", "runtime/aixion_control_tower.sqlite3")
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.projects: dict[str, Project] = {}
        self.ideas: dict[str, Idea] = {}
        self.work_orders: dict[str, WorkOrder] = {}
        self.approval_requests: dict[str, ApprovalRequest] = {}
        self.test_runs: dict[str, TestRun] = {}
        self.notifications: dict[str, Notification] = {}
        self.audit_events: list[AuditEvent] = []
        self._init_db()
        self.load()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
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

    def _load_entities(self, entity_type: str, model: type[T]) -> dict[str, T]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT entity_id, payload FROM kv_store WHERE entity_type = ? ORDER BY entity_id",
                (entity_type,),
            ).fetchall()
        return {entity_id: model.model_validate_json(payload) for entity_id, payload in rows}

    def load(self) -> None:
        self.projects = self._load_entities("project", Project)
        self.ideas = self._load_entities("idea", Idea)
        self.work_orders = self._load_entities("work_order", WorkOrder)
        self.approval_requests = self._load_entities("approval_request", ApprovalRequest)
        self.test_runs = self._load_entities("test_run", TestRun)
        self.notifications = self._load_entities("notification", Notification)
        self.audit_events = list(self._load_entities("audit_event", AuditEvent).values())

    def persist(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM kv_store")
            self._write_map(conn, "project", self.projects)
            self._write_map(conn, "idea", self.ideas)
            self._write_map(conn, "work_order", self.work_orders)
            self._write_map(conn, "approval_request", self.approval_requests)
            self._write_map(conn, "test_run", self.test_runs)
            self._write_map(conn, "notification", self.notifications)
            self._write_list(conn, "audit_event", self.audit_events)

    @staticmethod
    def _write_map(conn: sqlite3.Connection, entity_type: str, values: dict[str, BaseModel]) -> None:
        for entity_id, model in values.items():
            conn.execute(
                "INSERT INTO kv_store(entity_type, entity_id, payload) VALUES (?, ?, ?)",
                (entity_type, entity_id, model.model_dump_json()),
            )

    @staticmethod
    def _write_list(conn: sqlite3.Connection, entity_type: str, values: list[BaseModel]) -> None:
        for model in values:
            entity_id = getattr(model, "id")
            conn.execute(
                "INSERT INTO kv_store(entity_type, entity_id, payload) VALUES (?, ?, ?)",
                (entity_type, entity_id, model.model_dump_json()),
            )

    def reset(self) -> None:
        self.projects.clear()
        self.ideas.clear()
        self.work_orders.clear()
        self.approval_requests.clear()
        self.test_runs.clear()
        self.notifications.clear()
        self.audit_events.clear()
        with self._connect() as conn:
            conn.execute("DELETE FROM kv_store")


store = SQLiteBackedStore()
