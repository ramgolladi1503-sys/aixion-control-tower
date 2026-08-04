from __future__ import annotations

from typing import Any

from ..contracts import ActionProposal, CapabilityActionType, RelayProvider
from .configured_process import configured_process_adapter
from .generic_process import GenericProcessAdapter


def openclaw_tool_action(payload: dict[str, Any]) -> ActionProposal:
    tool_name = str(
        payload.get("toolName")
        or payload.get("tool_name")
        or payload.get("name")
        or "unknown"
    )
    tool_input = payload.get("input") or payload.get("toolInput") or payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {"value": tool_input}
    normalized = tool_name.lower()
    metadata = {
        "tool_name": tool_name,
        "tool_input": tool_input,
        "provider_hook": "before_tool_call",
    }
    if any(token in normalized for token in ("exec", "shell", "bash", "command")):
        command = tool_input.get("command") or tool_input.get("cmd") or tool_input.get("argv")
        return ActionProposal(
            action_type=CapabilityActionType.RUN_COMMAND,
            command=str(command or ""),
            estimated_runtime_seconds=120,
            metadata=metadata,
        )
    if any(token in normalized for token in ("write", "edit", "patch", "file")):
        raw_paths = tool_input.get("paths") or tool_input.get("path") or tool_input.get("file") or []
        if isinstance(raw_paths, str):
            raw_paths = [raw_paths]
        return ActionProposal(
            action_type=CapabilityActionType.MODIFY_FILES,
            paths=[str(path) for path in raw_paths],
            metadata=metadata,
        )
    if any(token in normalized for token in ("web", "fetch", "search", "http")):
        value = tool_input.get("url") or tool_input.get("domain") or []
        if isinstance(value, str):
            value = [value]
        return ActionProposal(
            action_type=CapabilityActionType.ACCESS_NETWORK,
            network_domains=[str(item) for item in value],
            metadata=metadata,
        )
    return ActionProposal(
        action_type=CapabilityActionType.CUSTOM,
        metadata=metadata,
    )


def build_openclaw_adapter() -> GenericProcessAdapter | None:
    """Build an OpenClaw JSONL adapter from an explicit argv array.

    The preferred production interception point is the included OpenClaw
    `before_tool_call` plugin. The configured process handles session lifecycle and
    structured output without shell interpolation.
    """
    return configured_process_adapter(
        environment_name="AIXION_OPENCLAW_ARGV",
        adapter_id="openclaw-gateway",
        provider=RelayProvider.OPENCLAW,
        display_name="OpenClaw Gateway",
        environment={
            "AIXION_OPENCLAW_APPROVAL_COMMAND": "aixion-relay hook openclaw",
        },
    )
