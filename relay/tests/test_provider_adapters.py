from __future__ import annotations

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
    assert event_type == EventType.AGENT_MESSAGE


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
