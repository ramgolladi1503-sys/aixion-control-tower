from __future__ import annotations

import hmac
import os
import sqlite3
import stat
import threading
from contextlib import contextmanager
from typing import Callable, Iterator, TypeVar

from pydantic import BaseModel

from .agent_credential_models import AgentCredentialRecord
from .agent_run_models import AgentRun, AgentRunEvent, AgentRunFaultConfig, AgentRunStep
from .agent_task_models import AgentTask, AgentTaskEvent
from .connector_models import AgentConnector
from .database_migrations import get_applied_migrations, run_migrations
from .device_registration_repository import SQLiteDeviceRegistrationRepository
from .work_order_repository import SQLiteWorkOrderRepository
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
    Workspace,
    now_utc,
)
from .settings import validate_startup_environment
from .trust_action_authorization_models import ActionAuthorization
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
        settings = validate_startup_environment()
        if settings.is_production:
            raise RuntimeError("SQLite-backed store cannot initialize with the production profile")
        self.db_path = settings.db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.users: dict[str, User] = {}
        self.workspaces: dict[str, Workspace] = {}
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
        self.action_authorizations: dict[str, ActionAuthorization] = {}
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
        self.device_registration_repository = SQLiteDeviceRegistrationRepository(self._connect)
        self.work_order_repository = SQLiteWorkOrderRepository(self._connect)
        self._init_db()
        self.load()

    @contextmanager
    def atomic(self) -> Iterator[None]:
        """Serialize a read-modify-persist sequence inside the API process."""
        with self._lock:
            yield

    def revoke_session(self, *, actor_user_id: str, token_hash: str) -> bool:
        """Revoke one SQLite-backed bearer session and persist its audit event."""
        with self.atomic():
            session = next(
                (
                    item
                    for item in self.sessions.values()
                    if hmac.compare_digest(item.token_hash, token_hash)
                    and item.user_id == actor_user_id
                ),
                None,
            )
            if session is None or session.revoked or session.expires_at <= now_utc():
                return False
            user = self.users.get(actor_user_id)
            if user is None or user.disabled or not user.email_verified:
                return False
            session.revoked = True
            self.audit_events.append(
                AuditEvent(
                    event_type="auth.session_revoked",
                    actor=user.email,
                    entity_id=session.id,
                    workspace_id=user.workspace_id,
                    details={"session_id": session.id, "reason": "user_logout"},
                )
            )
            try:
                self.persist()
            except Exception:
                self.load()
                raise
            return True

    def project_agent_task_creation(
        self,
        task: AgentTask,
        event: AgentTaskEvent,
        audit_event: AuditEvent,
    ) -> None:
        """Refresh compatibility maps after the repository commits creation."""
        if task.id != event.task_id or audit_event.entity_id != task.id:
            raise ValueError("AgentTask cache projection identities do not match.")
        with self._lock:
            self.agent_tasks[task.id] = task
            self.agent_task_events[event.id] = event
            if all(existing.id != audit_event.id for existing in self.audit_events):
                self.audit_events.append(audit_event)

    def project_work_order_creation(
        self,
        work_order: WorkOrder,
        audit_event: AuditEvent,
    ) -> None:
        """Publish a committed WorkOrder and its audit event to local readers."""
        if (
            audit_event.event_type
            not in {"work_order.created", "work_order.created_by_agent"}
            or audit_event.entity_id != work_order.id
            or audit_event.workspace_id != work_order.workspace_id
        ):
            raise ValueError("WorkOrder cache projection identities do not match.")
        with self._lock:
            self.work_orders[work_order.id] = work_order
            if all(existing.id != audit_event.id for existing in self.audit_events):
                self.audit_events.append(audit_event)

    def project_agent_run_creation(
        self,
        run: AgentRun,
        steps: list[AgentRunStep],
        events: list[AgentRunEvent],
        audit_event: AuditEvent,
    ) -> None:
        """Publish a committed AgentRun creation into compatibility maps."""
        if (
            any(step.run_id != run.id or step.task_id != run.task_id for step in steps)
            or any(event.run_id != run.id or event.task_id != run.task_id for event in events)
            or audit_event.entity_id != run.id
            or audit_event.workspace_id != run.workspace_id
        ):
            raise ValueError("AgentRun creation cache projection identities do not match.")
        with self._lock:
            self.agent_runs[run.id] = run
            self.agent_run_steps.update({step.id: step for step in steps})
            self.agent_run_events.update({event.id: event for event in events})
            if all(existing.id != audit_event.id for existing in self.audit_events):
                self.audit_events.append(audit_event)

    def project_agent_run_snapshot(
        self,
        run: AgentRun,
        steps: list[AgentRunStep],
        events: list[AgentRunEvent],
    ) -> None:
        """Refresh the SQLite compatibility projection from a durable run snapshot."""
        if (
            any(step.run_id != run.id or step.task_id != run.task_id for step in steps)
            or any(event.run_id != run.id or event.task_id != run.task_id for event in events)
        ):
            raise ValueError("AgentRun snapshot identities do not match.")
        with self._lock:
            existing_run = self.agent_runs.get(run.id)
            if existing_run is not None and existing_run is not run:
                for field_name in AgentRun.model_fields:
                    setattr(existing_run, field_name, getattr(run, field_name))
                run = existing_run
            self.agent_runs[run.id] = run
            step_ids = {step.id for step in steps}
            for step_id, existing_step in list(self.agent_run_steps.items()):
                if existing_step.run_id == run.id and step_id not in step_ids:
                    del self.agent_run_steps[step_id]
            for step in steps:
                existing_step = self.agent_run_steps.get(step.id)
                if existing_step is not None and existing_step is not step:
                    for field_name in AgentRunStep.model_fields:
                        setattr(existing_step, field_name, getattr(step, field_name))
                    step = existing_step
                self.agent_run_steps[step.id] = step
            event_ids = {event.id for event in events}
            for event_id, existing_event in list(self.agent_run_events.items()):
                if existing_event.run_id == run.id and event_id not in event_ids:
                    del self.agent_run_events[event_id]
            self.agent_run_events.update({event.id: event for event in events})

    def project_agent_run_lease(
        self,
        run: AgentRun,
        event: AgentRunEvent,
        audit_event: AuditEvent,
        step: AgentRunStep | None = None,
    ) -> None:
        """Publish a committed AgentRun lease and its audit trail."""
        if (
            event.run_id != run.id
            or event.task_id != run.task_id
            or (event.step_id is not None and event.step_id != (step.id if step else run.current_step_id))
            or (step is not None and (step.run_id != run.id or step.task_id != run.task_id))
            or audit_event.entity_id != run.id
            or audit_event.workspace_id != run.workspace_id
        ):
            raise ValueError("AgentRun lease cache projection identities do not match.")
        with self._lock:
            existing_run = self.agent_runs.get(run.id)
            if existing_run is not None and existing_run is not run:
                for field_name in AgentRun.model_fields:
                    setattr(existing_run, field_name, getattr(run, field_name))
                run = existing_run
            self.agent_runs[run.id] = run
            if step is not None:
                existing_step = self.agent_run_steps.get(step.id)
                if existing_step is not None and existing_step is not step:
                    for field_name in AgentRunStep.model_fields:
                        setattr(existing_step, field_name, getattr(step, field_name))
                    step = existing_step
                self.agent_run_steps[step.id] = step
            self.agent_run_events[event.id] = event
            if all(existing.id != audit_event.id for existing in self.audit_events):
                self.audit_events.append(audit_event)

    def project_agent_run_recovery(
        self,
        run: AgentRun,
        step: AgentRunStep | None,
        event: AgentRunEvent,
        audit_event: AuditEvent,
    ) -> None:
        """Publish a committed stale-lease recovery into compatibility maps."""
        if (
            event.run_id != run.id
            or event.task_id != run.task_id
            or event.step_id != (step.id if step else None)
            or (step is not None and (step.run_id != run.id or step.task_id != run.task_id))
            or audit_event.event_type != "agent_run.stale_lease_recovered"
            or audit_event.entity_id != run.id
            or audit_event.workspace_id != run.workspace_id
        ):
            raise ValueError("AgentRun recovery cache projection identities do not match.")
        with self._lock:
            existing_run = self.agent_runs.get(run.id)
            if existing_run is not None and existing_run is not run:
                for field_name in AgentRun.model_fields:
                    setattr(existing_run, field_name, getattr(run, field_name))
                run = existing_run
            self.agent_runs[run.id] = run
            if step is not None:
                existing_step = self.agent_run_steps.get(step.id)
                if existing_step is not None and existing_step is not step:
                    for field_name in AgentRunStep.model_fields:
                        setattr(existing_step, field_name, getattr(step, field_name))
                    step = existing_step
                self.agent_run_steps[step.id] = step
            self.agent_run_events[event.id] = event
            if all(existing.id != audit_event.id for existing in self.audit_events):
                self.audit_events.append(audit_event)

    def project_agent_run_control_transition(
        self,
        run: AgentRun,
        steps: list[AgentRunStep],
        events: list[AgentRunEvent],
        audit_events: list[AuditEvent],
    ) -> None:
        """Publish a committed operator transition into compatibility maps."""
        if (
            any(step.run_id != run.id or step.task_id != run.task_id for step in steps)
            or any(event.run_id != run.id or event.task_id != run.task_id for event in events)
            or any(
                audit.entity_id != run.id or audit.workspace_id != run.workspace_id
                for audit in audit_events
            )
        ):
            raise ValueError("AgentRun control transition projection identities do not match.")
        with self._lock:
            existing_run = self.agent_runs.get(run.id)
            if existing_run is not None and existing_run is not run:
                for field_name in AgentRun.model_fields:
                    setattr(existing_run, field_name, getattr(run, field_name))
                run = existing_run
            self.agent_runs[run.id] = run
            for step in steps:
                existing_step = self.agent_run_steps.get(step.id)
                if existing_step is not None and existing_step is not step:
                    for field_name in AgentRunStep.model_fields:
                        setattr(existing_step, field_name, getattr(step, field_name))
                    step = existing_step
                self.agent_run_steps[step.id] = step
            self.agent_run_events.update({event.id: event for event in events})
            existing_audit_ids = {event.id for event in self.audit_events}
            self.audit_events.extend(
                event for event in audit_events if event.id not in existing_audit_ids
            )

    def project_agent_task_transition(
        self,
        task: AgentTask,
        event: AgentTaskEvent,
        audit_event: AuditEvent,
        notification: Notification | None = None,
    ) -> None:
        """Refresh task compatibility maps only after a durable transition commits."""
        if task.id != event.task_id or audit_event.entity_id != task.id:
            raise ValueError("AgentTask transition cache projection identities do not match.")
        with self._lock:
            self.agent_tasks[task.id] = task
            self.agent_task_events[event.id] = event
            if notification is not None:
                self.notifications[notification.id] = notification
            if all(existing.id != audit_event.id for existing in self.audit_events):
                self.audit_events.append(audit_event)

    def project_agent_task_cancellation(
        self,
        task: AgentTask,
        task_event: AgentTaskEvent,
        task_audit: AuditEvent,
        approval: ApprovalRequest,
        approval_audit: AuditEvent,
        notification: Notification,
    ) -> None:
        """Publish a committed task and linked approval cancellation together."""
        if (
            task.id != task_event.task_id
            or task_audit.entity_id != task.id
            or task.approval_request_id != approval.id
            or approval_audit.entity_id != approval.id
            or notification.approval_request_id != approval.id
        ):
            raise ValueError("AgentTask cancellation cache projection identities do not match.")
        with self._lock:
            self.agent_tasks[task.id] = task
            self.agent_task_events[task_event.id] = task_event
            self.approval_requests[approval.id] = approval
            self.notifications[notification.id] = notification
            existing_audit_ids = {event.id for event in self.audit_events}
            self.audit_events.extend(
                event for event in (task_audit, approval_audit)
                if event.id not in existing_audit_ids
            )

    def _connect(self) -> sqlite3.Connection:
        self._secure_database_files(create_main=True)
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        try:
            self._secure_database_files(create_main=False)
            connection.execute("PRAGMA busy_timeout = 30000")
            return connection
        except Exception:
            connection.close()
            raise

    def _secure_database_files(self, *, create_main: bool) -> None:
        """Keep local SQLite data and any existing journal sidecars private."""
        parent = self.db_path.parent.stat()
        if parent.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not parent.st_mode & stat.S_ISVTX:
            raise PermissionError("SQLite database directory must not be writable by other users")

        paths = [(self.db_path, create_main)] + [
            (self.db_path.with_name(self.db_path.name + suffix), False)
            for suffix in ("-wal", "-shm", "-journal")
        ]
        for path, create in paths:
            is_transient_sidecar = path != self.db_path
            if create and not os.path.lexists(path):
                try:
                    descriptor = os.open(
                        path,
                        os.O_CREAT | os.O_EXCL | os.O_RDWR,
                        0o600,
                    )
                except FileExistsError:
                    pass
                else:
                    os.close(descriptor)
            if not os.path.lexists(path):
                continue
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                # SQLite removes journals and WAL sidecars as transactions
                # finish. Another connection can remove one after lexists()
                # succeeds but before lstat() runs; there is then no file left
                # to validate or chmod. Keep missing main database failures
                # visible because the database path itself is not transient.
                if is_transient_sidecar:
                    continue
                raise
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise PermissionError("SQLite database files must be regular files owned by the API user")
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                try:
                    os.chmod(path, 0o600)
                except FileNotFoundError:
                    if not is_transient_sidecar:
                        raise

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
            self.workspaces = self._load_entities("workspace", Workspace)
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
            self.action_authorizations = self._load_entities(
                "action_authorization",
                ActionAuthorization,
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

    def persist(self, *, extra_write: Callable[[sqlite3.Connection], None] | None = None) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM kv_store")
            self._write_map(conn, "user", self.users)
            self._write_map(conn, "workspace", self.workspaces)
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
            self._write_map(conn, "action_authorization", self.action_authorizations)
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
            if extra_write is not None:
                extra_write(conn)

    def persist_records(
        self,
        *,
        inserts: list[tuple[str, str, BaseModel]] | None = None,
    ) -> None:
        """Insert append-only records without rewriting unrelated process state."""
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for entity_type, entity_id, model in inserts or []:
                conn.execute(
                    "INSERT INTO kv_store(entity_type, entity_id, payload) VALUES (?, ?, ?)",
                    (entity_type, entity_id, model.model_dump_json()),
                )
            conn.commit()

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
            self.workspaces.clear()
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
            self.action_authorizations.clear()
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
                conn.execute("DELETE FROM auth_email_outbox")
                conn.execute("DELETE FROM auth_rate_limit_buckets")


store = SQLiteBackedStore()
