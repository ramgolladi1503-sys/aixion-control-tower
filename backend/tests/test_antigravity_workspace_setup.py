from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import stat

import pytest

SETUP_PATH = Path(__file__).parents[2] / "integrations" / "antigravity" / "setup_workspace.py"
SPEC = importlib.util.spec_from_file_location("aixion_antigravity_setup", SETUP_PATH)
assert SPEC and SPEC.loader
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)
HOOK_PATH = SETUP_PATH.with_name("aixion_pre_tool_hook.py")


class SimulatedInstallerCrash(BaseException):
    """Model process termination that bypasses ordinary installer rollback."""


def test_install_preserves_existing_hooks_and_writes_private_workspace_config(tmp_path: Path) -> None:
    workspace = tmp_path / "my project"
    workspace.mkdir()
    agents_dir = workspace / ".agents"
    agents_dir.mkdir()
    hooks_path = agents_dir / "hooks.json"
    hooks_path.write_text(json.dumps({"existing-linter": {"PostToolUse": []}}))
    hooks_path.chmod(0o640)

    config_path, returned_hooks_path = setup.install_workspace(
        workspace,
        api_base_url="http://127.0.0.1:8000/",
        agent_id="agent-1",
        project_id="project-1",
        home=tmp_path / "home",
        hook_script=HOOK_PATH,
        python="/usr/bin/python3",
    )

    hooks = json.loads(returned_hooks_path.read_text())
    assert hooks["existing-linter"] == {"PostToolUse": []}
    hook = hooks[setup.HOOK_NAME]["PreToolUse"][0]
    assert hook["matcher"] == "*"
    assert hook["hooks"][0]["timeout"] == 960
    assert str(HOOK_PATH) in hook["hooks"][0]["command"]
    assert "--config" in hook["hooks"][0]["command"]
    assert "agent_token" not in config_path.read_text()
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(config_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(hooks_path.stat().st_mode) == 0o640


def test_install_is_idempotent_and_remove_preserves_unrelated_hooks(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    kwargs = dict(
        api_base_url="https://control.example.test",
        agent_id="agent-1",
        project_id="project-1",
        home=tmp_path / "home",
        hook_script=HOOK_PATH,
        python="python3",
    )
    config_path, hooks_path = setup.install_workspace(workspace, **kwargs)
    assert setup.install_workspace(workspace, **kwargs) == (config_path, hooks_path)

    hooks = json.loads(hooks_path.read_text())
    hooks["other"] = {"Stop": []}
    hooks_path.write_text(json.dumps(hooks))
    removed_hook, removed_config = setup.remove_workspace(
        workspace, home=tmp_path / "home", hook_script=HOOK_PATH, python="python3"
    )
    assert (removed_hook, removed_config) == (True, True)
    assert json.loads(hooks_path.read_text()) == {"other": {"Stop": []}}
    assert not config_path.exists()


def test_install_recovers_after_config_write_interruption_and_remove_preserves_other_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agents_dir = workspace / ".agents"
    agents_dir.mkdir()
    hooks_path = agents_dir / "hooks.json"
    original_hooks = {"user-hook": {"PostToolUse": []}}
    hooks_path.write_text(json.dumps(original_hooks))
    home = tmp_path / "home"
    kwargs = dict(
        api_base_url="https://control.example.test",
        agent_id="agent-1",
        project_id="project-1",
        home=home,
        hook_script=HOOK_PATH,
        python="python3",
    )
    atomic_write = setup._atomic_write
    config_path = None

    def interrupt_before_hook(path: Path, data: bytes, mode: int) -> None:
        nonlocal config_path
        if path.name == "hooks.json":
            raise SimulatedInstallerCrash("simulated process interruption before hook registration")
        config_path = path
        atomic_write(path, data, mode)

    monkeypatch.setattr(setup, "_atomic_write", interrupt_before_hook)
    with pytest.raises(SimulatedInstallerCrash, match="simulated process interruption"):
        setup.install_workspace(workspace, **kwargs)

    assert config_path is not None and config_path.is_file()
    assert json.loads(hooks_path.read_text()) == original_hooks

    # A retry must accept the exact orphan config and finish registration.
    monkeypatch.setattr(setup, "_atomic_write", atomic_write)
    recovered_config, recovered_hooks = setup.install_workspace(workspace, **kwargs)
    assert recovered_config == config_path
    recovered = json.loads(recovered_hooks.read_text())
    assert recovered["user-hook"] == original_hooks["user-hook"]
    assert setup.HOOK_NAME in recovered

    assert setup.remove_workspace(workspace, home=home, hook_script=HOOK_PATH, python="python3") == (True, True)
    assert json.loads(hooks_path.read_text()) == original_hooks
    assert not config_path.exists()


def test_remove_cleans_only_valid_orphan_config_without_changing_hooks(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agents_dir = workspace / ".agents"
    agents_dir.mkdir()
    hooks_path = agents_dir / "hooks.json"
    original_hooks = {"user-hook": {"PostToolUse": []}}
    hooks_path.write_text(json.dumps(original_hooks))
    home = tmp_path / "home"
    config_path, expected_hooks = setup.install_workspace(
        workspace,
        api_base_url="https://control.example.test",
        agent_id="agent-1",
        project_id="project-1",
        home=home,
        hook_script=HOOK_PATH,
        python="python3",
    )
    assert hooks_path == expected_hooks
    hooks_path.write_text(json.dumps(original_hooks))

    assert setup.remove_workspace(workspace, home=home, hook_script=HOOK_PATH) == (False, True)
    assert json.loads(hooks_path.read_text()) == original_hooks
    assert not config_path.exists()


def test_remove_preserves_unknown_orphan_config_and_hooks(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    config_path, hooks_path = setup.install_workspace(
        workspace,
        api_base_url="https://control.example.test",
        agent_id="agent-1",
        project_id="project-1",
        home=home,
        hook_script=HOOK_PATH,
    )
    hooks_path.unlink()
    config_path.write_text(json.dumps({"user_data": "preserve"}))

    with pytest.raises(setup.SetupError, match="differs from this installer"):
        setup.remove_workspace(workspace, home=home, hook_script=HOOK_PATH)

    assert json.loads(config_path.read_text()) == {"user_data": "preserve"}


def test_remove_validates_managed_config_before_changing_hook(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    kwargs = dict(
        api_base_url="https://control.example.test",
        agent_id="agent-1",
        project_id="project-1",
        home=tmp_path / "home",
        hook_script=HOOK_PATH,
        python="python3",
    )
    config_path, hooks_path = setup.install_workspace(workspace, **kwargs)
    config_path.write_text("not valid json")
    original_hooks = hooks_path.read_bytes()

    with pytest.raises(setup.SetupError, match="safely read"):
        setup.remove_workspace(
            workspace,
            home=kwargs["home"],
            hook_script=HOOK_PATH,
            python="python3",
        )

    assert hooks_path.read_bytes() == original_hooks
    assert config_path.read_text() == "not valid json"


def test_remove_rejects_symlinked_home_without_mutating_managed_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    config_path, hooks_path = setup.install_workspace(
        workspace,
        api_base_url="https://control.example.test",
        agent_id="agent-1",
        project_id="project-1",
        home=home,
        hook_script=HOOK_PATH,
    )
    original_hooks = hooks_path.read_bytes()
    home_alias = tmp_path / "home-alias"
    home_alias.symlink_to(home, target_is_directory=True)

    with pytest.raises(setup.SetupError, match="Home config root must be a real directory"):
        setup.remove_workspace(workspace, home=home_alias, hook_script=HOOK_PATH)

    assert hooks_path.read_bytes() == original_hooks
    assert config_path.is_file()


def test_installer_rejects_insecure_remote_url_and_existing_hook_collision(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    with pytest.raises(setup.SetupError, match="HTTPS"):
        setup.install_workspace(
            workspace,
            api_base_url="http://control.example.test",
            agent_id="agent-1",
            project_id="project-1",
            home=home,
            hook_script=HOOK_PATH,
        )

    agents = workspace / ".agents"
    agents.mkdir()
    (agents / "hooks.json").write_text(json.dumps({setup.HOOK_NAME: {"enabled": False}}))
    with pytest.raises(setup.SetupError, match="already exists"):
        setup.install_workspace(
            workspace,
            api_base_url="https://control.example.test",
            agent_id="agent-1",
            project_id="project-1",
            home=home,
            hook_script=HOOK_PATH,
        )


def test_installer_refuses_symlinked_hooks_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agents = workspace / ".agents"
    agents.mkdir()
    target = tmp_path / "target.json"
    target.write_text("{}")
    (agents / "hooks.json").symlink_to(target)

    with pytest.raises(setup.SetupError, match="non-regular"):
        setup.install_workspace(
            workspace,
            api_base_url="https://control.example.test",
            agent_id="agent-1",
            project_id="project-1",
            home=tmp_path / "home",
            hook_script=HOOK_PATH,
        )


def test_installer_refuses_symlinked_agents_directory_without_external_writes(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external_agents = tmp_path / "external-agents"
    external_agents.mkdir()
    external_hooks = external_agents / "hooks.json"
    external_hooks.write_text('{"external-hook": true}')
    (workspace / ".agents").symlink_to(external_agents, target_is_directory=True)
    home = tmp_path / "home"

    with pytest.raises(setup.SetupError, match=r"\.agents path must be a real directory"):
        setup.install_workspace(
            workspace,
            api_base_url="https://control.example.test",
            agent_id="agent-1",
            project_id="project-1",
            home=home,
            hook_script=HOOK_PATH,
        )

    assert external_hooks.read_text() == '{"external-hook": true}'
    assert not (home / ".config").exists()


def test_remove_refuses_symlinked_agents_directory_without_external_unlink(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    config_path, hooks_path = setup.install_workspace(
        workspace,
        api_base_url="https://control.example.test",
        agent_id="agent-1",
        project_id="project-1",
        home=home,
        hook_script=HOOK_PATH,
        python="python3",
    )
    original_hooks = hooks_path.read_bytes()
    external_agents = tmp_path / "external-agents"
    external_agents.mkdir()
    external_hooks = external_agents / "hooks.json"
    external_hooks.write_bytes(original_hooks)
    hooks_path.unlink()
    hooks_path.parent.rmdir()
    hooks_path.parent.symlink_to(external_agents, target_is_directory=True)

    with pytest.raises(setup.SetupError, match=r"\.agents path must be a real directory"):
        setup.remove_workspace(workspace, home=home, hook_script=HOOK_PATH)

    assert external_hooks.read_bytes() == original_hooks
    assert config_path.is_file()


def test_installer_refuses_symlinked_config_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / ".config").symlink_to(outside, target_is_directory=True)

    with pytest.raises(setup.SetupError, match="real directory"):
        setup.install_workspace(
            workspace,
            api_base_url="https://control.example.test",
            agent_id="agent-1",
            project_id="project-1",
            home=home,
            hook_script=HOOK_PATH,
        )
