#!/usr/bin/env python3
"""Install or remove a workspace-scoped Antigravity approval hook."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import sys
import tempfile
from urllib.parse import urlsplit

HOOK_NAME = "aixion-mobile-approval-workspace"
HOOK_TIMEOUT_SECONDS = 960
MAX_CONFIG_BYTES = 16 * 1024


class SetupError(Exception):
    """A safe setup precondition was not met."""


def _api_url(value: str) -> str:
    value = value.strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise SetupError("API URL is malformed") from exc
    local_http = parsed.scheme == "http" and hostname in {"127.0.0.1", "localhost", "::1"}
    remote_https = parsed.scheme == "https" and bool(hostname)
    if (not hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or "?" in value or "#" in value or not (local_http or remote_https)):
        raise SetupError("Use HTTPS for remote APIs; HTTP is allowed only for loopback development")
    return value


def _validate_identifier(label: str, value: str) -> str:
    value = value.strip()
    if not value or len(value) > 200 or any(ord(char) < 32 for char in value):
        raise SetupError(f"{label} is empty or invalid")
    return value


def _read_json(path: Path, *, allow_missing: bool, require_private: bool = False) -> dict:
    if not os.path.lexists(path):
        if allow_missing:
            return {}
        raise SetupError(f"Expected file is missing: {path}")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise SetupError(f"Managed file must be a regular file owned by this user: {path}")
            if require_private and stat.S_IMODE(metadata.st_mode) & 0o077:
                raise SetupError(
                    f"Managed config permissions must exclude group and other users: {path}"
                )
            raw = stream.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            raise SetupError(f"JSON file exceeds {MAX_CONFIG_BYTES} bytes: {path}")
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SetupError(f"Cannot safely read JSON file: {path}") from exc
    if not isinstance(value, dict):
        raise SetupError(f"Expected a JSON object: {path}")
    return value


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = path.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or stat.S_ISLNK(parent.st_mode)
        or parent.st_uid != os.getuid()
    ):
        raise SetupError(f"Parent must be a real directory owned by this user: {path.parent}")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _validate_config_root(home: Path) -> None:
    if not os.path.lexists(home):
        raise SetupError("Home config root is missing")
    home_metadata = home.lstat()
    if not stat.S_ISDIR(home_metadata.st_mode) or stat.S_ISLNK(home_metadata.st_mode):
        raise SetupError("Home config root must be a real directory")
    if home_metadata.st_uid != os.getuid():
        raise SetupError("Home config root must be owned by the current user")
    config_root = home / ".config"
    aixion_dir = config_root / "aixion"
    workspace_dir = aixion_dir / "workspaces"
    for directory in (config_root, aixion_dir, workspace_dir):
        if os.path.lexists(directory):
            metadata = directory.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise SetupError(f"Config path component must be a real directory: {directory}")
            if metadata.st_uid != os.getuid():
                raise SetupError(f"Config path component must be owned by the current user: {directory}")


def _ensure_config_directory(home: Path) -> Path:
    if not os.path.lexists(home):
        home.mkdir(mode=0o700, parents=True)
    _validate_config_root(home)
    config_root = home / ".config"
    aixion_dir = config_root / "aixion"
    workspace_dir = aixion_dir / "workspaces"
    for directory in (config_root, aixion_dir, workspace_dir):
        if not os.path.lexists(directory):
            directory.mkdir(mode=0o700)
        if directory != config_root:
            os.chmod(directory, 0o700)
        _validate_config_root(home)
    return workspace_dir


def _paths(workspace: Path, home: Path, hook_script: Path) -> tuple[Path, Path, Path]:
    resolved = workspace.resolve(strict=True)
    if not resolved.is_dir():
        raise SetupError("Workspace must be an existing directory")
    if workspace.is_symlink():
        raise SetupError("Pass the real workspace path, not a symlink")
    digest = hashlib.sha256(os.fsencode(str(resolved))).hexdigest()[:24]
    config = home / ".config" / "aixion" / "workspaces" / f"{digest}.json"
    hooks_file = resolved / ".agents" / "hooks.json"
    script = hook_script.resolve(strict=True)
    if not script.is_file():
        raise SetupError("Approval hook script must be a regular file")
    return resolved, config, hooks_file


def _validate_workspace_hooks_directory(path: Path) -> None:
    if not os.path.lexists(path):
        return
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
    ):
        raise SetupError("Workspace .agents path must be a real directory owned by this user")


def _hook_entry(config: Path, hook_script: Path, python: str) -> dict:
    command = " ".join(shlex.quote(part) for part in (
        python, str(hook_script), "--config", str(config)
    ))
    return {
        "PreToolUse": [{
            "matcher": "*",
            "hooks": [{"type": "command", "command": command, "timeout": HOOK_TIMEOUT_SECONDS}],
        }]
    }


def install_workspace(
    workspace: Path,
    *,
    api_base_url: str,
    agent_id: str,
    project_id: str,
    home: Path | None = None,
    hook_script: Path | None = None,
    python: str | None = None,
) -> tuple[Path, Path]:
    home = home or Path.home()
    hook_script = hook_script or Path(__file__).with_name("aixion_pre_tool_hook.py")
    workspace, config_path, hooks_path = _paths(workspace, home, hook_script)
    _validate_workspace_hooks_directory(hooks_path.parent)
    if config_path.parent != _ensure_config_directory(home):
        raise SetupError("Unexpected workspace config path")

    config = {
        "api_base_url": _api_url(api_base_url),
        "agent_id": _validate_identifier("Agent ID", agent_id),
        "project_id": _validate_identifier("Project ID", project_id),
    }
    hooks = _read_json(hooks_path, allow_missing=True)
    expected_hook = _hook_entry(config_path, hook_script.resolve(), python or sys.executable)
    agents = hooks.get(HOOK_NAME)
    if agents is not None and agents != expected_hook:
        raise SetupError(f"Hook name {HOOK_NAME!r} already exists; refusing to replace it")
    if os.path.lexists(config_path):
        existing = _read_json(config_path, allow_missing=False, require_private=True)
        if existing != config:
            raise SetupError("Workspace config already exists with different values; refusing overwrite")
    config_created = False
    if not os.path.lexists(config_path):
        _atomic_write(config_path, (json.dumps(config, indent=2) + "\n").encode(), 0o600)
        config_created = True
    if agents is not None:
        return config_path, hooks_path

    hooks[HOOK_NAME] = expected_hook
    mode = stat.S_IMODE(hooks_path.stat().st_mode) if hooks_path.exists() else 0o600
    try:
        _atomic_write(hooks_path, (json.dumps(hooks, indent=2) + "\n").encode(), mode)
    except (OSError, SetupError):
        if config_created:
            config_path.unlink(missing_ok=True)
        raise
    return config_path, hooks_path


def remove_workspace(
    workspace: Path, *, home: Path | None = None, hook_script: Path | None = None, python: str | None = None
) -> tuple[bool, bool]:
    home = home or Path.home()
    hook_script = hook_script or Path(__file__).with_name("aixion_pre_tool_hook.py")
    _validate_config_root(home)
    workspace, config_path, hooks_path = _paths(workspace, home, hook_script)
    _validate_workspace_hooks_directory(hooks_path.parent)
    hooks = _read_json(hooks_path, allow_missing=True)
    entry = hooks.get(HOOK_NAME)
    if entry is None:
        # Installation writes the private config before the workspace hook.
        # A host crash between those atomic writes leaves an orphan. Remove it
        # only when it has the exact non-secret schema and valid values this
        # installer emits; unknown files at the derived path remain untouched.
        if not os.path.lexists(config_path):
            return False, False
        config = _read_json(config_path, allow_missing=False, require_private=True)
        if set(config) != {"api_base_url", "agent_id", "project_id"}:
            raise SetupError("Orphan workspace config differs from this installer; refusing to remove it")
        try:
            if _api_url(config["api_base_url"]) != config["api_base_url"]:
                raise SetupError("Orphan workspace config differs from this installer; refusing to remove it")
            if any(
                _validate_identifier(label, config[key]) != config[key]
                for key, label in (("agent_id", "Agent ID"), ("project_id", "Project ID"))
            ):
                raise SetupError("Orphan workspace config differs from this installer; refusing to remove it")
        except (TypeError, AttributeError) as exc:
            raise SetupError("Orphan workspace config differs from this installer; refusing to remove it") from exc
        config_path.unlink()
        return False, True
    expected_prefix = str(hook_script.resolve())
    serialized = json.dumps(entry)
    if expected_prefix not in serialized or str(config_path) not in serialized:
        raise SetupError("Existing hook entry differs from this installer; refusing to remove it")
    # Validate every managed artifact before mutating either file. If config is
    # malformed or unsafe (for example, a symlink), leave the active hook intact.
    config_exists = os.path.lexists(config_path)
    if config_exists:
        _read_json(config_path, allow_missing=False, require_private=True)
    del hooks[HOOK_NAME]
    mode = stat.S_IMODE(hooks_path.stat().st_mode)
    if hooks:
        _atomic_write(hooks_path, (json.dumps(hooks, indent=2) + "\n").encode(), mode)
    else:
        hooks_path.unlink()
    removed_config = False
    if config_exists:
        config_path.unlink()
        removed_config = True
    return True, removed_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path, help="Existing Antigravity workspace directory")
    parser.add_argument("--api-base-url", help="HTTPS API URL (loopback HTTP only for development)")
    parser.add_argument("--agent-id", help="External Antigravity agent ID")
    parser.add_argument("--project-id", help="The single allowed project ID")
    parser.add_argument("--remove", action="store_true", help="Remove this installer's workspace hook and metadata")
    args = parser.parse_args(argv)
    try:
        if args.remove:
            if any((args.api_base_url, args.agent_id, args.project_id)):
                raise SetupError("Do not combine --remove with install options")
            removed_hook, removed_config = remove_workspace(args.workspace)
            print(f"Removed workspace hook={removed_hook}; removed non-secret config={removed_config}")
            return 0
        if not all((args.api_base_url, args.agent_id, args.project_id)):
            raise SetupError("Install requires --api-base-url, --agent-id, and --project-id")
        config, hooks = install_workspace(
            args.workspace, api_base_url=args.api_base_url, agent_id=args.agent_id, project_id=args.project_id
        )
        print(f"Installed workspace-scoped hook: {hooks}")
        print(f"Non-secret config: {config}")
        print("Store the one-time agent token in macOS Keychain, then run the hook --check command shown in the README.")
        return 0
    except (OSError, SetupError) as exc:
        print(f"Antigravity workspace setup failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
