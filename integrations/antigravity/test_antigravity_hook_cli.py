from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import socket
import struct
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

HOOK_PATH = Path(__file__).with_name("aixion_pre_tool_hook.py")
SPEC = importlib.util.spec_from_file_location("aixion_antigravity_cli_hook", HOOK_PATH)
assert SPEC and SPEC.loader
hook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hook)


@pytest.mark.parametrize(
    ("decision", "reason"),
    [("allow", "read-only passthrough"), ("auto_approve", "phone approved"), ("deny", "rejected")],
)
def test_main_reads_stdin_and_emits_antigravity_decision(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    decision: str,
    reason: str,
) -> None:
    payload = b'{"toolCall":{"name":"run_command","args":{"command":"echo safe"}}}'
    monkeypatch.setattr(hook.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(payload)))
    monkeypatch.setattr(
        hook,
        "run",
        lambda received: (
            {"decision": decision, "reason": reason}
            if received["toolCall"]["name"] == "run_command"
            else {"decision": "deny", "reason": "unexpected input"}
        ),
    )

    assert hook.main([]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == f'{{"decision":"{decision}","reason":"{reason}"}}\n'


@pytest.mark.parametrize("raw_input", [b"{", b""])
def test_main_emits_fail_closed_deny_for_malformed_or_missing_input(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    raw_input: bytes,
) -> None:
    monkeypatch.setattr(hook.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw_input)))

    assert hook.main([]) == 0

    captured = capsys.readouterr()
    diagnostic = json.loads(captured.err)
    assert diagnostic["event"] == "terminal_decision"
    assert diagnostic["correlation_id"] is None
    assert diagnostic["approval_id"] is None
    assert diagnostic["decision"] == "deny"
    assert captured.out.startswith('{"decision":"deny","reason":"Aixion approval gate failed:')
    assert captured.out.endswith('"}\n')


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_main_denies_nonstandard_json_numbers_before_approval_requests(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    constant: str,
) -> None:
    raw_input = (
        '{"toolCall":{"name":"run_command","args":{"value":'
        f"{constant}" + "}}}"
    ).encode()
    requests: list[str] = []
    monkeypatch.setattr(hook.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw_input)))

    def unexpected_request(method: str, *_args: object, **_kwargs: object) -> dict:
        requests.append(method)
        raise AssertionError("Invalid hook input must not reach the approval API")

    monkeypatch.setattr(hook, "_request", unexpected_request)

    assert hook.main([]) == 0

    captured = capsys.readouterr()
    assert requests == []
    assert json.loads(captured.out)["decision"] == "deny"


@pytest.mark.parametrize("number", ["1e999", "-1e999"])
def test_main_denies_overflowing_json_numbers_before_approval_requests(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    number: str,
) -> None:
    raw_input = (
        '{"toolCall":{"name":"run_command","args":{"value":'
        f"{number}" + "}}}"
    ).encode()
    requests: list[str] = []
    monkeypatch.setattr(hook.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw_input)))

    def unexpected_request(method: str, *_args: object, **_kwargs: object) -> dict:
        requests.append(method)
        raise AssertionError("Invalid hook input must not reach the approval API")

    monkeypatch.setattr(hook, "_request", unexpected_request)

    assert hook.main([]) == 0

    captured = capsys.readouterr()
    assert requests == []
    assert json.loads(captured.out)["decision"] == "deny"


def _hook_payload() -> dict:
    return {
        "toolCall": {"name": "run_command", "args": {"command": "printf hello"}},
        "conversationId": "conversation-42",
        "stepIdx": 7,
    }


def _approval_from_request(body: dict, *, status: str = "PENDING_REVIEW") -> dict:
    """Build the exact response shape returned by the approval API."""
    return {
        "id": "approval_0123456789ab",
        "project_id": body["project_id"],
        "status": status,
        "title": body["title"],
        "summary": body["summary"],
        "agent_name": body["agent_name"],
        "target_branch": body["target_branch"],
        "files": body["files"],
        "test_plan": body["test_plan"],
        "rollback_plan": body["rollback_plan"],
        "source_provider": "ANTIGRAVITY",
        "source_agent_id": "agent-123",
        "source_agent_name": body.get("source_agent_name"),
        "source_session_id": body["source_session_id"],
        "source_invocation_id": body["source_invocation_id"],
        "source_task_url": body.get("source_task_url"),
    }


def _test_environment() -> dict[str, str]:
    return {
        "AIXION_API_BASE_URL": "http://127.0.0.1:8001",
        "AIXION_AGENT_ID": "agent-123",
        "AIXION_PROJECT_ID": "project-456",
        "AIXION_AGENT_TOKEN": "test-token",
        "AIXION_ALLOW_AGENT_TOKEN_ENV": "true",
        "AIXION_APPROVAL_TIMEOUT_SECONDS": "30",
    }


def test_diagnostic_approval_id_requires_backend_id_format() -> None:
    assert hook._validated_diagnostic_approval_id("approval_0123456789ab") == "approval_0123456789ab"
    assert hook._validated_diagnostic_approval_id("diagnostic_token_canary") is None


def test_read_only_symlink_to_secret_requires_phone_approval(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret_file = tmp_path / ".env"
    secret_file.write_text("API_KEY=not-for-phone", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    benign_link = workspace / "notes.txt"
    benign_link.symlink_to(secret_file)
    payload = _hook_payload()
    payload["toolCall"] = {
        "name": "view_file",
        "args": {"absolutePath": str(benign_link)},
    }
    requests: list[tuple[str, str]] = []
    created: dict = {}

    def request(
        method: str,
        url: str,
        _agent_id: str,
        _token: str,
        body: dict | None = None,
        **_kwargs: object,
    ) -> dict:
        requests.append((method, url))
        if method == "POST" and url.endswith("/agents/approvals"):
            assert body is not None
            created.update(_approval_from_request(body))
            return dict(created)
        if method == "GET" and url.endswith("/decision"):
            return {**created, "status": "DENIED"}
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr(hook, "_request", request)
    result = hook.run(payload, _test_environment())

    assert result == {"decision": "deny", "reason": "Control Tower decision: DENIED"}
    assert requests[0][0] == "POST"
    assert requests[0][1].endswith("/agents/approvals")


def test_read_only_symlink_resolution_error_requires_review(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    loop_a = workspace / "a.txt"
    loop_b = workspace / "b.txt"
    loop_a.symlink_to(loop_b)
    loop_b.symlink_to(loop_a)

    assert hook._has_sensitive_path({"absolutePath": str(loop_a)}) is True


def test_run_consumes_the_approved_invocation_before_returning_auto_approve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, dict | None]] = []
    created_approval: dict = {}

    def request(method: str, url: str, agent_id: str, token: str, body: dict | None = None, **_kwargs: object) -> dict:
        assert agent_id == "agent-123"
        assert token == "test-token"
        calls.append((method, url, body))
        if method == "POST" and url.endswith("/agents/approvals"):
            assert body is not None
            assert body["source_session_id"] == "conversation-42"
            assert body["source_invocation_id"].startswith("antigravity:v1:")
            created_approval.update(_approval_from_request(body))
            return dict(created_approval)
        if method == "GET" and url.endswith("/decision"):
            approved = {**created_approval, "status": "APPROVED"}
            approved["approved_payload_hash"] = hook._payload_hash(approved)
            return approved
        if method == "POST" and url.endswith("/consume"):
            return {"consumed": True}
        raise AssertionError(f"Unexpected approval protocol request: {method} {url}")

    monkeypatch.setattr(hook, "_request", request)
    monkeypatch.setattr(hook.time, "sleep", lambda _seconds: None)

    result = hook.run(_hook_payload(), _test_environment())

    assert result == {"decision": "auto_approve", "reason": "Approved from Aixion Control Tower"}
    assert [method for method, _url, _body in calls] == ["POST", "GET", "POST"]
    assert calls[-1][1].endswith("/agents/approvals/approval_0123456789ab/consume")


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_run_denies_nonstandard_json_api_response_without_consuming(
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
) -> None:
    requests: list[tuple[str, str]] = []

    def deadline_request(
        method: str,
        url: str,
        *_args: object,
        **_kwargs: object,
    ) -> SimpleNamespace:
        requests.append((method, url))
        return SimpleNamespace(status=201, headers={}, body=f'{{"id":{constant}}}'.encode())

    monkeypatch.setattr(hook, "deadline_request", deadline_request)
    monkeypatch.setattr(hook.time, "sleep", lambda _seconds: None)

    result = hook.run(_hook_payload(), _test_environment())

    assert result["decision"] == "deny"
    assert result["reason"] == "Control Tower approval creation remained ambiguous"
    assert len(requests) == hook.MAX_CREATE_ATTEMPTS
    assert all(method == "POST" for method, _url in requests)
    assert not any(url.endswith("/consume") for _method, url in requests)


def test_run_denial_never_consumes_or_allows_the_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    created_approval: dict = {}

    def request(method: str, url: str, _agent_id: str, _token: str, body: dict | None = None, **_kwargs: object) -> dict:
        calls.append((method, url))
        if method == "POST" and url.endswith("/agents/approvals"):
            assert body is not None
            created_approval.update(_approval_from_request(body))
            return dict(created_approval)
        if method == "GET" and url.endswith("/decision"):
            return {**created_approval, "status": "DENIED"}
        raise AssertionError(f"Denied invocation must not make another request: {method} {url}")

    monkeypatch.setattr(hook, "_request", request)

    result = hook.run(_hook_payload(), _test_environment())

    assert result == {"decision": "deny", "reason": "Control Tower decision: DENIED"}
    assert [method for method, _url in calls] == ["POST", "GET"]
    assert not any(url.endswith("/consume") for _method, url in calls)


def test_revoked_connector_credentials_during_decision_poll_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A 401 after creation must become a provider deny, never a claim."""
    created: dict = {}
    calls: list[str] = []
    payload = json.dumps(_hook_payload()).encode()
    monkeypatch.setattr(hook.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(payload)))
    for key, value in _test_environment().items():
        monkeypatch.setenv(key, value)

    def request(method: str, url: str, _agent_id: str, _token: str,
                body: dict | None = None, **_kwargs: object) -> dict:
        calls.append(url.rsplit("/", 1)[-1])
        if method == "POST" and url.endswith("/agents/approvals"):
            assert body is not None
            created.update(_approval_from_request(body))
            return dict(created)
        if method == "GET" and url.endswith("/decision"):
            raise hook.GateError("HTTP 401")
        raise AssertionError(f"Revoked connector must not continue: {method} {url}")

    monkeypatch.setattr(hook, "_request", request)

    assert hook.main([]) == 0

    output = capsys.readouterr()
    decision = json.loads(output.out)
    assert decision["decision"] == "deny"
    assert "GateError" in decision["reason"]
    assert calls == ["approvals", "decision"]
    assert "consume" not in calls


def test_revoked_connector_credentials_before_consume_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An approved snapshot cannot bypass revocation at the claim boundary."""
    created: dict = {}
    calls: list[str] = []

    def request(
        method: str,
        url: str,
        _agent_id: str,
        _token: str,
        body: dict | None = None,
        **_kwargs: object,
    ) -> dict:
        endpoint = url.rsplit("/", 1)[-1]
        calls.append(endpoint)
        if method == "POST" and endpoint == "approvals":
            assert body is not None
            created.update(_approval_from_request(body))
            return dict(created)
        if method == "GET" and endpoint == "decision":
            approved = {**created, "status": "APPROVED"}
            approved["approved_payload_hash"] = hook._payload_hash(approved)
            return approved
        if method == "POST" and endpoint == "consume":
            # Revoke the connector after the decision read but before the
            # backend's atomic authorization check for claiming delivery.
            raise hook.GateError("HTTP 401: token revoked")
        raise AssertionError(f"Unexpected request after revocation: {method} {url}")

    monkeypatch.setattr(hook, "_request", request)
    monkeypatch.setattr(hook.time, "sleep", lambda _seconds: None)

    result = hook.run(_hook_payload(), _test_environment())

    assert result == {
        "decision": "deny",
        "reason": "Approval delivery could not be claimed safely",
    }
    assert calls == ["approvals", "decision", "consume"]


def test_new_provider_session_cannot_reuse_previous_session_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restarted provider session gets a distinct invocation identity."""
    calls: list[tuple[str, str, dict | None]] = []
    previous_approval: dict | None = None

    def request(method: str, url: str, _agent_id: str, _token: str,
                body: dict | None = None, **_kwargs: object) -> dict:
        nonlocal previous_approval
        endpoint = url.rsplit("/", 1)[-1]
        calls.append((method, endpoint, body))
        if method == "POST" and url.endswith("/agents/approvals"):
            if body is None:
                raise AssertionError("Create request must include its invocation")
            if previous_approval is None:
                previous_approval = _approval_from_request(body)
                return dict(previous_approval)
            # Simulate a stale response/cache returning the prior session's row.
            return {**previous_approval, "status": "PENDING_REVIEW"}
        if method == "GET" and url.endswith("/decision"):
            assert previous_approval is not None
            return {**previous_approval, "status": "DENIED"}
        raise AssertionError(f"Stale approval must not advance: {method} {url}")

    monkeypatch.setattr(hook, "_request", request)
    first = _hook_payload()
    second = _hook_payload()
    second["conversationId"] = "conversation-after-restart"

    first_result = hook.run(first, _test_environment())
    second_result = hook.run(second, _test_environment())

    create_bodies = [body for method, endpoint, body in calls if method == "POST" and endpoint == "approvals"]
    assert len(create_bodies) == 2
    assert create_bodies[0] is not None and create_bodies[1] is not None
    assert create_bodies[0]["source_invocation_id"] != create_bodies[1]["source_invocation_id"]
    assert first_result["decision"] == "deny"
    assert second_result == {
        "decision": "deny",
        "reason": "Control Tower approval does not match the original tool invocation",
    }
    assert not any(method == "POST" and endpoint == "consume" for method, endpoint, _body in calls)


@pytest.mark.parametrize("failure", ["changed_payload", "invalid_approved_hash"])
def test_run_denies_invalid_approval_snapshot_or_hash_before_consuming(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    calls: list[str] = []
    created_approval: dict = {}

    def request(method: str, url: str, _agent_id: str, _token: str, body: dict | None = None, **_kwargs: object) -> dict:
        calls.append(method)
        if method == "POST" and url.endswith("/agents/approvals"):
            assert body is not None
            created_approval.update(_approval_from_request(body))
            return dict(created_approval)
        if method == "GET" and url.endswith("/decision"):
            approved = {**created_approval, "status": "APPROVED"}
            if failure == "changed_payload":
                approved["summary"] = "different invocation"
                approved["approved_payload_hash"] = hook._payload_hash(approved)
            else:
                approved["approved_payload_hash"] = hook._payload_hash(approved)
                approved["approved_payload_hash"] = "sha256:" + "0" * 64
            return approved
        raise AssertionError(f"Invalid approval must not be consumed: {method} {url}")

    monkeypatch.setattr(hook, "_request", request)

    result = hook.run(_hook_payload(), _test_environment())

    assert result["decision"] == "deny"
    if failure == "changed_payload":
        assert result["reason"] == "Approval payload changed after the original tool invocation"
    else:
        assert result["reason"] == "Approved payload integrity check failed"
    assert calls == ["POST", "GET"]


def test_run_recovers_lost_create_response_with_identical_invocation_retry(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[str, str, dict | None]] = []
    persisted: dict = {}

    def request(method: str, url: str, agent_id: str, token: str, body: dict | None = None, **_kwargs: object) -> dict:
        calls.append((method, url, body))
        if method == "POST" and url.endswith("/agents/approvals"):
            assert body is not None
            if not persisted:
                persisted.update(_approval_from_request(body))
                raise hook.AmbiguousRequestError("server committed but response was lost")
            assert body == calls[0][2]
            return dict(persisted)
        if method == "GET" and url.endswith("/decision"):
            approved = {**persisted, "status": "APPROVED"}
            approved["approved_payload_hash"] = hook._payload_hash(approved)
            return approved
        if method == "POST" and url.endswith("/consume"):
            return {"consumed": True}
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr(hook, "_request", request)
    monkeypatch.setattr(hook.time, "sleep", lambda _seconds: None)

    payload = _hook_payload()
    payload["toolCall"]["args"]["command"] = "diagnostic_action_canary"
    environment = _test_environment()
    environment["AIXION_AGENT_TOKEN"] = "diagnostic_token_canary"
    result = hook.run(payload, environment)

    assert result["decision"] == "auto_approve"
    assert [method for method, _url, _body in calls] == ["POST", "POST", "GET", "POST"]
    assert calls[0][2] is calls[1][2]
    assert calls[0][2]["source_invocation_id"] == persisted["source_invocation_id"]
    captured = capsys.readouterr()
    assert captured.out == ""
    events = [json.loads(line) for line in captured.err.splitlines()]
    correlation_id = hashlib.sha256(
        persisted["source_invocation_id"].encode("utf-8")
    ).hexdigest()[:16]
    retry = next(event for event in events if event["event"] == "approval_create_retry")
    terminal = next(event for event in events if event["event"] == "terminal_decision")
    assert retry["correlation_id"] == correlation_id
    assert retry["approval_id"] is None
    assert retry["attempt"] == 2
    assert terminal["correlation_id"] == correlation_id
    assert terminal["approval_id"] == "approval_0123456789ab"
    assert terminal["decision"] == "auto_approve"
    assert "diagnostic_token_canary" not in captured.err
    assert "diagnostic_action_canary" not in captured.err


def test_run_denies_after_bounded_ambiguous_create_retries(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[dict] = []

    def request(method: str, url: str, _agent_id: str, _token: str, body: dict | None = None, **_kwargs: object) -> dict:
        assert method == "POST" and url.endswith("/agents/approvals")
        assert body is not None
        calls.append(body.copy())
        raise hook.AmbiguousRequestError("response remained unavailable")

    monkeypatch.setattr(hook, "_request", request)
    monkeypatch.setattr(hook.time, "sleep", lambda _seconds: None)

    result = hook.run(_hook_payload(), _test_environment())

    assert result["decision"] == "deny"
    assert "remained ambiguous" in result["reason"]
    assert len(calls) == hook.MAX_CREATE_ATTEMPTS == 2
    assert calls[0] == calls[1]
    captured = capsys.readouterr()
    events = [json.loads(line) for line in captured.err.splitlines()]
    assert [event["event"] for event in events] == [
        "approval_create_retry",
        "approval_create_retry_exhausted",
        "terminal_decision",
    ]
    assert events[1]["attempt"] == 2
    assert events[2]["decision"] == "deny"


@pytest.mark.parametrize("failure", ["conflict", "mismatch"])
def test_run_denies_create_conflict_or_mismatched_retry_response(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    calls = 0

    def request(method: str, url: str, _agent_id: str, _token: str, body: dict | None = None, **_kwargs: object) -> dict:
        nonlocal calls
        calls += 1
        assert method == "POST" and url.endswith("/agents/approvals")
        assert body is not None
        if failure == "conflict":
            raise hook.GateError("HTTP 409")
        mismatched = _approval_from_request(body)
        mismatched["source_invocation_id"] += ":different"
        return mismatched

    monkeypatch.setattr(hook, "_request", request)

    result = hook.run(_hook_payload(), _test_environment())

    assert result["decision"] == "deny"
    assert calls == 1


@pytest.mark.parametrize(
    "invalid_id",
    [
        "approval_0123456789ab/../../identity",
        "approval_0123456789ab?scope=other",
        "approval_0123456789aZ",
        "",
    ],
)
def test_run_rejects_noncanonical_approval_id_before_followup_request(
    monkeypatch: pytest.MonkeyPatch,
    invalid_id: str,
) -> None:
    calls: list[str] = []

    def request(method: str, url: str, _agent_id: str, _token: str,
                body: dict | None = None, **_kwargs: object) -> dict:
        calls.append(url)
        assert method == "POST" and url.endswith("/agents/approvals")
        assert body is not None
        response = _approval_from_request(body)
        response["id"] = invalid_id
        return response

    monkeypatch.setattr(hook, "_request", request)

    result = hook.run(_hook_payload(), _test_environment())

    assert result["decision"] == "deny"
    assert len(calls) == 1


class _FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _patch_clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    clock = _FakeClock()
    monkeypatch.setattr(hook.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(hook.time, "sleep", clock.sleep)
    return clock


def _invocation_heartbeat(approval: dict) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "approval_id": approval["id"],
        "source_agent_id": approval["source_agent_id"],
        "source_invocation_id": approval["source_invocation_id"],
        "heartbeat_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=1)).isoformat(),
        "live": True,
    }


def test_invocation_heartbeat_retries_one_transport_failure_within_deadline(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clock = _patch_clock(monkeypatch)
    attempts: list[float] = []
    diagnostics = {
        "correlation_id": "0123456789abcdef",
        "approval_id": "approval_abcdef123456",
    }

    def request(_method: str, _url: str, _agent_id: str, _token: str,
                _body: dict, **kwargs: object) -> dict:
        attempts.append(float(kwargs["timeout_seconds"]))
        if len(attempts) == 1:
            try:
                raise TimeoutError("transient transport interruption")
            except TimeoutError as cause:
                raise hook.AmbiguousRequestError("heartbeat transport failed") from cause
        return {
            "approval_id": "approval-1",
            "source_agent_id": "agent-1",
            "source_invocation_id": "invocation-1",
            "heartbeat_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
            "live": True,
        }

    monkeypatch.setattr(hook, "_request", request)
    next_at, live = hook._refresh_invocation_lease_if_due(
        api_base="https://control.example",
        approval_id="approval-1",
        agent_id="agent-1",
        token="test-token",
        source_invocation_id="invocation-1",
        deadline=clock.now + 2.0,
        next_heartbeat_at=clock.now,
        diagnostics=diagnostics,
    )

    assert live is True
    assert next_at == clock.now + hook.INVOCATION_HEARTBEAT_INTERVAL_SECONDS
    assert len(attempts) == 2
    assert attempts[1] < attempts[0]
    assert clock.sleeps == [hook.INVOCATION_HEARTBEAT_RETRY_DELAY_SECONDS]
    diagnostic = capsys.readouterr().err.strip()
    assert json.loads(diagnostic) == {
        "component": "aixion_approval_hook",
        "provider": "ANTIGRAVITY",
        "event": "invocation_heartbeat_retry",
        "correlation_id": "0123456789abcdef",
        "approval_id": "approval_abcdef123456",
        "attempt": 2,
        "decision": None,
    }
    assert "test-token" not in diagnostic
    assert "invocation-1" not in diagnostic
    assert "approval-1" not in diagnostic


def test_invocation_heartbeat_retries_one_short_rate_limit_within_both_deadlines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _patch_clock(monkeypatch)
    attempts = 0
    approval = {
        "id": "approval-1",
        "source_agent_id": "agent-1",
        "source_invocation_id": "invocation-1",
    }
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
    lease_state: dict[str, float | None] = {
        "deadline": clock.now + 60,
        "approval_deadline": clock.now + 60,
    }

    def request(*_args: object, **_kwargs: object) -> dict:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise hook.RateLimitError(2, retry_after_header="2")
        return _invocation_heartbeat(approval)

    monkeypatch.setattr(hook, "_request", request)
    next_at, live = hook._refresh_invocation_lease_if_due(
        api_base="https://control.example",
        approval_id="approval-1",
        agent_id="agent-1",
        token="test-token",
        source_invocation_id="invocation-1",
        deadline=clock.now + 30,
        next_heartbeat_at=clock.now,
        approval_expires_at=expires_at,
        lease_state=lease_state,
    )

    assert live is True
    assert attempts == 2
    assert clock.sleeps == [2]
    assert next_at == clock.now + hook.INVOCATION_HEARTBEAT_INTERVAL_SECONDS
    assert lease_state["deadline"] is not None


@pytest.mark.parametrize(
    ("delay", "header", "host_budget", "approval_budget", "lease_budget"),
    [
        (2, None, 40, 60, 60),  # missing Retry-After
        (5, "5", 40, 60, 60),  # exceeds the lease-safe retry window
        (2, "2", 13, 60, 60),  # host deadline cannot cover delay plus request
        (2, "2", 40, 13, 60),  # approval expiry cannot cover delay plus request
        (2, "not-a-date", 40, 60, 60),
        (2, "2", 40, 60, 13),  # measured invocation lease cannot cover retry
    ],
)
def test_invocation_heartbeat_fails_closed_on_unsafe_rate_limit_retry(
    monkeypatch: pytest.MonkeyPatch,
    delay: int,
    header: str | None,
    host_budget: int,
    approval_budget: int,
    lease_budget: int,
) -> None:
    clock = _patch_clock(monkeypatch)
    attempts = 0
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=approval_budget)
    ).isoformat()
    lease_state: dict[str, float | None] = {
        "deadline": clock.now + lease_budget,
        "approval_deadline": clock.now + approval_budget,
    }

    def request(*_args: object, **_kwargs: object) -> dict:
        nonlocal attempts
        attempts += 1
        raise hook.RateLimitError(delay, retry_after_header=header)

    monkeypatch.setattr(hook, "_request", request)
    _, live = hook._refresh_invocation_lease_if_due(
        api_base="https://control.example",
        approval_id="approval-1",
        agent_id="agent-1",
        token="test-token",
        source_invocation_id="invocation-1",
        deadline=clock.now + host_budget,
        next_heartbeat_at=clock.now,
        approval_expires_at=expires_at,
        lease_state=lease_state,
    )

    assert live is False
    assert attempts == 1
    assert clock.sleeps == []


def test_invocation_heartbeat_denies_after_repeated_rate_limit_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _patch_clock(monkeypatch)
    attempts = 0
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
    lease_state: dict[str, float | None] = {
        "deadline": clock.now + 60,
        "approval_deadline": clock.now + 60,
    }

    def request(*_args: object, **_kwargs: object) -> dict:
        nonlocal attempts
        attempts += 1
        raise hook.RateLimitError(2, retry_after_header="2")

    monkeypatch.setattr(hook, "_request", request)
    _, live = hook._refresh_invocation_lease_if_due(
        api_base="https://control.example",
        approval_id="approval-1",
        agent_id="agent-1",
        token="test-token",
        source_invocation_id="invocation-1",
        deadline=clock.now + 30,
        next_heartbeat_at=clock.now,
        approval_expires_at=expires_at,
        lease_state=lease_state,
    )

    assert live is False
    assert attempts == 2
    assert clock.sleeps == [2]


def test_invocation_heartbeat_fails_closed_on_rate_limit_without_measured_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _patch_clock(monkeypatch)
    attempts = 0

    def request(*_args: object, **_kwargs: object) -> dict:
        nonlocal attempts
        attempts += 1
        raise hook.RateLimitError(2, retry_after_header="2")

    monkeypatch.setattr(hook, "_request", request)
    _, live = hook._refresh_invocation_lease_if_due(
        api_base="https://control.example",
        approval_id="approval-1",
        agent_id="agent-1",
        token="test-token",
        source_invocation_id="invocation-1",
        deadline=clock.now + 30,
        next_heartbeat_at=clock.now,
        approval_expires_at=(datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
        lease_state={"deadline": None, "approval_deadline": None},
    )

    assert live is False
    assert attempts == 1
    assert clock.sleeps == []


def test_invocation_heartbeat_does_not_retry_when_deadline_cannot_cover_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _patch_clock(monkeypatch)
    attempts = 0

    def request(*_args: object, **_kwargs: object) -> dict:
        nonlocal attempts
        attempts += 1
        try:
            raise OSError("transient transport interruption")
        except OSError as cause:
            raise hook.AmbiguousRequestError("heartbeat transport failed") from cause

    monkeypatch.setattr(hook, "_request", request)
    next_at, live = hook._refresh_invocation_lease_if_due(
        api_base="https://control.example",
        approval_id="approval-1",
        agent_id="agent-1",
        token="test-token",
        source_invocation_id="invocation-1",
        deadline=clock.now + hook.INVOCATION_HEARTBEAT_RETRY_DELAY_SECONDS,
        next_heartbeat_at=clock.now,
    )

    assert live is False
    assert next_at == clock.now
    assert attempts == 1
    assert clock.sleeps == []


def test_invocation_heartbeat_denies_after_transport_retry_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _patch_clock(monkeypatch)
    attempts = 0

    def request(*_args: object, **_kwargs: object) -> dict:
        nonlocal attempts
        attempts += 1
        try:
            raise TimeoutError("transport remains unavailable")
        except TimeoutError as cause:
            raise hook.AmbiguousRequestError("heartbeat transport failed") from cause

    monkeypatch.setattr(hook, "_request", request)
    next_at, live = hook._refresh_invocation_lease_if_due(
        api_base="https://control.example",
        approval_id="approval-1",
        agent_id="agent-1",
        token="test-token",
        source_invocation_id="invocation-1",
        deadline=clock.now + 2.0,
        next_heartbeat_at=clock.now,
    )

    assert live is False
    assert next_at == clock.now - hook.INVOCATION_HEARTBEAT_RETRY_DELAY_SECONDS
    assert attempts == 2
    assert clock.sleeps == [hook.INVOCATION_HEARTBEAT_RETRY_DELAY_SECONDS]


def test_long_wait_refreshes_exact_invocation_lease_before_same_call_is_consumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _patch_clock(monkeypatch)
    monkeypatch.setattr(hook, "INVOCATION_HEARTBEAT_INTERVAL_SECONDS", 1)
    calls: list[tuple[str, str, dict | None]] = []
    created: dict = {}
    decision_reads = 0

    def request(method: str, url: str, agent_id: str, token: str,
                body: dict | None = None, **_kwargs: object) -> dict:
        nonlocal decision_reads
        calls.append((method, url, body))
        assert agent_id == "agent-123"
        assert token == "test-token"
        if method == "POST" and url.endswith("/agents/approvals"):
            assert body is not None
            created.update(_approval_from_request(body))
            return dict(created)
        if method == "GET" and url.endswith("/decision"):
            decision_reads += 1
            if decision_reads == 1:
                return {**created, "status": "PENDING_REVIEW"}
            approved = {**created, "status": "APPROVED"}
            approved["approved_payload_hash"] = hook._payload_hash(approved)
            return approved
        if method == "POST" and url.endswith("/heartbeat"):
            assert body == {"source_invocation_id": created["source_invocation_id"]}
            return _invocation_heartbeat(created)
        if method == "POST" and url.endswith("/consume"):
            return {"consumed": True}
        raise AssertionError(f"Unexpected approval protocol request: {method} {url}")

    monkeypatch.setattr(hook, "_request", request)
    result = hook.run(_hook_payload(), _test_environment())

    assert result == {"decision": "auto_approve", "reason": "Approved from Aixion Control Tower"}
    assert [url.rsplit("/", 1)[-1] for _method, url, _body in calls] == [
        "approvals", "decision", "heartbeat", "decision", "consume"
    ]
    assert clock.sleeps == [hook.POLL_INTERVAL_SECONDS]


@pytest.mark.parametrize(
    "failure",
    [
        "unauthorized", "conflict", "timeout", "mismatch", "not_live",
        "malformed", "malformed_timestamp", "timezone_less_timestamp", "late",
    ],
)
def test_failed_or_late_invocation_heartbeat_denies_without_consuming(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    clock = _patch_clock(monkeypatch)
    monkeypatch.setattr(hook, "INVOCATION_HEARTBEAT_INTERVAL_SECONDS", 1)
    calls: list[str] = []
    created: dict = {}

    def request(method: str, url: str, _agent_id: str, _token: str,
                body: dict | None = None, **kwargs: object) -> dict:
        calls.append(url.rsplit("/", 1)[-1])
        if method == "POST" and url.endswith("/agents/approvals"):
            assert body is not None
            created.update(_approval_from_request(body))
            return dict(created)
        if method == "GET" and url.endswith("/decision"):
            return {**created, "status": "PENDING_REVIEW"}
        if method == "POST" and url.endswith("/heartbeat"):
            assert body == {"source_invocation_id": created["source_invocation_id"]}
            if failure == "unauthorized":
                raise hook.GateError("HTTP 401")
            if failure == "conflict":
                raise hook.GateError("HTTP 409")
            if failure == "timeout":
                raise hook.AmbiguousRequestError("heartbeat timed out")
            if failure == "late":
                clock.now += float(kwargs["timeout_seconds"])
                return _invocation_heartbeat(created)
            response = _invocation_heartbeat(created)
            if failure == "mismatch":
                response["source_invocation_id"] += ":other"
            elif failure == "not_live":
                response["live"] = False
            elif failure == "malformed":
                return []  # type: ignore[return-value]
            elif failure == "malformed_timestamp":
                response["heartbeat_at"] = "not-a-timestamp"
            elif failure == "timezone_less_timestamp":
                response["heartbeat_at"] = "2026-10-25T12:00:00"
            return response
        if method == "POST" and url.endswith("/expire"):
            return {**created, "status": "EXPIRED"}
        raise AssertionError(f"Heartbeat failure must not proceed: {method} {url}")

    monkeypatch.setattr(hook, "_request", request)
    environment = _test_environment()
    environment["AIXION_APPROVAL_TIMEOUT_SECONDS"] = "10"
    result = hook.run(_hook_payload(), environment)

    assert result["decision"] == "deny"
    assert result["reason"] == "Connector liveness could not be confirmed"
    assert calls == ["approvals", "decision", "heartbeat"]
    assert "consume" not in calls


def test_run_reconnects_after_ambiguous_decision_read_then_consumes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _patch_clock(monkeypatch)
    calls: list[str] = []
    created: dict = {}
    decision_reads = 0

    def request(method: str, url: str, _agent_id: str, _token: str, body: dict | None = None, **_kwargs: object) -> dict:
        nonlocal decision_reads
        calls.append(f"{method} {url.rsplit('/', 1)[-1]}")
        if method == "POST" and url.endswith("/agents/approvals"):
            assert body is not None
            created.update(_approval_from_request(body))
            return dict(created)
        if method == "GET" and url.endswith("/decision"):
            decision_reads += 1
            if decision_reads == 1:
                raise hook.AmbiguousRequestError("temporary decision read failure")
            approved = {**created, "status": "APPROVED"}
            approved["approved_payload_hash"] = hook._payload_hash(approved)
            return approved
        if method == "POST" and url.endswith("/consume"):
            return {"consumed": True}
        raise AssertionError(f"Unexpected approval protocol request: {method} {url}")

    monkeypatch.setattr(hook, "_request", request)
    result = hook.run(_hook_payload(), _test_environment())

    assert result["decision"] == "auto_approve"
    assert decision_reads == 2
    assert calls.count("POST consume") == 1
    assert clock.sleeps == [hook.POLL_INTERVAL_SECONDS]


@pytest.mark.parametrize(
    ("consume_response", "expected_decision"),
    [
        ("valid", "auto_approve"),
        ("partial", "deny"),
        ("malformed", "deny"),
        ("reset_after_claim", "deny"),
    ],
)
def test_loopback_decision_socket_close_retries_poll_and_never_retries_consume(
    monkeypatch: pytest.MonkeyPatch,
    consume_response: str,
    expected_decision: str,
) -> None:
    """Exercise real transport failures at decision and consume boundaries."""
    monkeypatch.setattr(hook.time, "sleep", lambda _seconds: None)
    created: dict[str, object] = {}
    decision_reads = 0
    consume_posts = 0
    committed_claims = 0

    class ProtocolHandler(BaseHTTPRequestHandler):
        def _send_json(self, value: dict[str, object]) -> None:
            body = json.dumps(value, separators=(",", ":")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            nonlocal decision_reads
            if self.path == "/health":
                self._send_json({"status": "ok"})
                return
            if self.path.startswith("/agents/identity?"):
                self._send_json({
                    "agent_id": "agent-123",
                    "provider": "ANTIGRAVITY",
                    "project_id": "project-456",
                })
                return
            if self.path.endswith("/decision"):
                decision_reads += 1
                if decision_reads == 1:
                    # Simulate a real peer reset after accepting the GET but
                    # before sending an HTTP status or body.
                    self.connection.setsockopt(
                        socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                    )
                    self.connection.close()
                    return
                approved = {**created, "status": "APPROVED"}
                approved["approved_payload_hash"] = hook._payload_hash(approved)
                self._send_json(approved)
                return
            self.send_error(404)

        def do_POST(self) -> None:
            nonlocal consume_posts, committed_claims
            length = int(self.headers.get("Content-Length", "0"))
            request_body = self.rfile.read(length) if length else b""
            if self.path == "/agents/approvals":
                created.update(_approval_from_request(json.loads(request_body)))
                self._send_json(created)
                return
            if self.path.endswith("/consume"):
                consume_posts += 1
                if consume_response == "reset_after_claim":
                    # Model the durable one-shot claim committing before the
                    # response is lost. The caller must deny and must not
                    # retry this non-idempotent POST.
                    committed_claims += 1
                    self.connection.setsockopt(
                        socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                    )
                    self.connection.close()
                    return
                if consume_response == "valid":
                    self._send_json({"consumed": True})
                elif consume_response == "partial":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "24")
                    self.end_headers()
                    self.wfile.write(b'{"consumed":')
                    self.wfile.flush()
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                else:
                    body = b'{"consumed":true,}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                return
            if self.path.endswith("/delivery-failure"):
                self._send_json({"recorded": True})
                return
            self.send_error(404)

        def log_message(self, *_args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), ProtocolHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    environment = _test_environment()
    environment["AIXION_API_BASE_URL"] = f"http://127.0.0.1:{server.server_port}"
    try:
        result = hook.run(_hook_payload(), environment)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

    assert result["decision"] == expected_decision
    assert decision_reads == 2
    assert consume_posts == 1
    assert committed_claims == (1 if consume_response == "reset_after_claim" else 0)


def test_run_honors_retry_after_only_until_host_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _patch_clock(monkeypatch)
    created: dict = {}
    calls: list[str] = []
    environment = _test_environment()
    environment["AIXION_APPROVAL_TIMEOUT_SECONDS"] = "3"

    def request(method: str, url: str, _agent_id: str, _token: str, body: dict | None = None, **_kwargs: object) -> dict:
        calls.append(f"{method} {url.rsplit('/', 1)[-1]}")
        if method == "POST" and url.endswith("/agents/approvals"):
            assert body is not None
            created.update(_approval_from_request(body))
            return dict(created)
        if method == "GET" and url.endswith("/decision"):
            raise hook.RateLimitError(9)
        if method == "POST" and url.endswith("/expire"):
            return {**created, "status": "EXPIRED"}
        raise AssertionError(f"Unexpected approval protocol request: {method} {url}")

    monkeypatch.setattr(hook, "_request", request)
    result = hook.run(_hook_payload(), environment)

    assert result["decision"] == "deny"
    assert clock.sleeps == [3]
    assert clock.now == 103
    assert "POST expire" in calls
    assert "POST consume" not in calls


def test_long_retry_after_renews_invocation_lease_before_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _patch_clock(monkeypatch)
    created: dict = {}
    calls: list[tuple[str, float]] = []
    environment = _test_environment()
    environment["AIXION_APPROVAL_TIMEOUT_SECONDS"] = "120"

    def request(method: str, url: str, _agent_id: str, _token: str, body: dict | None = None, **_kwargs: object) -> dict:
        calls.append((url.rsplit("/", 1)[-1], clock.now))
        if method == "POST" and url.endswith("/agents/approvals"):
            assert body is not None
            created.update(_approval_from_request(body))
            return dict(created)
        if method == "GET" and url.endswith("/decision"):
            if clock.now < 190:
                raise hook.RateLimitError(90)
            approved = {**created, "status": "APPROVED"}
            approved["approved_payload_hash"] = hook._payload_hash(approved)
            return approved
        if method == "POST" and url.endswith("/heartbeat"):
            return _invocation_heartbeat(created)
        if method == "POST" and url.endswith("/consume"):
            return {"consumed": True}
        raise AssertionError(f"Unexpected approval protocol request: {method} {url}")

    monkeypatch.setattr(hook, "_request", request)
    result = hook.run(_hook_payload(), environment)

    assert result["decision"] == "auto_approve"
    heartbeat_times = [at for endpoint, at in calls if endpoint == "heartbeat"]
    assert heartbeat_times
    assert all(
        at - previous <= hook.INVOCATION_HEARTBEAT_INTERVAL_SECONDS
        for previous, at in zip([100.0, *heartbeat_times], heartbeat_times)
    )
    first_heartbeat = calls.index(("heartbeat", heartbeat_times[0]))
    assert calls[first_heartbeat + 1][0] == "decision"
    assert clock.now == 190
    assert "consume" in [endpoint for endpoint, _ in calls]


@pytest.mark.parametrize("heartbeat_failure", ["not_live", "rate_limited"])
def test_long_retry_after_with_failed_heartbeat_denies_without_consuming(
    monkeypatch: pytest.MonkeyPatch,
    heartbeat_failure: str,
) -> None:
    clock = _patch_clock(monkeypatch)
    created: dict = {}
    calls: list[str] = []
    environment = _test_environment()
    environment["AIXION_APPROVAL_TIMEOUT_SECONDS"] = "120"

    def request(method: str, url: str, _agent_id: str, _token: str, body: dict | None = None, **_kwargs: object) -> dict:
        endpoint = url.rsplit("/", 1)[-1]
        calls.append(endpoint)
        if method == "POST" and url.endswith("/agents/approvals"):
            assert body is not None
            created.update(_approval_from_request(body))
            return dict(created)
        if method == "GET" and url.endswith("/decision"):
            raise hook.RateLimitError(90)
        if method == "POST" and url.endswith("/heartbeat"):
            if heartbeat_failure == "rate_limited":
                raise hook.RateLimitError(90)
            return {**_invocation_heartbeat(created), "live": False}
        raise AssertionError(f"Failed liveness must stop the approval wait: {method} {url}")

    monkeypatch.setattr(hook, "_request", request)
    result = hook.run(_hook_payload(), environment)

    assert result["decision"] == "deny"
    assert result["reason"] == "Connector liveness could not be confirmed"
    assert calls == ["approvals", "decision", "heartbeat"]
    assert clock.sleeps == [hook.INVOCATION_HEARTBEAT_INTERVAL_SECONDS]
    assert "consume" not in calls


def test_pending_poll_sleep_does_not_extend_configured_wait_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _patch_clock(monkeypatch)
    created: dict = {}
    decision_reads = 0
    calls: list[tuple[str, str, float | None]] = []
    environment = _test_environment()
    environment["AIXION_APPROVAL_TIMEOUT_SECONDS"] = "3"

    def request(method: str, url: str, _agent_id: str, _token: str, body: dict | None = None, **kwargs: object) -> dict:
        nonlocal decision_reads
        timeout = kwargs.get("timeout_seconds")
        calls.append((method, url.rsplit("/", 1)[-1], timeout if isinstance(timeout, float) else None))
        if method == "POST" and url.endswith("/agents/approvals"):
            assert body is not None
            created.update(_approval_from_request(body))
            return dict(created)
        if method == "GET" and url.endswith("/decision"):
            decision_reads += 1
            return {**created, "status": "PENDING_REVIEW"}
        if method == "POST" and url.endswith("/expire"):
            return {**created, "status": "EXPIRED"}
        raise AssertionError(f"Unexpected approval protocol request: {method} {url}")

    monkeypatch.setattr(hook, "_request", request)
    result = hook.run(_hook_payload(), environment)

    assert result["decision"] == "deny"
    assert decision_reads == 2
    assert clock.sleeps == [hook.POLL_INTERVAL_SECONDS, 1]
    assert clock.now == 103
    assert calls[1:] == [
        ("GET", "decision", 3.0),
        ("GET", "decision", 1.0),
        ("POST", "expire", 1.0),
    ]
    assert not any(method == "POST" and suffix == "consume" for method, suffix, _ in calls)


def test_slow_heartbeat_reduces_next_decision_request_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Heartbeat latency must not be added on top of a stale poll timeout."""
    clock = _patch_clock(monkeypatch)
    monkeypatch.setattr(hook, "INVOCATION_HEARTBEAT_INTERVAL_SECONDS", 1)
    created: dict = {}
    decision_timeouts: list[float] = []
    endpoints: list[str] = []
    decision_reads = 0
    environment = _test_environment()
    environment["AIXION_APPROVAL_TIMEOUT_SECONDS"] = "3"

    def request(
        method: str,
        url: str,
        _agent_id: str,
        _token: str,
        body: dict | None = None,
        **kwargs: object,
    ) -> dict:
        nonlocal decision_reads
        endpoints.append(url.rsplit("/", 1)[-1])
        if method == "POST" and url.endswith("/agents/approvals"):
            assert body is not None
            created.update(_approval_from_request(body))
            return dict(created)
        if method == "GET" and url.endswith("/decision"):
            decision_reads += 1
            timeout = kwargs.get("timeout_seconds")
            assert isinstance(timeout, float)
            decision_timeouts.append(timeout)
            if decision_reads == 1:
                return {**created, "status": "PENDING_REVIEW"}
            clock.now += timeout
            return {**created, "status": "PENDING_REVIEW"}
        if method == "POST" and url.endswith("/heartbeat"):
            timeout = kwargs.get("timeout_seconds")
            assert isinstance(timeout, float)
            # The successful heartbeat uses 0.75 of the remaining second.
            clock.now += 0.75
            return _invocation_heartbeat(created)
        if method == "POST" and url.endswith("/expire"):
            return {**created, "status": "EXPIRED"}
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr(hook, "_request", request)
    result = hook.run(_hook_payload(), environment)

    assert result["decision"] == "deny"
    assert decision_reads == 2
    assert decision_timeouts[0] == 3.0
    assert decision_timeouts[1] == pytest.approx(0.25)
    assert clock.now == pytest.approx(103.0)
    assert "consume" not in endpoints


def test_request_uses_the_supplied_transport_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[tuple[object, ...]] = []

    def deadline_request(*args: object, **kwargs: object) -> SimpleNamespace:
        observed.append((*args, kwargs.get("max_response_bytes")))
        return SimpleNamespace(status=200, headers={}, body=b"{}")

    monkeypatch.setattr(hook, "deadline_request", deadline_request)

    assert hook._request(
        "GET", "https://control.example/decision", "agent", "token", timeout_seconds=0.25
    ) == {}
    assert observed == [(
        "GET",
        "https://control.example/decision",
        {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Aixion-Agent-Id": "agent",
            "X-Aixion-Agent-Token": "token",
            "X-Aixion-Approval-Protocol": hook.APPROVAL_PROTOCOL_VERSION,
        },
        None,
        0.25,
        hook.MAX_RESPONSE_BYTES,
    )]

    with pytest.raises(hook.GateError, match="outside the allowed range"):
        hook._request(
            "GET", "https://control.example/decision", "agent", "token", timeout_seconds=10.01
        )


@pytest.mark.parametrize("status", [201, 204])
def test_heartbeat_request_requires_exact_http_200(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    monkeypatch.setattr(
        hook,
        "deadline_request",
        lambda *_args, **_kwargs: SimpleNamespace(status=status, headers={}, body=b"{}"),
    )

    with pytest.raises(hook.GateError, match=f"HTTP {status}"):
        hook._request(
            "POST",
            "https://control.example/agents/approvals/id/heartbeat",
            "agent-123",
            "test-token",
            {"source_invocation_id": "antigravity:v1:test"},
            expected_status=200,
        )


def test_rate_limit_error_preserves_retry_after_for_bounded_heartbeat_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        hook,
        "deadline_request",
        lambda *_args, **_kwargs: SimpleNamespace(
            status=429, headers={"Retry-After": "2"}, body=b"{}"
        ),
    )

    with pytest.raises(hook.RateLimitError) as raised:
        hook._request(
            "POST",
            "https://control.example/agents/approvals/id/heartbeat",
            "agent-123",
            "test-token",
            {"source_invocation_id": "antigravity:v1:test"},
            expected_status=200,
        )

    assert raised.value.retry_after_seconds == 2
    assert raised.value.retry_after_header == "2"


@pytest.mark.parametrize("failure", [TimeoutError("private timeout detail"), OSError("private socket detail")])
def test_deadline_transport_errors_are_sanitized_and_ambiguous(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(hook, "deadline_request", fail)
    with pytest.raises(hook.AmbiguousRequestError, match="outcome is unknown") as raised:
        hook._request("POST", "https://control.example/consume", "agent", "secret")
    assert "private" not in str(raised.value)


@pytest.mark.parametrize(
    ("status", "headers", "error", "message"),
    [
        (429, {"retry-after": "7"}, hook.RateLimitError, "rate limit"),
        (503, {}, hook.AmbiguousRequestError, "ambiguous server failure"),
        (302, {"location": "https://attacker.example/"}, hook.GateError, "HTTP 302"),
        (400, {}, hook.GateError, "HTTP 400"),
    ],
)
def test_deadline_transport_status_mapping(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    headers: dict[str, str],
    error: type[Exception],
    message: str,
) -> None:
    monkeypatch.setattr(
        hook,
        "deadline_request",
        lambda *_args, **_kwargs: SimpleNamespace(status=status, headers=headers, body=b"{}"),
    )
    with pytest.raises(error, match=message):
        hook._request("GET", "https://control.example/decision", "agent", "secret")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("7", 7),
        ("0", 0),
        ("9" * 100, hook.MAX_POLL_SECONDS),
        ("-1", hook.POLL_INTERVAL_SECONDS),
        ("+7", hook.POLL_INTERVAL_SECONDS),
        (" 7", hook.POLL_INTERVAL_SECONDS),
        ("7 ", hook.POLL_INTERVAL_SECONDS),
        ("1_0", hook.POLL_INTERVAL_SECONDS),
        ("+1", hook.POLL_INTERVAL_SECONDS),
        ("١", hook.POLL_INTERVAL_SECONDS),
        ("not a date", hook.POLL_INTERVAL_SECONDS),
        (None, hook.POLL_INTERVAL_SECONDS),
    ],
)
def test_retry_after_delta_and_invalid_values(value: str | None, expected: int) -> None:
    assert hook._parse_retry_after(value) == expected


def test_retry_after_http_date_uses_injected_utc_clock() -> None:
    now = datetime(2026, 10, 23, 12, 0, 0, tzinfo=timezone.utc)
    value = format_datetime(now + timedelta(seconds=37), usegmt=True)
    offset = format_datetime(now + timedelta(seconds=37), usegmt=False)
    past = format_datetime(now - timedelta(seconds=5), usegmt=True)
    assert hook._parse_retry_after(value, now=now) == 37
    assert hook._parse_retry_after(offset, now=now) == 37
    assert hook._parse_retry_after(past, now=now) == 0
    subsecond_now = now + timedelta(microseconds=500_000)
    next_second = format_datetime(now + timedelta(seconds=1), usegmt=True)
    assert hook._parse_retry_after(next_second, now=subsecond_now) == 1
    assert (
        hook._parse_retry_after("Fri, 31 Feb 2026 12:00:00 GMT", now=now)
        == hook.POLL_INTERVAL_SECONDS
    )
    assert (
        hook._parse_retry_after("Fri, 23 Oct 2026 12:01:00", now=now)
        == hook.POLL_INTERVAL_SECONDS
    )


def _prepare_connection_check(monkeypatch: pytest.MonkeyPatch, api_base: str) -> None:
    config = {"api_base_url": api_base, "agent_id": "agent", "project_id": "project"}
    monkeypatch.setattr(hook, "_configuration", lambda _env, _payload: config)
    monkeypatch.setattr(hook, "_agent_token", lambda _env, _config: "secret")
    monkeypatch.setattr(
        hook,
        "_request",
        lambda *_args, **_kwargs: {
            "agent_id": "agent",
            "provider": "ANTIGRAVITY",
            "project_id": "project",
        },
    )


def test_check_connection_uses_deadline_health_transport_and_response_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_connection_check(monkeypatch, "http://127.0.0.1:8000")
    observed: list[tuple[object, ...]] = []

    def deadline_request(*args: object, **kwargs: object) -> SimpleNamespace:
        observed.append((*args, kwargs.get("max_response_bytes")))
        return SimpleNamespace(status=200, headers={}, body=b'{"status":"ok"}')

    monkeypatch.setattr(hook, "deadline_request", deadline_request)
    assert hook.check_connection({})["agent_id"] == "agent"
    assert observed == [(
        "GET",
        "http://127.0.0.1:8000/health",
        {"Accept": "application/json"},
        None,
        5,
        hook.MAX_HEALTH_RESPONSE_BYTES,
    )]


@pytest.mark.parametrize(
    ("response", "error", "message"),
    [
        (SimpleNamespace(status=302, headers={}, body=b"{}"), hook.GateError, "HTTP 302"),
        (SimpleNamespace(status=503, headers={}, body=b"{}"), hook.GateError, "HTTP 503"),
        (SimpleNamespace(status=200, headers={}, body=b'{"status":"degraded"}'), hook.GateError, "did not return status ok"),
    ],
)
def test_check_connection_rejects_unhealthy_or_non_success_responses(
    monkeypatch: pytest.MonkeyPatch,
    response: SimpleNamespace,
    error: type[Exception],
    message: str,
) -> None:
    _prepare_connection_check(monkeypatch, "http://127.0.0.1:8000")
    monkeypatch.setattr(hook, "deadline_request", lambda *_args, **_kwargs: response)
    with pytest.raises(error, match=message):
        hook.check_connection({})


@pytest.mark.parametrize("failure", [TimeoutError("private timeout"), OSError("private socket error")])
def test_check_connection_sanitizes_health_transport_failures(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    _prepare_connection_check(monkeypatch, "http://127.0.0.1:8000")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(hook, "deadline_request", fail)
    with pytest.raises(hook.GateError, match="health check failed") as raised:
        hook.check_connection({})
    assert "private" not in str(raised.value)


def test_check_connection_loopback_redirect_is_not_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[str] = []

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/redirected")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _prepare_connection_check(monkeypatch, f"http://127.0.0.1:{server.server_port}")
    try:
        with pytest.raises(hook.GateError, match="HTTP 302"):
            hook.check_connection({})
        assert requests == ["/health"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


@pytest.mark.parametrize("slow_stage", ["headers", "body"])
def test_loopback_slow_response_obeys_absolute_transport_deadline(slow_stage: str) -> None:
    class SlowHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if slow_stage == "headers":
                time.sleep(0.2)
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
                return
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.flush()
            time.sleep(0.2)
            try:
                self.wfile.write(b"{}")
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        with pytest.raises(hook.AmbiguousRequestError, match="outcome is unknown"):
            hook._request(
                "GET",
                f"http://127.0.0.1:{server.server_port}/decision",
                "agent",
                "secret",
                timeout_seconds=0.05,
            )
        assert time.monotonic() - started < 0.18
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_loopback_redirect_is_not_followed() -> None:
    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1/should-not-follow")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(hook.GateError, match="HTTP 302"):
            hook._request(
                "GET",
                f"http://127.0.0.1:{server.server_port}/decision",
                "agent",
                "secret",
                timeout_seconds=1,
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_slow_poll_and_expiry_cleanup_stay_within_deadline_plus_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _patch_clock(monkeypatch)
    created: dict = {}
    timeouts: list[tuple[str, float]] = []
    environment = _test_environment()
    environment["AIXION_APPROVAL_TIMEOUT_SECONDS"] = "3"

    def request(method: str, url: str, _agent_id: str, _token: str, body: dict | None = None, **kwargs: object) -> dict:
        if method == "POST" and url.endswith("/agents/approvals"):
            assert body is not None
            created.update(_approval_from_request(body))
            return dict(created)
        timeout = kwargs.get("timeout_seconds")
        assert isinstance(timeout, float)
        timeouts.append((url.rsplit("/", 1)[-1], timeout))
        if method == "GET" and url.endswith("/decision"):
            clock.now += timeout
            raise hook.AmbiguousRequestError("read timed out")
        if method == "POST" and url.endswith("/expire"):
            clock.now += timeout
            return {**created, "status": "EXPIRED"}
        raise AssertionError(f"Unexpected approval protocol request: {method} {url}")

    monkeypatch.setattr(hook, "_request", request)
    result = hook.run(_hook_payload(), environment)

    assert result["decision"] == "deny"
    assert timeouts == [("decision", 3.0), ("expire", 1.0)]
    assert clock.now - 100.0 == 3 + hook.EXPIRY_CLEANUP_GRACE_SECONDS
    assert clock.now <= 100.0 + 3 + hook.EXPIRY_CLEANUP_GRACE_SECONDS


def test_late_consume_response_cannot_allow_after_host_wait_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _patch_clock(monkeypatch)
    created: dict = {}
    consume_timeouts: list[float] = []
    environment = _test_environment()
    environment["AIXION_APPROVAL_TIMEOUT_SECONDS"] = "3"

    def request(method: str, url: str, _agent_id: str, _token: str, body: dict | None = None, **kwargs: object) -> dict:
        if method == "POST" and url.endswith("/agents/approvals"):
            assert body is not None
            created.update(_approval_from_request(body))
            return dict(created)
        if method == "GET" and url.endswith("/decision"):
            clock.now += 2.5
            approved = {**created, "status": "APPROVED"}
            approved["approved_payload_hash"] = hook._payload_hash(approved)
            return approved
        if method == "POST" and url.endswith("/consume"):
            timeout = kwargs.get("timeout_seconds")
            assert isinstance(timeout, float)
            consume_timeouts.append(timeout)
            # Simulate the HTTP call returning after its socket operation timeout.
            clock.now += timeout + 0.1
            return {"consumed": True}
        raise AssertionError(f"Unexpected approval protocol request: {method} {url}")

    monkeypatch.setattr(hook, "_request", request)
    result = hook.run(_hook_payload(), environment)

    assert result["decision"] == "deny"
    assert "after the host wait deadline" in result["reason"]
    assert consume_timeouts == [pytest.approx(0.5)]
    assert clock.now > 103


def test_approval_read_returning_after_host_deadline_denies_without_consuming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late successful decision read must not authorize the expired host call."""
    clock = _patch_clock(monkeypatch)
    created: dict = {}
    calls: list[str] = []
    environment = _test_environment()
    environment["AIXION_APPROVAL_TIMEOUT_SECONDS"] = "3"

    def request(method: str, url: str, _agent_id: str, _token: str, body: dict | None = None, **_kwargs: object) -> dict:
        endpoint = url.rsplit("/", 1)[-1]
        calls.append(f"{method} {endpoint}")
        if method == "POST" and url.endswith("/agents/approvals"):
            assert body is not None
            created.update(_approval_from_request(body))
            return dict(created)
        if method == "GET" and url.endswith("/decision"):
            # The request starts before the host deadline but the approval
            # arrives only after it; this must not proceed to the one-shot claim.
            clock.now = 103.1
            approved = {**created, "status": "APPROVED"}
            approved["approved_payload_hash"] = hook._payload_hash(approved)
            return approved
        raise AssertionError(f"Late approval must not trigger another request: {method} {url}")

    monkeypatch.setattr(hook, "_request", request)
    result = hook.run(_hook_payload(), environment)

    assert result["decision"] == "deny"
    assert "after the host wait deadline" in result["reason"]
    assert calls == ["POST approvals", "GET decision"]
    assert clock.now > 103


@pytest.mark.parametrize(
    ("consume_finished_at", "expected_report_timeout"),
    [(103.5, 0.5), (104.1, None)],
)
def test_ambiguous_consume_report_uses_only_remaining_cleanup_grace(
    monkeypatch: pytest.MonkeyPatch,
    consume_finished_at: float,
    expected_report_timeout: float | None,
) -> None:
    clock = _patch_clock(monkeypatch)
    created: dict = {}
    request_timeouts: list[tuple[str, float]] = []
    environment = _test_environment()
    environment["AIXION_APPROVAL_TIMEOUT_SECONDS"] = "3"

    def request(method: str, url: str, _agent_id: str, _token: str, body: dict | None = None, **kwargs: object) -> dict:
        if method == "POST" and url.endswith("/agents/approvals"):
            assert body is not None
            created.update(_approval_from_request(body))
            return dict(created)
        if method == "GET" and url.endswith("/decision"):
            approved = {**created, "status": "APPROVED"}
            approved["approved_payload_hash"] = hook._payload_hash(approved)
            return approved
        timeout = kwargs.get("timeout_seconds")
        assert isinstance(timeout, float)
        request_timeouts.append((url.rsplit("/", 1)[-1], timeout))
        if method == "POST" and url.endswith("/consume"):
            clock.now = consume_finished_at
            raise hook.AmbiguousRequestError("claim response was lost")
        if method == "POST" and url.endswith("/delivery-failure"):
            return {"reported": True}
        raise AssertionError(f"Unexpected approval protocol request: {method} {url}")

    monkeypatch.setattr(hook, "_request", request)
    result = hook.run(_hook_payload(), environment)

    assert result["decision"] == "deny"
    assert "outcome was ambiguous" in result["reason"]
    assert request_timeouts[0] == ("consume", 3.0)
    if expected_report_timeout is None:
        assert request_timeouts == [("consume", 3.0)]
    else:
        assert request_timeouts == [
            ("consume", 3.0),
            ("delivery-failure", expected_report_timeout),
        ]


@pytest.mark.parametrize(
    "report_error",
    [hook.GateError("report endpoint unavailable"), hook.AmbiguousRequestError("report response lost")],
)
def test_failed_ambiguous_delivery_report_never_retries_claim_or_allows(
    monkeypatch: pytest.MonkeyPatch, report_error: Exception
) -> None:
    """Failure to persist cleanup evidence must not turn an unknown claim into allow."""
    _patch_clock(monkeypatch)
    created: dict = {}
    calls: list[str] = []
    environment = _test_environment()
    environment["AIXION_APPROVAL_TIMEOUT_SECONDS"] = "3"

    def request(
        method: str,
        url: str,
        _agent_id: str,
        _token: str,
        body: dict | None = None,
        **_kwargs: object,
    ) -> dict:
        endpoint = url.rsplit("/", 1)[-1]
        calls.append(f"{method} {endpoint}")
        if method == "POST" and endpoint == "approvals":
            assert body is not None
            created.update(_approval_from_request(body))
            return dict(created)
        if method == "GET" and endpoint == "decision":
            approved = {**created, "status": "APPROVED"}
            approved["approved_payload_hash"] = hook._payload_hash(approved)
            return approved
        if method == "POST" and endpoint == "consume":
            # The server may already have committed the at-most-once claim.
            raise hook.AmbiguousRequestError("claim response lost")
        if method == "POST" and endpoint == "delivery-failure":
            raise report_error
        raise AssertionError(f"Unexpected approval protocol request: {method} {url}")

    monkeypatch.setattr(hook, "_request", request)
    result = hook.run(_hook_payload(), environment)

    assert result["decision"] == "deny"
    assert "ambiguous" in result["reason"]
    assert calls.count("POST consume") == 1
    assert calls.count("POST delivery-failure") == 1


def test_run_exhausted_reconnect_wait_expires_and_never_consumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _patch_clock(monkeypatch)
    created: dict = {}
    calls: list[str] = []
    environment = _test_environment()
    environment["AIXION_APPROVAL_TIMEOUT_SECONDS"] = "3"

    def request(method: str, url: str, _agent_id: str, _token: str, body: dict | None = None, **_kwargs: object) -> dict:
        calls.append(f"{method} {url.rsplit('/', 1)[-1]}")
        if method == "POST" and url.endswith("/agents/approvals"):
            assert body is not None
            created.update(_approval_from_request(body))
            return dict(created)
        if method == "GET" and url.endswith("/decision"):
            raise hook.AmbiguousRequestError("decision service disconnected")
        if method == "POST" and url.endswith("/expire"):
            return {**created, "status": "EXPIRED"}
        raise AssertionError(f"Unexpected approval protocol request: {method} {url}")

    monkeypatch.setattr(hook, "_request", request)
    result = hook.run(_hook_payload(), environment)

    assert result["decision"] == "deny"
    assert clock.sleeps == [hook.POLL_INTERVAL_SECONDS, 1]
    assert clock.now == 103
    assert calls.count("POST expire") == 1
    assert "POST consume" not in calls
