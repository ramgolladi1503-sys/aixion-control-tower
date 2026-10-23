from __future__ import annotations

import time
from datetime import timedelta
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
import psycopg

from .application_repository_access import (
    approval_decision_repository_for_request,
    mcp_child_server_repository_for_request,
    project_repository_for_request,
    workspace_id_for_request,
)
from .auth import require_maintainer
from .mcp_gateway import MCPGatewayDecision, MCPGatewayToolCall
from .mcp_gateway_routes import (
    GatewaySubmitRequest,
    _postgres_mcp_submission,
    _resolve_postgres_gateway_request,
    gateway_for_project,
)
from .mcp_child_server_repository import (
    PostgresMCPChildServerRepository,
    SQLiteMCPChildServerRepository,
)
from .models import ApprovalStatus, AuthUser, MCPChildServer, now_utc
from .approval_expiry import expire_approval
from .mcp_pending_repository import MCPPendingRepositoryError
from .settings import get_settings
from .store import store
from .workspace_access import project_workspace_id, workspace_id_for_user

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
    return _registered_tools_from_servers(_enabled_child_servers(project_id), allow_demo=True)


def _registered_tools_from_servers(
    servers: list[MCPChildServer],
    *,
    allow_demo: bool,
) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for server in servers:
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
    return _demo_tools() if allow_demo else []


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
    registered_servers: list[MCPChildServer] | None = None,
) -> tuple[str, bool, dict[str, Any]] | None:
    servers = (
        _project_child_servers(project_id)
        if registered_servers is None
        else registered_servers
    )
    if not servers:
        return None

    matches = []
    for server in servers:
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


def _tool_call_from_params(
    project_id: str,
    params: dict[str, Any],
    *,
    registered_servers: list[MCPChildServer] | None = None,
    allow_demo: bool = True,
) -> MCPGatewayToolCall:
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

    registered_policy = _registered_tool_policy(
        project_id,
        tool_name,
        requested_server_name,
        registered_servers=registered_servers,
    )
    if registered_policy is not None:
        server_name, registered_mutating, input_schema = registered_policy
        _validate_arguments_against_schema(arguments, input_schema)
        mutating = registered_mutating
    else:
        if not allow_demo:
            raise ValueError("tools/call references an unknown or disabled registered tool")
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


def _postgres_mcp_tool_call(
    *,
    project_id: str,
    payload: MCPJsonRpcRequest,
    user: AuthUser,
    request: Request,
    registered_servers: list[MCPChildServer],
) -> MCPJsonRpcResponse:
    """Persist, wait for, and forward one PostgreSQL-backed mutating call.

    The JSON-RPC request ID is the transport idempotency key. Approval is read
    from the application repository on every poll, and forwarding is delegated
    to the durable claim/finalize implementation shared with MCP Gateway.
    """
    try:
        call = _tool_call_from_params(
            project_id,
            payload.params,
            registered_servers=registered_servers,
            allow_demo=False,
        )
    except ValueError as error:
        return _error_response(payload.id, code=-32602, message=str(error))

    if call.mutating is not True:
        raise HTTPException(
            status_code=503,
            detail="PostgreSQL MCP non-mutating forwarding is unavailable",
        )
    if payload.id is None:
        return _error_response(
            payload.id,
            code=-32600,
            message="Mutating MCP tools/call requires a JSON-RPC request ID for idempotency.",
        )
    idempotency_key = f"{project_id}:{call.session_id or ''}:{payload.id}"
    if len(idempotency_key) > 256:
        return _error_response(
            payload.id,
            code=-32600,
            message="Mutating MCP tools/call request ID is too long.",
        )

    submission = _postgres_mcp_submission(
        payload=GatewaySubmitRequest(project_id=project_id, request=call),
        user=user,
        request=request,
        idempotency_key=idempotency_key,
    )
    approval_id = submission.approval_request_id
    if not approval_id:
        raise HTTPException(status_code=503, detail="MCP approval identity was not persisted")

    try:
        approval_repository = approval_decision_repository_for_request(request)
        approval = approval_repository.get(approval_id)
    except (RuntimeError, psycopg.Error, MCPPendingRepositoryError) as error:
        raise HTTPException(status_code=503, detail="MCP approval state is unavailable") from error
    try:
        workspace_id = workspace_id_for_request(user, request)
    except (RuntimeError, psycopg.Error) as error:
        raise HTTPException(status_code=503, detail="MCP approval workspace is unavailable") from error
    if approval is None or workspace_id is None or approval.workspace_id != workspace_id:
        raise HTTPException(status_code=503, detail="MCP approval state is unavailable")

    # The submission helper resolves the target again before it writes the
    # durable request. Reject a concurrent registry change so the request can
    # never be approved against a different tool schema than the one validated
    # at the transport boundary.
    matching_servers = [server for server in registered_servers if server.name == call.server_name]
    try:
        approved_target = json.loads(approval.files[0].new_content or "")
    except (IndexError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=409, detail="Approved MCP target binding is invalid") from error
    if (
        len(matching_servers) != 1
        or approved_target.get("target_server_id") != matching_servers[0].id
        or approved_target.get("target_server_revision") != matching_servers[0].updated_at.isoformat()
    ):
        raise HTTPException(
            status_code=409,
            detail="MCP server configuration changed during submission; create a new request",
        )

    # Reuse the application's bounded approval TTL. This also bounds the
    # synchronous HTTP wait even when a caller reuses an older idempotency key.
    ttl_seconds = get_settings().agent_approval_ttl_seconds
    effective_expiry = approval.expires_at or (approval.created_at + timedelta(seconds=ttl_seconds))
    if effective_expiry.tzinfo is None or effective_expiry.utcoffset() is None:
        raise HTTPException(status_code=503, detail="MCP approval expiry is invalid")
    deadline = min(
        effective_expiry,
        now_utc() + timedelta(seconds=ttl_seconds),
    )
    monotonic_deadline = time.monotonic() + max(0.0, (deadline - now_utc()).total_seconds())

    while True:
        try:
            approval = approval_repository.get(approval_id)
        except (RuntimeError, psycopg.Error, MCPPendingRepositoryError) as error:
            raise HTTPException(status_code=503, detail="MCP approval state is unavailable") from error
        if approval is None or workspace_id is None or approval.workspace_id != workspace_id:
            raise HTTPException(status_code=503, detail="MCP approval state is unavailable")

        if approval.status == ApprovalStatus.APPROVED:
            decision = _resolve_postgres_gateway_request(
                approval_request_id=approval_id,
                user=user,
                request=request,
                pending_repository=request.app.state.postgres_repositories.mcp_pending_requests,
            )
            return _result_response(payload.id, _gateway_decision_to_mcp_result(decision))

        if approval.status not in {ApprovalStatus.REQUESTED, ApprovalStatus.PENDING_REVIEW}:
            decision = MCPGatewayDecision(
                forwarded=False,
                approval_required=True,
                approval_request_id=approval_id,
                status=approval.status,
                reason=f"Approval status is {approval.status}; MCP tool was not forwarded.",
            )
            result = _gateway_decision_to_mcp_result(decision)
            result["isError"] = True
            return _result_response(payload.id, result)

        now = now_utc()
        if effective_expiry <= now:
            try:
                # The shared expiry path handles both explicit deadlines and
                # legacy rows whose deadline is derived from the configured TTL.
                expire_approval(
                    approval,
                    actor="system:mcp-transport-timeout",
                    reason="deadline_elapsed",
                    now=now,
                    repository=approval_repository,
                )
                approval = approval_repository.get(approval_id)
            except Exception as error:
                raise HTTPException(status_code=503, detail="MCP approval expiry could not be persisted") from error
            if approval is None or approval.status not in {
                ApprovalStatus.EXPIRED,
                ApprovalStatus.DENIED,
                ApprovalStatus.REVISION_REQUESTED,
            }:
                raise HTTPException(status_code=503, detail="MCP approval expiry is unresolved")
            decision = MCPGatewayDecision(
                forwarded=False,
                approval_required=True,
                approval_request_id=approval_id,
                status=approval.status,
                reason="Approval expired before the MCP tool call was approved; the tool was not forwarded.",
            )
            result = _gateway_decision_to_mcp_result(decision)
            result["isError"] = True
            return _result_response(payload.id, result)
        if time.monotonic() >= monotonic_deadline:
            try:
                expire_approval(
                    approval,
                    actor="system:mcp-transport-timeout",
                    reason="transport_wait_timeout",
                    now=now,
                    force=True,
                    repository=approval_repository,
                )
                approval = approval_repository.get(approval_id)
            except Exception as error:
                raise HTTPException(status_code=503, detail="MCP approval timeout could not be persisted") from error
            if approval is None or approval.status not in {
                ApprovalStatus.EXPIRED,
                ApprovalStatus.DENIED,
                ApprovalStatus.REVISION_REQUESTED,
            }:
                raise HTTPException(status_code=503, detail="MCP approval timeout is unresolved")
            decision = MCPGatewayDecision(
                forwarded=False,
                approval_required=True,
                approval_request_id=approval_id,
                status=ApprovalStatus.EXPIRED,
                reason="MCP approval wait timed out; the tool was not forwarded.",
            )
            result = _gateway_decision_to_mcp_result(decision)
            result["isError"] = True
            return _result_response(payload.id, result)
        time.sleep(min(0.5, max(0.0, monotonic_deadline - time.monotonic())))


@router.post("/{project_id}/jsonrpc", response_model=MCPJsonRpcResponse)
def handle_mcp_jsonrpc(
    project_id: str,
    payload: MCPJsonRpcRequest,
    user: AuthUser = MaintainerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> MCPJsonRpcResponse:
    postgres_child_servers: list[MCPChildServer] | None = None
    if http_request is not None:
        try:
            repository = mcp_child_server_repository_for_request(http_request)
        except (RuntimeError, psycopg.Error) as error:
            raise HTTPException(status_code=503, detail="MCP server registry is unavailable") from error
        if isinstance(repository, PostgresMCPChildServerRepository):
            try:
                project_repository = project_repository_for_request(http_request)
                workspace_id = workspace_id_for_request(user, http_request)
                project = project_repository.get(project_id)
            except (RuntimeError, psycopg.Error) as error:
                raise HTTPException(status_code=503, detail="MCP project data is unavailable") from error
            if (
                project is None
                or workspace_id is None
                or project.workspace_id != workspace_id
            ):
                raise HTTPException(status_code=404, detail="Project not found")
            if payload.method in {"tools/list", "tools/call"}:
                try:
                    postgres_child_servers = repository.list_for_workspace(
                        workspace_id,
                        project_id=project_id,
                        enabled=True,
                    )
                except (RuntimeError, psycopg.Error) as error:
                    raise HTTPException(status_code=503, detail="MCP server registry is unavailable") from error
        elif isinstance(repository, SQLiteMCPChildServerRepository):
            if project_id not in store.projects:
                raise HTTPException(status_code=404, detail="Project not found")
            workspace_id = workspace_id_for_request(user, http_request)
            if workspace_id is not None and project_workspace_id(project_id) != workspace_id:
                raise HTTPException(status_code=404, detail="Project not found")
        else:
            raise HTTPException(
                status_code=503,
                detail=(
                    "MCP transport is unavailable for this application store until registry, "
                    "pending-request, approval, and forwarding state share a durable runtime."
                ),
            )
    else:
        if project_id not in store.projects:
            raise HTTPException(status_code=404, detail="Project not found")
        workspace_id = workspace_id_for_user(user)
        if workspace_id is not None and project_workspace_id(project_id) != workspace_id:
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
        tools = (
            _registered_tools_from_servers(postgres_child_servers, allow_demo=False)
            if postgres_child_servers is not None
            else _registered_tools(project_id)
        )
        return _result_response(payload.id, {"tools": tools})

    if postgres_child_servers is not None:
        return _postgres_mcp_tool_call(
            project_id=project_id,
            payload=payload,
            user=user,
            request=http_request,
            registered_servers=postgres_child_servers,
        )
    try:
        tool_call = _tool_call_from_params(project_id, payload.params)
    except ValueError as exc:
        return _error_response(payload.id, code=-32602, message=str(exc))

    decision = gateway_for_project(project_id).submit_tool_call(tool_call)
    return _result_response(payload.id, _gateway_decision_to_mcp_result(decision))
