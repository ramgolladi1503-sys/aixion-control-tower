from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from ..contracts import (
    ActionProposal,
    AdapterFeature,
    AdapterKind,
    AdapterManifest,
    CapabilityActionType,
    EventType,
    NormalizedEvent,
    RelayProvider,
    SessionResult,
)
from .base import AdapterContext, AgentAdapter, AgentSessionHandle
from .configured_process import configured_process_adapter
from .generic_process import GenericProcessAdapter


class NativeAntigravityHookSession(AgentSessionHandle):
    """Passive relay record for an already-running native Antigravity app."""

    def __init__(self, context: AdapterContext) -> None:
        self.context = context
        self._finished: asyncio.Future[SessionResult] = (
            asyncio.get_running_loop().create_future()
        )

    @property
    def remote_session_id(self) -> str | None:
        return None

    async def announce(self) -> None:
        await self.context.emit(
            NormalizedEvent(
                event_type=EventType.SESSION_STARTED,
                message=(
                    "Native Antigravity workspace attached through PreToolUse; "
                    "no replacement agent process was launched."
                ),
                payload={
                    "native_attach_only": True,
                    "launches_replacement_agent": False,
                    "provider_hook": "PreToolUse",
                },
            )
        )

    async def send_message(self, message: str, metadata: dict[str, Any]) -> None:
        del message, metadata
        raise RuntimeError(
            "Native Antigravity remains owned by its app; Aixion cannot inject prompts."
        )

    async def pause(self, reason: str) -> None:
        await self.context.emit(
            NormalizedEvent(
                event_type=EventType.SESSION_PAUSED,
                message=reason or "Native approval connector paused.",
            )
        )

    async def resume(self, reason: str) -> None:
        await self.context.emit(
            NormalizedEvent(
                event_type=EventType.SESSION_RESUMED,
                message=reason or "Native approval connector resumed.",
            )
        )

    async def cancel(self, reason: str) -> None:
        if not self._finished.done():
            self._finished.set_result(
                SessionResult(
                    success=False,
                    error=reason or "Native Antigravity connector cancelled.",
                    result={
                        "native_attach_only": True,
                        "launches_replacement_agent": False,
                    },
                )
            )
        await self.context.emit(
            NormalizedEvent(
                event_type=EventType.SESSION_CANCELLED,
                message=reason or "Native Antigravity connector cancelled.",
            )
        )

    async def sync(self) -> dict[str, Any]:
        return {
            "native_attach_only": True,
            "launches_replacement_agent": False,
            "provider_hook": "PreToolUse",
        }

    async def wait(self) -> SessionResult:
        return await self._finished


class NativeAntigravityHookAdapter(AgentAdapter):
    """Adapter for an already-running native Antigravity application.

    The universal relay creates only a passive session record. Every actionable
    decision enters through Google's documented PreToolUse hook and returns to the
    same blocked native conversation.
    """

    def __init__(self) -> None:
        self._manifest = AdapterManifest(
            adapter_id="antigravity-native-hook",
            provider=RelayProvider.ANTIGRAVITY,
            adapter_kind=AdapterKind.ANTIGRAVITY_HOOKS,
            display_name="Native Antigravity approval hook",
            available=True,
            features=[
                AdapterFeature.STRUCTURED_EVENTS,
                AdapterFeature.NATIVE_APPROVALS,
            ],
            metadata={
                "native_attach_only": True,
                "launches_replacement_agent": False,
                "provider_hook": "PreToolUse",
            },
        )

    @property
    def manifest(self) -> AdapterManifest:
        return self._manifest

    async def start(self, context: AdapterContext) -> AgentSessionHandle:
        session = NativeAntigravityHookSession(context)
        await session.announce()
        return session


def _canonical_sha256(payload: dict[str, Any]) -> str:
    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _tool_call(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Read the documented Antigravity PreToolUse camelCase contract.

    Current Antigravity payloads use ``toolCall.name`` and ``toolCall.args``.
    Legacy spellings remain accepted only so an installed older hook fails safely
    instead of silently losing the action details during an upgrade.
    """

    raw_tool_call = payload.get("toolCall")
    if isinstance(raw_tool_call, dict):
        name = str(raw_tool_call.get("name") or "unknown")
        args = raw_tool_call.get("args") or {}
        return name, args if isinstance(args, dict) else {"value": args}

    name = str(
        payload.get("tool_name")
        or payload.get("toolName")
        or payload.get("name")
        or "unknown"
    )
    args = (
        payload.get("tool_input")
        or payload.get("toolInput")
        or payload.get("input")
        or {}
    )
    return name, args if isinstance(args, dict) else {"value": args}


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return [str(value)]


def _base_metadata(
    payload: dict[str, Any],
    *,
    tool_name: str,
    tool_args: dict[str, Any],
) -> dict[str, Any]:
    workspace_paths = _string_list(payload.get("workspacePaths"))
    return {
        "provider_hook": "PreToolUse",
        "provider_payload_sha256": _canonical_sha256(payload),
        "conversation_id": str(payload.get("conversationId") or ""),
        "step_index": payload.get("stepIdx"),
        "workspace_paths": workspace_paths,
        "tool_name": tool_name,
        # Deliberately retain only argument names here. Exact command text and
        # target paths are represented in first-class ActionProposal fields;
        # source code, prompts and other potentially sensitive values are not
        # copied into generic cloud metadata.
        "tool_argument_names": sorted(str(key) for key in tool_args),
    }


def antigravity_tool_action(payload: dict[str, Any]) -> ActionProposal:
    tool_name, tool_args = _tool_call(payload)
    normalized = tool_name.lower()
    metadata = _base_metadata(
        payload,
        tool_name=tool_name,
        tool_args=tool_args,
    )

    if normalized == "run_command" or any(
        token in normalized for token in ("shell", "bash", "terminal_command")
    ):
        command = (
            tool_args.get("CommandLine")
            or tool_args.get("command")
            or tool_args.get("cmd")
            or tool_args.get("args")
        )
        cwd = tool_args.get("Cwd") or tool_args.get("cwd")
        metadata.update(
            {
                "cwd": str(cwd or ""),
                "run_persistent": bool(
                    tool_args.get("RunPersistent")
                    or tool_args.get("run_persistent")
                ),
                "requested_terminal_id": str(
                    tool_args.get("RequestedTerminalID") or ""
                ),
            }
        )
        return ActionProposal(
            action_type=CapabilityActionType.RUN_COMMAND,
            command=str(command or ""),
            estimated_runtime_seconds=120,
            metadata=metadata,
        )

    if normalized in {
        "write_to_file",
        "replace_file_content",
        "multi_replace_file_content",
    } or any(token in normalized for token in ("edit_file", "patch_file")):
        raw_paths = (
            tool_args.get("TargetFile")
            or tool_args.get("paths")
            or tool_args.get("path")
            or tool_args.get("file_path")
            or []
        )
        metadata.update(
            {
                "description": str(tool_args.get("Description") or ""),
                "overwrite": bool(tool_args.get("Overwrite", False)),
            }
        )
        return ActionProposal(
            action_type=CapabilityActionType.MODIFY_FILES,
            paths=_string_list(raw_paths),
            metadata=metadata,
        )

    if normalized in {"search_web", "read_url_content"} or normalized.startswith(
        "browser_"
    ):
        raw_domains = (
            tool_args.get("domain")
            or tool_args.get("Url")
            or tool_args.get("url")
            or tool_args.get("domains")
            or []
        )
        return ActionProposal(
            action_type=CapabilityActionType.ACCESS_NETWORK,
            network_domains=_string_list(raw_domains),
            metadata=metadata,
        )

    return ActionProposal(
        action_type=CapabilityActionType.CUSTOM,
        metadata=metadata,
    )


def build_antigravity_adapter() -> GenericProcessAdapter | None:
    """Build an optional explicitly managed Antigravity process adapter."""

    return configured_process_adapter(
        environment_name="AIXION_ANTIGRAVITY_ARGV",
        adapter_id="antigravity-hooks",
        provider=RelayProvider.ANTIGRAVITY,
        display_name="Managed Antigravity process with Aixion hooks",
        environment={
            "AIXION_ANTIGRAVITY_PRE_TOOL_HOOK": "aixion-relay hook antigravity",
        },
    )
