from __future__ import annotations

import asyncio
import json
import os
import signal
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..contracts import (
    ActionProposal,
    AdapterFeature,
    AdapterKind,
    AdapterManifest,
    EventType,
    NormalizedEvent,
    RelayProvider,
    SessionResult,
)
from .base import AdapterContext, AgentAdapter, AgentSessionHandle


class GenericProcessSession(AgentSessionHandle):
    def __init__(
        self,
        *,
        context: AdapterContext,
        process: asyncio.subprocess.Process,
    ) -> None:
        self._context = context
        self._process = process
        self._remote_session_id = f"process-{process.pid}"
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    @property
    def remote_session_id(self) -> str | None:
        return self._remote_session_id

    async def _write(self, payload: dict[str, Any]) -> None:
        if self._process.stdin is None or self._process.returncode is not None:
            raise RuntimeError("Agent process input is unavailable.")
        self._process.stdin.write(
            (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        )
        await self._process.stdin.drain()

    async def _read_stdout(self) -> None:
        if self._process.stdout is None:
            return
        while line := await self._process.stdout.readline():
            text = line.decode("utf-8", errors="replace").rstrip("\n")
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                await self._context.emit(
                    NormalizedEvent(
                        event_type=EventType.AGENT_MESSAGE,
                        message=text,
                        payload={"stream": "stdout", "structured": False},
                    )
                )
                continue
            await self._handle_payload(payload)

    async def _read_stderr(self) -> None:
        if self._process.stderr is None:
            return
        while line := await self._process.stderr.readline():
            text = line.decode("utf-8", errors="replace").rstrip("\n")
            await self._context.emit(
                NormalizedEvent(
                    event_type=EventType.COMMAND_OUTPUT,
                    message=text,
                    payload={"stream": "stderr"},
                )
            )

    async def _handle_payload(self, payload: dict[str, Any]) -> None:
        kind = str(payload.get("type") or payload.get("event_type") or "RAW")
        if kind == "APPROVAL_REQUIRED":
            raw_action = payload.get("action") or {}
            decision = await self._context.authorize(
                ActionProposal.model_validate(raw_action)
            )
            await self._write(
                {
                    "type": "APPROVAL_DECISION",
                    "request_id": payload.get("request_id"),
                    "action_id": decision.action_id,
                    "decision": decision.decision.value,
                    "reasons": decision.reasons,
                }
            )
            return
        try:
            event_type = EventType(kind)
        except ValueError:
            event_type = EventType.RAW
        await self._context.emit(
            NormalizedEvent(
                event_type=event_type,
                message=str(payload.get("message") or ""),
                payload=payload.get("payload") or payload,
                remote_session_id=payload.get("remote_session_id"),
                remote_turn_id=payload.get("remote_turn_id"),
            )
        )

    async def send_message(self, message: str, metadata: dict[str, Any]) -> None:
        await self._write(
            {
                "type": "USER_MESSAGE",
                "message": message,
                "metadata": metadata,
            }
        )

    async def pause(self, reason: str) -> None:
        if os.name != "posix":
            raise RuntimeError("Process pause is supported only on POSIX hosts.")
        self._process.send_signal(signal.SIGSTOP)
        await self._context.emit(
            NormalizedEvent(
                event_type=EventType.SESSION_PAUSED,
                message=reason or "Agent process paused.",
            )
        )

    async def resume(self, reason: str) -> None:
        if os.name != "posix":
            raise RuntimeError("Process resume is supported only on POSIX hosts.")
        self._process.send_signal(signal.SIGCONT)
        await self._context.emit(
            NormalizedEvent(
                event_type=EventType.SESSION_RESUMED,
                message=reason or "Agent process resumed.",
            )
        )

    async def cancel(self, reason: str) -> None:
        if self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=10)
            except TimeoutError:
                self._process.kill()
                await self._process.wait()
        await self._context.emit(
            NormalizedEvent(
                event_type=EventType.SESSION_CANCELLED,
                message=reason or "Agent process cancelled.",
            )
        )

    async def sync(self) -> dict[str, Any]:
        return {
            "pid": self._process.pid,
            "returncode": self._process.returncode,
            "remote_session_id": self.remote_session_id,
        }

    async def wait(self) -> SessionResult:
        returncode = await self._process.wait()
        await asyncio.gather(
            self._reader_task,
            self._stderr_task,
            return_exceptions=True,
        )
        success = returncode == 0
        await self._context.emit(
            NormalizedEvent(
                event_type=(
                    EventType.SESSION_COMPLETED
                    if success
                    else EventType.SESSION_FAILED
                ),
                message=f"Agent process exited with code {returncode}.",
                payload={"returncode": returncode},
            )
        )
        return SessionResult(
            success=success,
            remote_session_id=self.remote_session_id,
            result={"returncode": returncode},
            error=None if success else f"Agent process exited with code {returncode}.",
        )


class GenericProcessAdapter(AgentAdapter):
    def __init__(
        self,
        *,
        adapter_id: str,
        provider: RelayProvider,
        display_name: str,
        argv: list[str],
        environment: dict[str, str] | None = None,
    ) -> None:
        if not argv or not all(isinstance(item, str) and item for item in argv):
            raise ValueError("Generic process argv must be a non-empty string list.")
        self._argv = list(argv)
        self._environment = dict(environment or {})
        self._manifest = AdapterManifest(
            adapter_id=adapter_id,
            provider=provider,
            adapter_kind=AdapterKind.PROCESS_JSONL,
            display_name=display_name,
            executable=argv[0],
            available=True,
            features=[
                AdapterFeature.STRUCTURED_EVENTS,
                AdapterFeature.STEER,
                AdapterFeature.CANCEL,
                AdapterFeature.NATIVE_APPROVALS,
            ],
            metadata={"protocol": "aixion-jsonl-v1"},
        )

    @property
    def manifest(self) -> AdapterManifest:
        return self._manifest

    async def start(self, context: AdapterContext) -> AgentSessionHandle:
        workspace = Path(context.request.workspace_path).resolve()
        if not workspace.is_dir():
            raise FileNotFoundError(f"Workspace does not exist: {workspace}")
        environment = os.environ.copy()
        environment.update(self._environment)
        environment.update(
            {
                "AIXION_RELAY_SESSION_ID": context.request.session_id,
                "AIXION_APPROVAL_MODE": context.request.approval_mode,
            }
        )
        process = await asyncio.create_subprocess_exec(
            *self._argv,
            cwd=str(workspace),
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        handle = GenericProcessSession(context=context, process=process)
        await context.emit(
            NormalizedEvent(
                event_type=EventType.SESSION_STARTED,
                message=f"Started {self.manifest.display_name}.",
                payload={"pid": process.pid, "launch_id": uuid4().hex},
                remote_session_id=handle.remote_session_id,
            )
        )
        await handle.send_message(
            context.request.objective,
            {"kind": "initial_objective"},
        )
        return handle
