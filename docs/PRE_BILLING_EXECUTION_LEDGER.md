# Pre-Billing Execution Ledger

Updated: 2026-10-26
PR #204 head: `7cf0b150b71de78cb3e3fb24d191c3000ca7817f` (open, draft, unmerged)
Working tree: `/Volumes/TradeBotFast/AixionControlTower/goal-workstreams/android-design`

| Slice / criterion | Owner | Status | Evidence / artifact | Blocker | Next action |
|---|---|---|---|---|---|
| 0 — Approval contract | Integration lead | PASS | Accepted contract and state-transition rules in `docs/PRE_BILLING_WORK_LOCK.md` | None for this gate | Preserve contract; do not infer provider capability from it. |
| 1 — Complete PostgreSQL persistence locally | Integration lead; `pg_authority_gap` agent (read-only gap review) | FAIL | Exact-head CI green; isolated PostgreSQL migration, route, concurrency, rollback, and restart subgates recorded in work lock. Authenticated readiness reports `database_backend=mixed`, `application_store_ready=false`. | Application authority remains mixed; complete import/cutover and full selected-runtime exit evidence absent. | Select one bounded remaining authority gap from the agent report; implement only with explicit no-SQLite/fail-closed tests, then integrate into the complete cutover path. Keep production startup guard closed. |
| 2 — Connector protocol reliability | Integration lead | UNVERIFIED | A connector reliability subgate is recorded as accepted, but later work-lock entries keep the broader Slice 2 reliability gate open. | Full written gate has not been reconciled against current evidence criterion by criterion. | Reconcile timeouts, network loss/reconnect, duplicate delivery, revocation, server restart, and explicit liveness at exact current source; retain open status until all are evidenced. |
| 3 — Antigravity adapter | Integration lead; `antigravity_e2e_preflight` agent (read-only) | UNVERIFIED | Historical same-invocation evidence is revision-specific; current adapter tests are not a real provider round trip. | Current connector/profile and authenticated Android session must be ready; fresh human decision required for a live request. | Obtain preflight; prepare the exact isolated run without using normal provider profile or stale approval; run only with an active human decision path. |
| 4 — Codex local permission adapter | Unassigned | UNVERIFIED | No current real Codex phone round-trip evidence | Native Codex permission request integration and same-invocation outcome are unproven | After Slice 1/Android path is stable, test the documented local `PermissionRequest` path against the real Codex app; record supported surfaces and failures. |
| 5 — Android approval/account UX | `android_ui_slice5` agent (bounded UI implementation); integration lead (acceptance) | UNVERIFIED | Exact-head CI green; focused API 35 instrumentation 9/9; exact-head APK login screen rendered | Full authenticated approval flow, stale/expired behavior, accessibility, and decision/navigation device checks absent | Review UI agent diff, run focused checks, then authenticate on the emulator and perform device acceptance against an isolated local API. |
| 6 — Local E2E and failure certification | Integration lead | UNVERIFIED | Exact-head CI green; focused PostgreSQL and Android results recorded | Current-source provider round trips and complete failure matrix are absent | Produce one reproducible Antigravity evidence bundle first; then Codex and remaining failure cases, each tied to exact source/device/provider versions. |
| 7 — Cost-controlled staging handoff | Integration lead; account owner supplies decisions | BLOCKED | Offline deployment/cost/retention/operations artifacts exist; matrix details exact pending inputs | Owner choices and separate authorization for any live cloud rehearsal | Collect approved project, region, workload, retention, pricing, exposure, and operator values; do not activate billing or provision resources in this goal. |

## Active bounded work

- `android_ui_slice5`: edit only Android theme/auth/approval UI and associated tests; no commit/push; report changed files and focused evidence.
- `pg_authority_gap`: read-only review of the highest-priority Slice 1 authority gap; no runtime/data mutation.
- `antigravity_e2e_preflight`: read-only current-source setup check; no credentials, provider invocation, process restart, or phone decision.
- Local Android custom-API debug build: Gradle session `80395` is still live with no output. Inspect the same process/session before deciding whether it is stalled; do not restart solely because output is quiet.

## Integration rules

- The integration lead owns final acceptance and reconciles every agent result against the work-lock exit gate.
- Review file ownership before combining changes; agents share this worktree.
- Do not push until diffs and focused checks are reviewed. Do not merge.
- No cloud billing/resources, production data, deployment, or unverified provider/device claims.
