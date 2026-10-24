from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest

HOOK_PATH = Path(__file__).parents[2] / "integrations" / "antigravity" / "aixion_pre_tool_hook.py"
SPEC = importlib.util.spec_from_file_location("aixion_pre_tool_hook", HOOK_PATH)
assert SPEC and SPEC.loader
hook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hook)
_raw_hook_run = hook.run


def _call_hook(payload: dict, environ: dict | None = None) -> dict:
    # Fixtures mirror Antigravity's documented PreToolUse common fields and
    # trajectory step index unless a test explicitly supplies malformed input.
    payload = {"conversationId": "conversation-test", "stepIdx": 1, **payload}
    return _raw_hook_run(payload, environ)

ENV = {
    "AIXION_API_BASE_URL": "http://127.0.0.1:8012",
    "AIXION_AGENT_ID": "agent-test",
    "AIXION_AGENT_TOKEN": "test-token",
    "AIXION_ALLOW_AGENT_TOKEN_ENV": "true",
    "AIXION_PROJECT_ID": "project-test",
}


def test_hook_input_rejects_duplicate_nested_tool_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = (
        b'{"conversationId":"session","stepIdx":1,"toolCall":{"name":"run_command",'
        b'"args":{"command":"echo safe","command":"rm -rf /"}}}'
    )
    monkeypatch.setattr(hook.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw)))

    with pytest.raises(hook.GateError, match="invalid JSON"):
        hook._read_input()


def test_http_response_rejects_duplicate_approval_identity() -> None:
    response = SimpleNamespace(
        status=200,
        headers={},
        body=b'{"id":"approval-1","source_agent_id":"expected",'
        b'"source_agent_id":"attacker-controlled"}',
    )
    with patch.object(hook, "deadline_request", return_value=response):
        with pytest.raises(hook.AmbiguousRequestError, match="invalid response data"):
            hook._request(
                "GET",
                "http://127.0.0.1:8012/approvals/approval-1",
                "agent-test",
                "token",
            )


def test_configuration_rejects_duplicate_scope_key(tmp_path: Path) -> None:
    config_path = tmp_path / "antigravity.json"
    config_path.write_text(
        '{"api_base_url":"https://api.example","agent_id":"agent-a",'
        '"project_id":"project-a","project_id":"project-b"}',
        encoding="utf-8",
    )
    config_path.chmod(0o600)

    with pytest.raises(hook.GateError, match="unreadable or invalid"):
        hook._configuration({"AIXION_AGENT_CONFIG": str(config_path)}, {})


def test_http_request_advertises_approval_protocol_v1() -> None:
    captured: dict[str, object] = {}

    def deadline_request(method, url, headers, body, timeout, *, max_response_bytes):
        captured.update(
            method=method,
            url=url,
            headers=headers,
            body=body,
            timeout=timeout,
            max_response_bytes=max_response_bytes,
        )
        return SimpleNamespace(status=200, headers={}, body=b"{}")

    with patch.object(hook, "deadline_request", side_effect=deadline_request):
        assert hook._request(
            "GET",
            "http://127.0.0.1:8012/agents/approvals/id/decision",
            ENV["AIXION_AGENT_ID"],
            ENV["AIXION_AGENT_TOKEN"],
        ) == {}

    assert captured["method"] == "GET"
    assert captured["url"] == "http://127.0.0.1:8012/agents/approvals/id/decision"
    assert captured["headers"]["X-Aixion-Approval-Protocol"] == "1"
    assert captured["timeout"] == 10
    assert captured["max_response_bytes"] == hook.MAX_RESPONSE_BYTES


@pytest.mark.parametrize("timeout", ["invalid", "0", "-1", "901"])
def test_invalid_approval_timeout_denies_before_network(timeout: str) -> None:
    with patch.object(hook, "_request") as request:
        result = _call_hook(
            {"toolCall": {"name": "run_command", "args": {"CommandLine": "echo safe"}}},
            {**ENV, "AIXION_APPROVAL_TIMEOUT_SECONDS": timeout},
        )

    assert result["decision"] == "deny"
    assert "timeout" in result["reason"].lower()
    request.assert_not_called()


@pytest.mark.parametrize(
    "identity_fields",
    [
        {"conversationId": "s" * 201, "sessionId": "valid-fallback"},
        {"conversationId": 42, "sessionId": "valid-fallback"},
        {"conversationId": "   ", "sessionId": "valid-fallback"},
        {"sessionId": "s" * 201},
        {"sessionId": None},
        {"sessionId": "\t  \n"},
    ],
)
def test_invalid_session_identity_denies_before_credentials_or_network(identity_fields: dict) -> None:
    payload = {
        **identity_fields,
        "stepIdx": 1,
        "toolCall": {"name": "run_command", "args": {"CommandLine": "echo safe"}},
    }

    with patch.object(hook, "_request") as request, patch.object(hook, "_agent_token") as token:
        result = _raw_hook_run(payload, ENV)

    assert result["decision"] == "deny"
    assert "session" in result["reason"].lower()
    token.assert_not_called()
    request.assert_not_called()


def test_valid_session_fallback_is_preserved_exactly() -> None:
    created_body: dict = {}

    def request(method: str, _url: str, agent_id: str, _token: str, body: dict | None = None, **_kwargs: object) -> dict:
        nonlocal created_body
        if method == "POST":
            created_body = body or {}
            return {
                **created_body,
                "id": "approval-test",
                "status": "PENDING_REVIEW",
                "source_provider": "ANTIGRAVITY",
                "source_agent_id": agent_id,
                "source_agent_name": "Antigravity test",
            }
        return {**created_body, "id": "approval-test", "status": "DENIED",
                "source_provider": "ANTIGRAVITY", "source_agent_id": agent_id}

    session_id = "  session-fallback-exact  "
    payload = {
        "sessionId": session_id,
        "stepIdx": 1,
        "toolCall": {"name": "run_command", "args": {"CommandLine": "echo safe"}},
    }
    with patch.object(hook, "_request", side_effect=request):
        result = _raw_hook_run(payload, ENV)

    assert result["decision"] == "deny"
    assert created_body["source_session_id"] == session_id


def _approved_record(body: dict) -> dict:
    record = {
        **body,
        "id": "approval-test",
        "status": "APPROVED",
        "risk": {"level": "MEDIUM", "reasons": [], "blocked": False, "required_actions": []},
        "source_provider": "ANTIGRAVITY",
        "source_agent_id": "agent-test",
        "source_agent_name": "Antigravity test",
        "source_task_url": None,
        "created_by_user_id": None,
        "verified_source": True,
        "approved_payload_hash": None,
    }
    record["approved_payload_hash"] = hook._payload_hash(record)
    return record


def test_version_two_hash_binds_workspace_and_risk() -> None:
    record = _approved_record({
        "project_id": "project-test",
        "work_order_id": None,
        "title": "Run command",
        "summary": "Review command",
        "agent_name": "Antigravity test",
        "target_branch": "main",
        "files": [],
        "test_plan": [],
        "rollback_plan": "",
        "source_session_id": "session-test",
        "source_invocation_id": "invocation-test",
    })
    record.update({"approved_payload_hash_version": 2, "workspace_id": "workspace-test"})
    digest = hook._payload_hash(record)

    changed_risk = {**record, "risk": {**record["risk"], "level": "HIGH"}}
    changed_workspace = {**record, "workspace_id": "workspace-other"}
    assert digest.startswith("sha256:v2:")
    assert digest != hook._payload_hash(changed_risk)
    assert digest != hook._payload_hash(changed_workspace)


def test_unknown_hash_version_fails_closed() -> None:
    with pytest.raises(hook.GateError, match="Unsupported approval payload hash version"):
        hook._payload_hash({"approved_payload_hash_version": 3})


def test_explicit_read_only_tool_passes_through() -> None:
    result = _call_hook({"toolCall": {"name": "view_file", "args": {"AbsolutePath": "/tmp/a"}}}, {})
    assert result["decision"] == "allow"


@pytest.mark.parametrize("tool_name,args", [
    ("search_web", {"query": "debug access_token=very-secret-value"}),
    ("read_url_content", {"url": "https://example.test/?api_key=very-secret-value"}),
    ("grep_search", {"Query": "password: 'very-secret-value'"}),
])
def test_read_only_tool_with_secret_like_argument_is_denied_before_network(
    tool_name: str, args: dict
) -> None:
    with patch.object(hook, "_request") as request:
        result = _call_hook({"toolCall": {"name": tool_name, "args": args}}, {})

    assert result["decision"] == "deny"
    assert "secret-like" in result["reason"]
    request.assert_not_called()


@pytest.mark.parametrize("tool_name,args", [
    ("view_file", {"AbsolutePath": "/workspace/.env"}),
    ("view_file", {"AbsolutePath": r"C:\Users\demo\.ssh\id_ed25519"}),
    ("find_by_name", {"SearchDirectory": "/workspace", "Pattern": ".env*"}),
    ("grep_search", {"SearchPath": "/workspace/credentials/prod.json", "Query": "client_id"}),
    ("list_dir", {"DirectoryPath": "/workspace/.aws"}),
])
def test_read_only_tool_targets_secret_path_and_requires_mobile_approval(tool_name: str, args: dict) -> None:
    calls: list[str] = []
    created_body: dict = {}

    def request(method: str, _url: str, agent_id: str, _token: str, body: dict | None = None, **_kwargs: object) -> dict:
        nonlocal created_body
        calls.append(method)
        if method == "POST":
            created_body = body or {}
            return {
                **(body or {}),
                "id": "approval-test",
                "status": "PENDING_REVIEW",
                "project_id": "project-test",
                "source_provider": "ANTIGRAVITY",
                "source_agent_id": agent_id,
                "source_agent_name": "Antigravity test",
            }
        return {
            **(body or {}),
            "id": "approval-test",
            "status": "DENIED",
            "project_id": "project-test",
            "work_order_id": None,
            "title": "Antigravity approval: " + tool_name,
            "summary": "Review the exact Antigravity tool and arguments before this invocation continues.",
            "agent_name": "Antigravity",
            "target_branch": "current-workspace",
            "files": [{
                "path": f"antigravity://tool/{tool_name}",
                "change_type": "tool_call",
                "diff": json.dumps(args, sort_keys=True, separators=(",", ":")),
                "new_content": None,
            }],
            "test_plan": ["Confirm the tool and arguments match the intended task"],
            "rollback_plan": "Deny this request if uncertain. Approval authorizes only this displayed invocation.",
            "source_agent_id": agent_id,
            "source_agent_name": "Antigravity test",
            "source_session_id": created_body["source_session_id"],
            "source_invocation_id": created_body["source_invocation_id"],
            "risk": {"level": "MEDIUM", "reasons": [], "blocked": False, "required_actions": []},
            "source_provider": "ANTIGRAVITY",
            "source_task_url": None,
            "created_by_user_id": None,
            "verified_source": True,
            "approved_payload_hash": None,
        }

    with patch.object(hook, "_request", side_effect=request):
        result = _call_hook({"toolCall": {"name": tool_name, "args": args}}, ENV)

    assert calls == ["POST", "GET"]
    assert result["decision"] == "deny"
    assert "DENIED" in result["reason"]


@pytest.mark.parametrize("tool_name,args", [
    ("view_file", {"AbsolutePath": "/workspace/.env.example"}),
    ("view_file", {"AbsolutePath": "/workspace/src/main.py"}),
    ("find_by_name", {"SearchDirectory": "/workspace", "Pattern": "*.py"}),
])
def test_read_only_tool_with_non_secret_path_still_passes_through(tool_name: str, args: dict) -> None:
    with patch.object(hook, "_request") as request:
        result = _call_hook({"toolCall": {"name": tool_name, "args": args}}, {})
    assert result["decision"] == "allow"
    request.assert_not_called()


@pytest.mark.parametrize(
    "tool_name,args",
    [
        ("view_file", {}),
        ("view_file", {"path": "/workspace/.env"}),
        ("view_file", {"AbsolutePath": "/workspace/src/main.py", "path": "/workspace/.env"}),
        ("view_file", {"AbsolutePath": "/workspace/src/main.py", "absolute_path": None}),
        ("list_dir", {"DirectoryPath": "  "}),
        ("find_by_name", {"SearchDirectory": 42, "Pattern": "*.txt"}),
        ("grep_search", {"SearchPath": None, "Query": "hello"}),
    ],
)
def test_read_only_file_tool_with_missing_or_invalid_documented_path_requires_mobile_decision(
    tool_name: str, args: dict
) -> None:
    calls: list[str] = []
    created_body: dict = {}

    def request(
        method: str,
        _url: str,
        _agent_id: str,
        _token: str,
        body: dict | None = None,
        **_kwargs: object,
    ) -> dict:
        nonlocal created_body
        calls.append(method)
        if method == "POST":
            created_body = body or {}
            return {**_approved_record(created_body), "status": "PENDING_REVIEW"}
        return {**_approved_record(created_body), "status": "DENIED"}

    with patch.object(hook, "_request", side_effect=request):
        result = _call_hook({"toolCall": {"name": tool_name, "args": args}}, ENV)

    assert calls == ["POST", "GET"]
    assert result["decision"] == "deny"
    assert "DENIED" in result["reason"]
    assert json.loads(created_body["files"][0]["diff"]) == args


def test_unknown_tool_requires_mobile_approval_then_allows_exact_snapshot() -> None:
    created_body: dict = {}

    def request(method: str, url: str, agent_id: str, token: str, body: dict | None = None, **_kwargs: object) -> dict:
        nonlocal created_body
        assert agent_id == ENV["AIXION_AGENT_ID"]
        assert token == ENV["AIXION_AGENT_TOKEN"]
        if method == "POST" and url.endswith("/consume"):
            return {"consumed": True}
        if method == "POST":
            created_body = body or {}
            return {
                **created_body,
                "id": "approval-test",
                "status": "PENDING_REVIEW",
                "source_provider": "ANTIGRAVITY",
                "source_agent_id": agent_id,
                "source_agent_name": "Antigravity test",
            }
        return _approved_record(created_body)

    with patch.object(hook, "_request", side_effect=request), patch.object(hook.time, "sleep"):
        result = _call_hook(
            {"toolCall": {"name": "new_sensitive_tool", "args": {"message": "café"}}, "conversationId": "conv"},
            ENV,
        )

    assert result["decision"] == "auto_approve"
    assert created_body["source_session_id"] == "conv"
    assert created_body["source_invocation_id"].startswith("antigravity:v1:")


def test_duplicate_approval_delivery_claim_denies_tool() -> None:
    created_body: dict = {}

    def request(method: str, url: str, agent_id: str, _token: str, body: dict | None = None, **_kwargs: object) -> dict:
        nonlocal created_body
        if method == "POST" and url.endswith("/consume"):
            return {"consumed": False}
        if method == "POST":
            created_body = body or {}
            return {
                **created_body,
                "id": "approval-test",
                "status": "PENDING_REVIEW",
                "source_provider": "ANTIGRAVITY",
                "source_agent_id": agent_id,
                "source_agent_name": "Antigravity test",
            }
        return _approved_record(created_body)

    with patch.object(hook, "_request", side_effect=request), patch.object(hook.time, "sleep"):
        result = _call_hook(
            {"toolCall": {"name": "new_sensitive_tool", "args": {"message": "café"}}},
            ENV,
        )

    assert result["decision"] == "deny"
    assert "already claimed" in result["reason"]


def test_ambiguous_delivery_claim_response_denies_without_retry() -> None:
    calls: list[str] = []
    created_body: dict = {}

    def request(method: str, url: str, agent_id: str, _token: str, body: dict | None = None, **_kwargs: object) -> dict:
        nonlocal created_body
        if method == "POST" and url.endswith("/consume"):
            calls.append("consume")
            # The server may have durably inserted the unique claim before the
            # connection failed. The connector must not retry an ambiguous allow.
            raise hook.GateError("delivery claim response was lost")
        if method == "POST":
            calls.append("create")
            created_body = body or {}
            return {
                **created_body,
                "id": "approval-test",
                "status": "PENDING_REVIEW",
                "source_provider": "ANTIGRAVITY",
                "source_agent_id": agent_id,
                "source_agent_name": "Antigravity test",
            }
        calls.append("read")
        return _approved_record(created_body)

    with patch.object(hook, "_request", side_effect=request), patch.object(hook.time, "sleep"):
        result = _call_hook(
            {"toolCall": {"name": "run_command", "args": {"CommandLine": "echo safe"}}},
            ENV,
        )

    assert result["decision"] == "deny"
    assert "could not be claimed safely" in result["reason"]
    assert calls == ["create", "read", "consume"]


def test_revoked_connector_at_consume_denies_approved_tool_call() -> None:
    calls: list[str] = []
    created_body: dict = {}

    def request(method: str, url: str, agent_id: str, _token: str, body: dict | None = None, **_kwargs: object) -> dict:
        nonlocal created_body
        if method == "POST" and url.endswith("/consume"):
            calls.append("consume")
            raise hook.GateError("external agent token is revoked")
        if method == "POST":
            calls.append("create")
            created_body = body or {}
            return {
                **created_body,
                "id": "approval-test",
                "status": "PENDING_REVIEW",
                "source_provider": "ANTIGRAVITY",
                "source_agent_id": agent_id,
                "source_agent_name": "Antigravity test",
            }
        calls.append("read")
        return _approved_record(created_body)

    with patch.object(hook, "_request", side_effect=request), patch.object(hook.time, "sleep"):
        result = _call_hook(
            {"toolCall": {"name": "run_command", "args": {"CommandLine": "echo safe"}}},
            ENV,
        )

    assert result["decision"] == "deny"
    assert "could not be claimed safely" in result["reason"]
    assert calls == ["create", "read", "consume"]


def test_ambiguous_delivery_claim_is_reported_without_allowing_tool() -> None:
    calls: list[str] = []
    created_body: dict = {}
    failure_body: dict = {}

    def request(method: str, url: str, agent_id: str, _token: str, body: dict | None = None, **_kwargs: object) -> dict:
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
            return {
                **created_body,
                "id": "approval-test",
                "status": "PENDING_REVIEW",
                "source_provider": "ANTIGRAVITY",
                "source_agent_id": agent_id,
                "source_agent_name": "Antigravity test",
            }
        calls.append("read")
        return _approved_record(created_body)

    with patch.object(hook, "_request", side_effect=request), patch.object(hook.time, "sleep"):
        result = _call_hook(
            {"toolCall": {"name": "run_command", "args": {"CommandLine": "echo safe"}}},
            ENV,
        )

    assert result["decision"] == "deny"
    assert calls == ["create", "read", "consume", "report"]
    assert failure_body == {
        "source_invocation_id": created_body["source_invocation_id"],
        "approved_payload_hash": _approved_record(created_body)["approved_payload_hash"],
        "reason_code": "CLAIM_RESPONSE_AMBIGUOUS",
    }


def test_restart_retry_can_resume_existing_approved_invocation() -> None:
    calls: list[str] = []
    approved: dict = {}

    def request(method: str, url: str, agent_id: str, _token: str, body: dict | None = None, **_kwargs: object) -> dict:
        nonlocal approved
        calls.append(method)
        if method == "POST" and url.endswith("/consume"):
            return {"consumed": True}
        if method == "POST":
            approved = _approved_record(body or {})
            return approved
        return approved

    with patch.object(hook, "_request", side_effect=request), patch.object(hook.time, "sleep"):
        result = _call_hook(
            {"toolCall": {"name": "new_sensitive_tool", "args": {"message": "restart"}}},
            ENV,
        )

    assert result["decision"] == "auto_approve"
    assert calls == ["POST", "GET", "POST"]


def test_sensitive_tool_without_step_identity_is_denied_before_network() -> None:
    with patch.object(hook, "_request") as request:
        result = _raw_hook_run(
            {"conversationId": "conversation-test", "toolCall": {"name": "run_command", "args": {}}},
            ENV,
        )

    assert result["decision"] == "deny"
    assert "step identity" in result["reason"]
    request.assert_not_called()


def test_denied_mobile_decision_blocks_tool() -> None:
    def request(method: str, url: str, agent_id: str, token: str, body: dict | None = None, **_kwargs: object) -> dict:
        if method == "POST":
            return {"id": "approval-test", "status": "PENDING_REVIEW"}
        return {"status": "DENIED", "approved_payload_hash": None}

    with patch.object(hook, "_request", side_effect=request):
        result = _call_hook({"toolCall": {"name": "run_command", "args": {"CommandLine": "echo safe"}}}, ENV)
    assert result["decision"] == "deny"


def test_network_failure_while_waiting_emits_an_explicit_deny(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def request(method: str, _url: str, agent_id: str, _token: str, body: dict | None = None, **_kwargs: object) -> dict:
        if method == "POST":
            return {
                **(body or {}),
                "id": "approval-test",
                "status": "PENDING_REVIEW",
                "source_provider": "ANTIGRAVITY",
                "source_agent_id": agent_id,
                "source_agent_name": "Antigravity test",
            }
        raise hook.GateError("Control Tower is unavailable")

    monkeypatch.setenv("AIXION_API_BASE_URL", ENV["AIXION_API_BASE_URL"])
    monkeypatch.setenv("AIXION_AGENT_ID", ENV["AIXION_AGENT_ID"])
    monkeypatch.setenv("AIXION_AGENT_TOKEN", ENV["AIXION_AGENT_TOKEN"])
    monkeypatch.setenv("AIXION_ALLOW_AGENT_TOKEN_ENV", "true")
    monkeypatch.setenv("AIXION_PROJECT_ID", ENV["AIXION_PROJECT_ID"])
    monkeypatch.setattr(
        hook,
        "_read_input",
        lambda: {
            "conversationId": "conversation-test",
            "stepIdx": 1,
            "toolCall": {"name": "run_command", "args": {"CommandLine": "echo safe"}},
        },
    )
    monkeypatch.setattr(hook, "_request", request)

    exit_code = hook.main([])

    emitted = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert emitted["decision"] == "deny"
    assert "GateError" in emitted["reason"]


def test_ambiguous_decision_poll_reconnects_and_resumes_same_approval() -> None:
    created_body: dict = {}
    get_calls = 0
    calls: list[tuple[str, str]] = []

    def request(method: str, url: str, agent_id: str, _token: str, body: dict | None = None, **_kwargs: object) -> dict:
        nonlocal created_body, get_calls
        calls.append((method, url))
        if method == "POST" and url.endswith("/consume"):
            return {"consumed": True}
        if method == "POST":
            created_body = body or {}
            return {
                **created_body,
                "id": "approval-test",
                "status": "PENDING_REVIEW",
                "source_provider": "ANTIGRAVITY",
                "source_agent_id": agent_id,
                "source_agent_name": "Antigravity test",
            }
        get_calls += 1
        if get_calls == 1:
            raise hook.AmbiguousRequestError("temporary network loss")
        return _approved_record(created_body)

    with (
        patch.object(hook, "_request", side_effect=request),
        patch.object(hook.time, "sleep") as sleep,
    ):
        result = _call_hook(
            {"toolCall": {"name": "run_command", "args": {"CommandLine": "echo safe"}}},
            ENV,
        )

    assert result["decision"] == "auto_approve"
    assert get_calls == 2
    assert [method for method, _ in calls] == ["POST", "GET", "GET", "POST"]
    sleep.assert_called_once_with(hook.POLL_INTERVAL_SECONDS)


def test_repeated_ambiguous_decision_polls_back_off_and_expire_fail_closed() -> None:
    calls: list[tuple[str, str]] = []
    sleeps: list[int] = []
    clock = [0.0]

    def request(method: str, url: str, agent_id: str, _token: str, body: dict | None = None, **_kwargs: object) -> dict:
        calls.append((method, url))
        if method == "POST" and url.endswith("/agents/approvals"):
            return {
                **(body or {}),
                "id": "approval-test",
                "status": "PENDING_REVIEW",
                "source_provider": "ANTIGRAVITY",
                "source_agent_id": agent_id,
                "source_agent_name": "Antigravity test",
            }
        if method == "GET":
            raise hook.AmbiguousRequestError("temporary network loss")
        if url.endswith("/expire"):
            return {
                "id": "approval-test",
                "project_id": ENV["AIXION_PROJECT_ID"],
                "status": "EXPIRED",
                "source_provider": "ANTIGRAVITY",
                "source_agent_id": agent_id,
            }
        raise AssertionError(f"Unexpected request: {method} {url}")

    def monotonic() -> float:
        return clock[0]

    def sleep(seconds: int) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    with (
        patch.object(hook, "_request", side_effect=request),
        patch.object(hook.time, "monotonic", side_effect=monotonic),
        patch.object(hook.time, "sleep", side_effect=sleep),
    ):
        result = _call_hook(
            {"toolCall": {"name": "run_command", "args": {"CommandLine": "echo safe"}}},
            {**ENV, "AIXION_APPROVAL_TIMEOUT_SECONDS": "7"},
        )

    assert result["decision"] == "deny"
    assert sleeps == [2, 4, 1]
    assert sum(method == "GET" for method, _ in calls) == 3
    assert calls[-1][0] == "POST" and calls[-1][1].endswith("/expire")
    assert not any(url.endswith("/consume") for _, url in calls)


def test_approval_poll_identity_mismatch_cannot_authorize_tool() -> None:
    created_body: dict = {}

    def request(method: str, _url: str, agent_id: str, _token: str, body: dict | None = None, **_kwargs: object) -> dict:
        nonlocal created_body
        if method == "POST":
            created_body = body or {}
            return {
                **created_body,
                "id": "approval-test",
                "status": "PENDING_REVIEW",
                "source_provider": "ANTIGRAVITY",
                "source_agent_id": agent_id,
                "source_agent_name": "Antigravity test",
            }
        response = _approved_record(created_body)
        response["id"] = "approval-from-another-request"
        return response

    with patch.object(hook, "_request", side_effect=request):
        result = _call_hook(
            {"toolCall": {"name": "run_command", "args": {"CommandLine": "echo safe"}}},
            ENV,
        )

    assert result["decision"] == "deny"
    assert "different approval" in result["reason"]


def test_approval_created_for_different_invocation_is_not_polled() -> None:
    calls: list[str] = []

    def request(method: str, _url: str, agent_id: str, _token: str, body: dict | None = None, **_kwargs: object) -> dict:
        calls.append(method)
        if method == "POST":
            return {
                **json.loads(json.dumps(body or {})),
                "id": "approval-test",
                "status": "PENDING_REVIEW",
                "source_provider": "ANTIGRAVITY",
                "source_agent_id": agent_id,
                "source_agent_name": "Antigravity test",
            }
        raise AssertionError("A mismatched approval must not be polled")

    def mismatched_request(method: str, url: str, agent_id: str, token: str, body: dict | None = None, **_kwargs: object) -> dict:
        record = request(method, url, agent_id, token, body)
        if method == "POST":
            record["files"][0]["diff"] = '{"CommandLine":"echo different"}'
        return record

    with patch.object(hook, "_request", side_effect=mismatched_request):
        result = _call_hook(
            {"toolCall": {"name": "run_command", "args": {"CommandLine": "echo original"}}},
            ENV,
        )

    assert result["decision"] == "deny"
    assert "does not match the original tool invocation" in result["reason"]
    assert calls == ["POST"]


def test_changed_approved_payload_is_denied_even_with_a_matching_hash() -> None:
    created_body: dict = {}

    def request(method: str, _url: str, agent_id: str, _token: str, body: dict | None = None, **_kwargs: object) -> dict:
        nonlocal created_body
        if method == "POST":
            created_body = body or {}
            return {
                **created_body,
                "id": "approval-test",
                "status": "PENDING_REVIEW",
                "source_provider": "ANTIGRAVITY",
                "source_agent_id": agent_id,
                "source_agent_name": "Antigravity test",
            }
        changed_body = json.loads(json.dumps(created_body))
        changed_body["files"][0]["diff"] = '{"CommandLine":"echo changed"}'
        return _approved_record(changed_body)

    with patch.object(hook, "_request", side_effect=request):
        result = _call_hook(
            {"toolCall": {"name": "run_command", "args": {"CommandLine": "echo original"}}},
            ENV,
        )

    assert result["decision"] == "deny"
    assert "payload changed" in result["reason"]


def test_creation_response_with_wrong_project_is_not_polled() -> None:
    calls: list[str] = []

    def request(method: str, _url: str, agent_id: str, _token: str, body: dict | None = None, **_kwargs: object) -> dict:
        calls.append(method)
        return {
            **(body or {}),
            "id": "approval-test",
            "status": "PENDING_REVIEW",
            "source_provider": "ANTIGRAVITY",
            "source_agent_id": agent_id,
            "project_id": "different-project",
        }

    with patch.object(hook, "_request", side_effect=request):
        result = _call_hook(
            {"toolCall": {"name": "run_command", "args": {"CommandLine": "echo safe"}}},
            ENV,
        )

    assert result["decision"] == "deny"
    assert calls == ["POST"]


def test_rate_limited_decision_poll_retries_after_server_delay() -> None:
    created_body: dict = {}
    get_calls = 0

    def request(method: str, url: str, agent_id: str, token: str, body: dict | None = None, **_kwargs: object) -> dict:
        nonlocal created_body, get_calls
        if method == "POST" and url.endswith("/consume"):
            return {"consumed": True}
        if method == "POST":
            created_body = body or {}
            return {
                **created_body,
                "id": "approval-test",
                "status": "PENDING_REVIEW",
                "source_provider": "ANTIGRAVITY",
                "source_agent_id": agent_id,
                "source_agent_name": "Antigravity test",
            }
        get_calls += 1
        if get_calls == 1:
            raise hook.RateLimitError(7)
        return _approved_record(created_body)

    with (
        patch.object(hook, "_request", side_effect=request),
        patch.object(hook.time, "monotonic", return_value=0.0),
        patch.object(hook.time, "sleep") as sleep,
    ):
        result = _call_hook(
            {"toolCall": {"name": "run_command", "args": {"CommandLine": "echo safe"}}},
            ENV,
        )

    assert result["decision"] == "auto_approve"
    sleep.assert_called_once_with(7)
    assert get_calls == 2


def test_http_429_retry_after_header_is_parsed() -> None:
    response = SimpleNamespace(status=429, headers={"retry-after": "17"}, body=b"rate limited")
    with patch.object(hook, "deadline_request", return_value=response):
        with pytest.raises(hook.RateLimitError) as caught:
            hook._request("GET", "http://127.0.0.1:8012/decision", "agent-test", "test-token")

    assert caught.value.retry_after_seconds == 17


def test_timeout_expires_pending_server_approval_and_denies_tool() -> None:
    calls: list[tuple[str, str]] = []
    created_body: dict = {}

    def request(method: str, url: str, agent_id: str, token: str, body: dict | None = None, **_kwargs: object) -> dict:
        nonlocal created_body
        calls.append((method, url))
        if method == "POST" and url.endswith("/agents/approvals"):
            created_body = body or {}
            return {
                **json.loads(json.dumps(created_body)),
                "id": "approval-test",
                "status": "PENDING_REVIEW",
                "source_provider": "ANTIGRAVITY",
                "source_agent_id": agent_id,
                "source_agent_name": "Antigravity test",
            }
        if method == "GET":
            return {
                **json.loads(json.dumps(created_body)),
                "id": "approval-test",
                "status": "PENDING_REVIEW",
                "source_provider": "ANTIGRAVITY",
                "source_agent_id": agent_id,
                "source_agent_name": "Antigravity test",
            }
        if method == "POST" and url.endswith("/expire"):
            return {
                "id": "approval-test",
                "project_id": ENV["AIXION_PROJECT_ID"],
                "status": "EXPIRED",
                "source_provider": "ANTIGRAVITY",
                "source_agent_id": agent_id,
            }
        raise AssertionError(f"Unexpected request: {method} {url}")

    with (
        patch.object(hook, "_request", side_effect=request),
        patch.object(hook.time, "monotonic", side_effect=[0.0, 0.0, 1.2, 1.2, 1.2]),
        patch.object(hook.time, "sleep"),
    ):
        result = _call_hook(
            {"toolCall": {"name": "write_to_file", "args": {"TargetFile": "README.md"}}},
            {**ENV, "AIXION_APPROVAL_TIMEOUT_SECONDS": "1"},
        )

    assert result["decision"] == "deny"
    assert calls[-1][0] == "POST"
    assert calls[-1][1].endswith("/agents/approvals/approval-test/expire")


def test_approval_received_after_host_deadline_is_denied() -> None:
    created_body: dict = {}

    def request(method: str, _url: str, agent_id: str, _token: str, body: dict | None = None, **_kwargs: object) -> dict:
        nonlocal created_body
        if method == "POST":
            created_body = body or {}
            return {
                **created_body,
                "id": "approval-test",
                "status": "PENDING_REVIEW",
                "source_provider": "ANTIGRAVITY",
                "source_agent_id": agent_id,
                "source_agent_name": "Antigravity test",
            }
        return _approved_record(created_body)

    with (
        patch.object(hook, "_request", side_effect=request),
        patch.object(hook.time, "monotonic", side_effect=[0.0, 0.0, 2.0, 2.0]),
    ):
        result = _call_hook(
            {"toolCall": {"name": "run_command", "args": {"CommandLine": "echo safe"}}},
            {**ENV, "AIXION_APPROVAL_TIMEOUT_SECONDS": "1"},
        )

    assert result["decision"] == "deny"
    assert "after the host wait deadline" in result["reason"]


def test_approval_racing_with_timeout_is_denied() -> None:
    created_body: dict = {}
    calls: list[str] = []

    def request(method: str, url: str, agent_id: str, _token: str, body: dict | None = None, **_kwargs: object) -> dict:
        nonlocal created_body
        calls.append(method)
        if method == "POST" and not url.endswith("/expire"):
            created_body = body or {}
            return {
                **created_body,
                "id": "approval-test",
                "status": "PENDING_REVIEW",
                "source_provider": "ANTIGRAVITY",
                "source_agent_id": agent_id,
                "source_agent_name": "Antigravity test",
            }
        if method == "GET":
            return {
                **json.loads(json.dumps(created_body)),
                "id": "approval-test",
                "status": "PENDING_REVIEW",
                "source_provider": "ANTIGRAVITY",
                "source_agent_id": agent_id,
                "source_agent_name": "Antigravity test",
            }
        if method == "POST" and url.endswith("/expire"):
            return _approved_record(created_body)
        raise AssertionError(f"Unexpected request: {method} {url}")

    with (
        patch.object(hook, "_request", side_effect=request),
        patch.object(hook.time, "monotonic", side_effect=[0.0, 0.0, 1.2, 1.2, 1.2]),
    ):
        result = _call_hook(
            {"toolCall": {"name": "run_command", "args": {"CommandLine": "echo safe"}}},
            {**ENV, "AIXION_APPROVAL_TIMEOUT_SECONDS": "1"},
        )

    assert result["decision"] == "deny"
    assert result["reason"] == "Approval raced with the host timeout; the tool call was denied"
    assert calls == ["POST", "GET", "POST"]


@pytest.mark.parametrize("url", [
    "http://localhost.attacker.invalid",
    "http://127.0.0.1.attacker.invalid",
    "http://user@localhost",
])
def test_non_loopback_plain_http_is_rejected(url: str) -> None:
    with pytest.raises(hook.GateError):
        _call_hook(
            {"toolCall": {"name": "run_command", "args": {"CommandLine": "echo safe"}}},
            {**ENV, "AIXION_API_BASE_URL": url},
        )


@pytest.mark.parametrize("url", [
    "https://control.example.test/path?token=leak",
    "https://control.example.test/path#fragment",
    "https://control.example.test:invalid",
])
def test_api_url_query_fragment_and_invalid_port_are_rejected(url: str) -> None:
    with pytest.raises(hook.GateError):
        _call_hook(
            {"toolCall": {"name": "run_command", "args": {"CommandLine": "echo safe"}}},
            {**ENV, "AIXION_API_BASE_URL": url},
        )


def test_user_config_file_loads_non_secret_connection_settings() -> None:
    with tempfile.TemporaryDirectory() as directory:
        config_path = Path(directory) / "antigravity.json"
        config_path.write_text(json.dumps({
            "api_base_url": "https://control.example.test",
            "agent_id": "agent-from-config",
            "project_id": "project-from-config",
            "keychain_service": "com.aixion.test-agent",
        }))
        config_path.chmod(0o600)
        config = hook._configuration({"AIXION_AGENT_CONFIG": str(config_path)}, {})

    assert config["api_base_url"] == "https://control.example.test"
    assert config["agent_id"] == "agent-from-config"
    assert config["project_id"] == "project-from-config"
    assert config["keychain_service"] == "com.aixion.test-agent"


def test_config_file_requires_private_permissions() -> None:
    with tempfile.TemporaryDirectory() as directory:
        config_path = Path(directory) / "antigravity.json"
        config_path.write_text(json.dumps({
            "api_base_url": "https://control.example.test",
            "agent_id": "agent-from-config",
            "project_id": "project-from-config",
        }))
        config_path.chmod(0o644)
        with pytest.raises(hook.GateError, match="permissions"):
            hook._configuration({"AIXION_AGENT_CONFIG": str(config_path)}, {})


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="platform does not support no-follow opens")
def test_config_file_rejects_symlinks() -> None:
    with tempfile.TemporaryDirectory() as directory:
        target_path = Path(directory) / "target.json"
        target_path.write_text(json.dumps({
            "api_base_url": "https://control.example.test",
            "agent_id": "agent-from-config",
            "project_id": "project-from-config",
        }))
        target_path.chmod(0o600)
        link_path = Path(directory) / "antigravity.json"
        link_path.symlink_to(target_path)
        with pytest.raises(hook.GateError):
            hook._configuration({"AIXION_AGENT_CONFIG": str(link_path)}, {})


def test_preflight_health_omits_token_then_authenticates_connector_identity() -> None:
    config = {
        "api_base_url": "https://control.example.test",
        "agent_id": "agent-test",
        "project_id": "project-test",
    }

    def deadline_request(method, url, headers, body, timeout, *, max_response_bytes):
        assert method == "GET"
        assert body is None
        if url.endswith("/health"):
            assert "X-Aixion-Agent-Token" not in headers
            assert "X-Aixion-Agent-Id" not in headers
            assert timeout == 5
            assert max_response_bytes == hook.MAX_HEALTH_RESPONSE_BYTES
            content = b'{"status":"ok"}'
            status = 200
        else:
            assert url == (
                "https://control.example.test/agents/identity?provider=ANTIGRAVITY&project_id=project-test"
            )
            assert headers["X-Aixion-Agent-Token"] == "must-not-be-sent"
            assert headers["X-Aixion-Agent-Id"] == "agent-test"
            assert headers["X-Aixion-Approval-Protocol"] == "1"
            assert timeout == 10
            assert max_response_bytes == hook.MAX_RESPONSE_BYTES
            content = b'{"agent_id":"agent-test","provider":"ANTIGRAVITY",' \
                b'"workspace_id":"workspace-test","project_id":"project-test"}'
            status = 200
        return SimpleNamespace(status=status, headers={}, body=content)

    with (
        patch.object(hook, "_configuration", return_value=config),
        patch.object(hook, "_agent_token", return_value="must-not-be-sent"),
        patch.object(hook, "deadline_request", side_effect=deadline_request),
    ):
        result = hook.check_connection(ENV)

    assert result == {
        "api_base_url": "https://control.example.test",
        "agent_id": "agent-test",
        "project_id": "project-test",
    }


def test_preflight_rejects_authenticated_identity_mismatch() -> None:
    config = {
        "api_base_url": "https://control.example.test",
        "agent_id": "agent-test",
        "project_id": "project-test",
    }

    with (
        patch.object(hook, "_configuration", return_value=config),
        patch.object(hook, "_agent_token", return_value="test-token"),
        patch.object(
            hook,
            "deadline_request",
            return_value=SimpleNamespace(status=200, headers={}, body=b'{"status":"ok"}'),
        ),
        patch.object(hook, "_request", return_value={
            "agent_id": "agent-other",
            "provider": "ANTIGRAVITY",
            "workspace_id": "workspace-test",
            "project_id": "project-test",
        }),
    ):
        with pytest.raises(hook.GateError, match="identity, provider, or project scope"):
            hook.check_connection(ENV)


def test_macos_token_lookup_uses_keychain_account_and_service() -> None:
    config = {"agent_id": "agent-keychain", "keychain_service": "com.aixion.test-agent"}
    result = subprocess.CompletedProcess(args=[], returncode=0, stdout="secret-value\n", stderr="")
    with patch.object(hook.sys, "platform", "darwin"), patch.object(hook.subprocess, "run", return_value=result) as run:
        assert hook._agent_token({}, config) == "secret-value"
    assert run.call_args.args[0] == [
        "/usr/bin/security", "find-generic-password", "-s", "com.aixion.test-agent",
        "-a", "agent-keychain", "-w",
    ]


def test_temporary_token_file_requires_private_permissions() -> None:
    with tempfile.TemporaryDirectory() as directory:
        token_path = Path(directory) / "agent-token"
        token_path.write_text("test-token\n")
        token_path.chmod(0o600)
        assert hook._agent_token({"AIXION_AGENT_TOKEN_FILE": str(token_path)}, {}) == "test-token"
        token_path.chmod(0o644)
        with pytest.raises(hook.GateError, match="permissions"):
            hook._agent_token({"AIXION_AGENT_TOKEN_FILE": str(token_path)}, {})


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


def test_inline_secret_pattern_and_oversized_args_are_denied() -> None:
    fake_secret = "a" * 16
    secret = _call_hook(
        {"toolCall": {"name": "run_command", "args": {"CommandLine": f"curl --header 'Authorization: Bearer {fake_secret}'"}}},
        ENV,
    )
    oversized = _call_hook(
        {"toolCall": {"name": "write_to_file", "args": {"CodeContent": "x" * hook.MAX_ARGUMENTS_BYTES}}},
        ENV,
    )
    assert secret["decision"] == "deny"
    assert oversized["decision"] == "deny"


def test_preflight_cli_accepts_workspace_config_path(monkeypatch, capsys) -> None:
    with patch.object(hook, "check_connection", return_value={
        "api_base_url": "https://control.example.test",
        "agent_id": "agent-test",
        "project_id": "project-test",
    }) as check:
        monkeypatch.delenv("AIXION_AGENT_CONFIG", raising=False)
        assert hook.main(["--config", "/private/workspace-agent.json", "--check"]) == 0
    assert os.environ["AIXION_AGENT_CONFIG"] == "/private/workspace-agent.json"
    assert "preflight passed" in capsys.readouterr().out
    check.assert_called_once_with()
