from app.native_connector_contract import (
    REQUIRED_NATIVE_CONTROL_CAPABILITIES,
    NativeConnectorCapability,
    NativeConnectorManifest,
    NativeConnectorMode,
    NativeConnectorSupport,
    codex_public_connector_inventory,
    evaluate_native_control,
)


def _supported_native_manifest() -> NativeConnectorManifest:
    return NativeConnectorManifest(
        provider="TEST_PROVIDER",
        adapter_id="native-test-adapter",
        display_name="Supported native test adapter",
        mode=NativeConnectorMode.NATIVE_EXISTING_SESSION,
        support=NativeConnectorSupport.PUBLIC_SUPPORTED,
        capabilities=list(REQUIRED_NATIVE_CONTROL_CAPABILITIES),
        public_contract=True,
        evidence_references=["https://provider.example/native-approval-api"],
    )


def test_supported_native_connector_is_eligible() -> None:
    result = evaluate_native_control(_supported_native_manifest())

    assert result.native_control_eligible is True
    assert result.customer_label == "Native session"
    assert result.missing_capabilities == []
    assert result.blockers == []


def test_missing_resolution_capability_fails_closed() -> None:
    manifest = _supported_native_manifest()
    manifest.capabilities.remove(
        NativeConnectorCapability.RESOLVE_APPROVAL_REQUESTS
    )

    result = evaluate_native_control(manifest)

    assert result.native_control_eligible is False
    assert NativeConnectorCapability.RESOLVE_APPROVAL_REQUESTS in (
        result.missing_capabilities
    )
    assert "required native-session capabilities are missing" in result.blockers


def test_partner_required_connector_cannot_be_marked_native_ready() -> None:
    manifest = _supported_native_manifest().model_copy(
        update={"support": NativeConnectorSupport.PARTNER_REQUIRED}
    )

    result = evaluate_native_control(manifest)

    assert result.native_control_eligible is False
    assert "support level is PARTNER_REQUIRED" in result.blockers


def test_undocumented_connector_cannot_be_marked_native_ready() -> None:
    manifest = _supported_native_manifest().model_copy(
        update={"public_contract": False}
    )

    result = evaluate_native_control(manifest)

    assert result.native_control_eligible is False
    assert (
        "no documented public third-party connector contract is recorded"
        in result.blockers
    )


def test_managed_runtime_is_never_presented_as_native_session() -> None:
    manifest = NativeConnectorManifest(
        provider="CODEX",
        adapter_id="managed-codex",
        display_name="Managed Codex",
        mode=NativeConnectorMode.AIXION_MANAGED_SESSION,
        support=NativeConnectorSupport.MANAGED_ONLY,
        capabilities=[
            NativeConnectorCapability.START_MANAGED_RUNTIME,
            NativeConnectorCapability.OBSERVE_APPROVAL_REQUESTS,
            NativeConnectorCapability.RESOLVE_APPROVAL_REQUESTS,
        ],
        public_contract=True,
        evidence_references=["docs/research/managed-codex.md"],
    )

    result = evaluate_native_control(manifest)

    assert result.native_control_eligible is False
    assert result.customer_label == "Aixion-managed session"
    assert (
        "adapter owns a managed runtime instead of an existing native session"
        in result.blockers
    )


def test_codex_inventory_reports_partner_boundary_and_managed_fallback() -> None:
    native, managed = codex_public_connector_inventory()

    native_result = evaluate_native_control(native)
    managed_result = evaluate_native_control(managed)

    assert native.adapter_id == "codex-native-desktop"
    assert native.support == NativeConnectorSupport.PARTNER_REQUIRED
    assert native_result.native_control_eligible is False
    assert native_result.customer_label == "Native session"

    assert managed.adapter_id == "codex-aixion-managed-app-server"
    assert managed.mode == NativeConnectorMode.AIXION_MANAGED_SESSION
    assert managed_result.native_control_eligible is False
    assert managed_result.customer_label == "Aixion-managed session"


def test_capabilities_are_deduplicated() -> None:
    manifest = NativeConnectorManifest(
        provider="TEST",
        adapter_id="dedupe",
        display_name="Dedupe",
        mode=NativeConnectorMode.AIXION_MANAGED_SESSION,
        support=NativeConnectorSupport.MANAGED_ONLY,
        capabilities=[
            NativeConnectorCapability.START_MANAGED_RUNTIME,
            NativeConnectorCapability.START_MANAGED_RUNTIME,
        ],
        public_contract=True,
        evidence_references=["docs/test.md"],
    )

    assert manifest.capabilities == [
        NativeConnectorCapability.START_MANAGED_RUNTIME
    ]
