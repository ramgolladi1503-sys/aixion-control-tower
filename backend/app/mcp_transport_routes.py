from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import require_api_key
from .mcp_gateway import MCPGatewayDecision, MCPGatewayToolCall
from .mcp_gateway_routes import gateway_for_project
from .models import MCPChildServer
from .store import store

router = APIRouter(prefix="/mcp", tags=["mcp-transport"])
AuthDependency = Depends(require_api_key)

SUPPORTED_METHODS = {"initialize", "tools/list", "tools/call"}


class MCPJsonRpcRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: str | int | None = None
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class MCPJsonRpcError(BaseModel):
    code: int
    message: str
    data: dict[str, Any] | None = None


class MCPJsonRpcResponse(BaseModel):
    jsonrpc: str = "2.0"
    id: str | int | None = None
    result: dict[str, Any] | None = None
    error: MCPJsonRpcError | None = None


def _result_response(request_id: str | int | None, result: dict[str, Any]) -> MCPJsonRpcResponse:
    return MCPJsonRpcResponse(id=request_id, result=result)


def _error_response(
    request_id: str | int | None,
    *,
    code: int,
    message: str,
    data: dict[str, Any] | None = None,
) -> MCPJsonRpcResponse:
    return MCPJsonRpcResponse(id=request_id, error=MCPJsonRpcError(code=code, message=message, data=data))


def _enabled_child_servers(project_id: str) -> list[MCPChildServer]:
    return [
        server
        for server in store.mcp_child_servers.values()
        if server.project_id == project_id and server.enabled
    ]


def _registered_tools(project_id: str) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for server in _enabled_child_servers(project_id):
        for tool in server.tools:
            tools.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": tool.input_schema,
                    "annotations": {
                        "server_name": server.name,
                        "server_id": server.id,
                        "mutating": tool.mutating,
                    },
                }
            )
    if tools:
        return tools
    return _demo_tools()


def _demo_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "update_config",
            "description": "Demo mutating tool routed through Aixion approval wait mode.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                },
            },
            "annotations": {"server_name": "child-test-server", "mutating": True},
        },
        {
            "name": "read_status",
            "description": "Demo read-only tool that may pass without approval.",
            "inputSchema": {
                "type": "object",
                "properties": {"key": {"type": "string"}},
            },
            "annotations": {"server_name": "child-test-server", "mutating": False},
        },
    ]


def _tool_call_from_params(params: dict[str, Any]) -> MCPGatewayToolCall:
    tool_name = params.get("name")
    if not isinstance(tool_name, str) or not tool_name.strip():
        raise ValueError("tools/call params must include non-empty name")

    arguments = params.get("arguments", {})
    if not isinstance(arguments, dict):
        raise ValueError("tools/call arguments must be an object")

    server_name = params.get("server_name") or "child-test-server"
    if not isinstance(server_name, str):
        raise ValueError("server_name must be a string")

    session_id = params.get("session_id")
    if session_id is not None and not isinstance(session_id, str):
        raise ValueError("session_id must be a string")

    requested_by = params.get("requested_by") or "mcp-jsonrpc-client"
    if not isinstance(requested_by, str):
        raise ValueError("requested_by must be a string")

    mutating = params.get("mutating")
    if mutating is not None and not isinstance(mutating, bool):
        raise ValueError("mutating must be a boolean when provided")

    return MCPGatewayToolCall(
        server_name=server_name,
        tool_name=tool_name,
        arguments=arguments,
        session_id=session_id,
        requested_by=requested_by,
        mutating=mutating,
    )


def _gateway_decision_to_mcp_result(decision: MCPGatewayDecision) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": decision.reason or "MCP gateway decision recorded.",
            }
        ],
        "isError": False,
        "structuredContent": {
            "forwarded": decision.forwarded,
            "approval_required": decision.approval_required,
            "approval_request_id": decision.approval_request_id,
            "status": decision.status,
            "result": decision.result,
            "reason": decision.reason,
        },
    }


@router.post("/{project_id}/jsonrpc", response_model=MCPJsonRpcResponse)
def handle_mcp_jsonrpc(
    project_id: str,
    payload: MCPJsonRpcRequest,
    _: None = AuthDependency,
) -> MCPJsonRpcResponse:
    if project_id not in store.projects:
        raise HTTPException(status_code=404, detail="Project not found")
    if payload.jsonrpc != "2.0":
        return _error_response(payload.id, code=-32600, message="Only JSON-RPC 2.0 is supported")
    if payload.method not in SUPPORTED_METHODS:
        return _error_response(payload.id, code=-32601, message="Unsupported MCP method")

    if payload.method == "initialize":
        return _result_response(
            payload.id,
            {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "aixion-control-tower-mcp-gateway", "version": "0.1.0"},
                "capabilities": {"tools": {"listChanged": False}},
            },
        )

    if payload.method == "tools/list":
        return _result_response(payload.id, {"tools": _registered_tools(project_id)})

    try:
        tool_call = _tool_call_from_params(payload.params)
    except ValueError as exc:
        return _error_response(payload.id, code=-32602, message=str(exc))

    decision = gateway_for_project(project_id).submit_tool_call(tool_call)
    return _result_response(payload.id, _gateway_decision_to_mcp_result(decision))
