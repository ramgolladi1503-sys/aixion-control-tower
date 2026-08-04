from __future__ import annotations

import json

import httpx
import pytest

from aixion_relay.client import AixionRelayClient, RelayApiError
from aixion_relay.contracts import (
    ActionProposal,
    CapabilityActionType,
    PolicyDecision,
)


@pytest.mark.asyncio
async def test_client_uses_relay_token_and_parses_policy_decision() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["X-Aixion-Relay-Token"] == "relay-secret"
        if request.url.path.endswith("/actions"):
            body = json.loads(request.content)
            assert body["action"]["command"] == "pytest tests/test_safe.py"
            return httpx.Response(
                200,
                json={
                    "action": {"id": "action-1"},
                    "decision": {
                        "decision": "REQUIRE_APPROVAL",
                        "reasons": ["Strict mode"],
                        "obligations": [],
                    },
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = AixionRelayClient(
        base_url="https://aixion.test",
        relay_id="relay-1",
        relay_token="relay-secret",
        transport=httpx.MockTransport(handler),
    )
    try:
        decision = await client.propose_action(
            "session-1",
            ActionProposal(
                action_type=CapabilityActionType.RUN_COMMAND,
                command="pytest tests/test_safe.py",
            ),
        )
    finally:
        await client.close()

    assert decision.action_id == "action-1"
    assert decision.decision == PolicyDecision.REQUIRE_APPROVAL
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_client_surfaces_fail_closed_api_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "Policy blocked action"})

    client = AixionRelayClient(
        base_url="https://aixion.test",
        relay_id="relay-1",
        relay_token="relay-secret",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(RelayApiError, match="Policy blocked action"):
            await client.get_session_detail("session-1")
    finally:
        await client.close()
