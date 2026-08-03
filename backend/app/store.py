from __future__ import annotations

import sqlite3
import threading
from typing import TypeVar

from pydantic import BaseModel

from .agent_credential_models import AgentCredentialRecord
from .agent_run_models import AgentRun, AgentRunEvent, AgentRunFaultConfig, AgentRunStep
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
from .trust_models import (
    ActionConsumption,
    CapabilityLease,
    CredentialGrant,
    PolicyDecision,
    ProposedAction,
    ReviewerAttestation,
    TrustEvent,
)

T = TypeVar("T", bound=BaseModel)


class SQLiteBackedStore:
    """Persistent single-writer MVP store backed by SQLite.

    The API process remains the single owner of mutation. A re-entrant lock serializes
    writes across FastAPI worker threads. External schedulers and agents interact only
    through authenticated APIs and never mount the database directly.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
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
        self.agent_runs: dict[str, AgentRun] = {}
        self.agent_run_steps: dict[str, AgentRunStep] = {}
        self.agent_run_events: dict[str, AgentRunEvent] = {}
        self.agent_run_faults: dict[str, AgentRunFaultConfig] = {}
        self.capability_leases: dict[str, CapabilityLease] = {}
        self.reviewer_attestations: dict[str, ReviewerAttestation] = {}
        self.proposed_actions: dict[str, ProposedAction] = {}
        self.policy_decisions: dict[str, PolicyDecision] = {}
        self.action_consumptions: dict[str, ActionConsumption] = {}
        self.credential_grants: dict[str, CredentialGrant] = {}
        self.trust_events: dict[str, TrustEvent] = {}
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
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            run_migrations(conn)

    def applied_migrations(self) -> list[dict[str, str]]:
        with self._lock, self._connect() as conn:
            return get_applied_migrations(conn)

    def _load_entities(self, entity_type: str, model: type[T]) -> dict[str, T]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT entity_id, payload FROM kv_store WHERE entity_type = ? ORDER BY entity_id",
                (entity_type,),
            ).fetchall()
        return {
            entity_id: model.model_validate_json(payload)
            for entity_id, payload in rows
        }

    def load(self) -> None:
        with self._lock:
            self.users = self._load_entities("user", User)
            self.sessions = self._load_entities("session", SessionToken)
            self.invites = self._load_entities("invite", Invite)
            self.external_agents = self._load_entities("external_agent", ExternalAgent)
            self.external_agent_credentials = self._load_entities(
                "external_agent_credential",
                AgentCredentialRecord,
            )
            self.agent_connectors = self._load_entities("agent_connector", AgentConnector)
            self.device_registrations = self._load_entities(
                "device_registration",
                DeviceRegistration,
            )
            self.agent_tasks = self._load_entities("agent_task", AgentTask)
            self.agent_task_events = self._load_entities(
                "agent_task_event",
                AgentTaskEvent,
            )
            self.agent_runs = self._load_entities("agent_run", AgentRun)
            self.agent_run_steps = self._load_entities("agent_run_step", AgentRunStep)
            self.agent_run_events = self._load_entities("agent_run_event", AgentRunEvent)
            self.agent_run_faults = self._load_entities(
                "agent_run_fault",
                AgentRunFaultConfig,
            )
            self.capability_leases = self._load_entities(
                "capability_lease",
                CapabilityLease,
            )
            self.reviewer_attestations = self._load_entities(
                "reviewer_attestation",
                ReviewerAttestation,
            )
            self.proposed_actions = self._load_entities(
                "proposed_action",
                ProposedAction,
            )
            self.policy_decisions = self._load_entities(
                "policy_decision",
                PolicyDecision,
            )
            self.action_consumptions = self._load_entities(
                "action_consumption",
                ActionConsumption,
            )
            self.credential_grants = self._load_entities(
                "credential_grant",
                CredentialGrant,
            )
            self.trust_events = self._load_entities("trust_event", TrustEvent)
            self.projects = self._load_entities("project", Project)
            self.mcp_child_servers = self._load_entities(
                "mcp_child_server",
                MCPChildServer,
            )
            self.ideas = self._load_entities("idea", Idea)
            self.work_orders = self._load_entities("work_order", WorkOrder)
            self.approval_requests = self._load_entities(
                "approval_request",
                ApprovalRequest,
            )
            self.mcp_pending_requests = self._load_entities(
                "mcp_pending_request",
                MCPPendingRequest,
            )
            self.test_runs = self._load_entities("test_run", TestRun)
            self.notifications = self._load_entities("notification", Notification)
            self.audit_events = list(
                self._load_entities("audit_event", AuditEvent).values()
            )

    def persist(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM kv_store")
            self._write_map(conn, "user", self.users)
            self._write_map(conn, "session", self.sessions)
            self._write_map(conn, "invite", self.invites)
            self._write_map(conn, "external_agent", self.external_agents)
            self._write_map(
                conn,
                "external_agent_credential",
                self.external_agent_credentials,
            )
            self._write_map(conn, "agent_connector", self.agent_connectors)
            self._write_map(conn, "device_registration", self.device_registrations)
            self._write_map(conn, "agent_task", self.agent_tasks)
            self._write_map(conn, "agent_task_event", self.agent_task_events)
            self._write_map(conn, "agent_run", self.agent_runs)
            self._write_map(conn, "agent_run_step", self.agent_run_steps)
            self._write_map(conn, "agent_run_event", self.agent_run_events)
            self._write_map(conn, "agent_run_fault", self.agent_run_faults)
            self._write_map(conn, "capability_lease", self.capability_leases)
            self._write_map(
                conn,
                "reviewer_attestation",
                self.reviewer_attestations,
            )
            self._write_map(conn, "proposed_action", self.proposed_actions)
            self._write_map(conn, "policy_decision", self.policy_decisions)
            self._write_map(conn, "action_consumption", self.action_consumptions)
            self._write_map(conn, "credential_grant", self.credential_grants)
            self._write_map(conn, "trust_event", self.trust_events)
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
    def _write_list(
        conn: sqlite3.Connection,
        entity_type: str,
        values: list[BaseModel],
    ) -> None:
        for model in values:
            entity_id = getattr(model, "id")
            conn.execute(
                "INSERT INTO kv_store(entity_type, entity_id, payload) VALUES (?, ?, ?)",
                (entity_type, entity_id, model.model_dump_json()),
            )

    def reset(self) -> None:
        with self._lock:
            self.users.clear()
            self.sessions.clear()
            self.invites.clear()
            self.external_agents.clear()
            self.external_agent_credentials.clear()
            self.agent_connectors.clear()
            self.device_registrations.clear()
            self.agent_tasks.clear()
            self.agent_task_events.clear()
            self.agent_runs.clear()
            self.agent_run_steps.clear()
            self.agent_run_events.clear()
            self.agent_run_faults.clear()
            self.capability_leases.clear()
            self.reviewer_attestations.clear()
            self.proposed_actions.clear()
            self.policy_decisions.clear()
            self.action_consumptions.clear()
            self.credential_grants.clear()
            self.trust_events.clear()
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
