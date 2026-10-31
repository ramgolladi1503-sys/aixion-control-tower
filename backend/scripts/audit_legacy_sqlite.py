#!/usr/bin/env python3
"""Read-only structural and model audit for a legacy Control Tower SQLite DB.

This tool does not migrate, repair, redact, or assign ownership to records.
Use a quiesced, independently backed-up database copy for migration planning.
"""
from __future__ import annotations

import hashlib
import argparse
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app.agent_credential_models import AgentCredentialRecord
from app.agent_run_models import AgentRun, AgentRunEvent, AgentRunFaultConfig, AgentRunStep
from app.agent_task_models import AgentTask, AgentTaskEvent
from app.connector_models import AgentConnector
from app.database_migrations import MIGRATION_TABLE, known_migration_ids, run_migrations
from app.models import (
    ApprovalRequest, AuditEvent, DeviceRegistration, ExternalAgent, Idea, Invite,
    MCPChildServer, MCPPendingRequest, Notification, Project, SessionToken,
    TestRun, User, WorkOrder, Workspace,
)
from app.trust_action_authorization_models import ActionAuthorization
from app.trust_crypto import sha256_hex
from app.trust_models import (
    ActionConsumption, CapabilityLease, CredentialGrant, PolicyDecision,
    ProposedAction, ReviewerAttestation, TrustEvent,
)

MODEL_BY_ENTITY: dict[str, type[Any]] = {
    "user": User, "workspace": Workspace, "session": SessionToken, "invite": Invite,
    "external_agent": ExternalAgent, "external_agent_credential": AgentCredentialRecord,
    "agent_connector": AgentConnector, "device_registration": DeviceRegistration,
    "agent_task": AgentTask, "agent_task_event": AgentTaskEvent,
    "agent_run": AgentRun, "agent_run_step": AgentRunStep, "agent_run_event": AgentRunEvent,
    "agent_run_fault": AgentRunFaultConfig, "capability_lease": CapabilityLease,
    "reviewer_attestation": ReviewerAttestation, "proposed_action": ProposedAction,
    "policy_decision": PolicyDecision, "action_authorization": ActionAuthorization,
    "action_consumption": ActionConsumption, "credential_grant": CredentialGrant,
    "trust_event": TrustEvent, "project": Project, "mcp_child_server": MCPChildServer,
    "idea": Idea, "work_order": WorkOrder, "approval_request": ApprovalRequest,
    "mcp_pending_request": MCPPendingRequest, "test_run": TestRun,
    "notification": Notification, "audit_event": AuditEvent,
}

SCOPED_ENTITIES = set(MODEL_BY_ENTITY) - {"workspace"}


def _supported_source_tables() -> set[str]:
    """Derive the supported SQLite table set from the current migration chain.

    Running migrations against an in-memory database keeps this in sync with
    the schema the application itself creates without touching the audited
    source file.
    """
    conn = sqlite3.connect(":memory:")
    try:
        run_migrations(conn)
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()


def _json_object(raw: str) -> dict[str, Any] | None:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _integrity_check(conn: sqlite3.Connection) -> list[str]:
    return [row[0] for row in conn.execute("PRAGMA integrity_check")]


def _trust_event_material(event: TrustEvent) -> dict[str, Any]:
    return {
        "sequence": event.sequence,
        "event_type": event.event_type,
        "entity_type": event.entity_type,
        "entity_id": event.entity_id,
        "actor": event.actor,
        "correlation_id": event.correlation_id,
        "payload": event.payload,
        "previous_hash": event.previous_hash,
        "created_at": event.created_at,
    }


def _verify_trust_event_chain(
    rows: list[tuple[str, str, dict[str, Any] | None]],
) -> dict[str, Any]:
    event_rows = [(entity_id, payload) for entity_type, entity_id, payload in rows if entity_type == "trust_event"]
    parsed: list[tuple[str, TrustEvent]] = []
    for entity_id, payload in event_rows:
        try:
            event = TrustEvent.model_validate(payload)
        except Exception:
            return {
                "valid": False,
                "event_count": len(event_rows),
                "first_invalid_sequence": None,
                "reason": f"Invalid trust event payload for {entity_id}.",
            }
        if event.id != entity_id:
            return {
                "valid": False,
                "event_count": len(event_rows),
                "first_invalid_sequence": event.sequence,
                "reason": "Trust event key does not match event identity.",
            }
        parsed.append((entity_id, event))

    parsed.sort(key=lambda item: item[1].sequence)
    expected_sequence = 1
    expected_previous: str | None = None
    for _, event in parsed:
        if event.sequence != expected_sequence:
            return {
                "valid": False,
                "event_count": len(parsed),
                "first_invalid_sequence": event.sequence,
                "reason": f"Expected sequence {expected_sequence}, got {event.sequence}.",
            }
        if event.previous_hash != expected_previous:
            return {
                "valid": False,
                "event_count": len(parsed),
                "first_invalid_sequence": event.sequence,
                "reason": "Hash-chain predecessor mismatch.",
            }
        if sha256_hex(_trust_event_material(event)) != event.event_hash:
            return {
                "valid": False,
                "event_count": len(parsed),
                "first_invalid_sequence": event.sequence,
                "reason": "Event payload hash mismatch.",
            }
        expected_sequence += 1
        expected_previous = event.event_hash
    return {
        "valid": True,
        "event_count": len(parsed),
        "first_invalid_sequence": None,
        "reason": "",
    }


def audit(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError("database path must name an existing regular file")
    wal_sidecar = path.with_name(path.name + "-wal")
    shm_sidecar = path.with_name(path.name + "-shm")
    uri = f"file:{quote(path.resolve().as_posix(), safe='/')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        supported_tables = _supported_source_tables()
        unknown_tables = sorted(
            table for table in tables
            if not table.startswith("sqlite_") and table not in supported_tables
        )
        report: dict[str, Any] = {
            "status": "REVIEW_REQUIRED", "database_file": path.name,
            "migration_authorized": False,
            "scope": "read_only_structural_review_only",
            "snapshot_certified": False,
            "wal_sidecar_present": wal_sidecar.exists(),
            "shm_sidecar_present": shm_sidecar.exists(),
            "snapshot_note": (
                "This audit does not prove a quiesced or recoverable SQLite snapshot; "
                "WAL sidecars must be handled by a coordinated backup procedure."
            ),
            "read_only": True,
            # Do not echo arbitrary schema names from an untrusted snapshot.
            "tables": sorted(table for table in tables if table in supported_tables),
            "unknown_table_count": len(unknown_tables),
            "unknown_table_sha256": sorted(
                hashlib.sha256(table.encode("utf-8")).hexdigest()
                for table in unknown_tables
            ),
            "migration_rows": [], "entity_counts": {}, "unknown_entity_counts": {},
            "invalid_payload_counts": {}, "ownership_review_counts": {},
            "ownership_conflict_counts": {},
            "identity_conflict_counts": {},
            "parent_relationship_conflict_counts": {},
            "trust_event_chain_verification": {
                "valid": True,
                "event_count": 0,
                "first_invalid_sequence": None,
                "reason": "",
            },
            "record_findings": [],
            "findings": [],
        }
        if unknown_tables:
            report["findings"].append("unknown_source_tables")
        if report["wal_sidecar_present"]:
            report["findings"].append("sqlite_wal_sidecar_present")
        if report["shm_sidecar_present"]:
            report["findings"].append("sqlite_shm_sidecar_present")
        if "kv_store" not in tables:
            report["findings"].append("missing_kv_store_table")
            return report
        if not {"entity_type", "entity_id", "payload"}.issubset(
            {r[1] for r in conn.execute("PRAGMA table_info(kv_store)")}
        ):
            report["findings"].append("kv_store_schema_unrecognized")
            return report

        if MIGRATION_TABLE in tables:
            migration_rows = [dict(r) for r in conn.execute(
                f"SELECT migration_id, name, applied_at FROM {MIGRATION_TABLE} ORDER BY migration_id"
            )]
            report["migration_rows"] = migration_rows
            ids = {r["migration_id"] for r in migration_rows}
            unknown = sorted(ids - known_migration_ids())
            if unknown:
                report["findings"].append("unknown_migrations")
                report["unknown_migrations"] = unknown
        else:
            report["findings"].append("missing_migration_table")

        integrity = _integrity_check(conn)
        report["integrity_check"] = integrity
        if integrity != ["ok"]:
            report["findings"].append("sqlite_integrity_check_failed")

        rows = conn.execute("SELECT entity_type, entity_id, payload FROM kv_store ORDER BY entity_type, entity_id")
        counts: Counter[str] = Counter()
        invalid: Counter[str] = Counter()
        unknown: Counter[str] = Counter()
        needs_review: Counter[str] = Counter()
        invalid_records: set[tuple[str, str]] = set()
        unknown_records: set[tuple[str, str]] = set()
        workspaces: set[str] = set()
        project_workspace: dict[str, str | None] = {}
        workspace_owner: dict[str, str] = {}
        workspace_owner_conflicts: set[str] = set()
        user_ids_by_normalized_email: dict[str, set[str]] = defaultdict(set)
        payload_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        parsed_rows: list[tuple[str, str, dict[str, Any] | None]] = []
        for row in rows:
            entity_type, entity_id, raw = row["entity_type"], row["entity_id"], row["payload"]
            counts[entity_type] += 1
            payload = _json_object(raw)
            parsed_rows.append((entity_type, entity_id, payload))
            if payload is not None:
                payload_by_key[(entity_type, entity_id)] = payload
            model = MODEL_BY_ENTITY.get(entity_type)
            if model is None:
                unknown[entity_type] += 1
                unknown_records.add((entity_type, entity_id))
            elif payload is None:
                invalid[entity_type] += 1
                invalid_records.add((entity_type, entity_id))
            else:
                try:
                    model.model_validate(payload)
                except Exception:
                    invalid[entity_type] += 1
                    invalid_records.add((entity_type, entity_id))
            if entity_type == "workspace" and payload:
                workspaces.add(entity_id)
                owner_id = payload.get("owner_user_id")
                if isinstance(owner_id, str) and owner_id:
                    previous_workspace_id = workspace_owner.get(owner_id)
                    if previous_workspace_id and previous_workspace_id != entity_id:
                        workspace_owner_conflicts.add(owner_id)
                    else:
                        workspace_owner[owner_id] = entity_id
            if entity_type == "user" and payload:
                email = payload.get("email")
                if isinstance(email, str) and email.strip():
                    user_ids_by_normalized_email[email.strip().lower()].add(entity_id)
            if entity_type == "project" and payload:
                project_workspace[entity_id] = payload.get("workspace_id")

        duplicate_email_records = {
            entity_id
            for user_ids in user_ids_by_normalized_email.values()
            if len(user_ids) > 1
            for entity_id in user_ids
        }

        trust_event_verification = _verify_trust_event_chain(parsed_rows)
        report["trust_event_chain_verification"] = trust_event_verification
        if not trust_event_verification["valid"]:
            report["findings"].append("trust_event_chain_invalid")

        def inferred_owner_workspace(user_id: Any) -> str | None:
            if not isinstance(user_id, str) or user_id in workspace_owner_conflicts:
                return None
            return workspace_owner.get(user_id)

        def is_unscoped_unknown_agent_auth_failure(
            entity_id: str, payload: dict[str, Any] | None
        ) -> bool:
            if not payload or payload.get("event_type") != "agent.auth_failed":
                return False
            if payload.get("workspace_id") is not None:
                return False
            details = payload.get("details")
            details = details if isinstance(details, dict) else {}
            agent_id = details.get("agent_id") or payload.get("entity_id")
            return not (
                isinstance(agent_id, str)
                and ("external_agent", agent_id) in payload_by_key
            )

        def resolve_workspace(entity_type: str, entity_id: str, seen: set[tuple[str, str]] | None = None) -> str | None:
            seen = set() if seen is None else seen
            key = (entity_type, entity_id)
            if key in seen:
                return None
            seen.add(key)
            payload = payload_by_key.get(key)
            if payload is None:
                return None
            explicit = payload.get("workspace_id")
            if explicit:
                return explicit
            if entity_type == "user":
                return inferred_owner_workspace(entity_id)
            if entity_type in {"session", "device_registration", "notification"}:
                return resolve_workspace("user", payload.get("user_id") or "", seen)
            if entity_type == "invite":
                return resolve_workspace("user", payload.get("created_by_user_id") or "", seen)
            if entity_type in {"project", "idea", "work_order", "approval_request", "mcp_child_server", "mcp_pending_request", "test_run"}:
                project_id = payload.get("project_id")
                return resolve_workspace("project", project_id or "", seen)
            if entity_type in {"agent_task", "agent_task_event"}:
                if entity_type == "agent_task_event":
                    return resolve_workspace("agent_task", payload.get("task_id") or "", seen)
                return resolve_workspace("project", payload.get("project_id") or "", seen)
            if entity_type in {"agent_run", "agent_run_step", "agent_run_event", "agent_run_fault"}:
                if entity_type != "agent_run":
                    return resolve_workspace("agent_run", payload.get("run_id") or "", seen)
                return resolve_workspace("agent_task", payload.get("task_id") or "", seen)
            if entity_type in {"external_agent_credential"}:
                return resolve_workspace("external_agent", payload.get("agent_id") or "", seen)
            if entity_type == "capability_lease":
                return resolve_workspace("approval_request", payload.get("approval_request_id") or "", seen)
            if entity_type == "reviewer_attestation":
                return resolve_workspace("approval_request", payload.get("approval_request_id") or "", seen)
            if entity_type == "proposed_action":
                if payload.get("project_id"):
                    return resolve_workspace("project", payload["project_id"], seen)
                if payload.get("run_id"):
                    return resolve_workspace("agent_run", payload["run_id"], seen)
                if payload.get("task_id"):
                    return resolve_workspace("agent_task", payload["task_id"], seen)
                if payload.get("lease_id"):
                    return resolve_workspace("capability_lease", payload["lease_id"], seen)
            if entity_type == "policy_decision":
                return resolve_workspace("proposed_action", payload.get("proposed_action_id") or "", seen)
            if entity_type == "action_authorization":
                return resolve_workspace("proposed_action", payload.get("action_id") or "", seen)
            if entity_type == "action_consumption":
                return resolve_workspace("proposed_action", entity_id, seen)
            if entity_type == "credential_grant":
                return resolve_workspace("capability_lease", payload.get("lease_id") or "", seen)
            if entity_type == "trust_event":
                linked_type, linked_id = payload.get("entity_type"), payload.get("entity_id")
                return resolve_workspace(linked_type or "", linked_id or "", seen)
            if entity_type == "audit_event":
                event_type = payload.get("event_type")
                if isinstance(event_type, str) and event_type.startswith("agent_worker."):
                    # Worker outcome audits are keyed by AgentTask IDs; do
                    # not let colliding approval/work-order IDs determine
                    # their tenant scope. This one known event is path-keyed,
                    # so it has no task parent and stays unresolved.
                    if event_type == "agent_worker.container_validation_configured":
                        return None
                    return resolve_workspace("agent_task", payload.get("entity_id") or "", seen)
                details = payload.get("details") or {}
                linked_id = (
                    details.get("agent_id")
                    or details.get("entity_id")
                    or payload.get("entity_id")
                )
                if payload.get("event_type") in {
                    "agent.registered",
                    "agent.token_rotated",
                    "agent.token_revoked",
                    "agent.auth_failed",
                    "agent.rate_limited",
                }:
                    return resolve_workspace("external_agent", linked_id or "", seen)
                for candidate_type in ("approval_request", "work_order", "agent_task", "agent_run", "project"):
                    resolved = resolve_workspace(candidate_type, linked_id or "", seen.copy())
                    if resolved:
                        return resolved
            return None

        conflicts: Counter[str] = Counter()
        quarantined_global_audit_keys: set[tuple[str, str]] = set()
        ownership_review_keys: set[tuple[str, str]] = set()
        ownership_conflict_keys: set[tuple[str, str]] = set()
        workspace_resolutions: dict[tuple[str, str], tuple[str | None, str | None]] = {}
        for entity_type, entity_id, payload in parsed_rows:
            if entity_type not in SCOPED_ENTITIES or payload is None:
                continue
            if entity_type == "audit_event" and is_unscoped_unknown_agent_auth_failure(
                entity_id, payload
            ):
                # Failed authentication for an unknown agent has no tenant
                # owner. Preserve it in the importer quarantine, never infer
                # ownership from another entity with the same identifier.
                quarantined_global_audit_keys.add((entity_type, entity_id))
                workspace_resolutions[(entity_type, entity_id)] = (None, None)
                continue
            explicit = payload.get("workspace_id")
            if explicit:
                parent_payload = dict(payload)
                parent_payload.pop("workspace_id", None)
                payload_by_key[(entity_type, entity_id)] = parent_payload
                parent_workspace = resolve_workspace(entity_type, entity_id)
                payload_by_key[(entity_type, entity_id)] = payload
            else:
                parent_workspace = resolve_workspace(entity_type, entity_id)
            resolved = explicit or parent_workspace
            workspace_resolutions[(entity_type, entity_id)] = (explicit, parent_workspace)
            if resolved and resolved not in workspaces:
                needs_review[entity_type] += 1
                ownership_review_keys.add((entity_type, entity_id))
            elif not resolved:
                needs_review[entity_type] += 1
                ownership_review_keys.add((entity_type, entity_id))
            elif explicit and parent_workspace and explicit != parent_workspace:
                conflicts[entity_type] += 1
                ownership_conflict_keys.add((entity_type, entity_id))

        parent_conflicts: Counter[str] = Counter()
        identity_conflicts: Counter[str] = Counter()
        identity_conflict_keys = {
            ("user", entity_id) for entity_id in duplicate_email_records
        }
        if identity_conflict_keys:
            identity_conflicts["user"] = len(identity_conflict_keys)
        parent_conflict_keys: set[tuple[str, str]] = set()

        def parent_payload(entity_type: str, entity_id: Any) -> dict[str, Any] | None:
            if not isinstance(entity_id, str) or not entity_id:
                return None
            return payload_by_key.get((entity_type, entity_id))

        for entity_type, entity_id, payload in parsed_rows:
            if payload is None:
                continue
            invalid_parent = False
            if (
                entity_type == "audit_event"
                and isinstance(payload.get("event_type"), str)
                and payload["event_type"].startswith("agent_worker.")
            ):
                worker_event_type = payload["event_type"]
                if (
                    worker_event_type == "agent_worker.container_validation_configured"
                    or parent_payload("agent_task", payload.get("entity_id")) is None
                ):
                    invalid_parent = True
            project_id = payload.get("project_id")
            if entity_type in {"idea", "work_order", "approval_request", "agent_task", "mcp_child_server", "mcp_pending_request"} and project_id:
                if parent_payload("project", project_id) is None:
                    invalid_parent = True

            if entity_type == "work_order" and payload.get("idea_id"):
                idea = parent_payload("idea", payload.get("idea_id"))
                if idea is None or (
                    idea.get("project_id") is not None
                    and idea.get("project_id") != project_id
                ):
                    invalid_parent = True

            if entity_type == "approval_request" and payload.get("work_order_id"):
                work_order = parent_payload("work_order", payload.get("work_order_id"))
                if work_order is None or work_order.get("project_id") != project_id:
                    invalid_parent = True

            if entity_type == "agent_task" and payload.get("approval_request_id"):
                approval = parent_payload("approval_request", payload.get("approval_request_id"))
                if approval is None or (
                    project_id is not None and approval.get("project_id") != project_id
                ):
                    invalid_parent = True

            if entity_type == "agent_run":
                task = parent_payload("agent_task", payload.get("task_id"))
                if task is None:
                    invalid_parent = True
                else:
                    if project_id and task.get("project_id") and project_id != task.get("project_id"):
                        invalid_parent = True
                    approval_id = payload.get("approval_request_id")
                    if approval_id and approval_id != task.get("approval_request_id"):
                        invalid_parent = True

            if entity_type == "agent_task_event" and parent_payload("agent_task", payload.get("task_id")) is None:
                invalid_parent = True

            if entity_type in {"agent_run_step", "agent_run_event", "agent_run_fault"}:
                run = parent_payload("agent_run", payload.get("run_id"))
                if run is None or (
                    payload.get("task_id")
                    and run.get("task_id") != payload.get("task_id")
                ):
                    invalid_parent = True

            if entity_type == "mcp_pending_request" and payload.get("approval_request_id"):
                approval = parent_payload("approval_request", payload.get("approval_request_id"))
                if approval is None or approval.get("project_id") != project_id:
                    invalid_parent = True

            if entity_type == "workspace":
                owner_id = payload.get("owner_user_id")
                owner = parent_payload("user", owner_id)
                if owner is None or (
                    owner.get("workspace_id") is not None
                    and owner.get("workspace_id") != entity_id
                ) or (
                    isinstance(owner_id, str)
                    and owner_id in workspace_owner_conflicts
                ):
                    invalid_parent = True

            if entity_type == "user" and payload.get("workspace_id"):
                if parent_payload("workspace", payload.get("workspace_id")) is None:
                    invalid_parent = True

            if entity_type in {"session", "device_registration"}:
                user = parent_payload("user", payload.get("user_id"))
                if user is None:
                    invalid_parent = True
                else:
                    user_workspace = user.get("workspace_id") or inferred_owner_workspace(
                        user.get("id")
                    )
                    child_workspace = payload.get("workspace_id")
                    if entity_type == "device_registration" and (
                        not user_workspace
                        or (child_workspace and child_workspace != user_workspace)
                    ):
                        invalid_parent = True

            if entity_type == "invite":
                creator = parent_payload("user", payload.get("created_by_user_id"))
                workspace_id = payload.get("workspace_id")
                creator_workspace = (
                    creator.get("workspace_id") or inferred_owner_workspace(creator.get("id"))
                    if creator is not None
                    else None
                )
                if (
                    creator is None
                    or not workspace_id
                    or parent_payload("workspace", workspace_id) is None
                    or creator_workspace != workspace_id
                ):
                    invalid_parent = True
                accepted_user_id = payload.get("accepted_by_user_id")
                if accepted_user_id:
                    accepted_user = parent_payload("user", accepted_user_id)
                    accepted_workspace = (
                        accepted_user.get("workspace_id")
                        or inferred_owner_workspace(accepted_user.get("id"))
                        if accepted_user is not None
                        else None
                    )
                    if accepted_user is None or accepted_workspace != workspace_id:
                        invalid_parent = True

            if entity_type in {"external_agent", "agent_connector"}:
                for allowed_project_id in payload.get("allowed_project_ids", []):
                    allowed_project = parent_payload("project", allowed_project_id)
                    if allowed_project is None or (
                        payload.get("workspace_id") is not None
                        and allowed_project.get("workspace_id") != payload.get("workspace_id")
                    ):
                        invalid_parent = True
                creator_id = payload.get("created_by_user_id")
                creator = parent_payload("user", creator_id) if creator_id else None
                if creator_id and creator is None:
                    invalid_parent = True
                elif creator is not None and payload.get("workspace_id") is not None:
                    creator_workspace = creator.get("workspace_id") or inferred_owner_workspace(
                        creator.get("id")
                    )
                    if creator_workspace != payload.get("workspace_id"):
                        invalid_parent = True

            if entity_type in {"capability_lease", "reviewer_attestation"}:
                if parent_payload("approval_request", payload.get("approval_request_id")) is None:
                    invalid_parent = True

            if invalid_parent:
                parent_conflicts[entity_type] += 1
                parent_conflict_keys.add((entity_type, entity_id))

        report["entity_counts"] = dict(sorted(counts.items()))
        report["unknown_entity_counts"] = dict(sorted(unknown.items()))
        report["invalid_payload_counts"] = dict(sorted(invalid.items()))
        report["ownership_review_counts"] = dict(sorted(needs_review.items()))
        report["unscoped_auth_failure_quarantine_count"] = len(
            quarantined_global_audit_keys
        )
        report["ownership_conflict_counts"] = dict(sorted(conflicts.items()))
        report["identity_conflict_counts"] = dict(sorted(identity_conflicts.items()))
        report["parent_relationship_conflict_counts"] = dict(sorted(parent_conflicts.items()))
        record_findings: list[dict[str, Any]] = []
        # Aggregate counters alone do not identify records. Build row-level
        # findings from the exact audited records, without copying their payloads.
        for entity_type, entity_id, payload in parsed_rows:
            key = (entity_type, entity_id)
            reasons: list[str] = []
            if key in invalid_records:
                reasons.append("invalid_model_payload")
            if key in unknown_records:
                reasons.append("unknown_entity_type")
            if key in ownership_review_keys:
                explicit, inferred = workspace_resolutions.get(key, (None, None))
                resolved = explicit or inferred
                reasons.append(
                    "workspace_not_registered" if resolved else "workspace_unresolved"
                )
            if key in ownership_conflict_keys:
                reasons.append("workspace_parent_mismatch")
            if key in parent_conflict_keys:
                reasons.append("parent_relationship_conflict")
                if (
                    entity_type == "audit_event"
                    and payload is not None
                    and isinstance(payload.get("event_type"), str)
                    and payload["event_type"].startswith("agent_worker.")
                    and (
                        payload["event_type"] == "agent_worker.container_validation_configured"
                        or parent_payload("agent_task", payload.get("entity_id")) is None
                    )
                ):
                    reasons.append("worker_audit_parent_unresolved")
                if (
                    entity_type == "workspace"
                    and payload is not None
                    and isinstance(payload.get("owner_user_id"), str)
                    and payload.get("owner_user_id") in workspace_owner_conflicts
                ):
                    reasons.append("owner_has_multiple_workspaces")
            if key in identity_conflict_keys:
                reasons.append("duplicate_normalized_email")
            if entity_type == "user" and entity_id in workspace_owner_conflicts:
                reasons.append("owner_has_multiple_workspaces")
            if reasons:
                explicit, inferred = workspace_resolutions.get(key, (None, None))
                record_findings.append(
                    {
                        "entity_type": entity_type,
                        "entity_id": entity_id,
                        "explicit_workspace_id": explicit,
                        "inferred_workspace_id": inferred,
                        "findings": sorted(set(reasons)),
                    }
                )
        report["record_findings"] = sorted(
            record_findings,
            key=lambda item: (item["entity_type"], item["entity_id"]),
        )
        if invalid:
            report["findings"].append("invalid_model_payloads")
        if unknown:
            report["findings"].append("unknown_entity_types")
        if needs_review:
            report["findings"].append("ownership_requires_operator_mapping")
        if conflicts:
            report["findings"].append("ownership_parent_mismatch")
        if parent_conflicts:
            report["findings"].append("parent_relationship_conflicts")
        if workspace_owner_conflicts:
            report["findings"].append("workspace_owner_has_multiple_workspaces")
        if identity_conflict_keys:
            report["findings"].append("duplicate_normalized_user_email")
        # Recheck before declaring a pass so a sidecar created while the
        # read-only audit was running cannot be missed. If one was present at
        # open time, retain the finding even if it disappeared meanwhile.
        report["wal_sidecar_present"] = report["wal_sidecar_present"] or wal_sidecar.exists()
        report["shm_sidecar_present"] = report["shm_sidecar_present"] or shm_sidecar.exists()
        if report["wal_sidecar_present"] and "sqlite_wal_sidecar_present" not in report["findings"]:
            report["findings"].append("sqlite_wal_sidecar_present")
        if report["shm_sidecar_present"] and "sqlite_shm_sidecar_present" not in report["findings"]:
            report["findings"].append("sqlite_shm_sidecar_present")
        if not report["findings"]:
            report["status"] = "STRUCTURAL_REVIEW_PASSED"
        return report
    finally:
        conn.rollback()
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path, help="Existing SQLite file to inspect; opened read-only")
    parser.add_argument("--output", type=Path, help="Create a new JSON report file (must not already exist)")
    args = parser.parse_args()
    try:
        report = audit(args.database)
        rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.output:
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(rendered)
        else:
            sys.stdout.write(rendered)
        return 0 if report["status"] == "STRUCTURAL_REVIEW_PASSED" else 2
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"audit failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
