from __future__ import annotations

from typing import Any

import pytest
from aixion_relay.adapters.base import AdapterContext
from aixion_relay.adapters.claude import _tool_action
from aixion_relay.adapters.codex import CodexAppServerSession, _thread_id
from aixion_relay.contracts import (
    ActionDecision,
    CapabilityActionType,
    EventType,
    PolicyDecision,
    SessionStartRequest,
)


class FakeRpc:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.notifications: list[tuple[str, dict[str, Any] | None]] = []
        self.closed = False

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.requests.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "thread-fake"}}
        if method == "turn/start":
            return {"turn": {"id": f"turn-{len(self.requests)}"}}
        return {}

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.notifications.append((method, params))

    async def close(self) -> None:
        self.closed = True


class FakeProcess:
    pid = 4321
    returncode = None


def test_codex_thread_identity_and_notification_mapping() -> None:
    assert _thread_id({"thread": {"id": "thread-1"}}) == "thread-1"
    assert _thread_id({"threadId": "thread-2"}) == "thread-2"
    assert _thread_id({}, "thread-existing") == "thread-existing"

    event_type, message = CodexAppServerSession._normalize_notification(
        "item/commandExecution/outputDelta",
        {"delta": "1 passed"},
    )
    assert event_type == EventType.COMMAND_OUTPUT
    assert message == "1 passed"

    event_type, _ = CodexAppServerSession._normalize_notification(
        "turn/completed",
        {},
    )
    assert event_type == EventType.SESSION_COMPLETED


@pytest.mark.asyncio
async def test_codex_lifecycle_separates_thread_from_first_turn() -> None:
    events = []

    async def emit(event):
        events.append(event)

    async def authorize(_proposal):
        raise AssertionError("No approval expected")

    session = object.__new__(CodexAppServerSession)
    session.context = AdapterContext(
        request=SessionStartRequest(
            session_id="session-1",
            objective="",
            workspace_path="/tmp/repo",
        ),
        emit=emit,
        authorize=authorize,
    )
    session.process = FakeProcess()
    session.rpc = FakeRpc()
    session.thread_id = None
    session.turn_id = None
    session._initialized = False
    session._thread_started = False
    session._finished = __import__("asyncio").get_running_loop().create_future()
    session._cancelled = False

    await session.initialize()
    assert [method for method, _ in session.rpc.requests] == [
        "initialize",
        "thread/start",
    ]
    assert session.thread_id == "thread-fake"
    assert session.turn_id is None

    await session.start_turn("first prompt")
    await session.start_turn("second prompt")

    assert [method for method, _ in session.rpc.requests] == [
        "initialize",
        "thread/start",
        "turn/start",
        "turn/start",
    ]
    assert all(
        params.get("threadId") == "thread-fake"
        for method, params in session.rpc.requests
        if method == "turn/start"
    )


@pytest.mark.asyncio
async def test_codex_command_approval_uses_exact_command() -> None:
    events = []
    proposals = []

    async def emit(event):
        events.append(event)

    async def authorize(proposal):
        proposals.append(proposal)
        return ActionDecision(
            action_id="action-command",
            decision=PolicyDecision.ALLOW,
        )

    session = object.__new__(CodexAppServerSession)
    session.context = AdapterContext(
        request=SessionStartRequest(
            session_id="session-1",
            objective="Test Codex approval.",
            workspace_path="/tmp/repo",
        ),
        emit=emit,
        authorize=authorize,
    )
    session.thread_id = "thread-1"
    session.turn_id = "turn-1"

    response = await session._on_request(
        "item/commandExecution/requestApproval",
        {
            "item": {
                "id": "item-1",
                "command": ["python", "-m", "pytest", "tests/test_safe.py"],
                "cwd": "/tmp/repo",
            }
        },
    )

    assert response == {"decision": "accept"}
    assert len(proposals) == 1
    assert proposals[0].action_type == CapabilityActionType.RUN_COMMAND
    assert proposals[0].command == "python -m pytest tests/test_safe.py"
    assert [event.event_type for event in events] == [
        EventType.APPROVAL_REQUIRED,
        EventType.APPROVAL_RESOLVED,
    ]
    assert proposals[0].metadata["available_decisions"] == ["accept", "decline"]
    assert proposals[0].metadata["thread_id"] == "thread-1"
    assert proposals[0].metadata["turn_id"] == "turn-1"


@pytest.mark.asyncio
async def test_codex_preserves_provider_session_decision() -> None:
    proposals = []

    async def emit(_event):
        return None

    async def authorize(proposal):
        proposals.append(proposal)
        return ActionDecision(
            action_id="action-command",
            decision=PolicyDecision.ALLOW,
            obligations=["provider_decision:acceptForSession"],
        )

    session = object.__new__(CodexAppServerSession)
    session.context = AdapterContext(
        request=SessionStartRequest(
            session_id="session-1",
            objective="Test Codex approval.",
            workspace_path="/tmp/repo",
        ),
        emit=emit,
        authorize=authorize,
    )
    session.thread_id = "thread-1"
    session.turn_id = "turn-1"

    response = await session._on_request(
        "item/commandExecution/requestApproval",
        {
            "item": {
                "id": "item-1",
                "command": "pytest",
                "availableDecisions": ["accept", "acceptForSession", "decline"],
            }
        },
    )

    assert response == {"decision": "acceptForSession"}
    assert proposals[0].metadata["available_decisions"] == [
        "accept",
        "acceptForSession",
        "decline",
    ]


@pytest.mark.asyncio
async def test_codex_rejects_unavailable_provider_decision() -> None:
    async def emit(_event):
        return None

    async def authorize(_proposal):
        return ActionDecision(
            action_id="action-command",
            decision=PolicyDecision.ALLOW,
            obligations=["provider_decision:acceptForSession"],
        )

    session = object.__new__(CodexAppServerSession)
    session.context = AdapterContext(
        request=SessionStartRequest(
            session_id="session-1",
            objective="Test Codex approval.",
            workspace_path="/tmp/repo",
        ),
        emit=emit,
        authorize=authorize,
    )
    session.thread_id = "thread-1"
    session.turn_id = "turn-1"

    with pytest.raises(RuntimeError, match="not available"):
        await session._on_request(
            "item/commandExecution/requestApproval",
            {
                "item": {
                    "id": "item-1",
                    "command": "pytest",
                    "availableDecisions": ["accept", "decline"],
                }
            },
        )


@pytest.mark.asyncio
async def test_codex_file_approval_falls_back_to_grant_root_and_denies() -> None:
    proposals = []

    async def emit(_event):
        return None

    async def authorize(proposal):
        proposals.append(proposal)
        return ActionDecision(
            action_id="action-file",
            decision=PolicyDecision.BLOCK,
            reasons=["Outside approved path"],
        )

    session = object.__new__(CodexAppServerSession)
    session.context = AdapterContext(
        request=SessionStartRequest(
            session_id="session-2",
            objective="Test file approval.",
            workspace_path="/tmp/repo",
        ),
        emit=emit,
        authorize=authorize,
    )
    session.thread_id = "thread-2"
    session.turn_id = "turn-2"

    response = await session._on_request(
        "item/fileChange/requestApproval",
        {"item": {"id": "file-1", "grantRoot": "/tmp/repo/backend"}},
    )

    assert response == {"decision": "decline"}
    assert proposals[0].action_type == CapabilityActionType.MODIFY_FILES
    assert proposals[0].paths == ["/tmp/repo/backend"]


def test_claude_tools_map_to_common_action_contract() -> None:
    command = _tool_action("Bash", {"command": "pytest tests/test_safe.py"})
    assert command.action_type == CapabilityActionType.RUN_COMMAND
    assert command.command == "pytest tests/test_safe.py"

    file_change = _tool_action("Edit", {"file_path": "backend/app/safe.py"})
    assert file_change.action_type == CapabilityActionType.MODIFY_FILES
    assert file_change.paths == ["backend/app/safe.py"]

    network = _tool_action("WebFetch", {"url": "https://api.example.com/v1"})
    assert network.action_type == CapabilityActionType.ACCESS_NETWORK
    assert network.network_domains == ["https://api.example.com/v1"]

    unknown = _tool_action("DangerousUnknownTool", {"value": 1})
    assert unknown.action_type == CapabilityActionType.CUSTOM
