from __future__ import annotations

import sqlite3
from typing import TypeVar

from pydantic import BaseModel

from .agent_credential_models import AgentCredentialRecord
from .agent_task_models import AgentTask, AgentTaskEvent
from .connector_models import AgentConnector

from .database_migrations import get_applied_migrations, run_migrations
from .models import (
    ApprovalRequest,
    AuditEvent,
    DeviceRegistration,
    ExternalAgent,
    Idea,
    Invite,
    MCPChildServer,
    MCPPendingRequest,
    Notification,
    Project,
    SessionToken,
    TestRun,
    User,
    WorkOrder,
)
from .settings import validate_startup_environment

T = TypeVar("T", bound=BaseModel)


class SQLiteBackedStore:
    """Small persistent MVP store backed by SQLite.

    This is intentionally lightweight. It keeps the current service code simple while
    making data survive backend restarts. A normalized relational schema can replace
    this once the API shape stabilizes.
    """

    def __init__(self) -> None:
        self.db_path = validate_startup_environment().db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.users: dict[str, User] = {}
        self.sessions: dict[str, SessionToken] = {}
        self.invites: dict[str, Invite] = {}
        self.external_agents: dict[str, ExternalAgent] = {}
        self.external_agent_credentials: dict[str, AgentCredentialRecord] = {}
        self.agent_connectors: dict[str, AgentConnector] = {}
        self.device_registrations: dict[str, DeviceRegistration] = {}
        self.agent_tasks: dict[str, AgentTask] = {}
        self.agent_task_events: dict[str, AgentTaskEvent] = {}
        self.projects: dict[str, Project] = {}
        self.mcp_child_servers: dict[str, MCPChildServer] = {}
        self.ideas: dict[str, Idea] = {}
        self.work_orders: dict[str, WorkOrder] = {}
        self.approval_requests: dict[str, ApprovalRequest] = {}
        self.mcp_pending_requests: dict[str, MCPPendingRequest] = {}
        self.test_runs: dict[str, TestRun] = {}
        self.notifications: dict[str, Notification] = {}
        self.audit_events: list[AuditEvent] = []
        self._init_db()
        self.load()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            run_migrations(conn)

    def applied_migrations(self) -> list[dict[str, str]]:
        with self._connect() as conn:
            return get_applied_migrations(conn)

    def _load_entities(self, entity_type: str, model: type[T]) -> dict[str, T]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT entity_id, payload FROM kv_store WHERE entity_type = ? ORDER BY entity_id",
                (entity_type,),
            ).fetchall()
        return {entity_id: model.model_validate_json(payload) for entity_id, payload in rows}

    def load(self) -> None:
        self.users = self._load_entities("user", User)
        self.sessions = self._load_entities("session", SessionToken)
        self.invites = self._load_entities("invite", Invite)
        self.external_agents = self._load_entities("external_agent", ExternalAgent)
        self.external_agent_credentials = self._load_entities(
            "external_agent_credential",
            AgentCredentialRecord,
        )
        self.agent_connectors = self._load_entities("agent_connector", AgentConnector)
        self.device_registrations = self._load_entities("device_registration", DeviceRegistration)
        self.agent_tasks = self._load_entities("agent_task", AgentTask)
        self.agent_task_events = self._load_entities("agent_task_event", AgentTaskEvent)
        self.projects = self._load_entities("project", Project)
        self.mcp_child_servers = self._load_entities("mcp_child_server", MCPChildServer)
        self.ideas = self._load_entities("idea", Idea)
        self.work_orders = self._load_entities("work_order", WorkOrder)
        self.approval_requests = self._load_entities("approval_request", ApprovalRequest)
        self.mcp_pending_requests = self._load_entities("mcp_pending_request", MCPPendingRequest)
        self.test_runs = self._load_entities("test_run", TestRun)
        self.notifications = self._load_entities("notification", Notification)
        self.audit_events = list(self._load_entities("audit_event", AuditEvent).values())

    def persist(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM kv_store")
            self._write_map(conn, "user", self.users)
            self._write_map(conn, "session", self.sessions)
            self._write_map(conn, "invite", self.invites)
            self._write_map(conn, "external_agent", self.external_agents)
            self._write_map(conn, "external_agent_credential", self.external_agent_credentials)
            self._write_map(conn, "agent_connector", self.agent_connectors)
            self._write_map(conn, "device_registration", self.device_registrations)
            self._write_map(conn, "agent_task", self.agent_tasks)
            self._write_map(conn, "agent_task_event", self.agent_task_events)
            self._write_map(conn, "project", self.projects)
            self._write_map(conn, "mcp_child_server", self.mcp_child_servers)
            self._write_map(conn, "idea", self.ideas)
            self._write_map(conn, "work_order", self.work_orders)
            self._write_map(conn, "approval_request", self.approval_requests)
            self._write_map(conn, "mcp_pending_request", self.mcp_pending_requests)
            self._write_map(conn, "test_run", self.test_runs)
            self._write_map(conn, "notification", self.notifications)
            self._write_list(conn, "audit_event", self.audit_events)

    @staticmethod
    def _write_map(
        conn: sqlite3.Connection,
        entity_type: str,
        values: dict[str, BaseModel],
    ) -> None:
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
        self.users.clear()
        self.sessions.clear()
        self.invites.clear()
        self.external_agents.clear()
        self.external_agent_credentials.clear()
        self.agent_connectors.clear()
        self.device_registrations.clear()
        self.agent_tasks.clear()
        self.agent_task_events.clear()
        self.projects.clear()
        self.mcp_child_servers.clear()
        self.ideas.clear()
        self.work_orders.clear()
        self.approval_requests.clear()
        self.mcp_pending_requests.clear()
        self.test_runs.clear()
        self.notifications.clear()
        self.audit_events.clear()
        with self._connect() as conn:
            conn.execute("DELETE FROM kv_store")


store = SQLiteBackedStore()
