from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from .relay_models import RelayProvider


class NativeRelaySessionAttachRequest(BaseModel):
    provider: RelayProvider = RelayProvider.ANTIGRAVITY
    adapter_id: str = Field(default="antigravity-native-hook", min_length=1, max_length=120)
    conversation_id: str = Field(min_length=1, max_length=500)
    workspace_path: str = Field(min_length=1, max_length=2000)
    repository: str | None = Field(default=None, max_length=500)
    project_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("workspace_path")
    @classmethod
    def validate_workspace_path(cls, value: str) -> str:
        cleaned = value.strip().rstrip("/") or "/"
        if not cleaned.startswith("/") and not (
            len(cleaned) >= 3 and cleaned[1:3] in {":\\", ":/"}
        ):
            raise ValueError("workspace_path must be absolute")
        return cleaned

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if "/" not in cleaned or cleaned.startswith("/") or cleaned.endswith("/"):
            raise ValueError("repository must use owner/name format")
        return cleaned
