from __future__ import annotations

import json
import os
from collections.abc import Iterable

from ..contracts import RelayProvider
from .antigravity import build_antigravity_adapter
from .base import AgentAdapter
from .claude import ClaudeAgentSdkAdapter
from .codex import CodexAppServerAdapter
from .generic_process import GenericProcessAdapter
from .openclaw import build_openclaw_adapter


class AdapterRegistryError(RuntimeError):
    pass


class AdapterRegistry:
    def __init__(self, adapters: Iterable[AgentAdapter] = ()) -> None:
        self._adapters: dict[str, AgentAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: AgentAdapter) -> None:
        adapter_id = adapter.manifest.adapter_id
        if adapter_id in self._adapters:
            raise AdapterRegistryError(f"Duplicate adapter id: {adapter_id}")
        self._adapters[adapter_id] = adapter

    def get(self, adapter_id: str) -> AgentAdapter:
        adapter = self._adapters.get(adapter_id)
        if adapter is None:
            raise AdapterRegistryError(f"Unknown relay adapter: {adapter_id}")
        if not adapter.manifest.available:
            raise AdapterRegistryError(
                f"Relay adapter {adapter_id} is installed in configuration but unavailable."
            )
        return adapter

    def manifests(self):
        return [
            adapter.manifest
            for adapter in sorted(
                self._adapters.values(),
                key=lambda value: value.manifest.adapter_id,
            )
        ]

    async def close(self) -> None:
        for adapter in self._adapters.values():
            await adapter.close()


def _configured_generic_adapters() -> list[AgentAdapter]:
    raw = os.getenv("AIXION_GENERIC_ADAPTERS_JSON", "").strip()
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as error:
        raise AdapterRegistryError(
            "AIXION_GENERIC_ADAPTERS_JSON must be a JSON array."
        ) from error
    if not isinstance(entries, list):
        raise AdapterRegistryError("AIXION_GENERIC_ADAPTERS_JSON must be a JSON array.")
    adapters: list[AgentAdapter] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise AdapterRegistryError("Every generic adapter entry must be an object.")
        try:
            provider = RelayProvider(str(entry.get("provider", "CUSTOM")))
            argv = entry["argv"]
            if not isinstance(argv, list) or not argv:
                raise ValueError("argv must be a non-empty array")
            adapter = GenericProcessAdapter(
                adapter_id=str(entry["adapter_id"]),
                provider=provider,
                display_name=str(entry.get("display_name") or entry["adapter_id"]),
                argv=[str(item) for item in argv],
                environment={
                    str(key): str(value)
                    for key, value in (entry.get("environment") or {}).items()
                },
            )
        except (KeyError, TypeError, ValueError) as error:
            raise AdapterRegistryError(
                f"Invalid generic adapter entry: {entry!r}: {error}"
            ) from error
        adapters.append(adapter)
    return adapters


def discover_default_adapters() -> AdapterRegistry:
    adapters: list[AgentAdapter] = [
        CodexAppServerAdapter(),
        ClaudeAgentSdkAdapter(),
    ]
    for optional in (
        build_antigravity_adapter(),
        build_openclaw_adapter(),
    ):
        if optional is not None:
            adapters.append(optional)
    adapters.extend(_configured_generic_adapters())
    return AdapterRegistry(adapters)
