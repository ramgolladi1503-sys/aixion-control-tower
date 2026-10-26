# Pre-Billing Execution Ledger

Updated: 2026-10-26
PR #204 head: `26f05b7a73b340e39b208db9687c2d0484e2506f` (open, draft, unmerged; all eight checks green)
Working tree: `/Volumes/TradeBotFast/AixionControlTower/goal-workstreams/android-design`

| Slice / criterion | Owner | Status | Evidence / artifact | Blocker | Next action |
|---|---|---|---|---|---|
| 0 — Approval contract | Integration lead | PASS | Accepted contract and state-transition rules in `docs/PRE_BILLING_WORK_LOCK.md` | None for this gate | Preserve contract; do not infer provider capability from it. |
| 1 — Complete PostgreSQL persistence locally | Integration lead; `pg_authority_exit_gap` (read-only review complete) | FAIL | PR checks green; isolated PostgreSQL migration, route, concurrency, rollback, and restart subgates recorded. Current-source review: SQLite-backed defaults remain central; complete app-bound PostgreSQL repository bundle is selected only in opt-in PostgreSQL-auth mode. Authenticated readiness previously reported `database_backend=mixed`, `application_store_ready=false`. | Complete authority binding, safe import/cutover, and full-runtime exit evidence absent. | Build one app-wide repository-authority contract. For every durable route/worker/script bind to the lifespan-owned PostgreSQL bundle or return unavailable; keep production startup refused until inventory and cross-process verification pass. |
| 2 — Connector protocol reliability | Integration lead | UNVERIFIED | Current-source focused rerun is active in pytest session `91321`; failures were emitted before completion. The historic accepted contract-test subgate is revision-specific. | Current failures lack final traceback and disposition; no current-source gate acceptance. | Collect final output, diagnose each failure without weakening assertions, rerun exact failing cases and the full formal gate. |
| 3 — Antigravity adapter | Integration lead; `antigravity_e2e_preflight` agent (read-only) | UNVERIFIED | Historical same-invocation evidence is revision-specific; current adapter tests are not a real provider round trip. | Current connector/profile and authenticated Android session must be ready; fresh human decision required for a live request. | Obtain preflight; prepare the exact isolated run without using normal provider profile or stale approval; run only with an active human decision path. |
| 4 — Codex local permission adapter | Unassigned | UNVERIFIED | No current real Codex phone round-trip evidence | Native Codex permission request integration and same-invocation outcome are unproven | After Slice 1/Android path is stable, test the documented local `PermissionRequest` path against the real Codex app; record supported surfaces and failures. |
| 5 — Android approval/account UX | Android UI implementation; integration lead (acceptance) | UNVERIFIED | Current uncommitted diff is four approval-screen/test files (82 insertions, 4 deletions); `git diff --check` passes. Gradle session `44529` builds and runs unit/compile tasks; emulator 5556 is online. | Current diff has no completed build/test result or rendered approval-screen review. | Wait for session `44529`; then run connected instrumentation on emulator 5556, install the resulting debug APK, capture inbox/detail, and verify critical states/accessibility. |
| 6 — Local E2E and failure certification | Integration lead | UNVERIFIED | Exact-head CI green; focused PostgreSQL and Android results recorded | Current-source provider round trips and complete failure matrix are absent | Produce one reproducible Antigravity evidence bundle first; then Codex and remaining failure cases, each tied to exact source/device/provider versions. |
| 7 — Cost-controlled staging handoff | Integration lead; account owner supplies decisions | BLOCKED | Offline deployment/cost/retention/operations artifacts exist; matrix details exact pending inputs | Owner choices and separate authorization for any live cloud rehearsal | Collect approved project, region, workload, retention, pricing, exposure, and operator values; do not activate billing or provision resources in this goal. |

## Active bounded work

- Android local validation: Gradle session `44529` (current dirty UI changes; build, instrumentation compile, JVM tests); no result yet.
- Current-source connector validation: pytest session `91321` (10 focused modules, disposable PostgreSQL/SQLite paths); still active and has emitted failures; do not report a pass.
- Isolated current-source API: PID `35582`, loopback port 8006, external disposable PostgreSQL DB; `/health` returned HTTP 200 in 7.37s on the latest probe. No authenticated Android/provider E2E is currently established.
- Two read-only reviews completed: app-wide PostgreSQL authority gap and current Android UI validation gap. Their findings are reflected above.

## Integration rules

- The integration lead owns final acceptance and reconciles every agent result against the work-lock exit gate.
- Review file ownership before combining changes; agents share this worktree.
- Do not push until diffs and focused checks are reviewed. Do not merge.
- No cloud billing/resources, production data, deployment, or unverified provider/device claims.
