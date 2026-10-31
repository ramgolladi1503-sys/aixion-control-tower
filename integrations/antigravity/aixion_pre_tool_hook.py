#!/usr/bin/env python3
"""Fail-closed Antigravity PreToolUse gate backed by Control Tower approvals."""
from __future__ import annotations

import hashlib
import argparse
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urlencode, urlsplit

# Antigravity launches this hook as a standalone script, where sys.path starts
# at integrations/antigravity rather than at the repository root.
_REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPOSITORY_ROOT not in sys.path:
    sys.path.insert(0, _REPOSITORY_ROOT)

from integrations.aixion_deadline_http import request as deadline_request  # noqa: E402
from integrations.codex import host_security  # noqa: E402

MAX_INPUT_BYTES = 128 * 1024
MAX_ARGUMENTS_BYTES = 48 * 1024
MAX_CONFIG_BYTES = 16 * 1024
MAX_POLL_SECONDS = 15 * 60
POLL_INTERVAL_SECONDS = 2
INVOCATION_HEARTBEAT_INTERVAL_SECONDS = 15
INVOCATION_HEARTBEAT_RETRY_DELAY_SECONDS = 0.1
REQUEST_TIMEOUT_SECONDS = 10
HEARTBEAT_RATE_LIMIT_RETRY_MAX_SECONDS = (
    INVOCATION_HEARTBEAT_INTERVAL_SECONDS - REQUEST_TIMEOUT_SECONDS - 1
)
EXPIRY_CLEANUP_GRACE_SECONDS = 1
MAX_RECONNECT_BACKOFF_SECONDS = 10
MAX_CREATE_ATTEMPTS = 2
CREATE_RETRY_DELAY_SECONDS = 0.25
MAX_RESPONSE_BYTES = 256 * 1024
MAX_HEALTH_RESPONSE_BYTES = 64 * 1024
APPROVAL_PROTOCOL_VERSION = "1"
DIAGNOSTIC_APPROVAL_ID = re.compile(r"^approval_[a-f0-9]{12}$")
HEARTBEAT_FAILURE_CODES = frozenset({
    "host_deadline_expired",
    "rate_limited",
    "heartbeat_request_failed",
    "heartbeat_retry_exhausted",
    "heartbeat_retry_deadline_exhausted",
    "response_not_object",
    "approval_identity_mismatch",
    "connector_not_live",
    "heartbeat_timestamp_invalid",
    "lease_expired_or_invalid",
    "lease_timing_invalid",
})
SENSITIVE_KEY = re.compile(r"(token|secret|password|credential|private.?key|api.?key)", re.I)
SENSITIVE_VALUE = re.compile(
    r"(?:bearer\s+[A-Za-z0-9._~-]{8,}|(?:api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*[\"']?[A-Za-z0-9._~/-]{8,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)",
    re.I,
)
# Command arguments are sent verbatim to the approval API and phone. Reject
# credential option names even when their values do not resemble known tokens.
SENSITIVE_OPTION = re.compile(
    r"(?:^|\s)--?(?:password|passwd|pass|token|access[-_]?token|refresh[-_]?token|"
    r"api[-_]?key|secret|client[-_]?secret|with[-_]?token)(?=\s|=|:|$)",
    re.I,
)
MYSQL_COMMAND = re.compile(r"(?:^|[\s;&|])(?:sudo\s+)?(?:\S*/)?(?:mysql|mariadb)(?=\s|$)", re.I)
MYSQL_PASSWORD_OPTION = re.compile(r"(?:^|\s)-p\S*(?=\s|=|:|$)")
SSHPASS_COMMAND = re.compile(r"(?:^|[\s;&|])(?:sudo\s+)?(?:\S*/)?sshpass(?=\s|$)", re.I)
SSHPASS_PASSWORD_OPTION = re.compile(r"(?:^|\s)-p(?=\s|=|:|$|\S)")
READ_ONLY_TOOLS = frozenset({
    "view_file", "list_dir", "find_by_name", "grep_search",
    "search_web", "read_url_content", "list_permissions",
})
READ_PATH_ARGUMENTS = frozenset({"absolutepath", "directorypath", "searchdirectory", "searchpath"})
READ_ONLY_PATH_ARGUMENTS = {
    "view_file": "absolutepath",
    "list_dir": "directorypath",
    "find_by_name": "searchdirectory",
    "grep_search": "searchpath",
}
SAFE_ENV_EXAMPLES = frozenset({".env.example", ".env.sample", ".env.template", ".env.dist"})
SENSITIVE_PATH_COMPONENT = re.compile(
    r"^(?:\.env(?:$|[._-].*)|\.ssh|\.aws|\.azure|\.gnupg|\.netrc|\.npmrc|\.pypirc|"
    r"id_(?:rsa|dsa|ecdsa|ed25519)(?:$|[._-].*)|authorized_keys|known_hosts|"
    r".*(?:secret|credential|password|private[_-]?key|access[_-]?token|api[_-]?key).*|"
    r".*\.(?:pem|key|p12|pfx))$",
    re.I,
)
DEFAULT_CONFIG_PATH = os.path.expanduser("~/.config/aixion/antigravity.json")


class GateError(Exception):
    pass


class AmbiguousRequestError(GateError):
    """A request may have committed even though its response was not usable."""


class RateLimitError(GateError):
    def __init__(self, retry_after_seconds: int, *, retry_after_header: str | None = None) -> None:
        self.retry_after_seconds = max(1, min(MAX_POLL_SECONDS, retry_after_seconds))
        self.retry_after_header = retry_after_header
        super().__init__("Control Tower rate limit exceeded")


def _new_diagnostic_context() -> dict[str, str | None]:
    return {"correlation_id": None, "approval_id": None}


def _emit_diagnostic(
    *,
    context: dict[str, str | None],
    event: str,
    attempt: int | None = None,
    decision: str | None = None,
    failure_code: str | None = None,
) -> None:
    """Write fixed-field, content-free diagnostics without affecting the gate."""
    record = {
        "component": "aixion_approval_hook",
        "provider": "ANTIGRAVITY",
        "event": event,
        "correlation_id": context.get("correlation_id"),
        "approval_id": context.get("approval_id"),
        "attempt": attempt,
        "decision": decision,
    }
    if failure_code is not None:
        # Failure codes are fixed internal labels; never pass server text,
        # request payloads, or exception messages through this field.
        if failure_code not in HEARTBEAT_FAILURE_CODES:
            return
        record["failure_code"] = failure_code
    try:
        sys.stderr.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError:
        # Diagnostic output must never change an authorization result.
        return


def _emit_heartbeat_failure(
    diagnostics: dict[str, str | None] | None,
    failure_code: str,
) -> None:
    if diagnostics is None:
        return
    _emit_diagnostic(
        context=diagnostics,
        event="invocation_heartbeat_failed",
        failure_code=failure_code,
    )


def _short_correlation_id(source_invocation_id: str) -> str:
    return hashlib.sha256(source_invocation_id.encode("utf-8")).hexdigest()[:16]


def _lease_expiry_is_current(value: Any) -> bool:
    """Reject malformed, timezone-less, or already-expired lease responses."""
    if not isinstance(value, str) or not value:
        return False
    try:
        expires_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if expires_at.tzinfo is None:
        return False
    return expires_at > datetime.now(timezone.utc)


def _heartbeat_timestamp_is_valid(value: Any) -> bool:
    """Reject malformed or timezone-less heartbeat timestamps."""
    if not isinstance(value, str) or not value:
        return False
    try:
        heartbeat_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return heartbeat_at.tzinfo is not None


def _validated_diagnostic_approval_id(value: Any) -> str | None:
    if isinstance(value, str) and DIAGNOSTIC_APPROVAL_ID.fullmatch(value):
        return value
    return None


def _create_approval_with_retry(
    *,
    api_base: str,
    agent_id: str,
    token: str,
    body: dict[str, Any],
    diagnostics: dict[str, str | None] | None = None,
) -> dict[str, Any] | None:
    """Retry one ambiguous create using the exact same invocation and payload.

    Each HTTP attempt has a 10-second transport timeout. Two attempts and one
    short delay bound this recovery path to at most 20.25 seconds.
    """
    for attempt in range(MAX_CREATE_ATTEMPTS):
        try:
            return _request("POST", f"{api_base}/agents/approvals", agent_id, token, body)
        except AmbiguousRequestError:
            if attempt + 1 == MAX_CREATE_ATTEMPTS:
                if diagnostics is not None:
                    _emit_diagnostic(
                        context=diagnostics,
                        event="approval_create_retry_exhausted",
                        attempt=attempt + 1,
                    )
                return None
            if diagnostics is not None:
                _emit_diagnostic(
                    context=diagnostics,
                    event="approval_create_retry",
                    attempt=attempt + 2,
                )
            time.sleep(CREATE_RETRY_DELAY_SECONDS)
    return None


def _decision(value: str, reason: str) -> dict[str, str]:
    return {"decision": value, "reason": reason[:500]}


def _emit(value: dict[str, str]) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _reject_nonstandard_json_constant(value: str) -> None:
    """Reject JavaScript-style numeric constants accepted by Python's JSON parser."""
    raise ValueError(f"Non-standard JSON constant: {value}")


def _parse_finite_json_float(value: str) -> float:
    """Reject valid JSON exponents that overflow Python's finite float range."""
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("JSON number is outside the finite range")
    return parsed


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject ambiguous JSON objects instead of silently keeping the last key."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _read_input() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise GateError("Hook input exceeds the configured size limit")
    try:
        value = json.loads(
            raw,
            parse_float=_parse_finite_json_float,
            parse_constant=_reject_nonstandard_json_constant,
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise GateError("Hook input is invalid JSON") from exc
    if not isinstance(value, dict):
        raise GateError("Hook input must be a JSON object")
    return value


def _has_sensitive_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(SENSITIVE_KEY.search(str(key)) or _has_sensitive_key(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_has_sensitive_key(item) for item in value)
    return False


def _has_sensitive_value(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_has_sensitive_value(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_sensitive_value(item) for item in value)
    return isinstance(value, str) and (
        SENSITIVE_VALUE.search(value) is not None
        or SENSITIVE_OPTION.search(value) is not None
        or (
            MYSQL_COMMAND.search(value) is not None
            and MYSQL_PASSWORD_OPTION.search(value) is not None
        )
        or (
            SSHPASS_COMMAND.search(value) is not None
            and SSHPASS_PASSWORD_OPTION.search(value) is not None
        )
    )


def _has_sensitive_path(args: Any) -> bool:
    """Identify secret-bearing paths, including symlink targets, in Antigravity read arguments."""
    if not isinstance(args, dict):
        return False
    for key, value in args.items():
        normalized_key = re.sub(r"[^a-z]", "", str(key).lower())
        if normalized_key == "pattern" and isinstance(value, str):
            pattern = value.replace("\\", "/").strip()
            components = [part for part in pattern.split("/") if part and part not in {".", "**"}]
            if len(components) == 1 and components[0].lower() in SAFE_ENV_EXAMPLES:
                continue
            if any(
                SENSITIVE_PATH_COMPONENT.fullmatch(part.lower())
                or re.fullmatch(r"\.env(?:\*|\?\[.*\])", part.lower())
                for part in components
            ):
                return True
            continue
        if normalized_key not in READ_PATH_ARGUMENTS or not isinstance(value, str):
            continue
        normalized_path = value.replace("\\", "/").strip()
        components = [part for part in normalized_path.split("/") if part and part != "."]
        for component in components:
            candidate = component.lower()
            if candidate in SAFE_ENV_EXAMPLES:
                continue
            if SENSITIVE_PATH_COMPONENT.fullmatch(candidate):
                return True
        # The read-only fast path must not treat a harmless-looking symlink as
        # safe when it resolves into a credential-bearing location. If local
        # path resolution is ambiguous (for example, a symlink loop), require
        # phone review rather than bypassing the approval gate.
        try:
            resolved_path = Path(value).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            return True
        for component in resolved_path.parts:
            candidate = component.lower()
            if candidate in SAFE_ENV_EXAMPLES:
                continue
            if SENSITIVE_PATH_COMPONENT.fullmatch(candidate):
                return True
    return False


def _has_documented_read_path(tool_name: str, args: Any) -> bool:
    """Require a known, non-empty path before bypassing approval for file reads."""
    expected_key = READ_ONLY_PATH_ARGUMENTS.get(tool_name)
    if expected_key is None:
        return True
    if not isinstance(args, dict):
        return False
    has_documented_path = False
    documented_path_count = 0
    for key, value in args.items():
        normalized_key = re.sub(r"[^a-z]", "", str(key).lower())
        if normalized_key == expected_key:
            documented_path_count += 1
            if documented_path_count > 1 or not isinstance(value, str) or not value.strip():
                return False
            has_documented_path = True
            continue
        if re.search(r"(?:path|directory|folder|file|root|location)", normalized_key):
            return False
    return has_documented_path


def _request(
    method: str,
    url: str,
    agent_id: str,
    token: str,
    body: dict[str, Any] | None = None,
    *,
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
    expected_status: int | None = None,
) -> dict[str, Any]:
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise GateError("Control Tower request timeout is invalid")
    if not 0 < timeout_seconds <= REQUEST_TIMEOUT_SECONDS:
        raise GateError("Control Tower request timeout is outside the allowed range")
    payload = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
    headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Aixion-Agent-Id": agent_id,
            "X-Aixion-Agent-Token": token,
            "X-Aixion-Approval-Protocol": APPROVAL_PROTOCOL_VERSION,
    }
    try:
        response = deadline_request(
            method,
            url,
            headers,
            payload,
            timeout_seconds,
            max_response_bytes=MAX_RESPONSE_BYTES,
        )
        if response.status == 429:
            retry_after_header = next(
                (value for key, value in response.headers.items() if key.lower() == "retry-after"),
                None,
            )
            retry_after_seconds = _parse_retry_after(retry_after_header)
            raise RateLimitError(retry_after_seconds, retry_after_header=retry_after_header)
        if response.status >= 500:
            raise AmbiguousRequestError("Control Tower returned an ambiguous server failure")
        if expected_status is not None and response.status != expected_status:
            raise GateError(f"Control Tower returned HTTP {response.status}")
        if not 200 <= response.status < 300:
            raise GateError(f"Control Tower returned HTTP {response.status}")
        content = response.body
        try:
            result = json.loads(
                content,
                parse_float=_parse_finite_json_float,
                parse_constant=_reject_nonstandard_json_constant,
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise AmbiguousRequestError("Control Tower returned invalid response data") from exc
        if not isinstance(result, dict):
            raise AmbiguousRequestError("Unexpected Control Tower response")
        return result
    except (TimeoutError, OSError) as exc:
        raise AmbiguousRequestError("Control Tower request outcome is unknown") from exc


def _parse_retry_after(
    value: str | None, *, now: datetime | None = None
) -> int:
    """Parse both RFC delta-seconds and HTTP-date Retry-After values.

    Invalid headers use the normal bounded polling interval. The approval wait
    loop still clamps every delay to its monotonic invocation deadline.
    """
    if value is None:
        return POLL_INTERVAL_SECONDS
    if re.fullmatch(r"[0-9]+", value):
        try:
            return min(MAX_POLL_SECONDS, int(value))
        except ValueError:
            return POLL_INTERVAL_SECONDS
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            return POLL_INTERVAL_SECONDS
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            return POLL_INTERVAL_SECONDS
        delay = max(0, math.ceil((retry_at - current).total_seconds()))
        return min(MAX_POLL_SECONDS, delay)
    except (OverflowError, TypeError, ValueError):
        return POLL_INTERVAL_SECONDS


def _valid_retry_after_header(value: str | None) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    if re.fullmatch(r"[0-9]+", value.strip()):
        return True
    try:
        parsed = parsedate_to_datetime(value)
        return parsed.tzinfo is not None
    except (OverflowError, TypeError, ValueError):
        return False


def _monotonic_expiry_deadline(server_now_at: str, expires_at: str, *,
                               request_started_at: float,
                               response_received_at: float) -> float | None:
    try:
        server_now = datetime.fromisoformat(server_now_at.strip().replace("Z", "+00:00"))
        expiry = datetime.fromisoformat(expires_at.strip().replace("Z", "+00:00"))
    except (AttributeError, OverflowError, TypeError, ValueError):
        return None
    if server_now.tzinfo is None or expiry.tzinfo is None or expiry <= server_now:
        return None
    remaining_seconds = (expiry - server_now).total_seconds()
    # Use the server's own heartbeat/expiry pair to avoid relying on host clock
    # synchronization. Subtract the full request duration and one second for
    # response/clock uncertainty, intentionally underestimating the lease.
    return remaining_seconds - (response_received_at - request_started_at) - 1 + response_received_at


def _refresh_invocation_lease_if_due(
    *,
    api_base: str,
    approval_id: str,
    agent_id: str,
    token: str,
    source_invocation_id: str,
    deadline: float,
    next_heartbeat_at: float,
    approval_expires_at: str | None = None,
    lease_state: dict[str, float | None] | None = None,
    diagnostics: dict[str, str | None] | None = None,
    now: float | None = None,
) -> tuple[float, bool]:
    """Renew the invocation lease when due, without extending the hook deadline."""
    now = time.monotonic() if now is None else now
    if now < next_heartbeat_at:
        return next_heartbeat_at, True
    remaining_seconds = deadline - now
    if remaining_seconds <= 0:
        _emit_heartbeat_failure(diagnostics, "host_deadline_expired")
        return next_heartbeat_at, False
    heartbeat: Any
    for attempt in range(2):
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            _emit_heartbeat_failure(diagnostics, "host_deadline_expired")
            return next_heartbeat_at, False
        try:
            request_started_at = time.monotonic()
            heartbeat = _request(
                "POST",
                f"{api_base}/agents/approvals/{approval_id}/heartbeat",
                agent_id,
                token,
                {"source_invocation_id": source_invocation_id},
                timeout_seconds=min(REQUEST_TIMEOUT_SECONDS, remaining_seconds),
                expected_status=200,
            )
            break
        except RateLimitError as exc:
            delay = exc.retry_after_seconds
            remaining_seconds = deadline - time.monotonic()
            if (
                attempt != 0
                or not _valid_retry_after_header(exc.retry_after_header)
                or delay > HEARTBEAT_RATE_LIMIT_RETRY_MAX_SECONDS
                or remaining_seconds <= delay + REQUEST_TIMEOUT_SECONDS + 1
                or lease_state is None
                or lease_state.get("deadline") is None
                or lease_state["deadline"] <= time.monotonic() + delay + REQUEST_TIMEOUT_SECONDS + 1
                or lease_state.get("approval_deadline") is None
                or lease_state["approval_deadline"] <= time.monotonic() + delay + REQUEST_TIMEOUT_SECONDS + 1
            ):
                _emit_heartbeat_failure(diagnostics, "rate_limited")
                return next_heartbeat_at, False
            if diagnostics is not None:
                _emit_diagnostic(
                    context=diagnostics,
                    event="invocation_heartbeat_retry",
                    attempt=attempt + 2,
                )
            time.sleep(delay)
        except GateError as exc:
            # Only retry a transport failure whose original exception is
            # preserved. HTTP errors and malformed/ambiguous responses remain
            # fail-closed; the retry repeats the same idempotent invocation ID.
            transport_failure = isinstance(exc, AmbiguousRequestError) and isinstance(
                exc.__cause__, (TimeoutError, OSError)
            )
            if attempt != 0 or not transport_failure:
                _emit_heartbeat_failure(
                    diagnostics,
                    "heartbeat_retry_exhausted" if attempt != 0 else "heartbeat_request_failed",
                )
                return next_heartbeat_at, False
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= INVOCATION_HEARTBEAT_RETRY_DELAY_SECONDS:
                _emit_heartbeat_failure(diagnostics, "heartbeat_retry_deadline_exhausted")
                return next_heartbeat_at, False
            if diagnostics is not None:
                _emit_diagnostic(
                    context=diagnostics,
                    event="invocation_heartbeat_retry",
                    attempt=attempt + 2,
                )
            time.sleep(INVOCATION_HEARTBEAT_RETRY_DELAY_SECONDS)
    if time.monotonic() >= deadline:
        _emit_heartbeat_failure(diagnostics, "host_deadline_expired")
        return next_heartbeat_at, False
    if not isinstance(heartbeat, dict):
        _emit_heartbeat_failure(diagnostics, "response_not_object")
        return next_heartbeat_at, False
    if (
        heartbeat.get("approval_id") != approval_id
        or heartbeat.get("source_agent_id") != agent_id
        or heartbeat.get("source_invocation_id") != source_invocation_id
    ):
        _emit_heartbeat_failure(diagnostics, "approval_identity_mismatch")
        return next_heartbeat_at, False
    if heartbeat.get("live") is not True:
        _emit_heartbeat_failure(diagnostics, "connector_not_live")
        return next_heartbeat_at, False
    if not _heartbeat_timestamp_is_valid(heartbeat.get("heartbeat_at")):
        _emit_heartbeat_failure(diagnostics, "heartbeat_timestamp_invalid")
        return next_heartbeat_at, False
    if not _lease_expiry_is_current(heartbeat.get("expires_at")):
        _emit_heartbeat_failure(diagnostics, "lease_expired_or_invalid")
        return next_heartbeat_at, False
    if lease_state is not None:
        measured_deadline = _monotonic_expiry_deadline(
            heartbeat.get("heartbeat_at"),
            heartbeat.get("expires_at"),
            request_started_at=request_started_at,
            response_received_at=time.monotonic(),
        )
        if measured_deadline is None or measured_deadline <= time.monotonic():
            _emit_heartbeat_failure(diagnostics, "lease_timing_invalid")
            return next_heartbeat_at, False
        lease_state["deadline"] = measured_deadline
        lease_state["approval_deadline"] = (
            _monotonic_expiry_deadline(
                heartbeat.get("heartbeat_at"),
                approval_expires_at,
                request_started_at=request_started_at,
                response_received_at=time.monotonic(),
            )
            if isinstance(approval_expires_at, str)
            else None
        )
    return time.monotonic() + INVOCATION_HEARTBEAT_INTERVAL_SECONDS, True


def _approval_snapshot(approval: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_id": approval["project_id"],
        "work_order_id": approval.get("work_order_id"),
        "title": approval["title"],
        "summary": approval["summary"],
        "agent_name": approval["agent_name"],
        "target_branch": approval["target_branch"],
        "files": [
            {
                "path": item["path"],
                "change_type": item["change_type"],
                "diff": item["diff"],
                "new_content": item.get("new_content"),
            }
            for item in approval["files"]
        ],
        "test_plan": list(approval.get("test_plan", [])),
        "rollback_plan": approval.get("rollback_plan", ""),
        "source_provider": approval["source_provider"],
        "source_agent_id": approval.get("source_agent_id"),
        "source_agent_name": approval.get("source_agent_name"),
        "source_session_id": approval.get("source_session_id"),
        "source_invocation_id": approval.get("source_invocation_id"),
        "source_task_url": approval.get("source_task_url"),
    }


def _decision_snapshot(approval: dict[str, Any]) -> dict[str, Any]:
    """Bind decision polling to the server-derived context present at creation."""
    snapshot = _approval_snapshot(approval)
    # Keep historical v1 hash bytes stable; these server-derived fields are
    # compared separately so the decision cannot migrate to another workspace,
    # risk assessment, or hash contract while the invocation is waiting.
    snapshot["approved_payload_hash_version"] = approval.get(
        "approved_payload_hash_version", 1
    )
    snapshot["workspace_id"] = approval.get("workspace_id")
    snapshot["risk"] = deepcopy(approval.get("risk"))
    return snapshot


def _matches_invocation_payload(
    approval: dict[str, Any], *, body: dict[str, Any], agent_id: str
) -> bool:
    """Bind the server-created approval to the exact host call before polling it."""
    try:
        actual = _approval_snapshot(approval)
    except (KeyError, TypeError):
        return False
    expected = {
        "project_id": body["project_id"],
        "work_order_id": body.get("work_order_id"),
        "title": body["title"],
        "summary": body["summary"],
        "agent_name": body["agent_name"],
        "target_branch": body["target_branch"],
        "files": [
            {
                "path": item["path"],
                "change_type": item["change_type"],
                "diff": item["diff"],
                "new_content": item.get("new_content"),
            }
            for item in body["files"]
        ],
        "test_plan": list(body.get("test_plan", [])),
        "rollback_plan": body.get("rollback_plan", ""),
        "source_provider": "ANTIGRAVITY",
        "source_agent_id": agent_id,
        "source_session_id": body.get("source_session_id"),
        "source_invocation_id": body["source_invocation_id"],
        "source_task_url": body.get("source_task_url"),
    }
    return all(actual.get(field) == value for field, value in expected.items())


def _payload_hash(approval: dict[str, Any]) -> str:
    version = approval.get("approved_payload_hash_version", 1)
    if type(version) is not int:
        raise GateError("Invalid approval payload hash version")
    if version not in {1, 2}:
        raise GateError("Unsupported approval payload hash version")
    snapshot = _approval_snapshot(approval)
    if version == 2:
        snapshot["workspace_id"] = approval["workspace_id"]
        snapshot["risk"] = approval["risk"]
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return "sha256:" + digest if version == 1 else "sha256:v2:" + digest


def _matches_request_identity(
    approval: dict[str, Any], *, approval_id: str, project_id: str, agent_id: str
) -> bool:
    """Require every host response to identify the exact request being gated."""
    return (
        approval.get("id") == approval_id
        and approval.get("project_id") == project_id
        and approval.get("source_provider") == "ANTIGRAVITY"
        and approval.get("source_agent_id") == agent_id
    )


def _configuration(env: dict[str, str], payload: dict[str, Any]) -> dict[str, str]:
    config: dict[str, Any] = {}
    config_path = env.get("AIXION_AGENT_CONFIG", DEFAULT_CONFIG_PATH)
    has_config_file = os.path.lexists(config_path)
    if has_config_file:
        try:
            with host_security.open_private_file(
                Path(config_path), require_private=True, reject_shared_write=True
            ) as stream:
                raw_config = stream.read(MAX_CONFIG_BYTES + 1)
            if len(raw_config) > MAX_CONFIG_BYTES:
                raise GateError("Agent config exceeds its size limit")
            config = json.loads(
                raw_config,
                parse_float=_parse_finite_json_float,
                parse_constant=_reject_nonstandard_json_constant,
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        except host_security.HostSecurityError as exc:
            raise GateError(
                "Agent config is unreadable or invalid due to protected-file permissions or path policy"
            ) from exc
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise GateError("Agent config is unreadable or invalid") from exc
        if not isinstance(config, dict):
            raise GateError("Agent config must be a JSON object")
    elif not all(env.get(key) for key in (
        "AIXION_API_BASE_URL", "AIXION_AGENT_ID", "AIXION_PROJECT_ID"
    )):
        raise GateError("Agent config file or required non-secret environment values are missing")

    if has_config_file:
        # An installed, owner-protected config is the authority for routing and
        # Keychain identity. Inherited shell variables must not redirect the
        # credential-bearing request or select another agent/project.
        values = {
            "api_base_url": config.get("api_base_url", ""),
            "agent_id": config.get("agent_id", ""),
            "project_id": config.get("project_id", ""),
            "keychain_service": config.get(
                "keychain_service", "com.aixion.controltower.agent"
            ),
        }
    else:
        values = {
            "api_base_url": env.get("AIXION_API_BASE_URL", ""),
            "agent_id": env.get("AIXION_AGENT_ID", ""),
            "project_id": env.get("AIXION_PROJECT_ID", ""),
            "keychain_service": env.get(
                "AIXION_AGENT_TOKEN_KEYCHAIN_SERVICE",
                "com.aixion.controltower.agent",
            ),
        }
    return {key: str(value).strip() for key, value in values.items()}


def _validated_api_base(value: str) -> str:
    api_base = value.strip().rstrip("/")
    try:
        parsed = urlsplit(api_base)
        hostname = parsed.hostname
        parsed.port  # Force urllib's port validation before making a request.
    except ValueError as exc:
        raise GateError("Control Tower API URL is malformed") from exc
    local_hosts = {"127.0.0.1", "localhost", "::1"}
    local_http = parsed.scheme == "http" and hostname in local_hosts
    secure_remote = parsed.scheme == "https" and bool(hostname)
    if (
        not hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or "?" in api_base
        or "#" in api_base
        or not (local_http or secure_remote)
    ):
        raise GateError("Remote API endpoints require HTTPS and a URL without credentials, query, or fragment")
    return api_base


def check_connection(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Validate local setup, API health, and authenticated connector scope."""
    env = os.environ if environ is None else environ
    config = _configuration(env, {})
    api_base = _validated_api_base(config["api_base_url"])
    if not all((config["agent_id"], config["project_id"])):
        raise GateError("Agent ID and project ID must be configured")
    token = _agent_token(env, config)
    try:
        response = deadline_request(
            "GET",
            f"{api_base}/health",
            {"Accept": "application/json"},
            None,
            5,
            max_response_bytes=MAX_HEALTH_RESPONSE_BYTES,
        )
        if not 200 <= response.status < 300:
            raise GateError(f"Control Tower health check returned HTTP {response.status}")
        health = json.loads(
            response.body,
            parse_float=_parse_finite_json_float,
            parse_constant=_reject_nonstandard_json_constant,
            object_pairs_hook=_reject_duplicate_json_keys,
        )
        if not isinstance(health, dict) or health.get("status") != "ok":
            raise GateError("Control Tower health check did not return status ok")
    except (TimeoutError, OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise GateError("Control Tower health check failed") from exc
    identity_query = urlencode({"provider": "ANTIGRAVITY", "project_id": config["project_id"]})
    identity = _request(
        "GET",
        f"{api_base}/agents/identity?{identity_query}",
        config["agent_id"],
        token,
    )
    if (
        identity.get("agent_id") != config["agent_id"]
        or identity.get("provider") != "ANTIGRAVITY"
        or identity.get("project_id") != config["project_id"]
        or (
            identity.get("workspace_id") is not None
            and (
                not isinstance(identity.get("workspace_id"), str)
                or not identity["workspace_id"]
            )
        )
    ):
        raise GateError("Authenticated agent identity, provider, or project scope does not match")
    return {"api_base_url": api_base, "agent_id": config["agent_id"], "project_id": config["project_id"]}


def _agent_token(env: dict[str, str], config: dict[str, str]) -> str:
    token = env.get("AIXION_AGENT_TOKEN", "").strip()
    if token:
        if env.get("AIXION_ALLOW_AGENT_TOKEN_ENV") != "true":
            raise GateError(
                "Environment agent tokens are disabled; use macOS Keychain or a protected token file"
            )
        return token
    token_path = env.get("AIXION_AGENT_TOKEN_FILE", "").strip()
    if token_path:
        try:
            with host_security.open_private_file(
                Path(token_path), require_private=True, reject_shared_write=True
            ) as stream:
                token_bytes = stream.read(8193)
        except host_security.HostSecurityError as exc:
            raise GateError(
                "Agent token file is unavailable because protected-file permissions or path policy failed"
            ) from exc
        except OSError as exc:
            raise GateError("Agent token file is unavailable") from exc
        if len(token_bytes) > 8192:
            raise GateError("Agent token file exceeds its size limit")
        token = token_bytes.decode("utf-8").strip()
        if not token:
            raise GateError("Agent token file is empty")
        return token
    if sys.platform != "darwin":
        raise GateError("Agent token is missing; configure it through a protected secret source")
    try:
        result = subprocess.run(
            [
                "/usr/bin/security", "find-generic-password", "-s",
                config["keychain_service"], "-a", config["agent_id"], "-w",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GateError("Agent token was not available from macOS Keychain") from exc
    token = result.stdout.strip()
    if not token:
        raise GateError("Agent token was empty in macOS Keychain")
    return token


def _run(
    payload: dict[str, Any],
    environ: dict[str, str] | None,
    diagnostics: dict[str, str | None],
) -> dict[str, str]:
    env = os.environ if environ is None else environ
    call = payload.get("toolCall")
    if not isinstance(call, dict) or not isinstance(call.get("name"), str):
        raise GateError("Missing toolCall.name")
    tool_name = call["name"]
    args = call.get("args", {})
    if not isinstance(args, (dict, list, str, int, float, bool, type(None))):
        raise GateError("Tool arguments are not JSON-compatible")
    if _has_sensitive_key(args) or _has_sensitive_value(args):
        return _decision("deny", "Tool arguments contain a secret-like field; redact it before requesting approval")
    if (
        tool_name in READ_ONLY_TOOLS
        and _has_documented_read_path(tool_name, args)
        and not _has_sensitive_path(args)
    ):
        return _decision("allow", "Read-only tool left to Antigravity policy")

    try:
        approval_timeout_seconds = int(env.get("AIXION_APPROVAL_TIMEOUT_SECONDS", "600"))
    except (TypeError, ValueError):
        return _decision("deny", "Approval timeout configuration is invalid")
    if not 1 <= approval_timeout_seconds <= MAX_POLL_SECONDS:
        return _decision("deny", "Approval timeout must be between 1 and 900 seconds")

    # A session identifier is part of the idempotency key. Never coerce or
    # truncate it: distinct provider sessions must not collapse to one identity.
    if "conversationId" in payload:
        session_id = payload["conversationId"]
    else:
        session_id = payload.get("sessionId")
    step_index = payload.get("stepIdx")
    if (
        not isinstance(session_id, str)
        or not session_id.strip()
        or len(session_id) > 200
        or isinstance(step_index, bool)
        or not isinstance(step_index, int)
        or step_index < 0
    ):
        return _decision("deny", "Antigravity request is missing a valid session or step identity")

    config = _configuration(env, payload)
    api_base = _validated_api_base(config["api_base_url"])
    agent_id = config["agent_id"]
    project_id = config["project_id"]
    if not all((api_base, agent_id, project_id)):
        raise GateError("Set API base URL, agent credentials, and project")
    token = _agent_token(env, config)

    arg_bytes = json.dumps(args, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(arg_bytes) > MAX_ARGUMENTS_BYTES:
        return _decision("deny", "Tool arguments exceed the approval display limit")
    invocation_material = json.dumps(
        [session_id, step_index, tool_name, args], sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    invocation_id = "antigravity:v1:" + hashlib.sha256(invocation_material).hexdigest()
    diagnostics["correlation_id"] = _short_correlation_id(invocation_id)
    approval_body = {
        "project_id": project_id,
        "title": f"Antigravity approval: {tool_name}",
        "summary": "Review the exact Antigravity tool and arguments before this invocation continues.",
        "agent_name": "Antigravity",
        "target_branch": "current-workspace",
        "files": [{
            "path": f"antigravity://tool/{tool_name[:120]}",
            "change_type": "tool_call",
            "diff": arg_bytes.decode("utf-8"),
        }],
        "test_plan": ["Confirm the tool and arguments match the intended task"],
        "rollback_plan": "Deny this request if uncertain. Approval authorizes only this displayed invocation.",
        "source_session_id": session_id,
        "source_invocation_id": invocation_id,
    }
    try:
        created = _create_approval_with_retry(
            api_base=api_base,
            agent_id=agent_id,
            token=token,
            body=approval_body,
            diagnostics=diagnostics,
        )
    except GateError:
        return _decision("deny", "Control Tower rejected the approval request")
    if created is None:
        return _decision("deny", "Control Tower approval creation remained ambiguous")
    approval_id = created.get("id")
    if (
        not isinstance(approval_id, str)
        # This opaque ID is interpolated into every subsequent URL path. Match
        # the backend's canonical identifier grammar before any follow-up call;
        # a merely non-empty value could inject path segments or query syntax.
        or DIAGNOSTIC_APPROVAL_ID.fullmatch(approval_id) is None
        or created.get("status") not in {"PENDING_REVIEW", "REQUESTED", "APPROVED"}
        or not _matches_request_identity(
            created, approval_id=approval_id, project_id=project_id, agent_id=agent_id
        )
    ):
        return _decision("deny", "Control Tower did not create an actionable approval")
    if not _matches_invocation_payload(created, body=approval_body, agent_id=agent_id):
        return _decision("deny", "Control Tower approval does not match the original tool invocation")
    diagnostics["approval_id"] = _validated_diagnostic_approval_id(approval_id)
    try:
        created_snapshot = _decision_snapshot(created)
    except (KeyError, TypeError):
        return _decision("deny", "Control Tower approval payload is incomplete")

    wait_started_at = time.monotonic()
    deadline = wait_started_at + approval_timeout_seconds
    next_heartbeat_at = wait_started_at + INVOCATION_HEARTBEAT_INTERVAL_SECONDS
    lease_state: dict[str, float | None] = {"deadline": None}
    reconnect_backoff_seconds = POLL_INTERVAL_SECONDS
    while True:
        now = time.monotonic()
        remaining_seconds = deadline - now
        if remaining_seconds <= 0:
            break
        next_heartbeat_at, lease_live = _refresh_invocation_lease_if_due(
            api_base=api_base,
            approval_id=approval_id,
            agent_id=agent_id,
            token=token,
            source_invocation_id=invocation_id,
            deadline=deadline,
            next_heartbeat_at=next_heartbeat_at,
            approval_expires_at=created.get("expires_at"),
            lease_state=lease_state,
            diagnostics=diagnostics,
            now=now,
        )
        if not lease_live:
            return _decision("deny", "Connector liveness could not be confirmed")
        # A heartbeat can consume part of the remaining wait. Recompute the
        # request budget afterward so the decision read cannot use a stale
        # pre-heartbeat timeout and extend the host deadline.
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            break
        try:
            current = _request(
                "GET",
                f"{api_base}/agents/approvals/{approval_id}/decision",
                agent_id,
                token,
                timeout_seconds=min(REQUEST_TIMEOUT_SECONDS, remaining_seconds),
            )
        except RateLimitError as exc:
            now = time.monotonic()
            remaining_seconds = deadline - now
            if remaining_seconds <= 0:
                break
            # Do not let a server-directed backoff outlive the connector
            # invocation lease. Wake at the next heartbeat so liveness is
            # renewed before retrying the decision read.
            until_heartbeat_seconds = max(0.0, next_heartbeat_at - now)
            time.sleep(min(exc.retry_after_seconds, remaining_seconds, until_heartbeat_seconds))
            continue
        except AmbiguousRequestError:
            # Decision reads are idempotent. Retry them with a bounded delay,
            # but never retry approval creation or consumption after ambiguity.
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                break
            time.sleep(min(reconnect_backoff_seconds, remaining_seconds))
            reconnect_backoff_seconds = min(
                reconnect_backoff_seconds * 2, MAX_RECONNECT_BACKOFF_SECONDS
            )
            continue
        reconnect_backoff_seconds = POLL_INTERVAL_SECONDS
        received_at = time.monotonic()
        if not _matches_request_identity(
            current, approval_id=approval_id, project_id=project_id, agent_id=agent_id
        ):
            return _decision("deny", "Control Tower returned a different approval request")
        try:
            current_snapshot = _decision_snapshot(current)
            # The backend upgrades the approved hash from legacy v1 to v2
            # exactly once when the reviewer decides. Permit that transition
            # only when every other snapshot field is unchanged; v2 then binds
            # the workspace and risk while the action hash is verified below.
            approved_v1_to_v2 = (
                created_snapshot.get("approved_payload_hash_version") == 1
                and current.get("status") == "APPROVED"
                and current_snapshot.get("approved_payload_hash_version") == 2
            )
            if approved_v1_to_v2:
                current_snapshot["approved_payload_hash_version"] = 1
            if current_snapshot != created_snapshot:
                return _decision("deny", "Approval payload changed after the original tool invocation")
        except (KeyError, TypeError):
            return _decision("deny", "Control Tower approval payload is incomplete")
        status = current.get("status")
        if received_at >= deadline:
            if status == "APPROVED":
                return _decision("deny", "Approval arrived after the host wait deadline; the tool call was denied")
            if status in {"PENDING_REVIEW", "REQUESTED"}:
                break
        if status in {"DENIED", "REJECTED", "REVISION_REQUESTED", "EXPIRED", "CANCELLED", "BLOCKED", "FAILED"}:
            return _decision("deny", f"Control Tower decision: {status}")
        if status in {"APPROVED", "PENDING_REVIEW", "REQUESTED"}:
            next_heartbeat_at, lease_live = _refresh_invocation_lease_if_due(
                api_base=api_base,
                approval_id=approval_id,
                agent_id=agent_id,
                token=token,
                source_invocation_id=invocation_id,
                deadline=deadline,
                next_heartbeat_at=next_heartbeat_at,
                approval_expires_at=created.get("expires_at"),
                lease_state=lease_state,
                diagnostics=diagnostics,
            )
            if not lease_live:
                return _decision("deny", "Connector liveness could not be confirmed")
        if status == "APPROVED":
            if current.get("approved_payload_hash") != _payload_hash(current):
                return _decision("deny", "Approved payload integrity check failed")
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                return _decision("deny", "Approval arrived too late to claim before the host wait deadline")
            try:
                claimed = _request(
                    "POST",
                    f"{api_base}/agents/approvals/{approval_id}/consume",
                    agent_id,
                    token,
                    timeout_seconds=min(REQUEST_TIMEOUT_SECONDS, remaining_seconds),
                )
            except AmbiguousRequestError:
                cleanup_remaining_seconds = (
                    deadline + EXPIRY_CLEANUP_GRACE_SECONDS - time.monotonic()
                )
                if cleanup_remaining_seconds > 0:
                    _report_delivery_failure(
                        api_base=api_base,
                        approval_id=approval_id,
                        agent_id=agent_id,
                        token=token,
                        source_invocation_id=str(current.get("source_invocation_id") or ""),
                        approved_payload_hash=str(current.get("approved_payload_hash") or ""),
                        timeout_seconds=min(REQUEST_TIMEOUT_SECONDS, cleanup_remaining_seconds),
                    )
                return _decision("deny", "Approval delivery outcome was ambiguous; tool denied")
            except GateError:
                return _decision("deny", "Approval delivery could not be claimed safely")
            if time.monotonic() >= deadline:
                return _decision("deny", "Approval claim response arrived after the host wait deadline")
            if claimed.get("consumed") is not True:
                return _decision("deny", "Approval delivery was already claimed")
            return _decision("auto_approve", "Approved from Aixion Control Tower")
        if status not in {"PENDING_REVIEW", "REQUESTED"}:
            return _decision("deny", f"Unrecognized approval state: {status}")
        # Keep polling cadence inside the configured host wait. An unbounded
        # final interval could otherwise extend a short timeout by up to one
        # full polling period before the expiry transition is attempted.
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds > 0:
            time.sleep(min(POLL_INTERVAL_SECONDS, remaining_seconds))
    # The server also assigns an expiry when the approval is created. Try to
    # persist the host timeout, but permit at most one second of cleanup grace
    # after the configured decision-wait deadline. This path always denies.
    cleanup_remaining_seconds = deadline + EXPIRY_CLEANUP_GRACE_SECONDS - time.monotonic()
    if cleanup_remaining_seconds <= 0:
        return _decision("deny", "Approval timed out; expiration could not be confirmed")
    try:
        expired = _request(
            "POST",
            f"{api_base}/agents/approvals/{approval_id}/expire",
            agent_id,
            token,
            timeout_seconds=min(REQUEST_TIMEOUT_SECONDS, cleanup_remaining_seconds),
        )
    except GateError:
        return _decision("deny", "Approval timed out; expiration could not be confirmed")
    if time.monotonic() > deadline + EXPIRY_CLEANUP_GRACE_SECONDS:
        return _decision("deny", "Approval timed out; expiry response arrived after cleanup grace")
    if not _matches_request_identity(
        expired, approval_id=approval_id, project_id=project_id, agent_id=agent_id
    ):
        return _decision("deny", "Control Tower returned a different approval request")
    if expired.get("status") == "APPROVED":
        return _decision("deny", "Approval raced with the host timeout; the tool call was denied")
    if expired.get("status") != "EXPIRED":
        return _decision("deny", "Approval timed out; the tool call was denied")
    return _decision("deny", "Approval timed out; the request expired and the tool call was denied")


def run(payload: dict[str, Any], environ: dict[str, str] | None = None) -> dict[str, str]:
    diagnostics = _new_diagnostic_context()
    try:
        result = _run(payload, environ, diagnostics)
    except Exception:
        _emit_diagnostic(
            context=diagnostics,
            event="terminal_decision",
            decision="deny",
        )
        raise
    _emit_diagnostic(
        context=diagnostics,
        event="terminal_decision",
        decision=result["decision"],
    )
    return result


def _report_delivery_failure(
    *,
    api_base: str,
    approval_id: str,
    agent_id: str,
    token: str,
    source_invocation_id: str,
    approved_payload_hash: str,
    timeout_seconds: float,
) -> None:
    """Best-effort audit only; failure to report can never turn deny into allow."""
    try:
        _request(
            "POST",
            f"{api_base}/agents/approvals/{approval_id}/delivery-failure",
            agent_id,
            token,
            {
                "source_invocation_id": source_invocation_id,
                "approved_payload_hash": approved_payload_hash,
                "reason_code": "CLAIM_RESPONSE_AMBIGUOUS",
            },
            timeout_seconds=timeout_seconds,
        )
    except GateError:
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Antigravity approval gate")
    parser.add_argument("--config", help="Path to the protected non-secret agent configuration")
    parser.add_argument("--check", action="store_true", help="Check configuration and API health")
    args = parser.parse_args(argv)
    if args.config:
        os.environ["AIXION_AGENT_CONFIG"] = args.config
    if args.check:
        try:
            details = check_connection()
        except Exception as exc:
            sys.stderr.write(f"Aixion hook preflight failed: {exc.__class__.__name__}\n")
            return 1
        sys.stdout.write(
            "Aixion hook preflight passed: "
            f"{details['agent_id']} / {details['project_id']} at {details['api_base_url']}\n"
        )
        return 0
    try:
        payload = _read_input()
    except Exception as exc:  # fail closed and never print arguments or credentials
        _emit_diagnostic(
            context=_new_diagnostic_context(),
            event="terminal_decision",
            decision="deny",
        )
        _emit(_decision("deny", f"Aixion approval gate failed: {exc.__class__.__name__}"))
        return 0
    try:
        result = run(payload)
    except Exception as exc:  # fail closed and never print arguments or credentials
        _emit(_decision("deny", f"Aixion approval gate failed: {exc.__class__.__name__}"))
        return 0
    _emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
