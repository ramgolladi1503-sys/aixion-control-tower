# Gate 1 PostgreSQL Restore Rehearsal

**Date:** 2026-10-30  
**Code revision:** `57c0b89ef146091eb68f5e33397a956a417a0969`  
**Approved staging database:** `aixion_gate1_rehearsal_20261030_staging`  
**Separate restore-check database:** `aixion_gate1_restorecheck_20261030_7d3f5a46_test`  
**PostgreSQL:** 17.11, isolated local Unix-socket cluster

This result supersedes the earlier lock-log note that the custom-format dump had not yet been restored.

## Result

The retained staging dump was restored into a newly created database using `pg_restore --exit-on-error`. The command exited `0`. Neither the approved staging database nor the SQLite source was overwritten or changed.

| Read-back item | Staging | Restored |
|---|---:|---:|
| Users | 3 | 3 |
| Workspaces | 3 | 3 |
| Projects | 1 | 1 |
| Sessions | 5 | 5 |
| External agents | 1 | 1 |
| External-agent credentials | 1 | 1 |
| Authentication rate-limit buckets | 17 | 17 |
| Current audit events | 2 | 2 |
| Legacy audit events | 2 | 2 |
| Applied schema migrations | 40 | 40 |
| Unscoped unknown-agent `agent.auth_failed` events in quarantine | 2 | 2 |

Both restored quarantined events have `workspace_id = NULL`, `original_event_type = agent.auth_failed`, and `quarantine_reason = UNSCOPED_UNKNOWN_AGENT_AUTH_FAILURE`. They remain in immutable `legacy_audit_events`, outside tenant-scoped `audit_events`.

The read-only migration checker returned `READY` for the restored database, with no missing, unknown, or mismatched migrations and `apply_requested = false`.

## Retained Evidence

Evidence is stored owner-only under:

`/Volumes/TradeBotFast/AixionControlTower/gate1-staging-rehearsal-20261030/`

- Dump: `gate1-v15-57c0b89-staging-target.dump`
- Dump SHA-256: `4c75a874f4be6c0e8ddfc040bf8301dd5e9ec474226ce9c69ef25ea61c6cc5e6`
- Restore log: `gate1-v15-57c0b89-restorecheck.log`
- Restore log SHA-256: `32b225e706da34d9dcf01167cdc0db585eb538dd6166b0e9db30528dab97d4fc`
- Full reconciliation note: `gate1-v15-57c0b89-restore-reconciliation.txt`
- Reconciliation-note SHA-256: `80503c0d6f2be6cc965f979039cc1356ded89a4d1e753ad1cb0756cea9d79eb7`

## Boundary and Remaining Gate 1 Work

This verifies the rehearsal dump can be restored into a separate local database and that the listed counts and migration ledger match the approved staging database. It does **not** establish the authoritative production data source, full application-wide PostgreSQL repository authority, production cutover, scheduled backup retention, or restore-time objective.

The restored test database remains separate from the approved staging database. The PostgreSQL cluster is ephemeral and its data directory is under `/tmp/aixion-codex-native-e2e-20261029`; preserve the external evidence before any cluster lifecycle action. Do not drop either database or stop/clean the shared cluster as part of this work.

**Gate 1 remains open.** Production startup remains fail-closed until the application-wide PostgreSQL authority and cutover requirements are actually satisfied.
