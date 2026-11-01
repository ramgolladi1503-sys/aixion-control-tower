from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import Connection, sql

from app import postgres_database
from app.postgres_database import _migration_checksum, run_postgres_migrations


@pytest.fixture
def pg_run_validation_migration_context() -> Iterator[tuple[str, object]]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL migration tests")
    schema = f"ct_run_validation_migration_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        conn = psycopg.connect(dsn)
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return conn

    try:
        yield schema, connect
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


class TestRunValidationMigration:
    """Migration-only coverage; coordinator/runtime tests belong outside this class."""

    def _assert_schema(self, schema: str, connect) -> None:
        migration = next(
            item for item in postgres_database.POSTGRES_MIGRATIONS
            if item.migration_id == "pg_0033_agent_run_validation_intents"
        )
        with connect() as conn:
            ledger = conn.execute(
                "SELECT name,migration_checksum FROM schema_migrations WHERE migration_id=%s",
                (migration.migration_id,),
            ).fetchone()
            assert ledger == (migration.name, _migration_checksum(migration))
            assert conn.execute(
                "SELECT to_regclass(%s),to_regclass(%s)",
                ("agent_run_validation_intents", "agent_run_validation_command_claims"),
            ).fetchone() == (
                "agent_run_validation_intents", "agent_run_validation_command_claims",
            )

            parent = {
                name: (kind, definition)
                for name, kind, definition in conn.execute(
                    "SELECT conname,contype,pg_get_constraintdef(oid) "
                    "FROM pg_constraint WHERE conrelid=%s::regclass",
                    (f"{schema}.agent_run_validation_intents",),
                ).fetchall()
            }
            assert parent["agent_run_validation_intents_pkey"][0] == "p"
            parent_defs = " ".join(definition for _kind, definition in parent.values())
            assert "UNIQUE (workspace_id, run_id, step_id, idempotency_key)" in parent_defs
            assert "UNIQUE (intent_id, workspace_id)" in parent_defs
            assert "approval_hash" in parent_defs and "test_plan_hash" in parent_defs
            assert "bundle_revision" in parent_defs and "bundle_git_tree_sha" in parent_defs
            assert "^[a-f0-9]{40}$" in parent_defs
            assert "sha256:(v2:)?" in parent_defs
            assert conn.execute(
                "SELECT %s ~ '^sha256:(v2:)?[a-f0-9]{64}$', "
                "%s ~ '^sha256:(v2:)?[a-f0-9]{64}$', "
                "%s ~ '^sha256:(v2:)?[a-f0-9]{64}$'",
                ("sha256:" + "a" * 64, "sha256:v2:" + "b" * 64, "c" * 64),
            ).fetchone() == (True, True, False)
            assert conn.execute(
                "SELECT %s ~ '^[a-f0-9]{40}$', %s ~ '^[a-f0-9]{40}$'",
                ("a" * 40, "g" * 40),
            ).fetchone() == (True, False)
            parent_columns = {
                row[0] for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name='agent_run_validation_intents'",
                    (schema,),
                ).fetchall()
            }
            assert "action_id" not in parent_columns
            assert {"bundle_revision", "bundle_git_tree_sha"} <= parent_columns
            assert all(status in parent_defs for status in (
                "INTENT", "EXECUTION_CLAIMED", "RECONCILIATION_REQUIRED", "COMPLETED",
            ))
            assert "ON DELETE RESTRICT" in parent_defs
            assert "CASCADE" not in parent_defs

            child = {
                name: (kind, definition)
                for name, kind, definition in conn.execute(
                    "SELECT conname,contype,pg_get_constraintdef(oid) "
                    "FROM pg_constraint WHERE conrelid=%s::regclass",
                    (f"{schema}.agent_run_validation_command_claims",),
                ).fetchall()
            }
            assert child["agent_run_validation_command_claims_pkey"][0] == "p"
            child_defs = " ".join(definition for _kind, definition in child.values())
            assert "PRIMARY KEY (intent_id, command_index)" in child_defs
            assert "ON DELETE RESTRICT" in child_defs
            foreign_keys = [
                definition for kind, definition in child.values() if kind == "f"
            ]
            assert any(
                "FOREIGN KEY (intent_id, workspace_id)" in definition
                and "agent_run_validation_intents(intent_id, workspace_id)" in definition
                for definition in foreign_keys
            )
            assert any(
                "FOREIGN KEY (action_id, workspace_id)" in definition
                and "proposed_actions(action_id, workspace_id)" in definition
                for definition in foreign_keys
            )
            assert "workspace_id" in child_defs and "action_id" in child_defs
            assert "CLAIMED" in child_defs and "RESULT_RECORDED" in child_defs
            assert "RECONCILIATION_REQUIRED" in child_defs
            assert "1048576" in child_defs
            assert "stdout" in child_defs and "stderr" in child_defs and "command" in child_defs
            command_columns = {
                row[0] for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name='agent_run_validation_command_claims'",
                    (schema,),
                ).fetchall()
            }
            assert not {"stdout", "stderr", "command"} & command_columns
            assert {"stdout_sha256", "stdout_bytes", "stderr_sha256", "stderr_bytes"} <= command_columns
            triggers = {
                row[0] for row in conn.execute(
                    "SELECT tgname FROM pg_trigger "
                    "WHERE tgrelid IN (%s::regclass,%s::regclass) AND NOT tgisinternal",
                    (f"{schema}.agent_run_validation_intents",
                     f"{schema}.agent_run_validation_command_claims"),
                ).fetchall()
            }
            assert triggers == {
                "trg_agent_run_validation_intent_status_transition",
                "trg_agent_run_validation_command_status_transition",
            }
            indexes = conn.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname=%s "
                "AND indexname IN (%s,%s) ORDER BY indexname",
                (schema, "idx_agent_run_validation_reconcile",
                 "idx_agent_run_validation_claims_reconcile"),
            ).fetchall()
            assert {row[0] for row in indexes} == {
                "idx_agent_run_validation_reconcile",
                "idx_agent_run_validation_claims_reconcile",
            }

    def test_fresh_install_replay_and_constraints(
        self, pg_run_validation_migration_context,
    ) -> None:
        schema, connect = pg_run_validation_migration_context
        run_postgres_migrations(connect)
        self._assert_schema(schema, connect)
        run_postgres_migrations(connect)
        self._assert_schema(schema, connect)


class TestRunValidationIntentRepositoryUnit:
    def test_record_intent_uses_database_time_and_share_locks_lease(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from contextlib import nullcontext
        from datetime import datetime, timedelta, timezone
        from types import SimpleNamespace

        import app.postgres_agent_run_validation as validation
        from app.postgres_agent_run_validation import (
            AgentRunValidationIntent,
            PostgresAgentRunValidationIntentRepository,
            RunValidationIntentStatus,
        )
        from app.trust_models import CapabilityActionType, CapabilityLeaseStatus

        db_now = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
        stale_client_time = db_now - timedelta(days=5)
        command = "pytest -q"
        approval = SimpleNamespace(
            id="approval-1", project_id="project-1", workspace_id="workspace-1",
            status=SimpleNamespace(value="APPROVED"), test_plan=[command],
            approved_payload_hash="sha256:" + "a" * 64,
        )
        run = SimpleNamespace(
            id="run-1", task_id="task-1", workspace_id="workspace-1",
            approval_request_id="approval-1", capability_lease_id="lease-1",
            status=SimpleNamespace(value="RUNNING"), lease_token="token",
            lease_owner="worker", lease_expires_at=db_now + timedelta(hours=1),
            repository="owner/repo", updated_at=db_now - timedelta(minutes=1),
            correlation_id="correlation-1",
        )
        step = SimpleNamespace(
            id="step-1", run_id="run-1", task_id="task-1",
            status=validation.AgentRunStepStatus.RUNNING,
            step_type=validation.AgentRunStepType.RUN_VALIDATION,
            lease_owner="worker", lease_token="token",
            lease_expires_at=db_now + timedelta(hours=1),
            idempotency_key="step-key", updated_at=db_now - timedelta(seconds=30),
            attempt_count=1,
        )
        task = SimpleNamespace(
            id="task-1", provider="antigravity", external_agent_id="agent-1",
        )
        scope = SimpleNamespace(
            repository="owner/repo",
            allowed_actions={CapabilityActionType.RUN_COMMAND},
            allowed_commands={command},
        )
        lease = SimpleNamespace(
            id="lease-1", workspace_id="workspace-1", approval_request_id="approval-1",
            approved_payload_hash=approval.approved_payload_hash,
            status=CapabilityLeaseStatus.ACTIVE,
            expires_at=db_now + timedelta(hours=1), provider=task.provider,
            agent_id=task.external_agent_id, scope=scope, receipt_signature="sig",
        )
        record = AgentRunValidationIntent(
            id="intent-1", workspace_id="workspace-1", run_id="run-1",
            task_id="task-1", approval_id="approval-1", step_id="step-1",
            lease_id="lease-1", idempotency_key="step-key",
            approval_hash=approval.approved_payload_hash,
            test_plan_hash="b" * 64, lease_token_hash="c" * 64,
            bundle_revision="1" * 40, bundle_git_tree_sha="2" * 40,
            run_revision=run.updated_at, step_revision=step.updated_at,
            status=RunValidationIntentStatus.INTENT,
            created_at=stale_client_time, updated_at=stale_client_time,
        )

        class FakeConnection:
            def __init__(self) -> None:
                self.calls: list[tuple[str, tuple[object, ...]]] = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def transaction(self):
                return nullcontext()

            def execute(self, query, params=None):
                normalized = " ".join(str(query).split())
                self.calls.append((normalized, tuple(params or ())))
                if normalized == "SELECT clock_timestamp()":
                    return SimpleNamespace(fetchone=lambda: (db_now,))
                if normalized.startswith("SELECT intent_id,workspace_id,run_id,task_id"):
                    return SimpleNamespace(fetchone=lambda: None)
                return SimpleNamespace(rowcount=1)

        conn = FakeConnection()
        share_lock_arguments: list[bool] = []
        monkeypatch.setattr(validation, "compute_approval_payload_hash", lambda _approval: approval.approved_payload_hash)
        monkeypatch.setattr(
            validation, "sha256_hex",
            lambda value: "b" * 64 if value == [command] else "c" * 64,
        )
        monkeypatch.setattr(validation, "validate_validation_plan", lambda _approval: None)
        monkeypatch.setattr(validation, "lease_receipt_payload", lambda _lease: {})
        monkeypatch.setattr(validation, "verify_payload_signature", lambda *_args: True)
        repository = PostgresAgentRunValidationIntentRepository(lambda: conn)
        repository._lock_validation_authority = lambda *_args, **_kwargs: (approval, task, run, step)

        def get_lease(_conn, _lease_id, _workspace_id, *, for_share=False, **_kwargs):
            share_lock_arguments.append(for_share)
            return lease

        repository._trust.get_capability_lease = get_lease
        saved = repository.record_intent(
            record=record, expected_run=run, expected_step=step, approval=approval,
            worker_id="worker", lease_token="token", actor="worker:worker",
        )
        assert share_lock_arguments == [True]
        assert saved.created_at == db_now and saved.updated_at == db_now
        insert_calls = [call for call in conn.calls if call[0].startswith("INSERT INTO")]
        assert len(insert_calls) == 3
        parent_params = insert_calls[0][1]
        assert parent_params[17:19] == (db_now, db_now)
        assert parent_params[19].obj["created_at"] == db_now.isoformat().replace("+00:00", "Z")
        for query, params in insert_calls[1:]:
            assert "created_at" in query
            assert db_now in params

    def test_result_is_bounded_digest_only_and_claim_cas_is_strict(self) -> None:
        from contextlib import nullcontext
        from datetime import datetime, timezone
        from types import SimpleNamespace

        from app.postgres_agent_run_validation import (
            AgentRunValidationCommandClaim,
            AgentRunValidationIntent,
            PostgresAgentRunValidationIntentRepository,
            RunValidationIntentStatus,
            RunValidationCommandStatus,
        )
        from app.postgres_trust_repository import PostgresTrustRepositoryConflict
        from app.trust_crypto import sha256_hex

        class FakeConnection:
            def __init__(self, parent, claim, rowcount: int = 1) -> None:
                self.parent = parent
                self.claim = claim
                self.rowcount = rowcount
                self.calls: list[tuple[str, tuple[object, ...]]] = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def transaction(self):
                return nullcontext()

            def execute(self, query, params=None):
                self.calls.append((str(query), tuple(params or ())))
                normalized = " ".join(str(query).split())
                if normalized.startswith("SELECT run_id,task_id,approval_id,step_id"):
                    return SimpleNamespace(fetchone=lambda: (
                        self.parent.run_id, self.parent.task_id,
                        self.parent.approval_id, self.parent.step_id,
                    ))
                if normalized.startswith("SELECT intent_id,workspace_id,run_id,task_id"):
                    return SimpleNamespace(fetchone=lambda: (
                        self.parent.id, self.parent.workspace_id, self.parent.run_id,
                        self.parent.task_id, self.parent.approval_id, self.parent.step_id,
                        self.parent.lease_id, self.parent.idempotency_key,
                        self.parent.approval_hash, self.parent.test_plan_hash,
                        self.parent.lease_token_hash, self.parent.bundle_revision,
                        self.parent.bundle_git_tree_sha, self.parent.run_revision,
                        self.parent.step_revision, self.parent.status.value,
                        self.parent.reason, self.parent.created_at, self.parent.updated_at,
                        self.parent.model_dump(mode="json"),
                    ))
                if normalized.startswith("SELECT intent_id,workspace_id,action_id,command_index"):
                    return SimpleNamespace(fetchone=lambda: (
                        self.claim.intent_id, self.claim.workspace_id,
                        self.claim.action_id, self.claim.command_index,
                        self.claim.command_hash, self.claim.status.value,
                        self.claim.exit_code, self.claim.timed_out,
                        self.claim.stdout_sha256, self.claim.stdout_bytes,
                        self.claim.stderr_sha256, self.claim.stderr_bytes,
                        self.claim.reason, self.claim.claimed_at, self.claim.updated_at,
                        self.claim.model_dump(mode="json"),
                    ))
                if "clock_timestamp()" in normalized:
                    return SimpleNamespace(fetchone=lambda: (self.parent.updated_at,))
                return SimpleNamespace(rowcount=self.rowcount)

        now = datetime(2026, 10, 23, tzinfo=timezone.utc)
        intent = AgentRunValidationIntent(
            id="intent-1", workspace_id="workspace-1", run_id="run-1",
            task_id="task-1", approval_id="approval-1", step_id="step-1",
            lease_id="lease-1", idempotency_key="step-key",
            approval_hash="sha256:" + "d" * 64,
            test_plan_hash="e" * 64,
            lease_token_hash=sha256_hex({"lease_token": "lease-token"}),
            bundle_revision="1" * 40, bundle_git_tree_sha="2" * 40,
            run_revision=now, step_revision=now,
            status=RunValidationIntentStatus.EXECUTION_CLAIMED,
            created_at=now, updated_at=now,
        )
        claim = AgentRunValidationCommandClaim(
            intent_id="intent-1", workspace_id="workspace-1", action_id="action-1",
            command_index=0, command_hash=sha256_hex({"command": "pytest -q"}),
            status=RunValidationCommandStatus.CLAIMED,
            claimed_at=now, updated_at=now,
        )
        conn = FakeConnection(intent, claim)
        repository = PostgresAgentRunValidationIntentRepository(lambda: conn)
        command = "pytest -q"
        approval = SimpleNamespace(test_plan=[command])
        run = SimpleNamespace(updated_at=now)
        step = SimpleNamespace(updated_at=now)
        action = SimpleNamespace(
            id=claim.action_id, command=command,
            metadata={"command_index": 0, "bundle_revision": intent.bundle_revision,
                      "bundle_git_tree_sha": intent.bundle_git_tree_sha},
        )
        repository._lock_validation_authority = lambda *_args, **_kwargs: (approval, None, run, step)
        repository._trust.lock_agent_run_validation_command_binding = lambda *_args, **_kwargs: action
        saved = repository.record_result(
            claim=claim, worker_id="worker", lease_token="lease-token",
            exit_code=0, timed_out=False,
            stdout_sha256="b" * 64, stdout_bytes=12,
            stderr_sha256="c" * 64, stderr_bytes=0,
        )
        assert saved.status == RunValidationCommandStatus.RESULT_RECORDED
        assert len(conn.calls) == 5
        query, params = conn.calls[-1]
        assert "status='CLAIMED'" in query
        assert params[3:7] == ("b" * 64, 12, "c" * 64, 0)
        payload = params[8].obj
        assert payload["status"] == "RESULT_RECORDED"
        assert not {"command", "stdout", "stderr"} & payload.keys()

        stale_conn = FakeConnection(intent, claim, rowcount=0)
        stale_repository = PostgresAgentRunValidationIntentRepository(lambda: stale_conn)
        stale_repository._lock_validation_authority = lambda *_args, **_kwargs: (approval, None, run, step)
        stale_repository._trust.lock_agent_run_validation_command_binding = lambda *_args, **_kwargs: action
        with pytest.raises(PostgresTrustRepositoryConflict, match="one-shot claim CAS"):
            stale_repository.record_result(
                claim=claim, worker_id="worker", lease_token="lease-token",
                exit_code=0, timed_out=False,
                stdout_sha256="b" * 64, stdout_bytes=12,
                stderr_sha256="c" * 64, stderr_bytes=0,
            )

    def test_result_rejects_unbounded_or_malformed_evidence_before_database_write(self) -> None:
        from datetime import datetime, timezone

        from app.postgres_agent_run_validation import (
            AgentRunValidationCommandClaim,
            PostgresAgentRunValidationIntentRepository,
            RunValidationCommandStatus,
        )

        class NeverConnect:
            def __call__(self):
                raise AssertionError("invalid command evidence must fail before opening a connection")

        now = datetime(2026, 10, 23, tzinfo=timezone.utc)
        claim = AgentRunValidationCommandClaim(
            intent_id="intent-2", workspace_id="workspace-1", action_id="action-2",
            command_index=0, command_hash="a" * 64,
            status=RunValidationCommandStatus.CLAIMED,
            claimed_at=now, updated_at=now,
        )
        repository = PostgresAgentRunValidationIntentRepository(NeverConnect())
        with pytest.raises(ValueError, match="result is invalid"):
            repository.record_result(
                claim=claim, worker_id="worker", lease_token="token",
                exit_code=0, timed_out=False,
                stdout_sha256="invalid", stdout_bytes=12,
                stderr_sha256="c" * 64, stderr_bytes=0,
            )
        with pytest.raises(ValueError, match="result is invalid"):
            repository.record_result(
                claim=claim, worker_id="worker", lease_token="token",
                exit_code=0, timed_out=False,
                stdout_sha256="b" * 64, stdout_bytes=1_048_577,
                stderr_sha256="c" * 64, stderr_bytes=0,
            )

    def test_first_running_attempt_uses_current_attempt_count_in_consumption(self) -> None:
        from datetime import datetime, timezone
        from types import SimpleNamespace

        from app.postgres_agent_run_validation import (
            AgentRunValidationIntent,
            RunValidationIntentStatus,
            _validation_action_consumption,
        )

        now = datetime(2026, 10, 23, tzinfo=timezone.utc)
        intent = AgentRunValidationIntent(
            id="intent-attempt", workspace_id="ws", run_id="run", task_id="task",
            approval_id="approval", step_id="step", lease_id="lease",
            idempotency_key="step-key", approval_hash="sha256:" + "a" * 64,
            test_plan_hash="b" * 64, lease_token_hash="c" * 64,
            bundle_revision="1" * 40, bundle_git_tree_sha="2" * 40,
            run_revision=now, step_revision=now,
            status=RunValidationIntentStatus.EXECUTION_CLAIMED,
            created_at=now, updated_at=now,
        )
        action = SimpleNamespace(id="action-first")
        first_attempt = _validation_action_consumption(
            intent=intent, action=action, command_index=0,
            command_hash="d" * 64, runtime_seconds=60, attempt_count=1,
        )
        assert first_attempt.evidence["authorized_attempt_count"] == 1
        assert first_attempt.evidence["authorized_retry_consumed"] is False
        assert first_attempt.retry_consumed is False
        second_attempt = _validation_action_consumption(
            intent=intent, action=action, command_index=0,
            command_hash="d" * 64, runtime_seconds=60, attempt_count=2,
        )
        assert second_attempt.evidence["authorized_attempt_count"] == 2
        assert second_attempt.evidence["authorized_retry_consumed"] is True
        assert second_attempt.retry_consumed is True


class TestRunValidationMigrationUpgrade:
    def test_upgrade_from_pg_0032_preserves_rows_and_replays(
        self, pg_run_validation_migration_context, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        schema, connect = pg_run_validation_migration_context
        migrations = postgres_database.POSTGRES_MIGRATIONS
        target = next(i for i, item in enumerate(migrations)
                      if item.migration_id == "pg_0033_agent_run_validation_intents")
        monkeypatch.setattr(postgres_database, "POSTGRES_MIGRATIONS", migrations[:target])
        run_postgres_migrations(connect)
        workspace_id = f"ws_{uuid.uuid4().hex}"
        with connect() as conn:
            conn.execute(
                "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s::jsonb)",
                (workspace_id, '{"id":"' + workspace_id
                 + '","owner_user_id":"owner_' + uuid.uuid4().hex + '"}'),
            )
            assert conn.execute(
                "SELECT count(*) FROM schema_migrations WHERE migration_id=%s",
                ("pg_0032_agent_run_apply_change_intents",),
            ).fetchone() == (1,)

        monkeypatch.setattr(postgres_database, "POSTGRES_MIGRATIONS", migrations)
        run_postgres_migrations(connect)
        TestRunValidationMigration()._assert_schema(schema, connect)
        with connect() as conn:
            assert conn.execute(
                "SELECT payload FROM workspaces WHERE workspace_id=%s", (workspace_id,),
            ).fetchone()[0]["id"] == workspace_id
            assert conn.execute(
                "SELECT count(*) FROM schema_migrations WHERE migration_id=%s",
                ("pg_0033_agent_run_validation_intents",),
            ).fetchone() == (1,)
        run_postgres_migrations(connect)
        TestRunValidationMigration()._assert_schema(schema, connect)
