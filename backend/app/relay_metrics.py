from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from .relay_models import RelayCommandStatus, RelaySessionStatus, RelayStatus
from .store import store


def _line(
    name: str,
    value: int | float,
    labels: dict[str, str] | None = None,
) -> str:
    suffix = ""
    if labels:
        rendered = ",".join(
            f'{key}="{str(item).replace(chr(34), chr(92) + chr(34))}"'
            for key, item in sorted(labels.items())
        )
        suffix = "{" + rendered + "}"
    return f"{name}{suffix} {value}"


def prometheus_relay_metrics() -> str:
    now = datetime.now(timezone.utc)
    relay_statuses = Counter(relay.status.value for relay in store.relay_hosts.values())
    session_statuses = Counter(
        (session.provider.value, session.status.value)
        for session in store.relay_sessions.values()
    )
    command_statuses = Counter(
        command.status.value for command in store.relay_commands.values()
    )
    adapter_availability = Counter(
        (adapter.provider.value, adapter.adapter_id, str(adapter.available).lower())
        for relay in store.relay_hosts.values()
        for adapter in relay.adapters
    )
    expired_leases = sum(
        command.status in {
            RelayCommandStatus.LEASED,
            RelayCommandStatus.ACKNOWLEDGED,
        }
        and command.lease_expires_at is not None
        and command.lease_expires_at <= now
        for command in store.relay_commands.values()
    )
    event_counts = Counter(
        (event.provider.value, event.event_type.value)
        for event in store.relay_events.values()
    )

    lines = [
        "# HELP aixion_relay_hosts_total Relay hosts by state.",
        "# TYPE aixion_relay_hosts_total gauge",
    ]
    for status in RelayStatus:
        lines.append(
            _line(
                "aixion_relay_hosts_total",
                relay_statuses[status.value],
                {"status": status.value},
            )
        )

    lines.extend(
        [
            "# HELP aixion_relay_sessions_total Agent relay sessions by provider and state.",
            "# TYPE aixion_relay_sessions_total gauge",
        ]
    )
    providers = sorted({session.provider.value for session in store.relay_sessions.values()})
    for provider in providers:
        for status in RelaySessionStatus:
            lines.append(
                _line(
                    "aixion_relay_sessions_total",
                    session_statuses[(provider, status.value)],
                    {"provider": provider, "status": status.value},
                )
            )

    lines.extend(
        [
            "# HELP aixion_relay_commands_total Durable relay commands by state.",
            "# TYPE aixion_relay_commands_total gauge",
        ]
    )
    for status in RelayCommandStatus:
        lines.append(
            _line(
                "aixion_relay_commands_total",
                command_statuses[status.value],
                {"status": status.value},
            )
        )

    lines.extend(
        [
            "# HELP aixion_relay_command_expired_leases Current expired relay command leases.",
            "# TYPE aixion_relay_command_expired_leases gauge",
            _line("aixion_relay_command_expired_leases", expired_leases),
            "# HELP aixion_relay_events_total Normalized provider events.",
            "# TYPE aixion_relay_events_total counter",
        ]
    )
    for (provider, event_type), count in sorted(event_counts.items()):
        lines.append(
            _line(
                "aixion_relay_events_total",
                count,
                {"provider": provider, "event_type": event_type},
            )
        )

    lines.extend(
        [
            "# HELP aixion_relay_adapter_availability Relay adapter availability.",
            "# TYPE aixion_relay_adapter_availability gauge",
        ]
    )
    for (provider, adapter_id, available), count in sorted(adapter_availability.items()):
        lines.append(
            _line(
                "aixion_relay_adapter_availability",
                1 if available == "true" and count else 0,
                {
                    "provider": provider,
                    "adapter_id": adapter_id,
                },
            )
        )
    return "\n".join(lines) + "\n"
