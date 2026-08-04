from __future__ import annotations

import asyncio
import platform
from contextlib import suppress
from typing import Any
from uuid import uuid4

from . import __version__
from .adapters import AdapterContext, AdapterRegistry, AgentSessionHandle
from .client import AixionRelayClient, RelayApiError
from .config import RelayConfig
from .contracts import (
    ActionDecision,
    ActionProposal,
    AdapterFeature,
    CommandType,
    EventType,
    NormalizedEvent,
    PolicyDecision,
    RelayCommand,
    SessionResult,
    SessionStartRequest,
)


class RelayRuntimeError(RuntimeError):
    pass


class UniversalRelayRuntime:
    def __init__(
        self,
        *,
        config: RelayConfig,
        client: AixionRelayClient,
        registry: AdapterRegistry,
    ) -> None:
        self.config = config
        self.client = client
        self.registry = registry
        self._handles: dict[str, AgentSessionHandle] = {}
        self._session_tasks: dict[str, asyncio.Task[SessionResult]] = {}
        self._command_tasks: set[asyncio.Task[None]] = set()
        self._event_sequences: dict[str, int] = {}
        self._event_locks: dict[str, asyncio.Lock] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._stopping = asyncio.Event()
        self._parallel = asyncio.Semaphore(config.max_parallel_sessions)

    async def run_forever(self) -> None:
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        try:
            while not self._stopping.is_set():
                processed = await self.run_once()
                if not processed:
                    try:
                        await asyncio.wait_for(
                            self._stopping.wait(),
                            timeout=self.config.poll_seconds,
                        )
                    except TimeoutError:
                        pass
        finally:
            self._stopping.set()
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
            await self.shutdown()

    async def run_once(self) -> bool:
        command = await self.client.claim_command(
            worker_id=self.config.worker_id,
            lease_seconds=self.config.command_lease_seconds,
        )
        if command is None:
            return False
        task = asyncio.create_task(self._execute_command(command))
        self._command_tasks.add(task)
        task.add_done_callback(self._command_tasks.discard)
        return True

    async def drain_commands(self) -> None:
        while self._command_tasks:
            await asyncio.gather(*list(self._command_tasks), return_exceptions=False)

    async def _heartbeat_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.client.heartbeat(
                    version=__version__,
                    adapters=self.registry.manifests(),
                    active_session_count=len(self._handles),
                    metadata={
                        "python": platform.python_version(),
                        "platform": platform.platform(),
                    },
                )
            except RelayApiError:
                pass
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=self.config.heartbeat_seconds,
                )
            except TimeoutError:
                pass

    async def _execute_command(self, command: RelayCommand) -> None:
        async with self._parallel:
            heartbeat_task = asyncio.create_task(self._command_heartbeat(command))
            try:
                await self.client.acknowledge_command(
                    command,
                    worker_id=self.config.worker_id,
                )
                result = await self._dispatch(command)
                await self.client.complete_command(
                    command,
                    worker_id=self.config.worker_id,
                    success=result.success,
                    remote_session_id=result.remote_session_id,
                    error=result.error,
                    result=result.result,
                )
            except Exception as error:  # noqa: BLE001 - command failure must be recorded.
                with suppress(Exception):
                    await self.client.complete_command(
                        command,
                        worker_id=self.config.worker_id,
                        success=False,
                        error=str(error),
                        result={"exception_type": type(error).__name__},
                    )
            finally:
                heartbeat_task.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat_task

    async def _command_heartbeat(self, command: RelayCommand) -> None:
        interval = max(5.0, self.config.command_lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            await self.client.heartbeat_command(
                command,
                worker_id=self.config.worker_id,
                lease_seconds=self.config.command_lease_seconds,
            )

    async def _dispatch(self, command: RelayCommand) -> SessionResult:
        if command.command_type == CommandType.SHUTDOWN_RELAY:
            self._stopping.set()
            return SessionResult(success=True, result={"shutdown_requested": True})
        if not command.session_id:
            raise RelayRuntimeError(
                f"Relay command {command.command_type} requires a session id."
            )
        lock = self._session_locks.setdefault(command.session_id, asyncio.Lock())
        async with lock:
            if command.command_type == CommandType.START_SESSION:
                return await self._start_session(command)
            handle = await self._ensure_handle(command.session_id)
            reason = str(command.payload.get("reason") or "")
            if command.command_type == CommandType.SEND_MESSAGE:
                await handle.send_message(
                    str(command.payload.get("message") or ""),
                    command.payload.get("metadata") or {},
                )
            elif command.command_type == CommandType.PAUSE_SESSION:
                await handle.pause(reason)
            elif command.command_type == CommandType.RESUME_SESSION:
                await handle.resume(reason)
            elif command.command_type == CommandType.CANCEL_SESSION:
                await handle.cancel(reason)
                self._handles.pop(command.session_id, None)
            elif command.command_type == CommandType.SYNC_SESSION:
                state = await handle.sync()
                return SessionResult(
                    success=True,
                    remote_session_id=handle.remote_session_id,
                    result=state,
                )
            else:
                raise RelayRuntimeError(
                    f"Unsupported relay command type: {command.command_type}"
                )
            return SessionResult(
                success=True,
                remote_session_id=handle.remote_session_id,
                result={"command_type": command.command_type.value},
            )

    async def _start_session(self, command: RelayCommand) -> SessionResult:
        session_id = command.session_id
        assert session_id is not None
        if session_id in self._handles:
            handle = self._handles[session_id]
            return SessionResult(
                success=True,
                remote_session_id=handle.remote_session_id,
                result={"duplicate_start_ignored": True},
            )
        await self._load_sequence(session_id)
        payload = command.payload
        request = SessionStartRequest(
            session_id=session_id,
            objective=str(payload.get("objective") or ""),
            workspace_path=str(payload.get("workspace_path") or ""),
            repository=payload.get("repository"),
            approval_mode=str(payload.get("approval_mode") or "STRICT"),
            model=payload.get("model"),
            max_runtime_seconds=int(payload.get("max_runtime_seconds") or 3600),
            metadata=payload.get("metadata") or {},
        )
        adapter_id = str(payload.get("adapter_id") or "")
        adapter = self.registry.get(adapter_id)
        context = self._context(request)
        await self._emit(
            session_id,
            NormalizedEvent(
                event_type=EventType.SESSION_STARTING,
                message=f"Starting adapter {adapter_id}.",
                payload={"provider": adapter.manifest.provider.value},
            ),
        )
        handle = await adapter.start(context)
        self._handles[session_id] = handle
        self._session_tasks[session_id] = asyncio.create_task(
            self._monitor_session(session_id, handle)
        )
        return SessionResult(
            success=True,
            remote_session_id=handle.remote_session_id,
            result={"adapter_id": adapter_id, "started": True},
        )

    async def _ensure_handle(self, session_id: str) -> AgentSessionHandle:
        handle = self._handles.get(session_id)
        if handle is not None:
            return handle
        detail = await self.client.get_session_detail(session_id)
        session = detail["session"]
        adapter = self.registry.get(str(session["adapter_id"]))
        if AdapterFeature.RESUME not in adapter.manifest.features:
            raise RelayRuntimeError(
                f"Adapter {adapter.manifest.adapter_id} cannot restore a session after relay restart."
            )
        remote_session_id = session.get("remote_session_id")
        if not remote_session_id:
            raise RelayRuntimeError(
                f"Adapter {adapter.manifest.adapter_id} cannot restore a session without a provider session id."
            )
        await self._load_sequence(session_id, detail=detail)
        request = SessionStartRequest(
            session_id=session_id,
            objective="Resume the existing provider session without starting new work.",
            workspace_path=str(session["workspace_path"]),
            repository=session.get("repository"),
            approval_mode=str(session.get("approval_mode") or "STRICT"),
            model=session.get("model"),
            max_runtime_seconds=int(session.get("max_runtime_seconds") or 3600),
            metadata={
                **(session.get("metadata") or {}),
                "remote_session_id": remote_session_id,
                "resume_only": True,
            },
        )
        handle = await adapter.start(self._context(request))
        self._handles[session_id] = handle
        self._session_tasks[session_id] = asyncio.create_task(
            self._monitor_session(session_id, handle)
        )
        return handle

    def _context(self, request: SessionStartRequest) -> AdapterContext:
        return AdapterContext(
            request=request,
            emit=lambda event: self._emit(request.session_id, event),
            authorize=lambda proposal: self._authorize(request, proposal),
        )

    async def _load_sequence(
        self,
        session_id: str,
        *,
        detail: dict[str, Any] | None = None,
    ) -> None:
        if session_id in self._event_sequences:
            return
        detail = detail or await self.client.get_session_detail(session_id)
        self._event_sequences[session_id] = int(
            detail.get("session", {}).get("latest_event_sequence") or 0
        )

    async def _emit(self, session_id: str, event: NormalizedEvent) -> None:
        lock = self._event_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            await self._load_sequence(session_id)
            sequence = self._event_sequences[session_id] + 1
            event_id = f"{session_id}:{sequence}:{uuid4().hex}"
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    await self.client.post_event(
                        session_id,
                        event_id,
                        sequence,
                        event,
                    )
                    self._event_sequences[session_id] = sequence
                    return
                except RelayApiError as error:
                    last_error = error
                    if error.status_code and error.status_code < 500:
                        raise
                    await asyncio.sleep(0.5 * (attempt + 1))
            raise RelayRuntimeError(
                f"Unable to persist relay event after retries: {last_error}"
            )

    async def _authorize(
        self,
        request: SessionStartRequest,
        proposal: ActionProposal,
    ) -> ActionDecision:
        detail = await self.client.get_session_detail(request.session_id)
        session = detail["session"]
        enriched = proposal.model_copy(
            update={
                "run_id": proposal.run_id or session.get("run_id"),
                "task_id": proposal.task_id or session.get("task_id"),
                "project_id": proposal.project_id or session.get("project_id"),
                "repository": proposal.repository or session.get("repository"),
                "metadata": {
                    **proposal.metadata,
                    "relay_session_id": request.session_id,
                    "workspace_path": request.workspace_path,
                },
            }
        )
        decision = await self.client.propose_action(request.session_id, enriched)
        if decision.decision != PolicyDecision.REQUIRE_APPROVAL:
            return decision
        deadline = asyncio.get_running_loop().time() + request.max_runtime_seconds
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(1.0)
            decision = await self.client.get_action_status(
                request.session_id,
                decision.action_id,
            )
            if decision.decision != PolicyDecision.REQUIRE_APPROVAL:
                return decision
        return ActionDecision(
            action_id=decision.action_id,
            decision=PolicyDecision.BLOCK,
            reasons=["Aixion approval timed out before the session runtime budget expired."],
        )

    async def _monitor_session(
        self,
        session_id: str,
        handle: AgentSessionHandle,
    ) -> SessionResult:
        try:
            return await handle.wait()
        finally:
            self._handles.pop(session_id, None)
            self._session_tasks.pop(session_id, None)

    async def shutdown(self) -> None:
        self._stopping.set()
        for session_id, handle in list(self._handles.items()):
            with suppress(Exception):
                await handle.cancel("Aixion relay is shutting down.")
            self._handles.pop(session_id, None)
        session_tasks = list(self._session_tasks.values())
        for task in session_tasks:
            task.cancel()
        if session_tasks:
            await asyncio.gather(*session_tasks, return_exceptions=True)
        command_tasks = list(self._command_tasks)
        if command_tasks:
            await asyncio.gather(*command_tasks, return_exceptions=True)
        await self.registry.close()
        await self.client.close()
