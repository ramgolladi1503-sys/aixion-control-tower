# Pre-Billing Acceptance Matrix

**Snapshot:** 2026-10-26
**Repository:** `ramgolladi1503-sys/aixion-control-tower`
**PR:** [#204](https://github.com/ramgolladi1503-sys/aixion-control-tower/pull/204), open, draft, unmerged
**Exact PR head:** `26f05b7a73b340e39b208db9687c2d0484e2506f`
**Worktree:** `/Volumes/TradeBotFast/AixionControlTower/goal-workstreams/android-design`
**Scope:** `docs/PRE_BILLING_WORK_LOCK.md`. No billing, cloud changes, production data, deployment, or merge.

Statuses apply to the complete exit gate shown. A passing focused test proves only its named subgate.

| Slice | Exit gate (verbatim from the work lock) | Status | Current evidence and remaining work |
|---|---|---|---|
| 0 — Approval contract | contract and state transitions are documented; mismatch, expiry, replay, duplicate tap, lost connection, and connector restart behavior are explicit and fail closed. | **PASS** | Accepted by the lock. This is the protocol-contract gate, not provider-runtime proof. |
| 1 — PostgreSQL persistence | migrations work from an empty database and supported upgrade state; the full backend suite runs against PostgreSQL; multi-process concurrency, rollback, restart, and workspace-isolation checks pass; SQLite cannot start the production profile. | **FAIL** | PR checks pass and recorded isolated PostgreSQL suites cover migration, route, concurrency, rollback, restart, and isolation subgates. Authenticated readiness previously reported `database_backend=mixed` and `application_store_ready=false`. Current-source review identifies the systemic gap: SQLite-backed defaults remain central; a complete app-bound PostgreSQL repository bundle is selected only in opt-in PostgreSQL-auth mode. Production startup remains deliberately refused. Full authority binding, safe import/cutover, and selected-runtime exit evidence are absent. |
| 2 — Connector protocol | contract tests cover timeout, disconnect, reconnect, duplicate delivery, credential revocation, and server restart. No failure mode silently approves or loses the final state. | **UNVERIFIED** | The lock records an accepted contract-test subgate for an older source revision. A current-source focused rerun is live (pytest session 91321) and has emitted failures before completion; the final report is pending. Do not count the full slice as passed until failures are diagnosed and all criteria are re-evidenced at current source. |
| 3 — Antigravity adapter | a real Antigravity session on a local development stack pauses on an approval, the Android app receives that exact request, and the same session resumes on approve or stops on reject. Re-run after connector restart and prove timeout/revocation fail closed. A scripted demo alone does not pass. | **UNVERIFIED** | Genuine same-invocation phone approval evidence exists only for older revision `2ad22d4`. Current-source adapter and PostgreSQL tests pass, but a current-source provider→Android decision→same-invocation continuation and the required restart/timeout/revocation run are not evidenced. |
| 4 — Codex adapter | a real, identified Codex local permission request is reviewed on the phone, and the same Codex invocation resumes on approve or is prevented on deny. Verify timeout, API failure, stale/mismatched payload, and credential revocation deny. Unit tests and a mock API do not pass this gate; obtain evidence from the actual Codex app and connector. | **UNVERIFIED** | No current evidence shows a real Codex permission request reaching Android and the same invocation resuming/stopping. Native Codex Desktop interception remains an unverified product capability. |
| 5 — Android UX | release build checks reject unsafe production API configuration; approval screens never apply a decision to a different or stale request; navigation and critical approval actions are covered by automated tests and device checks. | **UNVERIFIED** | Exact-head CI and debug APK checks pass. The current dirty UI change caps/centers approval inbox and detail content at 720dp with two new bounds tests; Gradle session 44529 is still running and no rendered approval screen from this diff has been reviewed. Prior centered-login evidence and 9/9 instrumentation do not cover this diff. Authenticated approval behavior, stale-request device behavior, navigation, and accessibility remain unverified. |
| 6 — Local E2E/failure certification | all required automated checks pass against PostgreSQL, and each provider's real end-to-end evidence is recorded with build/revision, device, connector/provider version, and outcomes. Clearly label any unavailable physical-device or provider test as unverified. | **UNVERIFIED** | Exact-head CI checks are green. Targeted PostgreSQL and Android checks are recorded. Current-revision Antigravity and real Codex round trips, plus the complete failure matrix tied to a reproducible run, are still missing. Historical evidence is not carried forward. |
| 7 — Staging handoff | a reviewer can understand the proposed cloud resources, permissions, recurring costs, data handling, rollback, and owner before any billing action. Do not claim away-from-computer delivery from local-only tests. | **BLOCKED** | Offline deployment, cost, retention, and operations artifacts exist. Owner-approved project/region/workload/retention, dated regional pricing, uncapped exposure, named operators/channels, and staging values remain pending. No cloud or billing action is authorized or performed. |

## Exact-head CI evidence

At PR head `26f05b7a73b340e39b208db9687c2d0484e2506f`, all eight reported checks pass: Android validation, Backend validation, Android debug APK, Mission Control container, expected-blocked production startup guard, no-cloud Terraform validation, Backend CI build, and Backend CI test. These checks cover the committed PR head only; the four-file Android UI diff is uncommitted and not covered by them. This does not override the Slice 1 runtime authority gap or satisfy real provider/device acceptance gates.

## Immediate critical path

1. Close Slice 1's complete PostgreSQL authority and safe import/cutover requirements while retaining production fail-closed startup.
2. Re-establish a current-source isolated Antigravity connector and authenticated Android session, then capture one real same-invocation approve and reject result with a reproducible evidence bundle.
3. Complete the real Codex local `PermissionRequest` round trip and separately label unsupported/native Desktop surfaces.
4. Finish Android authenticated flow, navigation, accessibility, and failure-state acceptance.
5. Run the complete current-source PostgreSQL and provider failure certification; update each gate only from direct evidence.
6. Collect remaining Slice 7 owner inputs and keep all live cloud/billing steps separately authorized.

## Scope and safety

Do not weaken approval, execution, or production-startup guards to obtain a demo. Do not treat mock adapters, focused tests, a rendered login screen, or old provider evidence as full product acceptance. No production database, cloud resource, billing setting, deployment, or merge is changed by this matrix.
