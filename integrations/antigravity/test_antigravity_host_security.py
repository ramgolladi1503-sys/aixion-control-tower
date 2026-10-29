"""Host-security contract tests for Antigravity hook config and tokens."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from integrations.codex import host_security
from integrations.antigravity import aixion_pre_tool_hook as hook


def _without_posix_uid() -> SimpleNamespace:
    """Model Windows' os module surface without mutating the process os module."""
    return SimpleNamespace(
        path=os.path,
        lexists=os.path.lexists,
        O_RDONLY=os.O_RDONLY,
        O_NOFOLLOW=0,
        open=os.open,
        fdopen=os.fdopen,
        fstat=os.fstat,
    )


def _write_protected_file(path: Path, contents: bytes) -> None:
    if os.name == "nt":
        _secure_dir(path.parent)
        host_security.atomic_write(path, contents, 0o600)
        return
    path.write_bytes(contents)
    path.chmod(0o600)


def test_protected_config_uses_host_security_without_posix_uid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "antigravity.json"
    _write_protected_file(config_path, json.dumps({
        "api_base_url": "https://control.example.test",
        "agent_id": "agent-protected",
        "project_id": "project-protected",
    }).encode("utf-8"))
    monkeypatch.setattr(hook, "os", _without_posix_uid())
    monkeypatch.setattr(hook, "host_security", host_security, raising=False)

    assert hook._configuration({"AIXION_AGENT_CONFIG": str(config_path)}, {}) == {
        "api_base_url": "https://control.example.test",
        "agent_id": "agent-protected",
        "project_id": "project-protected",
        "keychain_service": "com.aixion.controltower.agent",
    }


def test_protected_token_file_uses_host_security_without_posix_uid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_path = tmp_path / "agent-token"
    _write_protected_file(token_path, b"protected-test-token\n")
    monkeypatch.setattr(hook, "os", _without_posix_uid())
    monkeypatch.setattr(hook, "host_security", host_security, raising=False)

    assert hook._agent_token(
        {"AIXION_AGENT_TOKEN_FILE": str(token_path)},
        {"agent_id": "agent-protected", "keychain_service": "service"},
    ) == "protected-test-token"


def _secure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    owner_sid = host_security._current_user_sid()
    owner = subprocess.run(
        ["icacls.exe", str(path), "/setowner", f"*{owner_sid}"],
        capture_output=True, text=True, check=False,
    )
    assert owner.returncode == 0, owner.stderr or owner.stdout
    host_security.ensure_private_directory(path)


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows ACL and reparse support")
def test_windows_antigravity_accepts_explicit_user_acl_config_and_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    protected_dir = tmp_path / "Aixion Protected"
    _secure_dir(protected_dir)
    config_path = protected_dir / "antigravity.json"
    token_path = protected_dir / "agent-token"
    host_security.atomic_write(config_path, json.dumps({
        "api_base_url": "https://control.example.test",
        "agent_id": "agent-win",
        "project_id": "project-win",
    }).encode(), 0o600)
    host_security.atomic_write(token_path, b"windows-test-token\n", 0o600)
    monkeypatch.setattr(hook, "host_security", host_security, raising=False)

    assert hook._configuration({"AIXION_AGENT_CONFIG": str(config_path)}, {})["agent_id"] == "agent-win"
    assert hook._agent_token(
        {"AIXION_AGENT_TOKEN_FILE": str(token_path)},
        {"agent_id": "agent-win", "keychain_service": "service"},
    ) == "windows-test-token"

    grant = subprocess.run(
        ["icacls.exe", str(token_path), "/grant", "*S-1-1-0:(R)"],
        capture_output=True, text=True, check=False,
    )
    assert grant.returncode == 0, grant.stderr or grant.stdout
    with pytest.raises(hook.GateError, match="Agent token file is unavailable"):
        hook._agent_token(
            {"AIXION_AGENT_TOKEN_FILE": str(token_path)},
            {"agent_id": "agent-win", "keychain_service": "service"},
        )


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows ACL and reparse support")
def test_windows_antigravity_rejects_broad_acl_and_reparse_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    protected_dir = tmp_path / "Aixion Protected"
    _secure_dir(protected_dir)
    config_path = protected_dir / "antigravity.json"
    host_security.atomic_write(config_path, b'{"api_base_url":"https://control.example.test"}', 0o600)
    grant = subprocess.run(
        ["icacls.exe", str(config_path), "/grant", "*S-1-1-0:(R)"],
        capture_output=True, text=True, check=False,
    )
    assert grant.returncode == 0, grant.stderr or grant.stdout
    monkeypatch.setattr(hook, "host_security", host_security, raising=False)
    with pytest.raises(hook.GateError, match="unreadable or invalid"):
        hook._configuration({"AIXION_AGENT_CONFIG": str(config_path)}, {})

    junction = tmp_path / "Aixion Junction"
    create = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "New-Item -ItemType Junction -Path $env:AIXION_TEST_JUNCTION "
         "-Target $env:AIXION_TEST_TARGET | Out-Null"],
        env={**os.environ, "AIXION_TEST_JUNCTION": str(junction),
             "AIXION_TEST_TARGET": str(protected_dir)},
        capture_output=True, text=True, check=False,
    )
    assert create.returncode == 0, create.stderr or create.stdout
    with pytest.raises(hook.GateError, match="unreadable or invalid"):
        hook._configuration({"AIXION_AGENT_CONFIG": str(junction / config_path.name)}, {})
