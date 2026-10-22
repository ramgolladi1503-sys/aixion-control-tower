from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

HOOK_PATH = (
    Path(__file__).parents[2]
    / "integrations"
    / "antigravity"
    / "aixion_pre_tool_hook.py"
)
SPEC = importlib.util.spec_from_file_location("antigravity_hook_revocation_test", HOOK_PATH)
assert SPEC and SPEC.loader
hook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hook)


def test_revoked_credential_during_wait_emits_deny_without_claim(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    calls: list[str] = []

    def deadline_request(method, url, _headers, body, timeout, *, max_response_bytes):
        assert timeout == 10
        assert max_response_bytes == hook.MAX_RESPONSE_BYTES
        calls.append(method)
        if method == "POST":
            assert body is not None
            body = json.loads(body)
            approval = {
                **body,
                "id": "approval-test",
                "status": "PENDING_REVIEW",
                "risk": {
                    "level": "HIGH",
                    "reasons": [],
                    "blocked": False,
                    "required_actions": [],
                },
                "source_provider": "ANTIGRAVITY",
                "source_agent_id": "agent-test",
                "source_agent_name": "Antigravity test",
                "source_task_url": None,
                "created_by_user_id": None,
                "verified_source": True,
                "approved_payload_hash": None,
            }
            return SimpleNamespace(
                status=200,
                headers={},
                body=json.dumps(approval).encode("utf-8"),
            )
        assert method == "GET"
        assert url.endswith("/decision")
        return SimpleNamespace(status=401, headers={}, body=b"Unauthorized")

    monkeypatch.setenv("AIXION_API_BASE_URL", "http://127.0.0.1:8012")
    monkeypatch.setenv("AIXION_AGENT_ID", "agent-test")
    monkeypatch.setenv("AIXION_AGENT_TOKEN", "test-token")
    monkeypatch.setenv("AIXION_ALLOW_AGENT_TOKEN_ENV", "true")
    monkeypatch.setenv("AIXION_ALLOW_AGENT_TOKEN_ENV", "true")
    monkeypatch.setenv("AIXION_PROJECT_ID", "project-test")
    monkeypatch.setenv("AIXION_AGENT_CONFIG", str(tmp_path / "missing-config.json"))
    monkeypatch.setattr(
        hook,
        "_read_input",
        lambda: {
            "conversationId": "conversation-test",
            "stepIdx": 1,
            "toolCall": {"name": "run_command", "args": {"CommandLine": "echo safe"}},
        },
    )
    monkeypatch.setattr(hook, "deadline_request", deadline_request)

    exit_code = hook.main([])
    output = capsys.readouterr().out
    emitted = json.loads(output)

    assert exit_code == 0
    assert emitted["decision"] == "deny"
    assert "GateError" in emitted["reason"]
    assert "test-token" not in output
    assert calls == ["POST", "GET"]
