from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from .agent_run_models import AgentRunStatus, AgentRunStepStatus, AgentRunSummary
from .store import store
from .trust_metrics import prometheus_trust_metrics


def build_agent_run_summary() -> AgentRunSummary:
    statuses = Counter(run.status for run in store.agent_runs.values())
    active_statuses = {
        AgentRunStatus.SCHEDULED,
        AgentRunStatus.RUNNING,
        AgentRunStatus.EVALUATING,
        AgentRunStatus.RETRY_WAIT,
        AgentRunStatus.PAUSED,
        AgentRunStatus.NEEDS_HUMAN,
    }
    queue_depth = sum(
        1
        for step in store.agent_run_steps.values()
        if step.status
        in {
            AgentRunStepStatus.PENDING,
            AgentRunStepStatus.READY,
            AgentRunStepStatus.RETRY_WAIT,
        }
    )
    return AgentRunSummary(
        total=len(store.agent_runs),
        active=sum(count for status, count in statuses.items() if status in active_statuses),
        retry_wait=statuses[AgentRunStatus.RETRY_WAIT],
        needs_human=statuses[AgentRunStatus.NEEDS_HUMAN],
        blocked=statuses[AgentRunStatus.BLOCKED],
        failed=statuses[AgentRunStatus.FAILED],
        succeeded=statuses[AgentRunStatus.SUCCEEDED],
        cancelled=statuses[AgentRunStatus.CANCELLED],
        queue_depth=queue_depth,
    )


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


def prometheus_agent_run_metrics() -> str:
    lines = [
        "# HELP aixion_agent_runs_total Durable agent runs by state.",
        "# TYPE aixion_agent_runs_total gauge",
    ]
    run_statuses = Counter(run.status.value for run in store.agent_runs.values())
    for status in AgentRunStatus:
        lines.append(
            _metric_line(
                "aixion_agent_runs_total",
                run_statuses[status.value],
                {"status": status.value},
            )
        )

    lines.extend(
        [
            "# HELP aixion_agent_run_steps_total Durable run steps by state and type.",
            "# TYPE aixion_agent_run_steps_total gauge",
        ]
    )
    step_statuses = Counter(
        (step.step_type.value, step.status.value)
        for step in store.agent_run_steps.values()
    )
    for (step_type, status), count in sorted(step_statuses.items()):
        lines.append(
            _metric_line(
                "aixion_agent_run_steps_total",
                count,
                {"step_type": step_type, "status": status},
            )
        )

    summary = build_agent_run_summary()
    lines.extend(
        [
            "# HELP aixion_agent_run_queue_depth Steps waiting for execution or retry.",
            "# TYPE aixion_agent_run_queue_depth gauge",
            _metric_line("aixion_agent_run_queue_depth", summary.queue_depth),
            "# HELP aixion_agent_run_retry_attempts_total Total attempts beyond the first.",
            "# TYPE aixion_agent_run_retry_attempts_total counter",
            _metric_line(
                "aixion_agent_run_retry_attempts_total",
                sum(
                    max(0, step.attempt_count - 1)
                    for step in store.agent_run_steps.values()
                ),
            ),
            "# HELP aixion_agent_run_duplicate_deliveries_total Duplicate callbacks ignored.",
            "# TYPE aixion_agent_run_duplicate_deliveries_total counter",
            _metric_line(
                "aixion_agent_run_duplicate_deliveries_total",
                sum(
                    1
                    for event in store.agent_run_events.values()
                    if event.event_type.value == "DUPLICATE_DELIVERY_IGNORED"
                ),
            ),
            "# HELP aixion_agent_run_stale_lease_recoveries_total Expired leases recovered.",
            "# TYPE aixion_agent_run_stale_lease_recoveries_total counter",
            _metric_line(
                "aixion_agent_run_stale_lease_recoveries_total",
                sum(
                    1
                    for event in store.agent_run_events.values()
                    if event.event_type.value == "STALE_LEASE_RECOVERED"
                ),
            ),
            "# HELP aixion_agent_run_step_duration_seconds Completed step duration.",
            "# TYPE aixion_agent_run_step_duration_seconds summary",
        ]
    )
    for step in sorted(
        store.agent_run_steps.values(),
        key=lambda item: (item.run_id, item.sequence),
    ):
        if step.duration_ms is not None:
            lines.append(
                _metric_line(
                    "aixion_agent_run_step_duration_seconds",
                    step.duration_ms / 1000.0,
                    {"run_id": step.run_id, "step_type": step.step_type.value},
                )
            )

    now = datetime.now(timezone.utc)
    expired_leases = sum(
        1
        for run in store.agent_runs.values()
        if run.lease_expires_at is not None and run.lease_expires_at <= now
    )
    lines.extend(
        [
            "# HELP aixion_agent_run_expired_leases Current expired run leases.",
            "# TYPE aixion_agent_run_expired_leases gauge",
            _metric_line("aixion_agent_run_expired_leases", expired_leases),
        ]
    )
    run_metrics = "\n".join(lines) + "\n"
    return run_metrics + prometheus_trust_metrics()
