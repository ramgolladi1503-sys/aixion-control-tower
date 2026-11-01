# Codex CLI to Android Approval E2E Checkpoint

**Date:** 2026-10-29 (Asia/Kolkata)
**Repository:** `ramgolladi1503-sys/aixion-control-tower`
**PR:** [#204](https://github.com/ramgolladi1503-sys/aixion-control-tower/pull/204), draft, unmerged
**Source revision:** `3b6cc1034b537835b852e43ee8daccd586d23145`
**Codex surface:** Codex CLI `0.163.0-alpha.11.2`; this does not test Codex Desktop.

## Isolated environment

- Host: local macOS development environment.
- Backend: local API on `127.0.0.1:8004`; Android emulator reaches it through host loopback.
- Persistence: isolated local PostgreSQL 17 database `aixion_phone_e2e`, copied for this test. No production or hosted data was used.
- Android: emulator `emulator-5556`, Android 15 / API 35, package `com.aixion.controltower.debug`, version `0.1.0` / version code `1`.
- Workspace and Codex home: isolated beneath `/tmp/aixion-codex-native-e2e-20261029`. A project-scoped `PermissionRequest` hook was reviewed and activated in an isolated Codex profile. The normal Codex profile and other agent tasks were not changed.

## Observed round trips

| Request | Phone decision | Backend state | Command outcome |
|---|---|---|---|
| `d3922b1e` (short ID shown in the test prompt) | Approve | App History showed Approved and decision consumed | The exact `mkdir /tmp/aixion-codex-native-e2e-20261029/approved-marker` invocation completed; marker exists. |
| `e0f18c1f` (short ID shown in the test prompt) | Deny | App showed Denied / reject | Marker absent. This pre-fix run returned the ambiguous hook reason “Connector liveness could not be confirmed.” |
| `approval_8c3d877413a2` | Deny | Isolated PostgreSQL row is `DENIED`; emulator detail showed “Decision closed” | `denied-marker-after-fix` is absent and the Codex CLI process remained open. The hook subprocess exited. Its final denial text was not preserved in the session transcript, so that message itself is not claimed as observed. |

The last denial request used medium-risk `codex://tool/Bash` with the exact arguments `mkdir /tmp/aixion-codex-native-e2e-20261029/denied-marker-after-fix`. The callback-scoped invocation identity was stored with the request. The app, backend row, exact command, and missing filesystem marker agree on the denial outcome.

## Fix and automated validation

The pre-fix denial exposed a heartbeat/decision race: when a lease heartbeat fails after a phone decision, the hook could return a generic liveness error without reading the terminal decision. The hook now makes a bounded final decision read after that heartbeat failure, validates the exact request identity and immutable payload snapshot, and preserves a recorded denial. It never treats an approval as sufficient after a failed heartbeat; pending or unreadable state continues to fail closed. Deadline exhaustion still denies without a decision read.

Commits on PR #204:

- `17fd43f1` — `fix(codex): preserve phone denial on heartbeat race`
- `3b6cc103` — `test(codex): cover post-heartbeat decision read`

Focused local validation at the current source revision:

- Codex hook and CLI suites: **179 passed**.
- Ruff: passed.
- Python compilation: passed.
- `git diff --check`: passed.
- Exact-head GitHub checks: Android validation, debug APK, Mission Control container, production startup guard, staging Terraform validation, Backend CI, and Android CI passed; **Backend validation remained pending at the time of this checkpoint**.

## Scope, limitations, and next gate

This is evidence for the Codex **CLI** `PermissionRequest` hook on one local machine and one Android emulator. It is not evidence for Codex Desktop interception, remote push delivery, a locked/background phone, multi-host availability, or production operation. The approved invocation completed and the phone-denied command did not execute, but the final hook denial text for the post-fix retry was not captured in the Codex transcript.

The complete Codex slice remains **UNVERIFIED**. Before acceptance, capture the post-fix Codex result directly and run controlled end-to-end timeout, API/backend failure, stale or changed payload, credential revocation, and connector/backend restart cases. Each must show fail-closed behavior for the same pending invocation. Mark Codex Desktop unsupported unless its native surface exposes a documented permission hook with equivalent evidence.

No new configuration key, database migration, cloud resource, billing setting, deployment, or merge was introduced by this checkpoint. Do not use this local result to claim away-from-computer delivery.

## Current PR-head follow-up (2026-10-29)

The last fully validated PR head before the current work was `cdddd0490f864b5c657f5b95b4488855ffd2e5e7`. Its exact-head GitHub checks all passed, including PostgreSQL-enabled Backend validation (**19m 48s**), Android validation and debug APK build, Mission Control container validation, the expected-blocked production startup guard, and staging Terraform validation without cloud access. The PR remains draft and unmerged.

The current worktree adds an initial Windows host adapter for Codex CLI: host-specific protected config paths, fail-closed owner/DACL and reparse-point checks, Windows Credential Manager token setup/removal, Windows command quoting, install/remove support, documentation, and a `windows-latest` validation workflow. Windows CI on heads `436ac4d4` and `53e2d9b7` exposed an owner mismatch for newly created test/runtime files and a leaked temporary descriptor on the failure path. The current fix assigns the current-user owner to newly created protected files/directories and closes the descriptor before cleanup. The focused local Codex and Windows-portable suites passed **206 tests, 6 skipped**; the skipped tests require native Windows ACL and Credential Manager APIs. Ruff, Python compilation, and `git diff --check` passed. A fresh Windows-native CI run is required to verify the fix; Windows support remains unverified until that exact-head job passes. This does not certify a same-invocation Windows Codex phone approval.

The same worktree includes a Home masthead layout refinement and a compact/large-text bounds and heading-semantics instrumentation assertion. Kotlin app compilation and Android instrumentation-test compilation passed. Emulator instrumentation did not run: existing build intermediates contain an AppleDouble `._drawable` entry, which was left untouched. This is presentation work only and does not close Android release acceptance.

Neither change extends the older phone E2E result from source revision `3b6cc103` to the current source head. The post-fix native Codex denial output still needs a fresh owner-decided phone run. Codex Desktop remains unsupported/unverified. Slice 4, Slice 5, and the overall production objective remain open. No cloud resource, billing, deployment, or merge action occurred.
