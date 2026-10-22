# Pre-Billing Staging Alert Matrix

**Status:** Review proposal only. No Cloud Monitoring policy, notification channel, budget, or cloud resource has been created.
**Scope:** Slice 7 staging handoff under [PRE_BILLING_WORK_LOCK.md](PRE_BILLING_WORK_LOCK.md).
**Related handoff:** [STAGING_HANDOFF_COST_MODEL.md](STAGING_HANDOFF_COST_MODEL.md).

## Current signal inventory

The repository contains these signal endpoints and metric implementations. Their
presence in source does not mean they are all bound to, or ready to monitor, a
production application runtime:

- `GET /health` for process-level liveness.
- `GET /ops/readiness` for configured dependencies, database migration checks, PostgreSQL probe state, recovery-snapshot availability, and configured integration flags. It is authenticated for maintainers.
- `GET /agent/runs/metrics` for agent-run and trust metrics. Production access
  is intended to use a workspace-bound metrics token, but the complete
  PostgreSQL application runtime and production scrape path are not currently
  active. Treat this endpoint and its metrics as implementation groundwork,
  not as an available production signal.

The endpoint implementations do not prove that approval requests reach a phone
or that a phone decision returns to the same provider invocation. The API
currently has split SQLite/PostgreSQL route and worker authority; production
startup remains disabled. Do not configure staging alert policies against
these application metrics until their producers, readers, workers, and scrape
authentication are bound to the selected durable runtime and verified there.
The `/agent/runs/metrics` endpoint now returns HTTP 503 if a PostgreSQL runtime,
non-SQLite AgentRun repository, or separately bound notification repository is
selected. This prevents stale process-local run/trust metrics from appearing
alongside PostgreSQL state; it does not make those metric families PostgreSQL
backed or satisfy the monitoring gate.

## Proposed staging alert definitions

Thresholds below are starting proposals for a low-volume staging environment. Review them against expected traffic and approved response hours before provisioning alert policies. Google Cloud metric data is sampled and delayed; alerts are not instantaneous safety interlocks.

| Alert | Proposed condition | Response | Evidence and limitation |
| --- | --- | --- | --- |
| API unavailable | External HTTPS uptime check fails in 3 consecutive 1-minute checks. Monitor `monitoring.googleapis.com/uptime_check/check_passed`. | Page the staging operator; stop connector certification until service recovery. | Requires a deployed HTTPS endpoint and configured notification channel. Local `/health` checks do not satisfy this condition. |
| API server errors | `run.googleapis.com/request_count`, filtered to `resource.type="cloud_run_revision"` and `metric.label.response_code_class="5xx"`, exceeds 1% of all revision requests over 5 minutes, with at least 20 requests in the window. | Inspect redacted service logs and readiness; roll back the immutable image if tied to a release. | Cloud Run request metrics cover requests reaching a revision; requests rejected before reaching a container are excluded. Data is sampled every 60 seconds and can take up to 120 seconds to appear. |
| API latency | p99 of `run.googleapis.com/request_latencies` exceeds 2 seconds for 10 minutes. | Check PostgreSQL pool wait, database load, and downstream dependencies before changing capacity. | This metric measures request time at the running container and excludes container startup latency. Validate the threshold against a staging baseline. |
| Database capacity | `cloudsql.googleapis.com/database/cpu/utilization` exceeds 0.80 for 15 minutes, or `cloudsql.googleapis.com/database/disk/utilization` exceeds 0.80 of quota. Review `cloudsql.googleapis.com/database/postgresql/num_backends` against the instance's configured connection limit. | Stop nonessential jobs, inspect connection pooling and storage growth, and obtain approval before resizing. | Cloud SQL metrics are sampled every 60 seconds and can be delayed up to 165 seconds. The connection limit must come from the approved instance configuration. This is not a project-wide cost cap. |
| Database availability | The authenticated application database probe fails for 3 consecutive 1-minute checks, or `/ops/readiness` reports `not_ready` for 3 consecutive checks. | Stop approval certification; use the reviewed recovery procedure. Never fail over to SQLite. | Requires a deployed Cloud SQL instance and a protected synthetic monitor identity. Do not expose readiness details publicly. |
| Push outbox backlog | `aixion_postgres_push_outbox_queue_depth > 0` and `aixion_postgres_push_outbox_oldest_pending_age_seconds > 300` for 5 minutes. | Stop claiming phone wake-up is timely; inspect the PostgreSQL dispatcher and FCM terminal-state breakdown. | These workspace-scoped metrics are emitted only when the PostgreSQL outbox repository is explicitly bound to `/agent/runs/metrics`. The app does not currently bind the complete PostgreSQL runtime, so this alert is not deployable yet. Tune 300 seconds against an approved staging SLO. |
| Spend review | Project budget notifications at 50%, 80%, and 100% of the separately approved monthly exposure. | At 50%, review actual usage; at 80%, stop nonessential jobs and deployments; at 100%, suspend staging and execute the reviewed teardown procedure for uncapped resources. | Notifications are not a universal spending ceiling. Cloud SQL and supporting services may continue to incur charges. |
| Backup freshness | No successful managed backup within the owner-approved backup interval. | Stop staging use for real approvals; investigate backup configuration and restore readiness. | The interval and retention must be explicitly approved. A “backup succeeded” signal does not prove restoreability. |

## Missing application-level alerts

The current app metrics do **not** expose complete durable approval delivery health. A PostgreSQL notification outbox aggregate is now available for an explicitly bound repository, but is not active in the default app and does not make the application PostgreSQL-authoritative. Before real approval staging:

1. Oldest pending approval age, with a bounded and non-sensitive label set.
2. Approval decision transaction conflicts and failures.
3. Connector poll/stream liveness and decision-consume failures, without tokens or action arguments.
4. Bind notification producers, readers, dispatcher, and these outbox metrics to the same PostgreSQL application runtime; verify terminal outcomes (`SENT`, `FAILED`, `SKIPPED`, `NO_DEVICE`) and retry exhaustion against PostgreSQL.
5. PostgreSQL pool saturation and transaction failures, using bounded aggregate labels.

Do not create alerts over invented metric names. Each application-level alert stays **unconfigured** until the metric is implemented, tested, and emitted from the production-authoritative PostgreSQL path. Avoid approval IDs, user IDs, connector IDs, project IDs, raw tokens, or tool arguments as metric labels.

## Notification and ownership requirements

Before creating a staging alert policy, record:

- Named primary and backup operators.
- Notification channel and an end-to-end test recipient.
- Response hours and escalation path.
- The action authorized for each severity.
- The staging project, region, and monthly exposure approved at the billing cut line.

Cloud alerting, budget notifications, and automatic spend controls must be verified in the separately authorized staging project. This document is a design proposal and does not authorize billing, API enablement, resource creation, IAM changes, deployment, or production traffic.

## References

- [Cloud Run monitoring and metrics](https://docs.cloud.google.com/run/docs/monitoring) — available service metrics and their scope.
- [Cloud Run metric definitions](https://docs.cloud.google.com/monitoring/api/metrics_gcp_p_z) — request count and latency sampling, labels, and collection delay.
- [Cloud SQL PostgreSQL monitoring](https://docs.cloud.google.com/sql/docs/postgres/monitor-instance) — instance monitoring and available metrics.
- [Cloud SQL metric definitions](https://docs.cloud.google.com/monitoring/api/metrics_gcp_c) — CPU utilization, disk utilization, and PostgreSQL connection metrics.
- [Cloud Monitoring uptime alerts](https://docs.cloud.google.com/monitoring/uptime-checks/uptime-alerting-policies) — uptime check alert configuration.
