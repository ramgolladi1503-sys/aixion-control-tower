from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from .auth import require_api_key
from .models import AuditEvent, MCPChildServer, MCPChildServerCreate, now_utc
from .store import store

router = APIRouter(prefix="/mcp-registry", tags=["mcp-registry"])
AuthDependency = Depends(require_api_key)


def _audit(event_type: str, entity_id: str, details: dict, actor: str = "mcp-registry") -> None:
    store.audit_events.append(AuditEvent(event_type=event_type, entity_id=entity_id, details=details, actor=actor))


@router.post("/child-servers", response_model=MCPChildServer)
def register_child_server(payload: MCPChildServerCreate, _: None = AuthDependency) -> MCPChildServer:
    if payload.project_id not in store.projects:
        raise HTTPException(status_code=404, detail="Project not found")
    duplicate = next(
        (
            server
            for server in store.mcp_child_servers.values()
            if server.project_id == payload.project_id and server.name == payload.name
        ),
        None,
    )
    if duplicate is not None:
        raise HTTPException(status_code=409, detail="MCP child server name already exists for project")

    server = MCPChildServer(**payload.model_dump())
    store.mcp_child_servers[server.id] = server
    _audit(
        "mcp.child_server.registered",
        server.id,
        {"project_id": server.project_id, "name": server.name, "tools": [tool.name for tool in server.tools]},
    )
    store.persist()
    return server


@router.get("/child-servers", response_model=list[MCPChildServer])
def list_child_servers(
    project_id: str | None = Query(default=None),
    enabled: bool | None = Query(default=None),
    _: None = AuthDependency,
) -> list[MCPChildServer]:
    servers = list(store.mcp_child_servers.values())
    if project_id is not None:
        servers = [server for server in servers if server.project_id == project_id]
    if enabled is not None:
        servers = [server for server in servers if server.enabled == enabled]
    return sorted(servers, key=lambda server: server.created_at, reverse=True)


@router.get("/child-servers/{server_id}", response_model=MCPChildServer)
def get_child_server(server_id: str, _: None = AuthDependency) -> MCPChildServer:
    server = store.mcp_child_servers.get(server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="MCP child server not found")
    return server


@router.post("/child-servers/{server_id}/enable", response_model=MCPChildServer)
def enable_child_server(server_id: str, _: None = AuthDependency) -> MCPChildServer:
    server = store.mcp_child_servers.get(server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="MCP child server not found")
    server.enabled = True
    server.updated_at = now_utc()
    _audit("mcp.child_server.enabled", server.id, {"project_id": server.project_id, "name": server.name})
    store.persist()
    return server


@router.post("/child-servers/{server_id}/disable", response_model=MCPChildServer)
def disable_child_server(server_id: str, _: None = AuthDependency) -> MCPChildServer:
    server = store.mcp_child_servers.get(server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="MCP child server not found")
    server.enabled = False
    server.updated_at = now_utc()
    _audit("mcp.child_server.disabled", server.id, {"project_id": server.project_id, "name": server.name})
    store.persist()
    return server
