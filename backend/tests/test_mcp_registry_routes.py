from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from fastapi.testclient import TestClient

from app.main import app
from app.store import store

client = TestClient(app)


def setup_function() -> None:
    store.reset()


def _create_project(name: str = "MCP Registry Project") -> str:
    response = client.post("/projects", json={"name": name, "description": "Registry test"})
    assert response.status_code == 200
    return response.json()["id"]


def _register_server(project_id: str, name: str = "filesystem") -> dict:
    response = client.post(
        "/mcp-registry/child-servers",
        json={
            "project_id": project_id,
            "name": name,
            "description": "Filesystem MCP child server",
            "transport": "test",
            "endpoint": "memory://filesystem",
            "enabled": True,
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


def test_register_list_get_disable_and_enable_child_server() -> None:
    project_id = _create_project()
    server = _register_server(project_id)

    assert server["project_id"] == project_id
    assert server["name"] == "filesystem"
    assert server["enabled"] is True
    assert [tool["name"] for tool in server["tools"]] == ["read_file", "write_file"]

    listed = client.get(f"/mcp-registry/child-servers?project_id={project_id}")
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert listed.json()[0]["id"] == server["id"]

    detail = client.get(f"/mcp-registry/child-servers/{server['id']}")
    assert detail.status_code == 200
    assert detail.json()["id"] == server["id"]

    disabled = client.post(f"/mcp-registry/child-servers/{server['id']}/disable")
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False

    enabled_filter = client.get("/mcp-registry/child-servers?enabled=true")
    assert enabled_filter.status_code == 200
    assert enabled_filter.json() == []

    reenabled = client.post(f"/mcp-registry/child-servers/{server['id']}/enable")
    assert reenabled.status_code == 200
    assert reenabled.json()["enabled"] is True

    audit_types = [event.event_type for event in store.audit_events]
    assert "mcp.child_server.registered" in audit_types
    assert "mcp.child_server.disabled" in audit_types
    assert "mcp.child_server.enabled" in audit_types


def test_register_child_server_blocks_unknown_project_and_duplicate_name() -> None:
    project_id = _create_project()
    _register_server(project_id)

    missing_project = client.post(
        "/mcp-registry/child-servers",
        json={"project_id": "project_missing", "name": "filesystem", "tools": []},
    )
    duplicate = client.post(
        "/mcp-registry/child-servers",
        json={"project_id": project_id, "name": "filesystem", "tools": []},
    )

    assert missing_project.status_code == 404
    assert duplicate.status_code == 409


def test_child_server_detail_returns_404_for_missing_id() -> None:
    response = client.get("/mcp-registry/child-servers/mcp_server_missing")

    assert response.status_code == 404
