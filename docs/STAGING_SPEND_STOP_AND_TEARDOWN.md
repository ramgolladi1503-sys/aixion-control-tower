# Staging Spend Stop and Teardown Checklist

**Status:** Offline operator checklist only. It is not an authorization or executable runbook.
**Scope:** Proposed GCP staging resources in `deploy/gcp/cloud-run-staging.yaml`,
`deploy/gcp/cloud-run-jobs-staging.yaml`, and `deploy/gcp/cloud-scheduler-staging.tf`.
**Gate:** Do not use until the owner confirms the exact project, billing association,
region, operators, data-retention decision, and maximum uncapped-service exposure.
The manifests remain `DO NOT APPLY` until Slice 1 and all Slice 7 gates pass.
**Related artifacts:** [Pre-billing work lock](PRE_BILLING_WORK_LOCK.md),
[cloud prework status](CLOUD_PREWORK_STATUS_2026-10-22.md), and
[staging handoff cost model](STAGING_HANDOFF_COST_MODEL.md).

This checklist has two distinct outcomes:

- **Containment** prevents new work while preserving recoverable data and configuration.
  It is the first response to a spend alert.
- **Decommissioning** permanently removes selected resources after retention,
  recovery, legal, and owner approvals. It is not the default response to an alert.

A Cloud Billing budget is not a universal hard cap. Usage reporting and enforcement
can lag; charges already incurred and uncapped services can remain billable. Verify
the actual project and resources in the console before acting. Never infer the
target from a local default project, a billing-account page, or a resource name.

## Before either procedure

Record in the approved change/incident record:

- Exact project ID and project number, billing account association, region, and
  Firebase project boundary.
- Triggering alert, current billing view and timestamp, budget/cap scope, and
  categories excluded from that cap.
- Named operator and reviewer, incident/change ID, decision time, and approved
  maximum exposure for remaining resources.
- Exact inventory of Cloud Scheduler jobs, Cloud Run services and jobs/executions,
  Cloud SQL instances and backups/PITR, Artifact Registry repositories/images,
  Secret Manager secrets/versions, log buckets/sinks/retention, Monitoring
  policies/channels, IAM bindings/service identities, network resources, and any
  Cloud Storage export locations.
- Whether any real user approvals, audit records, credentials, or personal data
  exist. If data classification or retention is unknown, stop before deletion.

If the project or inventory does not match the approved record, stop. Do not
disable billing or APIs as a shortcut: that can affect unrelated project services
and does not replace resource-by-resource verification.

## Copyable incident evidence record

Create one restricted record for each containment or decommissioning event. Keep
the authorization record separate and link to it here; this template does not
grant authority. Use `UNKNOWN` when a value cannot be verified. Do not infer a
project, resource, owner, cost, or approval from a local default or resource name.

```text
Record ID / incident or change ID:
Record type: CONTAINMENT | DECOMMISSIONING
Exact project ID and project number:
Billing account reference (restricted if needed):
Region(s) and Firebase project boundary:
Trigger and source timestamp:
Billing view/export reference and observation timestamp:
Budget or spend-cap scope:
Known charges/services outside that scope:
Owner authorization record reference and approved action/time window:
Named operator / role:
Named reviewer / role:
Data classification and retention decision reference:
Current approved maximum exposure for remaining resources:
If any required value is UNKNOWN, stop and record the blocker:
```

### Pre-action gate record

Mark each item `PASS`, `FAIL`, or `UNKNOWN` and link to restricted evidence. Any
`FAIL` or `UNKNOWN` blocks destructive actions; it does not authorize a narrower
action by default.

| Gate | Result | Evidence reference and timestamp | Operator / reviewer |
| --- | --- | --- | --- |
| Exact project, billing association, and region match the authorization | PENDING | PENDING | PENDING |
| Live resource inventory matches the approved inventory; no shared or unlisted resource is in scope | PENDING | PENDING | PENDING |
| Trigger, cap scope, and excluded cost categories have been reviewed | PENDING | PENDING | PENDING |
| Active executions and API/database writers are identified | PENDING | PENDING | PENDING |
| Data classification, retention, backup, and recovery decision is recorded | PENDING | PENDING | PENDING |
| Named operator, reviewer, response path, and time window are available | PENDING | PENDING | PENDING |
| Residual exposure is quantified, owner-approved, and within the authorized bound | PENDING | PENDING | PENDING |

An `UNKNOWN` exposure is not a bounded maximum. If the owner has not explicitly
accepted the residual exposure in the authorization record, do not proceed.

### Action and verification log

Record one row per action and resource. Capture the exact resource identifier,
not only its display name. For containment, record state and verification for
every applicable step. For decommissioning, include the approved deletion list
and the required recovery evidence before recording a deletion.

| Sequence | Exact resource ID / type | Authorized action and authorization reference | Observed pre-state, source, timestamp | Operator and action timestamp | Observed post-state, source, timestamp | Residual charge / retained data / owner | Reviewer |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| 2 | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| 3 | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |

If observed state differs from the approved inventory or an action fails, stop
the sequence, preserve evidence, and obtain a revised decision. Do not retry a
destructive action based only on a timeout or missing console response; first
read the resource state again and reconcile the outcome.

### Residual exposure and closure

| Service/resource category | Exact retained resource or `NONE VERIFIED` | Expected continuing charge and evidence, or `UNKNOWN` | Responsible owner | Next billing recheck date / evidence |
| --- | --- | --- | --- | --- |
| Cloud SQL compute, storage, backups, and PITR | PENDING | PENDING | PENDING | PENDING |
| Cloud Run services, jobs, and active executions | PENDING | PENDING | PENDING | PENDING |
| Scheduler, Artifact Registry, Secret Manager, logs/metrics, and network | PENDING | PENDING | PENDING | PENDING |
| Firebase/FCM and shared-project resources | PENDING | PENDING | PENDING | PENDING |
| Other discovered resource | PENDING | PENDING | PENDING | PENDING |

```text
Containment/decommissioning outcome: OPEN | CONTAINED | DECOMMISSIONED | BLOCKED
Unresolved actions or UNKNOWN states:
Residual exposure decision reference (must be owner-approved):
Follow-up billing observation date and evidence reference:
Operator signoff and timestamp:
Reviewer signoff and timestamp:
```

Do not mark the event closed while a required resource state, recovery decision,
or residual-cost owner is `UNKNOWN`. A zero or unchanged billing view immediately
after containment or deletion does not establish that charges have stopped.

## A. Containment: stop new work and bound ongoing exposure

Perform in order; record the observed state after each action.

1. **Pause both scheduler triggers first.** Pause the push and email Cloud
   Scheduler jobs. Verify each is paused and note any invocation already in flight.
   Pausing prevents future scheduled triggers; it does not cancel an execution
   already accepted by Cloud Run Jobs.
2. **Stop new API traffic using the reviewed service control.** The draft Cloud
   Run service has no generic pause operation. Apply only the owner-approved
   traffic/ingress restriction or service-removal action for this incident.
   Verify the public endpoint no longer accepts application requests. Do not
   assume scale-to-zero prevents request-driven charges.
3. **Drain or stop active Cloud Run Job executions.** Confirm no new executions
   are being submitted, then let bounded work finish or stop it under the
   incident decision. Verify the execution list is terminal. Do not retry jobs
   manually during containment; their outbox work is durable and must remain
   idempotent.
4. **Protect database correctness.** Confirm API and job writers are stopped
   before pausing or stopping Cloud SQL. If the database must remain recoverable,
   preserve it and its backups while the owner selects the next action. Stopping
   a Cloud SQL instance can reduce compute charges, but provisioned storage and
   retained backups can still incur charges.
5. **Revoke exposure, not evidence.** If compromise is suspected, revoke the
   affected runtime/scheduler access through the approved IAM process and rotate
   affected credentials. Preserve audit logs, migration ledger, secret version
   metadata, and incident evidence. Do not delete secret versions or audit data
   as a cost shortcut.
6. **Confirm containment.** Recheck scheduler state, job executions, API
   reachability, Cloud SQL state, active identities, and current billing
   categories. Capture timestamps and screenshots/exports in the restricted
   incident record; do not include secret values, tokens, approval payloads, or
   raw tool arguments.

Containment is not decommissioning. Keep resource owners and billing alerts active
until the reviewer confirms the residual-cost plan.

## B. Decommissioning: permanent removal after approval

Proceed only with a separate owner-approved decommission record and reviewer.
Do not delete a database, backup, secret, log, identity, or shared Firebase project
while its retention or dependency status is unknown.

1. **Freeze and verify the inventory.** Compare the live project inventory with
   the approved resource list. Identify shared resources and dependencies; record
   immutable image digests, migration state, secret names/versions, scheduler
   targets, and service identities. Stop if any unlisted resource is discovered.
2. **Disable triggers and application entry points.** Confirm both Scheduler jobs
   remain paused, no job execution is active, and API traffic is blocked. Delete
   the two scheduler jobs only after recording their definitions and required
   audit evidence. Then delete the two Cloud Run Jobs and the API service if the
   owner approved permanent service removal.
3. **Preserve database recovery evidence.** With all writers stopped, follow
   `POSTGRES_BACKUP_RESTORE_RUNBOOK.md` to create and verify the separately
   approved final backup/export. Record checksum, source identity, time,
   encryption/location, retention owner, and restore verification. Do not treat
   an unverified backup as recoverable.
4. **Resolve Cloud SQL retention explicitly.** Decide whether the instance,
   automated backups, on-demand backups, and PITR/WAL retention must be retained,
   exported, or deleted. Verify the selected Cloud SQL deletion/backup-retention
   behavior in the console before confirmation. Delete the instance only after
   the owner approves the data-loss boundary and the reviewer confirms the
   required recovery copy is accessible. Verify whether retained backup/storage
   charges continue.
5. **Remove stored images and exports deliberately.** Cloud Run service deletion
   does not remove Artifact Registry images. Retain only images required by the
   approved rollback/evidence period, then delete the remaining repository
   contents under the retention decision. Inspect Cloud Storage export buckets,
   lifecycle rules, and retained objects separately; deleting a service does not
   remove them.
6. **Retire secrets and identities after consumers are gone.** Confirm no service,
   job, migration, or break-glass process depends on each secret/version.
   Revoke runtime and scheduler bindings, then disable/delete secret versions
   only after the approved recovery window. Remove service accounts and custom
   IAM bindings after verifying they are not shared by another workload.
7. **Review observability and residual resources.** Apply the approved retention
   decision to log buckets, sinks, metrics, alert policies, and notification
   channels. Check for load balancers, static addresses, disks, snapshots, network
   egress/NAT, DNS, and other resources not represented by the current
   draft manifests. Do not delete a shared Firebase project; review FCM project
   resources and bindings separately.
8. **Verify closure and residual spend.** Re-inventory the project and compare it
   with the approved deletion list. Record every retained resource, its owner,
   retention end date, and expected residual charge. Recheck billing after
   reporting catches up; a zero or unchanged dashboard immediately after teardown
   is not proof that no charges remain. Keep the billing account/project owner
   and alerts in place until final charges and retained-resource costs are
   reviewed.

## Evidence required to close a rehearsal

A future disposable-project rehearsal should retain:

- Approved project/resource inventory and change authorization.
- Before/after resource state and timestamps for Scheduler, Cloud Run, Cloud SQL,
  backups, registry, secrets, IAM, logging, and monitoring.
- Proof no scheduled execution or API writer remained active before database
  backup/stop/delete.
- Backup checksum and isolated restore result if database data was retained.
- Cost/billing snapshots before and after, including a later check after billing
  reporting latency.
- List of residual resources, their owners, retention periods, and estimated
  continuing exposure.
- Operator and reviewer signoff, plus any failure and remediation record.

A rehearsal against local files, Terraform validation, a dry-run plan, or a
successful deletion of a test resource is not evidence that this procedure has
been rehearsed against the selected staging project.

## Resource-specific cautions

- **Cloud Scheduler:** pause before deleting targets; a pause does not stop an
  already-started Cloud Run Job execution.
- **Cloud Run:** deleting a service removes its revisions but does not delete
  Artifact Registry images. Service deletion is permanent; redeployment under the
  same name creates a new service.
- **Cloud SQL:** instance compute can be stopped, but provisioned storage and
  backups may remain chargeable. Final backup, retention, and restore authority
  must be settled before deletion.
- **Artifact Registry:** remove images separately and preserve any approved
  rollback image until its retention period ends.
- **Secret Manager and IAM:** revoke consumers before deleting secret material;
  do not remove shared human break-glass access needed for recovery.
- **Logging/Monitoring:** retention and ingestion charges may continue independently
  of application compute. Preserve the audit evidence required by policy.
- **Firebase/FCM:** treat its project and billing boundary as separate until
  ownership and sharing are confirmed. Do not delete the Firebase project to stop
  this staging workload.

## References

- [Cloud Scheduler pause and resume jobs](https://docs.cloud.google.com/scheduler/docs/creating)
- [Cloud Run service deletion and retained images](https://docs.cloud.google.com/run/docs/managing/services)
- [Cloud SQL PostgreSQL FAQ: stopped-instance, storage, and backup charges](https://docs.cloud.google.com/sql/docs/postgres/faq)
- [Cloud SQL backup and deletion recovery considerations](https://docs.cloud.google.com/sql/docs/postgres/backup-recovery/backups)
- [Cloud Billing reporting delays](https://docs.cloud.google.com/billing/docs/how-to/resolve-issues)
- [Cloud Billing spend-cap scope and enforcement limitations](https://docs.cloud.google.com/billing/docs/how-to/budgets-spend-caps)

