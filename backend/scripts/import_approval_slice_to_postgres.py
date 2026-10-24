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
from app.agent_run_models import AgentRun, AgentRunEvent, AgentRunStep
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

SCOPE = "approval_auth_agent_mcp_test_runs_connectors_email_task_notifications_delivery_rate_limits_invocation_leases_v12"
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
    "audit_event", "notification", "capability_lease",
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
    "audit_event": ("audit_events", "event_id"),
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


class ImportBlocked(RuntimeError):
    pass


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
    pending_fingerprints = sorted(
        hashlib.sha256(approval_id.encode("utf-8")).hexdigest()
        for approval_id, payload in approvals.items()
        if payload.get("status", ApprovalStatus.REQUESTED.value) in pending_statuses
        and isinstance(payload.get("source_agent_id"), str)
        and bool(payload.get("source_agent_id"))
        and isinstance(payload.get("source_invocation_id"), str)
        and bool(payload.get("source_invocation_id"))
    )
    approved_unconsumed_fingerprints = sorted(
        hashlib.sha256(approval_id.encode("utf-8")).hexdigest()
        for approval_id, payload in approvals.items()
        if payload.get("status") == ApprovalStatus.APPROVED.value
        and isinstance(payload.get("source_agent_id"), str)
        and bool(payload.get("source_agent_id"))
        and isinstance(payload.get("source_invocation_id"), str)
        and bool(payload.get("source_invocation_id"))
        and approval_id not in delivery_claims
    )
    return {
        "pending_native_invocation_count": len(pending_fingerprints),
        "approval_id_sha256": pending_fingerprints,
        "approved_unconsumed_invocation_count": len(approved_unconsumed_fingerprints),
        "approved_unconsumed_approval_id_sha256": approved_unconsumed_fingerprints,
    }


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
        required = {
            "id", "recipient", "encrypted_code", "status", "attempts", "created_at",
            "next_attempt_at", "lease_expires_at", "sent_at", "last_error",
        }
        if not required.issubset(columns):
            raise ImportBlocked("auth_email_outbox schema is incomplete; review source migration state")
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
            if not required.issubset(columns):
                raise ImportBlocked(f"{table} schema is incomplete; review source migration state")
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
        if not {"bucket_hash", "window_start", "request_count"}.issubset(columns):
            raise ImportBlocked("auth_rate_limit_buckets schema is incomplete; review source migration state")
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
    report = audit(path)
    if report.get("status") != "STRUCTURAL_REVIEW_PASSED":
        raise ImportBlocked("read-only structural audit did not pass; review its findings first")
    records = _read_records(path)
    selected = {key: value for key, value in records.items() if key in CORE_TYPES}
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
    ):
        safe_fingerprints = ",".join(
            invocation_drain["approval_id_sha256"]
            + invocation_drain["approved_unconsumed_approval_id_sha256"]
        )
        raise ImportBlocked(
            "source snapshot contains native invocations that must drain before import: "
            f"{invocation_drain['pending_native_invocation_count']} pending native invocation approval(s), "
            f"{invocation_drain['approved_unconsumed_invocation_count']} approved but unconsumed native invocation(s); "
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

    # Only audit events with a supported, workspace-verified parent are imported.
    supported_audits: dict[str, dict[str, Any]] = {}
    for event_id, payload in records.get("audit_event", {}).items():
        entity_id = payload.get("entity_id")
        event_type = payload.get("event_type")
        if entity_id in approvals:
            parent = approvals[entity_id]
            entity_type = "approval_request"
        elif entity_id in projects:
            parent = projects[entity_id]
            entity_type = "project"
        elif entity_id in work_orders:
            parent = work_orders[entity_id]
            entity_type = "work_order"
        elif entity_id in ideas:
            parent = ideas[entity_id]
            entity_type = "idea"
        elif entity_id in agents and payload.get("event_type") in {
            "agent.registered", "agent.token_revoked",
        }:
            parent = agents[entity_id]
            entity_type = "external_agent"
        elif entity_id in connectors:
            parent = connectors[entity_id]
            entity_type = "agent_connector"
        elif entity_id in selected.get("invite", {}):
            parent = selected["invite"][entity_id]
            entity_type = "invite"
        elif entity_id in sessions and event_type == "auth.session_revoked":
            session = sessions[entity_id]
            user = users.get(session.get("user_id"))
            if not user or user.get("workspace_id") != payload.get("workspace_id"):
                raise ImportBlocked(
                    f"audit_event/{event_id} references a missing or mismatched session owner"
                )
            parent = {**session, "workspace_id": user.get("workspace_id")}
            entity_type = "auth_session"
        elif entity_id in users and event_type in {
            "auth.account_deletion_requested",
            "auth.role_updated",
            "auth.sessions_revoked",
        }:
            parent = users[entity_id]
            entity_type = "user"
        elif entity_id in mcp_servers:
            parent = mcp_servers[entity_id]
            entity_type = "mcp_child_server"
        elif entity_id in mcp_pending:
            parent = mcp_pending[entity_id]
            entity_type = "mcp_pending_request"
        elif entity_id in test_runs:
            approval = approvals.get(test_runs[entity_id].get("approval_request_id"))
            if not approval:
                raise ImportBlocked(f"audit_event/{event_id} references an unimported test-run approval")
            parent = {**test_runs[entity_id], "workspace_id": approval.get("workspace_id")}
            entity_type = "test_run"
        elif (
            isinstance(payload.get("event_type"), str)
            and payload["event_type"].startswith("agent_task.")
            and entity_id in tasks
        ):
            parent = tasks[entity_id]
            entity_type = "agent_task"
        elif (
            isinstance(payload.get("event_type"), str)
            and payload["event_type"].startswith("agent_run.")
            and entity_id in runs
        ):
            parent = runs[entity_id]
            entity_type = "agent_run"
        else:
            continue
        if payload.get("workspace_id") != parent.get("workspace_id"):
            raise ImportBlocked(f"audit_event/{event_id} has mismatched parent ownership")
        # Entity type is repository context and is not represented in the legacy AuditEvent model.
        supported_audits[event_id] = {**payload, "_import_entity_type": entity_type}
    selected["audit_event"] = supported_audits
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

    # Import only notifications whose parent is part of this supported snapshot.
    # The database schema currently supports approval and AgentTask notifications.
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
        else:
            continue
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
        supported_notifications[notification_id] = payload
    selected["notification"] = supported_notifications

    imported_counts = {kind: len(selected.get(kind, {})) for kind in sorted(CORE_TYPES)}
    source_counts = dict(sorted(report.get("entity_counts", {}).items()))
    source_counts["auth_email_outbox"] = len(email_outbox)
    source_counts["approval_delivery_claim"] = len(delivery_claims)
    source_counts["approval_delivery_failure"] = len(delivery_failures)
    source_counts["approval_invocation_lease"] = len(invocation_leases)
    source_counts["auth_rate_limit_bucket"] = len(selected["auth_rate_limit_bucket"])
    source_counts = dict(sorted(source_counts.items()))
    omissions = {kind: count for kind, count in source_counts.items() if kind not in CORE_TYPES and count}
    for kind in CORE_TYPES:
        omitted = source_counts.get(kind, 0) - imported_counts.get(kind, 0)
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
        "source_counts": source_counts,
        "planned_counts": imported_counts,
        "derived_workspace_scope_counts": dict(sorted(derived_workspace_scope_counts.items())),
        "invocation_drain": {
            "status": "CLEAR",
            "lease_table_present": lease_table_present,
            **invocation_drain,
        },
        "sanitized_terminal_email_ciphertext_count": sanitized_terminal_email_ciphertexts,
        "omitted_counts": dict(sorted(omissions.items())),
        "partial": bool(omissions),
        "audit_status": report["status"],
        "migration_authorized": False,
        "snapshot_certified": False,
    }, selected, digest)


def _insert_payload(conn: psycopg.Connection, kind: str, entity_id: str, payload: dict[str, Any]) -> None:
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
            (entity_id, model.agent_id, agent[0], model.token_expires_at, model.token_revoked_at,
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
        conn.execute(
            "INSERT INTO notifications(notification_id,workspace_id,user_id,approval_id,task_id,entity_type,status,attempts,next_attempt_at,lease_expires_at,created_at,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (entity_id, model.workspace_id, model.user_id, approval_id, task_id,
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
                "agent_task", "agent_task_event", "agent_run", "agent_run_step", "agent_run_event",
                "approval_delivery_claim", "approval_delivery_failure",
                "capability_lease", "reviewer_attestation",
                "proposed_action", "policy_decision", "action_authorization", "action_consumption",
                "credential_grant", "trust_event", "audit_event", "notification", "auth_email_outbox",
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
    parser.add_argument("--apply", action="store_true", help="write the validated partial slice")
    parser.add_argument("--confirm-source-snapshot", action="store_true", help="assert an operator-created coordinated snapshot")
    parser.add_argument("--allow-partial-slice", action="store_true", help="acknowledge omitted entity types")
    parser.add_argument("--allow-remote-staging", action="store_true", help="allow a non-loopback staging server")
    args = parser.parse_args(argv)
    try:
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
