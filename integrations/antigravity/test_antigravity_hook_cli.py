from __future__ import annotations

import importlib.util
import io
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
    assert captured.err == ""
    assert captured.out.startswith('{"decision":"deny","reason":"Aixion approval gate failed:')
    assert captured.out.endswith('"}\n')
