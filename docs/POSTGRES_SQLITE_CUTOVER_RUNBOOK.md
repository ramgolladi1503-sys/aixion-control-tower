# SQLite to PostgreSQL Cutover Runbook (Slice 1)

**Status: staging/test procedure only.** This runbook documents a controlled rehearsal with the current partial importer. It is **not** authorization to migrate production, switch production traffic, enable cloud billing, or declare Slice 1 complete. The importer explicitly requires `AIXION_TEST_POSTGRES_DSN`, rejects database names that do not end in `_test` or `_staging`, and rejects remote targets unless `--allow-remote-staging` is supplied. The production startup guard must remain enabled until the Slice 1 exit gate in `PRE_BILLING_WORK_LOCK.md` is met.

## 1. Scope and limits

The supported importer is `backend/scripts/import_approval_slice_to_postgres.py`. Its current scope identifier is `approval_auth_agent_mcp_test_runs_connectors_email_task_notifications_delivery_rate_limits_invocation_leases_faults_legacy_audit_global_auth_quarantine_v15`. It imports a bounded set of records from the legacy SQLite `kv_store` into an otherwise empty PostgreSQL schema. It is deliberately partial: nonzero omitted entity counts require explicit acknowledgement, and even a clean staging import does not make PostgreSQL authoritative for every route, worker, scheduler, or process-local state path.

The script applies the checked-in PostgreSQL migrations as part of `--apply`, then writes the supported import in a transaction. Inactive or exhausted AgentRun fault settings are imported only when the exact parent AgentRun is present and supplies the matching workspace; active fault settings block the plan. It also fails closed for unsupported data, nonempty application tables, broken scope/parent relationships, invalid payloads, native invocation approvals that have not drained, a source snapshot that changes during inspection, or missing/mismatched source-manifest assertions. The exact `agent.auth_failed` event for an unknown agent with no workspace is retained in `legacy_audit_events` with `workspace_id NULL` and reason `UNSCOPED_UNKNOWN_AGENT_AUTH_FAILURE`; it never enters tenant-scoped audit reads. This is a staging data-preservation disposition, not proof that the event belongs to an account. All other unscoped events continue to block. The manifest validation binds an operator-recorded freeze and source identity to the exact snapshot bytes; it cannot independently prove the operator's assertions, so independent review and the explicit `--confirm-source-snapshot` acknowledgement remain required.

The runbook does not authorize a production migration. Do not point production traffic at this imported target. Do not remove or weaken the production startup refusal.

## 2. Roles and prerequisites

Assign an operator and an independent reviewer. Record the rehearsal change/ticket, exact application Git SHA, operator, reviewer, start time, source owner, target database/schema, and planned stop window. The reviewer must inspect the evidence before any staging cutover decision.

Before starting, confirm all of the following:

- This is a disposable local test database or an explicitly designated staging database whose name ends in `_test` or `_staging`; it contains no production or irreplaceable data.
- The target is dedicated to this rehearsal. The importer requires every application table to be empty before the first import (migration metadata and a pristine trust-chain head are the only exceptions). Do not mix a partial import into an existing target.
- PostgreSQL credentials are supplied through the environment using the existing `AIXION_TEST_POSTGRES_DSN`; do not put credentials in command arguments, reports, tickets, or chat. For a remote staging target, use verified TLS (`sslmode=verify-full`) and obtain the separately required authorization before using `--allow-remote-staging`.
- The source SQLite database and reports are owner-only. The importer requires an existing regular, non-symlink source file owned by the operator and mode `0600` or stricter. Keep source and evidence in access-controlled storage.
- For `--apply`, place an owner-only `<source-filename>.manifest.json` beside the source (for example, `backend.sqlite3.manifest.json`). It must bind the exact resolved snapshot path, byte size, and SHA-256; record `snapshot_integrity_check: "ok"`, `writer_quiesced: true`, `source_writable_database_handles_at_capture_end: 0`, `owner_source_identity_confirmed: true`, an absolute `source_path`, and a nonempty `capture_method`. The importer checks the manifest ownership/mode, size, and binding; it also independently runs SQLite `PRAGMA integrity_check` read-only before opening PostgreSQL. This manifest is an operator attestation, not independent proof of process quiescence.
- The selected application revision, Python environment/dependencies, target PostgreSQL version, and migration inventory are recorded. No production API/worker is pointed at the target.
- A tested, independent recovery copy of the source and a plan to preserve the target/evidence exist. A filesystem copy of only the main SQLite file while WAL is active is not a valid backup procedure.

If any prerequisite is unknown, stop. Do not treat a successful script exit as approval to proceed.

## 3. Freeze writers and create a stable source snapshot

1. Announce a bounded write freeze for the source application. Stop or pause **all** API instances, workers, schedulers, background dispatchers, and administrative scripts that can write the SQLite store. Record process/service identities and confirm no writer remains. Do not kill an unidentified process; use the application's normal shutdown and verify quiescence.
2. Drain and stop new work at the source. Wait for active database transactions and queued writes to finish. Preserve the original source and its independent backup unchanged.
3. Create a coordinated SQLite snapshot using SQLite's backup API (or the application's documented backup mechanism) while holding the write freeze. Do not copy the main `.db` file alone while WAL mode is active. If using a checkpoint procedure, do it only after all writers are stopped, verify its result, then snapshot. Keep any `-wal`/`-shm` files with the live source as appropriate; the importer refuses a snapshot path when matching WAL/SHM sidecars exist. Do not delete sidecars to make that check pass.
4. Place the standalone snapshot in a private directory, set owner-only permissions (`0600` file; private owner-only directory), and record its path, byte size, SHA-256, owner, source freeze interval, SQLite integrity/backup evidence, and the exact procedure used. Ensure the snapshot is a regular file, not a symlink. The report itself contains sensitive metadata; protect it accordingly.
5. Keep source writers frozen until drain and snapshot validation are complete. If any writer resumes or the snapshot procedure is uncertain, discard the candidate snapshot and restart this section.

## 4. Drain native approval invocations

Native Antigravity/Codex approval state is invocation-bound. A persisted approval timestamp or connector record is not proof that the original invocation remains paused, and an operator must never manufacture or extend an invocation lease to make the import pass.

1. On a quiesced snapshot, produce the read-only drain report (choose new, non-existing private report paths for every run):

   ```sh
   python backend/scripts/import_approval_slice_to_postgres.py \
     --source /secure/path/control-tower-snapshot.sqlite \
     --write-invocation-drain-report /secure/path/drain-report.json
   ```

   This mode cannot be combined with import/apply options. It returns `READY_FOR_OWNER_REVIEW` only when no native invocation is pending, approved-but-unconsumed, or unverifiable under the report's lease/identity checks. `BLOCKED` (or a nonzero exit) means stop: let real invocations complete or expire through the supported application lifecycle, create a new coordinated snapshot, and regenerate the report. Never hand-edit the report or database to clear a blocker.
2. Have the independent reviewer confirm the report's `source_snapshot_sha256` matches the frozen snapshot and review all status counts. Keep approval identifiers redacted as provided by the report; do not attempt to resolve hashes by exposing request payloads.
3. A `READY_FOR_OWNER_REVIEW` report is evidence of the drain criteria observed in that snapshot, not proof of a live native provider session or a production cutover approval. Continue to keep source writers stopped for the rehearsal snapshot.

## 5. Read-only source audit and importer dry run

Run the structural audit against the same frozen snapshot and create a new report file (the audit refuses to overwrite it):

```sh
python backend/scripts/audit_legacy_sqlite.py \
  --database /secure/path/control-tower-snapshot.sqlite \
  --output /secure/path/sqlite-audit.json
```

Review the complete JSON report. Require `STRUCTURAL_REVIEW_PASSED`, no unknown source tables, no invalid payloads, no unresolved ownership/parent conflicts other than the explicit ownerless unknown-agent authentication events counted for quarantine, and a valid trust-event chain. Verify that the importer plan places those exact records in the immutable legacy archive with `workspace_id` null and does not include them in `audit_events`. Any other finding that changes data coverage or ownership is an abort condition. The auditor is read-only and reports `migration_authorized: false`; it does not certify a coordinated backup or prove that all application state is in this database.

Run the importer without `--apply` to produce a dry-run plan:

```sh
python backend/scripts/import_approval_slice_to_postgres.py \
  --source /secure/path/control-tower-snapshot.sqlite
```

Review and retain the JSON output. Require `status` equal to `DRY_RUN_VALIDATED`, `migration_authorized: false`, `snapshot_certified: false`, `invocation_drain.status` equal to `CLEAR`, and the exact source hash to agree with the drain report. Reconcile `source_counts`, `planned_counts`, `omitted_counts`, and `partial`; verify every ownerless unknown-agent auth failure is counted under `legacy_audit_event`, has the explicit quarantine reason, and has no tenant scope. Identify every other omitted entity and its business impact with the reviewer. Unsupported notifications, malformed payloads, any other unresolved scope, or nonzero omissions that have not been explicitly accepted for this *staging rehearsal* block apply. Do not use `--allow-partial-slice` to conceal or waive a production completeness gap.

The importer rehashes the source and compares file identity/size/mtime before and after inspection. Any source mutation invalidates the plan. Recreate the snapshot and rerun the audit, drain report, and dry run; do not reuse stale evidence.

## 6. Apply to disposable test/staging PostgreSQL only

This procedure is allowed only after the preceding review, using a fresh empty disposable target. It is not a production procedure.

1. Set `AIXION_TEST_POSTGRES_DSN` through the approved secret/environment mechanism. Confirm the target database name ends in `_test` or `_staging`, and independently query/record the connected database and schema. Never print the DSN. The importer refuses other database suffixes and checks the connected target's emptiness under table locks.
2. Obtain reviewer sign-off on the exact source SHA-256, dry-run counts/omissions, target identity, application SHA, and planned partial-slice acknowledgement. If the plan reports omissions, a staging-only rehearsal requires the reviewer to record why they are acceptable for this rehearsal. This does not accept those omissions for production.
3. For loopback/local disposable PostgreSQL, invoke:

   ```sh
   python backend/scripts/import_approval_slice_to_postgres.py \
     --source /secure/path/control-tower-snapshot.sqlite \
     --apply \
     --confirm-source-snapshot \
     --allow-partial-slice
   ```

   `--confirm-source-snapshot` is an operator assertion that the coordinated snapshot procedure above was completed; it does not itself create or certify a snapshot. Apply additionally requires the manifest contract in Section 2 and verifies it against the exact snapshot. `--allow-partial-slice` is only the importer’s required acknowledgement for a known partial staging import.
4. For a remote **staging** target, use the same command with `--allow-remote-staging` only after remote-target authorization, verified TLS (`sslmode=verify-full`), and target identity confirmation. Never use this option for production. The code also rejects unsafe target names and requires the `AIXION_TEST_POSTGRES_DSN` environment variable.
5. The importer applies PostgreSQL schema migrations before importing records, then performs the data import transaction and validates counts/payloads before recording the source hash/scope in `sqlite_import_runs`. A failed run can therefore leave the target schema migrated even when the data transaction rolls back. Capture the full redacted command output and exit status; do not retry against a different target without redoing target confirmation and preflight.
6. A successful apply reports `STAGING_IMPORT_APPLIED`. That label means only that this supported partial import was applied to the test/staging target; it is not a production-ready signal.

Do not invent importer flags. The command above reflects the current parser in `backend/scripts/import_approval_slice_to_postgres.py`. For a future revision, re-check `python backend/scripts/import_approval_slice_to_postgres.py --help` and source before using this runbook.

## 7. Reconcile and retain evidence

The operator and reviewer must compare the following as one evidence packet, all bound to the same source SHA-256 and application Git SHA:

- Source snapshot manifest: creation procedure, freeze window, file size, SHA-256, permissions/ownership, integrity/backup evidence, and proof of no active SQLite writer.
- Read-only structural audit JSON and review disposition.
- Native invocation drain report JSON, including source SHA and ready/blocked state.
- Importer dry-run JSON and apply JSON/exit status; `source_counts`, `planned_counts`, `omitted_counts`, `partial`, `invocation_drain`, `audit_status`, and source hash must agree. Record any accepted omission explicitly as staging-only.
- PostgreSQL identity and migration evidence. Use the current schema checker against the same target, with `AIXION_POSTGRES_DSN` set securely to the same DSN:

  ```sh
  python backend/scripts/migrate_postgres.py --check
  ```

  Require `status` = `READY`, the expected database/schema, no missing/unknown/mismatched migration IDs, and the expected applied migration list. `--check` is read-only. Do not run `--apply` separately for this importer flow: the importer applies migrations itself. Do not infer the database identity solely from the DSN text.
- Import manifest row in `sqlite_import_runs` for the exact source SHA and importer scope, plus row counts/payload reconciliation reported by the importer. Retain a read-only query/export showing the manifest and expected target counts, with workspace/parent consistency checks. Protect all exports as sensitive data.
- Reviewer sign-off listing residual gaps, aborts, and whether the rehearsal is accepted. Record pass/fail per checklist item; do not convert unknown results into pass.

Any difference in source hash, count, scope, target identity, migration ledger, payload comparison, workspace boundary, quarantine reason, or parent linkage is a failed reconciliation. Stop application use of that staging target and investigate; do not patch rows manually and proceed.

## 8. Cutover decision gate and rollback boundary

### Staging rehearsal decision

The reviewer may accept the *staging rehearsal* only when every prerequisite and reconciliation item passes, the target is still isolated from production traffic, and the exact omissions and limits are understood. Keep production startup refusal enabled. This decision does not close Slice 1.

### Production cutover decision

**No production cutover is permitted under this runbook or with the current partial importer.** Slice 1 remains blocked until the locked exit gate is proven: complete application-wide PostgreSQL authority with no split SQLite/process-local persistence; supported empty and upgrade migrations; full backend suite against PostgreSQL; multi-process concurrency, rollback, restart, and workspace-isolation checks; and proof that SQLite cannot start the production profile. A future production migration requires a separate approved plan, complete importer/restore strategy, rehearsed recovery, owner authorization, and deployment-specific change approval.

### Rollback boundary

- **Before the first PostgreSQL write:** aborting means do not apply migrations or import data; leave the target untouched and restore normal source operation only after the source has been verified healthy. Keep source freeze as short as operationally safe.
- **After the first PostgreSQL write (including schema migrations):** do not treat switching an application pointer back to SQLite as rollback. The target may contain a migration ledger or imported state, and it is not a complete authoritative replacement. For a disposable rehearsal target, isolate it, preserve logs/evidence, and have the database owner discard/recreate that target through the approved staging procedure before a fresh rehearsal. Never drop or overwrite an uncertain/shared target. For any non-disposable/production target, stop and invoke the separately approved database recovery/incident procedure; this runbook provides no destructive recovery command.
- **After an application has accepted writes on PostgreSQL:** a source rollback is unsafe without a tested reverse/restore strategy and reconciliation of writes made after cutover. That strategy is not established here. Stop writes and use the separately approved recovery plan; do not silently point clients back at the stale SQLite source.

## 9. Abort conditions

Abort before apply if any of these occur:

- source writer/process state is unknown, a write resumes, or the snapshot procedure/WAL handling is not evidenced;
- snapshot has a WAL/SHM sidecar, is mutable during planning, is a symlink, has unsafe ownership/permissions, or its recorded hash differs between artifacts;
- drain status is `BLOCKED`, a pending/approved-unconsumed/unverifiable native invocation remains, or lease identity/currentness is uncertain;
- audit is not `STRUCTURAL_REVIEW_PASSED`, trust-chain verification fails, or source has unknown tables, malformed payloads, unresolved ownership/identity/parent findings other than the explicit unknown-agent auth quarantine, or unsupported notification types;
- importer does not return `DRY_RUN_VALIDATED`, omissions are unexplained, or scope/target compatibility is unclear;
- target is not disposable and empty, its database/schema identity cannot be confirmed, environment/credentials are ambiguous, remote TLS is not verified, or the target name does not satisfy importer guards;
- reviewer is unavailable, evidence is incomplete, or the application revision changes after evidence was generated.

Abort after apply if exit status is nonzero, result is not `STAGING_IMPORT_APPLIED`, migration check is not `READY`, counts/payloads/manifests differ, or any workspace/parent isolation check fails. Preserve the source and target evidence and follow the rollback boundary above. Do not manually edit imported rows to force a pass.

## 10. Operator checklist

- [ ] Rehearsal is explicitly test/staging; no production traffic or billing action is in scope.
- [ ] Operator/reviewer, app Git SHA, target database/schema, time window, and change record are documented.
- [ ] All source writers/workers are quiesced and verified; backup is independent and recoverable.
- [ ] SQLite snapshot is coordinated, stable, private, owner-only, and free of importer-blocking WAL/SHM sidecars; SHA-256 is recorded.
- [ ] Read-only invocation drain report is `READY_FOR_OWNER_REVIEW` for that exact source hash; no leases were synthesized.
- [ ] Structural audit is `STRUCTURAL_REVIEW_PASSED`; all source findings are reviewed and resolved.
- [ ] Importer dry run is `DRY_RUN_VALIDATED`; source/planned/omitted counts and partial scope are explicitly reconciled.
- [ ] Reviewer approved the exact source hash, current app revision, and disposable empty target identity.
- [ ] Apply used only the existing flags and `AIXION_TEST_POSTGRES_DSN`; successful status is `STAGING_IMPORT_APPLIED`.
- [ ] Migration check returns `READY`; manifest, counts, payloads, workspace isolation, and parent links reconcile.
- [ ] Source, reports, command outputs, target evidence, and reviewer disposition are retained under access control.
- [ ] Production startup guard remains enabled; no claim of production cutover or Slice 1 completion is made.

## 11. Current configuration keys and migration notes

This runbook adds **no application configuration keys**. It documents existing operational variables/guards: `AIXION_TEST_POSTGRES_DSN` for the partial importer, and `AIXION_POSTGRES_DSN` for the read-only migration checker. Keep credentials out of files and logs. A remote test/staging importer target requires the existing `--allow-remote-staging` flag and verified TLS. Schema migration `pg_0040_unscoped_legacy_auth_audit_quarantine` makes `legacy_audit_events.workspace_id` nullable and adds a constrained `quarantine_reason`; only the exact unknown-agent auth-failure reason may have null workspace. The target must be disposable and empty of application records. Existing production databases are outside this runbook's support.

## 12. Run instructions

From the repository root, first review this runbook and `docs/PRE_BILLING_WORK_LOCK.md`, confirm current importer behavior from its `--help` and source, then perform Sections 2–7 only for a disposable rehearsal. Keep command outputs and private JSON evidence. Do not use this document as authorization to enable billing, create cloud resources, change production configuration, or migrate production data.
