from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SETUP_PATH = Path(__file__).with_name("setup_workspace.py")
SPEC = importlib.util.spec_from_file_location("aixion_antigravity_workspace_setup", SETUP_PATH)
assert SPEC and SPEC.loader
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


@pytest.fixture
def install_paths(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = tmp_path / "aixion_pre_tool_hook.py"
    script.write_text("# test hook\n")
    config, hooks = setup.install_workspace(
        workspace,
        api_base_url="https://control.example.test",
        agent_id="agent_test",
        project_id="project_test",
        home=home,
        hook_script=script,
        python="/usr/bin/python3",
    )
    return home, workspace, script, hooks


def _read_hooks(path: Path) -> dict:
    return json.loads(path.read_text())


def test_remove_preserves_hook_that_only_contains_managed_paths(install_paths: tuple[Path, Path, Path, Path]) -> None:
    _home, _workspace, _script, hooks_path = install_paths
    hooks = _read_hooks(hooks_path)
    entry = hooks[setup.HOOK_NAME]
    entry["PreToolUse"][0]["hooks"][0]["timeout"] += 1
    hooks_path.write_text(json.dumps(hooks))

    with pytest.raises(setup.SetupError, match="differs from this installer"):
        setup.remove_workspace(_workspace, home=_home, hook_script=_script)

    assert _read_hooks(hooks_path) == hooks
    assert hooks_path.exists()


def test_remove_preserves_managed_hook_when_config_schema_was_changed(
    install_paths: tuple[Path, Path, Path, Path],
) -> None:
    home, workspace, script, hooks_path = install_paths
    _workspace, config_path, _hooks_path = setup._paths(workspace, home, script)
    config = json.loads(config_path.read_text())
    config["unexpected"] = "preserve me"
    config_path.write_text(json.dumps(config))
    config_path.chmod(0o600)
    original_hooks = hooks_path.read_bytes()

    with pytest.raises(setup.SetupError, match="Workspace config differs"):
        setup.remove_workspace(workspace, home=home, hook_script=script)

    assert hooks_path.read_bytes() == original_hooks
    assert config_path.exists()
    assert "unexpected" in json.loads(config_path.read_text())


def test_remove_deletes_only_exact_managed_hook_and_config(
    install_paths: tuple[Path, Path, Path, Path],
) -> None:
    home, workspace, script, hooks_path = install_paths
    hooks = _read_hooks(hooks_path)
    hooks["OtherProvider"] = {"enabled": True}
    hooks_path.write_text(json.dumps(hooks))
    hooks_path.chmod(0o600)
    _workspace, config_path, _hooks_path = setup._paths(workspace, home, script)

    assert setup.remove_workspace(workspace, home=home, hook_script=script) == (True, True)

    assert _read_hooks(hooks_path) == {"OtherProvider": {"enabled": True}}
    assert not config_path.exists()
