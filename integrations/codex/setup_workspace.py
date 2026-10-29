#!/usr/bin/env python3
"""Install or remove an opt-in, workspace-scoped Codex PermissionRequest hook."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
from typing import Any
from urllib.parse import urlsplit

_REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPOSITORY_ROOT not in sys.path:
    sys.path.insert(0, _REPOSITORY_ROOT)

from integrations.codex import host_security  # noqa: E402 - supports direct script execution

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
        with host_security.open_private_file(
            path, require_private=require_private, reject_shared_write=reject_shared_write
        ) as stream:
            raw = stream.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            raise SetupError(f"Managed JSON file exceeds {MAX_JSON_BYTES} bytes: {path}")
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
    except host_security.HostSecurityError as exc:
        raise SetupError(str(exc)) from exc
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise SetupError(f"Cannot safely read JSON file: {path}") from exc
    if not isinstance(value, dict):
        raise SetupError(f"Expected a JSON object: {path}")
    return value


def _private_file_snapshot(path: Path) -> tuple[bytes, int]:
    """Read exact bytes and mode from an owner-only regular file without following links."""
    try:
        with host_security.open_private_file(
            path, require_private=True, reject_shared_write=True
        ) as stream:
            # Bind the preserved mode to the already-validated open file rather
            # than re-statting a path that could have changed between checks.
            mode = stat.S_IMODE(os.fstat(stream.fileno()).st_mode)
            raw = stream.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            raise SetupError(f"Managed JSON file exceeds {MAX_JSON_BYTES} bytes: {path}")
        return raw, mode
    except (OSError, host_security.HostSecurityError) as exc:
        raise SetupError(f"Cannot safely snapshot managed config: {path}") from exc


def _hooks_file_snapshot(path: Path) -> tuple[bytes, int]:
    """Read exact bytes and mode for compensating a failed hook removal."""
    try:
        with host_security.open_private_file(
            path, require_private=False, reject_shared_write=True
        ) as stream:
            mode = stat.S_IMODE(os.fstat(stream.fileno()).st_mode)
            raw = stream.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            raise SetupError(f"Managed JSON file exceeds {MAX_JSON_BYTES} bytes: {path}")
        return raw, mode
    except (OSError, host_security.HostSecurityError) as exc:
        raise SetupError(f"Cannot safely snapshot Codex hooks file: {path}") from exc


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    try:
        host_security.atomic_write(path, data, mode)
    except (OSError, host_security.HostSecurityError) as exc:
        raise SetupError(f"Cannot safely write managed file: {path}") from exc


def _validate_config_root(home: Path, config_dir: Path) -> None:
    try:
        if os.name == "nt":
            host_security.ensure_no_reparse_path(home)
            host_security.ensure_no_reparse_path(config_dir)
            local_app_data = config_dir.parents[3]
            expected_local_app_data = home / "AppData" / "Local"
            if local_app_data.resolve() != expected_local_app_data.resolve():
                raise SetupError("Windows workspace config must remain under this user's LocalAppData")
            # The protected leaf is only safe if each existing ancestor in the
            # profile chain cannot be replaced by a broad principal. Windows
            # profile directories normally grant access to the user, SYSTEM,
            # and Administrators; anything else fails closed.
            for directory in (home, home / "AppData", local_app_data):
                host_security.validate_directory(directory)
            for directory in (config_dir.parents[2], config_dir.parents[1], config_dir.parent, config_dir):
                if os.path.lexists(directory):
                    host_security.validate_directory(directory)
            return
        if home.is_symlink() or not home.is_dir() or home.lstat().st_uid != os.getuid():
            raise SetupError("Home directory must be a real directory owned by this user")
        chain = [home / ".config", home / ".config" / "aixion", config_dir]
        for directory in chain:
            if os.path.lexists(directory):
                metadata = directory.lstat()
                if (not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
                        or metadata.st_uid != os.getuid()):
                    raise SetupError(f"Config path component must be a real owned directory: {directory}")
    except host_security.HostSecurityError as exc:
        raise SetupError(str(exc)) from exc


def _ensure_config_dir(home: Path, config_dir: Path) -> None:
    _validate_config_root(home, config_dir)
    if os.name == "nt":
        local_app_data = config_dir.parents[3]
        current = local_app_data
        for directory in (current / "Aixion", current / "Aixion" / "ControlTower",
                          current / "Aixion" / "ControlTower" / "Codex", config_dir):
            try:
                host_security.ensure_private_directory(directory)
            except host_security.HostSecurityError as exc:
                raise SetupError(str(exc)) from exc
            _validate_config_root(home, config_dir)
        return
    chain = [home / ".config", home / ".config" / "aixion", config_dir]
    for directory in chain:
        if not os.path.lexists(directory):
            directory.mkdir(mode=0o700)
        if directory != home / ".config":
            os.chmod(directory, 0o700)
        _validate_config_root(home, config_dir)


def _managed_paths(workspace: Path, home: Path, hook_script: Path) -> tuple[Path, Path, Path]:
    if os.name == "nt":
        try:
            host_security.ensure_no_reparse_path(workspace)
        except host_security.HostSecurityError as exc:
            raise SetupError(str(exc)) from exc
    resolved = workspace.resolve(strict=True)
    if not resolved.is_dir() or workspace.is_symlink():
        raise SetupError("Pass an existing real workspace directory, not a symlink")
    if os.name == "nt":
        try:
            host_security.validate_directory(resolved)
        except host_security.HostSecurityError as exc:
            raise SetupError("Windows Codex workspace directory ACL is unsafe") from exc
    digest = hashlib.sha256(os.fsencode(str(resolved))).hexdigest()[:24]
    config_path = host_security.workspace_config_dir(home) / f"codex-{digest}.json"
    hooks_path = resolved / ".codex" / "hooks.json"
    script_path = hook_script.resolve(strict=True)
    if not script_path.is_file():
        raise SetupError("Codex hook script must be a regular file")
    if os.name == "nt":
        try:
            with host_security.open_private_file(
                script_path, require_private=False, reject_shared_write=True
            ):
                pass
        except (OSError, host_security.HostSecurityError) as exc:
            raise SetupError("Codex hook script ACL or path is unsafe") from exc
    codex_dir = hooks_path.parent
    if os.path.lexists(codex_dir):
        metadata = codex_dir.lstat()
        if os.name == "nt":
            try:
                host_security.validate_directory(codex_dir)
            except host_security.HostSecurityError as exc:
                raise SetupError("Workspace .codex directory ACL is unsafe") from exc
        else:
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
    handler = {
        "type": "command", "command": command, "timeout": HOOK_TIMEOUT_SECONDS,
        "statusMessage": "Waiting for Aixion phone approval",
    }
    if os.name == "nt":
        handler["commandWindows"] = _windows_hook_command(config_path, hook_script, python)
    return {"matcher": "*", "hooks": [handler]}


def _windows_hook_command(config_path: Path, hook_script: Path, python: str) -> str:
    """Quote argv using the Windows CreateProcess command-line convention."""
    return subprocess.list2cmdline((
        python, str(hook_script.resolve()), "--config", str(config_path)
    ))


def _is_managed_entry(item: Any, config_path: Path, hook_script: Path) -> bool:
    if not isinstance(item, dict) or not isinstance(item.get("hooks"), list):
        return False
    def normalized(value: str) -> str:
        return value.replace("\\", "/").casefold()

    config_text = normalized(str(config_path))
    script_text = normalized(str(hook_script.resolve()))
    return any(
        isinstance(handler, dict)
        and config_text in normalized(str(handler.get("command", ""))
                                      + " " + str(handler.get("commandWindows", "")))
        and script_text in normalized(str(handler.get("command", ""))
                                      + " " + str(handler.get("commandWindows", "")))
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
    if os.name == "nt" and updated_hooks_bytes is not None:
        try:
            host_security.ensure_private_directory(hooks_path.parent)
        except host_security.HostSecurityError as exc:
            raise SetupError("Workspace .codex directory ACL is unsafe") from exc
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
    config_dir = host_security.workspace_config_dir(home)
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
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--api-base-url")
    parser.add_argument("--agent-id")
    parser.add_argument("--project-id")
    parser.add_argument("--remove", action="store_true")
    credential_mode = parser.add_mutually_exclusive_group()
    credential_mode.add_argument("--store-token", action="store_true")
    credential_mode.add_argument("--remove-token", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.store_token or args.remove_token:
            if os.name != "nt":
                raise SetupError("Windows Credential Manager commands are available only on Windows")
            if args.workspace or args.api_base_url or args.project_id or args.remove:
                raise SetupError("Credential commands cannot be combined with workspace install/remove options")
            agent_id = _identifier("Agent ID", args.agent_id or "")
            service = "com.aixion.controltower.agent"
            if args.remove_token:
                removed = host_security.delete_credential(service, agent_id)
                print(f"Removed Windows Credential Manager token: {removed}")
                return 0
            secret = getpass.getpass("Control Tower agent token: ")
            confirmation = getpass.getpass("Confirm token: ")
            if not secret or not hmac.compare_digest(secret, confirmation):
                raise SetupError("Token is empty or confirmation does not match")
            host_security.store_credential(service, agent_id, secret)
            print(f"Stored token in Windows Credential Manager for agent {agent_id}.")
            return 0
        if not args.workspace:
            raise SetupError("Workspace install/remove requires --workspace")
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
