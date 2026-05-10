from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from fastapi.testclient import TestClient

from app.main import app
from app.mcp_gateway_routes import child_received_count_for_test, reset_gateway_runtime_for_test
from app.models import MCPPendingStatus
from app.store import store

client = TestClient(app)


def setup_function() -> None:
    store.reset()
    reset_gateway_runtime_for_test()


def _create_project() -> str:
    response = client.post(
        "/projects",
        json={"name": "MCP Transport Proof", "description": "Protocol-facing gateway proof"},
    )
    assert response.status_code == 200
    return response.json()["id"]


def _register_child_server(project_id: str, *, enabled: bool = True) -> dict:
    response = client.post(
        "/mcp-registry/child-servers",
        json={
            "project_id": project_id,
            "name": "filesystem",
            "description": "Filesystem child server",
            "transport": "test",
            "endpoint": "memory://filesystem",
            "enabled": enabled,
            "tools": [
                {
                    "name": "read_file",
                    "description": "Read a file",
                    "input_schema": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    },
                    "mutating": False,
                },
                {
                    "name": "write_file",
                    "description": "Write a file",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                    },
                    "mutating": True,
                },
            ],
        },
    )
    assert response.status_code == 200
    return response.json()


def _jsonrpc(project_id: str, method: str, params: dict | None = None, request_id: str = "rpc_1"):
    return client.post(
        f"/mcp/{project_id}/jsonrpc",
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}},
    )


def test_mcp_initialize_and_tools_list_demo_fallback() -> None:
    project_id = _create_project()

    initialized = _jsonrpc(project_id, "initialize", request_id="init_1")
    listed = _jsonrpc(project_id, "tools/list", request_id="list_1")

    assert initialized.status_code == 200
    assert initialized.json()["id"] == "init_1"
    assert initialized.json()["result"]["serverInfo"]["name"] == "aixion-control-tower-mcp-gateway"

    assert listed.status_code == 200
    tools = listed.json()["result"]["tools"]
    assert {tool["name"] for tool in tools} >= {"update_config", "read_status"}
    assert tools[0]["annotations"]["server_name"] == "child-test-server"


def test_mcp_tools_list_uses_enabled_child_server_registry() -> None:
    project_id = _create_project()
    server = _register_child_server(project_id)

    listed = _jsonrpc(project_id, "tools/list", request_id="list_registry")

    assert listed.status_code == 200
    tools = listed.json()["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["read_file", "write_file"]
    assert tools[0]["annotations"]["server_name"] == "filesystem"
    assert tools[0]["annotations"]["server_id"] == server["id"]
    assert tools[0]["annotations"]["mutating"] is False
    assert tools[1]["annotations"]["mutating"] is True


def test_mcp_tools_list_ignores_disabled_child_servers() -> None:
    project_id = _create_project()
    _register_child_server(project_id, enabled=False)

    listed = _jsonrpc(project_id, "tools/list", request_id="list_disabled")

    assert listed.status_code == 200
    tools = listed.json()["result"]["tools"]
    assert {tool["name"] for tool in tools} >= {"update_config", "read_status"}
    assert "read_file" not in {tool["name"] for tool in tools}


def test_mcp_tools_call_mutating_request_waits_for_approval() -> None:
    project_id = _create_project()

    response = _jsonrpc(
        project_id,
        "tools/call",
        {
            "name": "update_config",
            "arguments": {"key": "demo", "value": "new-value"},
            "session_id": "transport_session",
            "requested_by": "mcp-client-test",
            "mutating": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["error"] is None
    structured = body["result"]["structuredContent"]
    assert structured["forwarded"] is False
    assert structured["approval_required"] is True
    assert structured["approval_request_id"] is not None
    assert child_received_count_for_test() == 0
    assert len(store.mcp_pending_requests) == 1
    assert next(iter(store.mcp_pending_requests.values())).status == MCPPendingStatus.WAITING_FOR_APPROVAL


def test_mcp_tools_call_read_only_forwards_without_approval() -> None:
    project_id = _create_project()

    response = _jsonrpc(
        project_id,
        "tools/call",
        {
            "name": "read_status",
            "arguments": {"key": "demo"},
            "mutating": False,
        },
    )

    assert response.status_code == 200
    structured = response.json()["result"]["structuredContent"]
    assert structured["forwarded"] is True
    assert structured["approval_required"] is False
    assert structured["approval_request_id"] is None
    assert child_received_count_for_test() == 1
    assert store.mcp_pending_requests == {}


def test_mcp_tools_call_can_be_approved_and_resolved_after_transport_submit() -> None:
    project_id = _create_project()

    call = _jsonrpc(
        project_id,
        "tools/call",
        {
            "name": "update_config",
            "arguments": {"key": "demo", "value": "approved"},
            "mutating": True,
        },
    )
    approval_id = call.json()["result"]["structuredContent"]["approval_request_id"]
    assert approval_id is not None
    assert child_received_count_for_test() == 0

    decision = client.post(
        f"/approvals/{approval_id}/decision",
        json={"decision": "approve", "reason": "approve transport-submitted MCP call"},
    )
    assert decision.status_code == 200

    resolved = client.post(f"/mcp-gateway/approvals/{approval_id}/resolve")

    assert resolved.status_code == 200
    assert resolved.json()["forwarded"] is True
    assert child_received_count_for_test() == 1
    assert next(iter(store.mcp_pending_requests.values())).status == MCPPendingStatus.FORWARDED


def test_mcp_jsonrpc_errors_for_invalid_method_and_params() -> None:
    project_id = _create_project()

    unsupported = _jsonrpc(project_id, "resources/list")
    missing_name = _jsonrpc(project_id, "tools/call", {"arguments": {"key": "demo"}})
    bad_arguments = _jsonrpc(project_id, "tools/call", {"name": "update_config", "arguments": []})

    assert unsupported.status_code == 200
    assert unsupported.json()["error"]["code"] == -32601

    assert missing_name.status_code == 200
    assert missing_name.json()["error"]["code"] == -32602

    assert bad_arguments.status_code == 200
    assert bad_arguments.json()["result"] is None
    assert bad_arguments.json()["error"]["code"] == -32602
    assert "arguments must be an object" in bad_arguments.json()["error"]["message"]


def test_mcp_jsonrpc_returns_404_for_unknown_project() -> None:
    response = client.post(
        "/mcp/project_missing/jsonrpc",
        json={"jsonrpc": "2.0", "id": "missing", "method": "initialize", "params": {}},
    )

    assert response.status_code == 404
