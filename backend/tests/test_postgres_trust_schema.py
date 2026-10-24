from __future__ import annotations

import os
import uuid
from decimal import Decimal

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from app.models import (
    ApprovalRequest,
    ApprovalStatus,
    Project,
    RiskAssessment,
    RiskLevel,
    User,
    UserRole,
    Workspace,
    now_utc,
)
from app.postgres_database import POSTGRES_MIGRATIONS, run_postgres_migrations
from app.trust_models import (
    CapabilityActionType,
    CapabilityLease,
    CapabilityLeaseMode,
    CapabilityScope,
)


@pytest.fixture
def migrated_trust_schema():
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL integration tests")
    schema = f"ct_trust_schema_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect():
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    try:
        run_postgres_migrations(connect)
        yield schema, connect
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_pg_0015_creates_all_workspace_scoped_trust_relations(migrated_trust_schema):
    schema, connect = migrated_trust_schema
    expected = {
        "capability_leases",
        "reviewer_attestations",
        "proposed_actions",
        "policy_decisions",
        "action_authorizations",
        "action_consumptions",
        "credential_grants",
    }
    with connect() as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema=%s AND table_type='BASE TABLE'",
            (schema,),
        ).fetchall()
        tables = {row[0] for row in rows}
        assert expected <= tables

        for table in expected:
            workspace_column = conn.execute(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s AND column_name='workspace_id'",
                (schema, table),
            ).fetchone()
            assert workspace_column == ("NO",), table
            foreign_keys = conn.execute(
                "SELECT count(*) FROM pg_constraint "
                "WHERE conrelid=to_regclass(%s) AND contype='f'",
                (f'"{schema}"."{table}"',),
            ).fetchone()[0]
            assert foreign_keys >= 1, table


def test_pg_0015_enforces_action_decision_and_consumption_relations(migrated_trust_schema):
    schema, connect = migrated_trust_schema
    with connect() as conn:
        constraints = {
            row[0]
            for row in conn.execute(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid=to_regclass(%s) AND contype='f'",
                (f'"{schema}"."policy_decisions"',),
            ).fetchall()
        }
        assert "policy_decisions_action_id_workspace_id_fkey" in constraints
        assert "policy_decisions_action_id_lease_id_workspace_id_fkey" in constraints

        consumption_constraints = {
            row[0]
            for row in conn.execute(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid=to_regclass(%s) AND contype='f'",
                (f'"{schema}"."action_consumptions"',),
            ).fetchall()
        }
        assert "action_consumptions_action_id_lease_id_workspace_id_fkey" in consumption_constraints


def test_pg_0015_migration_is_recorded_with_stable_checksum(migrated_trust_schema):
    _, connect = migrated_trust_schema
    with connect() as conn:
        row = conn.execute(
            "SELECT name,migration_checksum FROM schema_migrations "
            "WHERE migration_id='pg_0015_workspace_scoped_trust_records'"
        ).fetchone()
    assert row is not None
    assert row[0] == "Add workspace-scoped PostgreSQL trust and action records"
    assert len(row[1]) == 64


def test_pg_0016_upgrade_preserves_existing_lease_consumption_counters(monkeypatch):
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL integration tests")
    schema = f"ct_trust_upgrade_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect():
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    try:
        target_migration_id = "pg_0016_atomic_trust_consumption_budgets"
        target_index = next(
            index for index, migration in enumerate(POSTGRES_MIGRATIONS)
            if migration.migration_id == target_migration_id
        )
        assert POSTGRES_MIGRATIONS[target_index].migration_id == target_migration_id
        monkeypatch.setattr("app.postgres_database.POSTGRES_MIGRATIONS", POSTGRES_MIGRATIONS[:target_index])
        run_postgres_migrations(connect)

        workspace_id = f"workspace_{uuid.uuid4().hex}"
        user_id = f"user_{uuid.uuid4().hex}"
        project_id = f"project_{uuid.uuid4().hex}"
        approval_id = f"approval_{uuid.uuid4().hex}"
        now = now_utc()
        workspace = Workspace(id=workspace_id, name="Upgrade", owner_user_id=user_id)
        user = User(
            id=user_id,
            email=f"{user_id}@example.test",
            role=UserRole.OWNER,
            password_hash="test-password-hash",
            password_salt="test-password-salt",
            workspace_id=workspace_id,
        )
        project = Project(id=project_id, name="Upgrade", workspace_id=workspace_id)
        approval = ApprovalRequest(
            id=approval_id,
            project_id=project_id,
            title="Upgrade",
            summary="Existing approval",
            target_branch="feature/upgrade-test",
            files=[],
            risk=RiskAssessment(level=RiskLevel.MEDIUM),
            status=ApprovalStatus.APPROVED,
            workspace_id=workspace_id,
            created_at=now,
            updated_at=now,
        )
        lease = CapabilityLease(
            approval_request_id=approval_id,
            workspace_id=workspace_id,
            approved_payload_hash="sha256:" + "a" * 64,
            mode=CapabilityLeaseMode.BOUNDED,
            scope=CapabilityScope(
                repository="owner/repo",
                branch="feature/upgrade-test",
                allowed_actions=[CapabilityActionType.RUN_COMMAND],
            ),
            issued_by_user_id=user_id,
            expires_at=now,
            receipt_nonce="upgrade-nonce",
            receipt_signature="upgrade-signature",
            consumed_pull_requests=1,
            consumed_retries=2,
            consumed_runtime_seconds=13,
            consumed_cost_usd=0.45,
            created_at=now,
            updated_at=now,
        )

        with connect() as conn:
            conn.execute(
                "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
                (workspace_id, Jsonb(workspace.model_dump(mode="json"))),
            )
            conn.execute(
                "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,"
                "email_verified,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    user_id, workspace_id, user.email, user.email.lower(), user.role.value,
                    user.disabled, user.email_verified, Jsonb(user.model_dump(mode="json")),
                ),
            )
            conn.execute(
                "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
                (project_id, workspace_id, Jsonb(project.model_dump(mode="json"))),
            )
            conn.execute(
                "INSERT INTO approval_requests(approval_id,project_id,workspace_id,work_order_id,"
                "source_agent_id,status,expires_at,created_at,updated_at,version,source_invocation_id,payload) "
                "VALUES(%s,%s,%s,NULL,NULL,%s,NULL,%s,%s,0,NULL,%s)",
                (
                    approval_id, project_id, workspace_id, approval.status.value,
                    approval.created_at, approval.updated_at,
                    Jsonb(approval.model_dump(mode="json")),
                ),
            )
            conn.execute(
                "INSERT INTO capability_leases(lease_id,approval_id,workspace_id,issued_by_user_id,"
                "status,expires_at,payload) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (
                    lease.id, approval_id, workspace_id, user_id, lease.status.value,
                    lease.expires_at, Jsonb(lease.model_dump(mode="json")),
                ),
            )

        monkeypatch.setattr("app.postgres_database.POSTGRES_MIGRATIONS", POSTGRES_MIGRATIONS)
        run_postgres_migrations(connect)
        with connect() as conn:
            row = conn.execute(
                "SELECT consumed_pull_requests,consumed_retries,consumed_runtime_seconds,"
                "consumed_cost_usd,created_at,updated_at FROM capability_leases WHERE lease_id=%s",
                (lease.id,),
            ).fetchone()
            payload_row = conn.execute(
                "SELECT payload->>'consumed_pull_requests',payload->>'consumed_retries',"
                "payload->>'consumed_runtime_seconds',payload->>'consumed_cost_usd' "
                "FROM capability_leases WHERE lease_id=%s",
                (lease.id,),
            ).fetchone()
            assert row == (
                1, 2, 13, Decimal("0.45"), lease.created_at, lease.updated_at,
            )
            assert payload_row == ("1", "2", "13", "0.45")
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_pg_0017_keeps_v1_import_manifests_and_accepts_v2(monkeypatch):
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL integration tests")
    schema = f"ct_manifest_upgrade_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect():
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    try:
        monkeypatch.setattr("app.postgres_database.POSTGRES_MIGRATIONS", POSTGRES_MIGRATIONS[:-1])
        run_postgres_migrations(connect)
        with connect() as conn:
            conn.execute(
                "INSERT INTO sqlite_import_runs(source_sha256,scope,source_counts,imported_counts) "
                "VALUES(%s,%s,%s,%s)",
                ("1" * 64, "approval_transaction_slice_v1", Jsonb({}), Jsonb({})),
            )
        monkeypatch.setattr("app.postgres_database.POSTGRES_MIGRATIONS", POSTGRES_MIGRATIONS)
        run_postgres_migrations(connect)
        with connect() as conn:
            conn.execute(
                "INSERT INTO sqlite_import_runs(source_sha256,scope,source_counts,imported_counts) "
                "VALUES(%s,%s,%s,%s)",
                ("2" * 64, "approval_and_trust_slice_v2", Jsonb({}), Jsonb({})),
            )
            rows = conn.execute(
                "SELECT source_sha256,scope FROM sqlite_import_runs ORDER BY source_sha256"
            ).fetchall()
        assert rows == [
            ("1" * 64, "approval_transaction_slice_v1"),
            ("2" * 64, "approval_and_trust_slice_v2"),
        ]
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
