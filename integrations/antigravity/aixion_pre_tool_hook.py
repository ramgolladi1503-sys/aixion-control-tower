#!/usr/bin/env python3
"""Fail-closed Antigravity PreToolUse gate backed by Control Tower approvals."""
from __future__ import annotations

import hashlib
import argparse
import json
import os
import re
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlencode, urlsplit

MAX_INPUT_BYTES = 128 * 1024
MAX_ARGUMENTS_BYTES = 48 * 1024
MAX_CONFIG_BYTES = 16 * 1024
MAX_POLL_SECONDS = 15 * 60
POLL_INTERVAL_SECONDS = 2
MAX_RECONNECT_BACKOFF_SECONDS = 10
APPROVAL_PROTOCOL_VERSION = "1"
SENSITIVE_KEY = re.compile(r"(token|secret|password|credential|private.?key|api.?key)", re.I)
SENSITIVE_VALUE = re.compile(
    r"(?:bearer\s+[A-Za-z0-9._~-]{8,}|(?:api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*[\"']?[A-Za-z0-9._~/-]{8,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)",
    re.I,
)
READ_ONLY_TOOLS = frozenset({
    "view_file", "list_dir", "find_by_name", "grep_search",
    "search_web", "read_url_content", "list_permissions",
})
READ_PATH_ARGUMENTS = frozenset({"absolutepath", "directorypath", "searchdirectory", "searchpath"})
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
    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = max(1, min(MAX_POLL_SECONDS, retry_after_seconds))
        super().__init__("Control Tower rate limit exceeded")


def _decision(value: str, reason: str) -> dict[str, str]:
    return {"decision": value, "reason": reason[:500]}


def _emit(value: dict[str, str]) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _read_input() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise GateError("Hook input exceeds the configured size limit")
    value = json.loads(raw)
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
    return isinstance(value, str) and SENSITIVE_VALUE.search(value) is not None


def _has_sensitive_path(args: Any) -> bool:
    """Identify secret-bearing paths in documented Antigravity read arguments."""
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
    return False


def _request(
    method: str,
    url: str,
    agent_id: str,
    token: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        method=method,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Aixion-Agent-Id": agent_id,
            "X-Aixion-Agent-Token": token,
            "X-Aixion-Approval-Protocol": APPROVAL_PROTOCOL_VERSION,
        },
    )
    try:
        opener = urllib.request.build_opener(_NoRedirectHandler())
        with opener.open(request, timeout=10) as response:
            content = response.read(256 * 1024 + 1)
            if len(content) > 256 * 1024:
                raise AmbiguousRequestError("Control Tower response exceeds the size limit")
            try:
                result = json.loads(content)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise AmbiguousRequestError("Control Tower returned invalid response data") from exc
            if not isinstance(result, dict):
                raise AmbiguousRequestError("Unexpected Control Tower response")
            return result
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            retry_after_header = exc.headers.get("Retry-After") if exc.headers else None
            try:
                retry_after_seconds = int(retry_after_header) if retry_after_header else POLL_INTERVAL_SECONDS
            except (TypeError, ValueError):
                retry_after_seconds = POLL_INTERVAL_SECONDS
            raise RateLimitError(retry_after_seconds) from exc
        if exc.code >= 500:
            raise AmbiguousRequestError("Control Tower returned an ambiguous server failure") from exc
        raise GateError(f"Control Tower returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise AmbiguousRequestError("Control Tower request outcome is unknown") from exc


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward the agent token to a redirect destination."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


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
    if os.path.lexists(config_path):
        try:
            descriptor = os.open(config_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream:
                metadata = os.fstat(stream.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                    raise GateError("Agent config must be a regular file owned by this user")
                if stat.S_IMODE(metadata.st_mode) & 0o077:
                    raise GateError("Agent config permissions must exclude group and other users")
                raw_config = stream.read(MAX_CONFIG_BYTES + 1)
            if len(raw_config) > MAX_CONFIG_BYTES:
                raise GateError("Agent config exceeds its size limit")
            config = json.loads(raw_config)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GateError("Agent config is unreadable or invalid") from exc
        if not isinstance(config, dict):
            raise GateError("Agent config must be a JSON object")
    elif not all(env.get(key) for key in (
        "AIXION_API_BASE_URL", "AIXION_AGENT_ID", "AIXION_PROJECT_ID"
    )):
        raise GateError("Agent config file or required non-secret environment values are missing")

    values = {
        "api_base_url": env.get("AIXION_API_BASE_URL") or config.get("api_base_url", ""),
        "agent_id": env.get("AIXION_AGENT_ID") or config.get("agent_id", ""),
        "project_id": env.get("AIXION_PROJECT_ID") or config.get("project_id", ""),
        "keychain_service": env.get("AIXION_AGENT_TOKEN_KEYCHAIN_SERVICE")
        or config.get("keychain_service", "com.aixion.controltower.agent"),
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
    request = urllib.request.Request(
        f"{api_base}/health",
        method="GET",
        headers={"Accept": "application/json"},
    )
    try:
        opener = urllib.request.build_opener(_NoRedirectHandler())
        with opener.open(request, timeout=5) as response:
            content = response.read(64 * 1024 + 1)
            if len(content) > 64 * 1024:
                raise GateError("Health response exceeds the size limit")
            health = json.loads(content)
            if not isinstance(health, dict) or health.get("status") != "ok":
                raise GateError("Control Tower health check did not return status ok")
    except urllib.error.HTTPError as exc:
        raise GateError(f"Control Tower health check returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
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
            descriptor = os.open(token_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream:
                metadata = os.fstat(stream.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                    raise GateError("Agent token file must be a regular file owned by this user")
                if stat.S_IMODE(metadata.st_mode) & 0o077:
                    raise GateError("Agent token file permissions must exclude group and other users")
                token_bytes = stream.read(8193)
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


def run(payload: dict[str, Any], environ: dict[str, str] | None = None) -> dict[str, str]:
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
    if tool_name in READ_ONLY_TOOLS and not _has_sensitive_path(args):
        return _decision("allow", "Read-only tool left to Antigravity policy")

    try:
        approval_timeout_seconds = int(env.get("AIXION_APPROVAL_TIMEOUT_SECONDS", "600"))
    except (TypeError, ValueError):
        return _decision("deny", "Approval timeout configuration is invalid")
    if not 1 <= approval_timeout_seconds <= MAX_POLL_SECONDS:
        return _decision("deny", "Approval timeout must be between 1 and 900 seconds")

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
    session_id = str(payload.get("conversationId") or payload.get("sessionId") or "")[:200]
    step_index = payload.get("stepIdx")
    if not session_id or isinstance(step_index, bool) or not isinstance(step_index, int) or step_index < 0:
        return _decision("deny", "Antigravity request is missing its session or step identity")
    invocation_material = json.dumps(
        [session_id, step_index, tool_name, args], sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    invocation_id = "antigravity:v1:" + hashlib.sha256(invocation_material).hexdigest()
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
        "source_session_id": session_id or None,
        "source_invocation_id": invocation_id,
    }
    created = _request("POST", f"{api_base}/agents/approvals", agent_id, token, approval_body)
    approval_id = created.get("id")
    if (
        not isinstance(approval_id, str)
        or not approval_id
        or created.get("status") not in {"PENDING_REVIEW", "REQUESTED", "APPROVED"}
        or not _matches_request_identity(
            created, approval_id=approval_id, project_id=project_id, agent_id=agent_id
        )
    ):
        return _decision("deny", "Control Tower did not create an actionable approval")
    if not _matches_invocation_payload(created, body=approval_body, agent_id=agent_id):
        return _decision("deny", "Control Tower approval does not match the original tool invocation")
    try:
        created_snapshot = _approval_snapshot(created)
    except (KeyError, TypeError):
        return _decision("deny", "Control Tower approval payload is incomplete")

    deadline = time.monotonic() + approval_timeout_seconds
    reconnect_backoff_seconds = POLL_INTERVAL_SECONDS
    while time.monotonic() < deadline:
        try:
            current = _request("GET", f"{api_base}/agents/approvals/{approval_id}/decision", agent_id, token)
        except RateLimitError as exc:
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                break
            time.sleep(min(exc.retry_after_seconds, remaining_seconds))
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
            if _approval_snapshot(current) != created_snapshot:
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
        if status == "APPROVED":
            if current.get("approved_payload_hash") != _payload_hash(current):
                return _decision("deny", "Approved payload integrity check failed")
            try:
                claimed = _request(
                    "POST", f"{api_base}/agents/approvals/{approval_id}/consume", agent_id, token
                )
            except AmbiguousRequestError:
                _report_delivery_failure(
                    api_base=api_base,
                    approval_id=approval_id,
                    agent_id=agent_id,
                    token=token,
                    source_invocation_id=str(current.get("source_invocation_id") or ""),
                    approved_payload_hash=str(current.get("approved_payload_hash") or ""),
                )
                return _decision("deny", "Approval delivery outcome was ambiguous; tool denied")
            except GateError:
                return _decision("deny", "Approval delivery could not be claimed safely")
            if claimed.get("consumed") is not True:
                return _decision("deny", "Approval delivery was already claimed")
            return _decision("allow", "Approved from Aixion Control Tower")
        if status not in {"PENDING_REVIEW", "REQUESTED"}:
            return _decision("deny", f"Unrecognized approval state: {status}")
        time.sleep(POLL_INTERVAL_SECONDS)
    try:
        expired = _request(
            "POST", f"{api_base}/agents/approvals/{approval_id}/expire", agent_id, token
        )
    except GateError:
        return _decision("deny", "Approval timed out; expiration could not be confirmed")
    if not _matches_request_identity(
        expired, approval_id=approval_id, project_id=project_id, agent_id=agent_id
    ):
        return _decision("deny", "Control Tower returned a different approval request")
    if expired.get("status") == "APPROVED":
        return _decision("deny", "Approval raced with the host timeout; the tool call was denied")
    if expired.get("status") != "EXPIRED":
        return _decision("deny", "Approval timed out; the tool call was denied")
    return _decision("deny", "Approval timed out; the request expired and the tool call was denied")


def _report_delivery_failure(
    *,
    api_base: str,
    approval_id: str,
    agent_id: str,
    token: str,
    source_invocation_id: str,
    approved_payload_hash: str,
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
        result = run(_read_input())
    except Exception as exc:  # fail closed and never print arguments or credentials
        _emit(_decision("deny", f"Aixion approval gate failed: {exc.__class__.__name__}"))
        return 0
    _emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
