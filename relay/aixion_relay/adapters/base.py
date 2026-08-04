from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..contracts import (
    ActionDecision,
    ActionProposal,
    AdapterManifest,
    NormalizedEvent,
    SessionResult,
    SessionStartRequest,
)

EventSink = Callable[[NormalizedEvent], Awaitable[None]]
ActionBroker = Callable[[ActionProposal], Awaitable[ActionDecision]]


@dataclass(frozen=True)
class AdapterContext:
    request: SessionStartRequest
    emit: EventSink
    authorize: ActionBroker


class AgentSessionHandle(ABC):
    @property
    @abstractmethod
    def remote_session_id(self) -> str | None:
        raise NotImplementedError

    @abstractmethod
    async def send_message(self, message: str, metadata: dict[str, Any]) -> None:
        raise NotImplementedError

    @abstractmethod
    async def pause(self, reason: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def resume(self, reason: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def cancel(self, reason: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def sync(self) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def wait(self) -> SessionResult:
        raise NotImplementedError


class AgentAdapter(ABC):
    @property
    @abstractmethod
    def manifest(self) -> AdapterManifest:
        raise NotImplementedError

    @abstractmethod
    async def start(self, context: AdapterContext) -> AgentSessionHandle:
        raise NotImplementedError

    async def close(self) -> None:
        return None
