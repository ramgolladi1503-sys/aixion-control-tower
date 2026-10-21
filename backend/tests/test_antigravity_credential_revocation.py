from __future__ import annotations

import importlib.util
import io
import json
from email.message import Message
from pathlib import Path
import urllib.error

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


class Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self.body


def test_revoked_credential_during_wait_emits_deny_without_claim(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    calls: list[str] = []

    class Opener:
        def open(self, request, timeout: int) -> Response:
            assert timeout == 10
            method = request.get_method()
            calls.append(method)
            if method == "POST":
                body = json.loads(request.data)
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
                return Response(json.dumps(approval).encode("utf-8"))
            raise urllib.error.HTTPError(
                request.full_url,
                401,
                "Unauthorized",
                Message(),
                io.BytesIO(b""),
            )

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
    monkeypatch.setattr(hook.urllib.request, "build_opener", lambda *_handlers: Opener())

    exit_code = hook.main([])
    output = capsys.readouterr().out
    emitted = json.loads(output)

    assert exit_code == 0
    assert emitted["decision"] == "deny"
    assert "GateError" in emitted["reason"]
    assert "test-token" not in output
    assert calls == ["POST", "GET"]
