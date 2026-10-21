from __future__ import annotations

import importlib.util
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
    assert captured.err == ""
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
