"""Native Windows security and installer regression tests for Codex."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import uuid
import ctypes

import pytest

from integrations.codex import host_security
from integrations.codex import setup_workspace
from integrations.codex import aixion_permission_request_hook as hook

pytestmark = pytest.mark.skipif(os.name != "nt", reason="requires native Windows ACL and Credential Manager")


def _secure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    # Some Windows runner images create temporary paths with the Administrators
    # group as owner. Production correctly requires the current user's SID, so
    # make the fixture explicit instead of relaxing that validation.
    owner_sid = host_security._current_user_sid()
    owner = subprocess.run(
        ["icacls.exe", str(path), "/setowner", f"*{owner_sid}"],
        capture_output=True, text=True, check=False,
    )
    assert owner.returncode == 0, owner.stderr or owner.stdout
    host_security.ensure_private_directory(path)


def _profile_and_workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    home = tmp_path / "Aixion Test User"
    _secure_dir(home)
    _secure_dir(home / "AppData")
    local_app_data = home / "AppData" / "Local"
    _secure_dir(local_app_data)

    workspace = tmp_path / "Codex Workspaces" / "Project With Spaces"
    _secure_dir(workspace.parent)
    _secure_dir(workspace)
    script_parent = workspace / "Aixion Integration"
    _secure_dir(script_parent)
    script_path = script_parent / "approval hook.py"
    source = Path(hook.__file__).read_bytes()
    host_security.atomic_write(script_path, source, 0o600)
    return home, workspace, script_path


def test_windows_install_remove_round_trip_preserves_unrelated_hooks_and_quotes_paths(
    tmp_path: Path,
) -> None:
    home, workspace, script_path = _profile_and_workspace(tmp_path)
    codex_dir = workspace / ".codex"
    unrelated = {"matcher": "*", "hooks": [{"type": "command", "command": "reviewer"}]}
    baseline = {"hooks": {"SessionStart": [unrelated]}}
    _secure_dir(codex_dir)
    hooks_path = codex_dir / "hooks.json"
    host_security.atomic_write(hooks_path, json.dumps(baseline).encode("utf-8"), 0o600)
    config_path, hooks_path = setup_workspace.install_workspace(
        workspace,
        api_base_url="https://control.example.test",
        agent_id="agent_windows_test",
        project_id="project_windows_test",
        home=home,
        hook_script=script_path,
        python=r"C:\Program Files\Python\python.exe",
    )
    hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    handler = hooks["hooks"]["PermissionRequest"][0]["hooks"][0]
    assert handler["commandWindows"].startswith('"C:\\Program Files\\Python\\python.exe"')
    assert '"Aixion Integration\\approval hook.py"' in handler["commandWindows"]
    assert '"Aixion Test User\\AppData\\Local' in handler["commandWindows"]
    assert hooks_path.parent == codex_dir
    assert config_path.parent == host_security.workspace_config_dir(home)
    with host_security.open_private_file(config_path, require_private=True, reject_shared_write=True):
        pass

    removed = setup_workspace.remove_workspace(
        workspace, home=home, hook_script=script_path,
        python=r"C:\Program Files\Python\python.exe",
    )
    assert removed == (True, True)
    assert not config_path.exists()
    assert json.loads(hooks_path.read_text(encoding="utf-8")) == baseline


def test_windows_rejects_broad_dacl_and_reparse_workspace(tmp_path: Path) -> None:
    home, workspace, script_path = _profile_and_workspace(tmp_path)
    codex_dir = workspace / ".codex"
    _secure_dir(codex_dir)
    hooks_path = codex_dir / "hooks.json"
    host_security.atomic_write(hooks_path, b'{"hooks":{}}\n', 0o600)
    grant = subprocess.run(
        ["icacls.exe", str(hooks_path), "/grant", "*S-1-1-0:(R)"],
        capture_output=True, text=True, check=False,
    )
    assert grant.returncode == 0, grant.stderr or grant.stdout
    with pytest.raises(host_security.HostSecurityError, match="broad or unknown principal"):
        with host_security.open_private_file(hooks_path, require_private=True, reject_shared_write=True):
            pass

    junction = tmp_path / "Project Junction"
    command = subprocess.list2cmdline(["mklink", "/J", str(junction), str(workspace)])
    created = subprocess.run(
        ["cmd.exe", "/d", "/c", command], capture_output=True, text=True, check=False,
    )
    assert created.returncode == 0, created.stderr or created.stdout
    with pytest.raises(host_security.HostSecurityError, match="reparse point"):
        host_security.ensure_no_reparse_path(junction / "Aixion Integration" / "approval hook.py")


def test_windows_install_rejects_broad_profile_ancestor_acl(tmp_path: Path) -> None:
    home, workspace, script_path = _profile_and_workspace(tmp_path)
    grant = subprocess.run(
        ["icacls.exe", str(home), "/grant", "*S-1-1-0:(M)"],
        capture_output=True, text=True, check=False,
    )
    assert grant.returncode == 0, grant.stderr or grant.stdout

    with pytest.raises(setup_workspace.SetupError, match="broad or unknown principal"):
        setup_workspace.install_workspace(
            workspace,
            api_base_url="https://control.example.test",
            agent_id="agent_windows_test",
            project_id="project_windows_test",
            home=home,
            hook_script=script_path,
            python=r"C:\Program Files\Python\python.exe",
        )


def test_windows_atomic_write_closes_temp_handle_when_acl_setup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "Protected Files"
    _secure_dir(parent)
    destination = parent / "managed config.json"

    def reject_acl(*_args, **_kwargs) -> None:
        raise host_security.HostSecurityError("injected ACL setup failure")

    monkeypatch.setattr(host_security, "_windows_set_private_acl_path", reject_acl)
    with pytest.raises(host_security.HostSecurityError, match="injected ACL setup failure"):
        host_security.atomic_write(destination, b"{}\n", 0o600)

    assert not list(parent.glob(".managed config.json.*"))
    assert not destination.exists()


def test_windows_credential_manager_missing_present_and_revoked_fail_closed() -> None:
    service = "com.aixion.controltower.agent"
    account = f"agent_windows_test_{uuid.uuid4().hex}"
    config = {"keychain_service": service, "agent_id": account}
    assert host_security.delete_credential(service, account) is False
    with pytest.raises(hook.GateError, match="Windows Credential Manager"):
        hook._windows_agent_token(config)

    host_security.store_credential(service, account, "windows-ci-short-lived-token")
    try:
        assert hook._windows_agent_token(config) == "windows-ci-short-lived-token"
    finally:
        assert host_security.delete_credential(service, account) is True

    with pytest.raises(hook.GateError, match="Windows Credential Manager"):
        hook._windows_agent_token(config)


def test_windows_credential_write_zeroes_secret_buffers_even_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_zeroed_buffers: list[bytes] = []
    original_memset = ctypes.memset

    def observe_zero(address, value: int, size: int):
        original_memset(address, value, size)
        pointer = ctypes.cast(address, ctypes.c_void_p).value
        if pointer and size:
            captured_zeroed_buffers.append(ctypes.string_at(pointer, size))
        return address

    monkeypatch.setattr(host_security.ctypes, "memset", observe_zero)
    monkeypatch.setattr(host_security._CRED, "CredWriteW", lambda *_args: 0)
    with pytest.raises(host_security.HostSecurityError, match="store Windows Credential Manager"):
        host_security._windows_store_credential(
            "com.aixion.controltower.agent", "agent_cleanup_test", "secret-buffer-test"
        )

    assert len(captured_zeroed_buffers) == 2
    assert all(not any(buffer) for buffer in captured_zeroed_buffers)
