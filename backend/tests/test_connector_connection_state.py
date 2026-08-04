from app.connector_connection_state import (
    ConnectorConnectionEvidence,
    ConnectorConnectionStage,
    derive_connector_connection_status,
)


def test_backend_health_alone_is_not_connected_or_operational() -> None:
    status = derive_connector_connection_status(
        ConnectorConnectionEvidence(backend_reachable=True)
    )

    assert status.stage == ConnectorConnectionStage.BACKEND_REACHABLE
    assert status.customer_label == "Backend reachable; host not registered"
    assert status.operational is False
    assert status.approval_delivery_ready is False
    assert status.end_to_end_certified is False


def test_native_session_without_approval_subscription_is_not_ready() -> None:
    status = derive_connector_connection_status(
        ConnectorConnectionEvidence(
            backend_reachable=True,
            host_registered=True,
            provider_authorized=True,
            native_session_discovered=True,
        )
    )

    assert status.stage == ConnectorConnectionStage.NATIVE_SESSION_DISCOVERED
    assert "approval channel unverified" in status.customer_label
    assert status.operational is False


def test_phone_subscription_requires_prior_native_channel_evidence() -> None:
    status = derive_connector_connection_status(
        ConnectorConnectionEvidence(
            backend_reachable=True,
            host_registered=True,
            provider_authorized=True,
            native_session_discovered=True,
            mobile_device_subscribed=True,
        )
    )

    assert (
        status.stage
        == ConnectorConnectionStage.NATIVE_SESSION_DISCOVERED
    )
    assert status.approval_delivery_ready is False


def test_approval_delivery_ready_is_not_certified() -> None:
    status = derive_connector_connection_status(
        ConnectorConnectionEvidence(
            backend_reachable=True,
            host_registered=True,
            provider_authorized=True,
            native_session_discovered=True,
            native_approval_channel_verified=True,
            mobile_device_subscribed=True,
        )
    )

    assert status.stage == ConnectorConnectionStage.MOBILE_DEVICE_SUBSCRIBED
    assert status.operational is True
    assert status.approval_delivery_ready is True
    assert status.end_to_end_certified is False
    assert len(status.blockers) == 3


def test_certification_requires_approve_reject_and_same_context() -> None:
    incomplete = derive_connector_connection_status(
        ConnectorConnectionEvidence(
            backend_reachable=True,
            host_registered=True,
            provider_authorized=True,
            native_session_discovered=True,
            native_approval_channel_verified=True,
            mobile_device_subscribed=True,
            physical_approve_certified=True,
            physical_reject_certified=True,
        )
    )

    assert incomplete.end_to_end_certified is False
    assert (
        "Same native execution context continuation is not certified."
        in incomplete.blockers
    )


def test_complete_evidence_reaches_certified_state() -> None:
    status = derive_connector_connection_status(
        ConnectorConnectionEvidence(
            backend_reachable=True,
            host_registered=True,
            provider_authorized=True,
            native_session_discovered=True,
            native_approval_channel_verified=True,
            mobile_device_subscribed=True,
            physical_approve_certified=True,
            physical_reject_certified=True,
            same_execution_context_certified=True,
        )
    )

    assert (
        status.stage
        == ConnectorConnectionStage.END_TO_END_APPROVAL_CERTIFIED
    )
    assert status.customer_label == "Native approval control certified"
    assert status.operational is True
    assert status.approval_delivery_ready is True
    assert status.end_to_end_certified is True
    assert status.blockers == []


def test_first_missing_gate_stops_progression() -> None:
    status = derive_connector_connection_status(
        ConnectorConnectionEvidence(
            backend_reachable=True,
            host_registered=False,
            provider_authorized=True,
            native_session_discovered=True,
            native_approval_channel_verified=True,
            mobile_device_subscribed=True,
        )
    )

    assert status.stage == ConnectorConnectionStage.BACKEND_REACHABLE
    assert status.blockers == ["Desktop host is not registered."]
