from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from aixion_relay.adapters.base import AdapterContext, AgentAdapter, AgentSessionHandle
from aixion_relay.adapters.registry import AdapterRegistry
from aixion_relay.config import RelayConfig
from aixion_relay.contracts import (
    ActionDecision,
    ActionProposal,
    AdapterFeature,
    AdapterKind,
    AdapterManifest,
    CapabilityActionType,
    CommandType,
    EventType,
    NormalizedEvent,
    PolicyDecision,
    RelayCommand,
    RelayProvider,
    SessionResult,
)
from aixion_relay.runtime import UniversalRelayRuntime


class FakeHandle(AgentSessionHandle):
    def __init__(self, context: AdapterContext) -> None:
        self.context = context
        self.messages: list[str] = []
        self.cancelled = False
        self._done: asyncio.Future[SessionResult] = asyncio.get_running_loop().create_future()

    @property
    def remote_session_id(self) -> str | None:
        return "remote-fake"

    async def send_message(self, message: str, metadata: dict[str, Any]) -> None:
        del metadata
        self.messages.append(message)

    async def pause(self, reason: str) -> None:
        await self.context.emit(
            NormalizedEvent(event_type=EventType.SESSION_PAUSED, message=reason)
        )

    async def resume(self, reason: str) -> None:
        await self.context.emit(
            NormalizedEvent(event_type=EventType.SESSION_RESUMED, message=reason)
        )

    async def cancel(self, reason: str) -> None:
        self.cancelled = True
        if not self._done.done():
            self._done.set_result(SessionResult(success=False, error=reason))

    async def sync(self) -> dict[str, Any]:
        return {"messages": list(self.messages)}

    async def wait(self) -> SessionResult:
        return await self._done


class FakeAdapter(AgentAdapter):
    def __init__(self) -> None:
        self.handle: FakeHandle | None = None
        self._manifest = AdapterManifest(
            adapter_id="fake-adapter",
            provider=RelayProvider.CUSTOM,
            adapter_kind=AdapterKind.GENERIC,
            display_name="Fake",
            available=True,
            features=[AdapterFeature.STRUCTURED_EVENTS, AdapterFeature.CANCEL],
        )

    @property
    def manifest(self) -> AdapterManifest:
        return self._manifest

    async def start(self, context: AdapterContext) -> AgentSessionHandle:
        self.handle = FakeHandle(context)
        await context.emit(
            NormalizedEvent(
                event_type=EventType.SESSION_STARTED,
                message="Fake started",
                remote_session_id="remote-fake",
            )
        )
        return self.handle


class FakeClient:
    def __init__(self) -> None:
        self.events: list[tuple[int, NormalizedEvent]] = []
        self.completed: list[dict[str, Any]] = []
        self.decisions = [
            ActionDecision(
                action_id="action-1",
                decision=PolicyDecision.REQUIRE_APPROVAL,
            ),
            ActionDecision(
                action_id="action-1",
                decision=PolicyDecision.ALLOW,
            ),
        ]

    async def get_session_detail(self, session_id: str) -> dict[str, Any]:
        return {
            "session": {
                "id": session_id,
                "relay_id": "relay-1",
                "provider": "CUSTOM",
                "adapter_id": "fake-adapter",
                "objective": "Do safe work",
                "workspace_path": "/tmp",
                "repository": "owner/repo",
                "approval_mode": "STRICT",
                "max_runtime_seconds": 30,
                "latest_event_sequence": len(self.events),
                "metadata": {},
            },
            "commands": [],
            "events": [],
            "relay": {"id": "relay-1"},
        }

    async def post_event(
        self,
        session_id: str,
        event_id: str,
        sequence: int,
        event: NormalizedEvent,
    ) -> None:
        del session_id, event_id
        assert sequence == len(self.events) + 1
        self.events.append((sequence, event))

    async def propose_action(
        self,
        session_id: str,
        proposal: ActionProposal,
    ) -> ActionDecision:
        del session_id
        assert proposal.repository == "owner/repo"
        return self.decisions[0]

    async def get_action_status(
        self,
        session_id: str,
        action_id: str,
    ) -> ActionDecision:
        del session_id, action_id
        return self.decisions.pop(0) if len(self.decisions) == 1 else self.decisions.pop(1)

    async def complete_command(self, *args: Any, **kwargs: Any) -> None:
        self.completed.append(kwargs)

    async def acknowledge_command(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def heartbeat_command(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def heartbeat(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def claim_command(self, *args: Any, **kwargs: Any):
        return None

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_runtime_starts_adapter_and_preserves_event_order(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    client = FakeClient()
    config = RelayConfig(
        api_base_url="https://aixion.test",
        relay_id="relay-1",
        relay_token_secret_name="secret",
        worker_id="worker-1",
        workspace_roots=[str(tmp_path)],
        max_parallel_sessions=1,
    )
    runtime = UniversalRelayRuntime(
        config=config,
        client=client,  # type: ignore[arg-type]
        registry=AdapterRegistry([adapter]),
    )
    command = RelayCommand(
        id="command-1",
        relay_id="relay-1",
        session_id="session-1",
        command_type=CommandType.START_SESSION,
        status="LEASED",
        payload={
            "objective": "Do safe work",
            "workspace_path": str(tmp_path),
            "repository": "owner/repo",
            "provider": "CUSTOM",
            "adapter_id": "fake-adapter",
            "approval_mode": "STRICT",
            "max_runtime_seconds": 30,
        },
        idempotency_key="idempotency-1",
        lease_owner="worker-1",
        lease_token="lease-token-long-enough",
    )

    result = await runtime._dispatch(command)
    assert result.success is True
    assert adapter.handle is not None
    assert [event.event_type for _, event in client.events] == [
        EventType.SESSION_STARTING,
        EventType.SESSION_STARTED,
    ]
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_runtime_waits_for_exact_mobile_approval(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    client = FakeClient()
    config = RelayConfig(
        api_base_url="https://aixion.test",
        relay_id="relay-1",
        relay_token_secret_name="secret",
        worker_id="worker-1",
        workspace_roots=[str(tmp_path)],
    )
    runtime = UniversalRelayRuntime(
        config=config,
        client=client,  # type: ignore[arg-type]
        registry=AdapterRegistry([adapter]),
    )
    request = runtime._context(
        __import__("aixion_relay.contracts", fromlist=["SessionStartRequest"])
        .SessionStartRequest(
            session_id="session-1",
            objective="Do safe work",
            workspace_path=str(tmp_path),
            repository="owner/repo",
            max_runtime_seconds=5,
        )
    )
    decision = await request.authorize(
        ActionProposal(
            action_type=CapabilityActionType.RUN_COMMAND,
            command="pytest tests/test_safe.py",
        )
    )
    assert decision.decision == PolicyDecision.ALLOW
    await runtime.shutdown()
