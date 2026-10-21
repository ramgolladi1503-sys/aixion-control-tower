# PostgreSQL Backup and Restore Runbook

**Status:** Local synthetic restore drill passed. Cloud SQL backup configuration and restore rehearsal are not performed or approved.
**Scope:** Slice 7 preparation under [PRE_BILLING_WORK_LOCK.md](PRE_BILLING_WORK_LOCK.md).
**Last local drill:** 2026-10-21, PostgreSQL 17.11, isolated loopback test server.

## Safety boundary

- This runbook does not authorize billing, Cloud SQL creation, production data migration, or a restore over any existing database.
- The local drill used two newly created scratch databases on the isolated test server at `/tmp/aixion-ct-pg.O8jwnb/socket:55432`. Its only non-migration data was a synthetic workspace and project. The archive and restore target are test artifacts, not a production backup.
- A Cloud SQL restore must target a new, isolated recovery instance first. Do not point application traffic at it until the checks below pass and the recovery owner approves the switch.
- Before a staging environment is proposed for real approvals, the owner must approve RPO, RTO, retention, data location, and the maximum cost exposure for backups, WAL/PITR, storage, and any temporary restore instance. These values are deliberately not claimed here.
- PostgreSQL schema migrations are forward-only. Prefer a verified backup restored to an isolated target or a reviewed forward repair migration; do not delete migration-ledger rows or run improvised reverse DDL.

## Backup policy to decide before staging

Use Cloud SQL automated backups and point-in-time recovery (PITR) for operational recovery, plus an independent logical export before schema migrations and before any approved data migration. The staging owner must set and record:

1. Backup schedule and retention window, including WAL/PITR retention.
2. Recovery point and recovery time objectives, measured by a restore drill.
3. Which principals may create, read, and restore backups; keep restore authority separate from normal application runtime access.
4. Encryption, retention, deletion, and access logging for any Cloud Storage export. Do not make backup objects public.
5. The monthly cost allowance for backup storage, retained WAL, and temporary recovery resources.

Cloud SQL PITR restores to a new instance. Cloud SQL backup restoration can overwrite the target instance's data, so the approved first drill must use a new isolated target. Read Google's current [restore overview](https://docs.cloud.google.com/sql/docs/postgres/backup-recovery/restore), [PITR procedure](https://docs.cloud.google.com/sql/docs/postgres/backup-recovery/pitr), and [backup restore procedure](https://docs.cloud.google.com/sql/docs/postgres/backup-recovery/restoring) before operating a real instance.

## Logical backup procedure

Use a PostgreSQL client version supported by the source server. Authenticate through a protected `.pgpass` file (mode `0600`), a managed identity, or the approved Cloud SQL Auth Proxy/connector path. Do not put passwords in command arguments, shell history, logs, or this repository. The following commands use libpq environment variables for target selection; credentials must already be provided by the approved secure mechanism.

```bash
set -eu
umask 077

# Set these from the reviewed target record; never copy production values into source control.
export PGHOST='<approved-local-socket-or-proxy-host>'
export PGPORT='<approved-port>'
export PGDATABASE='<approved-database>'
export PGUSER='<approved-backup-role>'

backup_dir='<encrypted-restricted-backup-directory>'
install -d -m 700 "$backup_dir"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive="$backup_dir/${PGDATABASE}-${stamp}.dump"

pg_dump --format=custom --no-owner --no-privileges --file="$archive" "$PGDATABASE"
pg_restore --list "$archive" >/dev/null
shasum -a 256 "$archive" > "$archive.sha256"
```

Protect the archive and checksum with the approved storage encryption and retention controls. Record the source database identity, server version, backup time, archive size, checksum, migration IDs, and operator in the restricted operational record. A logical dump does not capture cluster-wide roles, service identities, instance flags, network policy, or secret values; provision those separately from reviewed infrastructure and access records.

For Cloud SQL, use the approved managed backup/PITR controls for instance recovery. For a logical export, follow Google's current [Cloud SQL PostgreSQL import/export guidance](https://docs.cloud.google.com/sql/docs/postgres/import-export/import-export-dmp), which specifies Cloud SQL-compatible dump options and IAM requirements. Do not assume a local `pg_dump` invocation is automatically interchangeable with every Cloud SQL import path.

## Restore into an isolated target

Never use `--clean` or restore directly over the active database for a drill. Create a fresh target database or instance, ensure the required database roles exist through the approved identity process, then restore:

```bash
set -eu
umask 077

# Point libpq at the isolated recovery target and use a restore-capable identity.
export PGHOST='<isolated-recovery-socket-or-proxy-host>'
export PGPORT='<isolated-recovery-port>'
export PGDATABASE='<maintenance-database>'
export PGUSER='<approved-restore-role>'

restore_db='<new-empty-recovery-database>'
archive='<verified-backup-archive>'
shasum -a 256 -c "$archive.sha256"
pg_restore --list "$archive" >/dev/null
createdb --no-password "$restore_db"
pg_restore --exit-on-error --single-transaction --no-owner --no-privileges \
  --dbname="$restore_db" "$archive"
```

If a Cloud SQL PITR restore is required, create the recovery instance through the reviewed Cloud SQL restore procedure and do not attach production traffic during validation. PITR always creates a new instance. If using a managed backup restore, confirm the selected target and its data-overwrite behavior before approving the operation.

## Verification and recovery decision

Run every check against the restored target, not the source:

1. Verify the archive checksum and that `pg_restore --list` succeeds.
2. Run `backend/scripts/migrate_postgres.py --check` with `AIXION_POSTGRES_DSN` pointed to the restored database. Require `READY`, no missing/unknown/mismatched migrations, and the expected migration IDs.
3. Verify expected table, row, and foreign-key counts against the restricted backup record. Check representative workspace/project/approval/audit data without printing sensitive payloads.
4. Run the backend repository and recovery integration suite using only synthetic or approved staging data.
5. Verify application health, authentication, workspace isolation, approval lookup/decision state, outbox recovery, and audit continuity before any traffic change.
6. Record restore start/end times, recovery point, data checks, observed RPO/RTO, warnings, operator, and go/no-go decision. Keep the restored target isolated until the recovery owner signs off.

The current application still has split SQLite/PostgreSQL route and worker authority. A PostgreSQL restore drill does not authorize production use until Slice 1 and the local failure-certification gates pass.

## Migration and rollback procedure

Before a reviewed schema migration, create and verify an independent backup, record the current immutable application image and migration ledger, and choose a maintenance window if the reviewed DDL may block active work. Apply migrations only with `backend/scripts/migrate_postgres.py --apply`, exact `--expected-database` and `--expected-schema`, and the required remote confirmation/TLS safeguards. The API never migrates automatically at startup.

If application rollout fails, stop or drain writers, preserve logs and the migration ledger, then either roll forward with a reviewed repair migration or restore the verified backup to a separate target and validate it. Never overwrite the only source database as the first recovery step. Re-enable traffic only after the responsible operator approves the restored state.

## Local drill record — 2026-10-21

- Target: isolated local PostgreSQL 17.11 server at `/tmp/aixion-ct-pg.O8jwnb/socket:55432`; no cloud account, API, billing, IAM, or resource was accessed.
- Created fresh source database `aixion_backup_drill_20261021` and applied the nine checked-in PostgreSQL migrations with the guarded migration CLI. Result: `READY`.
- Added only synthetic workspace `drill_workspace` and project `drill_project`; made the custom-format archive `aixion_backup_drill_with_fixture.dump`, wrote and checked its SHA-256 sidecar, and restored it with the documented `pg_restore --exit-on-error --single-transaction` command into the fresh database `aixion_restore_command_verify_20261021`.
- Restored target check: migration CLI returned `READY`; query verified 9 migration records, the synthetic workspace, the synthetic project scoped to that workspace, and the project foreign key. `pg_restore --list` and `shasum -a 256 -c` succeeded.
- Evidence artifacts are under `/tmp/aixion-control-tower-backup-drill-20261021/`. This is a local schema/data fixture drill only. It does not verify Cloud SQL backup scheduling, PITR, IAM, encryption-at-rest configuration, retention, restore time, or recovery of production application traffic.

## Configuration and tests

- Production metrics scraping uses `AIXION_METRICS_TOKEN` and `AIXION_METRICS_WORKSPACE_ID` in the protected API environment file. Generate a high-entropy token of at least 32 characters and bind it to one account workspace. Put the identical token in the protected `secrets/aixion_metrics_token` file mounted read-only into Prometheus. Do not pass it to the scheduler or Grafana. Rotate both copies together; a mismatch causes Prometheus scrapes to fail closed. Runtime readiness reports configured booleans only.
- The scheduler receives only `AIXION_API_BASE_URL` and its `AIXION_OWNER_TOKEN`. Grafana receives only `GF_*` settings. The CI Compose contract renders the configuration and checks these environment boundaries without printing interpolated secret values.
- No database schema migration is introduced by this runbook or the metrics scrape authentication configuration.
- The local drill used PostgreSQL 17.11 `pg_dump`/`pg_restore` and `backend/scripts/migrate_postgres.py`; it is reproducible only on the isolated local test service described above.
- Cloud SQL restore steps remain untested until a staging project, billing, resources, and a cost ceiling receive separate explicit authorization.
