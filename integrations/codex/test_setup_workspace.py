from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

SETUP_PATH = Path(__file__).with_name("setup_workspace.py")
SPEC = importlib.util.spec_from_file_location("aixion_codex_workspace_setup", SETUP_PATH)
assert SPEC and SPEC.loader
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


def _hooks(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _install(workspace: Path, home: Path) -> tuple[Path, Path]:
    return setup.install_workspace(
        workspace,
        api_base_url="http://127.0.0.1:8004",
        agent_id="agent_test_codex",
        project_id="project_test_codex",
        home=home,
    )


def test_install_is_workspace_scoped_idempotent_and_preserves_unrelated_hooks(
    tmp_path: Path,
) -> None:
    home = tmp_path / "config-home"
    home.mkdir(mode=0o700)
    workspace = tmp_path / "project"
    codex_dir = workspace / ".codex"
    codex_dir.mkdir(parents=True)
    hooks_path = codex_dir / "hooks.json"
    unrelated = {"matcher": "shell", "hooks": [{"type": "command", "command": "reviewer"}]}
    hooks_path.write_text(
        json.dumps({"hooks": {"PermissionRequest": [unrelated], "Stop": [{"hooks": []}]}}),
        encoding="utf-8",
    )
    hooks_path.chmod(0o600)

    config_path, installed_hooks_path = _install(workspace, home)
    first_result = _hooks(installed_hooks_path)
    first_bytes = installed_hooks_path.read_bytes()
    second_config_path, second_hooks_path = _install(workspace, home)

    assert second_config_path == config_path
    assert second_hooks_path == hooks_path
    assert installed_hooks_path.read_bytes() == first_bytes
    assert first_result["hooks"]["PermissionRequest"][0] == unrelated
    assert first_result["hooks"]["Stop"] == [{"hooks": []}]
    assert len(first_result["hooks"]["PermissionRequest"]) == 2
    assert config_path.parent == home / ".config" / "aixion" / "workspaces"
    assert os.stat(config_path).st_mode & 0o777 == 0o600
    assert json.loads(config_path.read_text(encoding="utf-8")) == {
        "api_base_url": "http://127.0.0.1:8004",
        "agent_id": "agent_test_codex",
        "project_id": "project_test_codex",
    }


def test_install_refuses_shared_writable_hooks_without_partial_config_or_mutation(
    tmp_path: Path,
) -> None:
    home = tmp_path / "config-home"
    home.mkdir(mode=0o700)
    workspace = tmp_path / "project"
    codex_dir = workspace / ".codex"
    codex_dir.mkdir(parents=True)
    hooks_path = codex_dir / "hooks.json"
    original = json.dumps({"hooks": {"PermissionRequest": []}}).encode()
    hooks_path.write_bytes(original)
    hooks_path.chmod(0o620)

    with pytest.raises(setup.SetupError, match="must not be group- or other-writable"):
        _install(workspace, home)

    assert hooks_path.read_bytes() == original
    assert not (home / ".config").exists()


def test_remove_deletes_only_managed_codex_hook_and_config(tmp_path: Path) -> None:
    home = tmp_path / "config-home"
    home.mkdir(mode=0o700)
    workspace = tmp_path / "project"
    workspace.mkdir()
    config_path, hooks_path = _install(workspace, home)
    data = _hooks(hooks_path)
    unrelated = {"matcher": "shell", "hooks": [{"type": "command", "command": "reviewer"}]}
    data["hooks"]["PermissionRequest"].append(unrelated)
    data["hooks"]["Stop"] = [{"hooks": []}]
    hooks_path.write_text(json.dumps(data), encoding="utf-8")
    hooks_path.chmod(0o600)

    assert setup.remove_workspace(workspace, home=home) == (True, True)

    remaining = _hooks(hooks_path)
    assert remaining == {
        "hooks": {
            "PermissionRequest": [unrelated],
            "Stop": [{"hooks": []}],
        }
    }
    assert not config_path.exists()
