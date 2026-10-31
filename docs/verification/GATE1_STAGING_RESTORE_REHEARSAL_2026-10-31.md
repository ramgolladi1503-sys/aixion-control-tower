# Gate 1 Staging Backup and Restore Rehearsal — 2026-10-31

## Scope

Read-only dump of the owner-approved local staging rehearsal database, followed
by a restore into a newly initialized, isolated PostgreSQL 17.11 test cluster.
The source SQLite database, live API, staging database, production database,
cloud resources, billing settings, and DNS were not modified.

## Backup artifact

- Source database: `aixion_gate1_rehearsal_20261030_staging`
- Source PostgreSQL cluster: local Unix socket
  `/tmp/aixion-codex-native-e2e-20261029/socket`, port `59174`
- Format: PostgreSQL custom archive, created with `pg_dump --format=custom
  --no-owner --no-acl`
- Size: 225,466 bytes
- SHA-256: `5ac9264fa9300baf5b22c9e8c2c4d9ec87917ae4e40151dc1098dfeb54063798`
- Retained artifact:
  `/Users/madhuram/Library/Application Support/AixionControlTower/gate1-recovery-20261031/staging.dump`
- File mode: `0600`; containing directory mode: `0700`

The dump archive list was readable with `pg_restore --list`.

## Restore and reconciliation

The archive restored with `pg_restore --exit-on-error --no-owner --no-acl`
into a fresh PostgreSQL 17.11 test cluster. The restore cluster was stopped
after verification and remains under the temporary directory
`/private/tmp/aixion-gate1-restore-20261031-01/`.

Source and restored database rows were compared for every public base table.
For each table, the number of rows and SHA-256 of sorted canonical
`to_jsonb(row)::text` values matched:

- Tables compared: 45
- Rows compared: 77
- Nonempty tables: 12
- `schema_migrations`: 40 rows
- `audit_events`: 2 rows
- `legacy_audit_events`: 2 rows
- `sqlite_import_runs`: 1 row

Result: staging archive restore and table-data reconciliation passed.

## Caveats and remaining Gate 1 work

A read-only `pg_database_size()` query against the source test cluster failed
because its `pg_tblspc` directory is absent. Ordinary reads, `pg_dump`, and the
fresh-cluster restore succeeded, but the source test cluster's filesystem
health is not established by this rehearsal.

This validates one local staging archive restore only. It does not prove
production backup retention, point-in-time recovery, disaster recovery,
application-wide PostgreSQL authority, import of a fresh quiesced SQLite
source, or production cutover readiness. The production startup guard remains
enabled. The active SQLite API was not stopped; a fresh authoritative source
snapshot still requires its coordinated maintenance window.
