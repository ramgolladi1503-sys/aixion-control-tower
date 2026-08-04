import pytest

from app.native_session_gateway import (
    NativeApprovalConflict,
    NativeApprovalDecisionLedger,
    NativeApprovalResolution,
    NativeContinuationReceipt,
    NativeSessionIdentity,
    build_native_approval_request,
    validate_native_resolution,
    verify_native_continuation,
)


def _identity() -> NativeSessionIdentity:
    return NativeSessionIdentity(
        provider="codex",
        host_id="host_mac_1",
        connector_id="connector_partner_1",
        native_session_id="session_native_1",
        native_thread_id="thread_native_1",
        native_turn_id="turn_native_1",
        provider_process_id=12345,
    )


def _request():
    return build_native_approval_request(
        provider_request_id="provider_request_1",
        provider_item_id="item_1",
        identity=_identity(),
        action_kind="RUN_COMMAND",
        command="python -m pytest backend/tests/test_safe.py",
        cwd="/Users/test/repo",
        reason="Run the approved regression test.",
        sandbox_scope="workspace-write",
        network_scope="restricted",
        available_decisions=["accept", "decline"],
        provider_payload={"provider_method": "requestApproval"},
    )


def _resolution(
    *,
    decision: str = "accept",
    payload_hash: str | None = None,
    device_id: str = "android_1",
) -> NativeApprovalResolution:
    request = _request()
    return NativeApprovalResolution(
        provider_request_id=request.provider_request_id,
        payload_hash=payload_hash or request.payload_hash,
        decision=decision,
        source_device_id=device_id,
        actor_user_id="user_1",
    )


def test_request_hash_covers_exact_native_identity_and_provider_semantics() -> None:
    request = _request()
    different_turn = build_native_approval_request(
        provider_request_id=request.provider_request_id,
        provider_item_id=request.provider_item_id,
        identity=request.identity.model_copy(
            update={"native_turn_id": "turn_native_2"}
        ),
        action_kind=request.action_kind,
        command=request.command,
        cwd=request.cwd,
        reason=request.reason,
        sandbox_scope=request.sandbox_scope,
        network_scope=request.network_scope,
        available_decisions=request.available_decisions,
        provider_payload=request.provider_payload,
    )

    assert request.identity.provider == "CODEX"
    assert request.payload_hash != different_turn.payload_hash


def test_exact_supported_resolution_is_valid() -> None:
    request = _request()
    resolution = _resolution()

    validate_native_resolution(request, resolution)


def test_payload_substitution_fails_closed() -> None:
    request = _request()
    resolution = _resolution(payload_hash="0" * 64)

    with pytest.raises(NativeApprovalConflict, match="payload hash mismatch"):
        validate_native_resolution(request, resolution)


def test_unsupported_provider_decision_fails_closed() -> None:
    request = _request()
    resolution = _resolution(decision="acceptForSession")

    with pytest.raises(NativeApprovalConflict, match="not available"):
        validate_native_resolution(request, resolution)


def test_first_valid_decision_wins_atomically() -> None:
    request = _request()
    ledger = NativeApprovalDecisionLedger()
    accepted = _resolution(decision="accept", device_id="android_1")
    declined = _resolution(decision="decline", device_id="mac_1")

    assert ledger.resolve(request, accepted) == accepted
    with pytest.raises(NativeApprovalConflict, match="already resolved"):
        ledger.resolve(request, declined)
    assert ledger.get(request.provider_request_id) == accepted


def test_identical_resolution_replay_is_idempotent() -> None:
    request = _request()
    ledger = NativeApprovalDecisionLedger()
    resolution = _resolution()

    first = ledger.resolve(request, resolution)
    replay = ledger.resolve(request, resolution)

    assert replay == first


def test_provider_receipt_proves_same_native_context_continues() -> None:
    request = _request()
    resolution = _resolution()
    receipt = NativeContinuationReceipt(
        provider_request_id=request.provider_request_id,
        payload_hash=request.payload_hash,
        decision=resolution.decision,
        identity=request.identity,
        provider_acknowledged=True,
        same_execution_context=True,
        provider_receipt_id="receipt_1",
    )

    verify_native_continuation(request, resolution, receipt)


def test_changed_thread_or_turn_cannot_be_certified_as_continuation() -> None:
    request = _request()
    resolution = _resolution()
    receipt = NativeContinuationReceipt(
        provider_request_id=request.provider_request_id,
        payload_hash=request.payload_hash,
        decision=resolution.decision,
        identity=request.identity.model_copy(
            update={"native_thread_id": "thread_duplicate"}
        ),
        provider_acknowledged=True,
        same_execution_context=True,
    )

    with pytest.raises(NativeApprovalConflict, match="context changed"):
        verify_native_continuation(request, resolution, receipt)


def test_provider_must_explicitly_acknowledge_continuation() -> None:
    request = _request()
    resolution = _resolution()
    receipt = NativeContinuationReceipt(
        provider_request_id=request.provider_request_id,
        payload_hash=request.payload_hash,
        decision=resolution.decision,
        identity=request.identity,
        provider_acknowledged=False,
        same_execution_context=False,
    )

    with pytest.raises(NativeApprovalConflict, match="did not acknowledge"):
        verify_native_continuation(request, resolution, receipt)
