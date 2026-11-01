# Gate 1 Local PostgreSQL Restore Rehearsal — 2026-11-01

## Result

**PASS: isolated local backup and restore rehearsal only.** This does not close Gate 1, establish production PostgreSQL authority, or certify Cloud SQL backup/PITR.

## Scope and source

- Source database: `aixion_gate1_reconcile_20261101_staging` in a local PostgreSQL 17.11 cluster on a private Unix socket, port 59175.
- The source was the already owner-approved staging reconciliation database. No production or cloud database was contacted or modified.
- The imported SQLite snapshot SHA-256 recorded by PostgreSQL was `2dd8f2e50250f2afa09160aab81d034b7bece0e575fb9be9bff604339561ba2e`.
- Archive: `/private/tmp/aixion-gate1-recovery-drill-20261101-01/source.dump` (224,732 bytes; mode `0600`). SHA-256: `c6a0ca9d0775fcb79537a9d4be8f942cfa557192777ce948dd83cbb01b83f522`.
- The archive and checksum are outside the repository on the FileVault-encrypted Mac Data volume. They contain sensitive application data; do not upload or share them. The directory is temporary and is not a durable backup destination.

## Restore and checks

- Restored with PostgreSQL 17.11 `pg_restore --exit-on-error --single-transaction --no-owner --no-privileges` into the new isolated database `aixion_gate1_restorecheck_20261101_b47ff09`. No existing database was overwritten.
- `pg_restore --list` succeeded and SHA-256 checksum verification returned `OK`.
- Repository migration check returned `READY`: all 40 expected migrations were present, with no missing, unknown, or mismatched migrations.
- Read-only comparison found identical row counts in all 45 public tables and identical `sqlite_import_runs` metadata in source and restore.
- The restored table counts include 3 users, 3 workspaces, 1 project, 5 sessions, 1 external agent, 1 external-agent credential, 17 auth rate-limit buckets, 2 tenant-scoped audit events, and 2 quarantined legacy audit events.
- The tenant-facing `audit_events` table contained zero rows with a null `workspace_id` in both databases. Both legacy audit rows remained in `legacy_audit_events`.

## Limits and follow-up

- This validates a local logical archive and isolated restore, not Cloud SQL scheduling, encryption/IAM, retention, PITR, measured RPO/RTO, or recovery in the deployment region.
- The staging database was already populated by the approved rehearsal. This does not make the importer a production cutover tool or remove the production startup guard.
- Keep both local databases and the restricted archive until an owner-approved durable backup destination and retention policy exist. The Mac temp directory may be cleared by system maintenance or restart.
- No configuration keys or schema changes were added. Reproduction used the existing `docs/POSTGRES_BACKUP_RESTORE_RUNBOOK.md`; no app service was restarted.
