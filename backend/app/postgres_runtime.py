from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field

import psycopg
from psycopg import Connection
from psycopg.conninfo import conninfo_to_dict
from psycopg_pool import ConnectionPool

from .application_repositories import ApplicationRepositories
from .agent_run_repository import PostgresAgentRunRepository
from .approval_repository import PostgresApprovalDecisionRepository
from .agent_task_repository import PostgresAgentTaskRepository
from .notification_outbox_repository import PostgresNotificationOutboxRepository
from .postgres_auth_rate_limits import PostgresAuthRateLimitRepository
from .postgres_auth_repository import PostgresAuthRepository
from .postgres_database import POSTGRES_MIGRATIONS, _migration_checksum
from .postgres_email_outbox_repository import PostgresEmailOutboxRepository
from .postgres_external_agent_auth_repository import (
    PostgresExternalAgentAuthRepository,
)
from .project_repository import PostgresProjectRepository
from .recovery_repository import PostgresRecoverySnapshotRepository
from .settings import Settings, get_settings
from .workspace_membership_repository import PostgresWorkspaceMembershipRepository
from .work_order_repository import PostgresWorkOrderRepository

_APPLICATION_NAME = "aixion-control-tower"


@dataclass(frozen=True)
class PostgresSchemaStatus:
    ready: bool
    migration_ledger_present: bool
    applied_migration_ids: tuple[str, ...]
    missing_migration_ids: tuple[str, ...]
    unknown_migration_ids: tuple[str, ...]
    mismatched_migration_ids: tuple[str, ...]


@dataclass(frozen=True)
class PostgresRuntimeProbe:
    schema: PostgresSchemaStatus
    pool_size: int
    pool_available: int
    pool_waiting: int

    @property
    def ready(self) -> bool:
        return self.schema.ready


@dataclass(frozen=True)
class PostgresRuntime:
    """Shared connection factory and PostgreSQL repositories.

    Connections are leased from a bounded pool for one repository transaction.
    This runtime does not run migrations or select itself as the application
    store. The owner must open and close the pool with its application lifecycle.
    """

    _dsn: str = field(repr=False)
    profile: str
    settings: Settings = field(default_factory=get_settings, repr=False, compare=False)
    _connector: Callable[..., Connection] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _pool: ConnectionPool | None = field(default=None, repr=False, compare=False)
    _repositories: ApplicationRepositories = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # Construct the repository bundle exactly once for this runtime. The
        # application lifespan publishes this object to routes and workers;
        # stable identity prevents components from accidentally binding to
        # distinct adapter instances with different lifecycle assumptions.
        object.__setattr__(
            self,
            "_repositories",
            ApplicationRepositories.from_connector(self.connect),
        )

    @classmethod
    def from_environment(
        cls,
        *,
        connector: Callable[..., Connection] | None = None,
    ) -> PostgresRuntime:
        dsn = os.getenv("AIXION_POSTGRES_DSN", "").strip()
        if not dsn:
            raise RuntimeError("AIXION_POSTGRES_DSN is required to create the PostgreSQL runtime")
        try:
            conninfo = conninfo_to_dict(dsn)
        except (psycopg.Error, ValueError):
            raise RuntimeError("AIXION_POSTGRES_DSN is invalid") from None

        settings = get_settings()
        profile = settings.profile
        if profile == "production" and conninfo.get("sslmode") != "verify-full":
            raise RuntimeError(
                "Production PostgreSQL connections require sslmode=verify-full"
            )
        return cls(
            _dsn=dsn,
            profile=profile,
            settings=settings,
            _connector=connector,
            _pool=(
                None
                if connector is not None
                else ConnectionPool(
                    conninfo=dsn,
                    kwargs=cls._connection_kwargs(settings),
                    min_size=settings.postgres_pool_min_size,
                    max_size=settings.postgres_pool_max_size,
                    timeout=settings.postgres_pool_wait_timeout_seconds,
                    max_waiting=settings.postgres_pool_max_waiting,
                    open=False,
                    name=_APPLICATION_NAME,
                )
            ),
        )

    @staticmethod
    def _connection_kwargs(settings: Settings) -> dict[str, object]:
        return {
            "connect_timeout": settings.postgres_connect_timeout_seconds,
            "application_name": _APPLICATION_NAME,
            "options": (
                f"-c statement_timeout={settings.postgres_statement_timeout_ms} "
                f"-c lock_timeout={settings.postgres_lock_timeout_ms} "
                "-c idle_in_transaction_session_timeout="
                f"{settings.postgres_idle_transaction_timeout_ms}"
            ),
        }

    def open(self, *, wait: bool = False) -> None:
        """Start pool workers; optionally wait for the configured minimum size."""
        if self._pool is not None:
            self._pool.open(wait=wait, timeout=self.settings.postgres_connect_timeout_seconds)

    def close(self) -> None:
        """Stop pool workers and return resources during application shutdown."""
        if self._pool is not None and not self._pool.closed:
            self._pool.close(timeout=self.settings.postgres_connect_timeout_seconds)

    def pool_stats(self) -> dict[str, int]:
        """Return bounded operational counters without DSN or connection data."""
        return self._pool.get_stats() if self._pool is not None else {}

    def probe(self) -> PostgresRuntimeProbe:
        """Return the shared schema and pool state used by startup and readiness."""
        stats = self.pool_stats()
        return PostgresRuntimeProbe(
            schema=self.verify_schema(),
            pool_size=max(0, stats.get("pool_size", 0)),
            pool_available=max(0, stats.get("pool_available", 0)),
            pool_waiting=max(0, stats.get("requests_waiting", 0)),
        )

    def connect(self):
        """Lease a transaction-scoped connection, or use an injected test connector."""
        if self._pool is not None:
            return self._pool.connection(timeout=self.settings.postgres_pool_wait_timeout_seconds)
        if self._connector is None:
            raise RuntimeError("PostgreSQL runtime has no connection provider")
        return self._connector(self._dsn, **self._connection_kwargs(self.settings))

    @property
    def auth(self) -> PostgresAuthRepository:
        return PostgresAuthRepository(self.connect)

    @property
    def auth_rate_limits(self) -> PostgresAuthRateLimitRepository:
        return PostgresAuthRateLimitRepository(self.connect)

    @property
    def email_outbox(self) -> PostgresEmailOutboxRepository:
        return PostgresEmailOutboxRepository(self.connect)

    @property
    def notification_outbox(self) -> PostgresNotificationOutboxRepository:
        return PostgresNotificationOutboxRepository(self.connect)

    @property
    def approval_decisions(self) -> PostgresApprovalDecisionRepository:
        return PostgresApprovalDecisionRepository(self.connect)

    @property
    def agent_tasks(self) -> PostgresAgentTaskRepository:
        return PostgresAgentTaskRepository(self.connect)

    @property
    def projects(self) -> PostgresProjectRepository:
        return PostgresProjectRepository(self.connect)

    @property
    def agent_runs(self) -> PostgresAgentRunRepository:
        return PostgresAgentRunRepository(self.connect)

    @property
    def workspace_membership(self) -> PostgresWorkspaceMembershipRepository:
        return PostgresWorkspaceMembershipRepository(self.connect)

    @property
    def external_agent_auth(self) -> PostgresExternalAgentAuthRepository:
        return PostgresExternalAgentAuthRepository(self.connect)

    @property
    def work_orders(self) -> PostgresWorkOrderRepository:
        return PostgresWorkOrderRepository(self.connect)

    @property
    def recovery_snapshots(self) -> PostgresRecoverySnapshotRepository:
        return self._repositories.recovery_snapshots

    @property
    def repositories(self) -> ApplicationRepositories:
        """Create the lifespan-owned repository bundle for this runtime."""
        return self._repositories

    def verify_schema(self) -> PostgresSchemaStatus:
        """Read migration state without applying or changing database schema."""
        with self.connect() as conn:
            ledger = conn.execute("SELECT to_regclass('schema_migrations')").fetchone()
            if ledger is None or ledger[0] is None:
                return PostgresSchemaStatus(
                    ready=False,
                    migration_ledger_present=False,
                    applied_migration_ids=(),
                    missing_migration_ids=tuple(
                        sorted(migration.migration_id for migration in POSTGRES_MIGRATIONS)
                    ),
                    unknown_migration_ids=(),
                    mismatched_migration_ids=(),
                )
            rows = conn.execute(
                "SELECT migration_id,name,migration_checksum FROM schema_migrations"
            ).fetchall()

        expected = {migration.migration_id: migration for migration in POSTGRES_MIGRATIONS}
        applied = {row[0]: row for row in rows}
        applied_ids = tuple(sorted(applied))
        missing_ids = tuple(sorted(set(expected) - set(applied)))
        unknown_ids = tuple(sorted(set(applied) - set(expected)))
        mismatched_ids = tuple(
            sorted(
                migration_id
                for migration_id, migration in expected.items()
                if migration_id in applied
                and (
                    applied[migration_id][1] != migration.name
                    or applied[migration_id][2] != _migration_checksum(migration)
                )
            )
        )
        return PostgresSchemaStatus(
            ready=not missing_ids and not unknown_ids and not mismatched_ids,
            migration_ledger_present=True,
            applied_migration_ids=applied_ids,
            missing_migration_ids=missing_ids,
            unknown_migration_ids=unknown_ids,
            mismatched_migration_ids=mismatched_ids,
        )
