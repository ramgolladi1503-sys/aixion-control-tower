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


def create_auth_email_outbox(conn: sqlite3.Connection) -> None:
    """Create a dedicated, retryable queue for authentication email delivery."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_email_outbox (
            id TEXT PRIMARY KEY,
            recipient TEXT NOT NULL,
            encrypted_code BLOB NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('PENDING', 'SENDING', 'SENT', 'DEAD')),
            attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            created_at TEXT NOT NULL,
            next_attempt_at TEXT NOT NULL,
            lease_expires_at TEXT,
            sent_at TEXT,
            last_error TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_auth_email_outbox_due "
        "ON auth_email_outbox(status, next_attempt_at, lease_expires_at)"
    )


def create_auth_rate_limit_buckets(conn: sqlite3.Connection) -> None:
    """Create hashed fixed-window buckets for public authentication routes."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_rate_limit_buckets (
            bucket_hash TEXT NOT NULL,
            window_start INTEGER NOT NULL,
            request_count INTEGER NOT NULL CHECK (request_count >= 0),
            PRIMARY KEY (bucket_hash, window_start)
        )
        """
    )


def index_auth_rate_limit_expiry(conn: sqlite3.Connection) -> None:
    """Keep expired-bucket cleanup bounded as the authentication table grows."""
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_auth_rate_limit_window_start "
        "ON auth_rate_limit_buckets(window_start)"
    )


def index_approval_invocation_identity(conn: sqlite3.Connection) -> None:
    """Enforce one durable approval per authenticated connector invocation."""
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_invocation_identity "
        "ON kv_store(COALESCE(json_extract(payload,'$.workspace_id'),''), "
        "json_extract(payload,'$.source_agent_id'), "
        "json_extract(payload,'$.source_invocation_id')) "
        "WHERE entity_type='approval_request' "
        "AND json_type(payload,'$.source_invocation_id')='text'"
    )


def index_approval_expiry_due(conn: sqlite3.Connection) -> None:
    """Bound maintenance scans to pending approval rows and their deadlines."""
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kv_approval_expiry_due "
        "ON kv_store(json_extract(payload,'$.status'), "
        "json_extract(payload,'$.expires_at'), json_extract(payload,'$.created_at'), entity_id) "
        "WHERE entity_type='approval_request'"
    )


def create_approval_delivery_claims(conn: sqlite3.Connection) -> None:
    """Record one-time connector delivery of an approved invocation."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS approval_delivery_claims (
            approval_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            source_agent_id TEXT NOT NULL,
            source_invocation_id TEXT NOT NULL,
            claimed_at TEXT NOT NULL,
            UNIQUE (workspace_id, source_agent_id, source_invocation_id),
            CHECK (length(approval_id) BETWEEN 1 AND 256),
            CHECK (length(source_agent_id) BETWEEN 1 AND 256),
            CHECK (length(source_invocation_id) BETWEEN 1 AND 256),
            CHECK (length(claimed_at) > 0)
        )
        """
    )


def create_approval_delivery_failures(conn: sqlite3.Connection) -> None:
    """Record connector-reported failures after an approved delivery claim."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS approval_delivery_failures (
            approval_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            source_agent_id TEXT NOT NULL,
            source_invocation_id TEXT NOT NULL,
            reason_code TEXT NOT NULL CHECK (reason_code = 'CLAIM_RESPONSE_AMBIGUOUS'),
            reported_at TEXT NOT NULL,
            FOREIGN KEY (approval_id)
                REFERENCES approval_delivery_claims(approval_id) ON DELETE RESTRICT,
            CHECK (length(approval_id) BETWEEN 1 AND 256),
            CHECK (length(workspace_id) BETWEEN 1 AND 256),
            CHECK (length(source_agent_id) BETWEEN 1 AND 256),
            CHECK (length(source_invocation_id) BETWEEN 1 AND 256),
            CHECK (length(reported_at) > 0)
        )
        """
    )


def create_approval_invocation_leases(conn: sqlite3.Connection) -> None:
    """Persist the last authenticated liveness signal for a connector invocation."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS approval_invocation_leases (
            approval_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            source_agent_id TEXT NOT NULL,
            source_invocation_id TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            UNIQUE (workspace_id, source_agent_id, source_invocation_id),
            CHECK (length(approval_id) BETWEEN 1 AND 256),
            CHECK (length(workspace_id) BETWEEN 1 AND 256),
            CHECK (length(source_agent_id) BETWEEN 1 AND 256),
            CHECK (length(source_invocation_id) BETWEEN 1 AND 256),
            CHECK (length(heartbeat_at) > 0),
            CHECK (length(expires_at) > 0)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_approval_invocation_lease_expiry "
        "ON approval_invocation_leases(expires_at)"
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS approval_invocation_lease_identity_insert
        BEFORE INSERT ON approval_invocation_leases
        WHEN NOT EXISTS (
            SELECT 1 FROM kv_store AS approval
            WHERE approval.entity_type='approval_request'
              AND approval.entity_id=NEW.approval_id
              AND json_extract(approval.payload,'$.workspace_id')=NEW.workspace_id
              AND json_extract(approval.payload,'$.source_agent_id')=NEW.source_agent_id
              AND json_extract(approval.payload,'$.source_invocation_id')=NEW.source_invocation_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'invocation lease identity must match its approval');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS approval_invocation_lease_identity_update
        BEFORE UPDATE OF approval_id, workspace_id, source_agent_id, source_invocation_id
        ON approval_invocation_leases
        WHEN NOT EXISTS (
            SELECT 1 FROM kv_store AS approval
            WHERE approval.entity_type='approval_request'
              AND approval.entity_id=NEW.approval_id
              AND json_extract(approval.payload,'$.workspace_id')=NEW.workspace_id
              AND json_extract(approval.payload,'$.source_agent_id')=NEW.source_agent_id
              AND json_extract(approval.payload,'$.source_invocation_id')=NEW.source_invocation_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'invocation lease identity must match its approval');
        END
        """
    )


def enforce_delivery_failure_claim_identity(conn: sqlite3.Connection) -> None:
    """Bind every failure row to all identity columns of its durable claim."""
    inconsistent = conn.execute(
        """
        SELECT 1 FROM approval_delivery_failures AS failure
        LEFT JOIN approval_delivery_claims AS claim
          ON claim.approval_id = failure.approval_id
         AND claim.workspace_id = failure.workspace_id
         AND claim.source_agent_id = failure.source_agent_id
         AND claim.source_invocation_id = failure.source_invocation_id
        WHERE claim.approval_id IS NULL
        LIMIT 1
        """
    ).fetchone()
    if inconsistent is not None:
        raise RuntimeError(
            "Cannot enforce delivery failure claim identity: inconsistent rows exist"
        )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS approval_delivery_failure_claim_identity_insert
        BEFORE INSERT ON approval_delivery_failures
        WHEN NOT EXISTS (
            SELECT 1 FROM approval_delivery_claims AS claim
            WHERE claim.approval_id = NEW.approval_id
              AND claim.workspace_id = NEW.workspace_id
              AND claim.source_agent_id = NEW.source_agent_id
              AND claim.source_invocation_id = NEW.source_invocation_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'delivery failure identity must match its claim');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS approval_delivery_failure_claim_identity_update
        BEFORE UPDATE OF approval_id, workspace_id, source_agent_id, source_invocation_id
        ON approval_delivery_failures
        WHEN NOT EXISTS (
            SELECT 1 FROM approval_delivery_claims AS claim
            WHERE claim.approval_id = NEW.approval_id
              AND claim.workspace_id = NEW.workspace_id
              AND claim.source_agent_id = NEW.source_agent_id
              AND claim.source_invocation_id = NEW.source_invocation_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'delivery failure identity must match its claim');
        END
        """
    )
MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        migration_id="0001_baseline_kv_store",
        name="Create baseline key-value persistence table",
        apply=baseline_kv_store,
    ),
    Migration(
        migration_id="0002_auth_email_outbox",
        name="Create authentication email outbox",
        apply=create_auth_email_outbox,
    ),
    Migration(
        migration_id="0003_auth_rate_limit_buckets",
        name="Create hashed authentication rate-limit buckets",
        apply=create_auth_rate_limit_buckets,
    ),
    Migration(
        migration_id="0004_auth_rate_limit_expiry_index",
        name="Index authentication rate-limit bucket expiry",
        apply=index_auth_rate_limit_expiry,
    ),
    Migration(
        migration_id="0005_approval_invocation_identity",
        name="Enforce unique connector approval invocation identity",
        apply=index_approval_invocation_identity,
    ),
    Migration(
        migration_id="0006_approval_delivery_claims",
        name="Record at-most-once approved invocation delivery",
        apply=create_approval_delivery_claims,
    ),
    Migration(
        migration_id="0007_approval_delivery_failures",
        name="Record connector-reported ambiguous approved delivery failures",
        apply=create_approval_delivery_failures,
    ),
    Migration(
        migration_id="0008_delivery_failure_claim_identity",
        name="Bind delivery failure reports to the complete durable claim identity",
        apply=enforce_delivery_failure_claim_identity,
    ),
    Migration(
        migration_id="0009_approval_invocation_leases",
        name="Track scoped connector invocation liveness leases",
        apply=create_approval_invocation_leases,
    ),
    Migration(
        migration_id="0010_approval_expiry_due_index",
        name="Index bounded approval expiration selection",
        apply=index_approval_expiry_due,
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
    """Apply pending database migrations safely and idempotently.

    Take SQLite's write reservation before reading the migration ledger. Without
    this reservation, two processes starting together can both observe the same
    pending migration and race on the ledger's primary key after applying it.
    """
    if conn.in_transaction:
        raise RuntimeError("SQLite migrations require a connection without an active transaction")
    conn.execute("BEGIN IMMEDIATE")
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
