from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import require_maintainer
from .mcp_gateway import MCPGatewayDecision, MCPGatewayToolCall
from .mcp_gateway_routes import gateway_for_project
from .models import AuthUser, MCPChildServer
from .store import store

router = APIRouter(prefix="/mcp", tags=["mcp-transport"])
MaintainerDependency = Depends(require_maintainer)

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


def _project_child_servers(project_id: str) -> list[MCPChildServer]:
    return [server for server in store.mcp_child_servers.values() if server.project_id == project_id]


def _enabled_child_servers(project_id: str) -> list[MCPChildServer]:
    return [server for server in _project_child_servers(project_id) if server.enabled]


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


def _registered_tool_policy(
    project_id: str,
    tool_name: str,
    requested_server_name: str | None,
) -> tuple[str, bool, dict[str, Any]] | None:
    registered_servers = _project_child_servers(project_id)
    if not registered_servers:
        return None

    matches = []
    for server in registered_servers:
        if requested_server_name is not None and requested_server_name != server.name:
            continue
        if not server.enabled:
            continue
        for tool in server.tools:
            if tool.name == tool_name:
                matches.append((server, tool))

    if len(matches) == 1:
        server, tool = matches[0]
        return server.name, tool.mutating, tool.input_schema
    if len(matches) > 1:
        raise ValueError("tools/call is ambiguous; provide server_name")
    raise ValueError("tools/call references an unknown or disabled registered tool")


def _validate_arguments_against_schema(arguments: dict[str, Any], schema: dict[str, Any]) -> None:
    """Minimal JSON-schema subset for registry-enforced tool arguments.

    This intentionally supports the subset the registry uses today: object type,
    required fields, primitive property types, and additionalProperties=false.
    It is not a full JSON Schema implementation.
    """
    if schema.get("type", "object") != "object":
        raise ValueError("registered tool input_schema must be an object schema")

    required = schema.get("required", [])
    if not isinstance(required, list):
        raise ValueError("registered tool input_schema required must be a list")
    for field_name in required:
        if not isinstance(field_name, str):
            raise ValueError("registered tool input_schema required entries must be strings")
        if field_name not in arguments:
            raise ValueError(f"tools/call arguments missing required field: {field_name}")

    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise ValueError("registered tool input_schema properties must be an object")

    if schema.get("additionalProperties") is False:
        allowed = set(properties.keys())
        extra = sorted(set(arguments.keys()) - allowed)
        if extra:
            raise ValueError(f"tools/call arguments include unsupported field: {extra[0]}")

    for field_name, value in arguments.items():
        property_schema = properties.get(field_name)
        if property_schema is None:
            continue
        if not isinstance(property_schema, dict):
            raise ValueError(f"registered tool input_schema property is invalid: {field_name}")
        expected_type = property_schema.get("type")
        if expected_type is None:
            continue
        if not _value_matches_json_type(value, expected_type):
            raise ValueError(f"tools/call argument has wrong type: {field_name} must be {expected_type}")


def _value_matches_json_type(value: Any, expected_type: str | list[str]) -> bool:
    expected_types = expected_type if isinstance(expected_type, list) else [expected_type]
    for item in expected_types:
        if item == "string" and isinstance(value, str):
            return True
        if item == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if item == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if item == "boolean" and isinstance(value, bool):
            return True
        if item == "object" and isinstance(value, dict):
            return True
        if item == "array" and isinstance(value, list):
            return True
        if item == "null" and value is None:
            return True
    return False


def _tool_call_from_params(project_id: str, params: dict[str, Any]) -> MCPGatewayToolCall:
    tool_name = params.get("name")
    if not isinstance(tool_name, str) or not tool_name.strip():
        raise ValueError("tools/call params must include non-empty name")

    arguments = params.get("arguments", {})
    if not isinstance(arguments, dict):
        raise ValueError("tools/call arguments must be an object")

    requested_server_name = params.get("server_name")
    if requested_server_name is not None and not isinstance(requested_server_name, str):
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

    registered_policy = _registered_tool_policy(project_id, tool_name, requested_server_name)
    if registered_policy is not None:
        server_name, registered_mutating, input_schema = registered_policy
        _validate_arguments_against_schema(arguments, input_schema)
        mutating = registered_mutating
    else:
        server_name = requested_server_name or "child-test-server"

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
    _: AuthUser = MaintainerDependency,
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
        tool_call = _tool_call_from_params(project_id, payload.params)
    except ValueError as exc:
        return _error_response(payload.id, code=-32602, message=str(exc))

    decision = gateway_for_project(project_id).submit_tool_call(tool_call)
    return _result_response(payload.id, _gateway_decision_to_mcp_result(decision))
