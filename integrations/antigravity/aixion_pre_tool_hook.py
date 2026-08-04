#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

MAX_INPUT_BYTES = 256 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
DEFAULT_TIMEOUT_SECONDS = 3600
DEFAULT_CONFIG_PATH = Path("~/.config/aixion/antigravity-hook.json").expanduser()


def _emit(decision: str, reason: str) -> int:
    """Emit a valid Antigravity hook decision.

    A deny is a successful hook result, not a process failure. Returning zero is
    important: Antigravity must consume the JSON decision rather than treating a
    fail-closed denial as an unavailable hook.
    """

    print(
        json.dumps(
            {
                "decision": decision,
                "reason": reason[:1000],
            },
            separators=(",", ":"),
        )
    )
    return 0


def _deny(reason: str) -> int:
    return _emit("deny", reason)


def _config_path() -> Path:
    configured = os.getenv("AIXION_ANTIGRAVITY_HOOK_CONFIG", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_CONFIG_PATH


def _load_config() -> dict[str, Any]:
    path = _config_path()
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _normalize_path(raw: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(raw)))


def _workspace_paths(payload: dict[str, Any]) -> list[str]:
    values: list[str] = []
    raw_workspaces = payload.get("workspacePaths") or []
    if isinstance(raw_workspaces, str):
        raw_workspaces = [raw_workspaces]
    if isinstance(raw_workspaces, list):
        values.extend(str(item) for item in raw_workspaces if str(item).strip())

    tool_call = payload.get("toolCall")
    if isinstance(tool_call, dict):
        args = tool_call.get("args")
        if isinstance(args, dict):
            cwd = args.get("Cwd") or args.get("cwd")
            if cwd:
                values.append(str(cwd))

    normalized: list[str] = []
    for value in values:
        candidate = _normalize_path(value)
        if candidate not in normalized:
            normalized.append(candidate)
    return normalized


def _path_within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def _resolve_session_id(
    payload: dict[str, Any],
    config: dict[str, Any],
) -> str:
    override = os.getenv("AIXION_RELAY_SESSION_ID", "").strip()
    if override:
        return override

    workspaces = config.get("workspaces")
    if not isinstance(workspaces, dict):
        return ""

    candidates = _workspace_paths(payload)
    matches: list[tuple[int, str]] = []
    for raw_root, raw_session_id in workspaces.items():
        root = _normalize_path(str(raw_root))
        session_id = str(raw_session_id).strip()
        if not session_id:
            continue
        if any(_path_within(candidate, root) for candidate in candidates):
            matches.append((len(root), session_id))
    if not matches:
        return ""
    matches.sort(reverse=True)
    return matches[0][1]


def _resolve_executable(config: dict[str, Any]) -> str:
    override = os.getenv("AIXION_RELAY_EXECUTABLE", "").strip()
    if override:
        return override
    configured = str(config.get("relayExecutable") or "").strip()
    return configured or "aixion-relay"


def _timeout_seconds() -> int:
    raw = os.getenv(
        "AIXION_HOOK_APPROVAL_TIMEOUT_SECONDS",
        str(DEFAULT_TIMEOUT_SECONDS),
    )
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        return _deny("Aixion rejected an oversized Antigravity hook payload.")
    try:
        payload: dict[str, Any] = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _deny("Aixion could not parse the Antigravity hook payload.")
    if not isinstance(payload, dict):
        return _deny("Aixion received an invalid Antigravity hook payload.")

    config = _load_config()
    session_id = _resolve_session_id(payload, config)
    if not session_id:
        return _deny(
            "This Antigravity workspace is not paired with an Aixion relay session."
        )

    executable = _resolve_executable(config)
    try:
        completed = subprocess.run(
            [
                executable,
                "hook",
                "antigravity",
                "--session-id",
                session_id,
            ],
            input=json.dumps(payload, separators=(",", ":")),
            text=True,
            capture_output=True,
            timeout=_timeout_seconds(),
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return _deny("Aixion mobile approval timed out.")
    except OSError:
        return _deny("Aixion relay is unavailable on this Mac.")

    encoded_stdout = completed.stdout.encode("utf-8", errors="replace")
    if not encoded_stdout or len(encoded_stdout) > MAX_OUTPUT_BYTES:
        return _deny("Aixion relay returned no valid approval decision.")
    try:
        decision = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return _deny("Aixion relay returned invalid approval JSON.")
    if not isinstance(decision, dict):
        return _deny("Aixion relay returned an invalid approval object.")

    normalized = str(decision.get("decision") or "").lower()
    reason = str(decision.get("reason") or "Aixion policy decision.")
    if normalized == "allow" and completed.returncode == 0:
        return _emit("allow", reason)
    if normalized == "deny":
        return _emit("deny", reason)
    return _deny("Aixion relay did not return a consistent allow or deny decision.")


if __name__ == "__main__":
    raise SystemExit(main())
