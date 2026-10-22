from __future__ import annotations

import importlib.util
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

HOOK_PATH = Path(__file__).with_name("aixion_permission_request_hook.py")
SPEC = importlib.util.spec_from_file_location("aixion_codex_cli_hook", HOOK_PATH)
assert SPEC and SPEC.loader
hook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hook)


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

    def request(method: str, url: str, _config: dict, _token: str, body: dict | None = None) -> dict:
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

    def request(method: str, url: str, _config: dict, _token: str, body: dict | None = None) -> dict:
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

    def request(method: str, url: str, _config: dict, _token: str, body: dict | None = None) -> dict:
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
