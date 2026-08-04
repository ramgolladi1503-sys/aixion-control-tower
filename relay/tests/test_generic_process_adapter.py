from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from aixion_relay.adapters.base import AdapterContext
from aixion_relay.adapters.generic_process import GenericProcessAdapter
from aixion_relay.contracts import (
    ActionDecision,
    EventType,
    PolicyDecision,
    RelayProvider,
    SessionStartRequest,
)


@pytest.mark.asyncio
async def test_generic_jsonl_agent_receives_exact_approval(tmp_path: Path) -> None:
    script = tmp_path / "fake_agent.py"
    script.write_text(
        """
import json
import sys

first = json.loads(sys.stdin.readline())
assert first["type"] == "USER_MESSAGE"
print(json.dumps({
    "type": "APPROVAL_REQUIRED",
    "request_id": "request-1",
    "action": {
        "action_type": "RUN_COMMAND",
        "command": "python -m pytest tests/test_safe.py",
        "estimated_runtime_seconds": 30
    }
}), flush=True)
decision = json.loads(sys.stdin.readline())
assert decision["type"] == "APPROVAL_DECISION"
assert decision["decision"] == "ALLOW"
print(json.dumps({
    "type": "COMMAND_OUTPUT",
    "message": "1 passed",
    "payload": {"exit_code": 0}
}), flush=True)
sys.exit(0)
""".strip()
        + "\n",
        encoding="utf-8",
    )
    events = []
    proposals = []

    async def emit(event):
        events.append(event)

    async def authorize(proposal):
        proposals.append(proposal)
        return ActionDecision(
            action_id="action-safe",
            decision=PolicyDecision.ALLOW,
        )

    adapter = GenericProcessAdapter(
        adapter_id="fake-jsonl",
        provider=RelayProvider.CUSTOM,
        display_name="Fake JSONL agent",
        argv=[sys.executable, "-u", str(script)],
    )
    handle = await adapter.start(
        AdapterContext(
            request=SessionStartRequest(
                session_id="session-1",
                objective="Run the safe validation.",
                workspace_path=str(tmp_path),
            ),
            emit=emit,
            authorize=authorize,
        )
    )
    result = await handle.wait()

    assert result.success is True
    assert len(proposals) == 1
    assert proposals[0].command == "python -m pytest tests/test_safe.py"
    assert [event.event_type for event in events] == [
        EventType.SESSION_STARTED,
        EventType.COMMAND_OUTPUT,
        EventType.SESSION_COMPLETED,
    ]


@pytest.mark.asyncio
async def test_generic_jsonl_agent_denial_is_returned_without_broadening(
    tmp_path: Path,
) -> None:
    script = tmp_path / "denied_agent.py"
    output = tmp_path / "decision.json"
    script.write_text(
        f"""
import json
import sys

json.loads(sys.stdin.readline())
print(json.dumps({{
    "type": "APPROVAL_REQUIRED",
    "request_id": "request-denied",
    "action": {{
        "action_type": "MODIFY_FILES",
        "paths": ["backend/app/unsafe.py"]
    }}
}}), flush=True)
decision = json.loads(sys.stdin.readline())
with open({str(output)!r}, "w", encoding="utf-8") as handle:
    json.dump(decision, handle)
sys.exit(2)
""".strip()
        + "\n",
        encoding="utf-8",
    )

    async def emit(_event):
        return None

    async def authorize(proposal):
        return ActionDecision(
            action_id="action-denied",
            decision=PolicyDecision.BLOCK,
            reasons=[f"Blocked exact paths: {proposal.paths}"],
        )

    adapter = GenericProcessAdapter(
        adapter_id="denied-jsonl",
        provider=RelayProvider.CUSTOM,
        display_name="Denied JSONL agent",
        argv=[sys.executable, "-u", str(script)],
    )
    handle = await adapter.start(
        AdapterContext(
            request=SessionStartRequest(
                session_id="session-denied",
                objective="Attempt an unsafe change.",
                workspace_path=str(tmp_path),
            ),
            emit=emit,
            authorize=authorize,
        )
    )
    result = await handle.wait()
    decision = json.loads(output.read_text(encoding="utf-8"))

    assert result.success is False
    assert decision["action_id"] == "action-denied"
    assert decision["decision"] == "BLOCK"
    assert decision["reasons"] == ["Blocked exact paths: ['backend/app/unsafe.py']"]
