from __future__ import annotations

import asyncio
import shutil
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
from ..jsonrpc import JsonRpcStdioClient
from .base import AdapterContext, AgentAdapter, AgentSessionHandle


def _text_input(message: str) -> list[dict[str, str]]:
    return [{"type": "text", "text": message}]


def _nested(payload: dict[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _thread_id(result: dict[str, Any] | None, fallback: str | None = None) -> str | None:
    value = (
        _nested(result or {}, "thread", "id")
        or (result or {}).get("threadId")
        or (result or {}).get("id")
        or fallback
    )
    if value is None or str(value) == "None":
        return None
    return str(value)


def _turn_id(result: dict[str, Any] | None, fallback: str | None = None) -> str | None:
    value = (
        _nested(result or {}, "turn", "id")
        or (result or {}).get("turnId")
        or (result or {}).get("id")
        or fallback
    )
    if value is None or str(value) == "None":
        return None
    return str(value)


def _supported_decisions(params: dict[str, Any]) -> list[str]:
    raw = (
        params.get("availableDecisions")
        or params.get("available_decisions")
        or _nested(params, "item", "availableDecisions")
        or _nested(params, "item", "available_decisions")
        or _nested(params, "item", "choices")
        or []
    )
    decisions: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                value = item
            elif isinstance(item, dict):
                value = str(item.get("decision") or item.get("id") or item.get("value") or "")
            else:
                value = ""
            if value and value not in decisions:
                decisions.append(value)
    if not decisions:
        decisions = ["accept", "decline"]
    if "decline" not in decisions:
        decisions.append("decline")
    return decisions


def _provider_resolution(decision: PolicyDecision, supported: list[str], obligations: list[str]) -> str:
    for obligation in obligations:
        prefix = "provider_decision:"
        if obligation.startswith(prefix):
            requested = obligation[len(prefix) :]
            if requested not in supported:
                raise RuntimeError(f"Provider decision {requested!r} is not available.")
            return requested
    if decision == PolicyDecision.ALLOW:
        if "accept" not in supported:
            raise RuntimeError("Policy allowed an action without provider accept support.")
        return "accept"
    if "decline" in supported:
        return "decline"
    if "cancel" in supported:
        return "cancel"
    raise RuntimeError("Provider request has no safe rejecting decision.")


class CodexAppServerSession(AgentSessionHandle):
    APPROVAL_METHODS = {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
    }

    def __init__(
        self,
        *,
        context: AdapterContext,
        process: asyncio.subprocess.Process,
    ) -> None:
        self.context = context
        self.process = process
        self.rpc = JsonRpcStdioClient(
            process,
            on_notification=self._on_notification,
            on_request=self._on_request,
        )
        self.thread_id: str | None = None
        self.turn_id: str | None = None
        self._initialized = False
        self._thread_started = False
        self._finished: asyncio.Future[SessionResult] = (
            asyncio.get_running_loop().create_future()
        )
        self._cancelled = False

    @property
    def remote_session_id(self) -> str | None:
        return self.thread_id

    async def initialize_json_rpc(self) -> None:
        if self._initialized:
            return
        await self.rpc.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "aixion-agent-relay",
                    "title": "Aixion Agent Relay",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        await self.rpc.notify("initialized")
        self._initialized = True

    async def create_thread(self) -> str:
        if self._thread_started and self.thread_id:
            return self.thread_id
        await self.initialize_json_rpc()
        result = await self.rpc.request(
            "thread/start",
            {
                "cwd": self.context.request.workspace_path,
                "approvalPolicy": "on-request",
                "sandbox": "workspace-write",
                "model": self.context.request.model,
            },
        )
        self.thread_id = _thread_id(result)
        if not self.thread_id:
            raise RuntimeError(f"Codex thread/start returned no thread id: {result!r}")
        self._thread_started = True
        await self.context.emit(
            NormalizedEvent(
                event_type=EventType.SESSION_STARTED,
                message="Codex app-server thread started.",
                payload={"thread_start": result or {}},
                remote_session_id=self.thread_id,
            )
        )
        return self.thread_id

    async def initialize(self) -> None:
        await self.create_thread()
        if self.context.request.objective:
            await self.start_turn(self.context.request.objective)

    async def start_turn(self, message: str) -> str | None:
        if not self.thread_id:
            await self.create_thread()
        result = await self.rpc.request(
            "turn/start",
            {
                "threadId": self.thread_id,
                "input": _text_input(message),
            },
        )
        self.turn_id = _turn_id(result)
        await self.context.emit(
            NormalizedEvent(
                event_type=EventType.USER_MESSAGE,
                message=message,
                payload={"turn_start": result or {}},
                remote_session_id=self.thread_id,
                remote_turn_id=self.turn_id,
            )
        )
        return self.turn_id

    async def _on_request(self, method: str, params: dict[str, Any]) -> Any:
        if method not in self.APPROVAL_METHODS:
            raise RuntimeError(f"Unsupported Codex app-server request: {method}")
        item = params.get("item") or params
        supported_decisions = _supported_decisions(params)
        if method.endswith("commandExecution/requestApproval"):
            command = (
                item.get("command")
                or item.get("commandText")
                or item.get("cmd")
            )
            if isinstance(command, list):
                command = " ".join(str(part) for part in command)
            proposal = ActionProposal(
                action_type=CapabilityActionType.RUN_COMMAND,
                command=str(command or ""),
                estimated_runtime_seconds=120,
                metadata={
                    "provider_method": method,
                    "codex_item_id": item.get("id"),
                    "provider_item_id": item.get("id"),
                    "provider_payload_hash_source": params,
                    "cwd": item.get("cwd") or self.context.request.workspace_path,
                    "reason": item.get("reason"),
                    "sandbox": item.get("sandbox") or item.get("sandboxScope"),
                    "network": item.get("network") or item.get("networkDomains"),
                    "policy_amendment": item.get("policyAmendment"),
                    "thread_id": self.thread_id,
                    "turn_id": self.turn_id,
                    "available_decisions": supported_decisions,
                    "raw_request": params,
                },
            )
        else:
            paths = item.get("paths") or item.get("files") or item.get("grantRoot") or []
            if isinstance(paths, str):
                paths = [paths]
            proposal = ActionProposal(
                action_type=CapabilityActionType.MODIFY_FILES,
                paths=[str(path) for path in paths],
                metadata={
                    "provider_method": method,
                    "codex_item_id": item.get("id"),
                    "provider_item_id": item.get("id"),
                    "provider_payload_hash_source": params,
                    "patch": item.get("patch"),
                    "cwd": item.get("cwd") or self.context.request.workspace_path,
                    "reason": item.get("reason"),
                    "sandbox": item.get("sandbox") or item.get("sandboxScope"),
                    "network": item.get("network") or item.get("networkDomains"),
                    "policy_amendment": item.get("policyAmendment"),
                    "thread_id": self.thread_id,
                    "turn_id": self.turn_id,
                    "available_decisions": supported_decisions,
                    "raw_request": params,
                },
            )
        await self.context.emit(
            NormalizedEvent(
                event_type=EventType.APPROVAL_REQUIRED,
                message=f"Codex requested {proposal.action_type} approval.",
                payload=proposal.model_dump(mode="json"),
                remote_session_id=self.thread_id,
                remote_turn_id=self.turn_id,
            )
        )
        decision = await self.context.authorize(proposal)
        provider_decision = _provider_resolution(
            decision.decision,
            supported_decisions,
            decision.obligations,
        )
        await self.context.emit(
            NormalizedEvent(
                event_type=EventType.APPROVAL_RESOLVED,
                message=f"Codex approval resolved as {decision.decision}.",
                payload={
                    "action_id": decision.action_id,
                    "decision": decision.decision,
                    "provider_decision": provider_decision,
                    "reasons": decision.reasons,
                },
                remote_session_id=self.thread_id,
                remote_turn_id=self.turn_id,
            )
        )
        return {"decision": provider_decision}

    async def _on_notification(self, method: str, params: dict[str, Any]) -> None:
        event_type, message = self._normalize_notification(method, params)
        await self.context.emit(
            NormalizedEvent(
                event_type=event_type,
                message=message,
                payload={"method": method, "params": params},
                remote_session_id=(
                    params.get("threadId")
                    or _nested(params, "thread", "id")
                    or self.thread_id
                ),
                remote_turn_id=(
                    params.get("turnId")
                    or _nested(params, "turn", "id")
                    or self.turn_id
                ),
            )
        )
        if method in {"turn/completed", "thread/completed"}:
            if not self._finished.done():
                self._finished.set_result(
                    SessionResult(
                        success=True,
                        remote_session_id=self.thread_id,
                        result={"method": method, "params": params},
                    )
                )
        elif method in {"turn/failed", "thread/failed", "error"}:
            error = str(params.get("message") or params.get("error") or method)
            if not self._finished.done():
                self._finished.set_result(
                    SessionResult(
                        success=False,
                        remote_session_id=self.thread_id,
                        error=error,
                        result={"method": method, "params": params},
                    )
                )

    @staticmethod
    def _normalize_notification(
        method: str,
        params: dict[str, Any],
    ) -> tuple[EventType, str]:
        method_lower = method.lower()
        text = (
            params.get("text")
            or params.get("delta")
            or params.get("message")
            or _nested(params, "item", "text")
            or ""
        )
        if "plan" in method_lower:
            return EventType.PLAN_UPDATED, str(text)
        if "reasoning" in method_lower:
            return EventType.REASONING_SUMMARY, str(text)
        if "command" in method_lower and "output" in method_lower:
            return EventType.COMMAND_OUTPUT, str(text)
        if "command" in method_lower and "start" in method_lower:
            return EventType.COMMAND_STARTED, str(text)
        if "file" in method_lower and ("change" in method_lower or "patch" in method_lower):
            return EventType.FILE_CHANGED, str(text)
        if "test" in method_lower:
            return EventType.TEST_RESULT, str(text)
        if method in {"turn/completed", "thread/completed"}:
            return EventType.SESSION_COMPLETED, "Codex turn completed."
        if method in {"turn/failed", "thread/failed", "error"}:
            return EventType.SESSION_FAILED, str(text or method)
        if "agentmessage" in method_lower or "message" in method_lower:
            return EventType.AGENT_MESSAGE, str(text)
        return EventType.RAW, str(text or method)

    async def send_message(self, message: str, metadata: dict[str, Any]) -> None:
        del metadata
        if self.turn_id and self.thread_id:
            try:
                await self.rpc.request(
                    "turn/steer",
                    {
                        "threadId": self.thread_id,
                        "turnId": self.turn_id,
                        "input": _text_input(message),
                    },
                )
                return
            except Exception:  # noqa: BLE001 - fallback for app-server versions without steer.
                pass
        await self.start_turn(message)

    async def pause(self, reason: str) -> None:
        if not self.thread_id or not self.turn_id:
            raise RuntimeError("Codex has no active turn to interrupt.")
        await self.rpc.request(
            "turn/interrupt",
            {"threadId": self.thread_id, "turnId": self.turn_id},
        )
        await self.context.emit(
            NormalizedEvent(
                event_type=EventType.SESSION_PAUSED,
                message=reason or "Codex turn interrupted.",
                remote_session_id=self.thread_id,
                remote_turn_id=self.turn_id,
            )
        )

    async def resume(self, reason: str) -> None:
        await self.context.emit(
            NormalizedEvent(
                event_type=EventType.SESSION_RESUMED,
                message=reason or "Codex session resumed.",
                remote_session_id=self.thread_id,
            )
        )

    async def cancel(self, reason: str) -> None:
        self._cancelled = True
        if self.thread_id and self.turn_id:
            try:
                await self.rpc.request(
                    "turn/interrupt",
                    {"threadId": self.thread_id, "turnId": self.turn_id},
                )
            except Exception:  # noqa: BLE001 - process termination is the final boundary.
                pass
        await self.close_process()
        if not self._finished.done():
            self._finished.set_result(
                SessionResult(
                    success=False,
                    remote_session_id=self.thread_id,
                    error=reason or "Codex session cancelled.",
                )
            )
        await self.context.emit(
            NormalizedEvent(
                event_type=EventType.SESSION_CANCELLED,
                message=reason or "Codex session cancelled.",
                remote_session_id=self.thread_id,
            )
        )

    async def sync(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "pid": self.process.pid,
            "returncode": self.process.returncode,
            "stderr_tail": self.rpc.stderr_lines[-20:],
        }

    async def wait(self) -> SessionResult:
        result = await self._finished
        if not self._cancelled:
            await self.close_process()
        return result

    async def close_process(self) -> None:
        await self.rpc.close()


class CodexAppServerAdapter(AgentAdapter):
    def __init__(self, executable: str = "codex") -> None:
        self.executable = executable
        available = shutil.which(executable) is not None
        self._manifest = AdapterManifest(
            adapter_id="codex-app-server",
            provider=RelayProvider.CODEX,
            adapter_kind=AdapterKind.CODEX_APP_SERVER,
            display_name="OpenAI Codex app-server",
            executable=executable,
            available=available,
            features=[
                AdapterFeature.STRUCTURED_EVENTS,
                AdapterFeature.RESUME,
                AdapterFeature.STEER,
                AdapterFeature.CANCEL,
                AdapterFeature.NATIVE_APPROVALS,
                AdapterFeature.FILE_DIFFS,
                AdapterFeature.TEST_RESULTS,
                AdapterFeature.TOKEN_USAGE,
            ],
            metadata={"transport": "json-rpc-stdio"},
        )

    @property
    def manifest(self) -> AdapterManifest:
        return self._manifest

    async def start(self, context: AdapterContext) -> AgentSessionHandle:
        session = await self.create_session(context)
        try:
            await session.initialize()
        except Exception:
            await session.close_process()
            raise
        return session

    async def create_process(self, workspace: Path) -> asyncio.subprocess.Process:
        executable = shutil.which(self.executable)
        if executable is None:
            raise FileNotFoundError(
                "Codex executable was not found. Install and authenticate the official Codex CLI."
            )
        if not workspace.is_dir():
            raise FileNotFoundError(f"Workspace does not exist: {workspace}")
        return await asyncio.create_subprocess_exec(
            executable,
            "app-server",
            "--listen",
            "stdio://",
            cwd=str(workspace),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def create_session(self, context: AdapterContext) -> CodexAppServerSession:
        workspace = Path(context.request.workspace_path).resolve()
        process = await self.create_process(workspace)
        session = CodexAppServerSession(context=context, process=process)
        return session
