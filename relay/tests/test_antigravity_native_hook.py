from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
HOOK_SCRIPT = (
    REPOSITORY_ROOT
    / "integrations"
    / "antigravity"
    / "aixion_pre_tool_hook.py"
)
CONFIGURATOR = (
    REPOSITORY_ROOT
    / "integrations"
    / "antigravity"
    / "configure_native_hook.py"
)


def _payload(workspace: Path) -> dict[str, object]:
    return {
        "toolCall": {
            "name": "run_command",
            "args": {
                "CommandLine": "printf native-antigravity-proof",
                "Cwd": str(workspace),
                "WaitMsBeforeAsync": 1000,
            },
        },
        "stepIdx": 1,
        "conversationId": "native-antigravity-conversation",
        "workspacePaths": [str(workspace)],
        "transcriptPath": str(workspace / "transcript.jsonl"),
        "artifactDirectoryPath": str(workspace / "artifacts"),
    }


def _fake_relay(path: Path, *, decision: str, returncode: int) -> Path:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "json.load(sys.stdin)\n"
        f"print(json.dumps({{'decision': {decision!r}, 'reason': 'test decision'}}))\n"
        f"raise SystemExit({returncode})\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _run_hook(
    *,
    workspace: Path,
    relay: Path,
    config_path: Path,
) -> subprocess.CompletedProcess[str]:
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "relayExecutable": str(relay),
                "workspaces": {str(workspace): "relay_session_native_test"},
            }
        ),
        encoding="utf-8",
    )
    os.chmod(config_path, 0o600)
    env = {
        **os.environ,
        "AIXION_ANTIGRAVITY_HOOK_CONFIG": str(config_path),
    }
    env.pop("AIXION_RELAY_SESSION_ID", None)
    env.pop("AIXION_RELAY_EXECUTABLE", None)
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(_payload(workspace)),
        text=True,
        capture_output=True,
        check=False,
        env=env,
        timeout=10,
    )


def test_native_hook_emits_fail_closed_json_with_zero_exit_for_bad_input() -> None:
    completed = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input="not-json",
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 0
    assert json.loads(completed.stdout)["decision"] == "deny"


def test_native_hook_allows_from_workspace_session_mapping(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    relay = _fake_relay(tmp_path / "fake-relay", decision="allow", returncode=0)
    completed = _run_hook(
        workspace=workspace,
        relay=relay,
        config_path=tmp_path / "hook-config.json",
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "decision": "allow",
        "reason": "test decision",
    }


def test_native_hook_preserves_explicit_deny_even_if_relay_exits_nonzero(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    relay = _fake_relay(tmp_path / "fake-relay", decision="deny", returncode=2)
    completed = _run_hook(
        workspace=workspace,
        relay=relay,
        config_path=tmp_path / "hook-config.json",
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["decision"] == "deny"


def test_native_hook_denies_unpaired_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    relay = _fake_relay(tmp_path / "fake-relay", decision="allow", returncode=0)
    config_path = tmp_path / "hook-config.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "relayExecutable": str(relay),
                "workspaces": {str(tmp_path / "other"): "relay_session_other"},
            }
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(_payload(workspace)),
        text=True,
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "AIXION_ANTIGRAVITY_HOOK_CONFIG": str(config_path),
        },
        timeout=10,
    )
    assert completed.returncode == 0
    assert json.loads(completed.stdout)["decision"] == "deny"


def test_configurator_installs_documented_global_hook_schema(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    relay = _fake_relay(tmp_path / "fake-relay", decision="allow", returncode=0)
    hooks_file = tmp_path / "gemini" / "hooks.json"
    config_file = tmp_path / "aixion" / "antigravity-hook.json"
    hooks_file.parent.mkdir(parents=True)
    hooks_file.write_text(
        json.dumps({"existing-hook": {"enabled": False}}),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(CONFIGURATOR),
            "--workspace",
            str(workspace),
            "--session-id",
            "relay_session_native_test",
            "--relay-executable",
            str(relay),
            "--hook-script",
            str(HOOK_SCRIPT),
            "--hooks-file",
            str(hooks_file),
            "--config-file",
            str(config_file),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr

    hooks = json.loads(hooks_file.read_text(encoding="utf-8"))
    assert "existing-hook" in hooks
    definition = hooks["aixion-mobile-approval"]
    assert definition["enabled"] is True
    pre_tool_use = definition["PreToolUse"][0]
    assert "run_command" in pre_tool_use["matcher"]
    assert pre_tool_use["hooks"][0]["type"] == "command"
    assert "aixion_pre_tool_hook.py" in pre_tool_use["hooks"][0]["command"]

    local_config = json.loads(config_file.read_text(encoding="utf-8"))
    assert local_config["workspaces"][str(workspace.resolve())] == (
        "relay_session_native_test"
    )
    assert stat.S_IMODE(config_file.stat().st_mode) == 0o600
