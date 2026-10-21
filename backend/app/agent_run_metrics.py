from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from .agent_run_models import AgentRunStatus, AgentRunStepStatus, AgentRunSummary
from .agent_run_repository import AgentRunRepository
from .store import store
from .notification_outbox_repository import PostgresNotificationOutboxRepository
from .trust_metrics import prometheus_trust_metrics
from .workspace_access import agent_run_scope_is_consistent, agent_run_workspace_id


def _workspace_runs(workspace_id: str | None):
    runs = [run for run in store.agent_runs.values() if agent_run_scope_is_consistent(run)]
    if workspace_id is None:
        return runs
    return [run for run in runs if agent_run_workspace_id(run) == workspace_id]


def build_agent_run_summary(
    workspace_id: str | None = None,
    *,
    repository: AgentRunRepository | None = None,
) -> AgentRunSummary:
    runs = _workspace_runs(workspace_id)
    if repository is not None:
        runs = repository.list_for_workspace(workspace_id=workspace_id, limit=500)
        if len(runs) == 500:
            raise RuntimeError(
                "AgentRun summary exceeded the repository page limit; refusing to report partial totals."
            )
    run_ids = {run.id for run in runs}
    steps = (
        [
            step
            for run_id in run_ids
            for step in repository.list_steps_for_run(run_id)
        ]
        if repository is not None
        else list(store.agent_run_steps.values())
    )
    statuses = Counter(run.status for run in runs)
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
        for step in steps
        if step.run_id in run_ids
        and step.status
        in {
            AgentRunStepStatus.PENDING,
            AgentRunStepStatus.READY,
            AgentRunStepStatus.RETRY_WAIT,
        }
    )
    return AgentRunSummary(
        total=len(runs),
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


def prometheus_agent_run_metrics(workspace_id: str | None = None) -> str:
    runs = _workspace_runs(workspace_id)
    run_ids = {run.id for run in runs}
    steps = [step for step in store.agent_run_steps.values() if step.run_id in run_ids]
    events = [event for event in store.agent_run_events.values() if event.run_id in run_ids]
    lines = [
        "# HELP aixion_agent_runs_total Durable agent runs by state.",
        "# TYPE aixion_agent_runs_total gauge",
    ]
    run_statuses = Counter(run.status.value for run in runs)
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
        for step in steps
    )
    for (step_type, status), count in sorted(step_statuses.items()):
        lines.append(
            _metric_line(
                "aixion_agent_run_steps_total",
                count,
                {"step_type": step_type, "status": status},
            )
        )

    step_duration_seconds: dict[str, list[float | int]] = {}
    for step in steps:
        if step.duration_ms is None:
            continue
        totals = step_duration_seconds.setdefault(step.step_type.value, [0.0, 0])
        totals[0] += step.duration_ms / 1000.0
        totals[1] += 1

    summary = build_agent_run_summary(workspace_id)
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
                    for step in steps
                ),
            ),
            "# HELP aixion_agent_run_duplicate_deliveries_total Duplicate callbacks ignored.",
            "# TYPE aixion_agent_run_duplicate_deliveries_total counter",
            _metric_line(
                "aixion_agent_run_duplicate_deliveries_total",
                sum(
                    1
                    for event in events
                    if event.event_type.value == "DUPLICATE_DELIVERY_IGNORED"
                ),
            ),
            "# HELP aixion_agent_run_stale_lease_recoveries_total Expired leases recovered.",
            "# TYPE aixion_agent_run_stale_lease_recoveries_total counter",
            _metric_line(
                "aixion_agent_run_stale_lease_recoveries_total",
                sum(
                    1
                    for event in events
                    if event.event_type.value == "STALE_LEASE_RECOVERED"
                ),
            ),
            "# HELP aixion_agent_run_step_duration_seconds Completed step duration.",
            "# TYPE aixion_agent_run_step_duration_seconds summary",
        ]
    )
    for step_type, (duration_sum, sample_count) in sorted(step_duration_seconds.items()):
        labels = {"step_type": step_type}
        lines.append(
            _metric_line(
                "aixion_agent_run_step_duration_seconds_sum",
                duration_sum,
                labels,
            )
        )
        lines.append(
            _metric_line(
                "aixion_agent_run_step_duration_seconds_count",
                sample_count,
                labels,
            )
        )

    now = datetime.now(timezone.utc)
    expired_leases = sum(
        1
        for run in runs
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
    return run_metrics + prometheus_trust_metrics(workspace_id)


def prometheus_postgres_notification_metrics(
    repository: PostgresNotificationOutboxRepository,
    workspace_id: str | None,
) -> str:
    """Expose explicit PostgreSQL outbox aggregates without leaking row identity."""
    if not workspace_id:
        raise ValueError("PostgreSQL notification metrics require workspace scope")
    snapshot = repository.metrics_snapshot(workspace_id=workspace_id)
    lines = [
        "# HELP aixion_postgres_push_notifications_total PostgreSQL push notifications by delivery state.",
        "# TYPE aixion_postgres_push_notifications_total gauge",
    ]
    for status, count in sorted(snapshot.status_counts.items()):
        lines.append(
            _metric_line(
                "aixion_postgres_push_notifications_total",
                count,
                {"status": status},
            )
        )
    lines.extend(
        [
            "# HELP aixion_postgres_push_outbox_queue_depth PostgreSQL notifications pending, retrying, or leased.",
            "# TYPE aixion_postgres_push_outbox_queue_depth gauge",
            _metric_line(
                "aixion_postgres_push_outbox_queue_depth", snapshot.queue_depth
            ),
            "# HELP aixion_postgres_push_outbox_oldest_pending_age_seconds Age of the oldest PostgreSQL queued notification.",
            "# TYPE aixion_postgres_push_outbox_oldest_pending_age_seconds gauge",
            _metric_line(
                "aixion_postgres_push_outbox_oldest_pending_age_seconds",
                snapshot.oldest_pending_age_seconds,
            ),
        ]
    )
    return "\n".join(lines) + "\n"
