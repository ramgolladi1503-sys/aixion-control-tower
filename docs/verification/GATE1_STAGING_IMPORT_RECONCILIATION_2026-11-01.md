# Gate 1 staging import reconciliation — 2026-11-01

## Result

The exact APFS SQLite snapshot was applied to a newly created, empty local
staging-only PostgreSQL database. The importer returned
`STAGING_IMPORT_APPLIED` and validated the planned records.

A separate database restored from the retained October 31 staging dump was
compared with the fresh import. All 43 nonvolatile application tables and all
36 application rows matched. The two unscoped unknown-agent authentication
failures are present in `legacy_audit_events` with the agreed quarantine
reason and no workspace linkage; no unscoped rows are present in
`audit_events`.

This closes the exact-source staging-import/reconciliation rehearsal only. It
does not close production PostgreSQL authority or authorize production import
or cutover.

## Isolated target and procedure

- Repository revision: `9d8f3c7114fca2c0f0bfe3be12b278f77c5fd51e`.
- PostgreSQL 17 scratch cluster:
  `/private/tmp/aixion-gate1-reconcile-20261101-01`
- The cluster listened only on a private Unix socket, port `59175`; no TCP
  listener was configured.
- Restored retained backup into:
  `aixion_gate1_snapshot_reconcile_20261101`
- Created a separate, initially empty target:
  `aixion_gate1_reconcile_20261101_staging`
- Applied the exact APFS snapshot using the repository importer with
  `--apply --confirm-source-snapshot`. The target name passed the importer’s
  `_staging` requirement and its public schema had zero tables before apply.
- No `--allow-partial-slice` acknowledgement was needed: the plan reported
  `partial=false` and `omitted_counts={}`.
- The importer returned `STAGING_IMPORT_APPLIED` for scope
  `approval_auth_agent_mcp_test_runs_connectors_email_task_notifications_delivery_rate_limits_invocation_leases_faults_legacy_audit_global_auth_quarantine_v15`.

The apply occurred after the earlier
`GATE1_SOURCE_SNAPSHOT_RECONCILIATION_2026-11-01.md` snapshot-only check. That
earlier note’s statement that no apply had occurred describes that earlier
check; this document records the later, separate scratch-cluster rehearsal.

## Source and imported counts

The import record source hash is
`2dd8f2e50250f2afa09160aab81d034b7bece0e575fb9be9bff604339561ba2e`, matching
the owner-only APFS snapshot and the retained staging archive’s import record.

The plan/import record reports:

| Entity | Source or planned/imported count |
| --- | ---: |
| users | 3 |
| workspaces | 3 |
| projects | 1 |
| sessions | 5 |
| external agents | 1 |
| external agent credentials | 1 |
| auth rate-limit buckets | 17 |
| source audit events | 4 |
| imported workspace-scoped audit events | 2 |
| planned legacy audit events | 2 |
| omitted entities | 0 |

Read-only target queries after apply confirmed:

- `schema_migrations`: 40 rows.
- `users=3`, `workspaces=3`, `projects=1`, `sessions=5`.
- `external_agents=1`, `external_agent_credentials=1`,
  `auth_rate_limit_buckets=17`.
- `audit_events` with `workspace_id IS NULL`: 0.
- `legacy_audit_events` with null workspace and reason
  `UNSCOPED_UNKNOWN_AGENT_AUTH_FAILURE` for event type `agent.auth_failed`: 2.
- Other workspace-linked `legacy_audit_events`: 0.

The retained backup’s import record has the same source hash and source/import
counts. Its restored database also contains exactly two matching quarantine
rows and zero unscoped `audit_events`.

## Cross-database row comparison

The restored backup and the fresh source import each contained 45 public base
tables. `schema_migrations` and `sqlite_import_runs` were excluded because
migration/import timestamps differ between the two rehearsals. For each of the
remaining 43 tables, row counts and MD5 digests of sorted `to_jsonb(row)::text`
values were compared.

Result: **43/43 tables matched; 36/36 application rows matched; zero
mismatched tables.** The importer also performs source-plan-to-target payload
validation during apply.

## Cleanup and limits

The scratch PostgreSQL server was stopped cleanly after read-only verification.
Its local data directory remains preserved at the path above; nothing on the
external TradeBot volume was deleted or changed. The retained staging dump and
the authoritative SQLite source were not modified.

The importer is intentionally scoped and is not an app-wide migration or
cutover tool. Production still requires PostgreSQL authority for all supported
routes, workers, schedulers, and process state; full migration/restart/
isolation verification; and production backup/restore and recovery evidence.
The production startup guard remains closed.

No production database, cloud resource, billing setting, DNS record, or
deployment was changed. No merge occurred.
