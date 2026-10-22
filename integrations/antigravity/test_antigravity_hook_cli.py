from __future__ import annotations

import importlib.util
import hashlib
import io
import json
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

    def request(method: str, url: str, agent_id: str, token: str, body: dict | None = None) -> dict:
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

    def request(method: str, url: str, _agent_id: str, _token: str, body: dict | None = None) -> dict:
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

    def request(method: str, url: str, _agent_id: str, _token: str, body: dict | None = None) -> dict:
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

    def request(method: str, url: str, agent_id: str, token: str, body: dict | None = None) -> dict:
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

    def request(method: str, url: str, _agent_id: str, _token: str, body: dict | None = None) -> dict:
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

    def request(method: str, url: str, _agent_id: str, _token: str, body: dict | None = None) -> dict:
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
