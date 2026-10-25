from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from .application_repository_access import (
    mcp_child_server_repository_for_request,
    project_repository_for_request,
    workspace_id_for_request,
)
from .auth import require_maintainer, require_reviewer
from .mcp_child_server_repository import (
    MCPChildServerConflict,
    MCPChildServerRepository,
    SQLiteMCPChildServerRepository,
)
from .models import AuditEvent, AuthUser, MCPChildServer, MCPChildServerCreate, now_utc
from .settings import get_settings
from .store import store

router = APIRouter(prefix="/mcp-registry", tags=["mcp-registry"])
ReviewerDependency = Depends(require_reviewer)
MaintainerDependency = Depends(require_maintainer)


def _repository_for_route(http_request: Request | None) -> MCPChildServerRepository:
    if http_request is None:
        _require_local_direct_access()
        # Preserve direct local/test calls while HTTP routes use explicit app state.
        return SQLiteMCPChildServerRepository(store._connect)
    return mcp_child_server_repository_for_request(http_request)


def _require_local_direct_access() -> None:
    settings = get_settings()
    if getattr(settings, "is_production", False) or getattr(
        settings, "postgres_auth_enabled", False
    ):
        raise HTTPException(
            status_code=503,
            detail="MCP registry requires an app-bound repository in production mode",
            headers={"Retry-After": "30"},
        )


def _workspace_for_route(user: AuthUser, http_request: Request | None) -> str | None:
    if http_request is None:
        _require_local_direct_access()
        from .workspace_access import workspace_id_for_user

        return workspace_id_for_user(user)
    return workspace_id_for_request(user, http_request)


def _server_for_user(
    server_id: str,
    user: AuthUser,
    http_request: Request | None,
) -> MCPChildServer:
    repository = _repository_for_route(http_request)
    server = repository.get(server_id)
    workspace_id = _workspace_for_route(user, http_request)
    project_repository = (
        project_repository_for_request(http_request)
        if http_request is not None
        else None
    )
    project = (
        project_repository.get(server.project_id)
        if server is not None and project_repository is not None
        else None
    )
    if (
        server is None
        or (http_request is not None and (project is None or project.workspace_id != server.workspace_id))
        or (workspace_id is not None and server.workspace_id != workspace_id)
    ):
        raise HTTPException(status_code=404, detail="MCP child server not found")
    return server


@router.post("/child-servers", response_model=MCPChildServer)
def register_child_server(
    payload: MCPChildServerCreate,
    user: AuthUser = MaintainerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> MCPChildServer:
    repository = _repository_for_route(http_request)
    projects = project_repository_for_request(http_request) if http_request is not None else None
    project = projects.get(payload.project_id) if projects is not None else store.projects.get(payload.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    workspace_id = _workspace_for_route(user, http_request)
    if workspace_id is not None and project.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Project not found")

    server = MCPChildServer(**payload.model_dump(), workspace_id=project.workspace_id)
    audit_event = AuditEvent(
        event_type="mcp.child_server.registered",
        entity_id=server.id,
        workspace_id=server.workspace_id,
        details={"project_id": server.project_id, "name": server.name, "tools": [tool.name for tool in server.tools]},
        actor=user.email,
    )
    try:
        repository.create(server=server, audit_event=audit_event)
    except MCPChildServerConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if isinstance(repository, SQLiteMCPChildServerRepository):
        store.mcp_child_servers[server.id] = server
        store.audit_events.append(audit_event)
    return server


@router.get("/child-servers", response_model=list[MCPChildServer])
def list_child_servers(
    project_id: str | None = None,
    enabled: bool | None = None,
    user: AuthUser = ReviewerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> list[MCPChildServer]:
    workspace_id = _workspace_for_route(user, http_request)
    if project_id is not None and http_request is not None:
        project = project_repository_for_request(http_request).get(project_id)
        if project is None or (workspace_id is not None and project.workspace_id != workspace_id):
            raise HTTPException(status_code=404, detail="Project not found")
    servers = _repository_for_route(http_request).list_for_workspace(
        workspace_id,
        project_id=project_id,
        enabled=enabled,
    )
    return sorted(servers, key=lambda server: server.created_at, reverse=True)


@router.get("/child-servers/{server_id}", response_model=MCPChildServer)
def get_child_server(
    server_id: str,
    user: AuthUser = ReviewerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> MCPChildServer:
    return _server_for_user(server_id, user, http_request)


def _set_server_enabled(
    server_id: str,
    *,
    enabled: bool,
    user: AuthUser,
    http_request: Request | None,
) -> MCPChildServer:
    repository = _repository_for_route(http_request)
    current = _server_for_user(server_id, user, http_request)
    updated = current.model_copy(update={"enabled": enabled, "updated_at": now_utc()})
    state = "enabled" if enabled else "disabled"
    audit_event = AuditEvent(
        event_type=f"mcp.child_server.{state}",
        entity_id=updated.id,
        workspace_id=updated.workspace_id,
        details={"project_id": updated.project_id, "name": updated.name},
        actor=user.email,
    )
    try:
        repository.update(
            server=updated,
            audit_event=audit_event,
            expected_updated_at=current.updated_at.isoformat(),
        )
    except MCPChildServerConflict as error:
        raise HTTPException(status_code=409, detail="MCP child server changed; reload and retry") from error
    if isinstance(repository, SQLiteMCPChildServerRepository):
        store.mcp_child_servers[updated.id] = updated
        store.audit_events.append(audit_event)
    return updated


@router.post("/child-servers/{server_id}/enable", response_model=MCPChildServer)
def enable_child_server(
    server_id: str,
    user: AuthUser = MaintainerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> MCPChildServer:
    return _set_server_enabled(
        server_id, enabled=True, user=user, http_request=http_request
    )


@router.post("/child-servers/{server_id}/disable", response_model=MCPChildServer)
def disable_child_server(
    server_id: str,
    user: AuthUser = MaintainerDependency,
    http_request: Request = None,  # type: ignore[assignment]
) -> MCPChildServer:
    return _set_server_enabled(
        server_id, enabled=False, user=user, http_request=http_request
    )
