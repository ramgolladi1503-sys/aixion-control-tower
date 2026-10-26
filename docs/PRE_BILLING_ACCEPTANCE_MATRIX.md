# Pre-Billing Acceptance Matrix

**Snapshot:** 2026-10-26
**Repository:** `ramgolladi1503-sys/aixion-control-tower`, PR #204
**PR head:** `c9b780246383c5fe86f07ffd6181fd651d1cad59`
**PR state:** open, draft, unmerged
**Worktree:** `/Volumes/TradeBotFast/AixionControlTower/goal-workstreams/android-design` at the PR head; importer and Android follow-ups are uncommitted and under validation
**Scope:** `docs/PRE_BILLING_WORK_LOCK.md`; no billing, cloud, production-data, deployment, or merge action.

Statuses describe the complete slice gate, not a subset of its tests. `PASS` means the full stated gate is supported by evidence; `UNVERIFIED` means sufficient evidence is absent; `BLOCKED` means a prerequisite prevents valid completion; `FAIL` means current evidence contradicts the gate.

| Slice | Locked acceptance gate | Status | Evidence and remaining work |
|---|---|---|---|
| 0 — Approval contract | Contract and state transitions are documented; mismatch, expiry, replay, duplicate tap, lost connection, and connector restart behavior are explicit and fail closed. | **PASS** | The lock accepts the documented contract, including ambiguous claim handling, cancellation races, and restart identity behavior. This is contract acceptance, not provider behavior proof. |
| 1 — PostgreSQL persistence | Migrations work from an empty database and supported upgrade state; the full backend suite runs against PostgreSQL; multi-process concurrency, rollback, restart, and workspace-isolation checks pass; SQLite cannot start the production profile. | **BLOCKED** | The PR checks passed, including backend validation and the expected production-startup refusal. A spawned OS process recovered a persisted stale AgentRun lease from disposable PostgreSQL 17, with the parent verifying the durable recovery event. Import planning now rejects unknown columns in `auth_email_outbox`, delivery claims/failures, and auth rate-limit buckets before target connection. `backend/tests/test_postgres_import.py` passed **81 tests, 26 skipped**; PostgreSQL-only cases were skipped because `AIXION_TEST_POSTGRES_DSN` is unset. Ruff, Python compilation, and `git diff --check` passed. Production startup remains intentionally blocked. Still open: application-wide PostgreSQL authority, supported import/cutover, and runtime-wide concurrency, rollback, restart, and isolation evidence. |
| 2 — Connector protocol | Contract tests cover timeout, disconnect, reconnect, duplicate delivery, credential revocation, and server restart. No failure mode silently approves or loses the final state. | **PASS** | The work lock accepts the connector protocol gate and PR checks passed at the current source line. This does not claim physical network-loss or native-provider behavior. |
| 3 — Antigravity adapter | A real Antigravity session on a local development stack pauses on an approval, the Android app receives that exact request, and the same session resumes on approve or stops on reject. Re-run after connector restart and prove timeout/revocation fail closed. A scripted demo alone does not pass. | **BLOCKED** | No current-head native same-invocation proof is recorded. The normal Antigravity profile has an active TradeBot scheduled task and was not used. A genuinely isolated native session and owner-scoped connector are required. |
| 4 — Codex adapter | A real, identified Codex local permission request is reviewed on the phone, and the same Codex invocation resumes on approve or is prevented on deny. Verify timeout, API failure, stale/mismatched payload, and credential revocation deny. Unit tests and a mock API do not pass this gate; obtain evidence from the actual Codex app and connector. | **UNVERIFIED** | Hook/API tests do not prove a real Codex `PermissionRequest` reaches the app or that the same native invocation continues or stops. Native Codex evidence and required failure cases remain outstanding. |
| 5 — Android UX | Release build checks reject unsafe production API configuration; approval screens never apply a decision to a different or stale request; navigation and critical approval actions are covered by automated tests and device checks. | **UNVERIFIED** | On 2026-10-26, emulator `emulator-5554` displayed the centered sign-in card with the email field focused and keyboard visible. Capture: `docs/verification/android-login-emulator-20261026.png`. Installed package was debug version `0.1.0`; its exact source commit was not established. The updated instrumentation source compiled with `:app:compileDebugAndroidTestKotlin`, but the targeted connected test failed before device execution because Gradle reported missing cached transform/accessor files. This proves only a signed-out screen render and source compilation, not login, approval review/decision, accessibility, or current-head artifact provenance. Earlier ANR evidence remains unresolved; this later capture does not diagnose or clear it. |
| 6 — Local E2E/failure certification | All required automated checks pass against PostgreSQL, and each provider's real end-to-end evidence is recorded with build/revision, device, connector/provider version, and outcomes. Clearly label any unavailable physical-device or provider test as unverified. | **UNVERIFIED** | All eight checks reported for PR head `c9b7802` passed. Current-head Antigravity same-invocation approval and Codex native flow are not verified. Do not substitute hook callbacks or mocked APIs for those provider gates. |
| 7 — Staging handoff | A reviewer can understand the proposed cloud resources, permissions, recurring costs, data handling, rollback, and owner before any billing action. Do not claim away-from-computer delivery from local-only tests. | **BLOCKED** | Offline topology, Terraform validation, cost notes, and runbooks exist. Owner-approved project, region, workload, retention, regional cost evidence, exposure limits, and alert operators remain outstanding. No cloud or billing action is authorized or performed. |

## Exact-head evidence

- PR #204: [GitHub pull request](https://github.com/ramgolladi1503-sys/aixion-control-tower/pull/204)
- Source head: `c9b780246383c5fe86f07ffd6181fd651d1cad59`
- PR state: open, draft, unmerged; merge state reported `CLEAN`.
- Exact-head PR checks: all eight reported checks completed successfully (Android CI build/test, Android debug APK, backend test/validation, staging Terraform validation without cloud access, Mission Control container validation, and expected production-startup refusal).
- Emulator capture: signed-out debug app screen only; installed APK source SHA was not matched, and no authenticated approval was created or decided.
- CI success and a rendered login screen do not establish production readiness or native provider continuation.

## Next acceptance actions

1. Continue Slice 1 by closing the remaining application-wide PostgreSQL authority and safe import/cutover gates while keeping the production startup guard closed.
2. Run a fresh connected Android instrumentation pass and review authenticated approval screens; keep the current screenshot scoped to sign-in rendering only.
3. Run current-head Antigravity phone approval in a genuinely isolated native environment and verify the same invocation resumes or stops.
4. Run and record the native Codex permission flow and required deny/failure cases.
5. Obtain the owner inputs required for staging handoff only after its prerequisites are satisfied.

PR #204 remains unmerged. No billing, cloud, production-data, or deployment action occurred.
