from __future__ import annotations

import json

import pytest

from aixion_relay.adapters.antigravity import antigravity_tool_action
from aixion_relay.adapters.configured_process import (
    ConfiguredProcessError,
    argv_from_environment,
)
from aixion_relay.adapters.openclaw import openclaw_tool_action
from aixion_relay.adapters.registry import discover_default_adapters
from aixion_relay.contracts import CapabilityActionType, RelayProvider


def test_default_registry_always_describes_codex_and_claude() -> None:
    registry = discover_default_adapters()
    manifests = {item.adapter_id: item for item in registry.manifests()}
    assert manifests["codex-app-server"].provider == RelayProvider.CODEX
    assert manifests["claude-agent-sdk"].provider == RelayProvider.CLAUDE


def test_antigravity_hook_maps_command_without_shell_execution() -> None:
    action = antigravity_tool_action(
        {
            "tool_name": "terminal_command",
            "tool_input": {"command": "pytest tests/test_feed.py"},
        }
    )
    assert action.action_type == CapabilityActionType.RUN_COMMAND
    assert action.command == "pytest tests/test_feed.py"


def test_openclaw_hook_maps_file_change() -> None:
    action = openclaw_tool_action(
        {
            "toolName": "edit_file",
            "input": {"path": "backend/app/feed.py"},
        }
    )
    assert action.action_type == CapabilityActionType.MODIFY_FILES
    assert action.paths == ["backend/app/feed.py"]


def test_configured_argv_requires_json_array(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_AGENT_ARGV", "agent --unsafe")
    with pytest.raises(ConfiguredProcessError, match="JSON string array"):
        argv_from_environment("TEST_AGENT_ARGV")

    monkeypatch.setenv("TEST_AGENT_ARGV", json.dumps(["agent", "--jsonl"]))
    assert argv_from_environment("TEST_AGENT_ARGV") == ["agent", "--jsonl"]
