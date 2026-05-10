from __future__ import annotations

import json
from urllib.error import URLError
from urllib.request import Request, urlopen

from .child_mcp_test_server import ChildMCPTestServer, ChildMCPToolCall
from .models import MCPChildServer
from .store import store


class MCPChildServerRouter:
    """Routes gateway-approved calls to a registered child server transport.

    This keeps the existing in-memory test child server behavior for demos/tests while
    introducing the production shape: registered server -> transport -> endpoint.
    """

    def __init__(self) -> None:
        self.default_test_server = ChildMCPTestServer()
        self.test_servers_by_name: dict[str, ChildMCPTestServer] = {}

    def call_tool(self, call: ChildMCPToolCall) -> dict:
        server = self._find_registered_server(call.server_name)
        if server is None:
            return self.default_test_server.call_tool(call)
        if server.transport == "test":
            return self._call_test_server(server, call)
        if server.transport == "http":
            return self._call_http_server(server, call)
        if server.transport == "http-jsonrpc":
            return self._call_http_jsonrpc_server(server, call)
        return {
            "ok": False,
            "server_name": server.name,
            "tool_name": call.tool_name,
            "error": f"Unsupported MCP child server transport: {server.transport}",
        }

    def reset_for_test(self) -> None:
        self.default_test_server.received_tool_calls.clear()
        self.test_servers_by_name.clear()

    def received_count_for_test(self) -> int:
        return len(self.default_test_server.received_tool_calls) + sum(
            len(server.received_tool_calls) for server in self.test_servers_by_name.values()
        )

    def received_count_for_server_for_test(self, server_name: str) -> int:
        if server_name == "child-test-server":
            return len(self.default_test_server.received_tool_calls)
        server = self.test_servers_by_name.get(server_name)
        return len(server.received_tool_calls) if server else 0

    @staticmethod
    def _find_registered_server(server_name: str) -> MCPChildServer | None:
        return next(
            (
                server
                for server in store.mcp_child_servers.values()
                if server.name == server_name and server.enabled
            ),
            None,
        )

    def _call_test_server(self, server: MCPChildServer, call: ChildMCPToolCall) -> dict:
        test_server = self.test_servers_by_name.setdefault(server.name, ChildMCPTestServer())
        return test_server.call_tool(call)

    @staticmethod
    def _call_http_server(server: MCPChildServer, call: ChildMCPToolCall) -> dict:
        if not server.endpoint:
            return {
                "ok": False,
                "server_name": server.name,
                "tool_name": call.tool_name,
                "error": "HTTP MCP child server endpoint is not configured.",
            }
        payload = json.dumps(call.model_dump(mode="json")).encode("utf-8")
        return MCPChildServerRouter._post_json(server, call, payload)

    @staticmethod
    def _call_http_jsonrpc_server(server: MCPChildServer, call: ChildMCPToolCall) -> dict:
        """Forward an approved call to an HTTP child MCP server using JSON-RPC tools/call.

        The legacy `http` transport posts Aixion's internal `ChildMCPToolCall` shape.
        This transport sends the MCP-facing shape a real child server expects:
        `{jsonrpc, id, method: tools/call, params: {name, arguments}}`.
        """
        if not server.endpoint:
            return {
                "ok": False,
                "server_name": server.name,
                "tool_name": call.tool_name,
                "error": "HTTP JSON-RPC MCP child server endpoint is not configured.",
            }
        request_id = call.session_id or f"aixion-{server.name}-{call.tool_name}"
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {
                    "name": call.tool_name,
                    "arguments": call.arguments,
                },
            }
        ).encode("utf-8")
        response = MCPChildServerRouter._post_json(server, call, payload)
        if "error" in response and "result" not in response:
            return response
        return {
            "ok": response.get("error") is None,
            "server_name": server.name,
            "tool_name": call.tool_name,
            "jsonrpc_response": response,
        }

    @staticmethod
    def _post_json(server: MCPChildServer, call: ChildMCPToolCall, payload: bytes) -> dict:
        request = Request(
            server.endpoint,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=10) as response:
                response_body = response.read().decode("utf-8")
        except URLError as exc:
            return {
                "ok": False,
                "server_name": server.name,
                "tool_name": call.tool_name,
                "error": str(exc),
            }
        if not response_body:
            return {"ok": True, "server_name": server.name, "tool_name": call.tool_name}
        try:
            parsed = json.loads(response_body)
        except json.JSONDecodeError:
            return {
                "ok": True,
                "server_name": server.name,
                "tool_name": call.tool_name,
                "raw_response": response_body,
            }
        return parsed if isinstance(parsed, dict) else {"ok": True, "response": parsed}
