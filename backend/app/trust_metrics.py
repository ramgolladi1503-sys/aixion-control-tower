from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from .store import store
from .trust_flight_recorder import verify_flight_recorder
from .postgres_trust_repository import PostgresTrustMetricsSnapshot
from .trust_models import CapabilityLeaseStatus, PolicyDecisionType
from .workspace_access import entity_workspace_id


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


def prometheus_trust_metrics(
    workspace_id: str | None = None,
    *,
    postgres_snapshot: PostgresTrustMetricsSnapshot | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    if postgres_snapshot is None:
        leases = [lease for lease in store.capability_leases.values()
                  if workspace_id is None or entity_workspace_id(lease.id) == workspace_id]
        actions = [action for action in store.proposed_actions.values()
                   if workspace_id is None or entity_workspace_id(action.id) == workspace_id]
        action_ids = {action.id for action in actions}
        grants = [grant for grant in store.credential_grants.values()
                  if workspace_id is None or entity_workspace_id(grant.id) == workspace_id]
        authorizations = [item for item in store.action_authorizations.values()
                          if workspace_id is None or item.action_id in action_ids]
        consumptions = {key: value for key, value in store.action_consumptions.items()
                        if workspace_id is None or key in action_ids}
        trust_events = [event for event in store.trust_events.values()
                        if workspace_id is None or entity_workspace_id(event.entity_id) == workspace_id]
        notifications = [
            item for item in store.notifications.values()
            if workspace_id is None
            or (item.workspace_id or entity_workspace_id(item.entity_id)) == workspace_id
        ]
        effective_lease_statuses = Counter()
        for lease in leases:
            status = lease.status
            if status == CapabilityLeaseStatus.ACTIVE and lease.expires_at <= now:
                status = CapabilityLeaseStatus.EXPIRED
            effective_lease_statuses[status.value] += 1

        decisions = [decision for decision in store.policy_decisions.values()
                     if decision.proposed_action_id in action_ids or workspace_id is None]
        decision_counts = Counter(decision.decision.value for decision in decisions)
        provider_decisions = Counter()
        for decision in decisions:
            action = store.proposed_actions.get(decision.proposed_action_id)
            if action:
                provider_decisions[(action.provider.value, decision.decision.value)] += 1

        verification = verify_flight_recorder()
        active_credentials = sum(
            not grant.revoked and grant.expires_at > now
            for grant in grants
        )
        revoked_credentials = sum(grant.revoked for grant in grants)
        push_statuses = Counter(item.push_status for item in notifications)
        authorization_count = len(authorizations)
        consumption_count = len(consumptions)
        trust_event_count = len(trust_events)
        push_attempt_count = sum(item.push_attempts for item in notifications)
        push_queue_depth = sum(
            item.push_status in {"PENDING", "RETRY_WAIT", "SENDING"}
            for item in notifications
        )
    else:
        if not workspace_id:
            raise ValueError("PostgreSQL trust metrics require workspace scope")
        effective_lease_statuses = Counter(postgres_snapshot.lease_status_counts)
        decision_counts = Counter(postgres_snapshot.policy_decision_counts)
        provider_decisions = Counter(postgres_snapshot.provider_decision_counts)
        verification = postgres_snapshot.verification
        active_credentials = postgres_snapshot.active_credential_count
        revoked_credentials = postgres_snapshot.revoked_credential_count
        push_statuses = Counter(postgres_snapshot.push_status_counts)
        authorization_count = postgres_snapshot.authorization_count
        consumption_count = postgres_snapshot.consumption_count
        trust_event_count = postgres_snapshot.trust_event_count
        push_attempt_count = postgres_snapshot.push_attempt_count
        push_queue_depth = postgres_snapshot.push_queue_depth

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
                authorization_count,
            ),
            "# HELP aixion_action_consumptions_total Allowed actions with sealed consumption.",
            "# TYPE aixion_action_consumptions_total counter",
            _metric_line(
                "aixion_action_consumptions_total",
                consumption_count,
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
                trust_event_count,
            ),
            "# HELP aixion_push_notifications_total Durable push notifications by delivery state.",
            "# TYPE aixion_push_notifications_total gauge",
        ]
    )
    for status, count in sorted(push_statuses.items()):
        lines.append(_metric_line("aixion_push_notifications_total", count, {"status": status}))
    lines.extend(
        [
            "# HELP aixion_push_dispatch_attempts_total Push delivery attempts recorded.",
            "# TYPE aixion_push_dispatch_attempts_total counter",
            _metric_line(
                "aixion_push_dispatch_attempts_total",
                push_attempt_count,
            ),
            "# HELP aixion_push_retry_queue_depth Pending, retrying, or leased pushes.",
            "# TYPE aixion_push_retry_queue_depth gauge",
            _metric_line(
                "aixion_push_retry_queue_depth",
                push_queue_depth,
            ),
        ]
    )
    return "\n".join(lines) + "\n"
