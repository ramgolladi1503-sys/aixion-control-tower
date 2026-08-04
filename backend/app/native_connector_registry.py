from __future__ import annotations

from collections.abc import Iterable

from .native_connector_contract import (
    NativeConnectorEligibility,
    NativeConnectorManifest,
    codex_public_connector_inventory,
    evaluate_native_control,
)


class NativeConnectorRegistryConflict(RuntimeError):
    """Raised when a connector registration would be ambiguous or unsafe."""


class NativeConnectorNotFound(LookupError):
    """Raised when a requested provider connector is not registered."""


class NativeConnectorRegistry:
    """Evidence-backed registry for native and managed provider connectors.

    The registry never infers native capability from provider name, process
    presence, adapter naming, or backend connectivity. Eligibility is computed
    from the connector's explicit manifest every time it is read.
    """

    def __init__(
        self,
        manifests: Iterable[NativeConnectorManifest] | None = None,
    ) -> None:
        self._manifests: dict[tuple[str, str], NativeConnectorManifest] = {}
        for manifest in manifests or ():
            self.register(manifest)

    @staticmethod
    def _key(provider: str, adapter_id: str) -> tuple[str, str]:
        normalized_provider = provider.strip().upper()
        normalized_adapter = adapter_id.strip()
        if not normalized_provider or not normalized_adapter:
            raise ValueError("provider and adapter_id must not be blank")
        return normalized_provider, normalized_adapter

    def register(
        self,
        manifest: NativeConnectorManifest,
        *,
        replace: bool = False,
    ) -> NativeConnectorManifest:
        key = self._key(manifest.provider, manifest.adapter_id)
        if key in self._manifests and not replace:
            raise NativeConnectorRegistryConflict(
                f"Connector {key[0]}/{key[1]} is already registered."
            )
        normalized = manifest.model_copy(update={"provider": key[0]})
        self._manifests[key] = normalized
        return normalized

    def get(self, provider: str, adapter_id: str) -> NativeConnectorManifest:
        key = self._key(provider, adapter_id)
        manifest = self._manifests.get(key)
        if manifest is None:
            raise NativeConnectorNotFound(
                f"Connector {key[0]}/{key[1]} is not registered."
            )
        return manifest

    def eligibility(
        self,
        provider: str,
        adapter_id: str,
    ) -> NativeConnectorEligibility:
        return evaluate_native_control(self.get(provider, adapter_id))

    def list(
        self,
        *,
        provider: str | None = None,
    ) -> list[NativeConnectorManifest]:
        normalized_provider = provider.strip().upper() if provider else None
        manifests = [
            manifest
            for (manifest_provider, _), manifest in self._manifests.items()
            if normalized_provider is None
            or manifest_provider == normalized_provider
        ]
        return sorted(
            manifests,
            key=lambda item: (item.provider, item.display_name, item.adapter_id),
        )

    def inventory(self) -> list[dict[str, object]]:
        return [
            {
                "manifest": manifest.model_dump(mode="json"),
                "eligibility": evaluate_native_control(manifest).model_dump(
                    mode="json"
                ),
            }
            for manifest in self.list()
        ]


def build_default_native_connector_registry() -> NativeConnectorRegistry:
    """Build the conservative built-in connector inventory."""

    return NativeConnectorRegistry(codex_public_connector_inventory())
