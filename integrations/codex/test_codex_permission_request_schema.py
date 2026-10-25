from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

HOOK_PATH = Path(__file__).with_name("aixion_permission_request_hook.py")
SPEC = importlib.util.spec_from_file_location("aixion_codex_schema_hook", HOOK_PATH)
assert SPEC and SPEC.loader
hook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hook)


@pytest.mark.parametrize(
    ("phone_status", "expected"),
    [("APPROVED", "allow"), ("DENIED", "deny")],
)
def test_documented_permission_request_schema_routes_phone_decision(
    monkeypatch: pytest.MonkeyPatch,
    phone_status: str,
    expected: str,
) -> None:
    """Exercise the real hook gate with the documented event shape.

    PermissionRequest's current public schema does not list tool_use_id. This
    regression ensures the adapter can bind a one-shot callback and return the
    phone decision without relying on that undocumented field.
    """
    workspace = Path(__file__).resolve().parents[2]
    event = {
        "hook_event_name": "PermissionRequest",
        "session_id": "session-schema-contract",
        "cwd": str(workspace),
        "permission_mode": "default",
        "turn_id": "turn-schema-contract",
        "tool_name": "Bash",
        "tool_input": {
            "command": "mkdir /tmp/aixion-codex-schema-contract",
            "description": "Create a disposable test directory",
        },
    }
    env = {
        "AIXION_API_BASE_URL": "http://127.0.0.1:8001",
        "AIXION_AGENT_ID": "agent-schema-contract",
        "AIXION_PROJECT_ID": "project-schema-contract",
        "AIXION_CODEX_WORKSPACE_PATH": str(workspace),
        "AIXION_AGENT_TOKEN": "test-token-schema-contract",
        "AIXION_ALLOW_AGENT_TOKEN_ENV": "true",
        "AIXION_CODEX_APPROVAL_TIMEOUT_SECONDS": "30",
    }
    calls: list[tuple[str, str, dict | None]] = []
    created: dict = {}

    def request(
        method: str,
        url: str,
        _config: dict,
        _token: str,
        body: dict | None = None,
        **_kwargs: object,
    ) -> dict:
        calls.append((method, url, body))
        if method == "POST" and url.endswith("/agents/approvals"):
            assert body is not None
            created.update(
                {
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
                    "source_agent_id": "agent-schema-contract",
                    "source_agent_name": "Codex schema test",
                    "source_session_id": body["source_session_id"],
                    "source_invocation_id": body["source_invocation_id"],
                    "source_task_url": None,
                }
            )
            return dict(created)
        if method == "GET" and url.endswith("/decision"):
            decision = {**created, "status": phone_status}
            if phone_status == "APPROVED":
                decision["approved_payload_hash"] = hook._hash(decision)
            return decision
        if method == "POST" and url.endswith("/consume"):
            assert phone_status == "APPROVED"
            return {"consumed": True}
        raise AssertionError(f"Unexpected approval protocol request: {method} {url}")

    monkeypatch.setattr(hook, "_request", request)

    assert hook.run(event, env) == (
        expected,
        "Approved from Aixion Control Tower"
        if expected == "allow"
        else "Control Tower decision: DENIED",
    )

    assert calls[0][0:2] == ("POST", "http://127.0.0.1:8001/agents/approvals")
    assert created["source_session_id"].startswith("codex:v1:session:")
    assert created["source_invocation_id"].startswith("codex:v1:")
    assert created["files"][0]["path"] == "codex://tool/Bash"
    assert created["files"][0]["diff"] == (
        '{"command":"mkdir /tmp/aixion-codex-schema-contract",'
        '"description":"Create a disposable test directory"}'
    )
    assert [method for method, _url, _body in calls] == (
        ["POST", "GET", "POST"] if expected == "allow" else ["POST", "GET"]
    )
