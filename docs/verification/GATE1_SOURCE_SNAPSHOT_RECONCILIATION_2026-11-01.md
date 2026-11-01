# Gate 1 source snapshot reconciliation — 2026-11-01

## Result

The current authoritative SQLite source was captured into an owner-only APFS
snapshot and passes the importer’s read-only dry run. Its SHA-256 matches the
source hash recorded in the retained staging PostgreSQL backup’s
`sqlite_import_runs` row.

This corrects one statement in
`GATE1_STAGING_SOURCE_RECONCILIATION_2026-10-31.md`: that row’s source hash
does match the new APFS snapshot. It does not reconcile the complete
application store or close Gate 1.

## Evidence

- Owner-identified SQLite source:
  `/Users/madhuram/Library/Application Support/AixionControlTower/antigravity-native-m1-20261029/backend.sqlite3`
- Source SHA-256 at snapshot creation and recheck:
  `17f49ae6989393e12fc9d8d88789f492a1bda20cb5ab90bc7552056d3f072491`
- No `lsof` database handles were reported at the current recheck
  (`lsof` exit 1); the prior API process recorded in the October 31 note is
  absent. No SQLite `-wal` or `-shm` sidecars were present at snapshot capture.
- APFS snapshot:
  `/Users/madhuram/Library/Application Support/AixionControlTower/gate1-source-snapshot-20261101T054111Z/backend.sqlite3`
- Snapshot SHA-256:
  `2dd8f2e50250f2afa09160aab81d034b7bece0e575fb9be9bff604339561ba2e`
- Snapshot size: 110,592 bytes; file mode `0600`; containing snapshot directory
  mode `0700`; manifest file mode `0600`.
- Manifest binds the source and snapshot paths, source hashes and stat values
  before and after capture, snapshot hash and size, `writer_quiesced: true`,
  zero writable database handles at capture end, and SQLite integrity `ok`.
- Current source hash still matches the manifest’s post-capture source hash;
  snapshot hash still matches the manifest and snapshot integrity is `ok`.
- Snapshot helper tests:
  `python3 -m pytest -q backend/tests/test_create_quiesced_sqlite_snapshot.py`
  — **5 passed**.
- Importer dry run against this exact APFS snapshot:
  `PYTHONPATH=backend python3 backend/scripts/import_approval_slice_to_postgres.py --source '<APFS snapshot path>'`
  — `DRY_RUN_VALIDATED`, `STRUCTURAL_REVIEW_PASSED`,
  `invocation_drain.status=CLEAR`, `omitted_counts={}`, `partial=false`.
  The report retains `migration_authorized=false` and
  `snapshot_certified=false`.
- Import plan reports 3 users, 3 workspaces, 1 project, 5 sessions, 1 external
  agent and credential, 17 auth rate-limit buckets, 2 planned `audit_event`
  rows, and 2 planned `legacy_audit_event` rows.
- The retained staging archive at
  `/Users/madhuram/Library/Application Support/AixionControlTower/gate1-recovery-20261031/staging.dump`
  contains an import record with source SHA-256
  `2dd8f2e50250f2afa09160aab81d034b7bece0e575fb9be9bff604339561ba2e`,
  matching this snapshot. Its recorded source counts include 4 audit events
  and 2 legacy audit events; imported counts include 2 audit events and 2
  legacy audit events. The archive’s prior restore rehearsal is documented in
  `GATE1_STAGING_RESTORE_REHEARSAL_2026-10-31.md`.
- The local PostgreSQL rehearsal endpoint at
  `/private/tmp/aixion-codex-native-e2e-20261029/socket`, port `59174`, is
  currently not responding. No PostgreSQL target was changed in this
  verification.

The owner-approved disposition for the two unscoped unknown-agent
authentication failures remains quarantine without workspace linkage or a
tenant-facing read path. This note does not claim a fresh import applied to a
running staging database.

## Storage distinction

A duplicate snapshot on the external TradeBot volume was retained as requested,
but that volume is exFAT and is excluded as an import-authority artifact because
its POSIX permission semantics are not reliable. The APFS copy and its manifest
are the source-bound artifacts used above. No files or databases were deleted.

## Remaining Gate 1 requirements

- Reconcile the exact source records and quarantine rows against an available
  staging PostgreSQL target; do not overwrite the prior rehearsal database.
- Complete supported data import and establish PostgreSQL authority across the
  full application, including routes, workers, schedulers, and process-local
  state.
- Verify migrations, restart/isolation behavior, backup retention, and recovery
  against the intended production runtime.
- Keep the production startup guard closed until those requirements pass.

No importer apply, PostgreSQL write, schema/config change, production setting,
cloud resource, billing, DNS, or deployment action occurred.
