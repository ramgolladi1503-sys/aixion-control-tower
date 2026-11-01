# Gate 1 Exact SQLite Source Reconciliation — 2026-11-01

## Result

**PASS: current owner-selected source snapshot and existing isolated staging import reconcile.** This closes the historical source-hash mismatch for the staging rehearsal only. It does not close Gate 1 or authorize a production cutover.

## Snapshot evidence

- Owner-selected source: `/Users/madhuram/Library/Application Support/AixionControlTower/antigravity-native-m1-20261029/backend.sqlite3`.
- Source SHA-256 immediately before and after snapshot: `17f49ae6989393e12fc9d8d88789f492a1bda20cb5ab90bc7552056d3f072491`.
- The former Uvicorn PID `38201` was absent. No process held the source file open, no API listener was present on port 8003, and no `-wal` or `-shm` sidecars existed. The MROS/TradeBot processes were not stopped or changed.
- The guarded helper created `/private/tmp/aixion-gate1-exact-source-snapshot-20261101-01/backend.sqlite3` with an owner-only manifest. The snapshot is 110,592 bytes, passes SQLite integrity validation, and has SHA-256 `2dd8f2e50250f2afa09160aab81d034b7bece0e575fb9be9bff604339561ba2e`.
- The manifest records the source identity acknowledgement, zero writable database handles at capture end, matching source identity/stat/hash before and after, `writer_quiesced: true`, and `snapshot_integrity_check: "ok"`. Snapshot status is `SNAPSHOT_CREATED_FOR_OWNER_REVIEW`; the helper set `migration_authorized: false`.

## Current importer dry run and staging comparison

- Current importer dry run returned `DRY_RUN_VALIDATED`, `STRUCTURAL_REVIEW_PASSED`, and invocation drain `CLEAR`.
- The plan reports no omitted entity counts and `partial: false` for this source dataset. It reports 4 source audit events, of which 2 are workspace-scoped and 2 are the approved unscoped legacy events retained in quarantine.
- The snapshot SHA-256 exactly matches the existing isolated PostgreSQL staging database’s `sqlite_import_runs.source_sha256`.
- In a read-only transaction, the staging import record matched the current dry-run source counts, planned/imported counts, importer scope, and source hash. Actual target counts matched all 37 planned entity counts.
- This explains the earlier apparent mismatch: `17f49…` was the raw source file hash; `2dd8…` is the byte hash of the SQLite backup-API snapshot consumed by the importer. The fresh helper manifest now binds both values to the exact source and snapshot.
- The two unscoped auth-failure events remain in `legacy_audit_events`; the tenant-facing audit table has zero unscoped rows. No user or event payloads are reproduced here.

## Limits and remaining Gate 1 work

- No new import, schema migration, or production database write was performed in this check; the existing staging target already contains the matching import.
- The importer is still a bounded migration path. A complete proof that all API routes, workers, scheduled work, and process-local state use PostgreSQL is still required, along with production cutover authorization and deployment-specific rollback planning.
- Cloud SQL backup/PITR configuration, retention, IAM, regional restore, and measured RPO/RTO remain unverified.
- The snapshot, manifest, restore archive, and restore evidence are under `/private/tmp` on the FileVault-encrypted Mac Data volume. They contain sensitive data, are not committed, and are not a durable backup location. Do not upload or share raw artifacts.
- No configuration keys or schema migrations were added. The local PostgreSQL cluster was stopped after the read-only comparison; the source and all external-disk contents were left unchanged.
