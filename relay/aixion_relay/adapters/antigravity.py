from __future__ import annotations

from typing import Any

from ..contracts import ActionProposal, CapabilityActionType, RelayProvider
from .configured_process import configured_process_adapter
from .generic_process import GenericProcessAdapter


def antigravity_tool_action(payload: dict[str, Any]) -> ActionProposal:
    tool_name = str(
        payload.get("tool_name")
        or payload.get("toolName")
        or payload.get("name")
        or "unknown"
    )
    tool_input = payload.get("tool_input") or payload.get("toolInput") or payload.get("input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {"value": tool_input}
    normalized = tool_name.lower()
    metadata = {
        "tool_name": tool_name,
        "tool_input": tool_input,
        "provider_hook": "PreToolUse",
    }
    if any(token in normalized for token in ("shell", "bash", "command", "terminal")):
        command = tool_input.get("command") or tool_input.get("cmd") or tool_input.get("args")
        return ActionProposal(
            action_type=CapabilityActionType.RUN_COMMAND,
            command=str(command or ""),
            estimated_runtime_seconds=120,
            metadata=metadata,
        )
    if any(token in normalized for token in ("write", "edit", "patch", "file")):
        raw_paths = tool_input.get("paths") or tool_input.get("path") or tool_input.get("file_path") or []
        if isinstance(raw_paths, str):
            raw_paths = [raw_paths]
        return ActionProposal(
            action_type=CapabilityActionType.MODIFY_FILES,
            paths=[str(path) for path in raw_paths],
            metadata=metadata,
        )
    if any(token in normalized for token in ("web", "fetch", "search", "http")):
        raw_domains = tool_input.get("domains") or tool_input.get("url") or []
        if isinstance(raw_domains, str):
            raw_domains = [raw_domains]
        return ActionProposal(
            action_type=CapabilityActionType.ACCESS_NETWORK,
            network_domains=[str(domain) for domain in raw_domains],
            metadata=metadata,
        )
    return ActionProposal(
        action_type=CapabilityActionType.CUSTOM,
        metadata=metadata,
    )


def build_antigravity_adapter() -> GenericProcessAdapter | None:
    """Build the Antigravity adapter when an operator supplies safe argv JSON.

    Example:
    `AIXION_ANTIGRAVITY_ARGV='["antigravity","run","--jsonl"]'`

    Antigravity's PreToolUse hook should invoke `aixion-relay hook antigravity`.
    The process adapter never evaluates a shell command string.
    """
    return configured_process_adapter(
        environment_name="AIXION_ANTIGRAVITY_ARGV",
        adapter_id="antigravity-hooks",
        provider=RelayProvider.ANTIGRAVITY,
        display_name="Google Antigravity with Aixion hooks",
        environment={
            "AIXION_ANTIGRAVITY_PRE_TOOL_HOOK": "aixion-relay hook antigravity",
        },
    )
