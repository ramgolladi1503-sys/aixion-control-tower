from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from psycopg import Connection

from .agent_task_models import AgentTaskStatus
from .models import AgentAuthType, AgentProvider, ApprovalStatus, UserRole

PG_MIGRATION_TABLE = "schema_migrations"
PG_MIGRATION_LOCK_ID = 71429831177


@dataclass(frozen=True)
class PostgresMigration:
    migration_id: str
    name: str
    statements: tuple[str, ...]


def _migration_checksum(migration: PostgresMigration) -> str:
    canonical = json.dumps(
        {
            "migration_id": migration.migration_id,
            "name": migration.name,
            "statements": migration.statements,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _sql_values(values: tuple[str, ...]) -> str:
    return ", ".join("'" + value.replace("'", "''") + "'" for value in values)


def _approval_transaction_schema() -> tuple[str, ...]:
    approval_statuses = _sql_values(tuple(status.value for status in ApprovalStatus))
    task_statuses = _sql_values(tuple(status.value for status in AgentTaskStatus))
    user_roles = _sql_values(tuple(role.value for role in UserRole))
    agent_providers = _sql_values(tuple(provider.value for provider in AgentProvider))
    agent_auth_types = _sql_values(tuple(auth_type.value for auth_type in AgentAuthType))
    return (
        """
        CREATE TABLE IF NOT EXISTS workspaces (
            workspace_id TEXT PRIMARY KEY,
            payload JSONB NOT NULL,
            CHECK (payload ? 'id' AND payload->>'id' = workspace_id)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
            email TEXT NOT NULL,
            normalized_email TEXT NOT NULL UNIQUE,
            role TEXT NOT NULL CHECK (role IN ({user_roles})),
            disabled BOOLEAN NOT NULL,
            email_verified BOOLEAN NOT NULL,
            payload JSONB NOT NULL,
            UNIQUE (user_id, workspace_id),
            CHECK (payload ? 'id' AND payload->>'id' = user_id),
            CHECK (payload ? 'workspace_id' AND payload->>'workspace_id' = workspace_id),
            CHECK (payload ? 'email' AND lower(btrim(payload->>'email')) = normalized_email),
            CHECK (payload ? 'role' AND payload->>'role' = role),
            CHECK (payload ? 'disabled' AND (payload->>'disabled')::boolean = disabled),
            CHECK (payload ? 'email_verified' AND (payload->>'email_verified')::boolean = email_verified)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_users_workspace_role ON users(workspace_id, role, user_id)",
        """
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
            token_hash TEXT NOT NULL UNIQUE,
            created_at TIMESTAMPTZ NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            revoked BOOLEAN NOT NULL,
            payload JSONB NOT NULL,
            CHECK (payload ? 'id' AND payload->>'id' = session_id),
            CHECK (payload ? 'user_id' AND payload->>'user_id' = user_id),
            CHECK (payload ? 'token_hash' AND payload->>'token_hash' = token_hash),
            CHECK (payload ? 'revoked' AND (payload->>'revoked')::boolean = revoked)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_sessions_user_expiry ON sessions(user_id, expires_at)",
        """
        CREATE TABLE IF NOT EXISTS projects (
            project_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
            payload JSONB NOT NULL,
            UNIQUE (project_id, workspace_id),
            CHECK (payload ? 'id' AND payload->>'id' = project_id),
            CHECK (payload ? 'workspace_id' AND payload->>'workspace_id' = workspace_id)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS external_agents (
            agent_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
            created_by_user_id TEXT,
            provider TEXT NOT NULL CHECK (provider IN ({agent_providers})),
            auth_type TEXT NOT NULL CHECK (auth_type IN ({agent_auth_types})),
            enabled BOOLEAN NOT NULL,
            secret_hash TEXT,
            payload JSONB NOT NULL,
            UNIQUE (agent_id, workspace_id),
            FOREIGN KEY (created_by_user_id, workspace_id)
                REFERENCES users(user_id, workspace_id) ON DELETE RESTRICT,
            CHECK (payload ? 'id' AND payload->>'id' = agent_id),
            CHECK (payload ? 'workspace_id' AND payload->>'workspace_id' = workspace_id),
            CHECK (payload ? 'provider' AND payload->>'provider' = provider),
            CHECK (payload ? 'auth_type' AND payload->>'auth_type' = auth_type),
            CHECK (payload ? 'enabled' AND (payload->>'enabled')::boolean = enabled),
            CHECK (
                payload ? 'created_by_user_id'
                AND (payload->>'created_by_user_id') IS NOT DISTINCT FROM created_by_user_id
            ),
            CHECK (
                payload ? 'secret_hash'
                AND (payload->>'secret_hash') IS NOT DISTINCT FROM secret_hash
            )
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_external_agents_workspace ON external_agents(workspace_id, agent_id)",
        """
        CREATE TABLE IF NOT EXISTS external_agent_credentials (
            credential_id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL UNIQUE,
            workspace_id TEXT NOT NULL,
            token_expires_at TIMESTAMPTZ,
            token_revoked_at TIMESTAMPTZ,
            token_rotated_at TIMESTAMPTZ,
            last_used_at TIMESTAMPTZ,
            failed_auth_count INTEGER NOT NULL CHECK (failed_auth_count >= 0),
            rate_limit_per_minute INTEGER NOT NULL CHECK (rate_limit_per_minute BETWEEN 1 AND 1000),
            rate_limit_window_started_at TIMESTAMPTZ,
            rate_limit_request_count INTEGER NOT NULL CHECK (rate_limit_request_count >= 0),
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            payload JSONB NOT NULL,
            FOREIGN KEY (agent_id, workspace_id)
                REFERENCES external_agents(agent_id, workspace_id) ON DELETE RESTRICT,
            CHECK (payload ? 'id' AND payload->>'id' = credential_id),
            CHECK (payload ? 'agent_id' AND payload->>'agent_id' = agent_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS device_registrations (
            device_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            token TEXT NOT NULL UNIQUE,
            app_version TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            payload JSONB NOT NULL,
            FOREIGN KEY (user_id, workspace_id)
                REFERENCES users(user_id, workspace_id) ON DELETE RESTRICT,
            CHECK (payload ? 'id' AND payload->>'id' = device_id),
            CHECK (payload ? 'user_id' AND payload->>'user_id' = user_id),
            CHECK (payload ? 'workspace_id' AND payload->>'workspace_id' = workspace_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_device_registrations_user ON device_registrations(user_id, device_id)",
        """
        CREATE TABLE IF NOT EXISTS ideas (
            idea_id TEXT PRIMARY KEY,
            project_id TEXT,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
            created_at TIMESTAMPTZ NOT NULL,
            payload JSONB NOT NULL,
            FOREIGN KEY (project_id, workspace_id)
                REFERENCES projects(project_id, workspace_id) ON DELETE RESTRICT,
            UNIQUE (idea_id, project_id),
            CHECK (payload ? 'id' AND payload->>'id' = idea_id),
            CHECK (
                payload ? 'project_id'
                AND (payload->>'project_id') IS NOT DISTINCT FROM project_id
            ),
            CHECK (payload ? 'workspace_id' AND payload->>'workspace_id' = workspace_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_ideas_workspace_project ON ideas(workspace_id, project_id, idea_id)",
        """
        CREATE TABLE IF NOT EXISTS work_orders (
            work_order_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
            idea_id TEXT,
            source_agent_id TEXT,
            created_at TIMESTAMPTZ NOT NULL,
            payload JSONB NOT NULL,
            FOREIGN KEY (project_id, workspace_id)
                REFERENCES projects(project_id, workspace_id) ON DELETE RESTRICT,
            FOREIGN KEY (idea_id, project_id)
                REFERENCES ideas(idea_id, project_id) ON DELETE RESTRICT,
            FOREIGN KEY (source_agent_id, workspace_id)
                REFERENCES external_agents(agent_id, workspace_id) ON DELETE RESTRICT,
            UNIQUE (work_order_id, project_id),
            CHECK (payload ? 'id' AND payload->>'id' = work_order_id),
            CHECK (payload ? 'project_id' AND payload->>'project_id' = project_id),
            CHECK (payload ? 'workspace_id' AND payload->>'workspace_id' = workspace_id),
            CHECK (
                payload ? 'idea_id'
                AND (payload->>'idea_id') IS NOT DISTINCT FROM idea_id
            ),
            CHECK (
                payload ? 'source_agent_id'
                AND (payload->>'source_agent_id') IS NOT DISTINCT FROM source_agent_id
            )
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_work_orders_workspace_project ON work_orders(workspace_id, project_id, created_at)",
        f"""
        CREATE TABLE IF NOT EXISTS approval_requests (
            approval_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
            work_order_id TEXT,
            source_agent_id TEXT,
            status TEXT NOT NULL CHECK (status IN ({approval_statuses})),
            expires_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
            payload JSONB NOT NULL,
            FOREIGN KEY (project_id, workspace_id)
                REFERENCES projects(project_id, workspace_id) ON DELETE RESTRICT,
            FOREIGN KEY (source_agent_id, workspace_id)
                REFERENCES external_agents(agent_id, workspace_id) ON DELETE RESTRICT,
            FOREIGN KEY (work_order_id, project_id)
                REFERENCES work_orders(work_order_id, project_id) ON DELETE RESTRICT,
            UNIQUE (approval_id, workspace_id),
            CHECK (payload ? 'id' AND payload->>'id' = approval_id),
            CHECK (payload ? 'project_id' AND payload->>'project_id' = project_id),
            CHECK (payload ? 'workspace_id' AND payload->>'workspace_id' = workspace_id),
            CHECK (
                payload ? 'work_order_id'
                AND (payload->>'work_order_id') IS NOT DISTINCT FROM work_order_id
            ),
            CHECK (payload ? 'status' AND payload->>'status' = status),
            CHECK (
                payload ? 'source_agent_id'
                AND (payload->>'source_agent_id') IS NOT DISTINCT FROM source_agent_id
            )
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_approval_requests_workspace_status "
        "ON approval_requests(workspace_id, status, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_approval_requests_source_agent "
        "ON approval_requests(source_agent_id, approval_id)",
        f"""
        CREATE TABLE IF NOT EXISTS agent_tasks (
            task_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
            external_agent_id TEXT,
            approval_request_id TEXT REFERENCES approval_requests(approval_id) ON DELETE RESTRICT,
            status TEXT NOT NULL CHECK (status IN ({task_statuses})),
            updated_at TIMESTAMPTZ NOT NULL,
            payload JSONB NOT NULL,
            FOREIGN KEY (project_id, workspace_id)
                REFERENCES projects(project_id, workspace_id) ON DELETE RESTRICT,
            FOREIGN KEY (external_agent_id, workspace_id)
                REFERENCES external_agents(agent_id, workspace_id) ON DELETE RESTRICT,
            CHECK (payload ? 'id' AND payload->>'id' = task_id),
            CHECK (payload ? 'project_id' AND payload->>'project_id' = project_id),
            CHECK (payload ? 'workspace_id' AND payload->>'workspace_id' = workspace_id),
            CHECK (
                payload ? 'external_agent_id'
                AND (payload->>'external_agent_id') IS NOT DISTINCT FROM external_agent_id
            ),
            CHECK (payload ? 'status' AND payload->>'status' = status),
            CHECK (
                payload ? 'approval_request_id'
                AND (payload->>'approval_request_id') IS NOT DISTINCT FROM approval_request_id
            )
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_agent_tasks_approval "
        "ON agent_tasks(approval_request_id, task_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_tasks_one_per_approval "
        "ON agent_tasks(approval_request_id) WHERE approval_request_id IS NOT NULL",
        """
        CREATE TABLE IF NOT EXISTS audit_events (
            event_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
            approval_id TEXT REFERENCES approval_requests(approval_id) ON DELETE RESTRICT,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            actor TEXT NOT NULL,
            details JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            payload JSONB NOT NULL,
            CHECK (payload ? 'id' AND payload->>'id' = event_id),
            CHECK (payload ? 'entity_id' AND payload->>'entity_id' = entity_id),
            CHECK (payload ? 'workspace_id' AND payload->>'workspace_id' = workspace_id),
            CHECK (payload ? 'event_type' AND payload->>'event_type' = event_type),
            CHECK (
                (entity_type = 'approval_request' AND approval_id IS NOT NULL AND approval_id = entity_id)
                OR (entity_type <> 'approval_request' AND approval_id IS NULL)
            )
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_audit_events_workspace_time "
        "ON audit_events(workspace_id, created_at, event_id)",
        "CREATE INDEX IF NOT EXISTS idx_audit_events_entity "
        "ON audit_events(entity_type, entity_id, created_at)",
        """
        CREATE OR REPLACE FUNCTION prevent_audit_event_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit records are immutable';
        END;
        $$ LANGUAGE plpgsql
        """,
        "DROP TRIGGER IF EXISTS audit_event_immutable ON audit_events",
        """
        CREATE TRIGGER audit_event_immutable
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION prevent_audit_event_mutation()
        """,
        """
        CREATE TABLE IF NOT EXISTS notifications (
            notification_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
            user_id TEXT,
            approval_id TEXT NOT NULL REFERENCES approval_requests(approval_id) ON DELETE RESTRICT,
            entity_type TEXT NOT NULL CHECK (entity_type = 'approval_request'),
            status TEXT NOT NULL CHECK (status IN (
                'PENDING', 'RETRY_WAIT', 'SENDING', 'SENT', 'FAILED', 'SKIPPED', 'NO_DEVICE'
            )),
            attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            next_attempt_at TIMESTAMPTZ,
            lease_expires_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL,
            payload JSONB NOT NULL,
            FOREIGN KEY (user_id, workspace_id)
                REFERENCES users(user_id, workspace_id) ON DELETE RESTRICT,
            CHECK (payload ? 'id' AND payload->>'id' = notification_id),
            CHECK (payload ? 'entity_id' AND payload->>'entity_id' = approval_id),
            CHECK (payload ? 'workspace_id' AND payload->>'workspace_id' = workspace_id),
            CHECK (
                payload ? 'user_id'
                AND (payload->>'user_id') IS NOT DISTINCT FROM user_id
            ),
            CHECK (payload ? 'entity_type' AND payload->>'entity_type' = entity_type),
            CHECK (payload ? 'push_status' AND payload->>'push_status' = status)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_notifications_pending "
        "ON notifications(status, next_attempt_at, lease_expires_at) "
        "WHERE status IN ('PENDING', 'SENDING')",
        """
        CREATE TABLE IF NOT EXISTS sqlite_import_runs (
            source_sha256 TEXT PRIMARY KEY CHECK (length(source_sha256) = 64),
            scope TEXT NOT NULL CHECK (scope = 'approval_transaction_slice_v1'),
            source_counts JSONB NOT NULL,
            imported_counts JSONB NOT NULL,
            imported_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
        """
        CREATE OR REPLACE FUNCTION prevent_sqlite_import_run_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'import manifest records are immutable';
        END;
        $$ LANGUAGE plpgsql
        """,
        "DROP TRIGGER IF EXISTS sqlite_import_run_immutable ON sqlite_import_runs",
        """
        CREATE TRIGGER sqlite_import_run_immutable
        BEFORE UPDATE OR DELETE ON sqlite_import_runs
        FOR EACH ROW EXECUTE FUNCTION prevent_sqlite_import_run_mutation()
        """,
    )


POSTGRES_MIGRATIONS = (
    PostgresMigration(
        migration_id="pg_0001_approval_transaction",
        name="Create normalized approval decision transaction tables",
        statements=_approval_transaction_schema(),
    ),
    PostgresMigration(
        migration_id="pg_0002_agent_task_events",
        name="Add workspace-scoped AgentTask event timeline",
        statements=(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_tasks_task_workspace "
            "ON agent_tasks(task_id, workspace_id)",
            """
            CREATE TABLE IF NOT EXISTS agent_task_events (
                event_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                actor TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                payload JSONB NOT NULL,
                FOREIGN KEY (task_id, workspace_id)
                    REFERENCES agent_tasks(task_id, workspace_id) ON DELETE RESTRICT,
                CHECK (payload ? 'id' AND payload->>'id' = event_id),
                CHECK (payload ? 'task_id' AND payload->>'task_id' = task_id),
                CHECK (payload ? 'event_type' AND payload->>'event_type' = event_type)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_agent_task_events_timeline "
            "ON agent_task_events(workspace_id, task_id, created_at, event_id)",
            "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS task_id TEXT",
            "ALTER TABLE notifications ALTER COLUMN approval_id DROP NOT NULL",
            "ALTER TABLE notifications DROP CONSTRAINT IF EXISTS notifications_approval_id_fkey",
            "ALTER TABLE notifications DROP CONSTRAINT IF EXISTS notifications_entity_type_check",
            """
            DO $$
            DECLARE check_name TEXT;
            BEGIN
                FOR check_name IN
                    SELECT conname FROM pg_constraint
                    WHERE conrelid = 'notifications'::regclass
                      AND contype = 'c'
                      AND (
                          pg_get_constraintdef(oid) LIKE '%entity_id%'
                          OR pg_get_constraintdef(oid) LIKE '%entity_type%'
                      )
                LOOP
                    EXECUTE format('ALTER TABLE notifications DROP CONSTRAINT %I', check_name);
                END LOOP;
            END $$
            """,
            "ALTER TABLE notifications ADD CONSTRAINT notifications_approval_fk "
            "FOREIGN KEY (approval_id) REFERENCES approval_requests(approval_id) ON DELETE RESTRICT",
            "ALTER TABLE notifications ADD CONSTRAINT notifications_task_scope_fk "
            "FOREIGN KEY (task_id, workspace_id) REFERENCES agent_tasks(task_id, workspace_id) ON DELETE RESTRICT",
            "ALTER TABLE notifications ADD CONSTRAINT notifications_entity_target_check CHECK ("
            "(entity_type='approval_request' AND approval_id IS NOT NULL AND task_id IS NULL) OR "
            "(entity_type='agent_task' AND task_id IS NOT NULL AND approval_id IS NULL))",
            "ALTER TABLE notifications ADD CONSTRAINT notifications_payload_entity_id_check "
            "CHECK (payload ? 'entity_id' AND payload->>'entity_id' = COALESCE(approval_id, task_id))",
            "ALTER TABLE notifications ADD CONSTRAINT notifications_payload_entity_type_check "
            "CHECK (payload ? 'entity_type' AND payload->>'entity_type' = entity_type)",
        ),
    ),
    PostgresMigration(
        migration_id="pg_0003_notification_outbox_claim_indexes",
        name="Index pending retries and expired notification leases",
        statements=(
            "CREATE INDEX IF NOT EXISTS idx_notifications_outbox_due "
            "ON notifications(next_attempt_at, created_at, notification_id) "
            "WHERE status IN ('PENDING', 'RETRY_WAIT')",
            "CREATE INDEX IF NOT EXISTS idx_notifications_outbox_expired_lease "
            "ON notifications(lease_expires_at, created_at, notification_id) "
            "WHERE status = 'SENDING'",
        ),
    ),
    PostgresMigration(
        migration_id="pg_0004_auth_onboarding",
        name="Add workspace invites and encrypted verification email outbox",
        statements=(
            "ALTER TABLE workspaces ADD CONSTRAINT workspaces_owner_user_payload_check "
            "CHECK (payload ? 'owner_user_id' AND length(payload->>'owner_user_id') > 0)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_workspaces_owner_user "
            "ON workspaces((payload->>'owner_user_id'))",
            """
            CREATE TABLE IF NOT EXISTS invites (
                invite_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
                email TEXT NOT NULL,
                normalized_email TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('OWNER','MAINTAINER','REVIEWER')),
                token_hash TEXT NOT NULL UNIQUE CHECK (token_hash ~ '^[0-9a-f]{64}$'),
                status TEXT NOT NULL CHECK (status IN ('PENDING','ACCEPTED','EXPIRED','REVOKED')),
                expires_at TIMESTAMPTZ NOT NULL,
                created_by_user_id TEXT NOT NULL,
                accepted_by_user_id TEXT,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL,
                payload JSONB NOT NULL,
                FOREIGN KEY (created_by_user_id, workspace_id)
                    REFERENCES users(user_id, workspace_id) ON DELETE RESTRICT,
                FOREIGN KEY (accepted_by_user_id, workspace_id)
                    REFERENCES users(user_id, workspace_id) ON DELETE RESTRICT,
                CHECK (normalized_email <> '' AND lower(btrim(email)) = normalized_email),
                CHECK ((status = 'ACCEPTED') = (accepted_by_user_id IS NOT NULL)),
                CHECK (payload ? 'id' AND payload->>'id' = invite_id),
                CHECK (payload ? 'workspace_id' AND payload->>'workspace_id' = workspace_id),
                CHECK (payload ? 'email' AND lower(btrim(payload->>'email')) = normalized_email),
                CHECK (payload ? 'role' AND payload->>'role' = role),
                CHECK (payload ? 'token_hash' AND payload->>'token_hash' = token_hash),
                CHECK (payload ? 'status' AND payload->>'status' = status),
                CHECK (
                    (payload->>'accepted_by_user_id') IS NOT DISTINCT FROM accepted_by_user_id
                )
            )
            """,
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_invites_pending_workspace_email "
            "ON invites(workspace_id, normalized_email) WHERE status='PENDING'",
            "CREATE INDEX IF NOT EXISTS idx_invites_workspace_created "
            "ON invites(workspace_id, created_at DESC, invite_id)",
            """
            CREATE TABLE IF NOT EXISTS auth_email_outbox (
                outbox_id TEXT PRIMARY KEY,
                recipient TEXT NOT NULL,
                encrypted_code BYTEA NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('PENDING','SENDING','SENT','DEAD')),
                attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                created_at TIMESTAMPTZ NOT NULL,
                next_attempt_at TIMESTAMPTZ NOT NULL,
                lease_expires_at TIMESTAMPTZ,
                sent_at TIMESTAMPTZ,
                last_error TEXT,
                CHECK (
                    (status IN ('PENDING','SENDING') AND octet_length(encrypted_code) >= 29)
                    OR (status IN ('SENT','DEAD') AND octet_length(encrypted_code) = 0)
                )
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_pg_auth_email_outbox_due "
            "ON auth_email_outbox(status,next_attempt_at,created_at,outbox_id) "
            "WHERE status IN ('PENDING','SENDING')",
            """
            CREATE TABLE IF NOT EXISTS auth_rate_limit_buckets (
                bucket_hash TEXT NOT NULL CHECK (bucket_hash ~ '^[0-9a-f]{64}$'),
                window_start BIGINT NOT NULL CHECK (window_start >= 0),
                request_count INTEGER NOT NULL CHECK (request_count > 0),
                PRIMARY KEY (bucket_hash, window_start)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_pg_auth_rate_limit_window_start "
            "ON auth_rate_limit_buckets(window_start)",
        ),
    ),
    PostgresMigration(
        migration_id="pg_0005_agent_run_state",
        name="Create workspace-scoped agent run state tables",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS agent_runs (
                run_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
                approval_id TEXT,
                status TEXT NOT NULL CHECK (status IN (
                    'SCHEDULED','RUNNING','EVALUATING','RETRY_WAIT','PAUSED',
                    'NEEDS_HUMAN','BLOCKED','SUCCEEDED','FAILED','CANCELLED'
                )),
                current_step_id TEXT,
                lease_owner TEXT,
                lease_token TEXT,
                lease_expires_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL,
                payload JSONB NOT NULL,
                UNIQUE (run_id, workspace_id),
                FOREIGN KEY (task_id, workspace_id)
                    REFERENCES agent_tasks(task_id, workspace_id) ON DELETE RESTRICT,
                FOREIGN KEY (project_id, workspace_id)
                    REFERENCES projects(project_id, workspace_id) ON DELETE RESTRICT,
                FOREIGN KEY (approval_id, workspace_id)
                    REFERENCES approval_requests(approval_id, workspace_id) ON DELETE RESTRICT,
                CHECK (payload ? 'id' AND payload->>'id' = run_id),
                CHECK (payload ? 'task_id' AND payload->>'task_id' = task_id),
                CHECK (payload ? 'project_id' AND payload->>'project_id' = project_id),
                CHECK (payload ? 'workspace_id' AND payload->>'workspace_id' = workspace_id),
                CHECK (payload ? 'approval_request_id' AND
                    (payload->>'approval_request_id') IS NOT DISTINCT FROM approval_id),
                CHECK (payload ? 'status' AND payload->>'status' = status)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_agent_runs_workspace_status "
            "ON agent_runs(workspace_id, status, updated_at, run_id)",
            "CREATE INDEX IF NOT EXISTS idx_agent_runs_task ON agent_runs(task_id, run_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_runs_one_active_per_task "
            "ON agent_runs(task_id) WHERE status NOT IN ('FAILED','CANCELLED','BLOCKED')",
            """
            CREATE TABLE IF NOT EXISTS agent_run_steps (
                step_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence >= 0),
                status TEXT NOT NULL CHECK (status IN (
                    'PENDING','READY','RUNNING','RETRY_WAIT','PAUSED','NEEDS_HUMAN',
                    'BLOCKED','SUCCEEDED','FAILED','CANCELLED','SKIPPED'
                )),
                updated_at TIMESTAMPTZ NOT NULL,
                payload JSONB NOT NULL,
                UNIQUE (run_id, sequence),
                FOREIGN KEY (run_id, workspace_id)
                    REFERENCES agent_runs(run_id, workspace_id) ON DELETE RESTRICT,
                FOREIGN KEY (task_id, workspace_id)
                    REFERENCES agent_tasks(task_id, workspace_id) ON DELETE RESTRICT,
                CHECK (payload ? 'id' AND payload->>'id' = step_id),
                CHECK (payload ? 'run_id' AND payload->>'run_id' = run_id),
                CHECK (payload ? 'task_id' AND payload->>'task_id' = task_id),
                CHECK (payload->>'status' = status),
                CHECK ((payload->>'sequence')::integer = sequence)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_agent_run_steps_run_status "
            "ON agent_run_steps(run_id, status, sequence)",
            """
            CREATE TABLE IF NOT EXISTS agent_run_events (
                event_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                event_type TEXT NOT NULL,
                payload JSONB NOT NULL,
                FOREIGN KEY (run_id, workspace_id)
                    REFERENCES agent_runs(run_id, workspace_id) ON DELETE RESTRICT,
                FOREIGN KEY (task_id, workspace_id)
                    REFERENCES agent_tasks(task_id, workspace_id) ON DELETE RESTRICT,
                CHECK (payload ? 'id' AND payload->>'id' = event_id),
                CHECK (payload ? 'run_id' AND payload->>'run_id' = run_id),
                CHECK (payload ? 'task_id' AND payload->>'task_id' = task_id),
                CHECK (payload ? 'event_type' AND payload->>'event_type' = event_type)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_agent_run_events_run_time "
            "ON agent_run_events(run_id, created_at, event_id)",
        ),
    ),
    PostgresMigration(
        migration_id="pg_0006_approval_invocation_identity",
        name="Enforce unique connector approval invocation identity",
        statements=(
            "ALTER TABLE approval_requests ADD COLUMN IF NOT EXISTS source_invocation_id TEXT",
            "UPDATE approval_requests SET source_invocation_id=payload->>'source_invocation_id' "
            "WHERE payload ? 'source_invocation_id'",
            "ALTER TABLE approval_requests ADD CONSTRAINT approval_invocation_payload_check "
            "CHECK (source_invocation_id IS NOT DISTINCT FROM (payload->>'source_invocation_id')) "
            "NOT VALID",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_invocation_identity "
            "ON approval_requests(workspace_id,source_agent_id,source_invocation_id) "
            "WHERE source_invocation_id IS NOT NULL",
        ),
    ),
    PostgresMigration(
        migration_id="pg_0007_approval_delivery_claims",
        name="Record at-most-once approved invocation delivery",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS approval_delivery_claims (
                approval_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                source_agent_id TEXT NOT NULL,
                source_invocation_id TEXT NOT NULL,
                claimed_at TIMESTAMPTZ NOT NULL,
                FOREIGN KEY (approval_id, workspace_id)
                    REFERENCES approval_requests(approval_id, workspace_id) ON DELETE RESTRICT,
                FOREIGN KEY (source_agent_id, workspace_id)
                    REFERENCES external_agents(agent_id, workspace_id) ON DELETE RESTRICT,
                UNIQUE (workspace_id, source_agent_id, source_invocation_id),
                CHECK (length(approval_id) BETWEEN 1 AND 256),
                CHECK (length(source_agent_id) BETWEEN 1 AND 256),
                CHECK (length(source_invocation_id) BETWEEN 1 AND 256)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_approval_delivery_claim_workspace "
            "ON approval_delivery_claims(workspace_id, claimed_at)",
        ),
    ),
    PostgresMigration(
        migration_id="pg_0008_approval_delivery_failures",
        name="Record connector-reported ambiguous approved delivery failures",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS approval_delivery_failures (
                approval_id TEXT PRIMARY KEY REFERENCES approval_delivery_claims(approval_id) ON DELETE RESTRICT,
                workspace_id TEXT NOT NULL,
                source_agent_id TEXT NOT NULL,
                source_invocation_id TEXT NOT NULL,
                reason_code TEXT NOT NULL CHECK (reason_code = 'CLAIM_RESPONSE_AMBIGUOUS'),
                reported_at TIMESTAMPTZ NOT NULL,
                CHECK (length(approval_id) BETWEEN 1 AND 256),
                CHECK (length(workspace_id) BETWEEN 1 AND 256),
                CHECK (length(source_agent_id) BETWEEN 1 AND 256),
                CHECK (length(source_invocation_id) BETWEEN 1 AND 256)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_approval_delivery_failures_workspace "
            "ON approval_delivery_failures(workspace_id, reported_at)",
        ),
    ),
    PostgresMigration(
        migration_id="pg_0009_delivery_failure_claim_identity",
        name="Bind delivery failure reports to the complete durable claim identity",
        statements=(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_delivery_claims_full_identity "
            "ON approval_delivery_claims(approval_id,workspace_id,source_agent_id,source_invocation_id)",
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'approval_delivery_failure_claim_identity_fk'
                      AND conrelid = 'approval_delivery_failures'::regclass
                ) THEN
                    ALTER TABLE approval_delivery_failures
                    ADD CONSTRAINT approval_delivery_failure_claim_identity_fk
                    FOREIGN KEY (approval_id,workspace_id,source_agent_id,source_invocation_id)
                    REFERENCES approval_delivery_claims
                        (approval_id,workspace_id,source_agent_id,source_invocation_id)
                    ON DELETE RESTRICT NOT VALID;
                END IF;
            END $$
            """,
            "ALTER TABLE approval_delivery_failures "
            "VALIDATE CONSTRAINT approval_delivery_failure_claim_identity_fk",
        ),
    ),
    PostgresMigration(
        migration_id="pg_0010_workspace_scoped_connectors",
        name="Add workspace-scoped connector persistence",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS agent_connectors (
                connector_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
                created_by_user_id TEXT NOT NULL,
                provider_label TEXT NOT NULL,
                auth_type TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('ENABLED','DISABLED')),
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL,
                payload JSONB NOT NULL,
                UNIQUE (connector_id,workspace_id),
                FOREIGN KEY (created_by_user_id,workspace_id)
                    REFERENCES users(user_id,workspace_id) ON DELETE RESTRICT,
                CHECK (payload ? 'id' AND payload->>'id'=connector_id),
                CHECK (payload ? 'workspace_id' AND payload->>'workspace_id'=workspace_id),
                CHECK (payload ? 'created_by_user_id' AND
                    payload->>'created_by_user_id'=created_by_user_id),
                CHECK (payload ? 'created_at' AND
                    (payload->>'created_at')::timestamptz=created_at),
                CHECK (payload ? 'provider_label' AND payload->>'provider_label'=provider_label),
                CHECK (payload ? 'auth_type' AND payload->>'auth_type'=auth_type),
                CHECK (payload ? 'status' AND payload->>'status'=status),
                CHECK (payload ? 'updated_at' AND
                    (payload->>'updated_at')::timestamptz=updated_at)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_agent_connectors_workspace "
            "ON agent_connectors(workspace_id,created_at,connector_id)",
            "CREATE INDEX IF NOT EXISTS idx_agent_connectors_enabled "
            "ON agent_connectors(workspace_id,status,connector_id)",
        ),
    ),
)

POSTGRES_MIGRATIONS = (
    *POSTGRES_MIGRATIONS,
    PostgresMigration(
        migration_id="pg_0011_workspace_scoped_mcp_child_servers",
        name="Add workspace-scoped MCP child-server registry persistence",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS mcp_child_servers (
                server_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
                name TEXT NOT NULL,
                enabled BOOLEAN NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL,
                payload JSONB NOT NULL,
                UNIQUE (server_id, workspace_id),
                UNIQUE (project_id, name),
                FOREIGN KEY (project_id, workspace_id)
                    REFERENCES projects(project_id, workspace_id) ON DELETE RESTRICT,
                CHECK (payload ? 'id' AND payload->>'id' = server_id),
                CHECK (payload ? 'project_id' AND payload->>'project_id' = project_id),
                CHECK (payload ? 'workspace_id' AND payload->>'workspace_id' = workspace_id),
                CHECK (payload ? 'name' AND payload->>'name' = name),
                CHECK (payload ? 'enabled' AND (payload->>'enabled')::boolean = enabled),
                CHECK (payload ? 'created_at' AND (payload->>'created_at')::timestamptz = created_at),
                CHECK (payload ? 'updated_at' AND (payload->>'updated_at')::timestamptz = updated_at)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_mcp_child_servers_workspace_project "
            "ON mcp_child_servers(workspace_id,project_id,created_at)",
            "CREATE INDEX IF NOT EXISTS idx_mcp_child_servers_enabled "
            "ON mcp_child_servers(workspace_id,enabled,project_id)",
        ),
    ),
    PostgresMigration(
        migration_id="pg_0012_workspace_scoped_mcp_pending_requests",
        name="Add transactional workspace-scoped MCP approval forwarding state",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS mcp_pending_requests (
                pending_id TEXT PRIMARY KEY,
                approval_id TEXT NOT NULL UNIQUE,
                project_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN (
                    'WAITING_FOR_APPROVAL','FORWARDING','FORWARDED',
                    'BLOCKED_BY_DECISION','ORPHANED','DEAD_LETTER'
                )),
                attempts INTEGER NOT NULL CHECK (attempts >= 0),
                lease_owner TEXT,
                lease_expires_at TIMESTAMPTZ,
                last_error TEXT,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL,
                payload JSONB NOT NULL,
                FOREIGN KEY (approval_id,workspace_id)
                    REFERENCES approval_requests(approval_id,workspace_id) ON DELETE RESTRICT,
                FOREIGN KEY (project_id,workspace_id)
                    REFERENCES projects(project_id,workspace_id) ON DELETE RESTRICT,
                CHECK (payload ? 'id' AND payload->>'id'=pending_id),
                CHECK (payload ? 'approval_request_id' AND payload->>'approval_request_id'=approval_id),
                CHECK (payload ? 'project_id' AND payload->>'project_id'=project_id),
                CHECK (payload ? 'workspace_id' AND payload->>'workspace_id'=workspace_id),
                CHECK (payload ? 'status' AND payload->>'status'=status),
                CHECK ((payload->>'attempts')::integer=attempts),
                CHECK ((payload->>'lease_owner') IS NOT DISTINCT FROM lease_owner),
                CHECK ((payload->>'lease_expires_at')::timestamptz IS NOT DISTINCT FROM lease_expires_at),
                CHECK ((payload->>'last_error') IS NOT DISTINCT FROM last_error),
                CHECK ((lease_owner IS NULL)=(lease_expires_at IS NULL)),
                CHECK ((status='FORWARDING')=(lease_owner IS NOT NULL))
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_mcp_pending_workspace_status "
            "ON mcp_pending_requests(workspace_id,status,created_at,pending_id)",
            "CREATE INDEX IF NOT EXISTS idx_mcp_pending_expired_lease "
            "ON mcp_pending_requests(lease_expires_at,pending_id) WHERE status='FORWARDING'",
        ),
    ),
    PostgresMigration(
        migration_id="pg_0013_workspace_scoped_test_runs",
        name="Add workspace-scoped test evidence records",
        statements=(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_project_workspace "
            "ON approval_requests(approval_id,project_id,workspace_id)",
            """
            CREATE TABLE IF NOT EXISTS test_runs (
                test_run_id TEXT PRIMARY KEY,
                approval_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                payload JSONB NOT NULL,
                FOREIGN KEY (approval_id,project_id,workspace_id)
                    REFERENCES approval_requests(approval_id,project_id,workspace_id) ON DELETE RESTRICT,
                CHECK (payload ? 'id' AND payload->>'id'=test_run_id),
                CHECK (payload ? 'approval_request_id' AND payload->>'approval_request_id'=approval_id),
                CHECK (payload ? 'created_at' AND (payload->>'created_at')::timestamptz=created_at)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_test_runs_workspace_created "
            "ON test_runs(workspace_id,created_at DESC,test_run_id DESC)",
            "CREATE INDEX IF NOT EXISTS idx_test_runs_approval "
            "ON test_runs(approval_id,created_at DESC,test_run_id DESC)",
        ),
    ),
    PostgresMigration(
        migration_id="pg_0014_workspace_scoped_trust_events",
        name="Add transactionally sequenced PostgreSQL trust event ledger",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS trust_event_heads (
                singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
                sequence BIGINT NOT NULL CHECK (sequence >= 0),
                event_hash TEXT
            )
            """,
            "INSERT INTO trust_event_heads(singleton,sequence,event_hash) "
            "VALUES (TRUE,0,NULL) ON CONFLICT (singleton) DO NOTHING",
            """
            CREATE TABLE IF NOT EXISTS trust_events (
                event_id TEXT PRIMARY KEY,
                sequence BIGINT NOT NULL UNIQUE CHECK (sequence > 0),
                workspace_id TEXT REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
                event_type TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                actor TEXT NOT NULL,
                correlation_id TEXT,
                previous_hash TEXT,
                event_hash TEXT NOT NULL UNIQUE,
                created_at TIMESTAMPTZ NOT NULL,
                payload JSONB NOT NULL,
                CHECK (payload ? 'id' AND payload->>'id'=event_id),
                CHECK ((payload->>'sequence')::bigint=sequence),
                CHECK ((payload->>'workspace_id') IS NOT DISTINCT FROM workspace_id),
                CHECK (payload->>'event_type'=event_type),
                CHECK (payload->>'entity_type'=entity_type),
                CHECK (payload->>'entity_id'=entity_id),
                CHECK (payload->>'actor'=actor),
                CHECK ((payload->>'correlation_id') IS NOT DISTINCT FROM correlation_id),
                CHECK ((payload->>'previous_hash') IS NOT DISTINCT FROM previous_hash),
                CHECK (payload->>'event_hash'=event_hash),
                CHECK ((payload->>'created_at')::timestamptz=created_at)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_trust_events_workspace_sequence "
            "ON trust_events(workspace_id,sequence)",
            """
            CREATE FUNCTION reject_trust_event_mutation() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'trust events are immutable';
            END;
            $$
            """,
            """
            CREATE TRIGGER trust_events_reject_mutation
            BEFORE UPDATE OR DELETE ON trust_events
            FOR EACH ROW EXECUTE FUNCTION reject_trust_event_mutation()
            """,
        ),
    ),
    PostgresMigration(
        migration_id="pg_0015_workspace_scoped_trust_records",
        name="Add workspace-scoped PostgreSQL trust and action records",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS capability_leases (
                lease_id TEXT PRIMARY KEY,
                approval_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
                issued_by_user_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('ACTIVE','EXPIRED','REVOKED','CONSUMED')),
                expires_at TIMESTAMPTZ NOT NULL,
                payload JSONB NOT NULL,
                UNIQUE (lease_id,workspace_id),
                FOREIGN KEY (approval_id,workspace_id)
                    REFERENCES approval_requests(approval_id,workspace_id) ON DELETE RESTRICT,
                FOREIGN KEY (issued_by_user_id,workspace_id)
                    REFERENCES users(user_id,workspace_id) ON DELETE RESTRICT,
                CHECK (payload ? 'id' AND payload->>'id'=lease_id),
                CHECK (payload ? 'approval_request_id' AND payload->>'approval_request_id'=approval_id),
                CHECK (payload ? 'workspace_id' AND payload->>'workspace_id'=workspace_id),
                CHECK (payload ? 'issued_by_user_id' AND payload->>'issued_by_user_id'=issued_by_user_id),
                CHECK (payload ? 'status' AND payload->>'status'=status),
                CHECK ((payload->>'expires_at')::timestamptz=expires_at)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_capability_leases_workspace_status "
            "ON capability_leases(workspace_id,status,expires_at,lease_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_capability_leases_one_active_per_approval "
            "ON capability_leases(workspace_id,approval_id) WHERE status='ACTIVE'",
            """
            CREATE TABLE IF NOT EXISTS reviewer_attestations (
                attestation_id TEXT PRIMARY KEY,
                approval_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
                reviewer_user_id TEXT NOT NULL,
                approval_payload_hash TEXT NOT NULL,
                decision TEXT NOT NULL CHECK (decision IN ('APPROVE','DENY')),
                created_at TIMESTAMPTZ NOT NULL,
                payload JSONB NOT NULL,
                UNIQUE (approval_id,workspace_id,reviewer_user_id,approval_payload_hash),
                FOREIGN KEY (approval_id,workspace_id)
                    REFERENCES approval_requests(approval_id,workspace_id) ON DELETE RESTRICT,
                FOREIGN KEY (reviewer_user_id,workspace_id)
                    REFERENCES users(user_id,workspace_id) ON DELETE RESTRICT,
                CHECK (payload ? 'id' AND payload->>'id'=attestation_id),
                CHECK (payload ? 'approval_request_id' AND payload->>'approval_request_id'=approval_id),
                CHECK (payload ? 'workspace_id' AND payload->>'workspace_id'=workspace_id),
                CHECK (payload ? 'reviewer_user_id' AND payload->>'reviewer_user_id'=reviewer_user_id),
                CHECK (payload ? 'approval_payload_hash' AND payload->>'approval_payload_hash'=approval_payload_hash),
                CHECK (payload ? 'decision' AND payload->>'decision'=decision),
                CHECK ((payload->>'created_at')::timestamptz=created_at)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_reviewer_attestations_workspace_approval "
            "ON reviewer_attestations(workspace_id,approval_id,created_at)",
            """
            CREATE TABLE IF NOT EXISTS proposed_actions (
                action_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
                lease_id TEXT,
                project_id TEXT,
                task_id TEXT,
                run_id TEXT,
                created_at TIMESTAMPTZ NOT NULL,
                payload JSONB NOT NULL,
                UNIQUE (action_id,workspace_id),
                UNIQUE (action_id,lease_id,workspace_id),
                FOREIGN KEY (lease_id,workspace_id)
                    REFERENCES capability_leases(lease_id,workspace_id) ON DELETE RESTRICT,
                FOREIGN KEY (project_id,workspace_id)
                    REFERENCES projects(project_id,workspace_id) ON DELETE RESTRICT,
                FOREIGN KEY (task_id,workspace_id)
                    REFERENCES agent_tasks(task_id,workspace_id) ON DELETE RESTRICT,
                FOREIGN KEY (run_id,workspace_id)
                    REFERENCES agent_runs(run_id,workspace_id) ON DELETE RESTRICT,
                CHECK (payload ? 'id' AND payload->>'id'=action_id),
                CHECK (payload ? 'workspace_id' AND payload->>'workspace_id'=workspace_id),
                CHECK ((payload->>'lease_id') IS NOT DISTINCT FROM lease_id),
                CHECK ((payload->>'project_id') IS NOT DISTINCT FROM project_id),
                CHECK ((payload->>'task_id') IS NOT DISTINCT FROM task_id),
                CHECK ((payload->>'run_id') IS NOT DISTINCT FROM run_id),
                CHECK ((payload->>'created_at')::timestamptz=created_at)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_proposed_actions_workspace_created "
            "ON proposed_actions(workspace_id,created_at DESC,action_id)",
            "CREATE INDEX IF NOT EXISTS idx_proposed_actions_lease "
            "ON proposed_actions(workspace_id,lease_id,created_at)",
            """
            CREATE TABLE IF NOT EXISTS policy_decisions (
                decision_id TEXT PRIMARY KEY,
                action_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
                lease_id TEXT,
                decision TEXT NOT NULL CHECK (decision IN ('ALLOW','REQUIRE_APPROVAL','BLOCK')),
                evaluated_at TIMESTAMPTZ NOT NULL,
                payload JSONB NOT NULL,
                UNIQUE (decision_id,action_id,workspace_id),
                FOREIGN KEY (action_id,workspace_id)
                    REFERENCES proposed_actions(action_id,workspace_id) ON DELETE RESTRICT,
                FOREIGN KEY (action_id,lease_id,workspace_id)
                    REFERENCES proposed_actions(action_id,lease_id,workspace_id) ON DELETE RESTRICT,
                CHECK (payload ? 'id' AND payload->>'id'=decision_id),
                CHECK (payload ? 'proposed_action_id' AND payload->>'proposed_action_id'=action_id),
                CHECK ((payload->>'lease_id') IS NOT DISTINCT FROM lease_id),
                CHECK (payload ? 'decision' AND payload->>'decision'=decision),
                CHECK ((payload->>'evaluated_at')::timestamptz=evaluated_at)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_policy_decisions_action_latest "
            "ON policy_decisions(workspace_id,action_id,evaluated_at DESC,decision_id DESC)",
            """
            CREATE TABLE IF NOT EXISTS action_authorizations (
                authorization_id TEXT PRIMARY KEY,
                action_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
                policy_decision_id TEXT NOT NULL,
                reviewer_user_id TEXT NOT NULL,
                decision TEXT NOT NULL CHECK (decision IN ('ALLOW','BLOCK')),
                action_payload_hash TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                payload JSONB NOT NULL,
                UNIQUE (action_id,workspace_id),
                FOREIGN KEY (action_id,workspace_id)
                    REFERENCES proposed_actions(action_id,workspace_id) ON DELETE RESTRICT,
                FOREIGN KEY (policy_decision_id,action_id,workspace_id)
                    REFERENCES policy_decisions(decision_id,action_id,workspace_id) ON DELETE RESTRICT,
                FOREIGN KEY (reviewer_user_id,workspace_id)
                    REFERENCES users(user_id,workspace_id) ON DELETE RESTRICT,
                CHECK (payload ? 'id' AND payload->>'id'=authorization_id),
                CHECK (payload ? 'action_id' AND payload->>'action_id'=action_id),
                CHECK (payload ? 'workspace_id' AND payload->>'workspace_id'=workspace_id),
                CHECK (payload ? 'policy_decision_id' AND payload->>'policy_decision_id'=policy_decision_id),
                CHECK (payload ? 'reviewer_user_id' AND payload->>'reviewer_user_id'=reviewer_user_id),
                CHECK (payload ? 'decision' AND payload->>'decision'=decision),
                CHECK (payload ? 'action_payload_hash' AND payload->>'action_payload_hash'=action_payload_hash),
                CHECK ((payload->>'created_at')::timestamptz=created_at)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_action_authorizations_workspace_created "
            "ON action_authorizations(workspace_id,created_at DESC,authorization_id)",
            """
            CREATE TABLE IF NOT EXISTS action_consumptions (
                action_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
                lease_id TEXT NOT NULL,
                consumed_at TIMESTAMPTZ NOT NULL,
                payload JSONB NOT NULL,
                FOREIGN KEY (action_id,lease_id,workspace_id)
                    REFERENCES proposed_actions(action_id,lease_id,workspace_id) ON DELETE RESTRICT,
                CHECK (payload ? 'action_id' AND payload->>'action_id'=action_id),
                CHECK (payload ? 'workspace_id' AND payload->>'workspace_id'=workspace_id),
                CHECK (payload ? 'lease_id' AND payload->>'lease_id'=lease_id),
                CHECK ((payload->>'consumed_at')::timestamptz=consumed_at),
                CHECK (payload ? 'consumption' AND jsonb_typeof(payload->'consumption')='object')
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_action_consumptions_workspace_time "
            "ON action_consumptions(workspace_id,consumed_at DESC,action_id)",
            """
            CREATE TABLE IF NOT EXISTS credential_grants (
                grant_id TEXT PRIMARY KEY,
                lease_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
                grant_type TEXT NOT NULL CHECK (grant_type IN (
                    'GITHUB_APP_INSTALLATION','OIDC_FEDERATION','INTERNAL_CAPABILITY_TOKEN'
                )),
                audience TEXT NOT NULL,
                subject TEXT NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                revoked BOOLEAN NOT NULL,
                token_hash TEXT NOT NULL,
                payload JSONB NOT NULL,
                UNIQUE (grant_id,workspace_id),
                UNIQUE (token_hash),
                FOREIGN KEY (lease_id,workspace_id)
                    REFERENCES capability_leases(lease_id,workspace_id) ON DELETE RESTRICT,
                CHECK (payload ? 'id' AND payload->>'id'=grant_id),
                CHECK (payload ? 'lease_id' AND payload->>'lease_id'=lease_id),
                CHECK (payload ? 'workspace_id' AND payload->>'workspace_id'=workspace_id),
                CHECK (payload ? 'grant_type' AND payload->>'grant_type'=grant_type),
                CHECK (payload ? 'audience' AND payload->>'audience'=audience),
                CHECK (payload ? 'subject' AND payload->>'subject'=subject),
                CHECK ((payload->>'expires_at')::timestamptz=expires_at),
                CHECK ((payload->>'revoked')::boolean=revoked),
                CHECK (payload ? 'token_hash' AND payload->>'token_hash'=token_hash)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_credential_grants_workspace_active "
            "ON credential_grants(workspace_id,expires_at,grant_id) WHERE revoked=FALSE",
        ),
    ),
    PostgresMigration(
        migration_id="pg_0016_atomic_trust_consumption_budgets",
        name="Add durable capability lease consumption counters",
        statements=(
            "ALTER TABLE capability_leases ADD COLUMN IF NOT EXISTS consumed_pull_requests "
            "INTEGER NOT NULL DEFAULT 0 CHECK (consumed_pull_requests >= 0)",
            "ALTER TABLE capability_leases ADD COLUMN IF NOT EXISTS consumed_retries "
            "INTEGER NOT NULL DEFAULT 0 CHECK (consumed_retries >= 0)",
            "ALTER TABLE capability_leases ADD COLUMN IF NOT EXISTS consumed_runtime_seconds "
            "INTEGER NOT NULL DEFAULT 0 CHECK (consumed_runtime_seconds >= 0)",
            "ALTER TABLE capability_leases ADD COLUMN IF NOT EXISTS consumed_cost_usd "
            "NUMERIC NOT NULL DEFAULT 0 CHECK (consumed_cost_usd >= 0)",
            "ALTER TABLE capability_leases ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ",
            "ALTER TABLE capability_leases ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ",
            "UPDATE capability_leases SET "
            "consumed_pull_requests=COALESCE((payload->>'consumed_pull_requests')::integer,0),"
            "consumed_retries=COALESCE((payload->>'consumed_retries')::integer,0),"
            "consumed_runtime_seconds=COALESCE((payload->>'consumed_runtime_seconds')::integer,0),"
            "consumed_cost_usd=COALESCE((payload->>'consumed_cost_usd')::numeric,0),"
            "created_at=(payload->>'created_at')::timestamptz,"
            "updated_at=(payload->>'updated_at')::timestamptz",
            "ALTER TABLE capability_leases ALTER COLUMN created_at SET NOT NULL",
            "ALTER TABLE capability_leases ALTER COLUMN updated_at SET NOT NULL",
            "ALTER TABLE capability_leases ADD CONSTRAINT capability_leases_budget_payload_check "
            "CHECK ((payload->>'consumed_pull_requests')::integer=consumed_pull_requests AND "
            "(payload->>'consumed_retries')::integer=consumed_retries AND "
            "(payload->>'consumed_runtime_seconds')::integer=consumed_runtime_seconds AND "
            "(payload->>'consumed_cost_usd')::numeric=consumed_cost_usd AND "
            "(payload->>'created_at')::timestamptz=created_at AND "
            "(payload->>'updated_at')::timestamptz=updated_at) NOT VALID",
            "ALTER TABLE capability_leases VALIDATE CONSTRAINT capability_leases_budget_payload_check",
        ),
    ),
    PostgresMigration(
        migration_id="pg_0017_trust_import_manifest_scope",
        name="Allow immutable manifest for approval and trust slice imports",
        statements=(
            "ALTER TABLE sqlite_import_runs DROP CONSTRAINT IF EXISTS sqlite_import_runs_scope_check",
            "ALTER TABLE sqlite_import_runs ADD CONSTRAINT sqlite_import_runs_scope_check "
            "CHECK (scope IN ('approval_transaction_slice_v1','approval_and_trust_slice_v2'))",
        ),
    ),
    PostgresMigration(
        migration_id="pg_0018_agent_history_import_manifest_scope",
        name="Allow immutable manifest for approval, trust, and agent history imports",
        statements=(
            "ALTER TABLE sqlite_import_runs DROP CONSTRAINT IF EXISTS sqlite_import_runs_scope_check",
            "ALTER TABLE sqlite_import_runs ADD CONSTRAINT sqlite_import_runs_scope_check "
            "CHECK (scope IN ('approval_transaction_slice_v1','approval_and_trust_slice_v2',"
            "'approval_trust_agent_history_v3'))",
        ),
    ),
    PostgresMigration(
        migration_id="pg_0019_connector_import_manifest_scope",
        name="Allow immutable manifest for approval, trust, agent history, and connector imports",
        statements=(
            "ALTER TABLE sqlite_import_runs DROP CONSTRAINT IF EXISTS sqlite_import_runs_scope_check",
            "ALTER TABLE sqlite_import_runs ADD CONSTRAINT sqlite_import_runs_scope_check "
            "CHECK (scope IN ('approval_transaction_slice_v1','approval_and_trust_slice_v2',"
            "'approval_trust_agent_history_v3','approval_trust_agent_history_connectors_v4'))",
        ),
    ),
    PostgresMigration(
        migration_id="pg_0020_invite_import_manifest_scope",
        name="Allow immutable manifest for partial account and agent-state imports",
        statements=(
            "ALTER TABLE sqlite_import_runs DROP CONSTRAINT IF EXISTS sqlite_import_runs_scope_check",
            "ALTER TABLE sqlite_import_runs ADD CONSTRAINT sqlite_import_runs_scope_check "
            "CHECK (scope IN ('approval_transaction_slice_v1','approval_and_trust_slice_v2',"
            "'approval_trust_agent_history_v3','approval_trust_agent_history_connectors_v4',"
            "'approval_auth_agent_history_connectors_v5'))",
        ),
    ),
    PostgresMigration(
        migration_id="pg_0021_email_outbox_import_manifest_scope",
        name="Allow immutable manifest for partial email outbox migration",
        statements=(
            "ALTER TABLE sqlite_import_runs DROP CONSTRAINT IF EXISTS sqlite_import_runs_scope_check",
            "ALTER TABLE sqlite_import_runs ADD CONSTRAINT sqlite_import_runs_scope_check "
            "CHECK (scope IN ('approval_transaction_slice_v1','approval_and_trust_slice_v2',"
            "'approval_trust_agent_history_v3','approval_trust_agent_history_connectors_v4',"
            "'approval_auth_agent_history_connectors_v5','approval_auth_agent_history_connectors_email_v6'))",
        ),
    ),
    PostgresMigration(
        migration_id="pg_0022_mcp_import_manifest_scope",
        name="Allow immutable manifest for partial MCP approval-flow migration",
        statements=(
            "ALTER TABLE sqlite_import_runs DROP CONSTRAINT IF EXISTS sqlite_import_runs_scope_check",
            "ALTER TABLE sqlite_import_runs ADD CONSTRAINT sqlite_import_runs_scope_check "
            "CHECK (scope IN ('approval_transaction_slice_v1','approval_and_trust_slice_v2',"
            "'approval_trust_agent_history_v3','approval_trust_agent_history_connectors_v4',"
            "'approval_auth_agent_history_connectors_v5','approval_auth_agent_history_connectors_email_v6',"
            "'approval_auth_agent_mcp_connectors_email_v7'))",
        ),
    ),
    PostgresMigration(
        migration_id="pg_0023_test_run_import_manifest_scope",
        name="Allow immutable manifest for approval evidence import",
        statements=(
            "ALTER TABLE sqlite_import_runs DROP CONSTRAINT IF EXISTS sqlite_import_runs_scope_check",
            "ALTER TABLE sqlite_import_runs ADD CONSTRAINT sqlite_import_runs_scope_check "
            "CHECK (scope IN ('approval_transaction_slice_v1','approval_and_trust_slice_v2',"
            "'approval_trust_agent_history_v3','approval_trust_agent_history_connectors_v4',"
            "'approval_auth_agent_history_connectors_v5','approval_auth_agent_history_connectors_email_v6',"
            "'approval_auth_agent_mcp_connectors_email_v7',"
            "'approval_auth_agent_mcp_test_runs_connectors_email_v8'))",
        ),
    ),
    PostgresMigration(
        migration_id="pg_0024_task_notification_import_manifest_scope",
        name="Allow immutable manifest for AgentTask notification imports",
        statements=(
            "ALTER TABLE sqlite_import_runs DROP CONSTRAINT IF EXISTS sqlite_import_runs_scope_check",
            "ALTER TABLE sqlite_import_runs ADD CONSTRAINT sqlite_import_runs_scope_check "
            "CHECK (scope IN ('approval_transaction_slice_v1','approval_and_trust_slice_v2',"
            "'approval_trust_agent_history_v3','approval_trust_agent_history_connectors_v4',"
            "'approval_auth_agent_history_connectors_v5','approval_auth_agent_history_connectors_email_v6',"
            "'approval_auth_agent_mcp_connectors_email_v7',"
            "'approval_auth_agent_mcp_test_runs_connectors_email_v8',"
            "'approval_auth_agent_mcp_test_runs_connectors_email_task_notifications_v9'))",
        ),
    ),
    PostgresMigration(
        migration_id="pg_0025_delivery_claim_import_manifest_scope",
        name="Allow immutable manifest for approval delivery-claim imports",
        statements=(
            "ALTER TABLE sqlite_import_runs DROP CONSTRAINT IF EXISTS sqlite_import_runs_scope_check",
            "ALTER TABLE sqlite_import_runs ADD CONSTRAINT sqlite_import_runs_scope_check "
            "CHECK (scope IN ('approval_transaction_slice_v1','approval_and_trust_slice_v2',"
            "'approval_trust_agent_history_v3','approval_trust_agent_history_connectors_v4',"
            "'approval_auth_agent_history_connectors_v5','approval_auth_agent_history_connectors_email_v6',"
            "'approval_auth_agent_mcp_connectors_email_v7',"
            "'approval_auth_agent_mcp_test_runs_connectors_email_v8',"
            "'approval_auth_agent_mcp_test_runs_connectors_email_task_notifications_v9',"
            "'approval_auth_agent_mcp_test_runs_connectors_email_task_notifications_delivery_v10'))",
        ),
    ),
    PostgresMigration(
        migration_id="pg_0026_auth_rate_limit_import_manifest_scope",
        name="Allow immutable manifest for authentication rate-limit imports",
        statements=(
            "ALTER TABLE sqlite_import_runs DROP CONSTRAINT IF EXISTS sqlite_import_runs_scope_check",
            "ALTER TABLE sqlite_import_runs ADD CONSTRAINT sqlite_import_runs_scope_check "
            "CHECK (scope IN ('approval_transaction_slice_v1','approval_and_trust_slice_v2',"
            "'approval_trust_agent_history_v3','approval_trust_agent_history_connectors_v4',"
            "'approval_auth_agent_history_connectors_v5','approval_auth_agent_history_connectors_email_v6',"
            "'approval_auth_agent_mcp_connectors_email_v7',"
            "'approval_auth_agent_mcp_test_runs_connectors_email_v8',"
            "'approval_auth_agent_mcp_test_runs_connectors_email_task_notifications_v9',"
            "'approval_auth_agent_mcp_test_runs_connectors_email_task_notifications_delivery_v10',"
            "'approval_auth_agent_mcp_test_runs_connectors_email_task_notifications_delivery_rate_limits_v11'))",
        ),
    ),
    PostgresMigration(
        migration_id="pg_0027_approval_invocation_leases",
        name="Track scoped connector invocation liveness leases",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS approval_invocation_leases (
                approval_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                source_agent_id TEXT NOT NULL,
                source_invocation_id TEXT NOT NULL,
                heartbeat_at TIMESTAMPTZ NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                FOREIGN KEY (approval_id, workspace_id)
                    REFERENCES approval_requests(approval_id, workspace_id) ON DELETE RESTRICT,
                FOREIGN KEY (source_agent_id, workspace_id)
                    REFERENCES external_agents(agent_id, workspace_id) ON DELETE RESTRICT,
                UNIQUE (workspace_id, source_agent_id, source_invocation_id),
                CHECK (length(approval_id) BETWEEN 1 AND 256),
                CHECK (length(workspace_id) BETWEEN 1 AND 256),
                CHECK (length(source_agent_id) BETWEEN 1 AND 256),
                CHECK (length(source_invocation_id) BETWEEN 1 AND 256),
                CHECK (expires_at >= heartbeat_at)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_approval_invocation_lease_expiry "
            "ON approval_invocation_leases(expires_at)",
        ),
    ),
    PostgresMigration(
        migration_id="pg_0028_invocation_lease_import_manifest_scope",
        name="Allow immutable manifest for invocation lease imports",
        statements=(
            "ALTER TABLE sqlite_import_runs DROP CONSTRAINT IF EXISTS sqlite_import_runs_scope_check",
            "ALTER TABLE sqlite_import_runs ADD CONSTRAINT sqlite_import_runs_scope_check "
            "CHECK (scope IN ('approval_transaction_slice_v1','approval_and_trust_slice_v2',"
            "'approval_trust_agent_history_v3','approval_trust_agent_history_connectors_v4',"
            "'approval_auth_agent_history_connectors_v5','approval_auth_agent_history_connectors_email_v6',"
            "'approval_auth_agent_mcp_connectors_email_v7',"
            "'approval_auth_agent_mcp_test_runs_connectors_email_v8',"
            "'approval_auth_agent_mcp_test_runs_connectors_email_task_notifications_v9',"
            "'approval_auth_agent_mcp_test_runs_connectors_email_task_notifications_delivery_v10',"
            "'approval_auth_agent_mcp_test_runs_connectors_email_task_notifications_delivery_rate_limits_v11',"
            "'approval_auth_agent_mcp_test_runs_connectors_email_task_notifications_delivery_rate_limits_invocation_leases_v12'))",
        ),
    ),
    PostgresMigration(
        migration_id="pg_0029_approval_expiry_sweep",
        name="Index bounded approval expiration selection",
        statements=(
            "CREATE INDEX IF NOT EXISTS idx_approval_requests_expiry_due "
                "ON approval_requests(status,expires_at,created_at,approval_id)",
        ),
    ),
)

POSTGRES_MIGRATIONS = (
    *POSTGRES_MIGRATIONS,
    PostgresMigration(
        migration_id="pg_0030_agent_run_branch_intents",
        name="Persist idempotent AgentRun branch creation intents",
        statements=(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_run_steps_id_workspace "
            "ON agent_run_steps(step_id,workspace_id)",
            """
            CREATE TABLE IF NOT EXISTS agent_run_branch_intents (
                intent_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                approval_id TEXT NOT NULL,
                step_id TEXT NOT NULL,
                action_id TEXT NOT NULL,
                lease_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                repository TEXT NOT NULL,
                source_branch TEXT NOT NULL,
                target_ref TEXT NOT NULL,
                source_sha TEXT NOT NULL,
                lease_token_hash TEXT NOT NULL,
                initial_lease_token_hash TEXT NOT NULL,
                run_revision TIMESTAMPTZ NOT NULL,
                step_revision TIMESTAMPTZ NOT NULL,
                status TEXT NOT NULL CHECK (status IN (
                    'INTENT','REMOTE_VERIFIED','RECONCILIATION_REQUIRED','COMPLETED'
                )),
                branch_url TEXT,
                reason TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL,
                payload JSONB NOT NULL,
                UNIQUE (workspace_id,run_id,step_id,idempotency_key),
                UNIQUE (action_id,workspace_id),
                FOREIGN KEY (run_id,workspace_id)
                    REFERENCES agent_runs(run_id,workspace_id) ON DELETE RESTRICT,
                FOREIGN KEY (task_id,workspace_id)
                    REFERENCES agent_tasks(task_id,workspace_id) ON DELETE RESTRICT,
                FOREIGN KEY (approval_id,workspace_id)
                    REFERENCES approval_requests(approval_id,workspace_id) ON DELETE RESTRICT,
                FOREIGN KEY (step_id,workspace_id)
                    REFERENCES agent_run_steps(step_id,workspace_id) ON DELETE RESTRICT,
                FOREIGN KEY (lease_id,workspace_id)
                    REFERENCES capability_leases(lease_id,workspace_id) ON DELETE RESTRICT,
                CHECK (payload ? 'id' AND payload->>'id'=intent_id),
                CHECK (payload ? 'workspace_id' AND payload->>'workspace_id'=workspace_id),
                CHECK (payload ? 'run_id' AND payload->>'run_id'=run_id),
                CHECK (payload ? 'task_id' AND payload->>'task_id'=task_id),
                CHECK (payload ? 'approval_id' AND payload->>'approval_id'=approval_id),
                CHECK (payload ? 'step_id' AND payload->>'step_id'=step_id),
                CHECK (payload ? 'action_id' AND payload->>'action_id'=action_id),
                CHECK (payload ? 'lease_id' AND payload->>'lease_id'=lease_id),
                CHECK (payload ? 'idempotency_key' AND payload->>'idempotency_key'=idempotency_key),
                CHECK (payload ? 'repository' AND payload->>'repository'=repository),
                CHECK (payload ? 'source_branch' AND payload->>'source_branch'=source_branch),
                CHECK (payload ? 'target_ref' AND payload->>'target_ref'=target_ref),
                CHECK (payload ? 'source_sha' AND payload->>'source_sha'=source_sha),
                CHECK (payload ? 'lease_token_hash' AND payload->>'lease_token_hash'=lease_token_hash),
                CHECK (payload ? 'initial_lease_token_hash' AND payload->>'initial_lease_token_hash'=initial_lease_token_hash),
                CHECK (payload ? 'status' AND payload->>'status'=status),
                CHECK (source_sha ~ '^[a-f0-9]{40}$'),
                CHECK (lease_token_hash ~ '^[a-f0-9]{64}$'),
                CHECK (initial_lease_token_hash ~ '^[a-f0-9]{64}$')
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_agent_run_branch_intents_reconcile "
            "ON agent_run_branch_intents(workspace_id,status,updated_at,intent_id) "
            "WHERE status IN ('INTENT','RECONCILIATION_REQUIRED')",
        ),
    ),
    PostgresMigration(
        migration_id="pg_0031_agent_run_branch_post_claim",
        name="Fence one-shot AgentRun branch create operations",
        statements=(
            "ALTER TABLE agent_run_branch_intents DROP CONSTRAINT IF EXISTS agent_run_branch_intents_status_check",
            "ALTER TABLE agent_run_branch_intents ADD CONSTRAINT agent_run_branch_intents_status_check "
            "CHECK (status IN ('INTENT','POST_CLAIMED','REMOTE_VERIFIED','RECONCILIATION_REQUIRED','COMPLETED'))",
            "DROP INDEX IF EXISTS idx_agent_run_branch_intents_reconcile",
            "CREATE INDEX idx_agent_run_branch_intents_reconcile "
            "ON agent_run_branch_intents(workspace_id,status,updated_at,intent_id) "
            "WHERE status IN ('INTENT','POST_CLAIMED','RECONCILIATION_REQUIRED')",
        ),
    ),
)


def run_postgres_migrations(connect: Callable[[], Connection]) -> list[dict[str, str]]:
    """Apply PostgreSQL migrations under a database-wide transaction lock."""
    with connect() as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (PG_MIGRATION_LOCK_ID,))
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {PG_MIGRATION_TABLE} (
                    migration_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    migration_checksum TEXT NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            # Older predeployment databases may have the initial table without
            # checksums. Keep the upgrade transactional, then reject any legacy
            # applied rows below until an operator verifies their exact schema.
            conn.execute(
                f"ALTER TABLE {PG_MIGRATION_TABLE} "
                "ADD COLUMN IF NOT EXISTS migration_checksum TEXT"
            )
            rows = conn.execute(
                f"SELECT migration_id, name, migration_checksum, applied_at "
                f"FROM {PG_MIGRATION_TABLE}"
            ).fetchall()
            applied = {row[0]: row for row in rows}
            known = {migration.migration_id for migration in POSTGRES_MIGRATIONS}
            unknown = sorted(set(applied) - known)
            if unknown:
                raise RuntimeError(
                    "Database schema contains migrations newer than this backend understands: "
                    + ", ".join(unknown)
                )

            for migration in POSTGRES_MIGRATIONS:
                prior = applied.get(migration.migration_id)
                checksum = _migration_checksum(migration)
                if prior is not None:
                    _, prior_name, prior_checksum, _ = prior
                    if prior_checksum is None:
                        raise RuntimeError(
                            "Applied PostgreSQL migration lacks a checksum; "
                            "operator verification is required: " + migration.migration_id
                        )
                    if prior_name != migration.name or prior_checksum != checksum:
                        raise RuntimeError(
                            "Applied PostgreSQL migration definition changed; "
                            "use a new forward migration: " + migration.migration_id
                        )
                    continue
                for statement in migration.statements:
                    conn.execute(statement)
                conn.execute(
                    f"INSERT INTO {PG_MIGRATION_TABLE} "
                    "(migration_id, name, migration_checksum, applied_at) "
                    "VALUES (%s, %s, %s, %s)",
                    (
                        migration.migration_id,
                        migration.name,
                        checksum,
                        datetime.now(timezone.utc),
                    ),
                )

            return [
                {
                    "migration_id": migration_id,
                    "name": name,
                    "migration_checksum": checksum,
                    "applied_at": applied_at.isoformat(),
                }
                for migration_id, name, checksum, applied_at in conn.execute(
                    f"SELECT migration_id, name, migration_checksum, applied_at "
                    f"FROM {PG_MIGRATION_TABLE} "
                    "ORDER BY migration_id"
                ).fetchall()
            ]
