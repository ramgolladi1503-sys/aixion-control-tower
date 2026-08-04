#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

HOOK_NAME = "aixion-mobile-approval"
SIDE_EFFECT_MATCHER = (
    "run_command|write_to_file|replace_file_content|"
    "multi_replace_file_content|manage_task|schedule|ask_permission|"
    "invoke_subagent|define_subagent|send_message|manage_subagents|generate_image"
)
DEFAULT_HOOKS_FILE = Path("~/.gemini/config/hooks.json").expanduser()
DEFAULT_CONFIG_FILE = Path("~/.config/aixion/antigravity-hook.json").expanduser()


def _load_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"Refusing unsafe configuration path: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not read valid JSON from {path}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"Expected a JSON object in {path}")
    return value


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise RuntimeError(f"Refusing unsafe configuration path: {path}")

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        finally:
            raise


def _absolute_executable(raw: str) -> str:
    candidate = raw.strip()
    if not candidate:
        discovered = shutil.which("aixion-relay")
        if not discovered:
            raise RuntimeError(
                "aixion-relay is not on PATH; provide --relay-executable."
            )
        candidate = discovered
    path = Path(candidate).expanduser().resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError(f"Relay executable is not executable: {path}")
    return str(path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Install the Aixion PreToolUse approval hook into native Antigravity "
            "without launching a replacement agent."
        )
    )
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--relay-executable", default="")
    parser.add_argument(
        "--hook-script",
        type=Path,
        default=Path(__file__).with_name("aixion_pre_tool_hook.py"),
    )
    parser.add_argument("--hooks-file", type=Path, default=DEFAULT_HOOKS_FILE)
    parser.add_argument("--config-file", type=Path, default=DEFAULT_CONFIG_FILE)
    parser.add_argument("--timeout", type=int, default=3600)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    workspace = args.workspace.expanduser().resolve()
    if not workspace.is_dir():
        raise RuntimeError(f"Workspace does not exist: {workspace}")
    session_id = str(args.session_id).strip()
    if not session_id:
        raise RuntimeError("--session-id cannot be empty")
    if args.timeout < 1:
        raise RuntimeError("--timeout must be at least one second")

    hook_script = args.hook_script.expanduser().resolve()
    if not hook_script.is_file():
        raise RuntimeError(f"Hook script does not exist: {hook_script}")
    relay_executable = _absolute_executable(args.relay_executable)

    config_file = args.config_file.expanduser().resolve()
    local_config = _load_object(config_file)
    raw_workspaces = local_config.get("workspaces")
    workspaces = dict(raw_workspaces) if isinstance(raw_workspaces, dict) else {}
    workspaces[str(workspace)] = session_id
    local_config.update(
        {
            "version": 1,
            "relayExecutable": relay_executable,
            "workspaces": workspaces,
        }
    )
    _atomic_write_json(config_file, local_config)

    hooks_file = args.hooks_file.expanduser().resolve()
    hooks = _load_object(hooks_file)
    command = " ".join(
        [
            shlex.quote(sys.executable),
            shlex.quote(str(hook_script)),
        ]
    )
    hooks[HOOK_NAME] = {
        "enabled": True,
        "PreToolUse": [
            {
                "matcher": SIDE_EFFECT_MATCHER,
                "hooks": [
                    {
                        "type": "command",
                        "command": command,
                        "timeout": args.timeout,
                    }
                ],
            }
        ],
    }
    _atomic_write_json(hooks_file, hooks)

    print(
        json.dumps(
            {
                "status": "configured",
                "workspace": str(workspace),
                "hooks_file": str(hooks_file),
                "hook_config_file": str(config_file),
                "native_agent": "Antigravity",
                "replacement_agent_launched": False,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"configure_native_hook: {error}", file=sys.stderr)
        raise SystemExit(1)
