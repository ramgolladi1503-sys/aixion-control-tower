from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from .models import new_id, now_utc


class MCPChildServerTransport(StrEnum):
    IN_MEMORY_TEST = "IN_MEMORY_TEST"
    HTTP = "HTTP"
    STDIO = "STDIO"
    SSE = "SSE"


class MCPToolDefinition(BaseModel):
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    mutating: bool = False


class MCPChildServerCreate(BaseModel):
    project_id: str
    name: str
    transport: MCPChildServerTransport = MCPChildServerTransport.IN_MEMORY_TEST
    endpoint: str | None = None
    enabled: bool = True
    tools: list[MCPToolDefinition] = Field(default_factory=list)


class MCPChildServer(MCPChildServerCreate):
    id: str = Field(default_factory=lambda: new_id("mcp_child"))
    created_at: object = Field(default_factory=now_utc)
    updated_at: object = Field(default_factory=now_utc)
