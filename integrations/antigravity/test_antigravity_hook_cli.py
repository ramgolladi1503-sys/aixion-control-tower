from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import threading
import time
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
    [("allow", "approved"), ("deny", "rejected")],
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


def test_run_consumes_the_approved_invocation_before_returning_allow(
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

    assert result == {"decision": "allow", "reason": "Approved from Aixion Control Tower"}
    assert [method for method, _url, _body in calls] == ["POST", "GET", "POST"]
    assert calls[-1][1].endswith("/agents/approvals/approval_0123456789ab/consume")


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


def test_run_denies_if_mobile_approval_payload_changes_after_creation(
    monkeypatch: pytest.MonkeyPatch,
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
            changed = {**created_approval, "status": "APPROVED", "summary": "different invocation"}
            changed["approved_payload_hash"] = hook._payload_hash(changed)
            return changed
        raise AssertionError(f"Changed payload must not be consumed: {method} {url}")

    monkeypatch.setattr(hook, "_request", request)

    result = hook.run(_hook_payload(), _test_environment())

    assert result == {
        "decision": "deny",
        "reason": "Approval payload changed after the original tool invocation",
    }
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

    assert result["decision"] == "allow"
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

    assert result["decision"] == "allow"
    assert decision_reads == 2
    assert calls.count("POST consume") == 1
    assert clock.sleeps == [hook.POLL_INTERVAL_SECONDS]


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
