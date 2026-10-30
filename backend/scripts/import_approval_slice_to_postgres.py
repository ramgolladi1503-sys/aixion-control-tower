#!/usr/bin/env python3
"""Dry-run-first importer for the explicitly partial app-state migration slice.

This does not authorize production migration or cutover. Apply only to a
disposable PostgreSQL database whose name ends in ``_test`` or ``_staging``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal
from typing import Any
from urllib.parse import quote

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from app.agent_task_models import AgentTask
from app.agent_task_models import AgentTaskEvent
from app.agent_credential_models import AgentCredentialRecord
from app.agent_run_models import AgentRun, AgentRunEvent, AgentRunFaultConfig, AgentRunStep
from app.approval_integrity import compute_approval_payload_hash
from app.connector_models import AgentConnector
from app.email_delivery import _decrypt_code
from app.models import (
    ApprovalRequest,
    ApprovalStatus,
    Invite,
    MCPChildServer,
    MCPPendingRequest,
    TestRun,
    AuditEvent,
    DeviceRegistration,
    ExternalAgent,
    Idea,
    Notification,
    Project,
    SessionToken,
    User,
    WorkOrder,
    Workspace,
)
from app.postgres_database import run_postgres_migrations
from scripts.audit_legacy_sqlite import audit
from app.trust_action_authorization import action_payload_hash, authorization_signature_payload
from app.trust_action_authorization_models import ActionAuthorization
from app.trust_policy import lease_receipt_payload
from app.trust_crypto import verify_payload_signature
from app.trust_models import (
    ActionConsumption,
    CapabilityLease,
    CredentialGrant,
    PolicyDecision,
    ProposedAction,
    ReviewerAttestation,
    TrustEvent,
)

SCOPE = "approval_auth_agent_mcp_test_runs_connectors_email_task_notifications_delivery_rate_limits_invocation_leases_faults_legacy_audit_global_auth_quarantine_v15"
ENTITY_TYPES = {
    "workspace": Workspace,
    "user": User,
    "invite": Invite,
    "session": SessionToken,
    "project": Project,
    "idea": Idea,
    "work_order": WorkOrder,
    "agent_connector": AgentConnector,
    "external_agent": ExternalAgent,
    "external_agent_credential": AgentCredentialRecord,
    "device_registration": DeviceRegistration,
    "approval_request": ApprovalRequest,
    "agent_task": AgentTask,
    "agent_task_event": AgentTaskEvent,
    "agent_run": AgentRun,
    "agent_run_step": AgentRunStep,
    "agent_run_event": AgentRunEvent,
    "agent_run_fault": AgentRunFaultConfig,
    "audit_event": AuditEvent,
    "notification": Notification,
    "capability_lease": CapabilityLease,
    "reviewer_attestation": ReviewerAttestation,
    "proposed_action": ProposedAction,
    "policy_decision": PolicyDecision,
    "action_authorization": ActionAuthorization,
    "credential_grant": CredentialGrant,
    "trust_event": TrustEvent,
    "mcp_child_server": MCPChildServer,
    "mcp_pending_request": MCPPendingRequest,
    "test_run": TestRun,
}
CORE_TYPES = {
    "workspace", "user", "invite", "session", "project", "idea", "work_order", "agent_connector", "external_agent",
    "external_agent_credential", "device_registration", "approval_request",
    "agent_task", "agent_task_event", "agent_run", "agent_run_step", "agent_run_event",
    "agent_run_fault",
    "audit_event", "legacy_audit_event", "notification", "capability_lease",
    "reviewer_attestation", "proposed_action", "policy_decision", "action_authorization",
    "action_consumption", "credential_grant", "trust_event",
    "auth_email_outbox",
    "approval_delivery_claim",
    "approval_delivery_failure",
    "approval_invocation_lease",
    "auth_rate_limit_bucket",
    "mcp_child_server", "mcp_pending_request",
    "test_run",
}
IMPORT_TABLES = {
    "workspace": ("workspaces", "workspace_id"),
    "user": ("users", "user_id"),
    "invite": ("invites", "invite_id"),
    "session": ("sessions", "session_id"),
    "project": ("projects", "project_id"),
    "idea": ("ideas", "idea_id"),
    "work_order": ("work_orders", "work_order_id"),
    "mcp_child_server": ("mcp_child_servers", "server_id"),
    "mcp_pending_request": ("mcp_pending_requests", "pending_id"),
    "test_run": ("test_runs", "test_run_id"),
    "agent_connector": ("agent_connectors", "connector_id"),
    "external_agent": ("external_agents", "agent_id"),
    "external_agent_credential": ("external_agent_credentials", "credential_id"),
    "device_registration": ("device_registrations", "device_id"),
    "approval_request": ("approval_requests", "approval_id"),
    "agent_task": ("agent_tasks", "task_id"),
    "agent_task_event": ("agent_task_events", "event_id"),
    "agent_run": ("agent_runs", "run_id"),
    "agent_run_step": ("agent_run_steps", "step_id"),
    "agent_run_event": ("agent_run_events", "event_id"),
    "agent_run_fault": ("agent_run_faults", "fault_id"),
    "audit_event": ("audit_events", "event_id"),
    "legacy_audit_event": ("legacy_audit_events", "event_id"),
    "notification": ("notifications", "notification_id"),
    "capability_lease": ("capability_leases", "lease_id"),
    "reviewer_attestation": ("reviewer_attestations", "attestation_id"),
    "proposed_action": ("proposed_actions", "action_id"),
    "policy_decision": ("policy_decisions", "decision_id"),
    "action_authorization": ("action_authorizations", "authorization_id"),
    "action_consumption": ("action_consumptions", "action_id"),
    "credential_grant": ("credential_grants", "grant_id"),
    "trust_event": ("trust_events", "event_id"),
    "auth_email_outbox": ("auth_email_outbox", "outbox_id"),
    "approval_delivery_claim": ("approval_delivery_claims", "approval_id"),
    "approval_delivery_failure": ("approval_delivery_failures", "approval_id"),
    "approval_invocation_lease": ("approval_invocation_leases", "approval_id"),
    "auth_rate_limit_bucket": ("auth_rate_limit_buckets", "bucket_hash"),
}
# Exhaustive disposition for every persistent table created by the checked-in
# SQLite migration chain. Keep partial/metadata states explicit: this importer
# is intentionally not a production cutover tool.
SOURCE_TABLE_DISPOSITIONS: dict[str, tuple[str, str | None]] = {
    "schema_migrations": ("metadata_only", None),
    "kv_store": ("partial_entity_import", "CORE_TYPES -> entity-specific PostgreSQL tables"),
    "auth_email_outbox": ("imported", "auth_email_outbox"),
    "auth_rate_limit_buckets": ("imported", "auth_rate_limit_buckets"),
    "approval_delivery_claims": ("imported", "approval_delivery_claims"),
    "approval_delivery_failures": ("imported", "approval_delivery_failures"),
    "approval_invocation_leases": ("imported", "approval_invocation_leases"),
}

# Explicit coverage for serialized rows in SQLiteBackedStore.kv_store. The
# Store's persistence calls and the repository write paths are the source of
# truth; tests compare those source-defined types with this inventory. Supported
# audit parents enter the live table; unresolved history is segregated.
SOURCE_KV_ENTITY_DISPOSITIONS: dict[str, tuple[str, str, str | None]] = {
    kind: (
        "segregated_import" if kind == "audit_event" else "imported",
        (
            "audit_events + legacy_audit_events (workspace-scoped archive and ownerless auth quarantine)"
            if kind == "audit_event" else IMPORT_TABLES[kind][0]
        ),
        (
            "Supported, workspace-verified parents enter audit_events; unresolved history is "
            "preserved in legacy_audit_events. Ownerless unknown-agent auth failures are "
            "quarantined with workspace_id NULL; sensitive delivery events and all other "
            "unscoped history block import."
            if kind == "audit_event"
            else None
        ),
    )
    for kind in ENTITY_TYPES
}
SOURCE_KV_ENTITY_DISPOSITIONS["action_consumption"] = (
    "derived_import",
    IMPORT_TABLES["action_consumption"][0],
    "Validated against its action, lease, and exactly one matching verified trust event.",
)


class ImportBlocked(RuntimeError):
    pass


def _source_table_coverage(path: Path) -> dict[str, dict[str, str | None]]:
    """Report dispositions for all persistent tables present in a source snapshot.

    Unknown tables fail before target connection. The migration-chain regression
    test ensures every application-created table has an explicit disposition.
    """
    uri = f"file:{quote(path.resolve().as_posix(), safe='/')}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as conn:
        conn.execute("PRAGMA query_only = ON")
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    unclassified = tables - SOURCE_TABLE_DISPOSITIONS.keys()
    if unclassified:
        raise ImportBlocked("SQLite source contains persistent tables without an importer disposition")
    return {
        name: {
            "disposition": SOURCE_TABLE_DISPOSITIONS[name][0],
            "target": SOURCE_TABLE_DISPOSITIONS[name][1],
        }
        for name in sorted(tables)
    }


def _source_kv_entity_type_counts(path: Path) -> dict[str, int]:
    """Return observed kv_store entity counts, refusing unreviewed row types.

    This is deliberately run during source preflight, before any PostgreSQL
    connection. It prevents a newly introduced SQLite persistence type from
    disappearing through the importer's supported-type selection filter.
    """
    uri = f"file:{quote(path.resolve().as_posix(), safe='/')}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as conn:
        conn.execute("PRAGMA query_only = ON")
        rows = conn.execute(
            "SELECT entity_type, count(*) FROM kv_store GROUP BY entity_type ORDER BY entity_type"
        ).fetchall()
    counts = {str(entity_type): int(count) for entity_type, count in rows}
    if counts.keys() - SOURCE_KV_ENTITY_DISPOSITIONS.keys():
        raise ImportBlocked(
            "SQLite kv_store contains entity types without an importer disposition"
        )
    return counts


def _source_kv_entity_coverage(
    source_counts: dict[str, int], imported_counts: dict[str, int]
) -> dict[str, dict[str, str | int | None]]:
    """Describe source and target coverage for the complete recognized inventory."""
    coverage: dict[str, dict[str, str | int | None]] = {}
    for kind, (disposition, target, reason) in sorted(SOURCE_KV_ENTITY_DISPOSITIONS.items()):
        source_count = source_counts.get(kind, 0)
        imported_count = imported_counts.get(kind, 0)
        if kind == "audit_event":
            imported_count += imported_counts.get("legacy_audit_event", 0)
        omitted_count = source_count - imported_count
        if omitted_count < 0:
            raise ImportBlocked(f"imported {kind} count exceeds its SQLite source count")
        coverage[kind] = {
            "disposition": disposition,
            "target": target,
            "reason": reason,
            "source_count": source_count,
            "imported_count": imported_count,
            "omitted_count": omitted_count,
        }
    return coverage


def _resolve_audit_event_parent(
    event_type: str | None,
    entity_id: str | None,
    *,
    workspace_id: str | None,
    sessions: dict[str, dict[str, Any]],
    users: dict[str, dict[str, Any]],
    tasks: dict[str, dict[str, Any]],
    runs: dict[str, dict[str, Any]],
    devices: dict[str, dict[str, Any]],
    agents: dict[str, dict[str, Any]],
    approvals: dict[str, dict[str, Any]],
    projects: dict[str, dict[str, Any]],
    work_orders: dict[str, dict[str, Any]],
    ideas: dict[str, dict[str, Any]],
    connectors: dict[str, dict[str, Any]],
    invites: dict[str, dict[str, Any]],
    workspaces: dict[str, dict[str, Any]],
    mcp_servers: dict[str, dict[str, Any]],
    mcp_pending: dict[str, dict[str, Any]],
    test_runs: dict[str, dict[str, Any]],
) -> tuple[str | None, dict[str, Any] | None, str]:
    """Resolve the audit parent using the exact precedence used by import.

    The third result distinguishes event-type rules from the legacy entity-ID
    fallback and also carries explicit unresolved cases for read-only review.
    """
    if event_type == "agent_worker.container_validation_configured":
        return None, None, "path_keyed_without_task_parent"
    if isinstance(event_type, str) and event_type.startswith("agent_worker."):
        parent = tasks.get(entity_id)
        return "agent_task", parent, "event_type_rule"
    if event_type == "auth.session_revoked":
        session = sessions.get(entity_id)
        user = users.get(session.get("user_id")) if session else None
        if session is not None and not user:
            return "auth_session", None, "missing_session_owner"
        if session is not None and user is not None:
            return "auth_session", {**session, "workspace_id": user.get("workspace_id")}, "event_type_rule"
    if event_type in {
        "notification.device_registered",
        "notification.device_reassigned",
        "notification.device_unregistered",
        "notification.device_token_invalidated",
    }:
        parent = devices.get(entity_id)
        if parent is not None:
            return "device_registration", parent, "event_type_rule"
        if event_type == "notification.device_unregistered" and workspace_id:
            workspace = workspaces.get(workspace_id)
            if workspace is not None:
                return "device_registration", {**workspace, "workspace_id": workspace_id}, "device_unregister_workspace_scope"
        if event_type == "notification.device_token_invalidated" and workspace_id:
            # FCM invalidation deletes the registration and appends its audit
            # row in one transaction. A later SQLite snapshot therefore has
            # no device row to join against. The event's explicit workspace
            # must still resolve to a persisted workspace; use that record
            # only as scope evidence and never synthesize a registration.
            workspace = workspaces.get(workspace_id)
            if workspace is not None:
                return (
                    "device_registration",
                    {**workspace, "workspace_id": workspace_id},
                    "device_token_invalidation_workspace_scope",
                )
        return "device_registration", None, "event_type_rule"
    if entity_id in users and event_type in {
        "auth.account_deletion_requested",
        "auth.role_updated",
        "auth.sessions_revoked",
    }:
        return "user", users[entity_id], "event_type_rule"
    if event_type in {
        "agent.registered",
        "agent.token_rotated",
        "agent.token_revoked",
        "agent.auth_failed",
        "agent.rate_limited",
    }:
        return "external_agent", agents.get(entity_id), "event_type_rule"

    candidates = (
        ("approval_request", approvals),
        ("project", projects),
        ("work_order", work_orders),
        ("idea", ideas),
        ("agent_connector", connectors),
        ("invite", invites),
        ("workspace", workspaces),
        ("mcp_child_server", mcp_servers),
        ("mcp_pending_request", mcp_pending),
    )
    for parent_type, collection in candidates:
        parent = collection.get(entity_id)
        if parent is not None:
            if parent_type == "workspace":
                parent = {**parent, "workspace_id": entity_id}
            return parent_type, parent, "entity_id_fallback"
    parent = test_runs.get(entity_id)
    if parent is not None:
        approval = approvals.get(parent.get("approval_request_id"))
        if approval is None:
            return "test_run", None, "missing_test_run_approval"
        return "test_run", {**parent, "workspace_id": approval.get("workspace_id")}, "entity_id_fallback"
    if isinstance(event_type, str) and event_type.startswith("agent_task."):
        return "agent_task", tasks.get(entity_id), "event_type_rule"
    if isinstance(event_type, str) and event_type.startswith("agent_run."):
        return "agent_run", runs.get(entity_id), "event_type_rule"
    return None, None, "unresolved_parent"


def _native_invocation_drain_summary(
    approvals: dict[str, dict[str, Any]],
    delivery_claims: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Return a safe summary for native invocation approvals that must drain.

    A cutover snapshot cannot prove that an old native invocation is still
    paused. Never synthesize its lease from persisted approval timestamps or
    connector metadata. Hash approval IDs so operators can correlate a blocked
    plan without exposing request payloads or credentials in CLI output.
    """
    pending_statuses = {
        ApprovalStatus.REQUESTED.value,
        ApprovalStatus.PENDING_REVIEW.value,
    }
    native_providers = {"ANTIGRAVITY", "CODEX"}
    pending_fingerprints: list[str] = []
    approved_unconsumed_fingerprints: list[str] = []
    unverifiable_fingerprints: list[str] = []
    native_approval_count = 0
    for approval_id, payload in approvals.items():
        if not (
            payload.get("source_provider") in native_providers
            or payload.get("source_invocation_id") is not None
        ):
            continue
        native_approval_count += 1
        status = payload.get("status", ApprovalStatus.REQUESTED.value)
        pending = status in pending_statuses
        approved_unconsumed = (
            status == ApprovalStatus.APPROVED.value and approval_id not in delivery_claims
        )
        approval_fingerprint = hashlib.sha256(approval_id.encode("utf-8")).hexdigest()
        if pending:
            pending_fingerprints.append(approval_fingerprint)
        if approved_unconsumed:
            approved_unconsumed_fingerprints.append(approval_fingerprint)
        identity_valid = (
            isinstance(payload.get("source_agent_id"), str)
            and bool(payload.get("source_agent_id"))
            and isinstance(payload.get("source_invocation_id"), str)
            and bool(payload.get("source_invocation_id"))
        )
        if (pending or approved_unconsumed) and not identity_valid:
            unverifiable_fingerprints.append(approval_fingerprint)
    pending_fingerprints.sort()
    approved_unconsumed_fingerprints.sort()
    unverifiable_fingerprints = sorted(set(unverifiable_fingerprints))
    return {
        "native_invocation_approval_count": native_approval_count,
        "pending_native_invocation_count": len(pending_fingerprints),
        "approval_id_sha256": pending_fingerprints,
        "approved_unconsumed_invocation_count": len(approved_unconsumed_fingerprints),
        "approved_unconsumed_approval_id_sha256": approved_unconsumed_fingerprints,
        "unverifiable_native_invocation_count": len(unverifiable_fingerprints),
        "unverifiable_approval_id_sha256": unverifiable_fingerprints,
    }


def build_invocation_drain_report(
    path: Path, *, generated_at: datetime | None = None
) -> dict[str, Any]:
    """Build a redacted, read-only cutover report bound to one stable snapshot."""
    _validate_source(path)
    initial_stat = path.stat()
    digest = _sha256_file(path)
    records = _read_records(path)
    claims, _failures = _read_delivery_state(path)
    leases, lease_table_present = _read_approval_invocation_leases(path)
    captured_at = generated_at or datetime.now(timezone.utc)
    if captured_at.tzinfo is None:
        raise ImportBlocked("drain report timestamp must include a timezone")
    captured_at = captured_at.astimezone(timezone.utc)

    approvals = records.get("approval_request", {})
    native_providers = {"ANTIGRAVITY", "CODEX"}
    drain_summary = _native_invocation_drain_summary(approvals, claims)
    native: dict[str, dict[str, Any]] = {}
    for approval_id, approval in approvals.items():
        provider = approval.get("source_provider")
        if provider in native_providers or approval.get("source_invocation_id") is not None:
            native[approval_id] = approval

    status_counts: Counter[str] = Counter()
    pending_fingerprints = list(drain_summary["approval_id_sha256"])
    approved_unconsumed_fingerprints = list(
        drain_summary["approved_unconsumed_approval_id_sha256"]
    )
    unverifiable_fingerprints = list(drain_summary["unverifiable_approval_id_sha256"])
    pending_statuses = {ApprovalStatus.REQUESTED.value, ApprovalStatus.PENDING_REVIEW.value}
    known_statuses = {status.value for status in ApprovalStatus}

    def fingerprint(approval_id: str) -> str:
        return hashlib.sha256(approval_id.encode("utf-8")).hexdigest()

    for approval_id, approval in native.items():
        status = approval.get("status")
        if not isinstance(status, str) or not status:
            status = "UNKNOWN"
        status_counts[status] += 1
        agent_id = approval.get("source_agent_id")
        invocation_id = approval.get("source_invocation_id")
        active = status in pending_statuses or status == ApprovalStatus.APPROVED.value
        identity_valid = (
            isinstance(agent_id, str) and bool(agent_id)
            and isinstance(invocation_id, str) and bool(invocation_id)
        )
        lease = leases.get(approval_id)
        lease_valid = bool(
            identity_valid
            and lease_table_present
            and lease is not None
            and lease.get("workspace_id") == approval.get("workspace_id")
            and lease.get("source_agent_id") == agent_id
            and lease.get("source_invocation_id") == invocation_id
        )
        if lease_valid:
            try:
                lease_expiry = datetime.fromisoformat(lease["expires_at"])
                approval_expiry = datetime.fromisoformat(approval["expires_at"])
                lease_valid = (
                    lease_expiry > captured_at
                    and approval_expiry > captured_at
                    and lease_expiry <= approval_expiry
                )
            except (KeyError, TypeError, ValueError):
                lease_valid = False
        if active and not lease_valid:
            unverifiable_fingerprints.append(fingerprint(approval_id))
        elif status == ApprovalStatus.APPROVED.value:
            claim = claims.get(approval_id)
            claim_matches = bool(
                claim
                and claim.get("workspace_id") == approval.get("workspace_id")
                and claim.get("source_agent_id") == agent_id
                and claim.get("source_invocation_id") == invocation_id
            )
            if not claim_matches:
                approved_unconsumed_fingerprints.append(fingerprint(approval_id))
                if claim is not None and fingerprint(approval_id) not in unverifiable_fingerprints:
                    unverifiable_fingerprints.append(fingerprint(approval_id))
        elif status not in known_statuses:
            unverifiable_fingerprints.append(fingerprint(approval_id))

    pending_fingerprints.sort()
    approved_unconsumed_fingerprints.sort()
    unverifiable_fingerprints = sorted(set(unverifiable_fingerprints))
    final_stat = path.stat()
    final_digest = _sha256_file(path)
    if (
        (initial_stat.st_dev, initial_stat.st_ino, initial_stat.st_size, initial_stat.st_mtime_ns)
        != (final_stat.st_dev, final_stat.st_ino, final_stat.st_size, final_stat.st_mtime_ns)
        or digest != final_digest
    ):
        raise ImportBlocked("source snapshot changed during drain inspection; discard it and create a stable snapshot")

    blocked = bool(
        pending_fingerprints or approved_unconsumed_fingerprints or unverifiable_fingerprints
    )
    return {
        "report_version": 1,
        "generated_at": captured_at.isoformat(),
        "source_snapshot_sha256": digest,
        "source_snapshot_size_bytes": initial_stat.st_size,
        "read_only": True,
        "status": "BLOCKED" if blocked else "READY_FOR_OWNER_REVIEW",
        "migration_authorized": False,
        "native_invocation_approval_count": len(native),
        "native_invocation_status_counts": dict(sorted(status_counts.items())),
        "pending_native_invocation_count": len(pending_fingerprints),
        "pending_approval_id_sha256": pending_fingerprints,
        "approved_unconsumed_invocation_count": len(approved_unconsumed_fingerprints),
        "approved_unconsumed_approval_id_sha256": approved_unconsumed_fingerprints,
        "unverifiable_native_invocation_count": len(unverifiable_fingerprints),
        "unverifiable_approval_id_sha256": unverifiable_fingerprints,
        "invocation_lease_table_present": lease_table_present,
    }


def build_audit_event_inventory_report(path: Path) -> dict[str, Any]:
    """Inventory audit parent/scope evidence from one owner-only SQLite snapshot.

    This helper only reads the local source. It does not connect to PostgreSQL
    and deliberately excludes audit IDs, actors, details, payloads, and tokens.
    Generic ID-based parent matches are labeled so they cannot be mistaken for
    an explicit event-writer contract.
    """
    _validate_source(path)
    initial_stat = path.stat()
    digest = _sha256_file(path)
    _source_table_coverage(path)
    _source_kv_entity_type_counts(path)
    records = _read_records(path)

    workspaces = records.get("workspace", {})
    users = records.get("user", {})
    sessions = records.get("session", {})
    approvals = records.get("approval_request", {})
    tasks = records.get("agent_task", {})
    runs = records.get("agent_run", {})
    devices = records.get("device_registration", {})
    agents = records.get("external_agent", {})
    projects = records.get("project", {})
    work_orders = records.get("work_order", {})
    ideas = records.get("idea", {})
    connectors = records.get("agent_connector", {})
    invites = records.get("invite", {})
    mcp_servers = records.get("mcp_child_server", {})
    mcp_pending = records.get("mcp_pending_request", {})
    test_runs = records.get("test_run", {})

    rows: list[dict[str, Any]] = []
    for _audit_id, event in records.get("audit_event", {}).items():
        event_type = event.get("event_type")
        entity_id = event.get("entity_id")
        workspace_id = event.get("workspace_id")
        if not isinstance(event_type, str) or not isinstance(entity_id, str):
            rows.append({
                "event_type": event_type if isinstance(event_type, str) else None,
                "entity_id": entity_id if isinstance(entity_id, str) else None,
                "workspace_id": workspace_id if isinstance(workspace_id, str) else None,
                "parent_entity_type": None,
                "parent_resolution": "none",
                "parent_exists": False,
                "parent_identity_match": None,
                "parent_workspace_exists": False,
                "workspace_match": False if workspace_id is not None else None,
                "classification": "invalid_event_identity",
            })
            continue
        workspace_id = workspace_id if isinstance(workspace_id, str) else None
        parent_type, parent, resolution = _resolve_audit_event_parent(
            event_type if isinstance(event_type, str) else None,
            entity_id if isinstance(entity_id, str) else None,
            workspace_id=workspace_id,
            sessions=sessions,
            users=users,
            tasks=tasks,
            runs=runs,
            devices=devices,
            agents=agents,
            approvals=approvals,
            projects=projects,
            work_orders=work_orders,
            ideas=ideas,
            connectors=connectors,
            invites=invites,
            workspaces=workspaces,
            mcp_servers=mcp_servers,
            mcp_pending=mcp_pending,
            test_runs=test_runs,
        )
        workspace_scoped_device_lifecycle = resolution in {
            "device_unregister_workspace_scope",
            "device_token_invalidation_workspace_scope",
        }
        parent_exists = parent is not None and not workspace_scoped_device_lifecycle
        # Workspace records are keyed by workspace_id in the source/target
        # persistence contract. The resolver annotates that key explicitly;
        # use it for identity evidence rather than assuming every parent uses
        # the generic model `id` field.
        identity_key = "workspace_id" if parent_type == "workspace" else "id"
        parent_identity = parent.get(identity_key) if parent_exists else None
        parent_identity_match = (
            parent_identity == entity_id
            if isinstance(parent_identity, str)
            else None
        )
        parent_workspace_id = None
        if parent is not None:
            if parent_type == "workspace":
                parent_workspace_id = entity_id
            else:
                candidate_scope = parent.get("workspace_id")
                parent_workspace_id = candidate_scope if isinstance(candidate_scope, str) else None
        parent_workspace_exists = (
            parent_workspace_id in workspaces if parent_workspace_id is not None else False
        )

        if resolution == "path_keyed_without_task_parent":
            classification = "path_keyed_no_parent"
        elif parent is None:
            classification = (
                "unscoped_parent_not_found" if workspace_id is None else "parent_not_found"
            )
        elif parent_exists and parent_identity_match is not True:
            classification = "parent_identity_mismatch_or_missing"
        elif workspace_id is None:
            classification = "event_workspace_missing"
        elif parent_workspace_id is None:
            classification = "parent_workspace_unresolved"
        elif workspace_id != parent_workspace_id:
            classification = "workspace_mismatch"
        elif workspace_id not in workspaces:
            classification = "workspace_not_found_in_snapshot"
        elif workspace_scoped_device_lifecycle:
            classification = "explicit_workspace_scope_without_live_device_parent"
        elif resolution == "entity_id_fallback":
            classification = "same_workspace_parent_via_entity_id_fallback"
        else:
            classification = "same_workspace_parent_verified"

        rows.append({
            "event_type": event_type,
            "entity_id": entity_id,
            "workspace_id": workspace_id,
            "parent_entity_type": parent_type,
            "parent_resolution": resolution,
            "parent_exists": parent_exists,
            "parent_identity_match": parent_identity_match,
            "parent_workspace_exists": parent_workspace_exists,
            "workspace_match": (
                workspace_id == parent_workspace_id
                if workspace_id is not None and parent_workspace_id is not None
                else None
            ),
            "classification": classification,
        })

    final_stat = path.stat()
    final_digest = _sha256_file(path)
    if (
        (initial_stat.st_dev, initial_stat.st_ino, initial_stat.st_size, initial_stat.st_mtime_ns)
        != (final_stat.st_dev, final_stat.st_ino, final_stat.st_size, final_stat.st_mtime_ns)
        or digest != final_digest
    ):
        raise ImportBlocked("source snapshot changed during audit inventory; discard it and create a stable snapshot")
    rows.sort(key=lambda row: (
        row["event_type"] or "",
        row["entity_id"] or "",
        row["workspace_id"] or "",
        row["classification"],
    ))
    return {
        "report_version": 1,
        "source_snapshot_sha256": digest,
        "source_snapshot_size_bytes": initial_stat.st_size,
        "read_only": True,
        "migration_authorized": False,
        "audit_event_count": len(rows),
        "classification_counts": dict(sorted(Counter(row["classification"] for row in rows).items())),
        "events": rows,
    }


def _write_private_report(path: Path, report: dict[str, Any]) -> None:
    """Create an owner-only report without replacing an existing artifact."""
    parent = path.parent
    if not parent.exists() or parent.is_symlink():
        raise ImportBlocked("report parent must be an existing real directory")
    parent_stat = parent.stat()
    if not stat.S_ISDIR(parent_stat.st_mode) or parent_stat.st_uid != os.getuid():
        raise ImportBlocked("report parent must be a directory owned by the current user")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise ImportBlocked("report already exists; choose a new path") from exc
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(json.dumps(report, sort_keys=True, indent=2) + "\n")
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _validate_source(path: Path) -> None:
    if not path.is_file() or path.is_symlink():
        raise ImportBlocked("source must be an existing regular, non-symlink file")
    if path.stat().st_uid != os.getuid():
        raise ImportBlocked("source file must be owned by the current user")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ImportBlocked("source file permissions must be owner-only (0600 or stricter)")
    if Path(str(path) + "-wal").exists() or Path(str(path) + "-shm").exists():
        raise ImportBlocked("SQLite WAL/SHM sidecar exists; create a coordinated snapshot first")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_records(path: Path) -> dict[str, dict[str, dict[str, Any]]]:
    uri = f"file:{quote(path.resolve().as_posix(), safe='/')}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as conn:
        conn.execute("PRAGMA query_only = ON")
        rows = conn.execute(
            "SELECT entity_type, entity_id, payload FROM kv_store ORDER BY entity_type, entity_id"
        ).fetchall()
    records: dict[str, dict[str, dict[str, Any]]] = {}
    for entity_type, entity_id, raw in rows:
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ImportBlocked(f"malformed JSON payload: {entity_type}/{entity_id}") from exc
        if not isinstance(payload, dict):
            raise ImportBlocked(f"non-object payload: {entity_type}/{entity_id}")
        records.setdefault(entity_type, {})[entity_id] = payload
    return records


def _block_unsupported_notifications(records: dict[str, dict[str, dict[str, Any]]]) -> None:
    """Refuse source notification types without a durable PostgreSQL target.

    Diagnostics contain only a count and stable fingerprints, never source IDs,
    entity types, titles, or bodies.
    """
    unsupported = []
    for notification_id, payload in records.get("notification", {}).items():
        if payload.get("entity_type") in {
            "approval_request", "agent_task", "project", "device_registration",
        }:
            continue
        unsupported.append(hashlib.sha256(notification_id.encode("utf-8")).hexdigest())
    if unsupported:
        fingerprints = ",".join(sorted(unsupported))
        raise ImportBlocked(
            "unsupported notification records block PostgreSQL import "
            f"(count={len(unsupported)}; sha256_fingerprints={fingerprints})"
        )


def _agent_run_fault_summary(
    records: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, Any]:
    """Summarize fault settings without exposing their data.

    Active fault injection can alter AgentRun behavior after cutover and blocks
    the plan. Inactive settings are imported so their durable history is kept.
    """
    active: list[str] = []
    inactive: list[str] = []
    for fault_id, payload in records.get("agent_run_fault", {}).items():
        fault = AgentRunFaultConfig.model_validate(payload)
        if fault.id != fault_id:
            raise ImportBlocked(f"agent_run_fault/{fault_id} payload identity does not match its SQLite key")
        if fault.enabled and fault.remaining_uses > 0:
            active.append(hashlib.sha256(fault_id.encode("utf-8")).hexdigest())
        else:
            inactive.append(hashlib.sha256(fault_id.encode("utf-8")).hexdigest())
    active.sort()
    inactive.sort()
    return {
        "active_count": len(active),
        "active_id_sha256": active,
        "inactive_import_count": len(inactive),
        "inactive_import_id_sha256": inactive,
    }


def _prepare_agent_run_faults(
    records: dict[str, dict[str, Any]],
    runs: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Validate inactive fault rows against their exact imported AgentRun parent.

    The legacy payload has no workspace field, so workspace authority comes
    only from the matching AgentRun record. A legacy optional workspace field,
    if present, must agree with that parent rather than override it.
    """
    result: dict[str, dict[str, Any]] = {}
    for fault_id, payload in records.items():
        try:
            fault = AgentRunFaultConfig.model_validate(payload)
        except Exception as error:
            raise ImportBlocked(f"agent_run_fault/{fault_id} has an invalid payload") from error
        if fault.id != fault_id:
            raise ImportBlocked(f"agent_run_fault/{fault_id} payload identity does not match its SQLite key")
        if fault.enabled and fault.remaining_uses > 0:
            raise ImportBlocked(f"agent_run_fault/{fault_id} is active and cannot be imported")
        if not isinstance(fault.run_id, str) or not fault.run_id:
            raise ImportBlocked(f"agent_run_fault/{fault_id} has no supported AgentRun parent")
        raw_parent = runs.get(fault.run_id)
        if not isinstance(raw_parent, dict):
            raise ImportBlocked(f"agent_run_fault/{fault_id} references a missing AgentRun parent")
        try:
            parent = AgentRun.model_validate(raw_parent)
        except Exception as error:
            raise ImportBlocked(f"agent_run_fault/{fault_id} references an invalid AgentRun parent") from error
        if parent.id != fault.run_id or not parent.workspace_id:
            raise ImportBlocked(f"agent_run_fault/{fault_id} references a mismatched or unscoped AgentRun parent")
        stated_workspace = payload.get("workspace_id")
        if stated_workspace is not None and stated_workspace != parent.workspace_id:
            raise ImportBlocked(f"agent_run_fault/{fault_id} workspace does not match its AgentRun parent")
        result[fault_id] = fault.model_dump(mode="json")
    return result


def _read_email_outbox(path: Path) -> tuple[dict[str, dict[str, Any]], int]:
    """Read encrypted outbox rows without decrypting verification material."""
    uri = f"file:{quote(path.resolve().as_posix(), safe='/')}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as conn:
        conn.execute("PRAGMA query_only = ON")
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='auth_email_outbox'"
        ).fetchone()
        if exists is None:
            return {}, 0
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(auth_email_outbox)").fetchall()
        }
        expected = {
            "id", "recipient", "encrypted_code", "status", "attempts", "created_at",
            "next_attempt_at", "lease_expires_at", "sent_at", "last_error",
        }
        if columns != expected:
            raise ImportBlocked("auth_email_outbox schema is unexpected; review source migration state")
        rows = conn.execute(
            "SELECT id,recipient,encrypted_code,status,attempts,created_at,next_attempt_at,"
            "lease_expires_at,sent_at,last_error FROM auth_email_outbox ORDER BY id"
        ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    sanitized_terminal_count = 0
    for row in rows:
        (
            outbox_id, recipient, encrypted_code, status, attempts, created_at,
            next_attempt_at, lease_expires_at, sent_at, last_error,
        ) = row
        if not isinstance(outbox_id, str) or not outbox_id or not isinstance(recipient, str) or not recipient:
            raise ImportBlocked("auth_email_outbox contains an invalid identity")
        if status not in {"PENDING", "SENDING", "SENT", "DEAD"} or not isinstance(attempts, int) or attempts < 0:
            raise ImportBlocked(f"auth_email_outbox/{outbox_id} has invalid delivery state")
        for field_name, value in (("created_at", created_at), ("next_attempt_at", next_attempt_at)):
            try:
                timestamp = datetime.fromisoformat(value)
            except (TypeError, ValueError) as exc:
                raise ImportBlocked(f"auth_email_outbox/{outbox_id} has invalid {field_name}") from exc
            if timestamp.tzinfo is None:
                raise ImportBlocked(f"auth_email_outbox/{outbox_id} has timezone-naive {field_name}")
        for field_name, value in (("lease_expires_at", lease_expires_at), ("sent_at", sent_at)):
            if value is not None:
                try:
                    timestamp = datetime.fromisoformat(value)
                except (TypeError, ValueError) as exc:
                    raise ImportBlocked(f"auth_email_outbox/{outbox_id} has invalid {field_name}") from exc
                if timestamp.tzinfo is None:
                    raise ImportBlocked(f"auth_email_outbox/{outbox_id} has timezone-naive {field_name}")
        encrypted = bytes(encrypted_code or b"")
        if status in {"PENDING", "SENDING"}:
            if len(encrypted) < 29:
                raise ImportBlocked(f"auth_email_outbox/{outbox_id} has malformed active ciphertext")
        elif encrypted:
            # Terminal rows are not deliverable; remove residual ciphertext so it
            # cannot be retained or accidentally reactivated by a later worker.
            encrypted = b""
            sanitized_terminal_count += 1
        result[outbox_id] = {
            "recipient": recipient,
            "encrypted_code": encrypted,
            "status": status,
            "attempts": attempts,
            "created_at": created_at,
            "next_attempt_at": next_attempt_at,
            "lease_expires_at": lease_expires_at,
            "sent_at": sent_at,
            "last_error": last_error,
        }
    return result, sanitized_terminal_count


def _read_delivery_state(
    path: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Read the durable SQLite at-most-once claim and ambiguous-failure tables."""
    uri = f"file:{quote(path.resolve().as_posix(), safe='/')}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as conn:
        conn.execute("PRAGMA query_only = ON")
        available = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('approval_delivery_claims','approval_delivery_failures')"
            )
        }
        claims: dict[str, dict[str, Any]] = {}
        failures: dict[str, dict[str, Any]] = {}
        for table, target, required in (
            (
                "approval_delivery_claims",
                claims,
                {"approval_id", "workspace_id", "source_agent_id", "source_invocation_id", "claimed_at"},
            ),
            (
                "approval_delivery_failures",
                failures,
                {"approval_id", "workspace_id", "source_agent_id", "source_invocation_id", "reason_code", "reported_at"},
            ),
        ):
            if table not in available:
                continue
            columns = {
                row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if columns != required:
                raise ImportBlocked(f"{table} schema is unexpected; review source migration state")
            order = "claimed_at" if table == "approval_delivery_claims" else "reported_at"
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY approval_id").fetchall()
            names = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            for row in rows:
                payload = dict(zip(names, row, strict=True))
                entity_id = payload.get("approval_id")
                if not isinstance(entity_id, str) or not entity_id:
                    raise ImportBlocked(f"{table} contains an invalid approval identity")
                timestamp = payload.get(order)
                try:
                    parsed = datetime.fromisoformat(timestamp)
                except (TypeError, ValueError) as error:
                    raise ImportBlocked(f"{table}/{entity_id} has an invalid {order}") from error
                if parsed.tzinfo is None:
                    raise ImportBlocked(f"{table}/{entity_id} has a timezone-naive {order}")
                for field in required - {order, "reason_code"}:
                    value = payload.get(field)
                    if not isinstance(value, str) or not value or len(value) > 256:
                        raise ImportBlocked(f"{table}/{entity_id} has an invalid {field}")
                if table == "approval_delivery_failures" and payload.get("reason_code") != "CLAIM_RESPONSE_AMBIGUOUS":
                    raise ImportBlocked(f"{table}/{entity_id} has an unsupported reason_code")
                target[entity_id] = {key: payload[key] for key in required}
    return claims, failures


def _read_approval_invocation_leases(
    path: Path,
) -> tuple[dict[str, dict[str, Any]], bool]:
    """Read the complete scoped invocation-lease relation from SQLite."""
    uri = f"file:{quote(path.resolve().as_posix(), safe='/')}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as conn:
        conn.execute("PRAGMA query_only = ON")
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='approval_invocation_leases'"
        ).fetchone()
        if exists is None:
            return {}, False
        columns = [
            row[1] for row in conn.execute("PRAGMA table_info(approval_invocation_leases)").fetchall()
        ]
        required = [
            "approval_id", "workspace_id", "source_agent_id", "source_invocation_id",
            "heartbeat_at", "expires_at",
        ]
        if columns != required:
            raise ImportBlocked(
                "approval_invocation_leases schema is unexpected; review source migration state"
            )
        rows = conn.execute(
            "SELECT approval_id,workspace_id,source_agent_id,source_invocation_id,heartbeat_at,expires_at "
            "FROM approval_invocation_leases ORDER BY approval_id"
        ).fetchall()
    leases: dict[str, dict[str, Any]] = {}
    invocation_keys: set[tuple[str, str, str]] = set()
    for approval_id, workspace_id, source_agent_id, source_invocation_id, heartbeat_at, expires_at in rows:
        values = {
            "approval_id": approval_id,
            "workspace_id": workspace_id,
            "source_agent_id": source_agent_id,
            "source_invocation_id": source_invocation_id,
        }
        if any(
            not isinstance(value, str) or not value or len(value) > 256
            for value in values.values()
        ):
            raise ImportBlocked("approval_invocation_leases contains an invalid identity")
        try:
            heartbeat = datetime.fromisoformat(heartbeat_at)
            expiry = datetime.fromisoformat(expires_at)
        except (TypeError, ValueError) as exc:
            raise ImportBlocked(f"approval_invocation_lease/{approval_id} has invalid timestamps") from exc
        if heartbeat.tzinfo is None or expiry.tzinfo is None or expiry < heartbeat:
            raise ImportBlocked(f"approval_invocation_lease/{approval_id} has invalid lease time bounds")
        unique_invocation = (workspace_id, source_agent_id, source_invocation_id)
        if unique_invocation in invocation_keys:
            raise ImportBlocked("approval_invocation_leases contains a duplicate scoped invocation")
        invocation_keys.add(unique_invocation)
        leases[approval_id] = {
            **values,
            "heartbeat_at": heartbeat.isoformat(),
            "expires_at": expiry.isoformat(),
        }
    return leases, True


def _read_auth_rate_limit_buckets(path: Path) -> dict[str, dict[str, int | str]]:
    """Read hashed fixed-window auth counters without exposing identifiers."""
    uri = f"file:{quote(path.resolve().as_posix(), safe='/')}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as conn:
        conn.execute("PRAGMA query_only = ON")
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='auth_rate_limit_buckets'"
        ).fetchone()
        if exists is None:
            return {}
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(auth_rate_limit_buckets)").fetchall()
        }
        expected = {"bucket_hash", "window_start", "request_count"}
        if columns != expected:
            raise ImportBlocked(
                "auth_rate_limit_buckets schema is unexpected; review source migration state"
            )
        rows = conn.execute(
            "SELECT bucket_hash,window_start,request_count FROM auth_rate_limit_buckets "
            "ORDER BY bucket_hash,window_start"
        ).fetchall()
    result: dict[str, dict[str, int | str]] = {}
    for bucket_hash, window_start, request_count in rows:
        if not isinstance(bucket_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", bucket_hash):
            raise ImportBlocked("auth_rate_limit_buckets contains an invalid bucket digest")
        if (
            not isinstance(window_start, int)
            or isinstance(window_start, bool)
            or window_start < 0
            or not isinstance(request_count, int)
            or isinstance(request_count, bool)
            or request_count <= 0
        ):
            raise ImportBlocked(f"auth_rate_limit_buckets/{bucket_hash} has invalid counter state")
        key = f"{bucket_hash}:{window_start}"
        result[key] = {
            "bucket_hash": bucket_hash,
            "window_start": window_start,
            "request_count": request_count,
        }
    return result


def build_plan(path: Path) -> tuple[dict[str, Any], dict[str, dict[str, dict[str, Any]]], str]:
    _validate_source(path)
    initial_stat = path.stat()
    digest = _sha256_file(path)
    source_table_coverage = _source_table_coverage(path)
    kv_source_counts = _source_kv_entity_type_counts(path)
    report = audit(path)
    if report.get("status") != "STRUCTURAL_REVIEW_PASSED":
        unresolved_worker_audits = [
            finding
            for finding in report.get("record_findings", [])
            if "worker_audit_parent_unresolved" in finding.get("findings", [])
        ]
        if unresolved_worker_audits:
            first = unresolved_worker_audits[0]
            raise ImportBlocked(
                "worker audit event has no supported AgentTask parent "
                f"(audit_event/{first.get('entity_id')}); review audit findings"
            )
        raise ImportBlocked("read-only structural audit did not pass; review its findings first")
    records = _read_records(path)
    _block_unsupported_notifications(records)
    agent_run_faults = _agent_run_fault_summary(records)
    if agent_run_faults["active_count"]:
        fingerprints = ",".join(agent_run_faults["active_id_sha256"])
        raise ImportBlocked(
            "active AgentRun fault injection settings block PostgreSQL import "
            f"(count={agent_run_faults['active_count']}; sha256_fingerprints={fingerprints}); "
            "disable or remove each setting in the source, create a new coordinated snapshot, "
            "and rerun the dry-run plan"
        )
    selected = {key: value for key, value in records.items() if key in CORE_TYPES}
    # kv_store has its own composite key in addition to the entity's payload
    # identity. Most entity maps are keyed by payload.id. External agent
    # credentials are intentionally keyed by agent_id in SQLite, however;
    # their normalized PostgreSQL primary key is payload.id (credential_id).
    # Validate that documented relationship rather than weakening the
    # identity guard for other entity kinds.
    for entity_type in ENTITY_TYPES:
        for entity_id, payload in selected.get(entity_type, {}).items():
            payload_id = payload.get("id")
            if entity_type == "external_agent_credential":
                identity_matches = (
                    isinstance(payload_id, str)
                    and bool(payload_id)
                    and payload.get("agent_id") == entity_id
                )
            else:
                identity_matches = isinstance(payload_id, str) and payload_id == entity_id
            if not identity_matches:
                raise ImportBlocked(
                    f"{entity_type}/{entity_id} payload identity does not match its SQLite key"
                )
    if records.get("approval_invocation_lease"):
        raise ImportBlocked(
            "approval_invocation_lease rows must come from the normalized SQLite lease table"
        )
    email_outbox, sanitized_terminal_email_ciphertexts = _read_email_outbox(path)
    selected["auth_email_outbox"] = email_outbox
    delivery_claims, delivery_failures = _read_delivery_state(path)
    selected["approval_delivery_claim"] = delivery_claims
    selected["approval_delivery_failure"] = delivery_failures
    invocation_leases, lease_table_present = _read_approval_invocation_leases(path)
    selected["approval_invocation_lease"] = invocation_leases
    selected["auth_rate_limit_bucket"] = _read_auth_rate_limit_buckets(path)
    # No inferred scope is accepted for records that establish imported ownership.
    for entity_type in ("user", "project", "approval_request", "agent_task", "notification"):
        for entity_id, payload in selected.get(entity_type, {}).items():
            if not payload.get("workspace_id"):
                raise ImportBlocked(f"{entity_type}/{entity_id} has no explicit workspace_id")
    workspaces = selected.get("workspace", {})
    users = selected.get("user", {})
    sessions = selected.get("session", {})
    projects = selected.get("project", {})
    ideas = selected.get("idea", {})
    work_orders = selected.get("work_order", {})
    agents = selected.get("external_agent", {})
    connectors = selected.get("agent_connector", {})
    credentials = selected.get("external_agent_credential", {})
    devices = selected.get("device_registration", {})
    derived_workspace_scope_counts: Counter[str] = Counter()
    approvals = selected.get("approval_request", {})
    invocation_drain = _native_invocation_drain_summary(approvals, delivery_claims)
    if (
        invocation_drain["pending_native_invocation_count"]
        or invocation_drain["approved_unconsumed_invocation_count"]
        or invocation_drain["unverifiable_native_invocation_count"]
    ):
        safe_fingerprints = ",".join(
            invocation_drain["approval_id_sha256"]
            + invocation_drain["approved_unconsumed_approval_id_sha256"]
            + invocation_drain["unverifiable_approval_id_sha256"]
        )
        raise ImportBlocked(
            "source snapshot contains native invocations that must drain before import: "
            f"{invocation_drain['pending_native_invocation_count']} pending native invocation approval(s), "
            f"{invocation_drain['approved_unconsumed_invocation_count']} approved but unconsumed native invocation(s), "
            f"{invocation_drain['unverifiable_native_invocation_count']} unverifiable native invocation(s); "
            "complete or explicitly reject/expire them through the owner flow "
            "before creating the migration snapshot; approval_id_sha256="
            f"{safe_fingerprints}"
        )
    for approval_id, lease in invocation_leases.items():
        approval = approvals.get(approval_id)
        agent = agents.get(lease["source_agent_id"])
        if (
            not approval
            or approval_id != lease["approval_id"]
            or approval.get("workspace_id") != lease["workspace_id"]
            or approval.get("source_agent_id") != lease["source_agent_id"]
            or approval.get("source_invocation_id") != lease["source_invocation_id"]
            or not agent
            or agent.get("workspace_id") != lease["workspace_id"]
        ):
            raise ImportBlocked(
                f"approval_invocation_lease/{approval_id} does not match its approval and agent scope"
            )
        try:
            approval_expiry = datetime.fromisoformat(approval["expires_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ImportBlocked(
                f"approval_invocation_lease/{approval_id} references an approval with invalid expiry"
            ) from exc
        lease_expiry = datetime.fromisoformat(lease["expires_at"])
        heartbeat = datetime.fromisoformat(lease["heartbeat_at"])
        if (
            approval_expiry.tzinfo is None
            or heartbeat > approval_expiry
            or lease_expiry > approval_expiry
        ):
            raise ImportBlocked(
                f"approval_invocation_lease/{approval_id} exceeds its approval expiry"
            )
    tasks = selected.get("agent_task", {})
    task_events = selected.get("agent_task_event", {})
    runs = selected.get("agent_run", {})
    selected["agent_run_fault"] = _prepare_agent_run_faults(
        records.get("agent_run_fault", {}), runs
    )
    run_steps = selected.get("agent_run_step", {})
    run_events = selected.get("agent_run_event", {})
    leases = selected.get("capability_lease", {})
    attestations = selected.get("reviewer_attestation", {})
    actions = selected.get("proposed_action", {})
    decisions = selected.get("policy_decision", {})
    authorizations = selected.get("action_authorization", {})
    grants = selected.get("credential_grant", {})
    trust_events = selected.get("trust_event", {})
    mcp_servers = selected.get("mcp_child_server", {})
    mcp_pending = selected.get("mcp_pending_request", {})
    test_runs = selected.get("test_run", {})
    for approval_id, claim in delivery_claims.items():
        approval = approvals.get(approval_id)
        if not approval:
            raise ImportBlocked(f"approval_delivery_claim/{approval_id} references a missing approval")
        if (
            approval.get("status") != ApprovalStatus.APPROVED.value
            or claim["workspace_id"] != approval.get("workspace_id")
            or claim["source_agent_id"] != approval.get("source_agent_id")
            or claim["source_invocation_id"] != approval.get("source_invocation_id")
        ):
            raise ImportBlocked(f"approval_delivery_claim/{approval_id} does not match its approved invocation")
        agent = agents.get(claim["source_agent_id"])
        if not agent or agent.get("workspace_id") != claim["workspace_id"]:
            raise ImportBlocked(f"approval_delivery_claim/{approval_id} references a missing or mismatched agent")
    for approval_id, failure in delivery_failures.items():
        claim = delivery_claims.get(approval_id)
        if not claim or any(
            failure[field] != claim[field]
            for field in ("workspace_id", "source_agent_id", "source_invocation_id")
        ):
            raise ImportBlocked(f"approval_delivery_failure/{approval_id} has no matching durable claim")
    for entity_id, payload in users.items():
        if payload.get("workspace_id") not in workspaces:
            raise ImportBlocked(f"user/{entity_id} references a missing workspace")
    for workspace_id, payload in workspaces.items():
        owner_id = payload.get("owner_user_id")
        owner = users.get(owner_id)
        if not owner or owner.get("workspace_id") != workspace_id:
            raise ImportBlocked(f"workspace/{workspace_id} has no matching imported owner")
    for session_id, payload in sessions.items():
        if payload.get("user_id") not in users:
            raise ImportBlocked(f"session/{session_id} references a missing imported user")
    for invite_id, payload in selected.get("invite", {}).items():
        workspace_id = payload.get("workspace_id")
        if not workspace_id or workspace_id not in workspaces:
            raise ImportBlocked(f"invite/{invite_id} has no explicit imported workspace")
        creator = users.get(payload.get("created_by_user_id"))
        if not creator or creator.get("workspace_id") != workspace_id:
            raise ImportBlocked(f"invite/{invite_id} has a missing or cross-workspace creator")
        acceptor_id = payload.get("accepted_by_user_id")
        if acceptor_id:
            acceptor = users.get(acceptor_id)
            if not acceptor or acceptor.get("workspace_id") != workspace_id:
                raise ImportBlocked(f"invite/{invite_id} has a missing or cross-workspace acceptor")
    for agent_id, payload in agents.items():
        workspace_id = payload.get("workspace_id")
        if workspace_id not in workspaces:
            raise ImportBlocked(f"external_agent/{agent_id} references a missing workspace")
        creator_id = payload.get("created_by_user_id")
        if creator_id and (
            creator_id not in users or users[creator_id].get("workspace_id") != workspace_id
        ):
            raise ImportBlocked(f"external_agent/{agent_id} has an unimported or mismatched creator")
    for agent_id, payload in agents.items():
        if any(
            project_id not in projects
            or projects[project_id].get("workspace_id") != payload.get("workspace_id")
            for project_id in payload.get("allowed_project_ids", [])
        ):
            raise ImportBlocked(f"external_agent/{agent_id} has an unimported or cross-workspace project grant")
    for connector_id, payload in connectors.items():
        workspace_id = payload.get("workspace_id")
        if not workspace_id or workspace_id not in workspaces:
            raise ImportBlocked(f"agent_connector/{connector_id} has no explicit imported workspace")
        creator_id = payload.get("created_by_user_id")
        if not creator_id or creator_id not in users or users[creator_id].get("workspace_id") != workspace_id:
            raise ImportBlocked(f"agent_connector/{connector_id} has a missing or cross-workspace owner")
        if any(
            project_id not in projects
            or projects[project_id].get("workspace_id") != workspace_id
            for project_id in payload.get("allowed_project_ids", [])
        ):
            raise ImportBlocked(f"agent_connector/{connector_id} has a missing or cross-workspace project grant")
    for credential_id, payload in credentials.items():
        agent = agents.get(payload.get("agent_id"))
        if not agent:
            raise ImportBlocked(f"external_agent_credential/{credential_id} references a missing agent")
    for device_id, payload in devices.items():
        user = users.get(payload.get("user_id"))
        if not user or user.get("workspace_id") != payload.get("workspace_id"):
            raise ImportBlocked(f"device_registration/{device_id} has an unimported or mismatched user")
    for entity_id, payload in projects.items():
        workspace = workspaces.get(payload.get("workspace_id"))
        if not workspace:
            raise ImportBlocked(f"project/{entity_id} references a missing workspace")
    # Resolve optional legacy workspace scope only from the exact linked project.
    for idea_id, payload in ideas.items():
        project_id = payload.get("project_id")
        project = projects.get(project_id) if project_id else None
        workspace_id = payload.get("workspace_id") or (project or {}).get("workspace_id")
        if not workspace_id or workspace_id not in workspaces:
            raise ImportBlocked(f"idea/{idea_id} has no resolvable workspace")
        if project_id and (not project or project.get("workspace_id") != workspace_id):
            raise ImportBlocked(f"idea/{idea_id} has a missing or mismatched project")
        if payload.get("workspace_id") and payload["workspace_id"] != workspace_id:
            raise ImportBlocked(f"idea/{idea_id} has mismatched workspace ownership")
        if not payload.get("workspace_id"):
            derived_workspace_scope_counts["idea"] += 1
        payload["workspace_id"] = workspace_id
    for work_order_id, payload in work_orders.items():
        project = projects.get(payload.get("project_id"))
        workspace_id = payload.get("workspace_id") or (project or {}).get("workspace_id")
        if not project or project.get("workspace_id") != workspace_id:
            raise ImportBlocked(f"work_order/{work_order_id} has a missing or mismatched project")
        if not payload.get("workspace_id"):
            derived_workspace_scope_counts["work_order"] += 1
        payload["workspace_id"] = workspace_id
        idea_id = payload.get("idea_id")
        if idea_id and (
            idea_id not in ideas or ideas[idea_id].get("project_id") != payload.get("project_id")
        ):
            raise ImportBlocked(f"work_order/{work_order_id} has a missing or mismatched idea")
        source_agent_id = payload.get("source_agent_id")
        if source_agent_id and (
            source_agent_id not in agents
            or agents[source_agent_id].get("workspace_id") != workspace_id
        ):
            raise ImportBlocked(f"work_order/{work_order_id} has an unimported or mismatched source agent")
    for entity_id, payload in approvals.items():
        project = projects.get(payload.get("project_id"))
        if not project or project.get("workspace_id") != payload.get("workspace_id"):
            raise ImportBlocked(f"approval_request/{entity_id} has missing or mismatched project scope")
        work_order_id = payload.get("work_order_id")
        if work_order_id and (
            work_order_id not in work_orders
            or work_orders[work_order_id].get("project_id") != payload.get("project_id")
            or work_orders[work_order_id].get("workspace_id") != payload.get("workspace_id")
        ):
            raise ImportBlocked(f"approval_request/{entity_id} has a missing or mismatched work order")
        source_agent_id = payload.get("source_agent_id")
        if source_agent_id and (
            source_agent_id not in agents
            or agents[source_agent_id].get("workspace_id") != payload.get("workspace_id")
        ):
            raise ImportBlocked(f"approval_request/{entity_id} has an unimported or mismatched source agent")
    for server_id, payload in mcp_servers.items():
        project = projects.get(payload.get("project_id"))
        workspace_id = payload.get("workspace_id") or (project or {}).get("workspace_id")
        if not project or not workspace_id or project.get("workspace_id") != workspace_id:
            raise ImportBlocked(f"mcp_child_server/{server_id} has missing or mismatched project scope")
        if not payload.get("workspace_id"):
            derived_workspace_scope_counts["mcp_child_server"] += 1
        payload["workspace_id"] = workspace_id
        MCPChildServer.model_validate(payload)
    for pending_id, payload in mcp_pending.items():
        project = projects.get(payload.get("project_id"))
        approval = approvals.get(payload.get("approval_request_id"))
        workspace_id = payload.get("workspace_id") or (project or {}).get("workspace_id")
        if (
            not project or not approval or not workspace_id
            or project.get("workspace_id") != workspace_id
            or approval.get("workspace_id") != workspace_id
            or approval.get("project_id") != payload.get("project_id")
        ):
            raise ImportBlocked(f"mcp_pending_request/{pending_id} has missing or mismatched approval scope")
        if not payload.get("workspace_id"):
            derived_workspace_scope_counts["mcp_pending_request"] += 1
        payload["workspace_id"] = workspace_id
        pending_model = MCPPendingRequest.model_validate(payload)
        if (
            (pending_model.status.value == "FORWARDING") != (pending_model.lease_owner is not None)
            or (pending_model.lease_owner is None) != (pending_model.lease_expires_at is None)
        ):
            raise ImportBlocked(f"mcp_pending_request/{pending_id} has inconsistent forwarding lease state")
    for test_run_id, payload in test_runs.items():
        approval = approvals.get(payload.get("approval_request_id"))
        if not approval:
            raise ImportBlocked(f"test_run/{test_run_id} references a missing approval")
        project_id = payload.get("project_id")
        if project_id and project_id != approval.get("project_id"):
            raise ImportBlocked(f"test_run/{test_run_id} project does not match its approval")
        TestRun.model_validate(payload)
    for entity_id, payload in tasks.items():
        project = projects.get(payload.get("project_id"))
        if not project or project.get("workspace_id") != payload.get("workspace_id"):
            raise ImportBlocked(f"agent_task/{entity_id} has missing or mismatched project scope")
        approval_id = payload.get("approval_request_id")
        if approval_id and (approval_id not in approvals or approvals[approval_id].get("workspace_id") != payload.get("workspace_id")):
            raise ImportBlocked(f"agent_task/{entity_id} has missing or mismatched approval link")
        external_agent_id = payload.get("external_agent_id")
        if external_agent_id and (
            external_agent_id not in agents
            or agents[external_agent_id].get("workspace_id") != payload.get("workspace_id")
        ):
            raise ImportBlocked(f"agent_task/{entity_id} has an unimported or mismatched external agent")
    for event_id, payload in task_events.items():
        task = tasks.get(payload.get("task_id"))
        if not task:
            raise ImportBlocked(f"agent_task_event/{event_id} references an unimported task")
    for step_id, payload in run_steps.items():
        run = runs.get(payload.get("run_id"))
        if not run or run.get("task_id") != payload.get("task_id"):
            raise ImportBlocked(f"agent_run_step/{step_id} has a missing or mismatched run/task")
    for event_id, payload in run_events.items():
        run = runs.get(payload.get("run_id"))
        if not run or run.get("task_id") != payload.get("task_id"):
            raise ImportBlocked(f"agent_run_event/{event_id} has a missing or mismatched run/task")
        step_id = payload.get("step_id")
        if step_id:
            step = run_steps.get(step_id)
            if not step or step.get("run_id") != payload.get("run_id"):
                raise ImportBlocked(f"agent_run_event/{event_id} references a missing or mismatched step")
    # Run and trust records are immutable source records: never infer or insert
    # workspace scope into their payloads, because action hashes and event hashes
    # bind those exact serialized values.
    explicit_scope_types = (
        "agent_run", "capability_lease", "reviewer_attestation", "proposed_action",
        "action_authorization", "credential_grant",
    )
    for entity_type in explicit_scope_types:
        for entity_id, payload in selected.get(entity_type, {}).items():
            workspace_id = payload.get("workspace_id")
            if not workspace_id or workspace_id not in workspaces:
                raise ImportBlocked(
                    f"{entity_type}/{entity_id} requires an explicit imported workspace_id"
                )

    def require_parent_workspace(
        child_type: str, child_id: str, parent_type: str, parent_id: str | None,
        workspace_id: str,
    ) -> dict[str, Any]:
        parent = selected.get(parent_type, {}).get(parent_id or "")
        if not parent or parent.get("workspace_id") != workspace_id:
            raise ImportBlocked(
                f"{child_type}/{child_id} references a missing or cross-workspace "
                f"{parent_type}/{parent_id}"
            )
        return parent

    for run_id, payload in runs.items():
        workspace_id = payload["workspace_id"]
        task = require_parent_workspace(
            "agent_run", run_id, "agent_task", payload.get("task_id"), workspace_id
        )
        require_parent_workspace(
            "agent_run", run_id, "project", payload.get("project_id"), workspace_id
        )
        approval_id = payload.get("approval_request_id")
        if approval_id:
            require_parent_workspace("agent_run", run_id, "approval_request", approval_id, workspace_id)
            if task.get("approval_request_id") not in (None, approval_id):
                raise ImportBlocked(f"agent_run/{run_id} approval does not match its task")
        lease_id = payload.get("capability_lease_id")
        if lease_id:
            require_parent_workspace("agent_run", run_id, "capability_lease", lease_id, workspace_id)

    for lease_id, payload in leases.items():
        workspace_id = payload["workspace_id"]
        lease = CapabilityLease.model_validate(payload)
        approval_payload = require_parent_workspace(
            "capability_lease", lease_id, "approval_request",
            payload.get("approval_request_id"), workspace_id,
        )
        approval_model = ApprovalRequest.model_validate(approval_payload)
        if (
            approval_model.status.value != "APPROVED"
            or not approval_model.approved_payload_hash
            or approval_model.approved_payload_hash != compute_approval_payload_hash(approval_model)
            or lease.approved_payload_hash != approval_model.approved_payload_hash
        ):
            raise ImportBlocked(f"capability_lease/{lease_id} is not bound to a valid approved payload")
        require_parent_workspace(
            "capability_lease", lease_id, "user", payload.get("issued_by_user_id"), workspace_id
        )
        agent_id = payload.get("agent_id")
        if agent_id:
            require_parent_workspace("capability_lease", lease_id, "external_agent", agent_id, workspace_id)
        if not verify_payload_signature(lease_receipt_payload(lease), lease.receipt_signature):
            raise ImportBlocked(f"capability_lease/{lease_id} receipt signature is invalid")
    for attestation_id, payload in attestations.items():
        workspace_id = payload["workspace_id"]
        attestation = ReviewerAttestation.model_validate(payload)
        approval_payload = require_parent_workspace(
            "reviewer_attestation", attestation_id, "approval_request",
            payload.get("approval_request_id"), workspace_id,
        )
        approval_model = ApprovalRequest.model_validate(approval_payload)
        if (
            not approval_model.approved_payload_hash
            or approval_model.approved_payload_hash != attestation.approval_payload_hash
            or approval_model.approved_payload_hash != compute_approval_payload_hash(approval_model)
        ):
            raise ImportBlocked(f"reviewer_attestation/{attestation_id} approval hash is invalid")
        require_parent_workspace(
            "reviewer_attestation", attestation_id, "user", payload.get("reviewer_user_id"), workspace_id
        )
        attestation_material = {
            "approval_request_id": attestation.approval_request_id,
            "approval_payload_hash": attestation.approval_payload_hash,
            "reviewer_user_id": attestation.reviewer_user_id,
            "reviewer_role": attestation.reviewer_role,
            "decision": attestation.decision,
            "reason": attestation.reason,
        }
        if not verify_payload_signature(attestation_material, attestation.signature):
            raise ImportBlocked(f"reviewer_attestation/{attestation_id} signature is invalid")
    for action_id, payload in actions.items():
        workspace_id = payload["workspace_id"]
        for parent_type, field in (
            ("capability_lease", "lease_id"), ("project", "project_id"),
            ("agent_task", "task_id"), ("agent_run", "run_id"),
        ):
            parent_id = payload.get(field)
            if parent_id:
                require_parent_workspace("proposed_action", action_id, parent_type, parent_id, workspace_id)
        if payload.get("run_id") and payload.get("task_id"):
            run = runs[payload["run_id"]]
            if run.get("task_id") != payload.get("task_id"):
                raise ImportBlocked(f"proposed_action/{action_id} task does not match its run")
    for decision_id, payload in decisions.items():
        action_id = payload.get("proposed_action_id")
        action_workspace = actions.get(action_id, {}).get("workspace_id", "")
        action = require_parent_workspace(
            "policy_decision", decision_id, "proposed_action",
            action_id,
            action_workspace,
        )
        workspace_id = action["workspace_id"]
        # PolicyDecision has no workspace field. Carry the verified parent scope
        # only in the import plan so related authorization checks can use it;
        # Pydantic serialization strips this non-model field before storage.
        payload["workspace_id"] = workspace_id
        if payload.get("lease_id") != action.get("lease_id"):
            raise ImportBlocked(f"policy_decision/{decision_id} lease does not match its action")
    for authorization_id, payload in authorizations.items():
        workspace_id = payload["workspace_id"]
        authorization = ActionAuthorization.model_validate(payload)
        action = require_parent_workspace(
            "action_authorization", authorization_id, "proposed_action",
            payload.get("action_id"), workspace_id,
        )
        decision = require_parent_workspace(
            "action_authorization", authorization_id, "policy_decision",
            payload.get("policy_decision_id"), workspace_id,
        )
        require_parent_workspace(
            "action_authorization", authorization_id, "user",
            payload.get("reviewer_user_id"), workspace_id,
        )
        if decision.get("proposed_action_id") != action.get("id"):
            raise ImportBlocked(f"action_authorization/{authorization_id} decision targets another action")
        proposed_action = ProposedAction.model_validate(action)
        if (
            authorization.action_payload_hash != action_payload_hash(proposed_action)
            or not verify_payload_signature(
                authorization_signature_payload(authorization), authorization.signature
            )
        ):
            raise ImportBlocked(f"action_authorization/{authorization_id} signature or action hash is invalid")
    for grant_id, payload in grants.items():
        require_parent_workspace(
            "credential_grant", grant_id, "capability_lease",
            payload.get("lease_id"), payload["workspace_id"],
        )

    # ActionConsumption has no own ID, workspace, or timestamp in SQLite. Its
    # durable target timestamp is taken only from the unique hash-verified
    # ACTION_CONSUMED event for the same action; never synthesize one.
    selected_consumptions: dict[str, dict[str, Any]] = {}
    consumed_events: Counter[str] = Counter()
    for event_id, payload in trust_events.items():
        if payload.get("event_type") == "ACTION_CONSUMED" and payload.get("entity_type") == "proposed_action":
            consumed_events[str(payload.get("entity_id"))] += 1
    for action_id, raw_consumption in records.get("action_consumption", {}).items():
        action = actions.get(action_id)
        if not action or not action.get("lease_id"):
            raise ImportBlocked(f"action_consumption/{action_id} has no imported action and lease")
        matching = [
            event for event in trust_events.values()
            if event.get("event_type") == "ACTION_CONSUMED"
            and event.get("entity_type") == "proposed_action"
            and event.get("entity_id") == action_id
        ]
        if consumed_events[action_id] != 1 or len(matching) != 1:
            raise ImportBlocked(
                f"action_consumption/{action_id} requires exactly one matching verified trust event"
            )
        event = matching[0]
        if event.get("workspace_id") not in (None, action.get("workspace_id")):
            raise ImportBlocked(f"action_consumption/{action_id} trust event has mismatched workspace")
        try:
            consumption = ActionConsumption.model_validate(raw_consumption)
        except Exception as error:
            raise ImportBlocked(f"action_consumption/{action_id} payload is invalid") from error
        selected_consumptions[action_id] = {
            "action_id": action_id,
            "workspace_id": action["workspace_id"],
            "lease_id": action["lease_id"],
            "consumed_at": event["created_at"],
            "consumption": consumption.model_dump(mode="json"),
        }
    selected["action_consumption"] = selected_consumptions
    linked: Counter[str] = Counter(
        str(payload["approval_request_id"]) for payload in tasks.values() if payload.get("approval_request_id")
    )
    if any(count > 1 for count in linked.values()):
        raise ImportBlocked("more than one AgentTask links to an approval; target schema supports one")

    # Live audit events must have a supported, workspace-verified parent.
    # Unlinked history is kept in a separate immutable table and never receives
    # an inferred parent or enters the live audit query surface.
    supported_audits: dict[str, dict[str, Any]] = {}
    legacy_audits: dict[str, dict[str, Any]] = {}
    for event_id, payload in records.get("audit_event", {}).items():
        entity_id = payload.get("entity_id")
        event_type = payload.get("event_type")
        entity_type, parent, resolution = _resolve_audit_event_parent(
            event_type if isinstance(event_type, str) else None,
            entity_id if isinstance(entity_id, str) else None,
            workspace_id=payload.get("workspace_id") if isinstance(payload.get("workspace_id"), str) else None,
            sessions=sessions,
            users=users,
            tasks=tasks,
            runs=runs,
            devices=devices,
            agents=agents,
            approvals=approvals,
            projects=projects,
            work_orders=work_orders,
            ideas=ideas,
            connectors=connectors,
            invites=selected.get("invite", {}),
            workspaces=workspaces,
            mcp_servers=mcp_servers,
            mcp_pending=mcp_pending,
            test_runs=test_runs,
        )
        if resolution == "path_keyed_without_task_parent":
            raise ImportBlocked(
                f"audit_event/{event_id} is a path-keyed worker configuration event without a task parent"
            )
        if resolution == "missing_session_owner":
            raise ImportBlocked(
                f"audit_event/{event_id} references a missing or mismatched session owner"
            )
        if resolution == "missing_test_run_approval":
            raise ImportBlocked(f"audit_event/{event_id} references an unimported test-run approval")
        if event_type == "auth.session_revoked" and parent is not None and (
            parent.get("workspace_id") != payload.get("workspace_id")
        ):
            raise ImportBlocked(
                f"audit_event/{event_id} references a missing or mismatched session owner"
            )
        if entity_type == "device_registration" and parent is None:
            raise ImportBlocked(
                f"audit_event/{event_id} references an unscoped device lifecycle event"
            )
        details = payload.get("details")
        details = details if isinstance(details, dict) else {}
        auth_agent_id = details.get("agent_id") or entity_id
        is_unscoped_unknown_auth_failure = (
            event_type == "agent.auth_failed"
            and payload.get("workspace_id") is None
            and isinstance(auth_agent_id, str)
            and auth_agent_id not in agents
        )
        if entity_type == "external_agent" and parent is None and not is_unscoped_unknown_auth_failure:
            raise ImportBlocked(
                f"audit_event/{event_id} references an unimported external agent"
            )
        if isinstance(event_type, str) and event_type.startswith("agent_worker.") and parent is None:
            raise ImportBlocked(
                f"audit_event/{event_id} references an unimported AgentTask worker parent"
            )
        if parent is None or entity_type is None:
            if event_type in {"approval.delivery_claimed", "approval.delivery_failed"}:
                raise ImportBlocked(
                    f"audit_event/{event_id} is an unresolved sensitive approval delivery event"
                )
            workspace_id = payload.get("workspace_id")
            if (not isinstance(workspace_id, str) or not workspace_id) and not is_unscoped_unknown_auth_failure:
                raise ImportBlocked(
                    f"audit_event/{event_id} has no explicit workspace for legacy preservation"
                )
            if workspace_id is not None and workspace_id not in workspaces:
                raise ImportBlocked(
                    f"audit_event/{event_id} workspace is missing or not included in this snapshot"
                )
            try:
                legacy_model = AuditEvent.model_validate(payload)
            except Exception as error:
                raise ImportBlocked(f"audit_event/{event_id} is invalid legacy history") from error
            legacy_audits[event_id] = {
                "source_sha256": digest,
                "event_id": event_id,
                "workspace_id": workspace_id,
                "quarantine_reason": (
                    "UNSCOPED_UNKNOWN_AGENT_AUTH_FAILURE"
                    if is_unscoped_unknown_auth_failure
                    else "UNLINKED_HISTORY_WITH_VERIFIED_WORKSPACE"
                ),
                "original_entity_id": legacy_model.entity_id,
                "original_event_type": legacy_model.event_type,
                "actor": legacy_model.actor,
                "details": legacy_model.details,
                "created_at": legacy_model.created_at.isoformat(),
                "source_payload": payload,
            }
            continue
        if payload.get("workspace_id") is None and entity_type == "agent_task" and isinstance(
            event_type, str
        ) and event_type.startswith("agent_worker."):
            payload = {**payload, "workspace_id": parent.get("workspace_id")}
        if (
            payload.get("workspace_id") is None
            and entity_type == "external_agent"
            and event_type in {"agent.auth_failed", "agent.rate_limited"}
        ):
            payload = {**payload, "workspace_id": parent.get("workspace_id")}
        if payload.get("workspace_id") != parent.get("workspace_id"):
            raise ImportBlocked(f"audit_event/{event_id} has mismatched parent ownership")
        # Entity type is repository context and is not represented in the legacy AuditEvent model.
        supported_audits[event_id] = {**payload, "_import_entity_type": entity_type}
    selected["audit_event"] = supported_audits
    selected["legacy_audit_event"] = legacy_audits
    audit_by_type: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for payload in supported_audits.values():
        audit_by_type.setdefault((payload.get("event_type", ""), payload.get("entity_id", "")), []).append(payload)
    for approval_id, claim in delivery_claims.items():
        matches = audit_by_type.get(("approval.delivery_claimed", approval_id), [])
        if len(matches) != 1 or any(
            matches[0].get("details", {}).get(field) != claim[source]
            for field, source in (
                ("source_agent_id", "source_agent_id"),
                ("source_invocation_id", "source_invocation_id"),
            )
        ):
            raise ImportBlocked(f"approval_delivery_claim/{approval_id} is missing its matching audit event")
    for approval_id, failure in delivery_failures.items():
        matches = audit_by_type.get(("approval.delivery_failed", approval_id), [])
        if len(matches) != 1 or any(
            matches[0].get("details", {}).get(field) != failure[source]
            for field, source in (
                ("source_agent_id", "source_agent_id"),
                ("source_invocation_id", "source_invocation_id"),
                ("reason_code", "reason_code"),
            )
        ):
            raise ImportBlocked(f"approval_delivery_failure/{approval_id} is missing its matching audit event")

    # Import notifications only after the preflight above has established that
    # every row uses a currently supported PostgreSQL target type.
    supported_notifications: dict[str, dict[str, Any]] = {}
    for notification_id, payload in records.get("notification", {}).items():
        entity_type = payload.get("entity_type")
        entity_id = payload.get("entity_id")
        if entity_type == "approval_request":
            parent = approvals.get(entity_id)
            if not parent:
                raise ImportBlocked(f"notification/{notification_id} references an unimported approval")
        elif entity_type == "agent_task":
            parent = tasks.get(entity_id)
            if not parent:
                raise ImportBlocked(f"notification/{notification_id} references an unimported AgentTask")
        elif entity_type == "project":
            parent = projects.get(entity_id)
            if not parent:
                raise ImportBlocked(f"notification/{notification_id} references an unimported project")
        elif entity_type == "device_registration":
            parent = devices.get(entity_id)
            if not parent:
                raise ImportBlocked(
                    f"notification/{notification_id} references an unimported device registration"
                )
        else:  # Defensive consistency check; preflight should have rejected it.
            raise ImportBlocked("notification entity type changed after source preflight")
        if payload.get("workspace_id") != parent.get("workspace_id"):
            raise ImportBlocked(f"notification/{notification_id} has mismatched workspace")
        user_id = payload.get("user_id")
        if user_id and (
            user_id not in users
            or users[user_id].get("workspace_id") != payload.get("workspace_id")
        ):
            raise ImportBlocked(f"notification/{notification_id} references an unimported or mismatched user")
        if (
            entity_type == "agent_task"
            and parent.get("created_by_user_id") is not None
            and user_id not in {None, parent.get("created_by_user_id")}
        ):
            raise ImportBlocked(f"notification/{notification_id} user does not own its AgentTask")
        if (
            entity_type == "device_registration"
            and user_id not in {None, parent.get("user_id")}
        ):
            raise ImportBlocked(
                f"notification/{notification_id} user does not own its device registration"
            )
        supported_notifications[notification_id] = payload
    selected["notification"] = supported_notifications

    imported_counts = {kind: len(selected.get(kind, {})) for kind in sorted(CORE_TYPES)}
    source_counts = dict(sorted(kv_source_counts.items()))
    source_counts["auth_email_outbox"] = len(email_outbox)
    source_counts["approval_delivery_claim"] = len(delivery_claims)
    source_counts["approval_delivery_failure"] = len(delivery_failures)
    source_counts["approval_invocation_lease"] = len(invocation_leases)
    source_counts["auth_rate_limit_bucket"] = len(selected["auth_rate_limit_bucket"])
    source_counts["legacy_audit_event"] = len(legacy_audits)
    source_counts = dict(sorted(source_counts.items()))
    omissions = {kind: count for kind, count in source_counts.items() if kind not in CORE_TYPES and count}
    for kind in CORE_TYPES:
        omitted = source_counts.get(kind, 0) - imported_counts.get(kind, 0)
        if kind == "audit_event":
            omitted -= len(legacy_audits)
        if omitted > 0:
            omissions[kind] = omitted
    final_stat = path.stat()
    final_digest = _sha256_file(path)
    if (
        (initial_stat.st_dev, initial_stat.st_ino, initial_stat.st_size, initial_stat.st_mtime_ns)
        != (final_stat.st_dev, final_stat.st_ino, final_stat.st_size, final_stat.st_mtime_ns)
        or digest != final_digest
    ):
        raise ImportBlocked("source snapshot changed during inspection; discard it and create a stable snapshot")
    return ({
        "status": "DRY_RUN_VALIDATED",
        "scope": SCOPE,
        "source_sha256": digest,
        "source_table_coverage": source_table_coverage,
        "source_kv_entity_coverage": _source_kv_entity_coverage(
            kv_source_counts, imported_counts
        ),
        "source_counts": source_counts,
        "planned_counts": imported_counts,
        "derived_workspace_scope_counts": dict(sorted(derived_workspace_scope_counts.items())),
        "invocation_drain": {
            "status": "CLEAR",
            "lease_table_present": lease_table_present,
            **invocation_drain,
        },
        "sanitized_terminal_email_ciphertext_count": sanitized_terminal_email_ciphertexts,
        "agent_run_fault_injection": {
            "status": "CLEAR",
            "inactive_settings": "IMPORTED",
            **agent_run_faults,
        },
        "omitted_counts": dict(sorted(omissions.items())),
        "partial": bool(omissions),
        "audit_status": report["status"],
        "migration_authorized": False,
        "snapshot_certified": False,
    }, selected, digest)


def _insert_payload(conn: psycopg.Connection, kind: str, entity_id: str, payload: dict[str, Any]) -> None:
    if kind == "legacy_audit_event":
        conn.execute(
            "INSERT INTO legacy_audit_events(source_sha256,event_id,workspace_id,"
            "quarantine_reason,original_entity_id,original_event_type,actor,details,created_at,source_payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                payload["source_sha256"], entity_id, payload["workspace_id"],
                payload["quarantine_reason"],
                payload["original_entity_id"], payload["original_event_type"],
                payload["actor"], Jsonb(payload["details"]),
                datetime.fromisoformat(payload["created_at"]),
                Jsonb(payload["source_payload"]),
            ),
        )
        return
    if kind == "agent_run_fault":
        model = AgentRunFaultConfig.model_validate(payload)
        parent = conn.execute(
            "SELECT workspace_id FROM agent_runs WHERE run_id=%s",
            (model.run_id,),
        ).fetchone()
        if parent is None or not parent[0]:
            raise ImportBlocked(f"agent_run_fault/{entity_id} references a missing or unscoped target AgentRun")
        if model.enabled and model.remaining_uses > 0:
            raise ImportBlocked(f"agent_run_fault/{entity_id} is active and cannot be imported")
        conn.execute(
            "INSERT INTO agent_run_faults(fault_id,workspace_id,run_id,step_type,fault_kind,"
            "enabled,remaining_uses,message,created_by,created_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                entity_id, parent[0], model.run_id,
                model.step_type.value if model.step_type is not None else None,
                model.fault_kind.value, model.enabled, model.remaining_uses,
                model.message, model.created_by, model.created_at,
                Jsonb(model.model_dump(mode="json")),
            ),
        )
        return
    if kind == "approval_invocation_lease":
        conn.execute(
            "INSERT INTO approval_invocation_leases "
            "(approval_id,workspace_id,source_agent_id,source_invocation_id,heartbeat_at,expires_at) "
            "VALUES(%s,%s,%s,%s,%s,%s)",
            (
                payload["approval_id"], payload["workspace_id"], payload["source_agent_id"],
                payload["source_invocation_id"], datetime.fromisoformat(payload["heartbeat_at"]),
                datetime.fromisoformat(payload["expires_at"]),
            ),
        )
        return
    if kind == "auth_rate_limit_bucket":
        conn.execute(
            "INSERT INTO auth_rate_limit_buckets(bucket_hash,window_start,request_count) "
            "VALUES(%s,%s,%s)",
            (payload["bucket_hash"], payload["window_start"], payload["request_count"]),
        )
        return
    if kind == "auth_email_outbox":
        conn.execute(
            "INSERT INTO auth_email_outbox(outbox_id,recipient,encrypted_code,status,attempts,"
            "created_at,next_attempt_at,lease_expires_at,sent_at,last_error) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                entity_id, payload["recipient"], payload["encrypted_code"], payload["status"],
                payload["attempts"], payload["created_at"], payload["next_attempt_at"],
                payload["lease_expires_at"], payload["sent_at"], payload["last_error"],
            ),
        )
        return
    if kind == "approval_delivery_claim":
        conn.execute(
            "INSERT INTO approval_delivery_claims(approval_id,workspace_id,source_agent_id,"
            "source_invocation_id,claimed_at) VALUES(%s,%s,%s,%s,%s)",
            (
                entity_id, payload["workspace_id"], payload["source_agent_id"],
                payload["source_invocation_id"], datetime.fromisoformat(payload["claimed_at"]),
            ),
        )
        return
    if kind == "approval_delivery_failure":
        conn.execute(
            "INSERT INTO approval_delivery_failures(approval_id,workspace_id,source_agent_id,"
            "source_invocation_id,reason_code,reported_at) VALUES(%s,%s,%s,%s,%s,%s)",
            (
                entity_id, payload["workspace_id"], payload["source_agent_id"],
                payload["source_invocation_id"], payload["reason_code"],
                datetime.fromisoformat(payload["reported_at"]),
            ),
        )
        return
    if kind == "action_consumption":
        conn.execute(
            "INSERT INTO action_consumptions(action_id,workspace_id,lease_id,consumed_at,payload) "
            "VALUES(%s,%s,%s,%s,%s)",
            (
                entity_id, payload["workspace_id"], payload["lease_id"],
                payload["consumed_at"], Jsonb(payload),
            ),
        )
        return
    model = ENTITY_TYPES[kind].model_validate(payload)
    if kind == "workspace":
        conn.execute("INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)", (entity_id, Jsonb(model.model_dump(mode="json"))))
    elif kind == "user":
        conn.execute(
            "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,email_verified,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (entity_id, model.workspace_id, model.email, model.email.strip().lower(), model.role.value,
             model.disabled, model.email_verified, Jsonb(model.model_dump(mode="json"))),
        )
    elif kind == "invite":
        conn.execute(
            "INSERT INTO invites(invite_id,workspace_id,email,normalized_email,role,token_hash,status,"
            "expires_at,created_by_user_id,accepted_by_user_id,created_at,updated_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                entity_id, model.workspace_id, model.email, model.email.strip().lower(),
                model.role.value, model.token_hash, model.status.value, model.expires_at,
                model.created_by_user_id, model.accepted_by_user_id, model.created_at,
                model.updated_at, Jsonb(model.model_dump(mode="json")),
            ),
        )
    elif kind == "session":
        conn.execute(
            "INSERT INTO sessions(session_id,user_id,token_hash,created_at,expires_at,revoked,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (entity_id, model.user_id, model.token_hash, model.created_at, model.expires_at,
             model.revoked, Jsonb(model.model_dump(mode="json"))),
        )
    elif kind == "project":
        conn.execute("INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)", (entity_id, model.workspace_id, Jsonb(model.model_dump(mode="json"))))
    elif kind == "mcp_child_server":
        conn.execute(
            "INSERT INTO mcp_child_servers(server_id,project_id,workspace_id,name,enabled,created_at,updated_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                entity_id, model.project_id, model.workspace_id, model.name, model.enabled,
                model.created_at, model.updated_at, Jsonb(model.model_dump(mode="json")),
            ),
        )
    elif kind == "idea":
        conn.execute(
            "INSERT INTO ideas(idea_id,project_id,workspace_id,created_at,payload) VALUES(%s,%s,%s,%s,%s)",
            (entity_id, model.project_id, model.workspace_id, model.created_at,
             Jsonb(model.model_dump(mode="json"))),
        )
    elif kind == "work_order":
        conn.execute(
            "INSERT INTO work_orders(work_order_id,project_id,workspace_id,idea_id,source_agent_id,created_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (entity_id, model.project_id, model.workspace_id, model.idea_id,
             model.source_agent_id, model.created_at, Jsonb(model.model_dump(mode="json"))),
        )
    elif kind == "agent_connector":
        conn.execute(
            "INSERT INTO agent_connectors(connector_id,workspace_id,created_by_user_id,"
            "provider_label,auth_type,status,created_at,updated_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                entity_id, model.workspace_id, model.created_by_user_id,
                model.provider_label.value, model.auth_type.value, model.status.value,
                model.created_at, model.updated_at,
                Jsonb(model.model_dump(mode="json")),
            ),
        )
    elif kind == "external_agent":
        conn.execute(
            "INSERT INTO external_agents(agent_id,workspace_id,created_by_user_id,provider,auth_type,enabled,secret_hash,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (entity_id, model.workspace_id, model.created_by_user_id, model.provider.value,
             model.auth_type.value, model.enabled, model.secret_hash,
             Jsonb(model.model_dump(mode="json"))),
        )
    elif kind == "external_agent_credential":
        agent = conn.execute(
            "SELECT workspace_id FROM external_agents WHERE agent_id=%s", (model.agent_id,)
        ).fetchone()
        if not agent:
            raise ImportBlocked(f"external_agent_credential/{entity_id} references a missing target agent")
        conn.execute(
            "INSERT INTO external_agent_credentials(credential_id,agent_id,workspace_id,token_expires_at,token_revoked_at,token_rotated_at,last_used_at,failed_auth_count,rate_limit_per_minute,rate_limit_window_started_at,rate_limit_request_count,created_at,updated_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (model.id, model.agent_id, agent[0], model.token_expires_at, model.token_revoked_at,
             model.token_rotated_at, model.last_used_at, model.failed_auth_count,
             model.rate_limit_per_minute, model.rate_limit_window_started_at,
             model.rate_limit_request_count, model.created_at, model.updated_at,
             Jsonb(model.model_dump(mode="json"))),
        )
    elif kind == "device_registration":
        conn.execute(
            "INSERT INTO device_registrations(device_id,user_id,workspace_id,platform,token,app_version,created_at,updated_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (entity_id, model.user_id, model.workspace_id, model.platform, model.token,
             model.app_version, model.created_at, model.updated_at,
             Jsonb(model.model_dump(mode="json"))),
        )
    elif kind == "approval_request":
        conn.execute(
            "INSERT INTO approval_requests(approval_id,project_id,workspace_id,work_order_id,"
            "source_agent_id,status,expires_at,created_at,updated_at,version,payload,"
            "source_invocation_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s,%s)",
            (entity_id, model.project_id, model.workspace_id, model.work_order_id,
             model.source_agent_id, model.status.value, model.expires_at,
             model.created_at, model.updated_at, Jsonb(model.model_dump(mode="json")),
             model.source_invocation_id),
        )
    elif kind == "mcp_pending_request":
        conn.execute(
            "INSERT INTO mcp_pending_requests(pending_id,approval_id,project_id,workspace_id,status,"
            "attempts,lease_owner,lease_expires_at,last_error,created_at,updated_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                entity_id, model.approval_request_id, model.project_id, model.workspace_id,
                model.status.value, model.attempts, model.lease_owner, model.lease_expires_at,
                model.last_error, model.created_at, model.updated_at,
                Jsonb(model.model_dump(mode="json")),
            ),
        )
    elif kind == "test_run":
        approval = conn.execute(
            "SELECT project_id,workspace_id FROM approval_requests WHERE approval_id=%s",
            (model.approval_request_id,),
        ).fetchone()
        if not approval:
            raise ImportBlocked(f"test_run/{entity_id} references a missing target approval")
        conn.execute(
            "INSERT INTO test_runs(test_run_id,approval_id,project_id,workspace_id,created_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s)",
            (
                entity_id, model.approval_request_id, approval[0], approval[1],
                model.created_at, Jsonb(model.model_dump(mode="json")),
            ),
        )
    elif kind == "agent_task":
        conn.execute(
            "INSERT INTO agent_tasks(task_id,project_id,workspace_id,external_agent_id,approval_request_id,status,updated_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (entity_id, model.project_id, model.workspace_id, model.external_agent_id,
             model.approval_request_id, model.status.value, model.updated_at,
             Jsonb(model.model_dump(mode="json"))),
        )
    elif kind == "agent_task_event":
        task = conn.execute(
            "SELECT workspace_id FROM agent_tasks WHERE task_id=%s",
            (model.task_id,),
        ).fetchone()
        if not task:
            raise ImportBlocked(f"agent_task_event/{entity_id} references a missing target task")
        conn.execute(
            "INSERT INTO agent_task_events(event_id,task_id,workspace_id,event_type,actor,created_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (entity_id, model.task_id, task[0], model.event_type.value, model.actor,
             model.created_at, Jsonb(model.model_dump(mode="json"))),
        )
    elif kind == "agent_run":
        conn.execute(
            "INSERT INTO agent_runs(run_id,task_id,project_id,workspace_id,approval_id,status,"
            "current_step_id,lease_owner,lease_token,lease_expires_at,updated_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                entity_id, model.task_id, model.project_id, model.workspace_id,
                model.approval_request_id, model.status.value, model.current_step_id,
                model.lease_owner, model.lease_token, model.lease_expires_at,
                model.updated_at, Jsonb(model.model_dump(mode="json")),
            ),
        )
    elif kind == "agent_run_step":
        run = conn.execute(
            "SELECT workspace_id,task_id FROM agent_runs WHERE run_id=%s",
            (model.run_id,),
        ).fetchone()
        if not run or run[1] != model.task_id:
            raise ImportBlocked(f"agent_run_step/{entity_id} references a missing or mismatched target run")
        conn.execute(
            "INSERT INTO agent_run_steps(step_id,run_id,task_id,workspace_id,sequence,status,updated_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (entity_id, model.run_id, model.task_id, run[0], model.sequence,
             model.status.value, model.updated_at, Jsonb(model.model_dump(mode="json"))),
        )
    elif kind == "agent_run_event":
        run = conn.execute(
            "SELECT workspace_id,task_id FROM agent_runs WHERE run_id=%s",
            (model.run_id,),
        ).fetchone()
        if not run or run[1] != model.task_id:
            raise ImportBlocked(f"agent_run_event/{entity_id} references a missing or mismatched target run")
        if model.step_id:
            step = conn.execute(
                "SELECT run_id FROM agent_run_steps WHERE step_id=%s", (model.step_id,)
            ).fetchone()
            if not step or step[0] != model.run_id:
                raise ImportBlocked(f"agent_run_event/{entity_id} references a missing or mismatched target step")
        conn.execute(
            "INSERT INTO agent_run_events(event_id,run_id,task_id,workspace_id,created_at,event_type,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (entity_id, model.run_id, model.task_id, run[0], model.created_at,
             model.event_type.value, Jsonb(model.model_dump(mode="json"))),
        )
    elif kind == "audit_event":
        entity_type = payload["_import_entity_type"]
        approval_id = model.entity_id if entity_type == "approval_request" else None
        conn.execute(
            "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,event_type,actor,details,created_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (entity_id, model.workspace_id, approval_id, entity_type, model.entity_id,
             model.event_type, model.actor, Jsonb(model.details), model.created_at,
             Jsonb(model.model_dump(mode="json"))),
        )
    elif kind == "notification":
        model = Notification.model_validate(payload)
        approval_id = model.entity_id if model.entity_type == "approval_request" else None
        task_id = model.entity_id if model.entity_type == "agent_task" else None
        project_id = model.entity_id if model.entity_type == "project" else None
        device_id = model.entity_id if model.entity_type == "device_registration" else None
        conn.execute(
            "INSERT INTO notifications(notification_id,workspace_id,user_id,approval_id,task_id,project_id,device_id,entity_type,status,attempts,next_attempt_at,lease_expires_at,created_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (entity_id, model.workspace_id, model.user_id, approval_id, task_id, project_id, device_id,
             model.entity_type, model.push_status, model.push_attempts,
             model.push_next_attempt_at, model.push_lease_expires_at, model.created_at,
             Jsonb(model.model_dump(mode="json"))),
        )
    elif kind == "capability_lease":
        conn.execute(
            "INSERT INTO capability_leases(lease_id,approval_id,workspace_id,issued_by_user_id,"
            "status,expires_at,consumed_pull_requests,consumed_retries,consumed_runtime_seconds,"
            "consumed_cost_usd,created_at,updated_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                entity_id, model.approval_request_id, model.workspace_id,
                model.issued_by_user_id, model.status.value, model.expires_at,
                model.consumed_pull_requests, model.consumed_retries,
                model.consumed_runtime_seconds, Decimal(str(model.consumed_cost_usd)),
                model.created_at, model.updated_at,
                Jsonb(model.model_dump(mode="json")),
            ),
        )
    elif kind == "reviewer_attestation":
        conn.execute(
            "INSERT INTO reviewer_attestations(attestation_id,approval_id,workspace_id,"
            "reviewer_user_id,approval_payload_hash,decision,created_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                entity_id, model.approval_request_id, model.workspace_id,
                model.reviewer_user_id, model.approval_payload_hash, model.decision.value,
                model.created_at, Jsonb(model.model_dump(mode="json")),
            ),
        )
    elif kind == "proposed_action":
        conn.execute(
            "INSERT INTO proposed_actions(action_id,workspace_id,lease_id,project_id,task_id,"
            "run_id,created_at,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                entity_id, model.workspace_id, model.lease_id, model.project_id,
                model.task_id, model.run_id, model.created_at,
                Jsonb(model.model_dump(mode="json")),
            ),
        )
    elif kind == "policy_decision":
        conn.execute(
            "INSERT INTO policy_decisions(decision_id,action_id,workspace_id,lease_id,decision,"
            "evaluated_at,payload) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (
                entity_id, model.proposed_action_id, payload["workspace_id"], model.lease_id,
                model.decision.value, model.evaluated_at, Jsonb(model.model_dump(mode="json")),
            ),
        )
    elif kind == "action_authorization":
        conn.execute(
            "INSERT INTO action_authorizations(authorization_id,action_id,workspace_id,"
            "policy_decision_id,reviewer_user_id,decision,action_payload_hash,created_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                entity_id, model.action_id, model.workspace_id, model.policy_decision_id,
                model.reviewer_user_id, model.decision.value, model.action_payload_hash,
                model.created_at, Jsonb(model.model_dump(mode="json")),
            ),
        )
    elif kind == "credential_grant":
        conn.execute(
            "INSERT INTO credential_grants(grant_id,lease_id,workspace_id,grant_type,audience,"
            "subject,expires_at,revoked,token_hash,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                entity_id, model.lease_id, model.workspace_id, model.grant_type.value,
                model.audience, model.subject, model.expires_at, model.revoked,
                model.token_hash, Jsonb(model.model_dump(mode="json")),
            ),
        )
    elif kind == "trust_event":
        conn.execute(
            "INSERT INTO trust_events(event_id,sequence,workspace_id,event_type,entity_type,entity_id,"
            "actor,correlation_id,previous_hash,event_hash,created_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                entity_id, model.sequence, model.workspace_id, model.event_type.value,
                model.entity_type, model.entity_id, model.actor, model.correlation_id,
                model.previous_hash, model.event_hash, model.created_at,
                Jsonb(model.model_dump(mode="json")),
            ),
        )


def _target_is_remote(dsn: str) -> bool:
    info = psycopg.conninfo.conninfo_to_dict(dsn)
    local_hosts = {"localhost", "127.0.0.1", "::1", "[::1]"}
    for key in ("host", "hostaddr"):
        value = info.get(key, "")
        for host in value.split(","):
            host = host.strip()
            if host and not host.startswith("/") and host not in local_hosts:
                return True
    return False


def _target_counts(conn: psycopg.Connection) -> dict[str, int]:
    return {
        kind: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for kind, (table, _) in IMPORT_TABLES.items()
    }


def _lock_and_check_target_application_state(conn: psycopg.Connection) -> bool:
    """Fail closed if a partial importer would mix with existing app state.

    The schema is intentionally broad, while this importer covers only the
    approval/trust slice. Every application table must therefore be empty at
    first import; only migration metadata and the pristine trust-chain head
    are allowed to exist.
    """
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema=current_schema() AND table_type='BASE TABLE' "
        "AND table_name NOT IN (%s,%s) ORDER BY table_name",
        ("schema_migrations", "trust_event_heads"),
    ).fetchall()
    for (table_name,) in rows:
        # Names originate from PostgreSQL catalog metadata, not caller input.
        conn.execute(
            sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(
                sql.Identifier(table_name)
            )
        )
        if conn.execute(
            sql.SQL("SELECT EXISTS(SELECT 1 FROM {} LIMIT 1)").format(
                sql.Identifier(table_name)
            )
        ).fetchone()[0]:
            return True
    trust_heads = conn.execute(
        "SELECT count(*), coalesce(bool_and(sequence=0 AND event_hash IS NULL), TRUE) "
        "FROM trust_event_heads"
    ).fetchone()
    if trust_heads[0] != 1 or not trust_heads[1]:
        return True
    return False


def _first_payload_mismatch(
    conn: psycopg.Connection,
    selected: dict[str, dict[str, dict[str, Any]]],
) -> str | None:
    """Return the entity type whose target payloads differ from the source plan."""
    for kind, (table, id_column) in IMPORT_TABLES.items():
        if kind == "legacy_audit_event":
            rows = conn.execute(
                "SELECT source_sha256,event_id,workspace_id,quarantine_reason,original_entity_id,"
                "original_event_type,actor,details,created_at,source_payload "
                "FROM legacy_audit_events"
            ).fetchall()
            actual = {
                (row[0], row[1]): {
                    "source_sha256": row[0], "event_id": row[1], "workspace_id": row[2],
                    "quarantine_reason": row[3],
                    "original_entity_id": row[4], "original_event_type": row[5],
                    "actor": row[6], "details": row[7],
                    "created_at": row[8].astimezone(timezone.utc).isoformat(),
                    "source_payload": row[9],
                }
                for row in rows
            }
            expected = {
                (payload["source_sha256"], event_id): {
                    **payload,
                    "created_at": datetime.fromisoformat(payload["created_at"])
                    .astimezone(timezone.utc).isoformat(),
                }
                for event_id, payload in selected.get(kind, {}).items()
            }
            if actual != expected:
                return kind
            continue
        if kind == "agent_run_fault":
            rows = conn.execute(
                "SELECT fault_id,workspace_id,run_id,payload FROM agent_run_faults"
            ).fetchall()
            actual = {
                fault_id: (workspace_id, run_id, payload)
                for fault_id, workspace_id, run_id, payload in rows
            }
            expected = {}
            for fault_id, raw_payload in selected.get(kind, {}).items():
                model = AgentRunFaultConfig.model_validate(raw_payload)
                run = selected.get("agent_run", {}).get(model.run_id or "")
                if run is None:
                    return kind
                parent = AgentRun.model_validate(run)
                expected[fault_id] = (
                    parent.workspace_id,
                    model.run_id,
                    model.model_dump(mode="json"),
                )
            normalized_actual = {
                fault_id: (
                    workspace_id,
                    run_id,
                    AgentRunFaultConfig.model_validate(payload).model_dump(mode="json"),
                )
                for fault_id, (workspace_id, run_id, payload) in actual.items()
            }
            if normalized_actual != expected:
                return kind
            continue
        if kind == "approval_invocation_lease":
            rows = conn.execute(
                "SELECT approval_id,workspace_id,source_agent_id,source_invocation_id,heartbeat_at,expires_at "
                "FROM approval_invocation_leases"
            ).fetchall()
            actual = {
                row[0]: {
                    "approval_id": row[0], "workspace_id": row[1], "source_agent_id": row[2],
                    "source_invocation_id": row[3],
                    "heartbeat_at": row[4].astimezone(timezone.utc).isoformat(),
                    "expires_at": row[5].astimezone(timezone.utc).isoformat(),
                }
                for row in rows
            }
            expected = {
                approval_id: {
                    **payload,
                    "heartbeat_at": datetime.fromisoformat(payload["heartbeat_at"])
                    .astimezone(timezone.utc).isoformat(),
                    "expires_at": datetime.fromisoformat(payload["expires_at"])
                    .astimezone(timezone.utc).isoformat(),
                }
                for approval_id, payload in selected.get(kind, {}).items()
            }
            if actual != expected:
                return kind
            continue
        if kind == "auth_rate_limit_bucket":
            rows = conn.execute(
                "SELECT bucket_hash,window_start,request_count FROM auth_rate_limit_buckets"
            ).fetchall()
            actual = {
                f"{row[0]}:{row[1]}": {
                    "bucket_hash": row[0], "window_start": row[1], "request_count": row[2],
                }
                for row in rows
            }
            if actual != selected.get(kind, {}):
                return kind
            continue
        if kind == "approval_delivery_claim":
            rows = conn.execute(
                "SELECT approval_id,workspace_id,source_agent_id,source_invocation_id,claimed_at "
                "FROM approval_delivery_claims"
            ).fetchall()
            actual = {
                row[0]: {
                    "approval_id": row[0], "workspace_id": row[1], "source_agent_id": row[2],
                    "source_invocation_id": row[3],
                    "claimed_at": row[4].astimezone(timezone.utc).isoformat(),
                }
                for row in rows
            }
            expected = {
                entity_id: {
                    **payload,
                    "claimed_at": datetime.fromisoformat(payload["claimed_at"])
                    .astimezone(timezone.utc)
                    .isoformat(),
                }
                for entity_id, payload in selected.get(kind, {}).items()
            }
            if actual != expected:
                return kind
            continue
        if kind == "approval_delivery_failure":
            rows = conn.execute(
                "SELECT approval_id,workspace_id,source_agent_id,source_invocation_id,"
                "reason_code,reported_at FROM approval_delivery_failures"
            ).fetchall()
            actual = {
                row[0]: {
                    "approval_id": row[0], "workspace_id": row[1], "source_agent_id": row[2],
                    "source_invocation_id": row[3], "reason_code": row[4],
                    "reported_at": row[5].astimezone(timezone.utc).isoformat(),
                }
                for row in rows
            }
            expected = {
                entity_id: {
                    **payload,
                    "reported_at": datetime.fromisoformat(payload["reported_at"])
                    .astimezone(timezone.utc)
                    .isoformat(),
                }
                for entity_id, payload in selected.get(kind, {}).items()
            }
            if actual != expected:
                return kind
            continue
        if kind == "auth_email_outbox":
            rows = conn.execute(
                "SELECT outbox_id,recipient,encrypted_code,status,attempts,created_at,"
                "next_attempt_at,lease_expires_at,sent_at,last_error FROM auth_email_outbox"
            ).fetchall()
            actual = {
                row[0]: {
                    "recipient": row[1], "encrypted_code": bytes(row[2]), "status": row[3],
                    "attempts": row[4],
                    "created_at": row[5].astimezone(timezone.utc).isoformat(),
                    "next_attempt_at": row[6].astimezone(timezone.utc).isoformat(),
                    "lease_expires_at": row[7].astimezone(timezone.utc).isoformat() if row[7] else None,
                    "sent_at": row[8].astimezone(timezone.utc).isoformat() if row[8] else None,
                    "last_error": row[9],
                }
                for row in rows
            }
            expected = selected.get(kind, {})
            expected = {
                entity_id: {
                    **payload,
                    "created_at": datetime.fromisoformat(payload["created_at"]).astimezone(timezone.utc).isoformat(),
                    "next_attempt_at": datetime.fromisoformat(payload["next_attempt_at"]).astimezone(timezone.utc).isoformat(),
                    "lease_expires_at": (
                        datetime.fromisoformat(payload["lease_expires_at"]).astimezone(timezone.utc).isoformat()
                        if payload["lease_expires_at"] else None
                    ),
                    "sent_at": (
                        datetime.fromisoformat(payload["sent_at"]).astimezone(timezone.utc).isoformat()
                        if payload["sent_at"] else None
                    ),
                }
                for entity_id, payload in expected.items()
            }
            if actual != expected:
                return kind
            continue
        actual = {
            entity_id: payload
            for entity_id, payload in conn.execute(
                f"SELECT {id_column}, payload FROM {table}"
            ).fetchall()
        }
        if kind == "action_consumption":
            expected = selected.get(kind, {})
        else:
            expected = {
                entity_id: ENTITY_TYPES[kind].model_validate(payload).model_dump(mode="json")
                for entity_id, payload in selected.get(kind, {}).items()
            }
        if kind == "external_agent_credential":
            expected = {
                model.id: model.model_dump(mode="json")
                for model in (
                    ENTITY_TYPES[kind].model_validate(payload)
                    for payload in selected.get(kind, {}).values()
                )
            }
        if actual != expected:
            return kind
    return None


def apply_import(dsn: str, digest: str, plan: dict[str, Any], selected: dict[str, dict[str, dict[str, Any]]]) -> None:
    for outbox_id, payload in selected.get("auth_email_outbox", {}).items():
        if payload["status"] not in {"PENDING", "SENDING"}:
            continue
        try:
            _decrypt_code(
                outbox_id=outbox_id,
                recipient=payload["recipient"],
                encrypted=payload["encrypted_code"],
            )
        except RuntimeError as exc:
            raise ImportBlocked(
                "active auth_email_outbox ciphertext could not be authenticated with "
                "the configured AIXION_EMAIL_OUTBOX_KEY"
            ) from exc
    run_postgres_migrations(lambda: psycopg.connect(dsn))
    with psycopg.connect(dsn) as conn:
        conn.execute("SET statement_timeout = '15s'")
        conn.execute("SET lock_timeout = '5s'")
        with conn.transaction():
            existing = conn.execute(
                "SELECT source_counts, imported_counts FROM sqlite_import_runs WHERE source_sha256=%s AND scope=%s",
                (digest, SCOPE),
            ).fetchone()
            if existing:
                if existing[0] != plan["source_counts"] or existing[1] != plan["planned_counts"]:
                    raise ImportBlocked("existing source manifest differs from this plan")
                if _target_counts(conn) != plan["planned_counts"]:
                    raise ImportBlocked("target counts changed after the recorded import; review before retrying")
                mismatch = _first_payload_mismatch(conn, selected)
                if mismatch is not None:
                    raise ImportBlocked(
                        f"target {mismatch} payloads changed after the recorded import; review before retrying"
                    )
                return
            if _lock_and_check_target_application_state(conn):
                raise ImportBlocked(
                    "target application schema is non-empty outside migration metadata; "
                    "partial import requires a pristine schema with no pre-existing application data"
                )
            ordering = (
                "auth_rate_limit_bucket", "workspace", "user", "invite", "session",
                "project", "mcp_child_server",
                "agent_connector", "external_agent", "external_agent_credential",
                "device_registration", "idea", "work_order", "approval_request",
                "approval_invocation_lease", "test_run",
                "mcp_pending_request",
                "agent_task", "agent_task_event", "agent_run", "agent_run_fault",
                "agent_run_step", "agent_run_event",
                "approval_delivery_claim", "approval_delivery_failure",
                "capability_lease", "reviewer_attestation",
                "proposed_action", "policy_decision", "action_authorization", "action_consumption",
                "credential_grant", "trust_event", "audit_event", "legacy_audit_event",
                "notification", "auth_email_outbox",
            )
            for kind in ordering:
                for entity_id, payload in selected.get(kind, {}).items():
                    _insert_payload(conn, kind, entity_id, payload)
            imported_trust_events = [
                TrustEvent.model_validate(payload)
                for payload in selected.get("trust_event", {}).values()
            ]
            if imported_trust_events:
                tail = max(imported_trust_events, key=lambda event: event.sequence)
                conn.execute(
                    "UPDATE trust_event_heads SET sequence=%s,event_hash=%s WHERE singleton=TRUE",
                    (tail.sequence, tail.event_hash),
                )
            if _target_counts(conn) != plan["planned_counts"]:
                raise ImportBlocked("target row counts do not match the validated import plan")
            mismatch = _first_payload_mismatch(conn, selected)
            if mismatch is not None:
                raise ImportBlocked(
                    f"imported {mismatch} payloads do not match the validated source plan"
                )
            conn.execute(
                "INSERT INTO sqlite_import_runs(source_sha256,scope,source_counts,imported_counts) VALUES(%s,%s,%s,%s)",
                (digest, SCOPE, Jsonb(plan["source_counts"]), Jsonb(plan["planned_counts"])),
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="owner-only, quiesced SQLite snapshot")
    parser.add_argument(
        "--write-invocation-drain-report",
        type=Path,
        help="write a private read-only native invocation drain report; never imports data",
    )
    parser.add_argument(
        "--write-audit-event-inventory-report",
        type=Path,
        help="write a redacted read-only audit parent inventory from the local SQLite snapshot",
    )
    parser.add_argument("--apply", action="store_true", help="write the validated partial slice")
    parser.add_argument("--confirm-source-snapshot", action="store_true", help="assert an operator-created coordinated snapshot")
    parser.add_argument("--allow-partial-slice", action="store_true", help="acknowledge omitted entity types")
    parser.add_argument("--allow-remote-staging", action="store_true", help="allow a non-loopback staging server")
    args = parser.parse_args(argv)
    try:
        if args.write_invocation_drain_report is not None:
            if (
                args.write_audit_event_inventory_report is not None
                or args.apply
                or args.confirm_source_snapshot
                or args.allow_partial_slice
                or args.allow_remote_staging
            ):
                raise ImportBlocked("invocation drain report mode cannot be combined with import or apply options")
            report = build_invocation_drain_report(args.source)
            _write_private_report(args.write_invocation_drain_report, report)
            print(json.dumps(report, sort_keys=True, indent=2))
            return 0 if report["status"] == "READY_FOR_OWNER_REVIEW" else 2
        if args.write_audit_event_inventory_report is not None:
            if (
                args.write_invocation_drain_report is not None
                or args.apply
                or args.confirm_source_snapshot
                or args.allow_partial_slice
                or args.allow_remote_staging
            ):
                raise ImportBlocked(
                    "audit event inventory mode cannot be combined with import or other report options"
                )
            report = build_audit_event_inventory_report(args.source)
            _write_private_report(args.write_audit_event_inventory_report, report)
            print(json.dumps({
                "status": "AUDIT_EVENT_INVENTORY_WRITTEN",
                "report_path": str(args.write_audit_event_inventory_report),
                "source_snapshot_sha256": report["source_snapshot_sha256"],
                "audit_event_count": report["audit_event_count"],
                "classification_counts": report["classification_counts"],
            }, sort_keys=True))
            return 0
        plan, records, digest = build_plan(args.source)
        if args.apply:
            dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "")
            if not dsn:
                raise ImportBlocked("AIXION_TEST_POSTGRES_DSN is required for apply")
            database = psycopg.conninfo.conninfo_to_dict(dsn).get("dbname", "")
            if not re.search(r"_(test|staging)$", database, re.IGNORECASE):
                raise ImportBlocked("apply target database name must end in _test or _staging")
            if _target_is_remote(dsn) and not args.allow_remote_staging:
                raise ImportBlocked("remote staging requires --allow-remote-staging")
            if not args.confirm_source_snapshot:
                raise ImportBlocked("apply requires --confirm-source-snapshot")
            if plan["partial"] and not args.allow_partial_slice:
                raise ImportBlocked("partial import requires --allow-partial-slice")
            apply_import(dsn, digest, plan, records)
            plan["status"] = "STAGING_IMPORT_APPLIED"
        print(json.dumps(plan, sort_keys=True, indent=2))
        return 0
    except (ImportBlocked, OSError, sqlite3.Error, psycopg.Error, ValueError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc), "migration_authorized": False}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
