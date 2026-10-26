# Pre-Billing Acceptance Matrix

**Snapshot:** 2026-10-26
**Repository:** `ramgolladi1503-sys/aixion-control-tower`, PR #204
**PR head:** `723f60800f819f836c3d61dfa48cd33b7d45e78e`
**PR state:** open, draft, unmerged
**Worktree:** `/private/tmp/aixion-control-tower-pr204-work`, clean at snapshot time
**Scope:** `docs/PRE_BILLING_WORK_LOCK.md`; no billing, cloud, production-data, deployment, or merge action.

`PASS` means the stated gate is supported by evidence; `PARTIAL` means only part of the gate is verified; `UNVERIFIED` means sufficient evidence is absent; `BLOCKED` means a prerequisite prevents valid completion; `FAIL` means current evidence contradicts the gate.

| Slice | Locked acceptance gate | Status | Evidence and remaining work |
|---|---|---|---|
| 0 — Approval contract | Contract and state transitions are documented; mismatch, expiry, replay, duplicate tap, lost connection, and connector restart behavior are explicit and fail closed. | **PASS** | The lock accepts the documented contract, including ambiguous claim handling, cancellation races, and restart identity behavior. This is contract acceptance, not provider behavior proof. |
| 1 — PostgreSQL persistence | Migrations work from an empty database and supported upgrade state; the full backend suite runs against PostgreSQL; multi-process concurrency, rollback, restart, and workspace-isolation checks pass; SQLite cannot start the production profile. | **BLOCKED** | Exact-head PR CI passed, including PostgreSQL Backend validation and the expected production-startup refusal. A new test at this head also passed: a spawned OS process entered FastAPI lifespan, recovered a persisted stale AgentRun lease from disposable PostgreSQL 17, and the parent process verified the durable recovery event. Still open: complete production application-wide PostgreSQL authority, safe supported import/cutover, and runtime-wide concurrency, rollback, restart, and isolation evidence. Keep production startup blocked. |
| 2 — Connector protocol | Contract tests cover timeout, disconnect, reconnect, duplicate delivery, credential revocation, and server restart. No failure mode silently approves or loses the final state. | **PASS** | The lock accepts the connector contract gate. The exact-head CI checks passed on `723f6080`; the new commit changes only the PostgreSQL AgentRun restart test. This does not claim physical network-loss or native-provider behavior. |
| 3 — Antigravity adapter | A real Antigravity session on a local development stack pauses on an approval, the Android app receives that exact request, and the same session resumes on approve or stops on reject. Re-run after connector restart and prove timeout/revocation fail closed. A scripted demo alone does not pass. | **BLOCKED** | Historical phone approval evidence exists on an older revision. No current-head native same-invocation proof is recorded. Antigravity is running in the normal profile with TradeBot activity; no isolated native session is available, and that profile was not used. |
| 4 — Codex adapter | A real, identified Codex local permission request is reviewed on the phone, and the same Codex invocation resumes on approve or is prevented on deny. Verify timeout, API failure, stale/mismatched payload, and credential revocation deny. Unit tests and a mock API do not pass this gate; obtain evidence from the actual Codex app and connector. | **UNVERIFIED** | Hook/API tests do not prove a real Codex `PermissionRequest` reaches the app and the same native invocation continues or stops. Obtain and record native Codex evidence and the required failure cases. |
| 5 — Android UX | Release build checks reject unsafe production API configuration; approval screens never apply a decision to a different or stale request; navigation and critical approval actions are covered by automated tests and device checks. | **PARTIAL** | The verified debug artifact from source head `043a972a` rendered the centered sign-in form on `emulator-5554`; Android source did not change in commit `723f6080`. A tap on the email field caused a `MainActivity` ANR after 65.748 seconds. The saved ANR trace is permission-protected; a bug-report attempt produced no archive and was stopped after ten minutes. No root cause is established. Full interaction and device acceptance remain open. |
| 6 — Local E2E/failure certification | All required automated checks pass against PostgreSQL, and each provider's real end-to-end evidence is recorded with build/revision, device, connector/provider version, and outcomes. Clearly label any unavailable physical-device or provider test as unverified. | **UNVERIFIED** | All nine exact-head PR checks passed. The current-head Antigravity same-invocation flow and Codex native flow are not verified. Do not substitute hook callbacks or mocked APIs for those provider gates. |
| 7 — Staging handoff | A reviewer can understand the proposed cloud resources, permissions, recurring costs, data handling, rollback, and owner before any billing action. Do not claim away-from-computer delivery from local-only tests. | **BLOCKED** | Offline topology, Terraform validation, cost notes, and runbooks exist. Owner-approved project, region, workload, retention, regional cost evidence, exposure limits, and alert operators remain outstanding. No cloud or billing action is authorized or performed. |

## Exact-head evidence

- PR #204: [GitHub pull request](https://github.com/ramgolladi1503-sys/aixion-control-tower/pull/204)
- Current source: `723f60800f819f836c3d61dfa48cd33b7d45e78e`
- Exact-head CI: [run 40302660250](https://github.com/ramgolladi1503-sys/aixion-control-tower/actions/runs/40302660250) — all nine PR checks completed successfully.
- The Android debug artifact used for the rendered login capture was built from `043a972a30b07096c86816360a41cd8019e2d352`; Android sources were unchanged at `723f6080`.
- Automated tests, CI success, and a rendered login screen do not establish production readiness or native provider continuation.

## Next acceptance actions

1. Continue Slice 1 by closing the remaining app-wide PostgreSQL authority and safe import/cutover gaps while keeping the production startup guard closed.
2. Diagnose the emulator ANR using an authorized way to export its protected trace; do not change Android code until the blocked main-thread work is identified.
3. Run current-head Antigravity phone approval in a genuinely isolated native environment and verify the same invocation resumes or stops.
4. Run and record the native Codex permission flow and required deny/failure cases.
5. Obtain the owner inputs required for the staging handoff only after its prerequisites are satisfied.

No new configuration keys or schema migrations were introduced by the restart regression. It adds one test file; rollout remains blocked on the acceptance items above. PR #204 remains unmerged.
