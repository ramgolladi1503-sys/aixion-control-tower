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


def _write_relay_config(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "api_base_url": "https://aixion.example.invalid",
                "relay_id": "relay_native_test",
                "relay_token_secret_name": "relay-token:relay_native_test",
                "worker_id": "relay-native-test",
                "workspace_roots": [str(path.parent)],
            }
        ),
        encoding="utf-8",
    )
    return path


def _fake_relay(
    path: Path,
    *,
    decision: str,
    returncode: int,
    expected_config: Path | None = None,
) -> Path:
    expected_args = None
    if expected_config is not None:
        expected_args = [
            "--config",
            str(expected_config.resolve()),
            "hook",
            "antigravity",
            "--session-id",
            "relay_session_native_test",
        ]
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"expected = {expected_args!r}\n"
        "if expected is not None and sys.argv[1:] != expected:\n"
        "    print('unexpected relay argv: ' + repr(sys.argv[1:]), file=sys.stderr)\n"
        "    raise SystemExit(9)\n"
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
    relay_config: Path | None,
) -> subprocess.CompletedProcess[str]:
    config_payload: dict[str, object] = {
        "version": 2,
        "relayExecutable": str(relay),
        "workspaces": {str(workspace): "relay_session_native_test"},
    }
    if relay_config is not None:
        config_payload["relayConfig"] = str(relay_config)
    config_path.write_text(json.dumps(config_payload), encoding="utf-8")
    os.chmod(config_path, 0o600)
    env = {
        **os.environ,
        "AIXION_ANTIGRAVITY_HOOK_CONFIG": str(config_path),
    }
    env.pop("AIXION_RELAY_SESSION_ID", None)
    env.pop("AIXION_RELAY_EXECUTABLE", None)
    env.pop("AIXION_RELAY_CONFIG", None)
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


def test_native_hook_pins_relay_config_from_workspace_session_mapping(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    relay_config = _write_relay_config(tmp_path / "relay-config.json")
    relay = _fake_relay(
        tmp_path / "fake-relay",
        decision="allow",
        returncode=0,
        expected_config=relay_config,
    )
    completed = _run_hook(
        workspace=workspace,
        relay=relay,
        config_path=tmp_path / "hook-config.json",
        relay_config=relay_config,
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
    relay_config = _write_relay_config(tmp_path / "relay-config.json")
    relay = _fake_relay(
        tmp_path / "fake-relay",
        decision="deny",
        returncode=2,
        expected_config=relay_config,
    )
    completed = _run_hook(
        workspace=workspace,
        relay=relay,
        config_path=tmp_path / "hook-config.json",
        relay_config=relay_config,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["decision"] == "deny"


def test_native_hook_denies_missing_relay_config(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    relay = _fake_relay(tmp_path / "fake-relay", decision="allow", returncode=0)
    completed = _run_hook(
        workspace=workspace,
        relay=relay,
        config_path=tmp_path / "hook-config.json",
        relay_config=None,
    )
    assert completed.returncode == 0
    output = json.loads(completed.stdout)
    assert output["decision"] == "deny"
    assert "relay configuration" in output["reason"].lower()


def test_native_hook_denies_invalid_relay_config(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    relay = _fake_relay(tmp_path / "fake-relay", decision="allow", returncode=0)
    missing_relay_config = tmp_path / "missing-relay-config.json"
    completed = _run_hook(
        workspace=workspace,
        relay=relay,
        config_path=tmp_path / "hook-config.json",
        relay_config=missing_relay_config,
    )
    assert completed.returncode == 0
    output = json.loads(completed.stdout)
    assert output["decision"] == "deny"
    assert "relay configuration" in output["reason"].lower()


def test_native_hook_denies_unpaired_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    relay_config = _write_relay_config(tmp_path / "relay-config.json")
    relay = _fake_relay(tmp_path / "fake-relay", decision="allow", returncode=0)
    config_path = tmp_path / "hook-config.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 2,
                "relayExecutable": str(relay),
                "relayConfig": str(relay_config),
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
    relay_config = _write_relay_config(tmp_path / "relay-config.json")
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
            "--relay-config",
            str(relay_config),
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
    assert local_config["version"] == 2
    assert local_config["relayConfig"] == str(relay_config.resolve())
    assert local_config["workspaces"][str(workspace.resolve())] == (
        "relay_session_native_test"
    )
    assert stat.S_IMODE(config_file.stat().st_mode) == 0o600


def test_configurator_rejects_missing_relay_config(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    relay = _fake_relay(tmp_path / "fake-relay", decision="allow", returncode=0)

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
            "--relay-config",
            str(tmp_path / "missing-relay-config.json"),
            "--hooks-file",
            str(tmp_path / "hooks.json"),
            "--config-file",
            str(tmp_path / "hook-config.json"),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 1
    assert "relay configuration is not a regular file" in completed.stderr.lower()
