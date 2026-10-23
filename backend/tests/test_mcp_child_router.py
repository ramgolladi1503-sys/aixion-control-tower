from __future__ import annotations

from types import SimpleNamespace

from app import mcp_child_router
from app.child_mcp_test_server import ChildMCPToolCall
from app.mcp_child_router import MCPChildServerRouter
from app.models import MCPChildServer
from app.store import store


def _server(
    *,
    name: str = "registered",
    workspace_id: str = "workspace-a",
    project_id: str = "project-a",
) -> MCPChildServer:
    return MCPChildServer(
        id=f"server-{name}",
        project_id=project_id,
        workspace_id=workspace_id,
        name=name,
        transport="test",
    )


class _Repository:
    def __init__(self, servers: list[MCPChildServer]) -> None:
        self.servers = servers
        self.scopes: list[tuple[str | None, str | None, bool | None]] = []

    def list_for_workspace(self, workspace_id, *, project_id=None, enabled=None):
        self.scopes.append((workspace_id, project_id, enabled))
        return self.servers


def setup_function() -> None:
    store.reset()


def test_postgres_mode_never_uses_process_local_child_server_registry(monkeypatch) -> None:
    store.mcp_child_servers["stale"] = _server(name="registered")
    monkeypatch.setattr(
        mcp_child_router,
        "get_settings",
        lambda: SimpleNamespace(postgres_auth_enabled=True),
    )
    router = MCPChildServerRouter()

    result = router.call_tool(ChildMCPToolCall(server_name="registered", tool_name="write"))

    assert result["ok"] is False
    assert router.received_count_for_test() == 0


def test_app_repository_lookup_is_workspace_and_project_scoped(monkeypatch) -> None:
    expected = _server()
    repository = _Repository([expected])
    monkeypatch.setattr(
        mcp_child_router,
        "mcp_child_server_repository_for_request",
        lambda request: repository,
    )
    request = SimpleNamespace()
    router = MCPChildServerRouter()

    result = router.call_tool(
        ChildMCPToolCall(server_name=expected.name, tool_name="write"),
        request=request,
        workspace_id="workspace-a",
        project_id="project-a",
    )

    assert result["ok"] is True
    assert repository.scopes == [("workspace-a", "project-a", True)]
    assert router.received_count_for_server_for_test(expected.name) == 1


def test_app_repository_lookup_rejects_mismatched_scope_and_does_not_dispatch(monkeypatch) -> None:
    repository = _Repository([_server(workspace_id="workspace-other")])
    monkeypatch.setattr(
        mcp_child_router,
        "mcp_child_server_repository_for_request",
        lambda request: repository,
    )
    router = MCPChildServerRouter()

    result = router.call_tool(
        ChildMCPToolCall(server_name="registered", tool_name="write"),
        request=SimpleNamespace(),
        workspace_id="workspace-a",
        project_id="project-a",
    )

    assert result["ok"] is False
    assert router.received_count_for_test() == 0


def test_sqlite_test_compatibility_still_uses_local_registry(monkeypatch) -> None:
    local = _server(name="local-server", workspace_id=None)
    store.mcp_child_servers[local.id] = local
    monkeypatch.setattr(
        mcp_child_router,
        "get_settings",
        lambda: SimpleNamespace(postgres_auth_enabled=False),
    )
    router = MCPChildServerRouter()

    result = router.call_tool(ChildMCPToolCall(server_name=local.name, tool_name="write"))

    assert result["ok"] is True
    assert router.received_count_for_server_for_test(local.name) == 1
