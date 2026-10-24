from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SETUP_PATH = ROOT / "integrations" / "codex" / "setup_workspace.py"
SPEC = importlib.util.spec_from_file_location("aixion_codex_workspace_setup_safety", SETUP_PATH)
assert SPEC is not None and SPEC.loader is not None
setup = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = setup
SPEC.loader.exec_module(setup)


def test_remove_preserves_unrelated_permission_request_hook(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    codex_dir = workspace / ".codex"
    codex_dir.mkdir()
    hooks_path = codex_dir / "hooks.json"
    unrelated_permission_hook = {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "python3 /tmp/review_guard.py"}],
    }
    unrelated_event_hook = {
        "matcher": "startup",
        "hooks": [{"type": "command", "command": "python3 /tmp/session_start.py"}],
    }
    original = {
        "description": "User-owned Codex hooks",
        "hooks": {
            "PermissionRequest": [unrelated_permission_hook],
            "SessionStart": [unrelated_event_hook],
        },
    }
    hooks_path.write_text(json.dumps(original), encoding="utf-8")

    setup.install_workspace(
        workspace,
        api_base_url="https://api.example.test",
        agent_id="agent-codex-test",
        project_id="project-codex-test",
        home=home,
    )

    installed = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert installed["description"] == original["description"]
    assert installed["hooks"]["PermissionRequest"][0] == unrelated_permission_hook
    assert len(installed["hooks"]["PermissionRequest"]) == 2

    assert setup.remove_workspace(workspace, home=home) == (True, True)

    remaining = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert remaining == original


def test_install_is_idempotent_for_private_existing_config(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    config_path, hooks_path = setup.install_workspace(
        workspace,
        api_base_url="https://api.example.test",
        agent_id="agent-codex-test",
        project_id="project-codex-test",
        home=home,
    )
    original_hooks = hooks_path.read_bytes()
    original_config = config_path.read_bytes()

    assert config_path.stat().st_mode & 0o777 == 0o600
    assert setup.install_workspace(
        workspace,
        api_base_url="https://api.example.test",
        agent_id="agent-codex-test",
        project_id="project-codex-test",
        home=home,
    ) == (config_path, hooks_path)
    assert config_path.read_bytes() == original_config
    assert hooks_path.read_bytes() == original_hooks


@pytest.mark.parametrize("unsafe_mode", [0o622, 0o666])
def test_install_rejects_group_or_other_writable_hooks_file_without_mutation(
    tmp_path: Path, unsafe_mode: int,
) -> None:
    workspace = tmp_path / "project"
    codex_dir = workspace / ".codex"
    codex_dir.mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    hooks_path = codex_dir / "hooks.json"
    original_hooks = json.dumps({"hooks": {"SessionStart": []}}).encode()
    hooks_path.write_bytes(original_hooks)
    hooks_path.chmod(unsafe_mode)

    with pytest.raises(setup.SetupError, match="must not be group- or other-writable"):
        setup.install_workspace(
            workspace,
            api_base_url="https://api.example.test",
            agent_id="agent-codex-test",
            project_id="project-codex-test",
            home=home,
        )

    assert hooks_path.read_bytes() == original_hooks
    assert hooks_path.stat().st_mode & 0o777 == unsafe_mode
    assert not (home / ".config" / "aixion").exists()


@pytest.mark.parametrize("unsafe_mode", [0o622, 0o666])
def test_remove_rejects_group_or_other_writable_hooks_file_without_mutation(
    tmp_path: Path, unsafe_mode: int,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    config_path, hooks_path = setup.install_workspace(
        workspace,
        api_base_url="https://api.example.test",
        agent_id="agent-codex-test",
        project_id="project-codex-test",
        home=home,
    )
    original_hooks = hooks_path.read_bytes()
    original_config = config_path.read_bytes()
    hooks_path.chmod(unsafe_mode)

    with pytest.raises(setup.SetupError, match="must not be group- or other-writable"):
        setup.remove_workspace(workspace, home=home)

    assert hooks_path.read_bytes() == original_hooks
    assert hooks_path.stat().st_mode & 0o777 == unsafe_mode
    assert config_path.read_bytes() == original_config


def test_install_refuses_group_or_other_readable_config_without_mutation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    config_path, hooks_path = setup.install_workspace(
        workspace,
        api_base_url="https://api.example.test",
        agent_id="agent-codex-test",
        project_id="project-codex-test",
        home=home,
    )
    original_config = config_path.read_bytes()
    original_hooks = hooks_path.read_bytes()

    for unsafe_mode in (0o644, 0o640):
        config_path.chmod(unsafe_mode)
        unsafe_config = config_path.read_bytes()
        unsafe_hooks = hooks_path.read_bytes()

        with pytest.raises(setup.SetupError, match="permissions must exclude group and other"):
            setup.install_workspace(
                workspace,
                api_base_url="https://api.example.test",
                agent_id="agent-codex-test",
                project_id="project-codex-test",
                home=home,
            )

        assert config_path.read_bytes() == unsafe_config == original_config
        assert config_path.stat().st_mode & 0o777 == unsafe_mode
        assert hooks_path.read_bytes() == unsafe_hooks == original_hooks
