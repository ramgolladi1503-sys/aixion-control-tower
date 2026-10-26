# Pre-Billing Acceptance Matrix

**Snapshot:** 2026-10-27
**Repository:** `ramgolladi1503-sys/aixion-control-tower`
**PR:** [#204](https://github.com/ramgolladi1503-sys/aixion-control-tower/pull/204), open, draft, unmerged
**Verified source baseline:** `cdcec1610f3e76670da1b00672ca36ee79f59966`; the Android timeout update was committed and pushed afterward as `aabf1303131ce97870b92b30086f79ce98866137`.
**Worktree:** `/Volumes/TradeBotFast/AixionControlTower/goal-workstreams/android-design`
**Working-tree changes:** Clean after the Android timeout and acceptance-matrix commits.
**Scope:** `docs/PRE_BILLING_WORK_LOCK.md`. No billing, cloud changes, production data, deployment, or merge.

Statuses apply to the complete exit gate shown. A passing focused test proves only its named subgate.

| Slice | Exit gate (verbatim from the work lock) | Status | Current evidence and remaining work |
|---|---|---|---|
| 0 — Approval contract | contract and state transitions are documented; mismatch, expiry, replay, duplicate tap, lost connection, and connector restart behavior are explicit and fail closed. | **PASS** | Accepted by the lock. This is the protocol-contract gate, not provider-runtime proof. |
| 1 — PostgreSQL persistence | migrations work from an empty database and supported upgrade state; the full backend suite runs against PostgreSQL; multi-process concurrency, rollback, restart, and workspace-isolation checks pass; SQLite cannot start the production profile. | **FAIL** | Focused PostgreSQL subgates and PR checks are recorded, but they do not establish application-wide authority. The current local API is in the local profile with PostgreSQL auth enabled and still has a SQLite compatibility database path. The lock records mixed database authority and `application_store_ready=false`; full repository binding, safe import/cutover, and selected-runtime evidence remain absent. Production SQLite startup stays fail-closed. |
| 2 — Connector protocol | contract tests cover timeout, disconnect, reconnect, duplicate delivery, credential revocation, and server restart. No failure mode silently approves or loses the final state. | **UNVERIFIED** | Prior focused contract tests cover named subgates, but the old in-progress session reference is stale and there is no current live run tied to this snapshot. Reconcile failures and re-run the full relevant current-source protocol/failure suite before closing the slice. |
| 3 — Antigravity adapter | a real Antigravity session on a local development stack pauses on an approval, the Android app receives that exact request, and the same session resumes on approve or stops on reject. Re-run after connector restart and prove timeout/revocation fail closed. A scripted demo alone does not pass. | **UNVERIFIED** | Current-source workspace hook is installed only at `/tmp/aixion-antigravity-phone-e2e/workspace`; its non-secret config is mode `0600`, and authenticated hook preflight passed for the scoped agent/project against local API port `8006`. The isolated app profile process is present. No fresh native invocation, Android approval decision, same-invocation resume/reject, or restart/timeout/revocation proof has occurred. Owner must foreground/sign in to the isolated Antigravity session if prompted; the normal TradeBot Antigravity session remains untouched. |
| 4 — Codex adapter | a real, identified Codex local permission request is reviewed on the phone, and the same Codex invocation resumes on approve or is prevented on deny. Verify timeout, API failure, stale/mismatched payload, and credential revocation deny. Unit tests and a mock API do not pass this gate; obtain evidence from the actual Codex app and connector. | **UNVERIFIED** | No current evidence shows a real Codex permission request reaching Android and the same invocation resuming/stopping. Codex Desktop interception remains an unverified capability; document supported surfaces separately. |
| 5 — Android UX | release build checks reject unsafe production API configuration; approval screens never apply a decision to a different or stale request; navigation and critical approval actions are covered by automated tests and device checks. | **UNVERIFIED** | Exact-head CI is green. On the uncommitted Android timeout diff, the debug APK built and 222 JVM tests passed; emulator login against the local API succeeded. These checks do not prove authenticated approval review/decision, stale-request handling, TalkBack/accessibility, or full rendered device acceptance. The Android timeout diff remains outside the PR head. |
| 6 — Local E2E/failure certification | all required automated checks pass against PostgreSQL, and each provider's real end-to-end evidence is recorded with build/revision, device, connector/provider version, and outcomes. Clearly label any unavailable physical-device or provider test as unverified. | **UNVERIFIED** | PR head checks are green and focused PostgreSQL/Android results are recorded. Current-source Antigravity and real Codex round trips, plus the complete failure matrix tied to reproducible runs, remain missing. |
| 7 — Staging handoff | a reviewer can understand the proposed cloud resources, permissions, recurring costs, data handling, rollback, and owner before any billing action. Do not claim away-from-computer delivery from local-only tests. | **BLOCKED** | Offline deployment, cost, retention, and operations artifacts exist. Owner-approved project/region/workload/retention, dated regional pricing, uncapped exposure, named operators/channels, and staging values remain pending. No cloud or billing action is authorized or performed. |

## Exact-head CI evidence

At prior PR head `cdcec1610f3e76670da1b00672ca36ee79f59966`, all reported checks were green. CI for the pushed Android timeout change at `aabf1303131ce97870b92b30086f79ce98866137` was queued when this snapshot was written. The local build and unit-test result are recorded separately above. Green CI does not close Slice 1's runtime authority gap or satisfy real provider/device acceptance gates.

## Immediate critical path

1. Complete the current-source M1 round trip: isolated Antigravity native request → Android exact request → fresh human decision → same invocation resumes or stops. The connector preflight is ready; isolated-profile sign-in/foreground and the native invocation remain.
2. Repeat the opposite decision path and prove the required timeout, mismatch, duplicate/replay, revocation, and connector/backend restart cases fail closed. Record exact revision, provider, connector, API, Android build, emulator, request, and outcome.
3. Continue Slice 1 PostgreSQL authority, import/cutover, and recovery work independently. It remains mandatory for production release but is not a prerequisite to demonstrating the local M1 product path.
4. Verify the supported Codex local permission surface on Android; label unsupported surfaces explicitly.
5. Finish Android authenticated approval UX, navigation, accessibility, and stale/failure-state acceptance.
6. Complete the current-source backend/provider certification and then collect remaining Slice 7 owner inputs. Keep cloud and billing actions separately authorized.

## Scope and safety

Do not weaken approval, execution, or production-startup guards to obtain a demo. Do not treat mock adapters, focused tests, a rendered login screen, or old provider evidence as full product acceptance. No production database, cloud resource, billing setting, deployment, or merge is changed by this matrix.
