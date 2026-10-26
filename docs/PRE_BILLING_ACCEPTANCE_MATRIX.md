# Pre-Billing Acceptance Matrix

**Snapshot:** 2026-10-26  
**Repository:** `ramgolladi1503-sys/aixion-control-tower`, PR #204  
**PR head:** `d0fdde1c017eb74027307fb40350ad94b5d87d5d`  
**PR state:** open, draft, unmerged  
**Worktree:** `/private/tmp/aixion-control-tower-pr204-work`, clean at snapshot time  
**Scope:** `docs/PRE_BILLING_WORK_LOCK.md`; no billing, cloud, production data, or merge action.

This is an evidence snapshot, not a declaration of production readiness. `PASS` means the stated gate is supported by the evidence listed; `PARTIAL` means named subchecks pass but the full criterion does not; `UNVERIFIED` means current sufficient evidence is absent; `BLOCKED` means a prerequisite prevents valid completion.

| Slice | Locked acceptance gate | Status | Evidence and remaining work |
|---|---|---|---|
| 0 — Approval contract | Contract and state transitions are documented; mismatch, expiry, replay, duplicate tap, lost connection, and connector restart behavior are explicit and fail closed. | **PASS** | The lock records the core contract as accepted and documents ambiguous decision retry, `CLAIMED` semantics, cancellation races, and restart identity behavior. This accepts the documented contract only; it does not prove provider behavior. |
| 1 — PostgreSQL persistence | Migrations work from an empty database and supported upgrade state; the full backend suite runs against PostgreSQL; multi-process concurrency, rollback, restart, and workspace-isolation checks pass; SQLite cannot start the production profile. | **BLOCKED** | Exact-head CI is green (10/10 check runs); `Backend validation` runs the backend suite with PostgreSQL 17, and the production-startup-guard job passes by confirming startup is blocked. Migration bootstrap/upgrade and focused race, rollback, restart, and isolation regressions are recorded. However, the app is not wholly PostgreSQL-authoritative, the importer is not a complete production cutover, and runtime-wide gates are not established. The static inventory is diagnostic, not proof of authority. Keep production startup blocked. |
| 2 — Connector protocol | Contract tests cover timeout, disconnect, reconnect, duplicate delivery, credential revocation, and server restart. No failure mode silently approves or loses the final state. | **UNVERIFIED** | The lock contains an accepted contract-test entry based on earlier source revisions, but later entries still call Slice 2 open. Current-head CI is green, but no current-head acceptance reconciles that status conflict. Re-run or map each required failure case to tests on this exact head, then correct the lock’s contradictory status. |
| 3 — Antigravity adapter | A real Antigravity session on a local development stack pauses on an approval, the Android app receives that exact request, and the same session resumes on approve or stops on reject. Re-run after connector restart and prove timeout/revocation fail closed. A scripted demo alone does not pass. | **BLOCKED** | The lock records an older real phone approve/deny round trip, but not against this immutable PR revision. The 2026-10-26 isolation check found the installed Antigravity process using the normal user profile and no confirmed safe second-profile launch; the normal TradeBot workspace must not be used. A separate macOS user or VM is required. The current emulator is online but has no Control Tower package installed. |
| 4 — Codex adapter | A real, identified Codex local permission request is reviewed on the phone, and the same Codex invocation resumes on approve or is prevented on deny. Verify timeout, API failure, stale/mismatched payload, and credential revocation deny. Unit tests and a mock API do not pass this gate; obtain evidence from the actual Codex app and connector. | **UNVERIFIED** | Local hook/API tests exist, but the lock contains no evidence of a real Codex `PermissionRequest` reaching the phone and the same invocation continuing or stopping. Run and record the native Codex flow and required failure cases; do not claim hosted-tool coverage. |
| 5 — Android UX | Release build checks reject unsafe production API configuration; approval screens never apply a decision to a different or stale request; navigation and critical approval actions are covered by automated tests and device checks. | **UNVERIFIED** | Exact-head Android CI passed JVM tests, instrumentation-source compilation, debug APK build, and release configuration guards. Instrumentation was compiled, not executed. The emulator is booted, but no app package is installed and no current rendered screen, navigation, or critical-action device check has been captured. |
| 6 — Local E2E/failure certification | All required automated checks pass against PostgreSQL, and each provider's real end-to-end evidence is recorded with build/revision, device, connector/provider version, and outcomes. Clearly label any unavailable physical-device or provider test as unverified. | **UNVERIFIED** | Exact-head automated checks pass. Historical Antigravity phone evidence is local/SQLite and lacks an immutable source/build identity; current-head provider E2E is absent. Codex native E2E is absent. Record each required failure scenario and provider evidence against exact build/revision before acceptance. |
| 7 — Staging handoff | A reviewer can understand the proposed cloud resources, permissions, recurring costs, data handling, rollback, and owner before any billing action. Do not claim away-from-computer delivery from local-only tests. | **BLOCKED** | Offline topology, Terraform validation, cost and runbook artifacts exist; exact-head staging Terraform validation passed without cloud access. Owner inputs remain pending, including confirmed project/region/workload/retention, dated regional cost evidence, uncapped exposure, and named alert operators. No billing or resource changes are authorized or performed. |

## Exact-head CI evidence

- PR #204: [GitHub pull request](https://github.com/ramgolladi1503-sys/aixion-control-tower/pull/204)
- Exact-head CI run: [run 40268301968](https://github.com/ramgolladi1503-sys/aixion-control-tower/actions/runs/40268301968)
- Public check-run API reported 10 completed successful checks for `d0fdde1c017eb74027307fb40350ad94b5d87d5d`.
- The CI workflow runs provider hook CLI tests and the full backend pytest suite with PostgreSQL 17. Android instrumentation sources compile in CI; the workflow does not execute those instrumentation tests on an emulator/device.
- These checks establish the checked-in automated job results for this revision. They do not establish real provider-to-phone continuation, full application-wide PostgreSQL authority, or production readiness.

## First genuine current-head E2E: critical path

1. Provide a genuinely isolated Antigravity environment (separate macOS user or VM); do not reuse the active TradeBot profile.
2. Use the PR-head hook and a disposable workspace with a test-only connector credential. Keep the API/database environment isolated and record exact revisions.
3. Install the exact-revision Android build in the booted emulator, authenticate, and confirm it targets the isolated API.
4. Trigger one owner-authorized harmless Antigravity tool invocation. Capture the exact request, PostgreSQL records, phone decision, and the same invocation's allow/deny result.
5. Prove duplicate/replay/expiry fail closed, then exercise connector restart and revocation gates. Since Slice 1's production-authoritative PostgreSQL runtime remains blocked, label any local-profile result precisely; do not describe it as production-runtime certification.

## Immediate highest-priority work

- Resolve the Slice 2 accepted-vs-open conflict with exact-head test mapping.
- Continue Slice 1's complete PostgreSQL authority/cutover work while preserving the production startup guard.
- Obtain the isolated Antigravity environment needed for current-head same-invocation phone proof.
- Install and render the exact-head Android app in the emulator; execute device instrumentation rather than treating compilation as a device pass.
- Keep Codex native E2E and owner-dependent Slice 7 items visibly unverified/blocked until their evidence exists.

No cloud, billing, deployment, production-data, or merge action was performed for this matrix.
