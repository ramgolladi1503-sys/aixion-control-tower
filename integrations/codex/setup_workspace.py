#!/usr/bin/env python3
"""Install or remove an opt-in, workspace-scoped Codex PermissionRequest hook."""
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
from typing import Any
from urllib.parse import urlsplit

HOOK_NAME = "aixion-control-tower-codex"
HOOK_TIMEOUT_SECONDS = 960
MAX_JSON_BYTES = 64 * 1024


class SetupError(Exception):
    """A setup precondition failed without modifying unrelated hooks."""


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Refuse ambiguous managed JSON instead of silently discarding entries."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _serialize_hooks_file(value: dict[str, Any]) -> bytes:
    """Serialize a managed hooks file only when the hook can read it back."""
    encoded = (json.dumps(value, indent=2) + "\n").encode()
    if len(encoded) > MAX_JSON_BYTES:
        raise SetupError(f"Updated Codex hooks file exceeds {MAX_JSON_BYTES} bytes")
    return encoded


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
            or not (local_http or remote_https)):
        raise SetupError("Use HTTPS for remote APIs; HTTP is allowed only for loopback development")
    return value


def _identifier(label: str, value: str) -> str:
    value = value.strip()
    if not value or len(value) > 200 or any(ord(char) < 32 for char in value):
        raise SetupError(f"{label} is empty or invalid")
    return value


def _read_json(path: Path, *, allow_missing: bool, require_private: bool = False,
               reject_shared_write: bool = False) -> dict:
    if not os.path.lexists(path):
        if allow_missing:
            return {}
        raise SetupError(f"Managed file is missing: {path}")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise SetupError(f"Managed file must be a regular file owned by this user: {path}")
            if require_private and stat.S_IMODE(metadata.st_mode) & 0o077:
                raise SetupError(f"Managed config permissions must exclude group and other users: {path}")
            if reject_shared_write and stat.S_IMODE(metadata.st_mode) & 0o022:
                raise SetupError(
                    f"Codex hooks file must not be group- or other-writable: {path}"
                )
            raw = stream.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            raise SetupError(f"Managed JSON file exceeds {MAX_JSON_BYTES} bytes: {path}")
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise SetupError(f"Cannot safely read JSON file: {path}") from exc
    if not isinstance(value, dict):
        raise SetupError(f"Expected a JSON object: {path}")
    return value


def _private_file_snapshot(path: Path) -> tuple[bytes, int]:
    """Read exact bytes and mode from an owner-only regular file without following links."""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            mode = stat.S_IMODE(metadata.st_mode)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or mode & 0o077):
                raise SetupError(f"Managed config permissions or ownership changed: {path}")
            raw = stream.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            raise SetupError(f"Managed JSON file exceeds {MAX_JSON_BYTES} bytes: {path}")
        return raw, mode
    except OSError as exc:
        raise SetupError(f"Cannot safely snapshot managed config: {path}") from exc


def _hooks_file_snapshot(path: Path) -> tuple[bytes, int]:
    """Read exact bytes and mode for compensating a failed hook removal."""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            mode = stat.S_IMODE(metadata.st_mode)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or mode & 0o022):
                raise SetupError(f"Codex hooks file permissions or ownership changed: {path}")
            raw = stream.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            raise SetupError(f"Managed JSON file exceeds {MAX_JSON_BYTES} bytes: {path}")
        return raw, mode
    except OSError as exc:
        raise SetupError(f"Cannot safely snapshot Codex hooks file: {path}") from exc


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or stat.S_ISLNK(parent.st_mode) or parent.st_uid != os.getuid():
        raise SetupError(f"Managed parent must be a real directory owned by this user: {path.parent}")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temporary)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _validate_config_root(home: Path, config_dir: Path) -> None:
    if home.is_symlink() or not home.is_dir() or home.lstat().st_uid != os.getuid():
        raise SetupError("Home directory must be a real directory owned by this user")
    chain = [home / ".config", home / ".config" / "aixion", config_dir]
    for directory in chain:
        if os.path.lexists(directory):
            metadata = directory.lstat()
            if (not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
                    or metadata.st_uid != os.getuid()):
                raise SetupError(f"Config path component must be a real owned directory: {directory}")


def _ensure_config_dir(home: Path, config_dir: Path) -> None:
    _validate_config_root(home, config_dir)
    chain = [home / ".config", home / ".config" / "aixion", config_dir]
    for directory in chain:
        if not os.path.lexists(directory):
            directory.mkdir(mode=0o700)
        if directory != home / ".config":
            os.chmod(directory, 0o700)
        _validate_config_root(home, config_dir)


def _managed_paths(workspace: Path, home: Path, hook_script: Path) -> tuple[Path, Path, Path]:
    resolved = workspace.resolve(strict=True)
    if not resolved.is_dir() or workspace.is_symlink():
        raise SetupError("Pass an existing real workspace directory, not a symlink")
    digest = hashlib.sha256(os.fsencode(str(resolved))).hexdigest()[:24]
    config_path = home / ".config" / "aixion" / "workspaces" / f"codex-{digest}.json"
    hooks_path = resolved / ".codex" / "hooks.json"
    script_path = hook_script.resolve(strict=True)
    if not script_path.is_file():
        raise SetupError("Codex hook script must be a regular file")
    codex_dir = hooks_path.parent
    if os.path.lexists(codex_dir):
        metadata = codex_dir.lstat()
        if (not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != os.getuid()):
            raise SetupError("Workspace .codex path must be a real directory owned by this user")
        if stat.S_IMODE(metadata.st_mode) & 0o022:
            raise SetupError("Workspace .codex directory must not be group- or other-writable")
    return resolved, config_path, hooks_path


def _hook_entry(config_path: Path, hook_script: Path, python: str) -> dict:
    command = " ".join(shlex.quote(part) for part in (
        python, str(hook_script.resolve()), "--config", str(config_path)
    ))
    return {"matcher": "*", "hooks": [{
        "type": "command", "command": command, "timeout": HOOK_TIMEOUT_SECONDS,
        "statusMessage": "Waiting for Aixion phone approval",
    }]}


def _is_managed_entry(item: Any, config_path: Path, hook_script: Path) -> bool:
    if not isinstance(item, dict) or not isinstance(item.get("hooks"), list):
        return False
    config_text = config_path.as_posix()
    script_text = hook_script.resolve().as_posix()
    return any(
        isinstance(handler, dict)
        and config_text in str(handler.get("command", ""))
        and script_text in str(handler.get("command", ""))
        for handler in item["hooks"]
    )


def install_workspace(workspace: Path, *, api_base_url: str, agent_id: str,
                      project_id: str, home: Path | None = None,
                      hook_script: Path | None = None, python: str | None = None) -> tuple[Path, Path]:
    home = home or Path.home()
    hook_script = hook_script or Path(__file__).with_name("aixion_permission_request_hook.py")
    workspace, config_path, hooks_path = _managed_paths(workspace, home, hook_script)
    config_dir = config_path.parent
    config = {"api_base_url": _api_url(api_base_url),
              "agent_id": _identifier("Agent ID", agent_id),
              "project_id": _identifier("Project ID", project_id),
              # Bind the protected registration to the installer-resolved
              # project path. Codex may report a session cwd below this root.
              "workspace_path": str(workspace)}
    # Validate existing operator-owned files before creating the managed config
    # directory or writing either installation file.
    hooks_file = _read_json(hooks_path, allow_missing=True, reject_shared_write=True)
    hooks = hooks_file.get("hooks", {})
    if not isinstance(hooks, dict):
        raise SetupError("Codex hooks.json 'hooks' field must be an object")
    existing_entries = hooks.get("PermissionRequest", [])
    if not isinstance(existing_entries, list):
        raise SetupError("Codex PermissionRequest hooks must be a list")
    entry = _hook_entry(config_path, hook_script, python or sys.executable)
    expected_entry = entry
    managed = [item for item in existing_entries if _is_managed_entry(item, config_path, hook_script)]
    if managed and managed != [expected_entry]:
        raise SetupError(f"Hook {HOOK_NAME!r} already exists with different contents")
    config_exists = os.path.lexists(config_path)
    existing_config = None
    previous_config_snapshot: tuple[bytes, int] | None = None
    if config_exists:
        existing_config = _read_json(config_path, allow_missing=False, require_private=True)
    legacy_config = {key: value for key, value in config.items() if key != "workspace_path"}
    config_upgrade = config_exists and existing_config == legacy_config
    if config_exists and existing_config != config and not config_upgrade:
        raise SetupError("Workspace Codex config exists with different values; refusing overwrite")
    if config_upgrade:
        previous_config_snapshot = _private_file_snapshot(config_path)
        try:
            snapshot_value = json.loads(previous_config_snapshot[0])
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SetupError("Managed config changed while preparing workspace upgrade") from exc
        if snapshot_value != existing_config:
            raise SetupError("Managed config changed while preparing workspace upgrade")
    updated_hooks_bytes: bytes | None = None
    hooks_mode = 0o600
    if not managed:
        hooks["PermissionRequest"] = [*existing_entries, entry]
        hooks_file["hooks"] = hooks
        updated_hooks_bytes = _serialize_hooks_file(hooks_file)
        hooks_mode = stat.S_IMODE(hooks_path.stat().st_mode) if hooks_path.exists() else 0o600
    _ensure_config_dir(home, config_dir)
    config_created = False
    if not config_exists:
        _atomic_write(config_path, (json.dumps(config, indent=2) + "\n").encode(), 0o600)
        config_created = True
    elif config_upgrade:
        _atomic_write(config_path, (json.dumps(config, indent=2) + "\n").encode(), 0o600)
    if updated_hooks_bytes is not None:
        try:
            _atomic_write(hooks_path, updated_hooks_bytes, hooks_mode)
        except (OSError, SetupError) as install_error:
            if config_created:
                try:
                    config_path.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    install_error.add_note(
                        f"Rollback could not remove the new managed config: {cleanup_error}"
                    )
            elif previous_config_snapshot is not None:
                previous_bytes, previous_mode = previous_config_snapshot
                try:
                    _atomic_write(config_path, previous_bytes, previous_mode)
                except (OSError, SetupError) as rollback_error:
                    install_error.add_note(
                        f"Rollback could not restore the previous managed config: {rollback_error}"
                    )
            raise
    return config_path, hooks_path


def remove_workspace(workspace: Path, *, home: Path | None = None,
                     hook_script: Path | None = None, python: str | None = None) -> tuple[bool, bool]:
    home = home or Path.home()
    hook_script = hook_script or Path(__file__).with_name("aixion_permission_request_hook.py")
    config_dir = home / ".config" / "aixion" / "workspaces"
    _validate_config_root(home, config_dir)
    _, config_path, hooks_path = _managed_paths(workspace, home, hook_script)
    hooks_file = _read_json(hooks_path, allow_missing=True, reject_shared_write=True)
    hooks = hooks_file.get("hooks", {})
    if not isinstance(hooks, dict):
        raise SetupError("Codex hooks.json 'hooks' field must be an object")
    entries = hooks.get("PermissionRequest", [])
    if not isinstance(entries, list):
        raise SetupError("Codex PermissionRequest hooks must be a list")
    managed = [item for item in entries if _is_managed_entry(item, config_path, hook_script)]
    if not managed:
        return False, False
    expected = _hook_entry(config_path, hook_script, python or sys.executable)
    if managed != [expected]:
        raise SetupError("Existing Aixion Codex hook differs from installer; refusing removal")
    config_exists = os.path.lexists(config_path)
    if config_exists:
        _read_json(config_path, allow_missing=False, require_private=True)
    hooks_snapshot = _hooks_file_snapshot(hooks_path)
    remaining = [item for item in entries if item not in managed]
    updated_hooks_bytes: bytes | None = None
    if remaining:
        hooks["PermissionRequest"] = remaining
        hooks_file["hooks"] = hooks
        updated_hooks_bytes = _serialize_hooks_file(hooks_file)
    else:
        hooks.pop("PermissionRequest", None)
        if hooks:
            hooks_file["hooks"] = hooks
            updated_hooks_bytes = _serialize_hooks_file(hooks_file)
        else:
            hooks_file.pop("hooks", None)
            if hooks_file:
                updated_hooks_bytes = _serialize_hooks_file(hooks_file)
    try:
        if updated_hooks_bytes is not None:
            _atomic_write(hooks_path, updated_hooks_bytes, hooks_snapshot[1])
        else:
            hooks_path.unlink()
        if config_exists:
            config_path.unlink()
    except (OSError, SetupError) as remove_error:
        # Hook and config are separate files, so removal cannot be one atomic
        # filesystem operation. If deleting the config fails after the hook
        # edit, restore the exact original hook bytes and mode. This prevents
        # a failed removal from silently leaving a half-removed installation.
        try:
            _atomic_write(hooks_path, hooks_snapshot[0], hooks_snapshot[1])
        except (OSError, SetupError) as rollback_error:
            remove_error.add_note(
                f"Rollback could not restore the original Codex hooks file: {rollback_error}"
            )
        raise
    return True, config_exists


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--api-base-url")
    parser.add_argument("--agent-id")
    parser.add_argument("--project-id")
    parser.add_argument("--remove", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.remove:
            if any((args.api_base_url, args.agent_id, args.project_id)):
                raise SetupError("Do not combine --remove with install options")
            removed_hook, removed_config = remove_workspace(args.workspace)
            print(f"Removed Codex hook={removed_hook}; removed non-secret config={removed_config}")
            return 0
        if not all((args.api_base_url, args.agent_id, args.project_id)):
            raise SetupError("Install requires --api-base-url, --agent-id, and --project-id")
        config_path, hooks_path = install_workspace(
            args.workspace, api_base_url=args.api_base_url,
            agent_id=args.agent_id, project_id=args.project_id,
        )
    except (OSError, SetupError) as exc:
        print(f"Codex hook setup failed: {exc}", file=sys.stderr)
        return 1
    print(f"Installed workspace-scoped Codex PermissionRequest hook: {hooks_path}")
    print(f"Non-secret config: {config_path}")
    print("Review and trust this hook in Codex before it can run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
