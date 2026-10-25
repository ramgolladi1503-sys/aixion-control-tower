from __future__ import annotations

import ast
import hashlib
import json
import os
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from app.database_migrations import run_migrations
from app.agent_run_models import (
    AgentRun, AgentRunEvent, AgentRunEventType, AgentRunStatus, AgentRunStep,
    AgentRunStepType,
)
from app.email_delivery import _decrypt_code, _encrypt_code
from app.approval_integrity import compute_approval_payload_hash
from app.models import ApprovalRequest, ApprovalStatus, RiskLevel, UserRole
from app.trust_action_authorization import action_payload_hash, authorization_signature_payload
from app.trust_action_authorization_models import (
    ActionAuthorization,
    ActionAuthorizationDecision,
)
from app.trust_crypto import sha256_hex, sign_payload
from app.trust_models import (
    ActionConsumption,
    CapabilityActionType,
    CapabilityLease,
    CapabilityLeaseMode,
    CapabilityLeaseStatus,
    CapabilityScope,
    CredentialGrant,
    CredentialGrantType,
    PolicyDecision,
    PolicyDecisionType,
    ProposedAction,
    ReviewerAttestation,
    ReviewerAttestationDecision,
    TrustEvent,
    TrustEventType,
)
from app.trust_policy import lease_receipt_payload
from app.postgres_trust_event_repository import PostgresTrustEventRepository
from scripts.import_approval_slice_to_postgres import (
    ImportBlocked,
    SOURCE_KV_ENTITY_DISPOSITIONS,
    SOURCE_TABLE_DISPOSITIONS,
    _target_is_remote,
    _source_table_coverage,
    apply_import,
    build_audit_event_inventory_report,
    build_invocation_drain_report,
    build_plan,
    main,
)


def _sqlite_store_persistent_entity_types() -> set[str]:
    """Read literal entity names written by SQLiteBackedStore.persist().

    Keep this source-derived so a new `_write_map`/`_write_list` call cannot
    silently expand the SQLite persistence contract without importer review.
    Dynamic `persist_records` inserts and repository-owned tables are not
    represented here; those need dedicated inventory as they are introduced.
    """
    source_path = Path(__file__).parents[1] / "app" / "store.py"
    module = ast.parse(source_path.read_text(encoding="utf-8"))
    store_class = next(
        node for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "SQLiteBackedStore"
    )
    persist_method = next(
        node for node in store_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "persist"
    )
    entity_types: set[str] = set()
    for node in ast.walk(persist_method):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"_write_map", "_write_list"} or len(node.args) < 2:
            continue
        entity_name = node.args[1]
        if isinstance(entity_name, ast.Constant) and isinstance(entity_name.value, str):
            entity_types.add(entity_name.value)
    return entity_types


def test_sqlite_persistent_entities_are_accounted_for_by_import_inventory():
    persistent_types = _sqlite_store_persistent_entity_types()

    documented_exclusions: set[str] = set()
    importer_accounted_types = set(SOURCE_KV_ENTITY_DISPOSITIONS)

    assert persistent_types, "SQLite persist() entity inventory unexpectedly empty"
    assert persistent_types <= importer_accounted_types | documented_exclusions, (
        "SQLite-backed entity types are missing from the PostgreSQL importer "
        "per-entity disposition inventory and its documented exclusions: "
        f"{sorted(persistent_types - importer_accounted_types - documented_exclusions)}"
    )
    assert "agent_run_fault" in importer_accounted_types
    assert SOURCE_KV_ENTITY_DISPOSITIONS["audit_event"][0] == "partial_import"
    assert SOURCE_KV_ENTITY_DISPOSITIONS["action_consumption"][0] == "derived_import"
    assert documented_exclusions.isdisjoint(importer_accounted_types)


def test_entity_coverage_reports_exact_partial_audit_counts():
    from scripts.import_approval_slice_to_postgres import _source_kv_entity_coverage

    coverage = _source_kv_entity_coverage(
        {"audit_event": 5, "approval_request": 1},
        {"audit_event": 3, "approval_request": 1},
    )

    assert coverage["audit_event"] == {
        "disposition": "partial_import",
        "target": "audit_events",
        "reason": SOURCE_KV_ENTITY_DISPOSITIONS["audit_event"][2],
        "source_count": 5,
        "imported_count": 3,
        "omitted_count": 2,
    }
    assert coverage["approval_request"]["omitted_count"] == 0


def test_every_migration_created_sqlite_table_has_an_explicit_import_disposition(tmp_path):
    path = tmp_path / "migration-schema.sqlite3"
    with sqlite3.connect(path) as conn:
        run_migrations(conn)
        migration_tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }

    assert migration_tables == set(SOURCE_TABLE_DISPOSITIONS)
    assert SOURCE_TABLE_DISPOSITIONS["schema_migrations"] == ("metadata_only", None)
    assert SOURCE_TABLE_DISPOSITIONS["kv_store"][0] == "partial_entity_import"
    assert {
        table
        for table, (disposition, _target) in SOURCE_TABLE_DISPOSITIONS.items()
        if disposition == "imported"
    } == migration_tables - {"schema_migrations", "kv_store"}

    observed = _source_table_coverage(path)
    assert set(observed) == migration_tables
    assert observed["kv_store"]["disposition"] == "partial_entity_import"


def test_source_table_coverage_fails_closed_for_unclassified_table(tmp_path):
    path = tmp_path / "new-persistent-table.sqlite3"
    with sqlite3.connect(path) as conn:
        run_migrations(conn)
        conn.execute("CREATE TABLE future_persistent_state (value TEXT NOT NULL)")

    with pytest.raises(ImportBlocked, match="without an importer disposition"):
        _source_table_coverage(path)


def test_unknown_kv_entity_type_blocks_before_postgres_connection(tmp_path, monkeypatch, capsys):
    path = tmp_path / "unknown-entity.sqlite3"
    _snapshot(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            ("future_secret_record", "future1", json.dumps({"id": "future1"})),
        )

    connect_calls = []

    def forbidden_connect(*args, **kwargs):
        connect_calls.append((args, kwargs))
        raise AssertionError("unknown source entity reached PostgreSQL")

    monkeypatch.setattr(psycopg, "connect", forbidden_connect)
    monkeypatch.setenv("AIXION_TEST_POSTGRES_DSN", "dbname=control_tower_test host=localhost")
    result = main([
        "--source", str(path), "--apply", "--confirm-source-snapshot",
        "--allow-partial-slice",
    ])

    assert result == 2
    assert connect_calls == []
    assert "without an importer disposition" in capsys.readouterr().err


@pytest.fixture(autouse=True)
def _email_outbox_test_key(monkeypatch):
    monkeypatch.setenv("AIXION_EMAIL_OUTBOX_KEY", "11" * 32)


def _snapshot(
    path,
    *,
    include_omitted=True,
    include_unsupported_notifications=False,
    include_legacy_notifications=False,
    include_trust=False,
    include_delivery_state=False,
    include_rate_limits=False,
    agent_run_faults=None,
):
    now = datetime.now(timezone.utc).isoformat()
    workspace = {"id": "ws1", "name": "Account", "owner_user_id": "u1", "created_at": now}
    project = {"id": "p1", "name": "Project", "workspace_id": "ws1", "created_at": now}
    approval = {
        "id": "a1", "project_id": "p1", "title": "Review", "summary": "Inspect change",
        "work_order_id": "w1", "target_branch": "main", "files": [], "risk": {"level": "LOW"},
        # Approved native invocations need an explicit durable delivery claim
        # before a cutover plan may proceed. Keep the generic fixture terminal;
        # the delivery-state fixtures below exercise the approved/consumed case.
        "status": "APPROVED" if include_delivery_state else "DENIED",
        "workspace_id": "ws1", "source_provider": "ANTIGRAVITY",
        "source_agent_id": "agent1", "source_invocation_id": "inv1",
        "created_at": now, "updated_at": now,
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
    }
    rows = [
        ("workspace", "ws1", workspace),
        ("user", "u1", {"id": "u1", "email": "owner@example.test", "password_hash": "hash", "password_salt": "salt", "workspace_id": "ws1", "created_at": now, "updated_at": now}),
        ("invite", "invite1", {
            "id": "invite1", "email": "reviewer@example.test", "role": "REVIEWER",
            "token_hash": "b" * 64, "status": "PENDING",
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
            "created_by_user_id": "u1", "workspace_id": "ws1",
            "created_at": now, "updated_at": now,
        }),
        ("project", "p1", project),
        ("agent_connector", "connector1", {
            "id": "connector1", "name": "Antigravity local connector",
            "connector_type": "LOCAL_BRIDGE", "provider_label": "ANTIGRAVITY",
            "auth_type": "BEARER", "status": "ENABLED", "health_status": "UNKNOWN",
            "allowed_project_ids": ["p1"], "allowed_repositories": [],
            "allowed_actions": ["CREATE_AGENT_TASK"], "rate_limit_per_minute": 60,
            "secret_ref": "connector_secret_ref1", "config": {"secret_hash": "a" * 64},
            "created_by_user_id": "u1", "workspace_id": "ws1",
            "created_at": now, "updated_at": now,
        }),
        ("external_agent", "agent1", {
            "id": "agent1", "provider": "ANTIGRAVITY", "display_name": "Test agent",
            "auth_type": "API_KEY", "allowed_project_ids": ["p1"],
            "allowed_actions": ["CREATE_APPROVAL"], "created_by_user_id": "u1",
            "workspace_id": "ws1", "enabled": True, "created_at": now, "updated_at": now,
        }),
        ("external_agent_credential", "credential1", {
            "id": "credential1", "agent_id": "agent1", "created_at": now,
            "updated_at": now,
        }),
        ("approval_request", "a1", approval),
        ("agent_task", "t1", {
            "id": "t1", "provider": "ANTIGRAVITY", "project_id": "p1",
            "title": "Agent task", "goal": "Wait for an approval decision",
            "workspace_id": "ws1", "approval_request_id": "a1", "external_agent_id": "agent1",
            "created_by_user_id": "u1",
            "status": "WAITING_FOR_APPROVAL", "created_at": now, "updated_at": now,
        }),
        ("agent_task_event", "te1", {
            "id": "te1", "task_id": "t1", "event_type": "APPROVAL_CREATED",
            "actor": "system", "message": "Approval request created.",
            "created_at": now,
        }),
        ("audit_event", "ev1", {
            "id": "ev1", "event_type": "approval.created", "actor": "system",
            "entity_id": "a1", "workspace_id": "ws1", "details": {}, "created_at": now,
        }),
        ("audit_event", "ev2", {
            "id": "ev2", "event_type": "agent.registered", "actor": "owner@example.test",
            "entity_id": "agent1", "workspace_id": "ws1", "details": {}, "created_at": now,
        }),
        ("audit_event", "ev3", {
            "id": "ev3", "event_type": "work_order.created", "actor": "owner@example.test",
            "entity_id": "w1", "workspace_id": "ws1", "details": {}, "created_at": now,
        }),
        ("audit_event", "ev4", {
            "id": "ev4", "event_type": "idea.created", "actor": "owner@example.test",
            "entity_id": "i1", "workspace_id": "ws1", "details": {}, "created_at": now,
        }),
        ("audit_event", "ev5", {
            "id": "ev5", "event_type": "connector.secret_revoked", "actor": "owner@example.test",
            "entity_id": "connector1", "workspace_id": "ws1", "details": {}, "created_at": now,
        }),
        ("audit_event", "ev6", {
            "id": "ev6", "event_type": "auth.invite_created", "actor": "owner@example.test",
            "entity_id": "invite1", "workspace_id": "ws1", "details": {}, "created_at": now,
        }),
        ("audit_event", "ev7", {
            "id": "ev7", "event_type": "mcp.child_server.created", "actor": "owner@example.test",
            "entity_id": "mcp1", "workspace_id": "ws1", "details": {}, "created_at": now,
        }),
        ("audit_event", "ev8", {
            "id": "ev8", "event_type": "test_run.created", "actor": "owner@example.test",
            "entity_id": "test1", "workspace_id": "ws1", "details": {}, "created_at": now,
        }),
        ("notification", "n1", {
            "id": "n1", "title": "Approval requested", "body": "Review approval a1",
            "entity_type": "approval_request", "entity_id": "a1", "workspace_id": "ws1",
            "status": "UNREAD", "push_status": "PENDING", "created_at": now,
        }),
        ("notification", "n2", {
            "id": "n2", "title": "Agent task update", "body": "Task completed",
            "entity_type": "agent_task", "entity_id": "t1", "workspace_id": "ws1",
            "user_id": "u1", "status": "UNREAD", "push_status": "PENDING", "created_at": now,
        }),
        ("device_registration", "d1", {
            "id": "d1", "user_id": "u1", "workspace_id": "ws1", "platform": "android",
            "token": "test-device-token", "created_at": now, "updated_at": now,
        }),
        ("idea", "i1", {
            "id": "i1", "project_id": "p1", "title": "Approval flow",
            "raw_text": "Make the approval review durable", "workspace_id": "ws1",
            "created_at": now,
        }),
        ("work_order", "w1", {
            "id": "w1", "project_id": "p1", "idea_id": "i1",
            "goal": "Persist approval flow", "source_agent_id": "agent1",
            "workspace_id": "ws1", "created_at": now,
        }),
        ("mcp_child_server", "mcp1", {
            "id": "mcp1", "project_id": "p1", "name": "Unmigrated MCP server",
            "transport": "test", "endpoint": "local://test", "enabled": True,
            "tools": [], "workspace_id": "ws1", "created_at": now, "updated_at": now,
        }),
        ("mcp_pending_request", "mcp_pending1", {
            "id": "mcp_pending1", "project_id": "p1", "workspace_id": "ws1",
            "approval_request_id": "a1", "server_name": "Local test server",
            "tool_name": "run_tests", "arguments": {"suite": "approval"},
            "requested_by": "mcp-client", "status": "WAITING_FOR_APPROVAL",
            "attempts": 0, "max_attempts": 3, "created_at": now, "updated_at": now,
        }),
        ("test_run", "test1", {
            "id": "test1", "approval_request_id": "a1", "project_id": "p1", "command": "pytest",
            "status": "PASSED", "output_summary": "ok", "created_at": now,
        }),
    ]
    if include_omitted:
        rows.append(("session", "s1", {"id": "s1", "user_id": "u1", "token_hash": "hash", "created_at": now, "expires_at": now}))
    if include_unsupported_notifications:
        rows.append(("notification", "n3", {
            "id": "n3", "title": "Project event", "body": "Unsupported legacy event",
            "entity_type": "unsupported_legacy_type", "entity_id": "p1", "workspace_id": "ws1",
            "status": "UNREAD", "push_status": "PENDING", "created_at": now,
        }))
    if include_legacy_notifications:
        rows.extend([
            ("notification", "project_notice", {
                "id": "project_notice", "title": "Project updated", "body": "Project scope notice",
                "entity_type": "project", "entity_id": "p1", "workspace_id": "ws1",
                "user_id": "u1", "status": "READ", "push_status": "SENT",
                "push_attempts": 1, "created_at": now,
            }),
            ("notification", "device_notice", {
                "id": "device_notice", "title": "Device registered", "body": "Device scope notice",
                "entity_type": "device_registration", "entity_id": "d1", "workspace_id": "ws1",
                "user_id": "u1", "status": "UNREAD", "push_status": "NO_DEVICE",
                "push_attempts": 2, "created_at": now,
            }),
        ])
    if agent_run_faults:
        rows.append(("agent_run", "fault-run", {
            "id": "fault-run", "task_id": "t1", "workspace_id": "ws1",
            "approval_request_id": "a1", "project_id": "p1", "status": "SUCCEEDED",
            "created_at": now, "updated_at": now,
        }))
    for fault in agent_run_faults or []:
        rows.append(("agent_run_fault", fault["id"], fault))
    if include_trust:
        approved = ApprovalRequest.model_validate({
            **approval,
            "status": ApprovalStatus.APPROVED,
            "approved_payload_hash_version": 2,
        })
        approved.approved_payload_hash = compute_approval_payload_hash(approved)
        for index, (kind, entity_id, payload) in enumerate(rows):
            if kind == "approval_request" and entity_id == "a1":
                rows[index] = (kind, entity_id, approved.model_dump(mode="json"))
                break

        run = AgentRun(
            id="run1", task_id="t1", workspace_id="ws1", approval_request_id="a1",
            project_id="p1", status=AgentRunStatus.SUCCEEDED,
        )
        step = AgentRunStep(
            id="step1", run_id=run.id, task_id="t1", sequence=0,
            step_type=AgentRunStepType.VALIDATE_SCOPE,
            idempotency_key="import-step-1",
        )
        run_event = AgentRunEvent(
            id="run-event1", run_id=run.id, task_id="t1", step_id=step.id,
            correlation_id="import-correlation-1", event_type=AgentRunEventType.RUN_CREATED,
        )
        lease = CapabilityLease(
            id="lease1", approval_request_id="a1", workspace_id="ws1",
            approved_payload_hash=approved.approved_payload_hash, mode=CapabilityLeaseMode.BOUNDED,
            scope=CapabilityScope(
                repository="owner/repo", branch="feature/import-test",
                allowed_actions=[CapabilityActionType.RUN_COMMAND], max_runtime_seconds=60,
                max_cost_usd=1.0,
            ),
            issued_by_user_id="u1", status=CapabilityLeaseStatus.CONSUMED,
            expires_at=datetime.fromisoformat(now) + timedelta(minutes=5),
            receipt_nonce="nonce-import", receipt_signature="pending",
            consumed_runtime_seconds=2, consumed_cost_usd=0.2,
        )
        action = ProposedAction(
            id="action1", lease_id=lease.id, run_id=run.id, task_id="t1", project_id="p1",
            workspace_id="ws1", action_type=CapabilityActionType.RUN_COMMAND,
            command="git status",
        )
        request_decision = PolicyDecision(
            id="decision-request", proposed_action_id=action.id, lease_id=lease.id,
            decision=PolicyDecisionType.REQUIRE_APPROVAL, risk_level=RiskLevel.MEDIUM,
        )
        allow_decision = PolicyDecision(
            id="decision-allow", proposed_action_id=action.id, lease_id=lease.id,
            decision=PolicyDecisionType.ALLOW, risk_level=RiskLevel.MEDIUM,
            evaluated_policy_version="trust-policy-v1+human-decision",
        )
        authorization = ActionAuthorization(
            id="authorization1", action_id=action.id, workspace_id="ws1",
            action_payload_hash=action_payload_hash(action),
            policy_decision_id=request_decision.id, reviewer_user_id="u1",
            reviewer_role=UserRole.OWNER, decision=ActionAuthorizationDecision.ALLOW,
            reason="Approved for import", signature="pending",
        )
        authorization.signature = sign_payload(authorization_signature_payload(authorization))
        attestation = ReviewerAttestation(
            id="attestation1", approval_request_id="a1", workspace_id="ws1",
            approval_payload_hash=approved.approved_payload_hash, reviewer_user_id="u1",
            reviewer_role=UserRole.OWNER, decision=ReviewerAttestationDecision.APPROVE,
            reason="Reviewed", signature="pending",
        )
        attestation_material = {
            "approval_request_id": attestation.approval_request_id,
            "approval_payload_hash": attestation.approval_payload_hash,
            "reviewer_user_id": attestation.reviewer_user_id,
            "reviewer_role": attestation.reviewer_role,
            "decision": attestation.decision,
            "reason": attestation.reason,
        }
        attestation.signature = sign_payload(attestation_material)
        grant = CredentialGrant(
            id="grant1", lease_id=lease.id, workspace_id="ws1",
            grant_type=CredentialGrantType.INTERNAL_CAPABILITY_TOKEN,
            audience="agent-runner", subject="agent1",
            expires_at=datetime.fromisoformat(now) + timedelta(minutes=5),
            token_hash="a" * 64,
        )
        consumption = ActionConsumption(actual_runtime_seconds=2, actual_cost_usd=0.2)
        event = TrustEvent(
            id="trust_event1", sequence=1, event_type=TrustEventType.ACTION_CONSUMED,
            entity_type="proposed_action", entity_id=action.id, workspace_id="ws1",
            actor="agent:agent1", payload={"effective_policy_decision_id": allow_decision.id},
            event_hash="pending",
        )
        event.event_hash = sha256_hex({
            "sequence": event.sequence,
            "event_type": event.event_type,
            "entity_type": event.entity_type,
            "entity_id": event.entity_id,
            "actor": event.actor,
            "correlation_id": event.correlation_id,
            "payload": event.payload,
            "previous_hash": event.previous_hash,
            "created_at": event.created_at,
        })
        lease.receipt_signature = sign_payload(lease_receipt_payload(lease))
        rows.extend([
            ("agent_run", run.id, run.model_dump(mode="json")),
            ("agent_run_step", step.id, step.model_dump(mode="json")),
            ("agent_run_event", run_event.id, run_event.model_dump(mode="json")),
            ("capability_lease", lease.id, lease.model_dump(mode="json")),
            ("reviewer_attestation", attestation.id, attestation.model_dump(mode="json")),
            ("proposed_action", action.id, action.model_dump(mode="json")),
            ("policy_decision", request_decision.id, request_decision.model_dump(mode="json")),
            ("policy_decision", allow_decision.id, allow_decision.model_dump(mode="json")),
            ("action_authorization", authorization.id, authorization.model_dump(mode="json")),
            ("action_consumption", action.id, consumption.model_dump(mode="json")),
            ("credential_grant", grant.id, grant.model_dump(mode="json")),
            ("trust_event", event.id, event.model_dump(mode="json")),
        ])
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE kv_store (entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(entity_type,entity_id))")
        conn.execute("CREATE TABLE schema_migrations (migration_id TEXT PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
        conn.execute("INSERT INTO schema_migrations VALUES ('0001_baseline_kv_store','baseline',?)", (now,))
        conn.execute(
            "CREATE TABLE auth_rate_limit_buckets (bucket_hash TEXT NOT NULL, "
            "window_start INTEGER NOT NULL, request_count INTEGER NOT NULL CHECK(request_count >= 0), "
            "PRIMARY KEY(bucket_hash,window_start))"
        )
        conn.execute(
            "CREATE TABLE approval_delivery_claims (approval_id TEXT PRIMARY KEY, "
            "workspace_id TEXT NOT NULL, source_agent_id TEXT NOT NULL, "
            "source_invocation_id TEXT NOT NULL, claimed_at TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE approval_delivery_failures (approval_id TEXT PRIMARY KEY, "
            "workspace_id TEXT NOT NULL, source_agent_id TEXT NOT NULL, "
            "source_invocation_id TEXT NOT NULL, reason_code TEXT NOT NULL, reported_at TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE approval_invocation_leases (approval_id TEXT PRIMARY KEY, "
            "workspace_id TEXT NOT NULL, source_agent_id TEXT NOT NULL, "
            "source_invocation_id TEXT NOT NULL, heartbeat_at TEXT NOT NULL, expires_at TEXT NOT NULL, "
            "UNIQUE(workspace_id,source_agent_id,source_invocation_id))"
        )
        conn.execute(
            "INSERT INTO approval_invocation_leases VALUES (?,?,?,?,?,?)",
            ("a1", "ws1", "agent1", "inv1", "2000-01-01T00:00:00+00:00", "2000-01-01T00:05:00+00:00"),
        )
        conn.execute(
            "CREATE TABLE auth_email_outbox (id TEXT PRIMARY KEY, recipient TEXT NOT NULL, "
            "encrypted_code BLOB NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL, "
            "created_at TEXT NOT NULL, next_attempt_at TEXT NOT NULL, lease_expires_at TEXT, "
            "sent_at TEXT, last_error TEXT)"
        )
        conn.executemany(
            "INSERT INTO auth_email_outbox VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                ("email_pending", "pending@example.test",
                 _encrypt_code(outbox_id="email_pending", recipient="pending@example.test", code="test-code"), "PENDING", 0,
                 now, now, None, None, None),
                ("email_dead", "dead@example.test", b"x" * 35, "DEAD", 3,
                 now, now, None, None, "DELIVERY_RETRIES_EXHAUSTED"),
            ],
        )
        conn.executemany("INSERT INTO kv_store VALUES (?,?,?)", [(t, i, json.dumps(p)) for t, i, p in rows])
        if include_rate_limits:
            conn.execute(
                "INSERT INTO auth_rate_limit_buckets VALUES (?,?,?)",
                ("c" * 64, 1_800_000_000, 3),
            )
        if include_trust:
            conn.executemany(
                "INSERT INTO approval_delivery_claims VALUES (?,?,?,?,?)",
                [("a1", "ws1", "agent1", "inv1", now)],
            )
            conn.executemany(
                "INSERT INTO kv_store VALUES (?,?,?)",
                [
                    ("audit_event", "delivery-claimed", json.dumps({
                        "id": "delivery-claimed", "event_type": "approval.delivery_claimed",
                        "actor": "agent:agent1", "entity_id": "a1", "workspace_id": "ws1",
                        "details": {"source_agent_id": "agent1", "source_invocation_id": "inv1"},
                        "created_at": now,
                    })),
                ],
            )
        if include_delivery_state:
            if not include_trust:
                raise ValueError("delivery-state snapshots require an approved trust fixture")
            conn.executemany(
                "INSERT INTO approval_delivery_failures VALUES (?,?,?,?,?,?)",
                [("a1", "ws1", "agent1", "inv1", "CLAIM_RESPONSE_AMBIGUOUS", now)],
            )
            conn.executemany(
                "INSERT INTO kv_store VALUES (?,?,?)",
                [
                    ("audit_event", "delivery-failed", json.dumps({
                        "id": "delivery-failed", "event_type": "approval.delivery_failed",
                        "actor": "agent:agent1", "entity_id": "a1", "workspace_id": "ws1",
                        "details": {
                            "source_agent_id": "agent1", "source_invocation_id": "inv1",
                            "reason_code": "CLAIM_RESPONSE_AMBIGUOUS",
                        },
                        "created_at": now,
                    })),
                ],
            )
    os.chmod(path, 0o600)


def _set_approval_status(path, status):
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT payload FROM kv_store WHERE entity_type='approval_request' AND entity_id='a1'"
        ).fetchone()
        payload = json.loads(row[0])
        payload["status"] = status
        conn.execute(
            "UPDATE kv_store SET payload=? WHERE entity_type='approval_request' AND entity_id='a1'",
            (json.dumps(payload),),
        )


def _trust_event_payload():
    event = TrustEvent(
        sequence=1,
        event_type=TrustEventType.ACTION_ALLOWED,
        entity_type="approval_request",
        entity_id="a1",
        workspace_id="ws1",
        actor="reviewer@example.test",
        payload={"decision": "ALLOW"},
        event_hash="pending",
    )
    event.event_hash = sha256_hex({
        "sequence": event.sequence,
        "event_type": event.event_type,
        "entity_type": event.entity_type,
        "entity_id": event.entity_id,
        "actor": event.actor,
        "correlation_id": event.correlation_id,
        "payload": event.payload,
        "previous_hash": event.previous_hash,
        "created_at": event.created_at,
    })
    return event.model_dump(mode="json")


@pytest.fixture
def postgres_import_dsn():
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL importer tests")
    schema = f"ct_import_{os.urandom(8).hex()}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    isolated_dsn = psycopg.conninfo.make_conninfo(
        dsn, options=f"-c search_path={schema}"
    )
    try:
        yield isolated_dsn
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_plan_is_read_only_for_supported_snapshot(tmp_path):
    path = tmp_path / "snapshot.sqlite3"
    _snapshot(path)
    before = path.read_bytes()

    plan, selected, digest = build_plan(path)

    assert path.read_bytes() == before
    assert plan["status"] == "DRY_RUN_VALIDATED"
    assert plan["migration_authorized"] is False
    assert plan["snapshot_certified"] is False
    assert plan["source_table_coverage"]["kv_store"] == {
        "disposition": "partial_entity_import",
        "target": "CORE_TYPES -> entity-specific PostgreSQL tables",
    }
    assert plan["source_table_coverage"]["schema_migrations"]["disposition"] == "metadata_only"
    entity_coverage = plan["source_kv_entity_coverage"]
    assert set(entity_coverage) == set(SOURCE_KV_ENTITY_DISPOSITIONS)
    assert entity_coverage["audit_event"]["disposition"] == "partial_import"
    assert entity_coverage["audit_event"]["target"] == "audit_events"
    assert entity_coverage["audit_event"]["reason"]
    for kind, coverage in entity_coverage.items():
        source_count = plan["source_counts"].get(kind, 0)
        imported_count = plan["planned_counts"].get(kind, 0)
        assert coverage["source_count"] == source_count
        assert coverage["imported_count"] == imported_count
        assert coverage["omitted_count"] == source_count - imported_count
    assert plan["partial"] is False
    assert plan["omitted_counts"].get("notification", 0) == 0
    assert plan["planned_counts"]["approval_request"] == 1
    assert plan["planned_counts"]["user"] == 1
    assert plan["planned_counts"]["invite"] == 1
    assert plan["planned_counts"]["session"] == 1
    assert plan["planned_counts"]["external_agent"] == 1
    assert plan["planned_counts"]["approval_invocation_lease"] == 1
    assert plan["planned_counts"]["agent_connector"] == 1
    assert plan["planned_counts"]["mcp_child_server"] == 1
    assert plan["planned_counts"]["mcp_pending_request"] == 1
    assert plan["planned_counts"]["external_agent_credential"] == 1
    assert plan["planned_counts"]["device_registration"] == 1
    assert plan["planned_counts"]["idea"] == 1
    assert plan["planned_counts"]["work_order"] == 1
    assert plan["planned_counts"]["test_run"] == 1
    assert plan["planned_counts"]["agent_task_event"] == 1
    assert plan["planned_counts"]["auth_email_outbox"] == 2
    assert plan["sanitized_terminal_email_ciphertext_count"] == 1
    assert len(digest) == 64
    assert selected["approval_request"]["a1"]["workspace_id"] == "ws1"
    assert plan["planned_counts"]["notification"] == 2
    assert selected["notification"]["n2"]["entity_type"] == "agent_task"
    assert plan["agent_run_fault_injection"] == {
        "status": "CLEAR",
        "inactive_settings": "IMPORTED",
        "active_count": 0,
        "active_id_sha256": [],
        "inactive_import_count": 0,
        "inactive_import_id_sha256": [],
    }
    assert plan["invocation_drain"] == {
        "status": "CLEAR",
        "lease_table_present": True,
        "native_invocation_approval_count": 1,
        "pending_native_invocation_count": 0,
        "approval_id_sha256": [],
        "approved_unconsumed_invocation_count": 0,
        "approved_unconsumed_approval_id_sha256": [],
        "unverifiable_native_invocation_count": 0,
        "unverifiable_approval_id_sha256": [],
    }


def test_invocation_drain_report_is_read_only_hash_bound_and_records_clear_outcome(tmp_path):
    path = tmp_path / "drained-snapshot.sqlite3"
    _snapshot(path)
    before = path.read_bytes()
    captured_at = datetime(2026, 10, 25, 1, 2, 3, tzinfo=timezone.utc)

    report = build_invocation_drain_report(path, generated_at=captured_at)

    assert path.read_bytes() == before
    assert report["status"] == "READY_FOR_OWNER_REVIEW"
    assert report["read_only"] is True
    assert report["migration_authorized"] is False
    assert report["source_snapshot_sha256"] == hashlib.sha256(before).hexdigest()
    assert report["generated_at"] == captured_at.isoformat()
    assert report["native_invocation_status_counts"] == {"DENIED": 1}
    assert report["pending_native_invocation_count"] == 0
    assert report["approved_unconsumed_invocation_count"] == 0
    assert report["unverifiable_native_invocation_count"] == 0
    assert report["unverifiable_approval_id_sha256"] == []


def test_audit_event_inventory_report_is_redacted_read_only_and_owner_only(
    tmp_path, monkeypatch, capsys
):
    path = tmp_path / "audit-source.sqlite3"
    _snapshot(path)
    before = path.read_bytes()
    report_dir = tmp_path / "reports"
    report_dir.mkdir(mode=0o700)
    report_path = report_dir / "audit-event-inventory.json"

    def forbidden_connect(*args, **kwargs):
        raise AssertionError("read-only audit inventory must not connect to PostgreSQL")

    monkeypatch.setattr(psycopg, "connect", forbidden_connect)
    result = main([
        "--source", str(path), "--write-audit-event-inventory-report", str(report_path),
    ])

    assert result == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "AUDIT_EVENT_INVENTORY_WRITTEN" in captured.out
    assert str(report_path) in captured.out
    assert "owner@example.test" not in captured.out
    assert "test-device-token" not in captured.out
    assert "ws1" not in captured.out
    stdout_summary = json.loads(captured.out)
    assert set(stdout_summary) == {
        "status", "report_path", "source_snapshot_sha256",
        "audit_event_count", "classification_counts",
    }
    assert "a1" not in stdout_summary.values()
    assert path.read_bytes() == before
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
    text = report_path.read_text(encoding="utf-8")
    report = json.loads(text)
    assert report["read_only"] is True
    assert report["migration_authorized"] is False
    assert report["audit_event_count"] == len(report["events"])
    assert sum(report["classification_counts"].values()) == report["audit_event_count"]
    assert "owner@example.test" not in text
    assert "test-device-token" not in text
    assert "secret_ref" not in text
    assert '"details"' not in text
    assert '"actor"' not in text
    for event in report["events"]:
        assert set(event) == {
            "event_type", "entity_id", "workspace_id", "parent_entity_type",
            "parent_resolution", "parent_exists", "parent_identity_match",
            "parent_workspace_exists",
            "workspace_match", "classification",
        }


def test_audit_event_inventory_classifies_unscoped_unknown_and_mismatched_rows(tmp_path):
    path = tmp_path / "audit-unclassified.sqlite3"
    _snapshot(path)
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES('audit_event',?,?)",
            [
                (
                    "unknown-unscoped",
                    json.dumps({
                        "id": "unknown-unscoped", "event_type": "future.event",
                        "entity_id": "no-such-parent", "workspace_id": None,
                        "actor": "must-not-leak@example.test", "details": {"token": "redact-me"},
                    }),
                ),
                (
                    "known-mismatch",
                    json.dumps({
                        "id": "known-mismatch", "event_type": "approval.decision",
                        "entity_id": "a1", "workspace_id": "other-workspace",
                        "actor": "must-not-leak@example.test", "details": {"token": "redact-me"},
                    }),
                ),
            ],
        )
    os.chmod(path, 0o600)

    report = build_audit_event_inventory_report(path)
    rows = {row["entity_id"]: row for row in report["events"]}

    assert rows["no-such-parent"]["classification"] == "unscoped_parent_not_found"
    assert rows["no-such-parent"]["parent_resolution"] == "unresolved_parent"
    assert rows["no-such-parent"]["workspace_id"] is None
    assert rows["a1"]["classification"] == "workspace_mismatch"
    assert rows["a1"]["parent_entity_type"] == "approval_request"
    assert rows["a1"]["parent_identity_match"] is True
    assert rows["a1"]["parent_workspace_exists"] is True
    assert rows["a1"]["workspace_match"] is False
    assert "must-not-leak@example.test" not in json.dumps(report)
    assert "redact-me" not in json.dumps(report)


def test_audit_event_inventory_verifies_workspace_parent_identity(tmp_path):
    path = tmp_path / "audit-workspace-parent.sqlite3"
    _snapshot(path)
    event = {
        "id": "audit-workspace-settings-inventory",
        "event_type": "workspace.settings_updated",
        "entity_id": "ws1",
        "workspace_id": "ws1",
        "actor": "owner@example.test",
        "details": {"setting": "approval_expiry"},
    }
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES('audit_event',?,?)",
            (event["id"], json.dumps(event)),
        )
    os.chmod(path, 0o600)

    report = build_audit_event_inventory_report(path)
    row = next(item for item in report["events"] if item["entity_id"] == "ws1")

    assert row["parent_entity_type"] == "workspace"
    assert row["parent_resolution"] == "entity_id_fallback"
    assert row["parent_exists"] is True
    assert row["parent_identity_match"] is True
    assert row["parent_workspace_exists"] is True
    assert row["workspace_match"] is True
    assert row["classification"] == "same_workspace_parent_via_entity_id_fallback"


def test_audit_event_inventory_rejects_source_mutation_during_preflight(tmp_path, monkeypatch):
    import scripts.import_approval_slice_to_postgres as importer

    path = tmp_path / "audit-mutated-during-preflight.sqlite3"
    _snapshot(path)
    os.chmod(path, 0o600)
    original_count = importer._source_kv_entity_type_counts

    def mutate_after_count(source_path):
        counts = original_count(source_path)
        with sqlite3.connect(source_path) as conn:
            conn.execute(
                "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
                ("audit_event", "added-during-preflight", json.dumps({
                    "id": "added-during-preflight", "event_type": "future.event",
                    "entity_id": "unknown", "workspace_id": None,
                })),
            )
        return counts

    monkeypatch.setattr(importer, "_source_kv_entity_type_counts", mutate_after_count)

    with pytest.raises(ImportBlocked, match="source snapshot changed during audit inventory"):
        build_audit_event_inventory_report(path)


def test_invocation_drain_report_persists_blocked_pending_state_without_source_mutation(
    tmp_path, capsys
):
    path = tmp_path / "pending-snapshot.sqlite3"
    _snapshot(path)
    _set_approval_status(path, "PENDING_REVIEW")
    before = path.read_bytes()
    report_path = tmp_path / "reports" / "drain-20261025.json"
    report_path.parent.mkdir(mode=0o700)

    result = main([
        "--source", str(path),
        "--write-invocation-drain-report", str(report_path),
    ])

    assert result == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "BLOCKED"
    assert path.read_bytes() == before
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
    report = json.loads(report_path.read_text())
    assert report["status"] == "BLOCKED"
    assert report["pending_native_invocation_count"] == 1
    assert report["pending_approval_id_sha256"] == [hashlib.sha256(b"a1").hexdigest()]
    assert report["unverifiable_native_invocation_count"] == 1
    assert report["unverifiable_approval_id_sha256"] == [hashlib.sha256(b"a1").hexdigest()]
    assert "Review" not in report_path.read_text()
    assert "agent1" not in report_path.read_text()


def test_invocation_drain_report_blocks_native_request_with_missing_invocation_identity(
    tmp_path,
):
    path = tmp_path / "unverifiable-snapshot.sqlite3"
    _snapshot(path)
    _set_approval_status(path, "REQUESTED")
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT payload FROM kv_store WHERE entity_type='approval_request' AND entity_id='a1'"
        ).fetchone()
        approval = json.loads(row[0])
        approval["source_invocation_id"] = None
        conn.execute(
            "UPDATE kv_store SET payload=? WHERE entity_type='approval_request' AND entity_id='a1'",
            (json.dumps(approval),),
        )
    os.chmod(path, 0o600)

    report = build_invocation_drain_report(path)

    assert report["status"] == "BLOCKED"
    assert report["pending_native_invocation_count"] == 1
    assert report["unverifiable_native_invocation_count"] == 1
    assert report["unverifiable_approval_id_sha256"] == [hashlib.sha256(b"a1").hexdigest()]
    assert "inv1" not in json.dumps(report)


@pytest.mark.parametrize("lease_state", ["missing", "expired", "mismatched"])
def test_invocation_drain_report_blocks_unverifiable_live_invocation_lease(
    tmp_path, lease_state: str
):
    path = tmp_path / f"unverifiable-lease-{lease_state}.sqlite3"
    _snapshot(path)
    _set_approval_status(path, "PENDING_REVIEW")
    with sqlite3.connect(path) as conn:
        if lease_state == "missing":
            conn.execute("DELETE FROM approval_invocation_leases WHERE approval_id='a1'")
        elif lease_state == "expired":
            expired = datetime.now(timezone.utc) - timedelta(minutes=5)
            conn.execute(
                "UPDATE approval_invocation_leases SET heartbeat_at=?,expires_at=? WHERE approval_id='a1'",
                ((expired - timedelta(seconds=1)).isoformat(), expired.isoformat()),
            )
        else:
            conn.execute(
                "UPDATE approval_invocation_leases SET source_invocation_id='other-invocation' "
                "WHERE approval_id='a1'"
            )
    os.chmod(path, 0o600)

    report = build_invocation_drain_report(path)

    assert report["status"] == "BLOCKED"
    assert report["pending_native_invocation_count"] == 1
    assert report["unverifiable_native_invocation_count"] == 1
    assert report["unverifiable_approval_id_sha256"] == [hashlib.sha256(b"a1").hexdigest()]


def test_plan_blocks_unverifiable_native_pending_identity_before_import(tmp_path):
    path = tmp_path / "pending-native-missing-identity.sqlite3"
    _snapshot(path)
    _set_approval_status(path, "REQUESTED")
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT payload FROM kv_store WHERE entity_type='approval_request' AND entity_id='a1'"
        ).fetchone()
        payload = json.loads(row[0])
        payload["source_agent_id"] = None
        payload["source_invocation_id"] = None
        conn.execute(
            "UPDATE kv_store SET payload=? WHERE entity_type='approval_request' AND entity_id='a1'",
            (json.dumps(payload),),
        )
    os.chmod(path, 0o600)

    with pytest.raises(ImportBlocked, match="1 unverifiable native invocation") as exc_info:
        build_plan(path)

    assert hashlib.sha256(b"a1").hexdigest() in str(exc_info.value)
    assert "agent1" not in str(exc_info.value)
    assert "inv1" not in str(exc_info.value)


def test_plan_refuses_unknown_sqlite_table_before_postgres_connection(tmp_path, monkeypatch):
    path = tmp_path / "snapshot_with_unknown_table.sqlite3"
    _snapshot(path)
    unknown_table = "private_extension_state"
    secret_marker = "DO_NOT_PRINT_EXTENSION_STATE"
    with sqlite3.connect(path) as conn:
        conn.execute(f'CREATE TABLE "{unknown_table}" (payload TEXT NOT NULL)')
        conn.execute(f'INSERT INTO "{unknown_table}" VALUES (?)', (secret_marker,))

    def forbidden_target_connection(*_args, **_kwargs):
        pytest.fail("unknown source table must block planning before PostgreSQL access")

    monkeypatch.setattr(
        "scripts.import_approval_slice_to_postgres.psycopg.connect",
        forbidden_target_connection,
    )
    with pytest.raises(ImportBlocked, match="without an importer disposition") as exc_info:
        build_plan(path)

    assert unknown_table not in str(exc_info.value)
    assert secret_marker not in str(exc_info.value)


@pytest.mark.parametrize("unsupported_type", ["unsupported_legacy_type"])
def test_plan_blocks_unsupported_notification_before_target_access(
    tmp_path, monkeypatch, unsupported_type,
):
    path = tmp_path / "unsupported_notification.sqlite3"
    _snapshot(path, include_unsupported_notifications=True)
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT payload FROM kv_store WHERE entity_type='notification' AND entity_id='n3'"
        ).fetchone()
        payload = json.loads(row[0])
        payload.update(
            {
                "entity_type": unsupported_type,
                "title": "PRIVATE_NOTIFICATION_TITLE",
                "body": "PRIVATE_NOTIFICATION_BODY",
            }
        )
        conn.execute(
            "UPDATE kv_store SET payload=? WHERE entity_type='notification' AND entity_id='n3'",
            (json.dumps(payload),),
        )

    def unexpected_target_connection(*_args, **_kwargs):
        pytest.fail("unsupported notification barrier must run before opening PostgreSQL")

    monkeypatch.setattr(
        "scripts.import_approval_slice_to_postgres.psycopg.connect",
        unexpected_target_connection,
    )
    with pytest.raises(ImportBlocked, match="unsupported notification records block PostgreSQL import") as exc_info:
        build_plan(path)

    reason = str(exc_info.value)
    assert "count=1" in reason
    assert hashlib.sha256(b"n3").hexdigest() in reason
    assert "n3" not in reason
    assert unsupported_type not in reason
    assert "PRIVATE_NOTIFICATION_TITLE" not in reason
    assert "PRIVATE_NOTIFICATION_BODY" not in reason


def test_plan_rejects_payload_identity_that_differs_from_sqlite_key(tmp_path):
    path = tmp_path / "mismatched_identity.sqlite3"
    _snapshot(path)
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT payload FROM kv_store WHERE entity_type='approval_request' AND entity_id='a1'"
        ).fetchone()
        payload = json.loads(row[0])
        payload["id"] = "different-approval-id"
        conn.execute(
            "UPDATE kv_store SET payload=? WHERE entity_type='approval_request' AND entity_id='a1'",
            (json.dumps(payload),),
        )

    with pytest.raises(
        ImportBlocked,
        match="approval_request/a1 payload identity does not match its SQLite key",
    ):
        build_plan(path)


def test_plan_blocks_approved_unconsumed_native_invocation_with_live_lease(tmp_path, monkeypatch):
    path = tmp_path / "live_approved_native_snapshot.sqlite3"
    _snapshot(path)
    _set_approval_status(path, "APPROVED")
    heartbeat = datetime.now(timezone.utc)
    expiry = heartbeat + timedelta(minutes=5)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE approval_invocation_leases SET heartbeat_at=?,expires_at=? WHERE approval_id='a1'",
            (heartbeat.isoformat(), expiry.isoformat()),
        )

    def unexpected_target_connection(*_args, **_kwargs):
        pytest.fail("live-invocation drain barrier must run before opening a PostgreSQL target")

    monkeypatch.setattr(
        "scripts.import_approval_slice_to_postgres.psycopg.connect",
        unexpected_target_connection,
    )
    with pytest.raises(ImportBlocked, match="1 approved but unconsumed native invocation") as exc_info:
        build_plan(path)
    assert hashlib.sha256(b"a1").hexdigest() in str(exc_info.value)


@pytest.mark.parametrize("lease_state", ["missing_row", "expired"])
def test_plan_blocks_approved_unconsumed_invocation_without_valid_lease(tmp_path, lease_state):
    path = tmp_path / f"approved_unconsumed_{lease_state}.sqlite3"
    _snapshot(path)
    _set_approval_status(path, "APPROVED")
    with sqlite3.connect(path) as conn:
        if lease_state == "missing_row":
            conn.execute("DELETE FROM approval_invocation_leases WHERE approval_id='a1'")
        else:
            expired = datetime.now(timezone.utc) - timedelta(minutes=10)
            expired_lease = expired + timedelta(minutes=1)
            conn.execute(
                "UPDATE approval_invocation_leases SET heartbeat_at=?,expires_at=? WHERE approval_id='a1'",
                (expired.isoformat(), expired_lease.isoformat()),
            )
    with pytest.raises(ImportBlocked, match="approved but unconsumed native invocation"):
        build_plan(path)


def test_plan_blocks_approved_unconsumed_invocation_when_lease_schema_is_missing(tmp_path):
    path = tmp_path / "missing_invocation_lease_schema.sqlite3"
    _snapshot(path)
    _set_approval_status(path, "APPROVED")
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE approval_invocation_leases")
    with pytest.raises(ImportBlocked, match="approved but unconsumed native invocation"):
        build_plan(path)


def test_plan_rejects_invocation_lease_scope_and_time_mismatch(tmp_path):
    path = tmp_path / "invalid_invocation_lease_snapshot.sqlite3"
    _snapshot(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE approval_invocation_leases SET source_invocation_id='wrong-invocation' "
            "WHERE approval_id='a1'"
        )
    with pytest.raises(ImportBlocked, match="does not match its approval and agent scope"):
        build_plan(path)

    invalid_time_path = tmp_path / "invalid_invocation_lease_time.sqlite3"
    _snapshot(invalid_time_path)
    with sqlite3.connect(invalid_time_path) as conn:
        conn.execute(
            "UPDATE approval_invocation_leases SET expires_at='1999-01-01T00:00:00+00:00' "
            "WHERE approval_id='a1'"
        )
    with pytest.raises(ImportBlocked, match="invalid lease time bounds"):
        build_plan(invalid_time_path)


@pytest.mark.parametrize("status", ["REQUESTED", "PENDING_REVIEW"])
def test_plan_blocks_pending_native_invocation_approvals_with_safe_summary(
    tmp_path,
    monkeypatch,
    status,
):
    path = tmp_path / "pending_native_snapshot.sqlite3"
    _snapshot(path)
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT payload FROM kv_store WHERE entity_type='approval_request' AND entity_id='a1'"
        ).fetchone()
        payload = json.loads(row[0])
        payload.update(
            {
                "status": status,
                "title": "SECRET_APPROVAL_TITLE",
                "summary": "SECRET_ACTION_PAYLOAD",
                "source_invocation_id": "SECRET_INVOCATION_ID",
            }
        )
        conn.execute(
            "UPDATE kv_store SET payload=? WHERE entity_type='approval_request' AND entity_id='a1'",
            (json.dumps(payload),),
        )

    def unexpected_target_connection(*_args, **_kwargs):
        pytest.fail("drain barrier must run before opening a PostgreSQL target")

    monkeypatch.setattr(
        "scripts.import_approval_slice_to_postgres.psycopg.connect",
        unexpected_target_connection,
    )

    with pytest.raises(ImportBlocked, match="1 pending native invocation approval") as exc_info:
        build_plan(path)

    reason = str(exc_info.value)
    assert hashlib.sha256(b"a1").hexdigest() in reason
    assert "SECRET_APPROVAL_TITLE" not in reason
    assert "SECRET_ACTION_PAYLOAD" not in reason
    assert "SECRET_INVOCATION_ID" not in reason
    assert "a1" not in reason


def test_delivery_state_is_included_and_requires_matching_invocation_audit(tmp_path):
    path = tmp_path / "snapshot.sqlite3"
    _snapshot(path, include_omitted=False, include_trust=True, include_delivery_state=True)

    plan, selected, digest = build_plan(path)

    assert plan["scope"] == "approval_auth_agent_mcp_test_runs_connectors_email_task_notifications_delivery_rate_limits_invocation_leases_faults_v13"
    assert plan["planned_counts"]["approval_delivery_claim"] == 1
    assert plan["planned_counts"]["approval_delivery_failure"] == 1
    assert plan["planned_counts"]["approval_invocation_lease"] == 1
    assert selected["approval_delivery_claim"]["a1"]["source_invocation_id"] == "inv1"
    assert selected["approval_delivery_failure"]["a1"]["reason_code"] == "CLAIM_RESPONSE_AMBIGUOUS"
    assert len(digest) == 64


def test_delivery_state_import_preserves_at_most_once_claim_and_failure(tmp_path, postgres_import_dsn):
    path = tmp_path / "snapshot.sqlite3"
    _snapshot(path, include_omitted=False, include_trust=True, include_delivery_state=True)
    plan, selected, digest = build_plan(path)

    apply_import(postgres_import_dsn, digest, plan, selected)
    apply_import(postgres_import_dsn, digest, plan, selected)

    with psycopg.connect(postgres_import_dsn) as conn:
        claim = conn.execute(
            "SELECT workspace_id,source_agent_id,source_invocation_id "
            "FROM approval_delivery_claims WHERE approval_id='a1'"
        ).fetchone()
        failure = conn.execute(
            "SELECT workspace_id,source_agent_id,source_invocation_id,reason_code "
            "FROM approval_delivery_failures WHERE approval_id='a1'"
        ).fetchone()
        assert claim == ("ws1", "agent1", "inv1")
        assert failure == ("ws1", "agent1", "inv1", "CLAIM_RESPONSE_AMBIGUOUS")
        lease = conn.execute(
            "SELECT workspace_id,source_agent_id,source_invocation_id,heartbeat_at,expires_at "
            "FROM approval_invocation_leases WHERE approval_id='a1'"
        ).fetchone()
        assert lease[:3] == ("ws1", "agent1", "inv1")
        assert lease[3] == datetime(2000, 1, 1, tzinfo=timezone.utc)
        assert lease[4] == datetime(2000, 1, 1, 0, 5, tzinfo=timezone.utc)


def test_plan_blocks_delivery_claim_with_mismatched_source_invocation(tmp_path):
    path = tmp_path / "snapshot.sqlite3"
    _snapshot(path, include_omitted=False, include_trust=True, include_delivery_state=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE approval_delivery_claims SET source_invocation_id='other-invocation' "
            "WHERE approval_id='a1'"
        )

    with pytest.raises(ImportBlocked, match="does not match its approved invocation"):
        build_plan(path)


def test_auth_rate_limit_buckets_are_preserved_and_malformed_rows_block(tmp_path, postgres_import_dsn):
    path = tmp_path / "snapshot.sqlite3"
    _snapshot(path, include_rate_limits=True)
    plan, selected, digest = build_plan(path)

    assert plan["planned_counts"]["auth_rate_limit_bucket"] == 1
    assert selected["auth_rate_limit_bucket"]["c" * 64 + ":1800000000"]["request_count"] == 3
    apply_import(postgres_import_dsn, digest, plan, selected)
    apply_import(postgres_import_dsn, digest, plan, selected)
    with psycopg.connect(postgres_import_dsn) as conn:
        assert conn.execute(
            "SELECT bucket_hash,window_start,request_count FROM auth_rate_limit_buckets"
        ).fetchone() == ("c" * 64, 1_800_000_000, 3)

    invalid_path = tmp_path / "invalid-snapshot.sqlite3"
    _snapshot(invalid_path)
    with sqlite3.connect(invalid_path) as conn:
        conn.execute("INSERT INTO auth_rate_limit_buckets VALUES (?,?,?)", ("NOT-A-DIGEST", 10, 1))
    with pytest.raises(ImportBlocked, match="invalid bucket digest"):
        build_plan(invalid_path)


def test_plan_refuses_task_notification_with_missing_parent(tmp_path):
    path = tmp_path / "snapshot.sqlite3"
    _snapshot(path)
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT payload FROM kv_store WHERE entity_type='notification' AND entity_id='n2'"
        ).fetchone()
        payload = json.loads(row[0])
        payload["entity_id"] = "missing-task"
        conn.execute(
            "UPDATE kv_store SET payload=? WHERE entity_type='notification' AND entity_id='n2'",
            (json.dumps(payload),),
        )

    with pytest.raises(ImportBlocked, match="notification/n2 references an unimported AgentTask"):
        build_plan(path)


def test_plan_rejects_group_readable_source(tmp_path):
    path = tmp_path / "snapshot.sqlite3"
    _snapshot(path)
    os.chmod(path, 0o640)

    with pytest.raises(ImportBlocked, match="owner-only"):
        build_plan(path)


def test_plan_rejects_wal_sidecar(tmp_path):
    path = tmp_path / "snapshot.sqlite3"
    _snapshot(path)
    (tmp_path / "snapshot.sqlite3-wal").write_bytes(b"not a valid snapshot")

    with pytest.raises(ImportBlocked, match="sidecar"):
        build_plan(path)


def test_plan_rejects_malformed_active_email_ciphertext(tmp_path):
    path = tmp_path / "snapshot.sqlite3"
    _snapshot(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE auth_email_outbox SET encrypted_code=? WHERE id='email_pending'",
            (b"too-short",),
        )

    with pytest.raises(ImportBlocked, match="malformed active ciphertext"):
        build_plan(path)


def test_plan_refuses_approval_without_explicit_workspace(tmp_path):
    path = tmp_path / "snapshot.sqlite3"
    _snapshot(path, include_omitted=False)
    with sqlite3.connect(path) as conn:
        row = conn.execute("SELECT payload FROM kv_store WHERE entity_type='approval_request'").fetchone()
        payload = json.loads(row[0])
        payload.pop("workspace_id")
        conn.execute("UPDATE kv_store SET payload=? WHERE entity_type='approval_request'", (json.dumps(payload),))

    with pytest.raises(ImportBlocked, match="explicit workspace_id"):
        build_plan(path)


def test_plan_refuses_agent_task_event_with_missing_parent(tmp_path):
    path = tmp_path / "snapshot.sqlite3"
    _snapshot(path, include_omitted=False)
    with sqlite3.connect(path) as conn:
        payload = json.loads(conn.execute(
            "SELECT payload FROM kv_store WHERE entity_type='agent_task_event'"
        ).fetchone()[0])
        payload["task_id"] = "missing-task"
        conn.execute(
            "UPDATE kv_store SET payload=? WHERE entity_type='agent_task_event'",
            (json.dumps(payload),),
        )

    with pytest.raises(ImportBlocked, match="structural audit did not pass"):
        build_plan(path)


def test_plan_refuses_agent_run_event_with_mismatched_step(tmp_path):
    path = tmp_path / "snapshot.sqlite3"
    _snapshot(path, include_omitted=False, include_trust=True)
    with sqlite3.connect(path) as conn:
        payload = json.loads(conn.execute(
            "SELECT payload FROM kv_store WHERE entity_type='agent_run_event'"
        ).fetchone()[0])
        payload["step_id"] = "missing-step"
        conn.execute(
            "UPDATE kv_store SET payload=? WHERE entity_type='agent_run_event'",
            (json.dumps(payload),),
        )

    with pytest.raises(ImportBlocked, match="agent_run_event.*missing or mismatched step"):
        build_plan(path)


def test_plan_refuses_mcp_pending_request_with_inconsistent_lease(tmp_path):
    path = tmp_path / "snapshot.sqlite3"
    _snapshot(path, include_omitted=False)
    with sqlite3.connect(path) as conn:
        payload = json.loads(conn.execute(
            "SELECT payload FROM kv_store WHERE entity_type='mcp_pending_request'"
        ).fetchone()[0])
        payload["lease_owner"] = "worker-1"
        conn.execute(
            "UPDATE kv_store SET payload=? WHERE entity_type='mcp_pending_request'",
            (json.dumps(payload),),
        )

    with pytest.raises(ImportBlocked, match="inconsistent forwarding lease state"):
        build_plan(path)


def test_plan_blocks_tampered_legacy_trust_history(tmp_path):
    path = tmp_path / "tampered_trust.sqlite3"
    _snapshot(path, include_omitted=False)
    event = _trust_event_payload()
    event["payload"]["decision"] = "BLOCK"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO kv_store VALUES (?,?,?)",
            ("trust_event", event["id"], json.dumps(event)),
        )

    with pytest.raises(ImportBlocked, match="structural audit did not pass"):
        build_plan(path)


def test_plan_reports_workspace_scope_derived_from_exact_project(tmp_path):
    path = tmp_path / "snapshot.sqlite3"
    _snapshot(path)
    with sqlite3.connect(path) as conn:
        for entity_type in ("idea", "work_order"):
            raw = conn.execute(
                "SELECT payload FROM kv_store WHERE entity_type=?", (entity_type,)
            ).fetchone()[0]
            payload = json.loads(raw)
            payload.pop("workspace_id")
            conn.execute(
                "UPDATE kv_store SET payload=? WHERE entity_type=?",
                (json.dumps(payload), entity_type),
            )

    plan, selected, _ = build_plan(path)

    assert plan["derived_workspace_scope_counts"] == {"idea": 1, "work_order": 1}
    assert selected["idea"]["i1"]["workspace_id"] == "ws1"
    assert selected["work_order"]["w1"]["workspace_id"] == "ws1"


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        ("host=/tmp/pg-socket dbname=aixion_test", False),
        ("host=localhost dbname=aixion_test", False),
        ("host=10.0.0.8 dbname=aixion_staging", True),
        ("hostaddr=10.0.0.8 dbname=aixion_staging", True),
        ("host=localhost,10.0.0.8 dbname=aixion_staging", True),
    ],
)
def test_target_remote_detection_includes_hostaddr_and_multi_host(dsn, expected):
    assert _target_is_remote(dsn) is expected


def test_apply_import_and_retry_are_transactional_and_idempotent(tmp_path, postgres_import_dsn):
    path = tmp_path / "snapshot.sqlite3"
    _snapshot(path)
    retry_at = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
    with sqlite3.connect(path) as source:
        for notification_id, changes in (
            ("n1", {"user_id": "u1", "status": "READ", "push_status": "SENT", "push_attempts": 2}),
            (
                "n2",
                {
                    "status": "UNREAD",
                    "push_status": "RETRY_WAIT",
                    "push_attempts": 4,
                    "push_next_attempt_at": retry_at,
                },
            ),
        ):
            row = source.execute(
                "SELECT payload FROM kv_store WHERE entity_type='notification' AND entity_id=?",
                (notification_id,),
            ).fetchone()
            payload = json.loads(row[0])
            payload.update(changes)
            source.execute(
                "UPDATE kv_store SET payload=? WHERE entity_type='notification' AND entity_id=?",
                (json.dumps(payload), notification_id),
            )
    plan, selected, digest = build_plan(path)

    apply_import(postgres_import_dsn, digest, plan, selected)
    apply_import(postgres_import_dsn, digest, plan, selected)

    with psycopg.connect(postgres_import_dsn) as conn:
        assert conn.execute("SELECT count(*) FROM workspaces").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM invites").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM auth_email_outbox").fetchone()[0] == 2
        pending_ciphertext = conn.execute(
            "SELECT encrypted_code FROM auth_email_outbox WHERE outbox_id='email_pending'"
        ).fetchone()[0]
        terminal_ciphertext = conn.execute(
            "SELECT encrypted_code FROM auth_email_outbox WHERE outbox_id='email_dead'"
        ).fetchone()[0]
        assert bytes(pending_ciphertext) == selected["auth_email_outbox"]["email_pending"]["encrypted_code"]
        assert bytes(terminal_ciphertext) == b""
        assert _decrypt_code(
            outbox_id="email_pending",
            recipient="pending@example.test",
            encrypted=bytes(pending_ciphertext),
        ) == "test-code"
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM agent_connectors").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM mcp_child_servers").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM mcp_pending_requests").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM test_runs").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM external_agents").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM external_agent_credentials").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM device_registrations").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM projects").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM approval_requests").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM agent_tasks").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM agent_task_events").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM ideas").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM work_orders").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM audit_events").fetchone()[0] == 8
        assert conn.execute("SELECT count(*) FROM notifications").fetchone()[0] == 2
        assert conn.execute(
            "SELECT task_id FROM notifications WHERE notification_id='n2'"
        ).fetchone()[0] == "t1"
        imported = conn.execute(
            "SELECT notification_id,workspace_id,user_id,approval_id,task_id,entity_type,status,"
            "attempts,next_attempt_at,payload FROM notifications ORDER BY notification_id"
        ).fetchall()
        approval_notification, task_notification = imported
        assert approval_notification[:8] == (
            "n1", "ws1", "u1", "a1", None, "approval_request", "SENT", 2,
        )
        assert approval_notification[9]["status"] == "READ"
        assert approval_notification[9]["push_status"] == "SENT"
        assert task_notification[:8] == (
            "n2", "ws1", "u1", None, "t1", "agent_task", "RETRY_WAIT", 4,
        )
        assert task_notification[8] == datetime.fromisoformat(retry_at)
        assert task_notification[9]["status"] == "UNREAD"
        assert task_notification[9]["push_status"] == "RETRY_WAIT"
        assert task_notification[9]["push_attempts"] == 4
        assert conn.execute("SELECT count(*) FROM sqlite_import_runs").fetchone()[0] == 1


def test_import_preserves_project_and_device_notifications_idempotently_and_scoped(
    tmp_path, postgres_import_dsn,
):
    path = tmp_path / "snapshot_with_legacy_notifications.sqlite3"
    _snapshot(path, include_legacy_notifications=True)
    plan, selected, digest = build_plan(path)
    assert plan["planned_counts"]["notification"] == 4
    assert selected["notification"]["project_notice"]["entity_type"] == "project"
    assert selected["notification"]["device_notice"]["entity_type"] == "device_registration"

    apply_import(postgres_import_dsn, digest, plan, selected)
    apply_import(postgres_import_dsn, digest, plan, selected)

    with psycopg.connect(postgres_import_dsn) as conn:
        rows = conn.execute(
            "SELECT notification_id,workspace_id,user_id,entity_type,approval_id,task_id,"
            "project_id,device_id,status,attempts,payload FROM notifications "
            "WHERE notification_id IN ('project_notice','device_notice') ORDER BY notification_id"
        ).fetchall()
        assert len(rows) == 2
        device_notice, project_notice = rows
        assert device_notice[:10] == (
            "device_notice", "ws1", "u1", "device_registration", None, None,
            None, "d1", "NO_DEVICE", 2,
        )
        assert device_notice[10]["entity_id"] == "d1"
        assert project_notice[:10] == (
            "project_notice", "ws1", "u1", "project", None, None,
            "p1", None, "SENT", 1,
        )
        assert project_notice[10]["entity_id"] == "p1"
        assert conn.execute(
            "SELECT count(*) FROM notifications WHERE notification_id IN ('project_notice','device_notice')"
        ).fetchone()[0] == 2


@pytest.mark.parametrize(
    ("entity_type", "entity_id", "user_id", "expected_error"),
    [
        ("project", "missing_project", "u1", "references an unimported project"),
        ("device_registration", "d1", "u2", "does not own its device registration"),
    ],
)
def test_plan_rejects_unscoped_project_or_device_notification(
    tmp_path, entity_type, entity_id, user_id, expected_error,
):
    path = tmp_path / f"bad_{entity_type}_notification.sqlite3"
    _snapshot(path, include_legacy_notifications=True)
    with sqlite3.connect(path) as conn:
        if entity_type == "device_registration":
            user = json.loads(conn.execute(
                "SELECT payload FROM kv_store WHERE entity_type='user' AND entity_id='u1'"
            ).fetchone()[0])
            user.update(id="u2", email="reviewer@example.test")
            conn.execute(
                "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES('user','u2',?)",
                (json.dumps(user),),
            )
        payload = json.loads(conn.execute(
            "SELECT payload FROM kv_store WHERE entity_type='notification' AND entity_id='project_notice'"
        ).fetchone()[0])
        payload.update(entity_type=entity_type, entity_id=entity_id, user_id=user_id)
        conn.execute(
            "UPDATE kv_store SET payload=? WHERE entity_type='notification' AND entity_id='project_notice'",
            (json.dumps(payload),),
        )
    with pytest.raises(ImportBlocked, match=expected_error):
        build_plan(path)


def test_import_preserves_workspace_scoped_agent_task_and_run_audits(tmp_path, postgres_import_dsn):
    path = tmp_path / "snapshot_with_run_audits.sqlite3"
    _snapshot(path)
    now = datetime.now(timezone.utc).isoformat()
    run = AgentRun(
        id="run-audit-test", task_id="t1", workspace_id="ws1", project_id="p1",
        status=AgentRunStatus.SUCCEEDED,
    )
    events = [
        {
            "id": "audit-task-test", "event_type": "agent_task.created",
            "actor": "owner@example.test", "entity_id": "t1", "workspace_id": "ws1",
            "details": {"source": "legacy", "decision": "preserve"}, "created_at": now,
        },
        {
            "id": "audit-run-test", "event_type": "agent_run.created",
            "actor": "owner@example.test", "entity_id": run.id, "workspace_id": "ws1",
            "details": {"source": "legacy", "decision": "preserve"}, "created_at": now,
        },
    ]
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            [
                ("agent_run", run.id, json.dumps(run.model_dump(mode="json"))),
                *[("audit_event", event["id"], json.dumps(event)) for event in events],
            ],
        )
    os.chmod(path, 0o600)

    plan, selected, digest = build_plan(path)

    assert plan["planned_counts"]["audit_event"] == 10
    assert selected["audit_event"]["audit-task-test"]["_import_entity_type"] == "agent_task"
    assert selected["audit_event"]["audit-run-test"]["_import_entity_type"] == "agent_run"
    apply_import(postgres_import_dsn, digest, plan, selected)
    apply_import(postgres_import_dsn, digest, plan, selected)

    with psycopg.connect(postgres_import_dsn) as conn:
        persisted = conn.execute(
            "SELECT event_id,workspace_id,entity_type,entity_id,event_type,details,payload "
            "FROM audit_events WHERE event_id IN ('audit-task-test','audit-run-test') "
            "ORDER BY event_id"
        ).fetchall()
    assert [(row[0], row[1], row[2], row[3], row[4]) for row in persisted] == [
        ("audit-run-test", "ws1", "agent_run", "run-audit-test", "agent_run.created"),
        ("audit-task-test", "ws1", "agent_task", "t1", "agent_task.created"),
    ]
    for row in persisted:
        assert row[5] == {"source": "legacy", "decision": "preserve"}
        assert row[6]["details"] == row[5]


def test_import_preserves_user_and_session_security_audits(tmp_path, postgres_import_dsn):
    path = tmp_path / "snapshot_with_auth_audits.sqlite3"
    _snapshot(path)
    now = datetime.now(timezone.utc).isoformat()
    events = [
        {
            "id": "audit-user-role", "event_type": "auth.role_updated",
            "actor": "owner@example.test", "entity_id": "u1", "workspace_id": "ws1",
            "details": {"target_user_id": "u1", "new_role": "REVIEWER"}, "created_at": now,
        },
        {
            "id": "audit-user-deletion", "event_type": "auth.account_deletion_requested",
            "actor": "owner@example.test", "entity_id": "u1", "workspace_id": "ws1",
            "details": {"target_user_id": "u1", "status": "RECEIVED"}, "created_at": now,
        },
        {
            "id": "audit-user-sessions", "event_type": "auth.sessions_revoked",
            "actor": "owner@example.test", "entity_id": "u1", "workspace_id": "ws1",
            "details": {"target_user_id": "u1", "revoked_sessions_count": 1}, "created_at": now,
        },
        {
            "id": "audit-session-logout", "event_type": "auth.session_revoked",
            "actor": "owner@example.test", "entity_id": "s1", "workspace_id": "ws1",
            "details": {"session_id": "s1", "reason": "user_logout"}, "created_at": now,
        },
    ]
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            [("audit_event", event["id"], json.dumps(event)) for event in events],
        )
    os.chmod(path, 0o600)

    plan, selected, digest = build_plan(path)

    assert plan["planned_counts"]["audit_event"] == 12
    assert selected["audit_event"]["audit-user-role"]["_import_entity_type"] == "user"
    assert selected["audit_event"]["audit-user-deletion"]["_import_entity_type"] == "user"
    assert selected["audit_event"]["audit-user-sessions"]["_import_entity_type"] == "user"
    assert selected["audit_event"]["audit-session-logout"]["_import_entity_type"] == "auth_session"

    apply_import(postgres_import_dsn, digest, plan, selected)

    with psycopg.connect(postgres_import_dsn) as conn:
        persisted = conn.execute(
            "SELECT event_id,workspace_id,entity_type,entity_id,event_type "
            "FROM audit_events WHERE event_id LIKE 'audit-user-%' OR event_id='audit-session-logout' "
            "ORDER BY event_id"
        ).fetchall()
    assert [(row[0], row[1], row[2], row[3], row[4]) for row in persisted] == [
        ("audit-session-logout", "ws1", "auth_session", "s1", "auth.session_revoked"),
        ("audit-user-deletion", "ws1", "user", "u1", "auth.account_deletion_requested"),
        ("audit-user-role", "ws1", "user", "u1", "auth.role_updated"),
        ("audit-user-sessions", "ws1", "user", "u1", "auth.sessions_revoked"),
    ]


def test_import_preserves_device_registration_lifecycle_audits(tmp_path, postgres_import_dsn):
    path = tmp_path / "snapshot_with_device_registration_audit.sqlite3"
    _snapshot(path)
    event = {
        "id": "audit-device-registered", "event_type": "notification.device_registered",
        "actor": "owner@example.test", "entity_id": "d1", "workspace_id": "ws1",
        "details": {"platform": "android", "token_fingerprint": "safe-fingerprint"},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            ("audit_event", event["id"], json.dumps(event)),
        )
    os.chmod(path, 0o600)

    plan, selected, digest = build_plan(path)

    assert plan["planned_counts"]["audit_event"] == 9
    assert selected["audit_event"][event["id"]]["_import_entity_type"] == "device_registration"
    apply_import(postgres_import_dsn, digest, plan, selected)
    apply_import(postgres_import_dsn, digest, plan, selected)

    with psycopg.connect(postgres_import_dsn) as conn:
        persisted = conn.execute(
            "SELECT workspace_id,entity_type,entity_id,event_type,details,payload "
            "FROM audit_events WHERE event_id=%s",
            (event["id"],),
        ).fetchone()
    assert persisted[:5] == (
        "ws1", "device_registration", "d1", "notification.device_registered",
        event["details"],
    )
    assert persisted[5] == {
        **event,
        "created_at": datetime.fromisoformat(event["created_at"])
        .astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
    }


@pytest.mark.parametrize(
    "event_type",
    [
        "notification.device_unregistered",
        "notification.device_token_invalidated",
    ],
)
def test_import_preserves_removed_device_audit_after_device_row_was_deleted(
    tmp_path, postgres_import_dsn, event_type,
):
    path = tmp_path / f"snapshot_with_{event_type.rsplit('.', 1)[-1]}_device_audit.sqlite3"
    _snapshot(path)
    event = {
        "id": f"audit-{event_type.rsplit('.', 1)[-1]}", "event_type": event_type,
        "actor": "owner@example.test", "entity_id": "d1", "workspace_id": "ws1",
        "details": {"platform": "android"},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with sqlite3.connect(path) as conn:
        conn.execute(
            "DELETE FROM kv_store WHERE entity_type='device_registration' AND entity_id='d1'"
        )
        conn.execute(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            ("audit_event", event["id"], json.dumps(event)),
        )
    os.chmod(path, 0o600)

    plan, selected, digest = build_plan(path)

    assert plan["planned_counts"]["device_registration"] == 0
    assert plan["planned_counts"]["audit_event"] == 9
    assert selected["audit_event"][event["id"]]["_import_entity_type"] == "device_registration"
    apply_import(postgres_import_dsn, digest, plan, selected)
    apply_import(postgres_import_dsn, digest, plan, selected)

    with psycopg.connect(postgres_import_dsn) as conn:
        device_count = conn.execute(
            "SELECT count(*) FROM device_registrations WHERE device_id='d1'"
        ).fetchone()[0]
        persisted = conn.execute(
            "SELECT workspace_id,entity_type,entity_id,event_type,details "
            "FROM audit_events WHERE event_id=%s",
            (event["id"],),
        ).fetchone()
    assert device_count == 0
    assert persisted == (
        "ws1", "device_registration", "d1", event_type,
        event["details"],
    )


def test_import_preserves_external_agent_token_rotation_audit(tmp_path, postgres_import_dsn):
    path = tmp_path / "snapshot_with_agent_token_rotation_audit.sqlite3"
    _snapshot(path)
    event = {
        "id": "audit-agent-token-rotated", "event_type": "agent.token_rotated",
        "actor": "owner@example.test", "entity_id": "agent1", "workspace_id": "ws1",
        "details": {"token_rotated_at": datetime.now(timezone.utc).isoformat()},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            ("audit_event", event["id"], json.dumps(event)),
        )
    os.chmod(path, 0o600)

    plan, selected, digest = build_plan(path)

    assert plan["planned_counts"]["audit_event"] == 9
    assert selected["audit_event"][event["id"]]["_import_entity_type"] == "external_agent"
    apply_import(postgres_import_dsn, digest, plan, selected)
    apply_import(postgres_import_dsn, digest, plan, selected)

    with psycopg.connect(postgres_import_dsn) as conn:
        persisted = conn.execute(
            "SELECT workspace_id,entity_type,entity_id,event_type,details "
            "FROM audit_events WHERE event_id=%s",
            (event["id"],),
        ).fetchone()
    assert persisted == (
        "ws1", "external_agent", "agent1", "agent.token_rotated", event["details"],
    )


@pytest.mark.parametrize("event_type", ["agent.auth_failed", "agent.rate_limited"])
def test_import_scopes_external_agent_security_audit_to_known_agent(
    tmp_path, postgres_import_dsn, event_type,
):
    path = tmp_path / f"snapshot_{event_type.rsplit('.', 1)[-1]}_agent_audit.sqlite3"
    _snapshot(path)
    event = {
        "id": f"audit-agent-{event_type.rsplit('.', 1)[-1]}", "event_type": event_type,
        "actor": "external-agent-auth", "entity_id": "agent1",
        "details": {"reason": "invalid_token"},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            ("audit_event", event["id"], json.dumps(event)),
        )
    os.chmod(path, 0o600)

    plan, selected, digest = build_plan(path)

    imported_event = selected["audit_event"][event["id"]]
    assert plan["planned_counts"]["audit_event"] == 9
    assert imported_event["_import_entity_type"] == "external_agent"
    assert imported_event["workspace_id"] == "ws1"
    apply_import(postgres_import_dsn, digest, plan, selected)
    apply_import(postgres_import_dsn, digest, plan, selected)

    with psycopg.connect(postgres_import_dsn) as conn:
        persisted = conn.execute(
            "SELECT workspace_id,entity_type,entity_id,event_type,payload "
            "FROM audit_events WHERE event_id=%s",
            (event["id"],),
        ).fetchone()
    assert persisted[:4] == ("ws1", "external_agent", "agent1", event_type)
    assert persisted[4]["workspace_id"] == "ws1"
    assert persisted[4]["details"] == event["details"]


def test_plan_fails_closed_for_unscoped_external_agent_auth_failure(tmp_path):
    path = tmp_path / "snapshot_with_unknown_agent_auth_failure.sqlite3"
    _snapshot(path)
    event = {
        "id": "audit-unknown-agent-auth-failure", "event_type": "agent.auth_failed",
        "actor": "external-agent-auth", "entity_id": "unknown_agent",
        "details": {"agent_id": None, "reason": "unknown_agent"},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            ("audit_event", event["id"], json.dumps(event)),
        )
    os.chmod(path, 0o600)

    with pytest.raises(ImportBlocked, match="structural audit did not pass"):
        build_plan(path)


@pytest.mark.parametrize(
    "event_type",
    [
        "agent_worker.task_released",
        "agent_worker.validation_run_completed",
    ],
)
def test_import_preserves_agent_task_worker_audits(tmp_path, postgres_import_dsn, event_type):
    path = tmp_path / f"snapshot_{event_type.rsplit('.', 1)[-1]}_worker_audit.sqlite3"
    _snapshot(path)
    event = {
        "id": f"audit-{event_type.rsplit('.', 1)[-1]}", "event_type": event_type,
        "actor": "agent-worker-test", "entity_id": "t1",
        "details": {"outcome": "recorded"},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            ("audit_event", event["id"], json.dumps(event)),
        )
    os.chmod(path, 0o600)

    plan, selected, digest = build_plan(path)

    imported_event = selected["audit_event"][event["id"]]
    assert plan["planned_counts"]["audit_event"] == 9
    assert imported_event["_import_entity_type"] == "agent_task"
    assert imported_event["workspace_id"] == "ws1"
    apply_import(postgres_import_dsn, digest, plan, selected)
    apply_import(postgres_import_dsn, digest, plan, selected)

    with psycopg.connect(postgres_import_dsn) as conn:
        persisted = conn.execute(
            "SELECT workspace_id,entity_type,entity_id,event_type,payload "
            "FROM audit_events WHERE event_id=%s",
            (event["id"],),
        ).fetchone()
    assert persisted[:4] == ("ws1", "agent_task", "t1", event_type)
    assert persisted[4]["workspace_id"] == "ws1"
    assert persisted[4]["details"] == event["details"]


@pytest.mark.parametrize("collision_type", ["approval_request", "work_order"])
def test_worker_audit_resolves_task_before_colliding_entity_ids(
    tmp_path, postgres_import_dsn, collision_type,
):
    path = tmp_path / f"snapshot_worker_collision_{collision_type}.sqlite3"
    _snapshot(path)
    now = datetime.now(timezone.utc).isoformat()
    collision = None
    if collision_type == "approval_request":
        with sqlite3.connect(path) as conn:
            raw = conn.execute(
                "SELECT payload FROM kv_store WHERE entity_type='approval_request' AND entity_id='a1'"
            ).fetchone()[0]
        collision = json.loads(raw)
        collision.update({
            "id": "t1", "project_id": "p2", "workspace_id": "ws2",
            # Keep this foreign ID collision terminal and non-native. A
            # pending Antigravity approval without invocation identity must
            # correctly block migration before audit-parent resolution.
            "status": "DENIED", "source_provider": "MANUAL",
            "work_order_id": None,
            "approved_payload_hash": None,
            "source_agent_id": None, "source_invocation_id": None,
        })
    else:
        with sqlite3.connect(path) as conn:
            raw = conn.execute(
                "SELECT payload FROM kv_store WHERE entity_type='work_order' AND entity_id='w1'"
            ).fetchone()[0]
        collision = json.loads(raw)
        collision.update({
            "id": "t1", "project_id": "p2", "workspace_id": "ws2",
            "idea_id": None, "source_agent_id": None,
        })
    event = {
        "id": f"audit-worker-collision-{collision_type}",
        "event_type": "agent_worker.task_released", "actor": "worker",
        "entity_id": "t1", "workspace_id": "ws1", "details": {},
        "created_at": now,
    }
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            [
                ("workspace", "ws2", json.dumps({
                    "id": "ws2", "name": "Other account", "owner_user_id": "u2",
                    "created_at": now,
                })),
                ("user", "u2", json.dumps({
                    "id": "u2", "email": "other@example.test", "password_hash": "hash",
                    "password_salt": "salt", "workspace_id": "ws2",
                    "created_at": now, "updated_at": now,
                })),
                ("project", "p2", json.dumps({
                    "id": "p2", "name": "Other project", "workspace_id": "ws2",
                    "created_at": now,
                })),
                (collision_type, "t1", json.dumps(collision)),
                ("audit_event", event["id"], json.dumps(event)),
            ],
        )
    os.chmod(path, 0o600)

    plan, selected, digest = build_plan(path)

    imported = selected["audit_event"][event["id"]]
    assert imported["_import_entity_type"] == "agent_task"
    assert imported["workspace_id"] == "ws1"
    apply_import(postgres_import_dsn, digest, plan, selected)
    apply_import(postgres_import_dsn, digest, plan, selected)
    with psycopg.connect(postgres_import_dsn) as conn:
        persisted = conn.execute(
            "SELECT workspace_id,entity_type,entity_id,event_type FROM audit_events WHERE event_id=%s",
            (event["id"],),
        ).fetchone()
    assert persisted == ("ws1", "agent_task", "t1", "agent_worker.task_released")


@pytest.mark.parametrize(
    ("event_type", "entity_id"),
    [
        ("agent_worker.validation_run_completed", "missing-task"),
        ("agent_worker.future_event", "missing-task"),
        ("agent_worker.container_validation_configured", "/tmp/validation-cwd"),
    ],
)
def test_plan_fails_closed_for_worker_audit_without_task_parent(
    tmp_path, event_type, entity_id,
):
    path = tmp_path / f"snapshot_worker_without_parent_{event_type.rsplit('.', 1)[-1]}.sqlite3"
    _snapshot(path)
    event = {
        "id": "audit-worker-without-parent", "event_type": event_type,
        "actor": "worker", "entity_id": entity_id, "workspace_id": "ws1",
        "details": {}, "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            ("audit_event", event["id"], json.dumps(event)),
        )
    os.chmod(path, 0o600)

    with pytest.raises(ImportBlocked, match="worker audit event has no supported AgentTask parent"):
        build_plan(path)


@pytest.mark.parametrize(
    ("event_type", "entity_id"),
    [
        ("notification.device_registered", "d1"),
        ("notification.device_reassigned", "d1"),
        ("notification.device_unregistered", "d1"),
        ("notification.device_token_invalidated", "d1"),
        ("agent.token_rotated", "agent1"),
        ("agent.auth_failed", "agent1"),
        ("agent.rate_limited", "agent1"),
        ("agent_worker.validation_run_completed", "t1"),
    ],
)
def test_plan_rejects_production_identity_lifecycle_audit_with_wrong_workspace(
    tmp_path, event_type, entity_id,
):
    path = tmp_path / f"snapshot_{event_type.rsplit('.', 1)[-1]}_wrong_workspace.sqlite3"
    _snapshot(path)
    event = {
        "id": "audit-identity-cross-workspace", "event_type": event_type,
        "actor": "owner@example.test", "entity_id": entity_id,
        "workspace_id": "ws2", "details": {},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            [
                (
                    "workspace", "ws2",
                    json.dumps({
                        "id": "ws2", "name": "Other account", "owner_user_id": "u2",
                        "created_at": event["created_at"],
                    }),
                ),
                (
                    "user", "u2",
                    json.dumps({
                        "id": "u2", "email": "other@example.test", "password_hash": "hash",
                        "password_salt": "salt", "workspace_id": "ws2",
                        "created_at": event["created_at"], "updated_at": event["created_at"],
                    }),
                ),
                ("audit_event", event["id"], json.dumps(event)),
            ],
        )
    os.chmod(path, 0o600)

    with pytest.raises(ImportBlocked):
        build_plan(path)


def test_plan_resolves_auth_audit_by_event_domain_when_ids_collide(tmp_path):
    path = tmp_path / "snapshot_with_cross_entity_id_collision.sqlite3"
    _snapshot(path)
    now = datetime.now(timezone.utc).isoformat()
    user = {
        "id": "a1", "email": "collision@example.test", "password_hash": "hash",
        "password_salt": "salt", "workspace_id": "ws1",
        "created_at": now, "updated_at": now,
    }
    event = {
        "id": "audit-colliding-user-role", "event_type": "auth.role_updated",
        "actor": "owner@example.test", "entity_id": "a1", "workspace_id": "ws1",
        "details": {"target_user_id": "a1", "new_role": "REVIEWER"}, "created_at": now,
    }
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            [
                ("user", "a1", json.dumps(user)),
                ("audit_event", event["id"], json.dumps(event)),
            ],
        )
    os.chmod(path, 0o600)

    _plan, selected, _digest = build_plan(path)

    imported = selected["audit_event"][event["id"]]
    assert imported["_import_entity_type"] == "user"
    assert imported["entity_id"] == "a1"


def test_import_preserves_workspace_scoped_audit_event_idempotently(
    tmp_path, postgres_import_dsn
):
    path = tmp_path / "snapshot_with_workspace_audit.sqlite3"
    _snapshot(path)
    event = {
        "id": "audit-workspace-settings", "event_type": "workspace.settings_updated",
        "actor": "owner@example.test", "entity_id": "ws1", "workspace_id": "ws1",
        "details": {"setting": "approval_expiry", "value": "24h"},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            ("audit_event", event["id"], json.dumps(event)),
        )
    os.chmod(path, 0o600)

    plan, selected, digest = build_plan(path)
    assert plan["planned_counts"]["audit_event"] == 9
    assert selected["audit_event"][event["id"]]["_import_entity_type"] == "workspace"

    apply_import(postgres_import_dsn, digest, plan, selected)
    apply_import(postgres_import_dsn, digest, plan, selected)

    with psycopg.connect(postgres_import_dsn) as conn:
        persisted = conn.execute(
            "SELECT workspace_id,entity_type,entity_id,event_type,details "
            "FROM audit_events WHERE event_id=%s",
            (event["id"],),
        ).fetchone()
        assert persisted == (
            "ws1", "workspace", "ws1", "workspace.settings_updated", event["details"]
        )
        assert conn.execute(
            "SELECT count(*) FROM audit_events WHERE event_id=%s", (event["id"],)
        ).fetchone()[0] == 1


def test_plan_rejects_workspace_audit_with_mismatched_workspace_scope(tmp_path):
    path = tmp_path / "snapshot_with_mismatched_workspace_audit.sqlite3"
    _snapshot(path)
    event = {
        "id": "audit-workspace-cross-scope", "event_type": "workspace.settings_updated",
        "actor": "owner@example.test", "entity_id": "ws1",
        "workspace_id": "foreign-workspace", "details": {},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            ("audit_event", event["id"], json.dumps(event)),
        )
    os.chmod(path, 0o600)

    with pytest.raises(ImportBlocked):
        build_plan(path)


@pytest.mark.parametrize(
    ("event_type", "entity_id"),
    [
        ("auth.role_updated", "u1"),
        ("auth.session_revoked", "s1"),
    ],
)
def test_import_rejects_auth_audit_outside_parent_workspace(
    tmp_path, event_type, entity_id
):
    path = tmp_path / f"snapshot_{event_type.replace('.', '_')}.sqlite3"
    _snapshot(path)
    event = {
        "id": "audit-auth-cross-workspace", "event_type": event_type,
        "actor": "owner@example.test", "entity_id": entity_id,
        "workspace_id": "foreign-workspace", "details": {},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)",
            ("audit_event", event["id"], json.dumps(event)),
        )
    os.chmod(path, 0o600)

    with pytest.raises(ImportBlocked):
        build_plan(path)


@pytest.mark.parametrize(
    ("event_type", "entity_id", "entity_type"),
    [
        ("agent_task.created", "t1", "agent_task"),
        ("agent_run.created", "run-audit-scope-test", "agent_run"),
    ],
)
def test_plan_rejects_cross_workspace_task_and_run_audits(
    tmp_path, event_type, entity_id, entity_type,
):
    path = tmp_path / f"{entity_type}_cross_workspace_audit.sqlite3"
    _snapshot(path)
    now = datetime.now(timezone.utc).isoformat()
    if entity_type == "agent_run":
        run = AgentRun(
            id=entity_id, task_id="t1", workspace_id="ws1", project_id="p1",
            status=AgentRunStatus.SUCCEEDED,
        )
        run_record = ("agent_run", run.id, json.dumps(run.model_dump(mode="json")))
    else:
        run_record = None
    audit = {
        "id": "cross-workspace-audit", "event_type": event_type, "actor": "system",
        "entity_id": entity_id, "workspace_id": "another-workspace", "details": {},
        "created_at": now,
    }
    with sqlite3.connect(path) as conn:
        rows = [("audit_event", audit["id"], json.dumps(audit))]
        if run_record:
            rows.append(run_record)
        conn.executemany("INSERT INTO kv_store(entity_type,entity_id,payload) VALUES(?,?,?)", rows)
    os.chmod(path, 0o600)

    # The read-only legacy audit may reject this before importer planning; either
    # way the cross-workspace audit record must never enter an import plan.
    with pytest.raises(ImportBlocked):
        build_plan(path)


def test_apply_import_rejects_email_key_mismatch_before_schema_changes(
    tmp_path, postgres_import_dsn, monkeypatch
):
    path = tmp_path / "snapshot.sqlite3"
    _snapshot(path)
    plan, selected, digest = build_plan(path)
    monkeypatch.setenv("AIXION_EMAIL_OUTBOX_KEY", "22" * 32)

    with pytest.raises(ImportBlocked, match="could not be authenticated"):
        apply_import(postgres_import_dsn, digest, plan, selected)

    with psycopg.connect(postgres_import_dsn) as conn:
        migration_table_count = conn.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema=current_schema() AND table_name='schema_migrations'"
        ).fetchone()[0]
    assert migration_table_count == 0


def test_import_preserves_trust_state_signatures_and_hash_chain(tmp_path, postgres_import_dsn):
    path = tmp_path / "approval_and_trust.sqlite3"
    _snapshot(path, include_omitted=False, include_trust=True)
    plan, selected, digest = build_plan(path)

    assert plan["scope"] == "approval_auth_agent_mcp_test_runs_connectors_email_task_notifications_delivery_rate_limits_invocation_leases_faults_v13"
    assert plan["planned_counts"]["agent_run"] == 1
    assert plan["planned_counts"]["agent_run_step"] == 1
    assert plan["planned_counts"]["agent_run_event"] == 1
    assert plan["planned_counts"]["capability_lease"] == 1
    assert plan["planned_counts"]["reviewer_attestation"] == 1
    assert plan["planned_counts"]["proposed_action"] == 1
    assert plan["planned_counts"]["policy_decision"] == 2
    assert plan["planned_counts"]["action_authorization"] == 1
    assert plan["planned_counts"]["action_consumption"] == 1
    assert plan["planned_counts"]["credential_grant"] == 1
    assert plan["planned_counts"]["trust_event"] == 1

    apply_import(postgres_import_dsn, digest, plan, selected)
    apply_import(postgres_import_dsn, digest, plan, selected)

    with psycopg.connect(postgres_import_dsn) as conn:
        for table in (
            "agent_runs", "capability_leases", "reviewer_attestations", "proposed_actions",
            "agent_task_events", "agent_run_steps", "agent_run_events",
            "agent_connectors", "invites", "mcp_child_servers", "mcp_pending_requests",
            "policy_decisions", "action_authorizations", "action_consumptions",
            "credential_grants", "trust_events",
        ):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1 or (
                table == "policy_decisions"
                and conn.execute("SELECT count(*) FROM policy_decisions").fetchone()[0] == 2
            ), table
        event = conn.execute(
            "SELECT event_id,sequence,event_hash,payload FROM trust_events"
        ).fetchone()
        head = conn.execute(
            "SELECT sequence,event_hash FROM trust_event_heads WHERE singleton=TRUE"
        ).fetchone()
        consumption = conn.execute(
            "SELECT payload->'consumption' FROM action_consumptions WHERE action_id='action1'"
        ).fetchone()[0]
        assert event[0:3] == ("trust_event1", 1, selected["trust_event"]["trust_event1"]["event_hash"])
        assert head == (1, event[2])
        assert consumption["actual_runtime_seconds"] == 2
        assert conn.execute("SELECT count(*) FROM sqlite_import_runs").fetchone()[0] == 1
    verification = PostgresTrustEventRepository(
        lambda: psycopg.connect(postgres_import_dsn)
    ).verify()
    assert verification.valid is True
    assert verification.event_count == 1


def test_partial_import_refuses_preexisting_omitted_domain_data(
    tmp_path,
    postgres_import_dsn,
):
    path = tmp_path / "partial_snapshot.sqlite3"
    _snapshot(path, include_omitted=False)
    plan, selected, digest = build_plan(path)
    from app.postgres_database import run_postgres_migrations

    run_postgres_migrations(lambda: psycopg.connect(postgres_import_dsn))
    with psycopg.connect(postgres_import_dsn) as conn:
        # Even though this importer has an MCP slice, it must not create a
        # mixed-authority schema beside any pre-existing application records.
        created_at = datetime.now(timezone.utc)
        updated_at = datetime.now(timezone.utc)
        conn.execute(
            "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
            ("unrelated", psycopg.types.json.Jsonb({"id": "unrelated", "name": "Other", "owner_user_id": "owner", "created_at": datetime.now(timezone.utc).isoformat()})),
        )
        conn.execute(
            "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
            ("unrelated-project", "unrelated", psycopg.types.json.Jsonb({"id": "unrelated-project", "workspace_id": "unrelated"})),
        )
        conn.execute(
            "INSERT INTO mcp_child_servers(server_id,project_id,workspace_id,name,enabled,created_at,updated_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                "mcp-existing", "unrelated-project", "unrelated", "existing", True,
                created_at, updated_at,
                psycopg.types.json.Jsonb({
                    "id": "mcp-existing", "project_id": "unrelated-project",
                    "workspace_id": "unrelated", "name": "existing", "enabled": True,
                    "created_at": created_at.isoformat(),
                    "updated_at": updated_at.isoformat(),
                }),
            ),
        )

    with pytest.raises(ImportBlocked, match="partial import requires a pristine schema"):
        apply_import(postgres_import_dsn, digest, plan, selected)

    with psycopg.connect(postgres_import_dsn) as conn:
        assert conn.execute("SELECT count(*) FROM mcp_child_servers").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM approval_requests").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM sqlite_import_runs").fetchone()[0] == 0


def test_active_agent_run_fault_blocks_apply_even_with_partial_acknowledgement(
    tmp_path, monkeypatch, capsys,
):
    path = tmp_path / "active_fault_snapshot.sqlite3"
    fault_id = "diagnostic-fault-secret-id"
    _snapshot(path, include_omitted=False, agent_run_faults=[{
        "id": fault_id,
        "run_id": "fault-run",
        "step_type": None,
        "fault_kind": "WORKER_CRASH",
        "enabled": True,
        "remaining_uses": 1,
        "message": "must-not-appear-in-output",
        "created_by": "test",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }])
    writes = []
    monkeypatch.setenv("AIXION_TEST_POSTGRES_DSN", "postgresql://unused.invalid/approval_test")
    monkeypatch.setattr(
        "scripts.import_approval_slice_to_postgres.apply_import",
        lambda *args, **kwargs: writes.append((args, kwargs)),
    )

    exit_code = main([
        "--source", str(path), "--apply", "--allow-partial-slice",
        "--confirm-source-snapshot",
    ])

    captured = capsys.readouterr()
    expected_fingerprint = hashlib.sha256(fault_id.encode()).hexdigest()
    assert exit_code == 2
    assert '"status": "BLOCKED"' in captured.err
    assert "active AgentRun fault injection settings block PostgreSQL import" in captured.err
    assert "disable or remove each setting" in captured.err
    assert expected_fingerprint in captured.err
    assert fault_id not in captured.err
    assert "must-not-appear-in-output" not in captured.err
    assert writes == []


def test_integer_encoded_enabled_agent_run_fault_blocks_import(tmp_path):
    path = tmp_path / "integer_enabled_fault_snapshot.sqlite3"
    _snapshot(path, include_omitted=False, agent_run_faults=[{
        "id": "integer-enabled-fault",
        "run_id": "fault-run",
        "step_type": None,
        "fault_kind": "WORKER_CRASH",
        "enabled": 1,
        "remaining_uses": 1,
        "message": "legacy SQLite boolean representation",
        "created_by": "test",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }])

    with pytest.raises(ImportBlocked, match="active AgentRun fault injection settings"):
        build_plan(path)


def test_inactive_agent_run_faults_are_imported(tmp_path):
    path = tmp_path / "inactive_fault_snapshot.sqlite3"
    fault_id = "inactive-diagnostic-fault"
    _snapshot(path, include_omitted=False, agent_run_faults=[{
        "id": fault_id,
        "run_id": "fault-run",
        "step_type": None,
        "fault_kind": "WORKER_CRASH",
        "enabled": False,
        "remaining_uses": 3,
        "message": "excluded diagnostic",
        "created_by": "test",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }])

    plan, selected, _ = build_plan(path)

    assert selected["agent_run_fault"][fault_id]["enabled"] is False
    assert plan["agent_run_fault_injection"] == {
        "status": "CLEAR",
        "inactive_settings": "IMPORTED",
        "active_count": 0,
        "active_id_sha256": [],
        "inactive_import_count": 1,
        "inactive_import_id_sha256": [hashlib.sha256(fault_id.encode()).hexdigest()],
    }
    assert plan["planned_counts"]["agent_run_fault"] == 1
    assert plan["omitted_counts"].get("agent_run_fault", 0) == 0


def test_plan_refuses_invalid_trust_authorization_signature(tmp_path):
    path = tmp_path / "invalid_trust_signature.sqlite3"
    _snapshot(path, include_omitted=False, include_trust=True)
    with sqlite3.connect(path) as conn:
        raw = conn.execute(
            "SELECT payload FROM kv_store WHERE entity_type='action_authorization'"
        ).fetchone()[0]
        payload = json.loads(raw)
        payload["signature"] = "invalid-signature"
        conn.execute(
            "UPDATE kv_store SET payload=? WHERE entity_type='action_authorization'",
            (json.dumps(payload),),
        )

    with pytest.raises(ImportBlocked, match="signature or action hash is invalid"):
        build_plan(path)


def test_plan_refuses_consumption_without_matching_trust_event(tmp_path):
    path = tmp_path / "orphaned_trust_consumption.sqlite3"
    _snapshot(path, include_omitted=False, include_trust=True)
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM kv_store WHERE entity_type='trust_event'")

    with pytest.raises(ImportBlocked, match="exactly one matching verified trust event"):
        build_plan(path)


def test_idempotent_retry_rejects_same_count_target_content_drift(
    tmp_path,
    postgres_import_dsn,
):
    path = tmp_path / "snapshot.sqlite3"
    _snapshot(path)
    plan, selected, digest = build_plan(path)
    apply_import(postgres_import_dsn, digest, plan, selected)

    with psycopg.connect(postgres_import_dsn) as conn:
        conn.execute(
            "UPDATE projects SET payload=jsonb_set(payload,'{name}','\"Tampered\"') "
            "WHERE project_id='p1'"
        )
        assert conn.execute("SELECT count(*) FROM projects").fetchone()[0] == 1

    with pytest.raises(ImportBlocked, match="target project payloads changed"):
        apply_import(postgres_import_dsn, digest, plan, selected)


def test_idempotent_retry_rejects_invocation_lease_drift(tmp_path, postgres_import_dsn):
    path = tmp_path / "snapshot.sqlite3"
    _snapshot(path)
    plan, selected, digest = build_plan(path)
    apply_import(postgres_import_dsn, digest, plan, selected)
    with psycopg.connect(postgres_import_dsn) as conn:
        conn.execute(
            "UPDATE approval_invocation_leases SET source_invocation_id='tampered' "
            "WHERE approval_id='a1'"
        )
    with pytest.raises(ImportBlocked, match="target approval_invocation_lease payloads changed"):
        apply_import(postgres_import_dsn, digest, plan, selected)


def test_apply_import_rolls_back_rows_when_a_parent_constraint_fails(tmp_path, postgres_import_dsn):
    path = tmp_path / "snapshot.sqlite3"
    _snapshot(path, include_omitted=False)
    plan, selected, digest = build_plan(path)
    selected["project"]["p1"]["workspace_id"] = "missing-workspace"

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        apply_import(postgres_import_dsn, digest, plan, selected)

    with psycopg.connect(postgres_import_dsn) as conn:
        assert conn.execute("SELECT count(*) FROM workspaces").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM projects").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM sqlite_import_runs").fetchone()[0] == 0


def test_apply_import_rolls_back_when_invocation_lease_scope_fails(tmp_path, postgres_import_dsn):
    path = tmp_path / "snapshot.sqlite3"
    _snapshot(path, include_omitted=False)
    plan, selected, digest = build_plan(path)
    selected["approval_invocation_lease"]["a1"]["workspace_id"] = "missing-workspace"
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        apply_import(postgres_import_dsn, digest, plan, selected)
    with psycopg.connect(postgres_import_dsn) as conn:
        assert conn.execute("SELECT count(*) FROM workspaces").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM approval_requests").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM approval_invocation_leases").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM sqlite_import_runs").fetchone()[0] == 0
