from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from typing import Any

from ..contracts import (
    ActionProposal,
    AdapterFeature,
    AdapterKind,
    AdapterManifest,
    CapabilityActionType,
    EventType,
    NormalizedEvent,
    PolicyDecision,
    RelayProvider,
    SessionResult,
)
from .base import AdapterContext, AgentAdapter, AgentSessionHandle


def _model_payload(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "__dict__"):
        return {
            key: item
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return {"value": str(value)}


def _tool_action(tool_name: str, tool_input: dict[str, Any]) -> ActionProposal:
    normalized = tool_name.lower()
    metadata = {"tool_name": tool_name, "tool_input": tool_input}
    if normalized in {"bash", "shell", "terminal"}:
        command = tool_input.get("command") or tool_input.get("cmd") or ""
        return ActionProposal(
            action_type=CapabilityActionType.RUN_COMMAND,
            command=str(command),
            estimated_runtime_seconds=120,
            metadata=metadata,
        )
    if normalized in {"write", "edit", "multiedit", "notebookedit"}:
        path = tool_input.get("file_path") or tool_input.get("path")
        paths = [str(path)] if path else []
        return ActionProposal(
            action_type=CapabilityActionType.MODIFY_FILES,
            paths=paths,
            metadata=metadata,
        )
    if normalized in {"webfetch", "websearch"}:
        domain = tool_input.get("url") or tool_input.get("domain")
        return ActionProposal(
            action_type=CapabilityActionType.ACCESS_NETWORK,
            network_domains=[str(domain)] if domain else [],
            metadata=metadata,
        )
    if normalized in {"read", "glob", "grep"}:
        path = tool_input.get("file_path") or tool_input.get("path")
        return ActionProposal(
            action_type=CapabilityActionType.READ_REPOSITORY,
            paths=[str(path)] if path else [],
            metadata=metadata,
        )
    return ActionProposal(
        action_type=CapabilityActionType.CUSTOM,
        metadata=metadata,
    )


class ClaudeAgentSession(AgentSessionHandle):
    def __init__(self, *, context: AdapterContext, client: Any) -> None:
        self.context = context
        self.client = client
        self._remote_session_id: str | None = None
        self._query_lock = asyncio.Lock()
        self._finished: asyncio.Future[SessionResult] = (
            asyncio.get_running_loop().create_future()
        )
        self._reader_task: asyncio.Task[None] | None = None
        self._cancelled = False

    @property
    def remote_session_id(self) -> str | None:
        return self._remote_session_id

    async def start(self) -> None:
        connect = getattr(self.client, "connect", None)
        if connect is not None:
            await connect()
        await self.context.emit(
            NormalizedEvent(
                event_type=EventType.SESSION_STARTED,
                message="Claude Agent SDK session started.",
            )
        )
        await self._query(self.context.request.objective)

    async def _query(self, prompt: str) -> None:
        async with self._query_lock:
            await self.client.query(prompt)
            self._reader_task = asyncio.create_task(self._read_response())

    async def _read_response(self) -> None:
        try:
            async for message in self.client.receive_response():
                await self._handle_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - provider errors become evidence.
            await self.context.emit(
                NormalizedEvent(
                    event_type=EventType.SESSION_FAILED,
                    message=str(error),
                    payload={"exception_type": type(error).__name__},
                    remote_session_id=self._remote_session_id,
                )
            )
            if not self._finished.done():
                self._finished.set_result(
                    SessionResult(
                        success=False,
                        remote_session_id=self._remote_session_id,
                        error=str(error),
                    )
                )

    async def _handle_message(self, message: Any) -> None:
        payload = _model_payload(message)
        message_type = type(message).__name__
        session_id = (
            payload.get("session_id")
            or payload.get("sessionId")
            or payload.get("id")
        )
        if session_id and "result" in message_type.lower():
            self._remote_session_id = str(session_id)
        event_type = EventType.RAW
        text = str(payload.get("text") or payload.get("result") or "")
        lowered = message_type.lower()
        if "assistant" in lowered:
            event_type = EventType.AGENT_MESSAGE
        elif "system" in lowered:
            event_type = EventType.PLAN_UPDATED
        elif "result" in lowered:
            success = not bool(payload.get("is_error") or payload.get("error"))
            event_type = EventType.SESSION_COMPLETED if success else EventType.SESSION_FAILED
        elif "tool" in lowered:
            event_type = EventType.TOOL_OUTPUT
        await self.context.emit(
            NormalizedEvent(
                event_type=event_type,
                message=text or message_type,
                payload={"message_type": message_type, "message": payload},
                remote_session_id=self._remote_session_id,
            )
        )
        if "result" in lowered and not self._finished.done():
            success = event_type == EventType.SESSION_COMPLETED
            self._finished.set_result(
                SessionResult(
                    success=success,
                    remote_session_id=self._remote_session_id,
                    result=payload,
                    error=None if success else str(payload.get("error") or text),
                )
            )

    async def send_message(self, message: str, metadata: dict[str, Any]) -> None:
        del metadata
        if self._reader_task and not self._reader_task.done():
            await self._reader_task
        await self._query(message)

    async def pause(self, reason: str) -> None:
        interrupt = getattr(self.client, "interrupt", None)
        if interrupt is None:
            raise RuntimeError("Installed Claude Agent SDK does not expose interrupt().")
        await interrupt()
        await self.context.emit(
            NormalizedEvent(
                event_type=EventType.SESSION_PAUSED,
                message=reason or "Claude session interrupted.",
                remote_session_id=self._remote_session_id,
            )
        )

    async def resume(self, reason: str) -> None:
        await self.context.emit(
            NormalizedEvent(
                event_type=EventType.SESSION_RESUMED,
                message=reason or "Claude session ready for another message.",
                remote_session_id=self._remote_session_id,
            )
        )

    async def cancel(self, reason: str) -> None:
        self._cancelled = True
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        disconnect = getattr(self.client, "disconnect", None)
        if disconnect is not None:
            await disconnect()
        if not self._finished.done():
            self._finished.set_result(
                SessionResult(
                    success=False,
                    remote_session_id=self._remote_session_id,
                    error=reason or "Claude session cancelled.",
                )
            )
        await self.context.emit(
            NormalizedEvent(
                event_type=EventType.SESSION_CANCELLED,
                message=reason or "Claude session cancelled.",
                remote_session_id=self._remote_session_id,
            )
        )

    async def sync(self) -> dict[str, Any]:
        return {
            "remote_session_id": self._remote_session_id,
            "reader_active": bool(self._reader_task and not self._reader_task.done()),
        }

    async def wait(self) -> SessionResult:
        result = await self._finished
        if not self._cancelled:
            disconnect = getattr(self.client, "disconnect", None)
            if disconnect is not None:
                await disconnect()
        return result


class ClaudeAgentSdkAdapter(AgentAdapter):
    def __init__(self) -> None:
        self._available = importlib.util.find_spec("claude_agent_sdk") is not None
        self._manifest = AdapterManifest(
            adapter_id="claude-agent-sdk",
            provider=RelayProvider.CLAUDE,
            adapter_kind=AdapterKind.CLAUDE_AGENT_SDK,
            display_name="Anthropic Claude Agent SDK",
            executable="claude",
            available=self._available,
            features=[
                AdapterFeature.STRUCTURED_EVENTS,
                AdapterFeature.RESUME,
                AdapterFeature.STEER,
                AdapterFeature.CANCEL,
                AdapterFeature.NATIVE_APPROVALS,
                AdapterFeature.MCP,
                AdapterFeature.SUBAGENTS,
                AdapterFeature.FILE_DIFFS,
                AdapterFeature.TEST_RESULTS,
                AdapterFeature.TOKEN_USAGE,
                AdapterFeature.COST_USAGE,
            ],
            metadata={"integration": "ClaudeSDKClient.can_use_tool"},
        )

    @property
    def manifest(self) -> AdapterManifest:
        return self._manifest

    async def start(self, context: AdapterContext) -> AgentSessionHandle:
        if not self._available:
            raise RuntimeError(
                "claude-agent-sdk is not installed. Install aixion-agent-relay[claude]."
            )
        from claude_agent_sdk import (  # type: ignore[import-not-found]
            ClaudeAgentOptions,
            ClaudeSDKClient,
            PermissionResultAllow,
            PermissionResultDeny,
        )

        workspace = Path(context.request.workspace_path).resolve()
        if not workspace.is_dir():
            raise FileNotFoundError(f"Workspace does not exist: {workspace}")

        async def can_use_tool(
            tool_name: str,
            tool_input: dict[str, Any],
            permission_context: Any,
        ) -> Any:
            proposal = _tool_action(tool_name, tool_input)
            proposal.metadata = {
                **proposal.metadata,
                "permission_context": _model_payload(permission_context),
            }
            await context.emit(
                NormalizedEvent(
                    event_type=EventType.APPROVAL_REQUIRED,
                    message=f"Claude requested permission for {tool_name}.",
                    payload=proposal.model_dump(mode="json"),
                )
            )
            decision = await context.authorize(proposal)
            await context.emit(
                NormalizedEvent(
                    event_type=EventType.APPROVAL_RESOLVED,
                    message=f"Claude permission resolved as {decision.decision}.",
                    payload=decision.model_dump(mode="json"),
                )
            )
            if decision.decision == PolicyDecision.ALLOW:
                return PermissionResultAllow(updated_input=tool_input)
            return PermissionResultDeny(
                message="; ".join(decision.reasons) or "Aixion denied this action."
            )

        options = ClaudeAgentOptions(
            cwd=str(workspace),
            model=context.request.model,
            permission_mode="default",
            can_use_tool=can_use_tool,
            resume=context.request.metadata.get("remote_session_id"),
        )
        client = ClaudeSDKClient(options=options)
        session = ClaudeAgentSession(context=context, client=client)
        await session.start()
        return session
