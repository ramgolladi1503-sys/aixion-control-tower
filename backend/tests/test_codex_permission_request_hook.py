from __future__ import annotations

import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sys
import threading
import time

import pytest

ROOT = Path(__file__).parents[2]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


hook = _load_module(
    "aixion_codex_permission_request_hook",
    ROOT / "integrations" / "codex" / "aixion_permission_request_hook.py",
)
setup = _load_module(
    "aixion_codex_setup_workspace",
    ROOT / "integrations" / "codex" / "setup_workspace.py",
)

ENV = {
    "AIXION_API_BASE_URL": "http://127.0.0.1:8012",
    "AIXION_AGENT_ID": "agent-codex-test",
    "AIXION_AGENT_TOKEN": "test-token",
    "AIXION_ALLOW_AGENT_TOKEN_ENV": "true",
    "AIXION_PROJECT_ID": "project-codex-test",
}


def test_http_request_advertises_approval_protocol_v1(monkeypatch) -> None:
    captured = {}

    def request(method, url, headers, body, timeout_seconds, *, max_response_bytes):
        captured.update(method=method, url=url, headers=headers, body=body,
                        timeout=timeout_seconds, limit=max_response_bytes)
        return type("Response", (), {"status": 200, "headers": {}, "body": b"{}"})()

    monkeypatch.setattr(hook, "deadline_request", request)
    assert hook._request(
        "GET",
        "http://127.0.0.1:8012/agents/approvals/id/decision",
        {"agent_id": ENV["AIXION_AGENT_ID"]},
        ENV["AIXION_AGENT_TOKEN"],
    ) == {}

    assert captured["headers"]["X-Aixion-Approval-Protocol"] == "1"
    assert captured["timeout"] == 10
    assert captured["limit"] == hook.MAX_RESPONSE_BYTES


@pytest.mark.parametrize(
    ("status", "headers", "exception", "message"),
    [
        (429, {"Retry-After": "7"}, hook.RateLimitError, "rate limit"),
        (503, {}, hook.AmbiguousRequestError, "ambiguous server failure"),
        (302, {}, hook.GateError, "HTTP 302"),
        (400, {}, hook.GateError, "HTTP 400"),
    ],
)
def test_http_status_mapping_remains_fail_closed(
    status: int, headers: dict, exception: type[Exception], message: str, monkeypatch
) -> None:
    response = type("Response", (), {"status": status, "headers": headers, "body": b"{}"})()
    monkeypatch.setattr(hook, "deadline_request", lambda *_args, **_kwargs: response)
    with pytest.raises(exception, match=message) as raised:
        hook._request(
            "POST", "http://127.0.0.1:8012/agents/approvals",
            {"agent_id": ENV["AIXION_AGENT_ID"]}, ENV["AIXION_AGENT_TOKEN"], {},
        )
    if isinstance(raised.value, hook.RateLimitError):
        assert raised.value.retry_after_seconds == 7


def test_hook_input_rejects_duplicate_identity_keys(monkeypatch) -> None:
    raw = (
        b'{"hook_event_name":"PermissionRequest","session_id":"first",'
        b'"session_id":"second","turn_id":"turn","tool_name":"Bash",'
        b'"tool_input":{"command":"echo safe"}}'
    )
    monkeypatch.setattr(hook.sys, "stdin", type("Stdin", (), {"buffer": io.BytesIO(raw)})())

    with pytest.raises(hook.GateError, match="invalid JSON"):
        hook._read_input()


def test_api_response_rejects_duplicate_approval_identity(monkeypatch) -> None:
    response = type("Response", (), {
        "status": 200,
        "headers": {},
        "body": b'{"id":"approval_one","source_agent_id":"expected",'
        b'"source_agent_id":"attacker"}',
    })()
    monkeypatch.setattr(hook, "deadline_request", lambda *_args, **_kwargs: response)

    with pytest.raises(hook.AmbiguousRequestError, match="invalid response data"):
        hook._request(
            "GET",
            "http://127.0.0.1:8012/agents/approvals/approval_one/decision",
            {"agent_id": ENV["AIXION_AGENT_ID"]},
            ENV["AIXION_AGENT_TOKEN"],
        )


@pytest.mark.parametrize("stall", ["headers", "body"])
def test_loopback_slow_response_is_bounded_and_denies_without_late_consume(
    stall: str, monkeypatch
) -> None:
    """A timed-out HTTP attempt can never become a late Codex allow/consume."""
    paths: list[str] = []
    created_records: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args) -> None:
            return

        def _json(self, value: dict) -> None:
            raw = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            self.wfile.flush()

        def do_POST(self) -> None:
            paths.append(self.path)
            if self.path == "/agents/approvals":
                size = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(size))
                record = _record(body)
                created_records.append(record)
                if stall == "headers":
                    time.sleep(0.35)
                self._json(record)
                return
            if self.path.endswith("/consume"):
                # The server accepts the one-shot consume, then stalls before
                # returning the response. The hook must deny and must not retry.
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "17")
                self.end_headers()
                self.wfile.write(b'{"consumed":')
                self.wfile.flush()
                time.sleep(1.3)
                try:
                    self.wfile.write(b'true}')
                    self.wfile.flush()
                except OSError:
                    pass
                return
            self._json({"status": "ok"})

        def do_GET(self) -> None:
            paths.append(self.path)
            record = dict(created_records[0])
            record["status"] = "APPROVED"
            record["approved_payload_hash"] = hook._hash(record)
            self._json(record)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    monkeypatch.setattr(hook, "MAX_REQUEST_TIMEOUT_SECONDS", 0.1 if stall == "headers" else 10.0)
    env = {
        **ENV,
        "AIXION_API_BASE_URL": f"http://127.0.0.1:{server.server_port}",
        "AIXION_CODEX_APPROVAL_TIMEOUT_SECONDS": "1",
    }
    started = time.monotonic()
    try:
        result = hook.run(_event(), env)
        elapsed = time.monotonic() - started
        # Let a late response reach the server before inspecting one-shot calls.
        time.sleep(0.4)
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=1)

    assert result[0] == "deny"
    if stall == "headers":
        assert elapsed < 0.8
        assert paths.count("/agents/approvals") == hook.MAX_CREATE_ATTEMPTS
        assert not any(path.endswith("/consume") for path in paths)
    else:
        assert elapsed < 1.2
        assert sum(path.endswith("/consume") for path in paths) == 1


def test_preflight_checks_health_without_token_then_authenticates_connector_scope(monkeypatch) -> None:
    def request(method, url, headers, _body, timeout_seconds, *, max_response_bytes):
        if url.endswith("/health"):
            assert method == "GET"
            assert "X-Aixion-Agent-Token" not in headers
            assert "X-Aixion-Agent-Id" not in headers
            assert timeout_seconds == 5
            body = b'{"status":"ok"}'
        else:
            assert url == (
                "http://127.0.0.1:8012/agents/identity?provider=CODEX&project_id=project-codex-test"
            )
            assert headers["X-Aixion-Agent-Token"] == "test-token"
            assert headers["X-Aixion-Agent-Id"] == "agent-codex-test"
            assert headers["X-Aixion-Approval-Protocol"] == "1"
            assert timeout_seconds == 10
            body = (
                b'{"agent_id":"agent-codex-test","provider":"CODEX",'
                b'"workspace_id":"workspace-test","project_id":"project-codex-test"}'
            )
        return type("Response", (), {"status": 200, "headers": {}, "body": body})()

    monkeypatch.setattr(hook, "deadline_request", request)

    assert hook.check_connection(ENV) == {
        "api_base_url": "http://127.0.0.1:8012",
        "agent_id": "agent-codex-test",
        "project_id": "project-codex-test",
    }


def test_preflight_rejects_authenticated_identity_mismatch(monkeypatch) -> None:
    def request(_method, url, _headers, _body, _timeout, *, max_response_bytes):
        assert url.endswith("/health")
        return type("Response", (), {"status": 200, "headers": {}, "body": b'{"status":"ok"}'})()

    monkeypatch.setattr(hook, "deadline_request", request)
    monkeypatch.setattr(hook, "_request", lambda *_args, **_kwargs: {
        "agent_id": "agent-other",
        "provider": "CODEX",
        "workspace_id": "workspace-test",
        "project_id": ENV["AIXION_PROJECT_ID"],
    })

    with pytest.raises(hook.GateError, match="identity, provider, or project scope"):
        hook.check_connection(ENV)


def test_version_two_hash_binds_workspace_and_risk() -> None:
    approval = {
        "project_id": "project-codex-test",
        "work_order_id": None,
        "title": "Run command",
        "summary": "Review command",
        "agent_name": "Codex test",
        "target_branch": "main",
        "files": [],
        "test_plan": [],
        "rollback_plan": "",
        "source_provider": "CODEX",
        "source_agent_id": ENV["AIXION_AGENT_ID"],
        "source_agent_name": "Codex test",
        "source_session_id": "session-test",
        "source_invocation_id": "invocation-test",
        "source_task_url": None,
        "approved_payload_hash_version": 2,
        "workspace_id": "workspace-test",
        "risk": {"level": "MEDIUM", "reasons": [], "blocked": False, "required_actions": []},
    }
    digest = hook._hash(approval)

    changed_risk = {**approval, "risk": {**approval["risk"], "level": "HIGH"}}
    changed_workspace = {**approval, "workspace_id": "workspace-other"}
    assert digest.startswith("sha256:v2:")
    assert digest != hook._hash(changed_risk)
    assert digest != hook._hash(changed_workspace)


def test_unknown_hash_version_fails_closed() -> None:
    with pytest.raises(hook.GateError, match="Unsupported approval payload hash version"):
        hook._hash({"approved_payload_hash_version": 3})


def _event(args: object | None = None) -> dict:
    return {
        "hook_event_name": "PermissionRequest",
        "permission_mode": "default",
        "session_id": "session-123",
        "turn_id": "turn-456",
        "tool_use_id": "call-789",
        "tool_name": "Bash",
        "tool_input": {"command": "touch approved.txt"} if args is None else args,
    }


def _record(body: dict, *, status: str = "PENDING_REVIEW") -> dict:
    return {
        **body,
        "id": "approval_0123456789ab",
        "status": status,
        "source_provider": "CODEX",
        "source_agent_id": ENV["AIXION_AGENT_ID"],
        "source_agent_name": "Codex phone gate",
        "source_task_url": None,
        "approved_payload_hash": None,
    }


def test_phone_approval_allows_exact_codex_permission_request(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    created_body: dict = {}

    def request(method, url, _config, _token, body=None, **_kwargs):
        nonlocal created_body
        calls.append((method, url))
        if method == "POST" and url.endswith("/consume"):
            return {"consumed": True}
        if method == "POST":
            created_body = body or {}
            return _record(body or {})
        approved = _record({
            "project_id": ENV["AIXION_PROJECT_ID"],
            "title": "Codex permission: Bash",
            "summary": created_body["summary"],
            "agent_name": "Codex",
            "target_branch": "current-workspace",
            "files": [{"path": "codex://tool/Bash", "change_type": "tool_call",
                       "diff": '{"command":"touch approved.txt"}', "new_content": None}],
            "test_plan": ["Confirm the requested Codex tool and arguments are expected"],
            "rollback_plan": "Deny this request if uncertain. Approval applies only to this invocation.",
            "source_session_id": created_body["source_session_id"],
            "source_invocation_id": created_body["source_invocation_id"],
        }, status="APPROVED")
        approved["approved_payload_hash"] = hook._hash(approved)
        return approved

    monkeypatch.setattr(hook, "_request", request)
    assert hook.run(_event(), ENV) == ("allow", "Approved from Aixion Control Tower")
    assert calls[0] == ("POST", "http://127.0.0.1:8012/agents/approvals")
    assert calls[1] == ("GET", "http://127.0.0.1:8012/agents/approvals/approval_0123456789ab/decision")
    assert calls[2] == ("POST", "http://127.0.0.1:8012/agents/approvals/approval_0123456789ab/consume")


def test_ambiguous_decision_poll_reconnects_and_resumes_same_request(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    created_body: dict = {}
    get_calls = 0

    def request(method, url, _config, _token, body=None, **_kwargs):
        nonlocal created_body, get_calls
        calls.append((method, url))
        if method == "POST" and url.endswith("/consume"):
            return {"consumed": True}
        if method == "POST":
            created_body = body or {}
            return _record(created_body)
        get_calls += 1
        if get_calls == 1:
            raise hook.AmbiguousRequestError("temporary network loss")
        approved = _record(created_body, status="APPROVED")
        approved["approved_payload_hash"] = hook._hash(approved)
        return approved

    monkeypatch.setattr(hook, "_request", request)
    sleeps: list[int] = []
    monkeypatch.setattr(hook.time, "sleep", sleeps.append)

    assert hook.run(_event(), ENV) == ("allow", "Approved from Aixion Control Tower")
    assert get_calls == 2
    assert [method for method, _ in calls] == ["POST", "GET", "GET", "POST"]
    assert sleeps == [hook.POLL_INTERVAL_SECONDS]


def test_repeated_ambiguous_decision_polls_expire_and_deny(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    sleeps: list[int] = []
    clock = [0.0]

    def request(method, url, _config, _token, body=None, **_kwargs):
        calls.append((method, url))
        if method == "POST" and url.endswith("/agents/approvals"):
            return _record(body or {})
        if method == "GET":
            raise hook.AmbiguousRequestError("temporary network loss")
        if url.endswith("/expire"):
            return {
                "id": "approval_0123456789ab",
                "project_id": ENV["AIXION_PROJECT_ID"],
                "source_provider": "CODEX",
                "source_agent_id": ENV["AIXION_AGENT_ID"],
                "status": "EXPIRED",
            }
        raise AssertionError(f"Unexpected request: {method} {url}")

    def monotonic():
        return clock[0]

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(hook, "_request", request)
    monkeypatch.setattr(hook.time, "monotonic", monotonic)
    monkeypatch.setattr(hook.time, "sleep", sleep)

    assert hook.run(
        _event(), {**ENV, "AIXION_CODEX_APPROVAL_TIMEOUT_SECONDS": "7"}
    ) == ("deny", "Approval timed out; the request expired and the Codex request was denied")
    assert sleeps == [2, 4, 1]
    assert sum(method == "GET" for method, _ in calls) == 3
    assert calls[-1][0] == "POST" and calls[-1][1].endswith("/expire")
    assert not any(url.endswith("/consume") for _, url in calls)


def test_provider_invocation_identity_is_stable_across_hook_restarts() -> None:
    first = hook._invocation_identity(_event(), tool_name="Bash", tool_input=_event()["tool_input"])
    retry = hook._invocation_identity(_event(), tool_name="Bash", tool_input=_event()["tool_input"])
    different_turn = hook._invocation_identity(
        {**_event(), "turn_id": "turn-457"},
        tool_name="Bash",
        tool_input=_event()["tool_input"],
    )
    different_input = hook._invocation_identity(
        _event(), tool_name="Bash", tool_input={"command": "touch other.txt"}
    )
    different_tool_call = hook._invocation_identity(
        {**_event(), "tool_use_id": "call-790"},
        tool_name="Bash",
        tool_input=_event()["tool_input"],
    )

    assert first == retry
    assert (
        first is not None
        and different_turn is not None
        and different_input is not None
        and different_tool_call is not None
    )
    assert first[1] != different_turn[1]
    assert first[1] != different_input[1]
    assert first[1] != different_tool_call[1]


def test_documented_permission_request_gets_callback_scoped_invocation_identity() -> None:
    event = _event()
    event.pop("tool_use_id")

    first_callback = hook._invocation_identity(
        event, tool_name="Bash", tool_input=event["tool_input"]
    )
    second_callback = hook._invocation_identity(
        event, tool_name="Bash", tool_input=event["tool_input"]
    )

    assert first_callback is not None and second_callback is not None
    assert first_callback[0] == second_callback[0]
    assert first_callback[1] != second_callback[1]


def test_documented_permission_request_without_tool_use_id_completes_phone_flow(monkeypatch) -> None:
    event = _event()
    event.pop("tool_use_id")
    calls: list[tuple[str, str, dict | None]] = []
    approval: dict = {}

    def request(method, url, _config, _token, body=None, **_kwargs):
        nonlocal approval
        calls.append((method, url, body))
        if method == "POST" and url.endswith("/consume"):
            return {"consumed": True}
        if method == "POST":
            approval = _record(body or {}, status="APPROVED")
            approval["approved_payload_hash"] = hook._hash(approval)
            return approval
        return approval

    monkeypatch.setattr(hook, "_request", request)

    assert hook.run(event, ENV) == ("allow", "Approved from Aixion Control Tower")
    create = calls[0][2]
    assert create is not None
    assert create["source_invocation_id"].startswith("codex:v1:")
    assert calls[-1][0] == "POST" and calls[-1][1].endswith("/consume")


def test_second_callback_cannot_reuse_stale_approved_record_without_tool_use_id(monkeypatch) -> None:
    """A create retry/replay must not carry approval across callback identities."""
    event = _event()
    event.pop("tool_use_id")
    calls: list[tuple[str, str]] = []
    first_callback_record: dict = {}
    create_bodies: list[dict] = []
    create_count = 0

    def request(method, url, _config, _token, body=None, **_kwargs):
        nonlocal create_count, first_callback_record
        calls.append((method, url))
        if method == "POST" and url.endswith("/agents/approvals"):
            create_count += 1
            create_bodies.append(body or {})
            if create_count == 1:
                first_callback_record = _record(body or {})
                return first_callback_record
            # Simulate the server returning a previously approved record for
            # the first callback when the second callback creates its request.
            stale_approved = {**first_callback_record, "status": "APPROVED"}
            stale_approved["approved_payload_hash"] = hook._hash(stale_approved)
            return stale_approved
        if method == "GET":
            return {**first_callback_record, "status": "DENIED"}
        if method == "POST" and url.endswith("/consume"):
            return {"consumed": True}
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr(hook, "_request", request)

    # Complete the first callback as denied so its record remains available for
    # the simulated stale replay without ever consuming an approval.
    first_result = hook.run(event, ENV)
    first_callback_call_count = len(calls)
    second_result = hook.run(event, ENV)

    assert first_result == ("deny", "Control Tower decision: DENIED")
    assert second_result == (
        "deny",
        "Control Tower did not create a matching actionable approval",
    )
    assert create_count == 2
    assert create_bodies[0]["source_invocation_id"] != create_bodies[1]["source_invocation_id"]
    assert first_callback_record["source_invocation_id"] == create_bodies[0]["source_invocation_id"]
    assert sum(method == "GET" for method, _ in calls) == 1
    assert calls[first_callback_call_count:] == [
        ("POST", "http://127.0.0.1:8012/agents/approvals")
    ]
    assert not any(url.endswith("/consume") for _, url in calls)


def test_session_identity_is_stable_across_turns_and_tool_calls() -> None:
    first = hook._invocation_identity(
        _event(), tool_name="Bash", tool_input=_event()["tool_input"]
    )
    next_tool = hook._invocation_identity(
        {**_event(), "turn_id": "turn-457", "tool_use_id": "call-790"},
        tool_name="Bash",
        tool_input={"command": "echo next"},
    )

    assert first is not None and next_tool is not None
    assert first[0] == next_tool[0]
    assert first[1] != next_tool[1]


def test_restart_retry_can_resume_existing_approved_invocation(monkeypatch) -> None:
    calls: list[str] = []
    approved: dict = {}

    def request(method, url, _config, _token, body=None, **_kwargs):
        nonlocal approved
        calls.append(method)
        if method == "POST" and url.endswith("/consume"):
            return {"consumed": True}
        if method == "POST":
            approved = _record(body or {}, status="APPROVED")
            approved["approved_payload_hash"] = hook._hash(approved)
            return approved
        return approved

    monkeypatch.setattr(hook, "_request", request)

    assert hook.run(_event(), ENV) == ("allow", "Approved from Aixion Control Tower")
    assert calls == ["POST", "GET", "POST"]


@pytest.mark.parametrize("missing", ["session_id", "turn_id"])
def test_missing_stable_invocation_identity_denies_before_network(missing: str, monkeypatch) -> None:
    event = _event()
    event.pop(missing)
    monkeypatch.setattr(hook, "_request", lambda *args, **kwargs: pytest.fail("must not call API"))

    behavior, reason = hook.run(event, ENV)

    assert behavior == "deny"
    assert "stable session, turn, or tool-call identity" in reason


def test_malformed_present_tool_use_id_denies_before_network(monkeypatch) -> None:
    event = {**_event(), "tool_use_id": 42}
    monkeypatch.setattr(hook, "_request", lambda *args, **kwargs: pytest.fail("must not call API"))

    behavior, reason = hook.run(event, ENV)

    assert behavior == "deny"
    assert "stable session, turn, or tool-call identity" in reason


def test_duplicate_approval_delivery_claim_denies_codex_request(monkeypatch) -> None:
    created_body: dict = {}

    def request(method, url, _config, _token, body=None, **_kwargs):
        nonlocal created_body
        if method == "POST" and url.endswith("/consume"):
            return {"consumed": False}
        if method == "POST":
            created_body = body or {}
            return _record(body or {})
        approved = _record({
            "project_id": ENV["AIXION_PROJECT_ID"],
            "title": "Codex permission: Bash",
            "summary": created_body["summary"],
            "agent_name": "Codex",
            "target_branch": "current-workspace",
            "files": [{"path": "codex://tool/Bash", "change_type": "tool_call",
                       "diff": '{"command":"touch approved.txt"}', "new_content": None}],
            "test_plan": ["Confirm the requested Codex tool and arguments are expected"],
            "rollback_plan": "Deny this request if uncertain. Approval applies only to this invocation.",
            "source_session_id": created_body["source_session_id"],
            "source_invocation_id": created_body["source_invocation_id"],
        }, status="APPROVED")
        approved["approved_payload_hash"] = hook._hash(approved)
        return approved

    monkeypatch.setattr(hook, "_request", request)
    behavior, reason = hook.run(_event(), ENV)
    assert behavior == "deny"
    assert "already claimed" in reason


def test_ambiguous_delivery_claim_is_reported_without_allowing_codex_request(monkeypatch) -> None:
    created_body: dict = {}
    failure_body: dict = {}
    calls: list[str] = []

    def request(method, url, _config, _token, body=None, **_kwargs):
        nonlocal created_body, failure_body
        if method == "POST" and url.endswith("/consume"):
            calls.append("consume")
            raise hook.AmbiguousRequestError("connection outcome unknown")
        if method == "POST" and url.endswith("/delivery-failure"):
            calls.append("report")
            failure_body = body or {}
            return {"recorded": True}
        if method == "POST":
            calls.append("create")
            created_body = body or {}
            return _record(created_body)
        calls.append("read")
        approved = _record(created_body, status="APPROVED")
        approved["approved_payload_hash"] = hook._hash(approved)
        return approved

    monkeypatch.setattr(hook, "_request", request)
    behavior, reason = hook.run(_event(), ENV)

    assert behavior == "deny"
    assert "ambiguous" in reason
    assert calls == ["create", "read", "consume", "report"]
    approved = _record(created_body, status="APPROVED")
    assert failure_body == {
        "source_invocation_id": created_body["source_invocation_id"],
        "approved_payload_hash": hook._hash(approved),
        "reason_code": "CLAIM_RESPONSE_AMBIGUOUS",
    }


def test_phone_rejection_denies_codex_request(monkeypatch) -> None:
    created_body: dict = {}

    def request(method, _url, _config, _token, body=None, **_kwargs):
        nonlocal created_body
        if method == "POST":
            created_body = body or {}
            return _record(body or {})
        return _record({
            "project_id": ENV["AIXION_PROJECT_ID"],
            "title": "Codex permission: Bash",
            "summary": created_body["summary"],
            "agent_name": "Codex",
            "target_branch": "current-workspace",
            "files": [{"path": "codex://tool/Bash", "change_type": "tool_call",
                       "diff": '{"command":"touch approved.txt"}', "new_content": None}],
            "test_plan": ["Confirm the requested Codex tool and arguments are expected"],
            "rollback_plan": "Deny this request if uncertain. Approval applies only to this invocation.",
            "source_session_id": created_body["source_session_id"],
            "source_invocation_id": created_body["source_invocation_id"],
        }, status="DENIED")

    monkeypatch.setattr(hook, "_request", request)
    behavior, message = hook.run(_event(), ENV)
    assert behavior == "deny"
    assert "DENIED" in message


@pytest.mark.parametrize("args", [
    {"command": "echo access_token=very-secret-value"},
    {"password": "not-displayable"},
])
def test_secret_like_arguments_are_denied_before_network(args, monkeypatch) -> None:
    monkeypatch.setattr(hook, "_request", lambda *args, **kwargs: pytest.fail("must not call API"))
    assert hook.run(_event(args), ENV)[0] == "deny"


def test_oversized_arguments_are_denied_before_network(monkeypatch) -> None:
    monkeypatch.setattr(hook, "_request", lambda *args, **kwargs: pytest.fail("must not call API"))
    assert hook.run(_event({"large": "x" * hook.MAX_ARGUMENTS_BYTES}), ENV)[0] == "deny"


def test_mismatched_approval_identity_denies(monkeypatch) -> None:
    def request(method, _url, _config, _token, body=None, **_kwargs):
        if method == "POST":
            record = _record(body or {})
            record["source_agent_id"] = "another-agent"
            return record
        pytest.fail("must not poll a mismatched approval")

    monkeypatch.setattr(hook, "_request", request)
    assert hook.run(_event(), ENV)[0] == "deny"


def test_changed_payload_denies_even_when_approval_is_approved(monkeypatch) -> None:
    created_body: dict = {}

    def request(method, _url, _config, _token, body=None, **_kwargs):
        nonlocal created_body
        if method == "POST":
            created_body = body or {}
            return _record(body or {})
        record = _record({
            "project_id": ENV["AIXION_PROJECT_ID"],
            "title": "Codex permission: Bash",
            "summary": created_body["summary"],
            "agent_name": "Codex", "target_branch": "current-workspace",
            "files": [{"path": "codex://tool/Bash", "change_type": "tool_call",
                       "diff": '{"command":"different"}', "new_content": None}],
            "test_plan": ["Confirm the requested Codex tool and arguments are expected"],
            "rollback_plan": "Deny this request if uncertain. Approval applies only to this invocation.",
            "source_session_id": created_body["source_session_id"],
            "source_invocation_id": created_body["source_invocation_id"],
        }, status="APPROVED")
        record["approved_payload_hash"] = hook._hash(record)
        return record

    monkeypatch.setattr(hook, "_request", request)
    behavior, reason = hook.run(_event(), ENV)
    assert behavior == "deny"
    assert "changed" in reason


def test_timeout_expires_approval_and_denies(monkeypatch) -> None:
    calls: list[str] = []
    created_body: dict = {}

    def request(method, _url, _config, _token, body=None, **_kwargs):
        nonlocal created_body
        calls.append(method)
        if method == "POST":
            created_body = body or {}
            return _record(body or {})
        return _record({
            "project_id": ENV["AIXION_PROJECT_ID"],
            "title": "Codex permission: Bash",
            "summary": created_body["summary"],
            "agent_name": "Codex", "target_branch": "current-workspace",
            "files": [{"path": "codex://tool/Bash", "change_type": "tool_call",
                       "diff": '{"command":"touch approved.txt"}', "new_content": None}],
            "test_plan": ["Confirm the requested Codex tool and arguments are expected"],
            "rollback_plan": "Deny this request if uncertain. Approval applies only to this invocation.",
            "source_session_id": created_body["source_session_id"],
            "source_invocation_id": created_body["source_invocation_id"],
        }, status="EXPIRED")

    times = iter([0.0, 2.0])
    monkeypatch.setattr(hook, "_request", request)
    monkeypatch.setattr(hook.time, "monotonic", lambda: next(times))
    result = hook.run(_event(), {**ENV, "AIXION_CODEX_APPROVAL_TIMEOUT_SECONDS": "1"})
    assert result[0] == "deny"
    assert calls == ["POST", "POST"]


def test_approval_read_returning_after_deadline_is_not_claimed(monkeypatch) -> None:
    calls: list[str] = []
    created_body: dict = {}
    clock = [0.0]
    timeouts: list[float] = []

    def request(method, _url, _config, _token, body=None, **kwargs):
        nonlocal created_body
        calls.append(method)
        if "timeout_seconds" in kwargs:
            timeouts.append(kwargs["timeout_seconds"])
        if method == "POST" and len(calls) == 1:
            created_body = body or {}
            return _record(created_body)
        if method == "GET":
            approved = _record(created_body, status="APPROVED")
            approved["approved_payload_hash"] = hook._hash(approved)
            clock[0] = 2.0
            return approved
        return _record(created_body, status="APPROVED")

    monkeypatch.setattr(hook, "_request", request)
    monkeypatch.setattr(hook.time, "monotonic", lambda: clock[0])

    behavior, reason = hook.run(
        _event(), {**ENV, "AIXION_CODEX_APPROVAL_TIMEOUT_SECONDS": "1"}
    )

    assert behavior == "deny"
    assert "timeout" in reason.lower()
    assert calls == ["POST", "GET", "POST"]
    assert timeouts == [1.0, hook.EXPIRY_CLEANUP_TIMEOUT_SECONDS]


def test_error_response_from_hook_is_explicit_codex_deny(monkeypatch, capsys) -> None:
    monkeypatch.setattr(hook, "_read_input", _event)
    monkeypatch.setattr(hook, "_request", lambda *args, **kwargs: (_ for _ in ()).throw(hook.GateError("offline")))
    monkeypatch.setattr(sys, "argv", ["codex-hook"])
    monkeypatch.setenv("AIXION_API_BASE_URL", ENV["AIXION_API_BASE_URL"])
    monkeypatch.setenv("AIXION_AGENT_ID", ENV["AIXION_AGENT_ID"])
    monkeypatch.setenv("AIXION_AGENT_TOKEN", ENV["AIXION_AGENT_TOKEN"])
    monkeypatch.setenv("AIXION_ALLOW_AGENT_TOKEN_ENV", "true")
    monkeypatch.setenv("AIXION_PROJECT_ID", ENV["AIXION_PROJECT_ID"])
    assert hook.main([]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
    assert output["hookSpecificOutput"]["decision"]["behavior"] == "deny"


def test_process_environment_token_requires_explicit_opt_in() -> None:
    with pytest.raises(hook.GateError, match="Environment agent tokens are disabled"):
        hook._agent_token({"AIXION_AGENT_TOKEN": "test-token"}, {})

    assert hook._agent_token(
        {
            "AIXION_AGENT_TOKEN": "test-token",
            "AIXION_ALLOW_AGENT_TOKEN_ENV": "true",
        },
        {},
    ) == "test-token"


def test_setup_and_remove_preserve_unrelated_codex_hooks(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    codex_dir = workspace / ".codex"
    codex_dir.mkdir()
    hooks_path = codex_dir / "hooks.json"
    hooks_path.write_text(json.dumps({"hooks": {"SessionStart": [{"matcher": "startup", "hooks": []}]}}))
    config_path, returned_hooks_path = setup.install_workspace(
        workspace, api_base_url="https://api.example.test",
        agent_id="agent-codex-test", project_id="project-codex-test", home=home,
    )
    assert returned_hooks_path == hooks_path
    assert config_path.stat().st_mode & 0o077 == 0
    installed = json.loads(hooks_path.read_text())
    assert "SessionStart" in installed["hooks"]
    assert len(installed["hooks"]["PermissionRequest"]) == 1
    assert setup.remove_workspace(workspace, home=home) == (True, True)
    remaining = json.loads(hooks_path.read_text())
    assert remaining == {"hooks": {"SessionStart": [{"matcher": "startup", "hooks": []}]}}


def test_remove_rejects_symlinked_home_without_mutating_managed_files(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    config_path, hooks_path = setup.install_workspace(
        workspace,
        api_base_url="https://api.example.test",
        agent_id="agent-codex-test",
        project_id="project-codex-test",
        home=home,
    )
    original_hooks = hooks_path.read_bytes()
    home_alias = tmp_path / "home-alias"
    home_alias.symlink_to(home, target_is_directory=True)

    with pytest.raises(setup.SetupError, match="Home directory must be a real directory"):
        setup.remove_workspace(workspace, home=home_alias)

    assert hooks_path.read_bytes() == original_hooks
    assert config_path.is_file()


def test_setup_rejects_remote_http(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    with pytest.raises(setup.SetupError, match="HTTPS"):
        setup.install_workspace(workspace, api_base_url="http://api.example.test",
                                agent_id="agent", project_id="project", home=home)


def test_failed_hook_install_does_not_delete_existing_workspace_config(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    config_path, hooks_path = setup.install_workspace(
        workspace, api_base_url="https://api.example.test",
        agent_id="agent-codex-test", project_id="project-codex-test", home=home,
    )
    hooks_path.unlink()
    original_atomic_write = setup._atomic_write

    def fail_hook_file_write(path: Path, data: bytes, mode: int) -> None:
        if path == hooks_path:
            raise OSError("simulated filesystem failure")
        original_atomic_write(path, data, mode)

    monkeypatch.setattr(setup, "_atomic_write", fail_hook_file_write)
    with pytest.raises(OSError, match="simulated filesystem failure"):
        setup.install_workspace(
            workspace, api_base_url="https://api.example.test",
            agent_id="agent-codex-test", project_id="project-codex-test", home=home,
        )
    assert config_path.exists()
