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
        "workspace_path": str(workspace.resolve()),
    }


def test_install_records_canonical_workspace_when_parent_uses_symlink(tmp_path: Path) -> None:
    home = tmp_path / "config-home"
    home.mkdir(mode=0o700)
    real_parent = tmp_path / "real-parent"
    workspace = real_parent / "project"
    workspace.mkdir(parents=True)
    parent_alias = tmp_path / "parent-alias"
    parent_alias.symlink_to(real_parent, target_is_directory=True)

    config_path, _ = _install(parent_alias / "project", home)

    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["workspace_path"] == str(workspace.resolve())


def test_install_rejects_workspace_path_that_is_itself_a_symlink(tmp_path: Path) -> None:
    home = tmp_path / "config-home"
    home.mkdir(mode=0o700)
    workspace = tmp_path / "project"
    workspace.mkdir()
    workspace_alias = tmp_path / "project-alias"
    workspace_alias.symlink_to(workspace, target_is_directory=True)

    with pytest.raises(setup.SetupError, match="not a symlink"):
        _install(workspace_alias, home)

    assert not (home / ".config").exists()


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


@pytest.mark.parametrize("mode", [0o770, 0o707])
def test_install_refuses_shared_writable_codex_directory_without_partial_mutation(
    tmp_path: Path, mode: int,
) -> None:
    home = tmp_path / "config-home"
    home.mkdir(mode=0o700)
    workspace = tmp_path / "project"
    codex_dir = workspace / ".codex"
    codex_dir.mkdir(parents=True)
    codex_dir.chmod(mode)
    hooks_path = codex_dir / "hooks.json"
    original = json.dumps({"hooks": {"PermissionRequest": []}}).encode()
    hooks_path.write_bytes(original)
    hooks_path.chmod(0o600)

    with pytest.raises(setup.SetupError, match=r"\.codex directory must not be group- or other-writable"):
        _install(workspace, home)

    assert hooks_path.read_bytes() == original
    assert not (home / ".config").exists()


def test_install_rejects_codex_directory_owned_by_another_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "config-home"
    home.mkdir(mode=0o700)
    workspace = tmp_path / "project"
    (workspace / ".codex").mkdir(parents=True)
    current_uid = os.getuid()
    monkeypatch.setattr(setup.os, "getuid", lambda: current_uid + 1)

    with pytest.raises(setup.SetupError, match=r"\.codex path must be a real directory owned by this user"):
        _install(workspace, home)

    assert not (home / ".config").exists()


def test_remove_refuses_shared_writable_codex_directory_without_mutation(
    tmp_path: Path,
) -> None:
    home = tmp_path / "config-home"
    home.mkdir(mode=0o700)
    workspace = tmp_path / "project"
    workspace.mkdir()
    config_path, hooks_path = _install(workspace, home)
    hooks_before = hooks_path.read_bytes()
    config_before = config_path.read_bytes()
    hooks_path.parent.chmod(0o770)

    with pytest.raises(setup.SetupError, match=r"\.codex directory must not be group- or other-writable"):
        setup.remove_workspace(workspace, home=home)

    assert hooks_path.read_bytes() == hooks_before
    assert config_path.read_bytes() == config_before


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


def test_remove_restores_hook_if_config_deletion_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "config-home"
    home.mkdir(mode=0o700)
    workspace = tmp_path / "project"
    workspace.mkdir()
    config_path, hooks_path = _install(workspace, home)
    hooks_before = hooks_path.read_bytes()
    hooks_mode_before = hooks_path.stat().st_mode & 0o777
    config_before = config_path.read_bytes()
    original_unlink = Path.unlink

    def fail_config_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path == config_path:
            raise OSError("simulated config removal failure")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_config_unlink)

    with pytest.raises(OSError, match="simulated config removal failure"):
        setup.remove_workspace(workspace, home=home)

    assert hooks_path.read_bytes() == hooks_before
    assert hooks_path.stat().st_mode & 0o777 == hooks_mode_before
    assert config_path.read_bytes() == config_before
