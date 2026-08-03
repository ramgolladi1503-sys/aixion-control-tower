from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from .store import store
from .trust_flight_recorder import verify_flight_recorder
from .trust_models import CapabilityLeaseStatus, PolicyDecisionType


def _metric_line(
    name: str,
    value: int | float,
    labels: dict[str, str] | None = None,
) -> str:
    label_text = ""
    if labels:
        rendered = ",".join(
            f'{key}="{str(item).replace(chr(34), chr(92) + chr(34))}"'
            for key, item in sorted(labels.items())
        )
        label_text = "{" + rendered + "}"
    return f"{name}{label_text} {value}"


def prometheus_trust_metrics() -> str:
    now = datetime.now(timezone.utc)
    effective_lease_statuses = Counter()
    for lease in store.capability_leases.values():
        status = lease.status
        if status == CapabilityLeaseStatus.ACTIVE and lease.expires_at <= now:
            status = CapabilityLeaseStatus.EXPIRED
        effective_lease_statuses[status.value] += 1

    decision_counts = Counter(
        decision.decision.value for decision in store.policy_decisions.values()
    )
    provider_decisions = Counter()
    for decision in store.policy_decisions.values():
        action = store.proposed_actions.get(decision.proposed_action_id)
        if action:
            provider_decisions[(action.provider.value, decision.decision.value)] += 1

    verification = verify_flight_recorder()
    active_credentials = sum(
        not grant.revoked and grant.expires_at > now
        for grant in store.credential_grants.values()
    )
    revoked_credentials = sum(grant.revoked for grant in store.credential_grants.values())

    lines = [
        "# HELP aixion_capability_leases_total Capability leases by effective state.",
        "# TYPE aixion_capability_leases_total gauge",
    ]
    for status in CapabilityLeaseStatus:
        lines.append(
            _metric_line(
                "aixion_capability_leases_total",
                effective_lease_statuses[status.value],
                {"status": status.value},
            )
        )

    lines.extend(
        [
            "# HELP aixion_policy_decisions_total Trust policy decisions.",
            "# TYPE aixion_policy_decisions_total counter",
        ]
    )
    for decision in PolicyDecisionType:
        lines.append(
            _metric_line(
                "aixion_policy_decisions_total",
                decision_counts[decision.value],
                {"decision": decision.value},
            )
        )
    for (provider, decision), count in sorted(provider_decisions.items()):
        lines.append(
            _metric_line(
                "aixion_policy_decisions_by_provider_total",
                count,
                {"provider": provider, "decision": decision},
            )
        )

    lines.extend(
        [
            "# HELP aixion_action_authorizations_total Exact human action decisions.",
            "# TYPE aixion_action_authorizations_total counter",
            _metric_line(
                "aixion_action_authorizations_total",
                len(store.action_authorizations),
            ),
            "# HELP aixion_action_consumptions_total Allowed actions with sealed consumption.",
            "# TYPE aixion_action_consumptions_total counter",
            _metric_line(
                "aixion_action_consumptions_total",
                len(store.action_consumptions),
            ),
            "# HELP aixion_credential_grants_active Active short-lived credentials.",
            "# TYPE aixion_credential_grants_active gauge",
            _metric_line("aixion_credential_grants_active", active_credentials),
            "# HELP aixion_credential_grants_revoked_total Revoked credentials.",
            "# TYPE aixion_credential_grants_revoked_total counter",
            _metric_line(
                "aixion_credential_grants_revoked_total",
                revoked_credentials,
            ),
            "# HELP aixion_trust_flight_recorder_valid Hash-chain verification state.",
            "# TYPE aixion_trust_flight_recorder_valid gauge",
            _metric_line(
                "aixion_trust_flight_recorder_valid",
                1 if verification.valid else 0,
            ),
            "# HELP aixion_trust_flight_recorder_events_total Recorded trust events.",
            "# TYPE aixion_trust_flight_recorder_events_total gauge",
            _metric_line(
                "aixion_trust_flight_recorder_events_total",
                verification.event_count,
            ),
        ]
    )
    return "\n".join(lines) + "\n"
