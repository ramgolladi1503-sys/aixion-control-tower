from __future__ import annotations

import importlib.util
import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

HOOK_PATH = Path(__file__).with_name("aixion_permission_request_hook.py")
SPEC = importlib.util.spec_from_file_location("aixion_codex_cli_hook", HOOK_PATH)
assert SPEC and SPEC.loader
hook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hook)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("7", 7), ("not a date", hook.POLL_INTERVAL_SECONDS), (None, hook.POLL_INTERVAL_SECONDS)],
)
def test_retry_after_delta_and_invalid_values(value: str | None, expected: int) -> None:
    assert hook._parse_retry_after(value) == expected


def test_retry_after_http_date_uses_injected_utc_clock() -> None:
    now = datetime(2026, 10, 23, 12, 0, 0, tzinfo=timezone.utc)
    value = format_datetime(now + timedelta(seconds=37), usegmt=True)
    past = format_datetime(now - timedelta(seconds=5), usegmt=True)
    assert hook._parse_retry_after(value, now=now) == 37
    assert hook._parse_retry_after(past, now=now) == 0


@pytest.mark.parametrize(
    ("behavior", "message"),
    [("allow", "approved"), ("deny", "rejected")],
)
def test_main_reads_stdin_and_emits_codex_permission_decision(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    behavior: str,
    message: str,
) -> None:
    event = {
        "hook_event_name": "PermissionRequest",
        "tool_name": "shell",
        "tool_input": {"command": "echo safe"},
    }
    monkeypatch.setattr(
        hook.sys,
        "stdin",
        SimpleNamespace(buffer=io.BytesIO(json.dumps(event).encode("utf-8"))),
    )
    monkeypatch.setattr(
        hook,
        "run",
        lambda received: (
            (behavior, message)
            if received["tool_name"] == "shell"
            else ("deny", "unexpected input")
        ),
    )

    assert hook.main([]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": (
                {"behavior": "allow"}
                if behavior == "allow"
                else {"behavior": "deny", "message": message}
            ),
        }
    }
    assert captured.out.endswith("\n")


@pytest.mark.parametrize("raw_input", [b"{", b""])
def test_main_emits_codex_deny_for_malformed_or_missing_input(
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
    assert json.loads(captured.out) == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": "deny",
                "message": "Aixion approval gate failed: GateError",
            },
        }
    }
    assert captured.out.endswith("\n")


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_main_denies_nonstandard_json_numbers(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    constant: str,
) -> None:
    raw_input = (
        '{"hook_event_name":"PermissionRequest","session_id":"s1",'
        '"turn_id":"t1","tool_name":"shell","tool_input":{"value":'
        + constant
        + "}}"
    ).encode("utf-8")
    monkeypatch.setattr(hook.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw_input)))

    assert hook.main([]) == 0

    captured = capsys.readouterr()
    diagnostic = json.loads(captured.err)
    assert diagnostic["event"] == "terminal_decision"
    assert diagnostic["correlation_id"] is None
    assert diagnostic["approval_id"] is None
    assert diagnostic["decision"] == "deny"
    assert json.loads(captured.out) == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": "deny",
                "message": "Aixion approval gate failed: GateError",
            },
        }
    }


def test_invocation_identity_is_stable_when_codex_provides_tool_use_id() -> None:
    event = {"session_id": "session-1", "turn_id": "turn-2", "tool_use_id": "call-3"}

    first = hook._invocation_identity(event, tool_name="shell", tool_input={"command": "pwd"})
    second = hook._invocation_identity(event, tool_name="shell", tool_input={"command": "pwd"})

    assert first is not None
    assert first == second


def test_invocation_identity_is_callback_scoped_when_tool_use_id_is_absent() -> None:
    event = {"session_id": "session-1", "turn_id": "turn-2"}

    first = hook._invocation_identity(event, tool_name="shell", tool_input={"command": "pwd"})
    second = hook._invocation_identity(event, tool_name="shell", tool_input={"command": "pwd"})

    assert first is not None
    assert second is not None
    assert first[0] == second[0]
    assert first[1] != second[1]


def test_invocation_identity_rejects_malformed_present_tool_use_id() -> None:
    event = {"session_id": "session-1", "turn_id": "turn-2", "tool_use_id": " "}

    assert hook._invocation_identity(event, tool_name="shell", tool_input={"command": "pwd"}) is None


def _codex_event() -> dict:
    return {
        "hook_event_name": "PermissionRequest",
        "session_id": "session-1",
        "turn_id": "turn-2",
        "tool_use_id": "call-3",
        "tool_name": "shell",
        "tool_input": {"command": "echo safe"},
    }


def _codex_environment() -> dict[str, str]:
    return {
        "AIXION_API_BASE_URL": "http://127.0.0.1:8001",
        "AIXION_AGENT_ID": "agent-123",
        "AIXION_PROJECT_ID": "project-456",
        "AIXION_AGENT_TOKEN": "test-token",
        "AIXION_ALLOW_AGENT_TOKEN_ENV": "true",
        "AIXION_CODEX_APPROVAL_TIMEOUT_SECONDS": "30",
    }


def _codex_approval(body: dict) -> dict:
    return {
        "id": "approval_0123456789ab",
        "project_id": body["project_id"],
        "status": "PENDING_REVIEW",
        "title": body["title"],
        "summary": body["summary"],
        "agent_name": body["agent_name"],
        "target_branch": body["target_branch"],
        "files": body["files"],
        "test_plan": body["test_plan"],
        "rollback_plan": body["rollback_plan"],
        "source_provider": "CODEX",
        "source_agent_id": "agent-123",
        "source_agent_name": "Codex test agent",
        "source_session_id": body["source_session_id"],
        "source_invocation_id": body["source_invocation_id"],
        "source_task_url": None,
    }


def test_diagnostic_approval_id_requires_backend_id_format() -> None:
    assert hook._validated_diagnostic_approval_id("approval_0123456789ab") == "approval_0123456789ab"
    assert hook._validated_diagnostic_approval_id("diagnostic_token_canary") is None


def test_run_recovers_lost_create_response_with_identical_invocation_retry(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[str, str, dict | None]] = []
    persisted: dict = {}

    def request(method: str, url: str, _config: dict, _token: str, body: dict | None = None, **_kwargs) -> dict:
        calls.append((method, url, body))
        if method == "POST" and url.endswith("/agents/approvals"):
            assert body is not None
            if not persisted:
                persisted.update(_codex_approval(body))
                raise hook.AmbiguousRequestError("server committed but response was lost")
            assert body == calls[0][2]
            return dict(persisted)
        if method == "GET" and url.endswith("/decision"):
            approved = {**persisted, "status": "APPROVED"}
            approved["approved_payload_hash"] = hook._hash(approved)
            return approved
        if method == "POST" and url.endswith("/consume"):
            return {"consumed": True}
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr(hook, "_request", request)
    monkeypatch.setattr(hook.time, "sleep", lambda _seconds: None)

    event = _codex_event()
    event["tool_input"]["command"] = "diagnostic_action_canary"
    environment = _codex_environment()
    environment["AIXION_AGENT_TOKEN"] = "diagnostic_token_canary"
    result = hook.run(event, environment)

    assert result == ("allow", "Approved from Aixion Control Tower")
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
    assert terminal["decision"] == "allow"
    assert "diagnostic_token_canary" not in captured.err
    assert "diagnostic_action_canary" not in captured.err


def test_run_denies_after_bounded_ambiguous_create_retries(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[dict] = []

    def request(method: str, url: str, _config: dict, _token: str, body: dict | None = None, **_kwargs) -> dict:
        assert method == "POST" and url.endswith("/agents/approvals")
        assert body is not None
        calls.append(body.copy())
        raise hook.AmbiguousRequestError("response remained unavailable")

    monkeypatch.setattr(hook, "_request", request)
    monkeypatch.setattr(hook.time, "sleep", lambda _seconds: None)

    result = hook.run(_codex_event(), _codex_environment())

    assert result[0] == "deny"
    assert "remained ambiguous" in result[1]
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

    def request(method: str, url: str, _config: dict, _token: str, body: dict | None = None, **_kwargs) -> dict:
        nonlocal calls
        calls += 1
        assert method == "POST" and url.endswith("/agents/approvals")
        assert body is not None
        if failure == "conflict":
            raise hook.GateError("HTTP 409")
        mismatched = _codex_approval(body)
        mismatched["source_invocation_id"] += ":different"
        return mismatched

    monkeypatch.setattr(hook, "_request", request)

    result = hook.run(_codex_event(), _codex_environment())

    assert result[0] == "deny"
    assert calls == 1


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
    return {
        "approval_id": approval["id"],
        "source_agent_id": approval["source_agent_id"],
        "source_invocation_id": approval["source_invocation_id"],
        "heartbeat_at": "2026-10-22T00:00:00+00:00",
        "expires_at": "2026-10-22T00:01:00+00:00",
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

    def request(_method: str, _url: str, _config: dict, _token: str,
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
            "heartbeat_at": "2026-10-23T00:00:00+00:00",
            "expires_at": "2026-10-23T00:01:00+00:00",
            "live": True,
        }

    monkeypatch.setattr(hook, "_request", request)
    next_at, live = hook._refresh_invocation_lease_if_due(
        api_base="https://control.example",
        approval_id="approval-1",
        config={"agent_id": "agent-1"},
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
        "provider": "CODEX",
        "event": "invocation_heartbeat_retry",
        "correlation_id": "0123456789abcdef",
        "approval_id": "approval_abcdef123456",
        "attempt": 2,
        "decision": None,
    }
    assert "test-token" not in diagnostic
    assert "invocation-1" not in diagnostic
    assert "approval-1" not in diagnostic


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
        config={"agent_id": "agent-1"},
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
        config={"agent_id": "agent-1"},
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

    def request(method: str, url: str, config: dict, token: str,
                body: dict | None = None, **_kwargs: object) -> dict:
        nonlocal decision_reads
        calls.append((method, url, body))
        assert config["agent_id"] == "agent-123"
        assert token == "test-token"
        if method == "POST" and url.endswith("/agents/approvals"):
            assert body is not None
            created.update(_codex_approval(body))
            return dict(created)
        if method == "GET" and url.endswith("/decision"):
            decision_reads += 1
            if decision_reads == 1:
                return {**created, "status": "PENDING_REVIEW"}
            approved = {**created, "status": "APPROVED"}
            approved["approved_payload_hash"] = hook._hash(approved)
            return approved
        if method == "POST" and url.endswith("/heartbeat"):
            assert body == {"source_invocation_id": created["source_invocation_id"]}
            return _invocation_heartbeat(created)
        if method == "POST" and url.endswith("/consume"):
            return {"consumed": True}
        raise AssertionError(f"Unexpected approval protocol request: {method} {url}")

    monkeypatch.setattr(hook, "_request", request)
    result = hook.run(_codex_event(), _codex_environment())

    assert result == ("allow", "Approved from Aixion Control Tower")
    assert [url.rsplit("/", 1)[-1] for _method, url, _body in calls] == [
        "approvals", "decision", "heartbeat", "decision", "consume"
    ]
    assert clock.sleeps == [hook.POLL_INTERVAL_SECONDS]


@pytest.mark.parametrize(
    "failure",
    ["unauthorized", "conflict", "timeout", "mismatch", "not_live", "malformed", "late"],
)
def test_failed_or_late_invocation_heartbeat_denies_without_consuming(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    clock = _patch_clock(monkeypatch)
    monkeypatch.setattr(hook, "INVOCATION_HEARTBEAT_INTERVAL_SECONDS", 1)
    calls: list[str] = []
    created: dict = {}

    def request(method: str, url: str, _config: dict, _token: str,
                body: dict | None = None, **kwargs: object) -> dict:
        calls.append(url.rsplit("/", 1)[-1])
        if method == "POST" and url.endswith("/agents/approvals"):
            assert body is not None
            created.update(_codex_approval(body))
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
            return response
        if method == "POST" and url.endswith("/expire"):
            return {**created, "status": "EXPIRED"}
        raise AssertionError(f"Heartbeat failure must not proceed: {method} {url}")

    monkeypatch.setattr(hook, "_request", request)
    environment = _codex_environment()
    environment["AIXION_CODEX_APPROVAL_TIMEOUT_SECONDS"] = "10"
    result = hook.run(_codex_event(), environment)

    assert result == ("deny", "Connector liveness could not be confirmed")
    assert calls == ["approvals", "decision", "heartbeat"]
    assert "consume" not in calls


def test_run_reconnects_after_ambiguous_decision_read_then_consumes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _patch_clock(monkeypatch)
    calls: list[str] = []
    created: dict = {}
    decision_reads = 0

    def request(method: str, url: str, _config: dict, _token: str, body: dict | None = None, **_kwargs) -> dict:
        nonlocal decision_reads
        calls.append(f"{method} {url.rsplit('/', 1)[-1]}")
        if method == "POST" and url.endswith("/agents/approvals"):
            assert body is not None
            created.update(_codex_approval(body))
            return dict(created)
        if method == "GET" and url.endswith("/decision"):
            decision_reads += 1
            if decision_reads == 1:
                raise hook.AmbiguousRequestError("temporary decision read failure")
            approved = {**created, "status": "APPROVED"}
            approved["approved_payload_hash"] = hook._hash(approved)
            return approved
        if method == "POST" and url.endswith("/consume"):
            return {"consumed": True}
        raise AssertionError(f"Unexpected approval protocol request: {method} {url}")

    monkeypatch.setattr(hook, "_request", request)
    result = hook.run(_codex_event(), _codex_environment())

    assert result == ("allow", "Approved from Aixion Control Tower")
    assert decision_reads == 2
    assert calls.count("POST consume") == 1
    assert clock.sleeps == [hook.POLL_INTERVAL_SECONDS]


def test_run_honors_retry_after_only_until_host_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _patch_clock(monkeypatch)
    created: dict = {}
    calls: list[str] = []
    request_timeouts: list[float] = []
    environment = _codex_environment()
    environment["AIXION_CODEX_APPROVAL_TIMEOUT_SECONDS"] = "3"

    def request(method: str, url: str, _config: dict, _token: str, body: dict | None = None, **kwargs) -> dict:
        calls.append(f"{method} {url.rsplit('/', 1)[-1]}")
        if "timeout_seconds" in kwargs:
            request_timeouts.append(kwargs["timeout_seconds"])
        if method == "POST" and url.endswith("/agents/approvals"):
            assert body is not None
            created.update(_codex_approval(body))
            return dict(created)
        if method == "GET" and url.endswith("/decision"):
            raise hook.RateLimitError(9)
        if method == "POST" and url.endswith("/expire"):
            return {**created, "status": "EXPIRED"}
        raise AssertionError(f"Unexpected approval protocol request: {method} {url}")

    monkeypatch.setattr(hook, "_request", request)
    result = hook.run(_codex_event(), environment)

    assert result[0] == "deny"
    assert clock.sleeps == [3]
    assert clock.now == 103
    assert "POST expire" in calls
    assert "POST consume" not in calls
    assert request_timeouts == [3, hook.EXPIRY_CLEANUP_TIMEOUT_SECONDS]


def test_long_retry_after_renews_invocation_lease_before_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _patch_clock(monkeypatch)
    created: dict = {}
    calls: list[tuple[str, float]] = []
    environment = _codex_environment()
    environment["AIXION_CODEX_APPROVAL_TIMEOUT_SECONDS"] = "120"

    def request(method: str, url: str, _config: dict, _token: str, body: dict | None = None, **_kwargs) -> dict:
        calls.append((url.rsplit("/", 1)[-1], clock.now))
        if method == "POST" and url.endswith("/agents/approvals"):
            assert body is not None
            created.update(_codex_approval(body))
            return dict(created)
        if method == "GET" and url.endswith("/decision"):
            if clock.now < 190:
                raise hook.RateLimitError(90)
            approved = {**created, "status": "APPROVED"}
            approved["approved_payload_hash"] = hook._hash(approved)
            return approved
        if method == "POST" and url.endswith("/heartbeat"):
            return _invocation_heartbeat(created)
        if method == "POST" and url.endswith("/consume"):
            return {"consumed": True}
        raise AssertionError(f"Unexpected approval protocol request: {method} {url}")

    monkeypatch.setattr(hook, "_request", request)
    result = hook.run(_codex_event(), environment)

    assert result == ("allow", "Approved from Aixion Control Tower")
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
    environment = _codex_environment()
    environment["AIXION_CODEX_APPROVAL_TIMEOUT_SECONDS"] = "120"

    def request(method: str, url: str, _config: dict, _token: str, body: dict | None = None, **_kwargs) -> dict:
        endpoint = url.rsplit("/", 1)[-1]
        calls.append(endpoint)
        if method == "POST" and url.endswith("/agents/approvals"):
            assert body is not None
            created.update(_codex_approval(body))
            return dict(created)
        if method == "GET" and url.endswith("/decision"):
            raise hook.RateLimitError(90)
        if method == "POST" and url.endswith("/heartbeat"):
            if heartbeat_failure == "rate_limited":
                raise hook.RateLimitError(90)
            return {**_invocation_heartbeat(created), "live": False}
        raise AssertionError(f"Failed liveness must stop the approval wait: {method} {url}")

    monkeypatch.setattr(hook, "_request", request)
    result = hook.run(_codex_event(), environment)

    assert result == ("deny", "Connector liveness could not be confirmed")
    assert calls == ["approvals", "decision", "heartbeat"]
    assert clock.sleeps == [hook.INVOCATION_HEARTBEAT_INTERVAL_SECONDS]
    assert "consume" not in calls


def test_late_consume_response_never_allows_codex_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _patch_clock(monkeypatch)
    created: dict = {}
    calls: list[tuple[str, float | None]] = []

    def request(
        method: str,
        url: str,
        _config: dict,
        _token: str,
        body: dict | None = None,
        **kwargs,
    ) -> dict:
        calls.append((method, kwargs.get("timeout_seconds")))
        if method == "POST" and url.endswith("/agents/approvals"):
            assert body is not None
            created.update(_codex_approval(body))
            return dict(created)
        if method == "GET" and url.endswith("/decision"):
            approved = {**created, "status": "APPROVED"}
            approved["approved_payload_hash"] = hook._hash(approved)
            clock.now += 0.25
            return approved
        if method == "POST" and url.endswith("/consume"):
            # Model a transport that consumes the entire remaining deadline
            # before returning a successful but too-late claim response.
            clock.now += float(kwargs["timeout_seconds"])
            return {"consumed": True}
        if method == "POST" and url.endswith("/expire"):
            return {**created, "status": "EXPIRED"}
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr(hook, "_request", request)
    result = hook.run(
        _codex_event(),
        {**_codex_environment(), "AIXION_CODEX_APPROVAL_TIMEOUT_SECONDS": "1"},
    )

    assert result[0] == "deny"
    assert "after the Codex request deadline" in result[1]
    assert [method for method, _timeout in calls] == ["POST", "GET", "POST"]
    assert calls[1][1] == pytest.approx(1.0)
    assert calls[2][1] == pytest.approx(0.75)


def test_ambiguous_consume_skips_failure_report_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _patch_clock(monkeypatch)
    created: dict = {}
    calls: list[tuple[str, float | None]] = []

    def request(
        method: str,
        url: str,
        _config: dict,
        _token: str,
        body: dict | None = None,
        **kwargs,
    ) -> dict:
        calls.append((url.rsplit("/", 1)[-1], kwargs.get("timeout_seconds")))
        if method == "POST" and url.endswith("/agents/approvals"):
            assert body is not None
            created.update(_codex_approval(body))
            return dict(created)
        if method == "GET" and url.endswith("/decision"):
            approved = {**created, "status": "APPROVED"}
            approved["approved_payload_hash"] = hook._hash(approved)
            return approved
        if method == "POST" and url.endswith("/consume"):
            clock.now += float(kwargs["timeout_seconds"])
            raise hook.AmbiguousRequestError("claim outcome unknown")
        if method == "POST" and url.endswith("/delivery-failure"):
            pytest.fail("report must not exceed the configured approval deadline")
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr(hook, "_request", request)
    result = hook.run(
        _codex_event(),
        {**_codex_environment(), "AIXION_CODEX_APPROVAL_TIMEOUT_SECONDS": "2"},
    )

    assert result[0] == "deny"
    assert "ambiguous" in result[1]
    assert [name for name, _timeout in calls] == ["approvals", "decision", "consume"]
    assert calls[-1][1] == pytest.approx(2.0)


def test_http_request_applies_capped_deadline_and_response_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, str, dict, bytes | None, float, int]] = []

    def request(method, url, headers, body, timeout_seconds, *, max_response_bytes):
        observed.append((method, url, headers, body, timeout_seconds, max_response_bytes))
        return SimpleNamespace(status=200, headers={}, body=b'{"ok":true}')

    monkeypatch.setattr(hook, "deadline_request", request)
    config = {"agent_id": "agent-123"}

    assert hook._request(
        "GET", "https://example.test/decision", config, "test-token", timeout_seconds=2.5
    ) == {"ok": True}
    assert hook._request(
        "GET", "https://example.test/decision", config, "test-token", timeout_seconds=90
    ) == {"ok": True}
    assert [call[4] for call in observed] == [2.5, hook.MAX_REQUEST_TIMEOUT_SECONDS]
    assert all(call[0] == "GET" and call[1] == "https://example.test/decision" for call in observed)
    assert all(call[5] == hook.MAX_RESPONSE_BYTES for call in observed)
    assert observed[0][2]["X-Aixion-Approval-Protocol"] == "1"


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
            "https://example.test/agents/approvals/id/heartbeat",
            {"agent_id": "agent-123"},
            "test-token",
            {"source_invocation_id": "codex:v1:test"},
            expected_status=200,
        )


def test_run_exhausted_reconnect_wait_expires_and_never_consumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _patch_clock(monkeypatch)
    created: dict = {}
    calls: list[str] = []
    environment = _codex_environment()
    environment["AIXION_CODEX_APPROVAL_TIMEOUT_SECONDS"] = "3"

    def request(method: str, url: str, _config: dict, _token: str, body: dict | None = None, **_kwargs) -> dict:
        calls.append(f"{method} {url.rsplit('/', 1)[-1]}")
        if method == "POST" and url.endswith("/agents/approvals"):
            assert body is not None
            created.update(_codex_approval(body))
            return dict(created)
        if method == "GET" and url.endswith("/decision"):
            raise hook.AmbiguousRequestError("decision service disconnected")
        if method == "POST" and url.endswith("/expire"):
            return {**created, "status": "EXPIRED"}
        raise AssertionError(f"Unexpected approval protocol request: {method} {url}")

    monkeypatch.setattr(hook, "_request", request)
    result = hook.run(_codex_event(), environment)

    assert result[0] == "deny"
    assert clock.sleeps == [hook.POLL_INTERVAL_SECONDS, 1]
    assert clock.now == 103
    assert calls.count("POST expire") == 1
    assert "POST consume" not in calls
