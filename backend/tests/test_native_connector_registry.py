import pytest

from app.native_connector_contract import (
    REQUIRED_NATIVE_CONTROL_CAPABILITIES,
    NativeConnectorManifest,
    NativeConnectorMode,
    NativeConnectorSupport,
)
from app.native_connector_registry import (
    NativeConnectorNotFound,
    NativeConnectorRegistry,
    NativeConnectorRegistryConflict,
    build_default_native_connector_registry,
)


def test_default_registry_separates_native_codex_and_managed_fallback() -> None:
    registry = build_default_native_connector_registry()

    connectors = registry.list(provider="codex")

    assert [item.adapter_id for item in connectors] == [
        "codex-aixion-managed-app-server",
        "codex-native-desktop",
    ]
    assert (
        registry.eligibility("CODEX", "codex-native-desktop")
        .native_control_eligible
        is False
    )
    managed = registry.eligibility(
        "CODEX",
        "codex-aixion-managed-app-server",
    )
    assert managed.native_control_eligible is False
    assert managed.customer_label == "Aixion-managed session"


def test_registry_does_not_infer_native_support_from_adapter_name() -> None:
    registry = NativeConnectorRegistry()
    registry.register(
        NativeConnectorManifest(
            provider="FAKE",
            adapter_id="native-approval-super-connector",
            display_name="Misleading adapter name",
            mode=NativeConnectorMode.NATIVE_EXISTING_SESSION,
            support=NativeConnectorSupport.UNVERIFIED,
            capabilities=[],
            public_contract=False,
        )
    )

    eligibility = registry.eligibility(
        "fake",
        "native-approval-super-connector",
    )

    assert eligibility.native_control_eligible is False
    assert "support level is UNVERIFIED" in eligibility.blockers


def test_duplicate_registration_fails_without_explicit_replace() -> None:
    registry = build_default_native_connector_registry()
    manifest = registry.get("CODEX", "codex-native-desktop")

    with pytest.raises(NativeConnectorRegistryConflict):
        registry.register(manifest)


def test_explicit_replacement_recomputes_eligibility() -> None:
    registry = build_default_native_connector_registry()
    supported = NativeConnectorManifest(
        provider="CODEX",
        adapter_id="codex-native-desktop",
        display_name="Codex approved partner connector",
        mode=NativeConnectorMode.NATIVE_EXISTING_SESSION,
        support=NativeConnectorSupport.PUBLIC_SUPPORTED,
        capabilities=list(REQUIRED_NATIVE_CONTROL_CAPABILITIES),
        public_contract=True,
        evidence_references=["https://provider.example/supported-contract"],
    )

    registry.register(supported, replace=True)

    assert (
        registry.eligibility("codex", "codex-native-desktop")
        .native_control_eligible
        is True
    )


def test_unknown_connector_fails_closed() -> None:
    registry = build_default_native_connector_registry()

    with pytest.raises(NativeConnectorNotFound):
        registry.eligibility("CODEX", "unknown")


def test_inventory_includes_manifest_and_computed_eligibility() -> None:
    inventory = build_default_native_connector_registry().inventory()

    assert len(inventory) == 2
    native = next(
        item
        for item in inventory
        if item["manifest"]["adapter_id"] == "codex-native-desktop"
    )
    assert native["manifest"]["mode"] == "NATIVE_EXISTING_SESSION"
    assert native["eligibility"]["native_control_eligible"] is False


def test_provider_filter_is_case_insensitive() -> None:
    registry = build_default_native_connector_registry()

    assert registry.list(provider="codex") == registry.list(provider="CODEX")
