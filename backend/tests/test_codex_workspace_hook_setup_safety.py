from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

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
