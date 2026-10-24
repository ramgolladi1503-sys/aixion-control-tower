#!/usr/bin/env python3
"""Route a Codex PermissionRequest through the Aixion phone approval inbox."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import time
import uuid
from typing import Any
from urllib.parse import urlencode, urlsplit

# The hook is launched as a standalone script by Codex, where sys.path starts
# at integrations/codex rather than at the repository root.
_REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPOSITORY_ROOT not in sys.path:
    sys.path.insert(0, _REPOSITORY_ROOT)

from integrations.aixion_deadline_http import request as deadline_request  # noqa: E402

MAX_INPUT_BYTES = 128 * 1024
MAX_ARGUMENTS_BYTES = 48 * 1024
MAX_CONFIG_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
MAX_WAIT_SECONDS = 15 * 60
MAX_REQUEST_TIMEOUT_SECONDS = 10.0
EXPIRY_CLEANUP_TIMEOUT_SECONDS = 1.0
POLL_INTERVAL_SECONDS = 2
INVOCATION_HEARTBEAT_INTERVAL_SECONDS = 15
INVOCATION_HEARTBEAT_RETRY_DELAY_SECONDS = 0.1
MAX_RECONNECT_BACKOFF_SECONDS = 10
MAX_CREATE_ATTEMPTS = 2
CREATE_RETRY_DELAY_SECONDS = 0.25
APPROVAL_PROTOCOL_VERSION = "1"
DIAGNOSTIC_APPROVAL_ID = re.compile(r"^approval_[a-f0-9]{12}$")
DEFAULT_CONFIG_PATH = os.path.expanduser("~/.config/aixion/codex.json")
SENSITIVE_KEY = re.compile(r"(token|secret|password|credential|private.?key|api.?key)", re.I)
SENSITIVE_VALUE = re.compile(
    r"(?:bearer\s+[A-Za-z0-9._~-]{8,}|(?:api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*[\"']?[A-Za-z0-9._~/-]{8,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)",
    re.I,
)


class GateError(Exception):
    """A safe, non-sensitive failure reason for the approval gate."""


class AmbiguousRequestError(GateError):
    """A request may have committed even though its response was not usable."""


class RateLimitError(GateError):
    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = max(1, min(MAX_WAIT_SECONDS, retry_after_seconds))
        super().__init__("Control Tower rate limit exceeded")


def _new_diagnostic_context() -> dict[str, str | None]:
    return {"correlation_id": None, "approval_id": None}


def _emit_diagnostic(
    *,
    context: dict[str, str | None],
    event: str,
    attempt: int | None = None,
    decision: str | None = None,
) -> None:
    """Write fixed-field, content-free diagnostics without affecting the gate."""
    record = {
        "component": "aixion_approval_hook",
        "provider": "CODEX",
        "event": event,
        "correlation_id": context.get("correlation_id"),
        "approval_id": context.get("approval_id"),
        "attempt": attempt,
        "decision": decision,
    }
    try:
        sys.stderr.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError:
        # Diagnostic output must never change an authorization result.
        return


def _short_correlation_id(source_invocation_id: str) -> str:
    return hashlib.sha256(source_invocation_id.encode("utf-8")).hexdigest()[:16]


def _validated_diagnostic_approval_id(value: Any) -> str | None:
    if isinstance(value, str) and DIAGNOSTIC_APPROVAL_ID.fullmatch(value):
        return value
    return None


def _create_approval_with_retry(
    *,
    api_base: str,
    config: dict[str, str],
    token: str,
    body: dict[str, Any],
    diagnostics: dict[str, str | None] | None = None,
) -> dict[str, Any] | None:
    """Retry one ambiguous create with unchanged callback identity and payload.

    Each HTTP attempt has a 10-second transport timeout. Two attempts and one
    short delay bound this recovery path to at most 20.25 seconds.
    """
    for attempt in range(MAX_CREATE_ATTEMPTS):
        try:
            return _request(
                "POST", f"{api_base}/agents/approvals", config, token, body
            )
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


def _emit_decision(behavior: str, message: str) -> None:
    result: dict[str, Any] = {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": behavior},
        }
    }
    if behavior == "deny":
        result["hookSpecificOutput"]["decision"]["message"] = message[:500]
    sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _read_input() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise GateError("Codex hook input exceeds its size limit")
    try:
        value = json.loads(
            raw,
            parse_constant=_reject_nonstandard_json_constant,
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise GateError("Codex hook input is invalid JSON") from exc
    if not isinstance(value, dict) or value.get("hook_event_name") != "PermissionRequest":
        raise GateError("Expected a Codex PermissionRequest event")
    return value


def _contains_sensitive(value: Any) -> bool:
    if isinstance(value, dict):
        return any(SENSITIVE_KEY.search(str(key)) or _contains_sensitive(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_sensitive(item) for item in value)
    return isinstance(value, str) and SENSITIVE_VALUE.search(value) is not None


def _validated_api_base(value: str) -> str:
    api_base = value.strip().rstrip("/")
    try:
        parsed = urlsplit(api_base)
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise GateError("Control Tower API URL is malformed") from exc
    local_http = parsed.scheme == "http" and hostname in {"127.0.0.1", "localhost", "::1"}
    remote_https = parsed.scheme == "https" and bool(hostname)
    if (not hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or "?" in api_base or "#" in api_base or not (local_http or remote_https)):
        raise GateError("Remote Control Tower API URLs must use HTTPS")
    return api_base


def _configuration(env: dict[str, str]) -> dict[str, str]:
    config_path = env.get("AIXION_CODEX_CONFIG", DEFAULT_CONFIG_PATH)
    config: Any = {}
    if os.path.lexists(config_path):
        try:
            descriptor = os.open(config_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream:
                metadata = os.fstat(stream.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                    raise GateError("Codex config must be a regular file owned by this user")
                if stat.S_IMODE(metadata.st_mode) & 0o077:
                    raise GateError("Codex config permissions must exclude group and other users")
                raw = stream.read(MAX_CONFIG_BYTES + 1)
            if len(raw) > MAX_CONFIG_BYTES:
                raise GateError("Codex config exceeds its size limit")
            # Config selects the API, agent, project, and credential source. Do
            # not silently accept duplicate keys or JavaScript-only constants:
            # the hook applies the same strict JSON rules to provider input and
            # API responses, so configuration must have one unambiguous value
            # for every field as well.
            config = json.loads(
                raw,
                parse_constant=_reject_nonstandard_json_constant,
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise GateError("Codex config is unreadable or invalid") from exc
    elif not all(env.get(name) for name in (
        "AIXION_API_BASE_URL", "AIXION_AGENT_ID", "AIXION_PROJECT_ID"
    )):
        raise GateError("Codex config file or required environment values are missing")
    if not isinstance(config, dict):
        raise GateError("Codex config must be a JSON object")
    values = {
        "api_base_url": env.get("AIXION_API_BASE_URL") or config.get("api_base_url", ""),
        "agent_id": env.get("AIXION_AGENT_ID") or config.get("agent_id", ""),
        "project_id": env.get("AIXION_PROJECT_ID") or config.get("project_id", ""),
        "keychain_service": env.get("AIXION_AGENT_TOKEN_KEYCHAIN_SERVICE")
        or config.get("keychain_service", "com.aixion.controltower.agent"),
    }
    if any(not isinstance(value, str) or not value.strip() for value in values.values()):
        raise GateError("Codex config is missing required values")
    values = {key: value.strip() for key, value in values.items()}
    values["api_base_url"] = _validated_api_base(values["api_base_url"])
    return values


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
            descriptor = os.open(token_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream:
                metadata = os.fstat(stream.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                    raise GateError("Agent token file must be a regular file owned by this user")
                if stat.S_IMODE(metadata.st_mode) & 0o077:
                    raise GateError("Agent token file permissions must exclude group and other users")
                raw = stream.read(8193)
        except OSError as exc:
            raise GateError("Agent token file is unavailable") from exc
        if len(raw) > 8192 or not raw.strip():
            raise GateError("Agent token file is empty or too large")
        return raw.decode("utf-8").strip()
    if sys.platform != "darwin":
        raise GateError("Agent token is missing; configure a protected secret source")
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", config["keychain_service"],
             "-a", config["agent_id"], "-w"],
            check=True, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GateError("Agent token was not available from macOS Keychain") from exc
    token = result.stdout.strip()
    if not token:
        raise GateError("Agent token is empty in macOS Keychain")
    return token


def _request(method: str, url: str, config: dict[str, str], token: str,
             body: dict[str, Any] | None = None,
             timeout_seconds: float = MAX_REQUEST_TIMEOUT_SECONDS,
             expected_status: int | None = None) -> dict[str, Any]:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise GateError("Control Tower request timeout is invalid")
    request_timeout = min(float(timeout_seconds), MAX_REQUEST_TIMEOUT_SECONDS)
    try:
        encoded = None if body is None else json.dumps(
            body, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise GateError("Control Tower request contains invalid JSON data") from exc
    headers = {
        "Accept": "application/json", "Content-Type": "application/json",
        "X-Aixion-Agent-Id": config["agent_id"], "X-Aixion-Agent-Token": token,
        "X-Aixion-Approval-Protocol": APPROVAL_PROTOCOL_VERSION,
    }
    try:
        response = deadline_request(
            method, url, headers, encoded, request_timeout,
            max_response_bytes=MAX_RESPONSE_BYTES,
        )
        if response.status == 429:
            retry_after = next(
                (value for key, value in response.headers.items() if key.lower() == "retry-after"),
                None,
            )
            delay = _parse_retry_after(retry_after)
            raise RateLimitError(delay)
        if response.status >= 500:
            raise AmbiguousRequestError("Control Tower returned an ambiguous server failure")
        if expected_status is not None and response.status != expected_status:
            raise GateError(f"Control Tower returned HTTP {response.status}")
        if not 200 <= response.status < 300:
            raise GateError(f"Control Tower returned HTTP {response.status}")
        raw = response.body
        if len(raw) > MAX_RESPONSE_BYTES:
            raise AmbiguousRequestError("Control Tower response exceeds its size limit")
        try:
            result = json.loads(
                raw,
                parse_float=_parse_finite_json_float,
                parse_constant=_reject_nonstandard_json_constant,
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise AmbiguousRequestError("Control Tower returned invalid response data") from exc
        if not isinstance(result, dict):
            raise AmbiguousRequestError("Control Tower returned an invalid response")
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
            return min(MAX_WAIT_SECONDS, int(value))
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
        return min(MAX_WAIT_SECONDS, delay)
    except (OverflowError, TypeError, ValueError):
        return POLL_INTERVAL_SECONDS


def _refresh_invocation_lease_if_due(
    *,
    api_base: str,
    approval_id: str,
    config: dict[str, str],
    token: str,
    source_invocation_id: str,
    deadline: float,
    next_heartbeat_at: float,
    diagnostics: dict[str, str | None] | None = None,
) -> tuple[float, bool]:
    """Renew the invocation lease when due, without extending the hook deadline."""
    now = time.monotonic()
    if now < next_heartbeat_at:
        return next_heartbeat_at, True
    remaining_seconds = deadline - now
    if remaining_seconds <= 0:
        return next_heartbeat_at, False
    heartbeat: Any
    for attempt in range(2):
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            return next_heartbeat_at, False
        try:
            heartbeat = _request(
                "POST",
                f"{api_base}/agents/approvals/{approval_id}/heartbeat",
                config,
                token,
                {"source_invocation_id": source_invocation_id},
                timeout_seconds=min(MAX_REQUEST_TIMEOUT_SECONDS, remaining_seconds),
                expected_status=200,
            )
            break
        except GateError as exc:
            # Only retry a transport failure whose original exception is
            # preserved. HTTP errors and malformed/ambiguous responses remain
            # fail-closed; the retry repeats the same idempotent invocation ID.
            transport_failure = isinstance(exc, AmbiguousRequestError) and isinstance(
                exc.__cause__, (TimeoutError, OSError)
            )
            if attempt != 0 or not transport_failure:
                return next_heartbeat_at, False
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= INVOCATION_HEARTBEAT_RETRY_DELAY_SECONDS:
                return next_heartbeat_at, False
            if diagnostics is not None:
                _emit_diagnostic(
                    context=diagnostics,
                    event="invocation_heartbeat_retry",
                    attempt=attempt + 2,
                )
            time.sleep(INVOCATION_HEARTBEAT_RETRY_DELAY_SECONDS)
    if time.monotonic() >= deadline:
        return next_heartbeat_at, False
    if not isinstance(heartbeat, dict):
        return next_heartbeat_at, False
    if (
        heartbeat.get("approval_id") != approval_id
        or heartbeat.get("source_agent_id") != config["agent_id"]
        or heartbeat.get("source_invocation_id") != source_invocation_id
        or heartbeat.get("live") is not True
        or not isinstance(heartbeat.get("heartbeat_at"), str)
        or not heartbeat["heartbeat_at"]
        or not isinstance(heartbeat.get("expires_at"), str)
        or not heartbeat["expires_at"]
    ):
        return next_heartbeat_at, False
    return time.monotonic() + INVOCATION_HEARTBEAT_INTERVAL_SECONDS, True


def _snapshot(approval: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_id": approval["project_id"],
        "work_order_id": approval.get("work_order_id"),
        "title": approval["title"],
        "summary": approval["summary"],
        "agent_name": approval["agent_name"],
        "target_branch": approval["target_branch"],
        "files": [{
            "path": item["path"], "change_type": item["change_type"],
            "diff": item["diff"], "new_content": item.get("new_content"),
        } for item in approval["files"]],
        "test_plan": list(approval.get("test_plan", [])),
        "rollback_plan": approval.get("rollback_plan", ""),
        "source_provider": approval["source_provider"],
        "source_agent_id": approval.get("source_agent_id"),
        "source_agent_name": approval.get("source_agent_name"),
        "source_session_id": approval.get("source_session_id"),
        "source_invocation_id": approval.get("source_invocation_id"),
        "source_task_url": approval.get("source_task_url"),
    }


def _matches(approval: dict[str, Any], *, body: dict[str, Any], config: dict[str, str]) -> bool:
    try:
        expected = dict(body)
        expected.update({
            "source_provider": "CODEX", "source_agent_id": config["agent_id"],
            "source_agent_name": approval["source_agent_name"],
            "source_task_url": None,
        })
        return _snapshot(approval) == {
            "project_id": expected["project_id"],
            "work_order_id": expected.get("work_order_id"),
            "title": expected["title"], "summary": expected["summary"],
            "agent_name": expected["agent_name"], "target_branch": expected["target_branch"],
            "files": expected["files"], "test_plan": expected["test_plan"],
            "rollback_plan": expected["rollback_plan"], "source_provider": "CODEX",
            "source_agent_id": config["agent_id"],
            "source_agent_name": approval["source_agent_name"],
            "source_session_id": body["source_session_id"],
            "source_invocation_id": body["source_invocation_id"],
            "source_task_url": None,
        }
    except (KeyError, TypeError):
        return False


def _hash(approval: dict[str, Any]) -> str:
    version = approval.get("approved_payload_hash_version", 1)
    if type(version) is not int:
        raise GateError("Invalid approval payload hash version")
    if version not in {1, 2}:
        raise GateError("Unsupported approval payload hash version")
    snapshot = _snapshot(approval)
    if version == 2:
        snapshot["workspace_id"] = approval["workspace_id"]
        snapshot["risk"] = approval["risk"]
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return "sha256:" + digest if version == 1 else "sha256:v2:" + digest


def _identity_matches(approval: dict[str, Any], *, approval_id: str,
                      config: dict[str, str]) -> bool:
    return (approval.get("id") == approval_id
            and approval.get("project_id") == config["project_id"]
            and approval.get("source_provider") == "CODEX"
            and approval.get("source_agent_id") == config["agent_id"])


def _invocation_identity(
    event: dict[str, Any], *, tool_name: str, tool_input: Any
) -> tuple[str, str] | None:
    """Bind identity to Codex's call ID or this synchronous hook callback."""
    session = event.get("session_id")
    turn = event.get("turn_id")
    if not isinstance(session, str) or not session.strip():
        return None
    if not isinstance(turn, str) or not turn.strip():
        return None
    if len(session) > 200 or len(turn) > 200 or len(tool_name) > 256:
        return None
    if "tool_use_id" in event:
        tool_use = event["tool_use_id"]
        if not isinstance(tool_use, str) or not tool_use.strip() or len(tool_use) > 200:
            return None
        identity_source = "provider"
    else:
        # PermissionRequest omits tool_use_id in the documented schema. The hook
        # runs synchronously for one pending native request, so a per-process
        # nonce binds this approval to that callback. A restarted callback gets
        # a new ID and cannot reuse an earlier phone decision.
        tool_use = "callback:" + uuid.uuid4().hex
        identity_source = "callback"
    session_material = json.dumps(
        {"session_id": session},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    normalized = {
        "session_id": session[:200],
        "turn_id": turn[:200],
        "identity_source": identity_source,
        "tool_use_id": tool_use[:200],
        "tool_name": tool_name,
        "tool_input": tool_input,
    }
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    source_session_id = "codex:v1:session:" + hashlib.sha256(session_material).hexdigest()
    source_invocation_id = "codex:v1:" + hashlib.sha256(encoded).hexdigest()
    return source_session_id, source_invocation_id


def _run(
    event: dict[str, Any],
    environ: dict[str, str] | None,
    diagnostics: dict[str, str | None],
) -> tuple[str, str]:
    env = os.environ if environ is None else environ
    if event.get("hook_event_name") != "PermissionRequest":
        raise GateError("Only PermissionRequest events are supported")
    tool_name = event.get("tool_name")
    args = event.get("tool_input")
    if not isinstance(tool_name, str) or not tool_name.strip() or not isinstance(args, (dict, list, str, int, float, bool, type(None))):
        raise GateError("Codex permission request is missing tool identity or JSON arguments")
    if _contains_sensitive(args):
        return "deny", "Tool arguments contain secret-like material; redact it before approval"
    try:
        arg_bytes = json.dumps(
            args, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise GateError("Codex permission request arguments are not valid JSON") from exc
    if len(arg_bytes) > MAX_ARGUMENTS_BYTES:
        return "deny", "Tool arguments exceed the approval display limit"

    # Validate the local wait budget before creating a server-side pending
    # request. Otherwise a bad setting denies the Codex callback but leaves a
    # phone approval that can never resume that callback.
    try:
        timeout = int(env.get("AIXION_CODEX_APPROVAL_TIMEOUT_SECONDS", "600"))
    except ValueError:
        return "deny", "Codex approval timeout configuration is invalid"
    if not 1 <= timeout <= MAX_WAIT_SECONDS:
        return "deny", "Codex approval timeout is outside the allowed range"

    invocation_identity = _invocation_identity(event, tool_name=tool_name, tool_input=args)
    if invocation_identity is None:
        return "deny", "Codex permission request is missing stable session, turn, or tool-call identity"
    source_session_id, source_invocation_id = invocation_identity
    diagnostics["correlation_id"] = _short_correlation_id(source_invocation_id)

    config = _configuration(env)
    token = _agent_token(env, config)
    safe_tool = re.sub(r"[^A-Za-z0-9_.:-]", "_", tool_name)[:120]
    body = {
        "project_id": config["project_id"],
        "title": f"Codex permission: {tool_name[:120]}",
        "summary": "Review the exact Codex tool request before this paused invocation continues.",
        "agent_name": "Codex",
        "target_branch": "current-workspace",
        "files": [{"path": f"codex://tool/{safe_tool}", "change_type": "tool_call",
                   "diff": arg_bytes.decode("utf-8"), "new_content": None}],
        "test_plan": ["Confirm the requested Codex tool and arguments are expected"],
        "rollback_plan": "Deny this request if uncertain. Approval applies only to this invocation.",
        "source_session_id": source_session_id,
        "source_invocation_id": source_invocation_id,
    }
    api_base = config["api_base_url"]
    try:
        created = _create_approval_with_retry(
            api_base=api_base,
            config=config,
            token=token,
            body=body,
            diagnostics=diagnostics,
        )
    except GateError:
        return "deny", "Control Tower rejected the approval request"
    if created is None:
        return "deny", "Control Tower approval creation remained ambiguous"
    approval_id = created.get("id")
    # Approval IDs are used as URL path segments in every subsequent request.
    # Require the backend's canonical opaque-ID format before interpolating it;
    # the diagnostic validator alone is not an authorization boundary.
    if (not isinstance(approval_id, str)
            or DIAGNOSTIC_APPROVAL_ID.fullmatch(approval_id) is None
            or created.get("status") not in {"PENDING_REVIEW", "REQUESTED", "APPROVED"}
            or not _identity_matches(created, approval_id=approval_id, config=config)
            or not _matches(created, body=body, config=config)):
        return "deny", "Control Tower did not create a matching actionable approval"
    diagnostics["approval_id"] = _validated_diagnostic_approval_id(approval_id)
    original = _snapshot(created)
    wait_started_at = time.monotonic()
    deadline = wait_started_at + timeout
    next_heartbeat_at = wait_started_at + INVOCATION_HEARTBEAT_INTERVAL_SECONDS
    reconnect_backoff_seconds = POLL_INTERVAL_SECONDS
    while time.monotonic() < deadline:
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            break
        next_heartbeat_at, lease_live = _refresh_invocation_lease_if_due(
            api_base=api_base,
            approval_id=approval_id,
            config=config,
            token=token,
            source_invocation_id=source_invocation_id,
            deadline=deadline,
            next_heartbeat_at=next_heartbeat_at,
            diagnostics=diagnostics,
        )
        if not lease_live:
            return "deny", "Connector liveness could not be confirmed"
        try:
            current = _request(
                "GET",
                f"{api_base}/agents/approvals/{approval_id}/decision",
                config,
                token,
                timeout_seconds=remaining_seconds,
            )
        except RateLimitError as exc:
            now = time.monotonic()
            remaining_seconds = max(0.0, deadline - now)
            # Chunk Retry-After at the next heartbeat. A long server-directed
            # backoff must not allow this still-running callback's lease to
            # expire before the hook can renew it.
            until_heartbeat_seconds = max(0.0, next_heartbeat_at - now)
            time.sleep(min(exc.retry_after_seconds, remaining_seconds, until_heartbeat_seconds))
            continue
        except AmbiguousRequestError:
            # Decision reads are idempotent; creation and consume writes are
            # never retried after an ambiguous outcome.
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                break
            time.sleep(min(reconnect_backoff_seconds, remaining_seconds))
            reconnect_backoff_seconds = min(
                reconnect_backoff_seconds * 2, MAX_RECONNECT_BACKOFF_SECONDS
            )
            continue
        reconnect_backoff_seconds = POLL_INTERVAL_SECONDS
        if not _identity_matches(current, approval_id=approval_id, config=config):
            return "deny", "Control Tower returned a different approval request"
        try:
            if _snapshot(current) != original:
                return "deny", "Approval payload changed after the original Codex request"
        except (KeyError, TypeError):
            return "deny", "Control Tower approval payload is incomplete"
        status = current.get("status")
        if time.monotonic() >= deadline and status in {"APPROVED", "PENDING_REVIEW", "REQUESTED"}:
            # A slow decision read must not extend the host's configured wait.
            # The expiry endpoint resolves a concurrent approval without ever
            # claiming or returning it as an allow.
            break
        if status in {"DENIED", "REJECTED", "REVISION_REQUESTED", "EXPIRED", "CANCELLED", "BLOCKED", "FAILED"}:
            return "deny", f"Control Tower decision: {status}"
        if status in {"APPROVED", "PENDING_REVIEW", "REQUESTED"}:
            next_heartbeat_at, lease_live = _refresh_invocation_lease_if_due(
                api_base=api_base,
                approval_id=approval_id,
                config=config,
                token=token,
                source_invocation_id=source_invocation_id,
                deadline=deadline,
                next_heartbeat_at=next_heartbeat_at,
                diagnostics=diagnostics,
            )
            if not lease_live:
                return "deny", "Connector liveness could not be confirmed"
        if status == "APPROVED":
            if current.get("approved_payload_hash") != _hash(current):
                return "deny", "Approved payload integrity check failed"
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                break
            try:
                claimed = _request(
                    "POST",
                    f"{api_base}/agents/approvals/{approval_id}/consume",
                    config,
                    token,
                    timeout_seconds=remaining_seconds,
                )
            except AmbiguousRequestError:
                _report_delivery_failure(
                    api_base=api_base,
                    approval_id=approval_id,
                    config=config,
                    token=token,
                    source_invocation_id=str(current.get("source_invocation_id") or ""),
                    approved_payload_hash=str(current.get("approved_payload_hash") or ""),
                    timeout_seconds=max(0.0, deadline - time.monotonic()),
                )
                return "deny", "Approval delivery outcome was ambiguous; Codex request denied"
            except GateError:
                return "deny", "Approval delivery could not be claimed safely"
            if time.monotonic() >= deadline:
                return "deny", "Approval arrived after the Codex request deadline"
            if claimed.get("consumed") is not True:
                return "deny", "Approval delivery was already claimed"
            return "allow", "Approved from Aixion Control Tower"
        if status not in {"PENDING_REVIEW", "REQUESTED"}:
            return "deny", f"Unrecognized Control Tower approval state: {status}"
        time.sleep(min(POLL_INTERVAL_SECONDS, max(0, deadline - time.monotonic())))
    try:
        expired = _request(
            "POST",
            f"{api_base}/agents/approvals/{approval_id}/expire",
            config,
            token,
            timeout_seconds=EXPIRY_CLEANUP_TIMEOUT_SECONDS,
        )
    except GateError:
        return "deny", "Approval timed out; expiration could not be confirmed"
    if not _identity_matches(expired, approval_id=approval_id, config=config):
        return "deny", "Control Tower returned a different approval request"
    if expired.get("status") == "APPROVED":
        return "deny", "Approval raced with the host timeout; the Codex request was denied"
    if expired.get("status") != "EXPIRED":
        return "deny", "Approval timed out; the Codex request was denied"
    return "deny", "Approval timed out; the request expired and the Codex request was denied"


def run(event: dict[str, Any], environ: dict[str, str] | None = None) -> tuple[str, str]:
    diagnostics = _new_diagnostic_context()
    try:
        result = _run(event, environ, diagnostics)
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
        decision=result[0],
    )
    return result


def _report_delivery_failure(
    *,
    api_base: str,
    approval_id: str,
    config: dict[str, str],
    token: str,
    source_invocation_id: str,
    approved_payload_hash: str,
    timeout_seconds: float,
) -> None:
    """Best-effort audit only; failure to report can never turn deny into allow."""
    if timeout_seconds <= 0:
        return
    try:
        _request(
            "POST",
            f"{api_base}/agents/approvals/{approval_id}/delivery-failure",
            config,
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


def check_connection(environ: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ if environ is None else environ
    config = _configuration(env)
    token = _agent_token(env, config)
    try:
        response = deadline_request(
            "GET", f"{config['api_base_url']}/health", {"Accept": "application/json"},
            None, 5, max_response_bytes=64 * 1024,
        )
        if response.status != 200 or json.loads(response.body).get("status") != "ok":
            raise GateError("Control Tower health check did not return status ok")
    except (OSError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise GateError("Control Tower health check failed") from exc
    identity_query = urlencode({"provider": "CODEX", "project_id": config["project_id"]})
    identity = _request(
        "GET",
        f"{config['api_base_url']}/agents/identity?{identity_query}",
        config,
        token,
    )
    if (
        identity.get("agent_id") != config["agent_id"]
        or identity.get("provider") != "CODEX"
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
    return {"api_base_url": config["api_base_url"], "agent_id": config["agent_id"],
            "project_id": config["project_id"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Protected non-secret workspace configuration")
    parser.add_argument("--check", action="store_true", help="Check configuration and API health")
    args = parser.parse_args(argv)
    if args.config:
        os.environ["AIXION_CODEX_CONFIG"] = args.config
    if args.check:
        try:
            details = check_connection()
        except Exception as exc:
            sys.stderr.write(f"Aixion Codex hook preflight failed: {exc.__class__.__name__}\n")
            return 1
        sys.stdout.write(f"Aixion Codex hook preflight passed for {details['agent_id']} / {details['project_id']}\n")
        return 0
    try:
        event = _read_input()
    except Exception as exc:
        _emit_diagnostic(
            context=_new_diagnostic_context(),
            event="terminal_decision",
            decision="deny",
        )
        behavior, message = "deny", f"Aixion approval gate failed: {exc.__class__.__name__}"
    else:
        try:
            behavior, message = run(event)
        except Exception as exc:
            behavior, message = "deny", f"Aixion approval gate failed: {exc.__class__.__name__}"
    _emit_decision(behavior, message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
