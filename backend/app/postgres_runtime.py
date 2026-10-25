from __future__ import annotations

import os
import re
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
from .postgres_database import PG_MIGRATION_TABLE, POSTGRES_MIGRATIONS, _migration_checksum
from .postgres_email_outbox_repository import PostgresEmailOutboxRepository
from .postgres_external_agent_auth_repository import (
    PostgresExternalAgentAuthRepository,
)
from .project_repository import PostgresProjectRepository
from .recovery_repository import PostgresRecoverySnapshotRepository
from .settings import Settings, get_settings
from .workspace_membership_repository import PostgresWorkspaceMembershipRepository
from .work_order_repository import PostgresWorkOrderRepository
from .postgres_agent_run_fault_repository import PostgresAgentRunFaultRepository

_APPLICATION_NAME = "aixion-control-tower"
_CREATE_TABLE_PATTERN = re.compile(
    r"\bCREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([a-z_][a-z0-9_]*)\b",
    re.IGNORECASE,
)
_ALTER_ADD_COLUMN_PATTERN = re.compile(
    r"\bALTER\s+TABLE\s+(?:ONLY\s+)?([a-z_][a-z0-9_]*)\s+"
    r"ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-z_][a-z0-9_]*)\b",
    re.IGNORECASE,
)
_TABLE_CONSTRAINT_STARTS = {
    "CHECK",
    "CONSTRAINT",
    "EXCLUDE",
    "FOREIGN",
    "PRIMARY",
    "UNIQUE",
}

# These are deliberately explicit, high-value tenant-isolation and approval
# identity invariants. Migration checksums prove the recorded migration source,
# while this manifest verifies that PostgreSQL still has the actual objects.
_REQUIRED_FOREIGN_KEYS = (
    {
        "name": "users_workspace_id_fkey",
        "table": "users",
        "columns": ("workspace_id",),
        "referenced_table": "workspaces",
        "referenced_columns": ("workspace_id",),
        "delete_action": "r",  # RESTRICT
        "update_action": "a",  # NO ACTION
        "match_type": "s",  # SIMPLE
    },
    {
        "name": "agent_tasks_project_id_workspace_id_fkey",
        "table": "agent_tasks",
        "columns": ("project_id", "workspace_id"),
        "referenced_table": "projects",
        "referenced_columns": ("project_id", "workspace_id"),
        "delete_action": "r",  # RESTRICT
        "update_action": "a",  # NO ACTION
        "match_type": "s",  # SIMPLE
    },
)
_REQUIRED_UNIQUE_INDEXES = (
    {
        "name": "idx_approval_invocation_identity",
        "table": "approval_requests",
        "columns": ("workspace_id", "source_agent_id", "source_invocation_id"),
        "predicate": "source_invocation_id IS NOT NULL",
    },
)


def _required_postgres_relations() -> tuple[str, ...]:
    """Return every table the applied migrations declare, plus their ledger."""
    names = {PG_MIGRATION_TABLE}
    for migration in POSTGRES_MIGRATIONS:
        for statement in migration.statements:
            names.update(_CREATE_TABLE_PATTERN.findall(statement))
    return tuple(sorted(names))


def _matching_parenthesis_end(source: str, opening: int) -> int | None:
    """Return the closing parenthesis for migration DDL, ignoring quoted text."""
    depth = 0
    quote: str | None = None
    index = opening
    while index < len(source):
        char = source[index]
        if quote is not None:
            if char == quote:
                if index + 1 < len(source) and source[index + 1] == quote:
                    index += 2
                    continue
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _top_level_definitions(source: str) -> tuple[str, ...]:
    """Split a CREATE TABLE body at commas outside nested/quoted expressions."""
    definitions: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    index = 0
    while index < len(source):
        char = source[index]
        if quote is not None:
            if char == quote:
                if index + 1 < len(source) and source[index + 1] == quote:
                    index += 2
                    continue
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            definitions.append(source[start:index])
            start = index + 1
        index += 1
    definitions.append(source[start:])
    return tuple(definitions)


def _required_postgres_columns() -> dict[str, tuple[str, ...]]:
    """Derive required table columns from the checked-in migration DDL.

    This checks column presence only. Migration checksums remain authoritative
    for declared DDL; the probe additionally detects out-of-band column loss.
    """
    columns: dict[str, set[str]] = {
        PG_MIGRATION_TABLE: {
            "migration_id",
            "name",
            "migration_checksum",
            "applied_at",
        }
    }
    for migration in POSTGRES_MIGRATIONS:
        for statement in migration.statements:
            for match in _CREATE_TABLE_PATTERN.finditer(statement):
                table = match.group(1).lower()
                opening = statement.find("(", match.end())
                if opening < 0:
                    continue
                closing = _matching_parenthesis_end(statement, opening)
                if closing is None:
                    continue
                table_columns = columns.setdefault(table, set())
                for definition in _top_level_definitions(statement[opening + 1 : closing]):
                    first_token = re.match(r"\s*([a-z_][a-z0-9_]*)\b", definition, re.I)
                    if first_token is None:
                        continue
                    name = first_token.group(1).lower()
                    if name.upper() not in _TABLE_CONSTRAINT_STARTS:
                        table_columns.add(name)
            for match in _ALTER_ADD_COLUMN_PATTERN.finditer(statement):
                table, column = match.groups()
                columns.setdefault(table.lower(), set()).add(column.lower())
    return {table: tuple(sorted(names)) for table, names in sorted(columns.items())}


@dataclass(frozen=True)
class PostgresSchemaStatus:
    ready: bool
    migration_ledger_present: bool
    applied_migration_ids: tuple[str, ...]
    missing_migration_ids: tuple[str, ...]
    unknown_migration_ids: tuple[str, ...]
    mismatched_migration_ids: tuple[str, ...]
    missing_relation_names: tuple[str, ...]
    missing_column_names: tuple[str, ...]
    missing_constraint_names: tuple[str, ...]
    missing_index_names: tuple[str, ...]


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
    def agent_run_faults(self) -> PostgresAgentRunFaultRepository:
        return PostgresAgentRunFaultRepository(self.connect)

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
        expected_columns = _required_postgres_columns()
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
                    missing_relation_names=_required_postgres_relations(),
                    missing_column_names=tuple(
                        f"{table}.{column}"
                        for table, names in expected_columns.items()
                        for column in names
                    ),
                    missing_constraint_names=tuple(
                        invariant["name"] for invariant in _REQUIRED_FOREIGN_KEYS
                    ),
                    missing_index_names=tuple(
                        invariant["name"] for invariant in _REQUIRED_UNIQUE_INDEXES
                    ),
                )
            rows = conn.execute(
                "SELECT migration_id,name,migration_checksum FROM schema_migrations"
            ).fetchall()
            missing_relations = tuple(
                sorted(
                    row[0]
                    for row in conn.execute(
                        "SELECT relation_name "
                        "FROM unnest(%s::text[]) AS expected(relation_name) "
                        "WHERE to_regclass(relation_name) IS NULL",
                        (list(_required_postgres_relations()),),
                    ).fetchall()
                )
            )
            present_columns = {
                (row[0], row[1])
                for row in conn.execute(
                    "SELECT table_name,column_name FROM information_schema.columns "
                    "WHERE table_schema=current_schema() AND table_name=ANY(%s)",
                    (list(expected_columns),),
                ).fetchall()
            }
            missing_columns = tuple(
                f"{table}.{column}"
                for table, names in expected_columns.items()
                for column in names
                if (table, column) not in present_columns
            )
            missing_constraints, missing_indexes = _missing_critical_schema_objects(conn)

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
            ready=(
                not missing_ids
                and not unknown_ids
                and not mismatched_ids
                and not missing_relations
                and not missing_columns
                and not missing_constraints
                and not missing_indexes
            ),
            migration_ledger_present=True,
            applied_migration_ids=applied_ids,
            missing_migration_ids=missing_ids,
            unknown_migration_ids=unknown_ids,
            mismatched_migration_ids=mismatched_ids,
            missing_relation_names=missing_relations,
            missing_column_names=missing_columns,
            missing_constraint_names=missing_constraints,
            missing_index_names=missing_indexes,
        )


def _missing_critical_schema_objects(conn: Connection) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return manifest objects that are absent or differ from their contract."""
    missing_constraints: list[str] = []
    for expected in _REQUIRED_FOREIGN_KEYS:
        row = conn.execute(
            """
            SELECT constraint_row.convalidated,
                   constraint_row.confdeltype,
                   constraint_row.confupdtype,
                   constraint_row.confmatchtype,
                   constraint_row.condeferrable,
                   constraint_row.condeferred,
                   ARRAY(
                       SELECT attribute.attname
                       FROM unnest(constraint_row.conkey) WITH ORDINALITY AS key(attnum, position)
                       JOIN pg_attribute AS attribute
                         ON attribute.attrelid = constraint_row.conrelid
                        AND attribute.attnum = key.attnum
                       ORDER BY key.position
                   ),
                   referenced_relation.relname,
                   ARRAY(
                       SELECT attribute.attname
                       FROM unnest(constraint_row.confkey) WITH ORDINALITY AS key(attnum, position)
                       JOIN pg_attribute AS attribute
                         ON attribute.attrelid = constraint_row.confrelid
                        AND attribute.attnum = key.attnum
                       ORDER BY key.position
                   )
            FROM pg_constraint AS constraint_row
            JOIN pg_class AS local_relation
              ON local_relation.oid = constraint_row.conrelid
            JOIN pg_namespace AS local_namespace
              ON local_namespace.oid = local_relation.relnamespace
            JOIN pg_class AS referenced_relation
              ON referenced_relation.oid = constraint_row.confrelid
            JOIN pg_namespace AS referenced_namespace
              ON referenced_namespace.oid = referenced_relation.relnamespace
            WHERE local_namespace.nspname = current_schema()
              AND referenced_namespace.nspname = current_schema()
              AND local_relation.relname = %s
              AND constraint_row.conname = %s
              AND constraint_row.contype = 'f'
            """,
            (expected["table"], expected["name"]),
        ).fetchone()
        if row is None or row != (
            True,
            expected["delete_action"],
            expected["update_action"],
            expected["match_type"],
            False,
            False,
            list(expected["columns"]),
            expected["referenced_table"],
            list(expected["referenced_columns"]),
        ):
            missing_constraints.append(expected["name"])

    missing_indexes: list[str] = []
    for expected in _REQUIRED_UNIQUE_INDEXES:
        row = conn.execute(
            """
            SELECT index_row.indisunique,
                   index_row.indimmediate,
                   index_row.indisvalid,
                   index_row.indisready,
                   index_method.amname,
                   index_row.indnkeyatts,
                   index_row.indnatts,
                   ARRAY(
                       SELECT pg_get_indexdef(index_row.indexrelid, position, true)
                       FROM generate_series(1, index_row.indnkeyatts) AS position
                       ORDER BY position
                   ),
                   regexp_replace(
                       pg_get_expr(index_row.indpred, index_row.indrelid),
                       '\\s+', ' ', 'g'
                   )
            FROM pg_index AS index_row
            JOIN pg_class AS index_relation
              ON index_relation.oid = index_row.indexrelid
            JOIN pg_am AS index_method
              ON index_method.oid = index_relation.relam
            JOIN pg_class AS table_relation
              ON table_relation.oid = index_row.indrelid
            JOIN pg_namespace AS table_namespace
              ON table_namespace.oid = table_relation.relnamespace
            WHERE table_namespace.nspname = current_schema()
              AND table_relation.relname = %s
              AND index_relation.relname = %s
            """,
            (expected["table"], expected["name"]),
        ).fetchone()
        predicate = " ".join((row[8] or "").replace("(", "").replace(")", "").split()) if row else ""
        if row is None or row[:8] != (
            True,
            True,
            True,
            True,
            "btree",
            len(expected["columns"]),
            len(expected["columns"]),
            list(expected["columns"]),
        ) or predicate != expected["predicate"]:
            missing_indexes.append(expected["name"])

    return tuple(missing_constraints), tuple(missing_indexes)
