# Gate 1 source database quiescence check

Date: 2026-10-31
Purpose: determine whether a new staging source snapshot can be safely captured.

## Owner-confirmed staging source

The owner identified this SQLite source as the source for the staging rehearsal:

`/Users/madhuram/Library/Application Support/AixionControlTower/antigravity-native-m1-20261029/backend.sqlite3`

The existing candidate copy is at:

`/Volumes/TradeBot/AixionControlTower/slice1-source-snapshot-candidate-not-quiesced-20261029T095010Z/backend.sqlite3`

Its manifest records SHA-256
`294003ad65846e10508f5ece13911dad65ad50e5f90bf0e1964508f05211aff2`,
but also records `writer_quiesced: false`, 29 writable database handles at capture, and `owner_source_identity_confirmed: false` at that time. It is not suitable as a coordinated source snapshot.

## Current read-only source check

At the time of this check:

- The owner-confirmed source file existed, was mode `0600`, and was 110,592 bytes.
- No `-wal` or `-shm` sidecars were present.
- `lsof` showed a Python process, PID `38201`, with nine open writable handles (`3u` through `11u`) on the source database.

The open writable handles establish that the source is not quiesced. The absence of WAL/SHM sidecars does not override that finding.

## Action and gate status

No process was stopped, no source file was changed, and no new snapshot or import was attempted. The previously imported staging rehearsal must not be treated as a fresh capture of this currently active source.

Gate 1 remains open. A new source snapshot requires a coordinated maintenance window in which the owner-approved application/API writer is stopped through its normal shutdown procedure, all writer handles are confirmed closed, and a new owner-only SQLite backup is captured and hashed. The production startup guard remains enabled.

## Follow-up read-only check (2026-10-31 04:22 UTC)

The same source remains open and unmodified:

- `ps` identifies PID `38201` as `uvicorn app.main:app --host 127.0.0.1 --port 8003 --log-level info`.
- `lsof` reports 112 descriptors in read/write (`u`) mode for the source path. These are file descriptors, not a count of distinct SQLite connections; any read/write descriptor means quiescence has not been established.
- The source remains 110,592 bytes with no reported modification since Oct 30 03:41:47 local time. Current SHA-256: `17f49ae6989393e12fc9d8d88789f492a1bda20cb5ab90bc7552056d3f072491`.
- The current hash differs from the older non-quiesced candidate hash above. That candidate remains unsuitable and is not a substitute for a fresh coordinated snapshot.

No service was stopped, and no source, staging, or production database was written. Gate 1 remains open.

## Read-only source/staging reconciliation (2026-10-31)

The owner-selected SQLite source was opened with `mode=ro` and `PRAGMA query_only=ON`; no values or identifiers were read. SQLite `integrity_check` returned `ok`. The file SHA-256 is `17f49ae6989393e12fc9d8d88789f492a1bda20cb5ab90bc7552056d3f072491`; its `schema_migrations` table contains `0001_baseline_kv_store` through `0010_approval_expiry_due_index`, and `PRAGMA user_version` is `0`.

The source contains these key counts: `user=3`, `workspace=3`, `project=1`, `session=5`, `external_agent=1`, `external_agent_credential=1`, `audit_event=4`, `auth_rate_limit_bucket=17`, and zero rows for `auth_email_outbox`, `approval_delivery_claim`, `approval_delivery_failure`, and `approval_invocation_lease`. Its four audit events are stored in `kv_store`; the importer must split the two confirmed unscoped auth failures into the approved quarantine while retaining the two workspace-scoped events in `audit_events`.

The approved staging PostgreSQL database is available through the existing private Unix-socket cluster at `/private/tmp/aixion-codex-native-e2e-20261029/socket`, port `59174`; it is not listening on TCP/5432. Read-only inspection shows 40 migrations, `users=3`, `workspaces=3`, `projects=1`, `sessions=5`, `external_agents=1`, `external_agent_credentials=1`, `auth_rate_limit_buckets=17`, `audit_events=2`, and `legacy_audit_events=2`. Its `sqlite_import_runs` record is for source hash `2dd8f2e50250f2afa09160aab81d034b7bece0e575fb9be9bff604339561ba2e`, not the current source hash above. Counts match but source hashes differ, so this target is a prior rehearsal and is not reconciled to the current exact source. No PostgreSQL data was changed and the shared cluster was not stopped or cleaned.

The live SQLite API remains on `127.0.0.1:8003` under PID `38201` with writable database handles. Although it currently has no established TCP client, the writer is not quiesced. No snapshot, import, or cutover was attempted. A new exact-source rehearsal still requires an owner-coordinated writer-freeze window and a successful run of `backend/scripts/create_quiesced_sqlite_snapshot.py` against that stable source.
