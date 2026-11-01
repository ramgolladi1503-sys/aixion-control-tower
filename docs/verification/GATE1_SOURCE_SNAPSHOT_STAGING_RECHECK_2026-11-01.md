# Gate 1 source snapshot and staging recheck — 2026-11-01

## Result

The owner-identified SQLite source was captured while no source database
handles were open. The snapshot is stored on the external APFS volume with
owner-only permissions. The source and snapshot have matching logical SQLite
dump hashes. The snapshot SHA-256 matches the `sqlite_import_runs` source hash
in the existing local staging reconciliation database.

This refresh confirms source-to-staging snapshot identity and the existing
staging row counts. It does not establish full application PostgreSQL authority,
production cutover, or production backup/recovery readiness.

## Source and snapshot evidence

- Owner-confirmed source:
  `/Users/madhuram/Library/Application Support/AixionControlTower/antigravity-native-m1-20261029/backend.sqlite3`
- Source SHA-256 before and after capture:
  `17f49ae6989393e12fc9d8d88789f492a1bda20cb5ab90bc7552056d3f072491`
- Prior source API PID `38201` was absent. `lsof` reported no open source
  database handles before or after capture. No `-wal` or `-shm` sidecars were
  present.
- Logical SQLite dump SHA-256 for both source and snapshot:
  `894a4fe65fec7df2219d764a567ba95ea2865d2c408ec99249d4625d2508a215`
- External APFS snapshot:
  `/Volumes/AixionAndroidSDK/gate1-source-snapshot-20261101/backend.sqlite3`
- Snapshot SHA-256:
  `2dd8f2e50250f2afa09160aab81d034b7bece0e575fb9be9bff604339561ba2e`
- Snapshot size: 110,592 bytes; file mode `0600`; containing directory mode
  `0700`; adjacent manifest mode `0600`.
- Snapshot manifest SHA-256:
  `11aa24b1ea06f2bc1d408c6bfb4fd15260943e7a338a47a44209388159d65c0e`
- The snapshot helper verified SQLite integrity as `ok`.
- Importer dry run returned `DRY_RUN_VALIDATED`, scope v15,
  `partial=false`, `omitted_counts={}`, and `migration_authorized=false`.
  The report is owner-only at
  `/Volumes/AixionAndroidSDK/gate1-source-snapshot-20261101/import-dry-run.json`.
- A separate duplicate snapshot was retained at
  `/Volumes/TradeBot/gate1-source-snapshot-20261101/`. Its bytes match the
  APFS snapshot, but the TradeBot volume is exFAT; it is not used as an
  import-authority artifact. No files were deleted.

## Read-only staging reconciliation

The preserved PostgreSQL 17 scratch cluster at
`/private/tmp/aixion-gate1-reconcile-20261101-01/data` was restarted on its
private Unix socket, queried in a read-only transaction, rolled back, and
stopped cleanly. Its data directory remains present.

Database `aixion_gate1_reconcile_20261101_staging` reports:

- `sqlite_import_runs.source_sha256` matches the snapshot SHA-256 above.
- 40 schema migrations.
- 3 users, 3 workspaces, 1 project, 5 sessions.
- 1 external agent and 1 credential; 17 auth rate-limit buckets.
- 2 workspace-scoped audit events and 2 unscoped legacy audit records
  quarantined with `UNSCOPED_UNKNOWN_AGENT_AUTH_FAILURE`.
- 0 unscoped rows in tenant-facing `audit_events`.

The two quarantined records remain immutable and have no workspace linkage.
No production database, source data, cloud resource, billing setting, DNS
record, or deployment was changed.

## Remaining Gate 1 requirements

- Route every production-supported account, workspace, session, connector,
  approval, decision, audit, outbox, worker, scheduler, and recovery path
  through one PostgreSQL-authoritative runtime, or explicitly disable any
  unsupported production surface.
- Complete the production migration/cutover plan and keep the startup guard
  closed until app-wide authority is verified.
- Verify production backup retention, restore, recovery, and RTO/RPO.

Gate 1 remains open.
