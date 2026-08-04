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


def test_antigravity_hook_maps_documented_run_command_payload() -> None:
    action = antigravity_tool_action(
        {
            "toolCall": {
                "name": "run_command",
                "args": {
                    "CommandLine": "pytest tests/test_feed.py",
                    "Cwd": "/workspace/project",
                    "WaitMsBeforeAsync": 5000,
                },
            },
            "stepIdx": 19,
            "conversationId": "ec33ebf9-0cba-4100-8142-c61503f6c587",
            "workspacePaths": ["/workspace/project"],
            "transcriptPath": "/private/transcript.jsonl",
            "artifactDirectoryPath": "/private/artifacts",
        }
    )
    assert action.action_type == CapabilityActionType.RUN_COMMAND
    assert action.command == "pytest tests/test_feed.py"
    assert action.metadata["cwd"] == "/workspace/project"
    assert action.metadata["conversation_id"] == (
        "ec33ebf9-0cba-4100-8142-c61503f6c587"
    )
    assert action.metadata["step_index"] == 19
    assert len(action.metadata["provider_payload_sha256"]) == 64


def test_antigravity_hook_maps_documented_file_write_without_copying_code() -> None:
    action = antigravity_tool_action(
        {
            "toolCall": {
                "name": "write_to_file",
                "args": {
                    "TargetFile": "/workspace/project/example.py",
                    "Overwrite": True,
                    "CodeContent": "SECRET_SOURCE_MUST_NOT_ENTER_GENERIC_METADATA",
                    "Description": "Create example",
                },
            },
            "conversationId": "conversation-1",
            "workspacePaths": ["/workspace/project"],
            "stepIdx": 3,
        }
    )
    assert action.action_type == CapabilityActionType.MODIFY_FILES
    assert action.paths == ["/workspace/project/example.py"]
    assert action.metadata["overwrite"] is True
    serialized_metadata = json.dumps(action.metadata)
    assert "SECRET_SOURCE_MUST_NOT_ENTER_GENERIC_METADATA" not in serialized_metadata
    assert action.metadata["tool_argument_names"] == [
        "CodeContent",
        "Description",
        "Overwrite",
        "TargetFile",
    ]


def test_antigravity_payload_hash_is_canonical() -> None:
    first = {
        "conversationId": "conversation-1",
        "toolCall": {
            "name": "run_command",
            "args": {"Cwd": "/workspace", "CommandLine": "pwd"},
        },
    }
    second = {
        "toolCall": {
            "args": {"CommandLine": "pwd", "Cwd": "/workspace"},
            "name": "run_command",
        },
        "conversationId": "conversation-1",
    }
    assert (
        antigravity_tool_action(first).metadata["provider_payload_sha256"]
        == antigravity_tool_action(second).metadata["provider_payload_sha256"]
    )


def test_antigravity_legacy_payload_remains_fail_safe_compatible() -> None:
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
