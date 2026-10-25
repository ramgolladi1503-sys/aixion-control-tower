#!/usr/bin/env python3
"""Read-only verifier for a live Antigravity-to-phone approval round trip.

This tool does not create approvals or alter data. Run it after approving a
fresh, unique harmless command in the phone app and observing that Antigravity
resumed that same command. It validates the durable PostgreSQL records and the
command's fresh filesystem marker; it cannot replace the human/device proof.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any


class EvidenceError(Exception):
    """The supplied live evidence is incomplete or inconsistent."""


DEFAULT_MAX_CERTIFICATION_AGE_SECONDS = 900
MAX_CERTIFICATION_AGE_SECONDS = 3600


def _parse_timestamp(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamp must be ISO-8601 with timezone") from exc
    if result.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return result.astimezone(timezone.utc)


def _json_object(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError("expected arguments must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("expected arguments must be a JSON object")
    return parsed


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _timestamp_utc(value: Any, *, label: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise EvidenceError(f"{label} is not a valid ISO-8601 timestamp") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise EvidenceError(f"{label} is missing or has no timezone")
    return value.astimezone(timezone.utc)


def _verify_evidence_freshness(
    *,
    approval: dict[str, Any],
    decision_event: dict[str, Any],
    claim: dict[str, Any],
    claim_event: dict[str, Any],
    started_at: datetime,
    max_age_seconds: int,
    now: datetime | None = None,
) -> dict[str, datetime]:
    start = _timestamp_utc(started_at, label="Certification start time")
    current = _timestamp_utc(now or datetime.now(timezone.utc), label="Current time")
    if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= MAX_CERTIFICATION_AGE_SECONDS:
        raise EvidenceError(
            f"Maximum certification age must be from 1 to {MAX_CERTIFICATION_AGE_SECONDS} seconds"
        )
    if start > current or current - start > timedelta(seconds=max_age_seconds):
        raise EvidenceError("Certification start time is in the future or outside the fresh-run window")

    timestamps = {
        "approval_created_at": _timestamp_utc(approval.get("created_at"), label="Approval creation time"),
        "decision_created_at": _timestamp_utc(decision_event.get("created_at"), label="Approval decision time"),
        "claim_created_at": _timestamp_utc(claim.get("claimed_at"), label="Delivery claim time"),
        "claim_event_created_at": _timestamp_utc(
            claim_event.get("created_at"), label="Delivery claim audit time"
        ),
    }
    if timestamps["approval_created_at"] < start:
        raise EvidenceError("Approval predates this certification run")
    if timestamps["decision_created_at"] < timestamps["approval_created_at"]:
        raise EvidenceError("Approval decision predates approval creation")
    if timestamps["claim_created_at"] < timestamps["decision_created_at"]:
        raise EvidenceError("Delivery claim predates the approval decision")
    if timestamps["claim_event_created_at"] < timestamps["claim_created_at"]:
        raise EvidenceError("Delivery claim audit event predates the durable delivery claim")
    return timestamps


def _computed_approved_payload_hash(payload: dict[str, Any]) -> str:
    """Recompute the frozen approval hash using the backend v1/v2 contract."""
    version = payload.get("approved_payload_hash_version", 1)
    if type(version) is not int or version not in {1, 2}:
        raise EvidenceError("Approval payload hash version is unsupported")
    try:
        snapshot = {
            "project_id": payload["project_id"],
            "work_order_id": payload.get("work_order_id"),
            "title": payload["title"],
            "summary": payload["summary"],
            "agent_name": payload["agent_name"],
            "target_branch": payload["target_branch"],
            "files": [
                {
                    "path": item["path"],
                    "change_type": item["change_type"],
                    "diff": item["diff"],
                    "new_content": item.get("new_content"),
                }
                for item in payload["files"]
            ],
            "test_plan": list(payload.get("test_plan", [])),
            "rollback_plan": payload.get("rollback_plan", ""),
            "source_provider": payload["source_provider"],
            "source_agent_id": payload.get("source_agent_id"),
            "source_agent_name": payload.get("source_agent_name"),
            "source_session_id": payload.get("source_session_id"),
            "source_invocation_id": payload.get("source_invocation_id"),
            "source_task_url": payload.get("source_task_url"),
        }
        if version == 2:
            snapshot["workspace_id"] = payload["workspace_id"]
            snapshot["risk"] = payload["risk"]
    except (KeyError, TypeError) as exc:
        raise EvidenceError("Approval payload is incomplete for its hash version") from exc
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return "sha256:" + digest if version == 1 else "sha256:v2:" + digest


def _verify_payload(
    payload: dict[str, Any], *, tool_name: str, expected_arguments: dict[str, Any]
) -> None:
    files = payload.get("files")
    if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict):
        raise EvidenceError("Approval does not contain exactly one Antigravity tool payload")
    file_change = files[0]
    if file_change.get("path") != f"antigravity://tool/{tool_name[:120]}":
        raise EvidenceError("Approval tool name does not match the requested tool")
    if file_change.get("change_type") != "tool_call":
        raise EvidenceError("Approval is not marked as an Antigravity tool call")
    try:
        actual_arguments = json.loads(file_change.get("diff", ""))
    except (TypeError, json.JSONDecodeError) as exc:
        raise EvidenceError("Approval tool arguments are not valid JSON") from exc
    if not isinstance(actual_arguments, dict) or _canonical_json(actual_arguments) != _canonical_json(
        expected_arguments
    ):
        raise EvidenceError("Approved arguments do not exactly match the expected command")


def verify_records(
    *,
    approval: dict[str, Any],
    decision_events: list[dict[str, Any]],
    claim: dict[str, Any] | None,
    claim_events: list[dict[str, Any]],
    expected_approval_id: str,
    expected_agent_id: str,
    expected_project_id: str,
    expected_workspace_id: str,
    tool_name: str,
    expected_arguments: dict[str, Any],
    expected_started_at: datetime,
    max_age_seconds: int = DEFAULT_MAX_CERTIFICATION_AGE_SECONDS,
) -> dict[str, Any]:
    """Validate DB rows after fetching them from one PostgreSQL snapshot."""
    payload = approval.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise EvidenceError("Approval payload is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise EvidenceError("Approval payload is missing")
    if not isinstance(expected_workspace_id, str) or not expected_workspace_id.strip():
        raise EvidenceError("Expected workspace identity is required")
    if approval.get("workspace_id") != expected_workspace_id:
        raise EvidenceError("Approval database workspace scope does not match")
    if payload.get("workspace_id") != expected_workspace_id:
        raise EvidenceError("Approval payload workspace scope does not match")
    if approval.get("approval_id") != expected_approval_id or payload.get("id") != expected_approval_id:
        raise EvidenceError("Approval identity does not match")
    if approval.get("status") != "APPROVED" or payload.get("status") != "APPROVED":
        raise EvidenceError("Approval is not durably approved")
    if payload.get("source_provider") != "ANTIGRAVITY":
        raise EvidenceError("Approval provider is not ANTIGRAVITY")
    if approval.get("source_agent_id") != expected_agent_id or payload.get("source_agent_id") != expected_agent_id:
        raise EvidenceError("Approval agent identity does not match")
    if payload.get("project_id") != expected_project_id:
        raise EvidenceError("Approval project scope does not match")
    invocation_id = payload.get("source_invocation_id")
    if not isinstance(invocation_id, str) or not invocation_id.startswith("antigravity:v1:"):
        raise EvidenceError("Approval has no valid Antigravity invocation identity")
    approved_hash = payload.get("approved_payload_hash")
    if not isinstance(approved_hash, str) or not approved_hash:
        raise EvidenceError("Approval has no approved payload hash")
    if approved_hash != _computed_approved_payload_hash(payload):
        raise EvidenceError("Approval payload does not match its frozen payload hash")
    _verify_payload(payload, tool_name=tool_name, expected_arguments=expected_arguments)

    if len(decision_events) != 1:
        raise EvidenceError("Expected exactly one approval decision audit event")
    decision = decision_events[0]
    details = decision.get("details")
    if isinstance(details, str):
        details = json.loads(details)
    if (
        decision.get("event_type") != "approval.decision"
        or decision.get("approval_id") != expected_approval_id
        or decision.get("workspace_id") != expected_workspace_id
        or not isinstance(details, dict)
        or details.get("new_status") != "APPROVED"
        or details.get("approved_payload_hash") != approved_hash
    ):
        raise EvidenceError("Decision audit event does not bind the approved payload hash")

    if not isinstance(claim, dict):
        raise EvidenceError("No durable at-most-once delivery claim exists")
    if (
        claim.get("approval_id") != expected_approval_id
        or claim.get("workspace_id") != expected_workspace_id
        or claim.get("source_agent_id") != expected_agent_id
        or claim.get("source_invocation_id") != invocation_id
    ):
        raise EvidenceError("Delivery claim does not match this exact approval invocation")
    if len(claim_events) != 1:
        raise EvidenceError("Expected exactly one approval.delivery_claimed audit event")
    claim_event = claim_events[0]
    claim_details = claim_event.get("details")
    if isinstance(claim_details, str):
        claim_details = json.loads(claim_details)
    if (
        claim_event.get("event_type") != "approval.delivery_claimed"
        or claim_event.get("approval_id") != expected_approval_id
        or claim_event.get("workspace_id") != expected_workspace_id
        or not isinstance(claim_details, dict)
        or claim_details.get("source_agent_id") != expected_agent_id
        or claim_details.get("source_invocation_id") != invocation_id
        or claim_details.get("approved_payload_hash") != approved_hash
    ):
        raise EvidenceError("Delivery audit event does not match the approved invocation")
    freshness = _verify_evidence_freshness(
        approval=approval,
        decision_event=decision,
        claim=claim,
        claim_event=claim_event,
        started_at=expected_started_at,
        max_age_seconds=max_age_seconds,
    )
    return {
        "approval_id": expected_approval_id,
        "agent_id": expected_agent_id,
        "project_id": expected_project_id,
        "workspace_id": expected_workspace_id,
        "source_invocation_id": invocation_id,
        "approved_payload_hash": approved_hash,
        "decision_events": len(decision_events),
        "delivery_claim_events": len(claim_events),
        **{key: value.isoformat() for key, value in freshness.items()},
    }


def _connect(dsn: str):
    try:
        import psycopg
    except ImportError as exc:
        raise EvidenceError("Install backend requirements to use the PostgreSQL verifier") from exc
    return psycopg.connect(dsn, connect_timeout=5, autocommit=False)


def verify_database(args: argparse.Namespace) -> dict[str, Any]:
    dsn = args.dsn or os.environ.get("AIXION_E2E_POSTGRES_DSN")
    if not dsn:
        raise EvidenceError("Set --dsn or AIXION_E2E_POSTGRES_DSN for the disposable PostgreSQL database")
    conn = _connect(dsn)
    try:
        host = (conn.info.host or "").lower()
        database = (conn.info.dbname or "").lower()
        if host and host not in {"localhost", "127.0.0.1", "::1"} and not host.startswith("/"):
            raise EvidenceError("Verifier accepts only loopback or Unix-socket PostgreSQL hosts")
        if not any(label in database for label in ("e2e", "test", "disposable")):
            raise EvidenceError("Database name must identify an E2E, test, or disposable database")
        with conn.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                "SELECT approval_id, source_agent_id, status, payload, workspace_id, created_at "
                "FROM approval_requests WHERE approval_id=%s",
                (args.approval_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise EvidenceError("Approval was not found in the configured PostgreSQL database")
            approval = {
                "approval_id": row[0], "source_agent_id": row[1], "status": row[2],
                "payload": row[3], "workspace_id": row[4], "created_at": row[5],
            }
            cursor.execute(
                "SELECT approval_id, event_type, details, workspace_id, created_at FROM audit_events "
                "WHERE approval_id=%s AND event_type='approval.decision' "
                "AND details->>'new_status'='APPROVED'",
                (args.approval_id,),
            )
            decision_events = [
                {"approval_id": r[0], "event_type": r[1], "details": r[2], "workspace_id": r[3], "created_at": r[4]}
                for r in cursor.fetchall()
            ]
            cursor.execute(
                "SELECT approval_id, source_agent_id, source_invocation_id, workspace_id, claimed_at "
                "FROM approval_delivery_claims WHERE approval_id=%s",
                (args.approval_id,),
            )
            claim_row = cursor.fetchone()
            claim = None if claim_row is None else {
                "approval_id": claim_row[0], "source_agent_id": claim_row[1],
                "source_invocation_id": claim_row[2], "workspace_id": claim_row[3],
                "claimed_at": claim_row[4],
            }
            cursor.execute(
                "SELECT approval_id, event_type, details, workspace_id, created_at FROM audit_events "
                "WHERE approval_id=%s AND event_type='approval.delivery_claimed'",
                (args.approval_id,),
            )
            claim_events = [
                {"approval_id": r[0], "event_type": r[1], "details": r[2], "workspace_id": r[3], "created_at": r[4]}
                for r in cursor.fetchall()
            ]
        conn.rollback()  # End the enforced read-only transaction without committing.
    finally:
        conn.close()
    result = verify_records(
        approval=approval,
        decision_events=decision_events,
        claim=claim,
        claim_events=claim_events,
        expected_approval_id=args.approval_id,
        expected_agent_id=args.agent_id,
        expected_project_id=args.project_id,
        expected_workspace_id=args.workspace_id,
        tool_name=args.tool_name,
        expected_arguments=args.expected_arguments,
        expected_started_at=args.started_at,
        max_age_seconds=args.max_age_seconds,
    )
    marker = Path(args.marker).expanduser()
    try:
        marker_stat = marker.stat()
    except OSError as exc:
        raise EvidenceError("Expected command-created marker does not exist") from exc
    claimed_at = _timestamp_utc(result["claim_created_at"], label="Delivery claim time")
    if marker_stat.st_mtime < claimed_at.timestamp():
        raise EvidenceError("Marker predates the durable delivery claim; use a fresh unique marker path")
    result["marker_path"] = str(marker.resolve())
    result["marker_mtime_utc"] = datetime.fromtimestamp(
        marker_stat.st_mtime, timezone.utc
    ).isoformat()
    result["result"] = "PASS_DURABLE_APPROVAL_AND_FRESH_MARKER"
    result["human_observation_required"] = (
        "Confirm Antigravity resumed the same pending call after the phone decision; "
        "this script cannot observe the provider UI."
    )
    result["physical_phone_provider_flow_proven"] = False
    result["application_or_hook_revision_proven"] = False
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approval-id", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--tool-name", required=True)
    parser.add_argument("--expected-arguments", type=_json_object, required=True)
    parser.add_argument("--marker", required=True, help="Unique path created only by the approved tool call")
    parser.add_argument("--started-at", required=True, type=_parse_timestamp)
    parser.add_argument(
        "--max-age-seconds",
        type=int,
        default=DEFAULT_MAX_CERTIFICATION_AGE_SECONDS,
        help=(
            "Maximum age of --started-at (default: %(default)s; max: "
            f"{MAX_CERTIFICATION_AGE_SECONDS}). A timed-out run needs a fresh invocation."
        ),
    )
    parser.add_argument("--dsn", help="Disposable PostgreSQL DSN; may instead use AIXION_E2E_POSTGRES_DSN")
    args = parser.parse_args(argv)
    try:
        result = verify_database(args)
    except Exception as exc:
        sys.stderr.write(f"E2E evidence verification failed: {exc.__class__.__name__}: {exc}\n")
        return 1
    sys.stdout.write(json.dumps(result, sort_keys=True, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
