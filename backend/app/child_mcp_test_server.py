from __future__ import annotations

from pydantic import BaseModel, Field


class ChildMCPToolCall(BaseModel):
    """Small JSON-RPC-like MCP tool-call envelope used by the wait-mode proof.

    This is intentionally test-focused. The product proof is not that this child
    server is powerful. The proof is that the gateway can prevent this server from
    seeing mutating calls until approval exists.
    """

    server_name: str = "child-test-server"
    tool_name: str
    arguments: dict = Field(default_factory=dict)
    session_id: str | None = None
    requested_by: str = "mcp-client"


class ChildMCPTestServer:
    """In-memory child MCP server used to prove gateway enforcement.

    The received_tool_calls list is the enforcement witness. If a mutating call is
    waiting, denied, or expired, this list must stay empty.
    """

    def __init__(self) -> None:
        self.received_tool_calls: list[ChildMCPToolCall] = []

    def call_tool(self, call: ChildMCPToolCall) -> dict:
        self.received_tool_calls.append(call)
        return {
            "ok": True,
            "server_name": call.server_name,
            "tool_name": call.tool_name,
            "received_count": len(self.received_tool_calls),
        }
