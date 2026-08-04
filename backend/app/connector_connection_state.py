from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class ConnectorConnectionStage(StrEnum):
    DISCONNECTED = "DISCONNECTED"
    BACKEND_REACHABLE = "BACKEND_REACHABLE"
    HOST_REGISTERED = "HOST_REGISTERED"
    PROVIDER_AUTHORIZED = "PROVIDER_AUTHORIZED"
    NATIVE_SESSION_DISCOVERED = "NATIVE_SESSION_DISCOVERED"
    NATIVE_APPROVAL_CHANNEL_VERIFIED = "NATIVE_APPROVAL_CHANNEL_VERIFIED"
    MOBILE_DEVICE_SUBSCRIBED = "MOBILE_DEVICE_SUBSCRIBED"
    END_TO_END_APPROVAL_CERTIFIED = "END_TO_END_APPROVAL_CERTIFIED"


class ConnectorConnectionEvidence(BaseModel):
    """Evidence gates behind customer-visible connector status."""

    backend_reachable: bool = False
    host_registered: bool = False
    provider_authorized: bool = False
    native_session_discovered: bool = False
    native_approval_channel_verified: bool = False
    mobile_device_subscribed: bool = False
    physical_approve_certified: bool = False
    physical_reject_certified: bool = False
    same_execution_context_certified: bool = False


class ConnectorConnectionStatus(BaseModel):
    stage: ConnectorConnectionStage
    customer_label: str
    operational: bool
    approval_delivery_ready: bool
    end_to_end_certified: bool
    blockers: list[str]


def derive_connector_connection_status(
    evidence: ConnectorConnectionEvidence,
) -> ConnectorConnectionStatus:
    """Derive the highest sequentially proven state and fail closed on gaps."""

    checks: list[tuple[ConnectorConnectionStage, bool, str]] = [
        (
            ConnectorConnectionStage.BACKEND_REACHABLE,
            evidence.backend_reachable,
            "Backend is not reachable.",
        ),
        (
            ConnectorConnectionStage.HOST_REGISTERED,
            evidence.host_registered,
            "Desktop host is not registered.",
        ),
        (
            ConnectorConnectionStage.PROVIDER_AUTHORIZED,
            evidence.provider_authorized,
            "Provider authorization is not verified.",
        ),
        (
            ConnectorConnectionStage.NATIVE_SESSION_DISCOVERED,
            evidence.native_session_discovered,
            "No supported existing native session is discovered.",
        ),
        (
            ConnectorConnectionStage.NATIVE_APPROVAL_CHANNEL_VERIFIED,
            evidence.native_approval_channel_verified,
            "Native approval event subscription is not verified.",
        ),
        (
            ConnectorConnectionStage.MOBILE_DEVICE_SUBSCRIBED,
            evidence.mobile_device_subscribed,
            "The mobile device is not subscribed to the approval channel.",
        ),
    ]

    stage = ConnectorConnectionStage.DISCONNECTED
    blockers: list[str] = []
    for candidate, passed, blocker in checks:
        if not passed:
            blockers.append(blocker)
            break
        stage = candidate

    approval_ready = stage == ConnectorConnectionStage.MOBILE_DEVICE_SUBSCRIBED
    certification_blockers: list[str] = []
    if approval_ready:
        if not evidence.physical_approve_certified:
            certification_blockers.append(
                "Physical mobile approve path is not certified."
            )
        if not evidence.physical_reject_certified:
            certification_blockers.append(
                "Physical mobile reject path is not certified."
            )
        if not evidence.same_execution_context_certified:
            certification_blockers.append(
                "Same native execution context continuation is not certified."
            )

    certified = approval_ready and not certification_blockers
    if certified:
        stage = ConnectorConnectionStage.END_TO_END_APPROVAL_CERTIFIED
    blockers.extend(certification_blockers)

    labels = {
        ConnectorConnectionStage.DISCONNECTED: "Not connected",
        ConnectorConnectionStage.BACKEND_REACHABLE: (
            "Backend reachable; host not registered"
        ),
        ConnectorConnectionStage.HOST_REGISTERED: (
            "Host registered; provider not authorized"
        ),
        ConnectorConnectionStage.PROVIDER_AUTHORIZED: (
            "Provider authorized; native session not discovered"
        ),
        ConnectorConnectionStage.NATIVE_SESSION_DISCOVERED: (
            "Native session found; approval channel unverified"
        ),
        ConnectorConnectionStage.NATIVE_APPROVAL_CHANNEL_VERIFIED: (
            "Approval channel verified; mobile subscription pending"
        ),
        ConnectorConnectionStage.MOBILE_DEVICE_SUBSCRIBED: (
            "Approval delivery ready; certification pending"
        ),
        ConnectorConnectionStage.END_TO_END_APPROVAL_CERTIFIED: (
            "Native approval control certified"
        ),
    }

    return ConnectorConnectionStatus(
        stage=stage,
        customer_label=labels[stage],
        operational=stage
        in {
            ConnectorConnectionStage.MOBILE_DEVICE_SUBSCRIBED,
            ConnectorConnectionStage.END_TO_END_APPROVAL_CERTIFIED,
        },
        approval_delivery_ready=approval_ready or certified,
        end_to_end_certified=certified,
        blockers=blockers,
    )
