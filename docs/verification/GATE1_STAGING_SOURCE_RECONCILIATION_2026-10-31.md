# Gate 1 Staging Source Reconciliation — 2026-10-31

## Result

**NOT RECONCILED — DO NOT USE THE EXISTING REHEARSAL DATABASE AS CUTOVER EVIDENCE.**

The owner-identified SQLite source is still open by its API process. The available
source copies are explicitly classified as online-analysis-only or non-quiesced.
The populated local PostgreSQL rehearsal database records a different source hash
from either available copy. Counts alone do not establish that its rows came from
the current authoritative source.

No source database, PostgreSQL row, schema, cloud resource, DNS record, or billing
setting was changed during this check.

## Evidence inspected

| Evidence | Observed state |
| --- | --- |
| Authoritative source | `backend.sqlite3`, last modified 2026-10-30 03:41:47 IST; API PID `38201` is still running with writable file descriptors. |
| Internal candidate `candidate.sqlite3` | SQLite integrity check is `ok`; SHA-256 `294003ad65846e10508f5ece13911dad65ad50e5f90bf0e1964508f05211aff2`; inventory and invocation-drain reports say `read_only=true`, `migration_authorized=false`, and `READY_FOR_OWNER_REVIEW`. |
| Candidate manifest on external volume | `writer_quiesced=false`; says the source API had writable handles, owner identity was not certified, and the candidate cannot authorize import or cutover. |
| 2026-10-31 online-analysis copy | Manifest classification is `NON_AUTHORITATIVE_ONLINE_ANALYSIS_ONLY_NOT_APPROVED_FOR_IMPORT_OR_CUTOVER`; `source_writers_quiesced=false`, writable handle count is unknown, snapshot is uncertified, and storage is exFAT where POSIX owner-only modes are unreliable. Snapshot SHA-256: `71276b0f61c8e65286baecc79b2038febd63ef7927b3e30117cde38eedf9352`. |
| Local PostgreSQL 17.11 rehearsal database | `aixion_gate1_rehearsal_20261030_staging` exists and has 40 migrations and one import-run record. Its recorded source SHA-256 is `2dd8f2e50250f2afa09160aab81d034b7bece0e575fb9be9bff604339561ba2e`, which matches neither inspected snapshot. |
| Rehearsal table counts | `users=3`, `workspaces=3`, `approval_requests=0`, `audit_events=2`, `legacy_audit_events=2`; there are two unscoped legacy audit rows and zero unscoped rows in `audit_events`. |
| Import record counts | Source counts record four audit events and two legacy audit events; imported counts record two audit events and two legacy audit events. This is consistent with the agreed quarantine disposition, but does not prove source identity or whole-app cutover. |

All PostgreSQL inspection queries ran inside read-only transactions and were rolled
back. The source SQLite file was not opened or hashed during this check because its
API writer remains active.

## Required next step

The owner must schedule a controlled API writer freeze and keep it stopped for the
snapshot window. Then create a new source-bound SQLite backup and manifest on a
filesystem with reliable owner-only permissions (not the exFAT volume), run the
source audit/invocation-drain checks against that exact snapshot, reconcile all
counts and the two unscoped `agent.auth_failed` events, and perform a fresh staging
import into a separately empty approved database. Do not overwrite or drop the
existing populated rehearsal database. Resume the API only after the snapshot is
verified and the owner accepts the freeze window.

This remains a staging rehearsal gate. Production cutover additionally requires
whole-application PostgreSQL authority, migration/rollback validation, and
backup/restore evidence. Production startup must remain fail-closed.
