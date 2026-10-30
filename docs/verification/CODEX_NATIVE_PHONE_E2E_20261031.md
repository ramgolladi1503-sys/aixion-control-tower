# Codex CLI to Control Tower phone E2E

Date: 2026-10-31  
Repository source: `f254658cfca66d3d9475edca98345db13aaaa8c8`  
Pull request: #204, draft and unmerged

## Result

The current Codex `PermissionRequest` hook completed both a phone approval and a phone denial against the Android debug app. The hook source was loaded from this repository revision. Both decisions were bound to the exact native Codex invocation that produced the request.

This is a local integration proof. The API process was bound to `/private/tmp/aixion-live-demo-20261031/aixion-test.sqlite3` on `127.0.0.1:8012`; it was not a PostgreSQL or hosted deployment. The phone surface was Android emulator `emulator-5564`, package `com.aixion.controltower.debug`.

## Approve

- Approval: `approval_5a56c6758b8b`
- Provider: `CODEX`; source marked verified
- Native invocation: `codex:v1:1084682c39880459fb7e8ece0c84c3e8edac9750061028cc4be9f36de438848b`
- Reviewed command: create the unique temporary marker directory `aixion-live-approved-marker-short-20261031`
- Server result: `APPROVED`
- Audit events: `approval.created_by_agent`, `approval.decision`, `approval.delivery_claimed`
- Same-invocation result: Codex resumed and created the expected marker directory.

## Deny

- Approval: `approval_18a18f965d5d`
- Provider: `CODEX`; source marked verified
- Native invocation: `codex:v1:588ec1ba22e8a61ecd2c6ebc5648bae9be7376414822b376b298712177b82301`
- Reviewed command: create the unique temporary marker directory `aixion-live-denied-marker-proof-20261031`
- Server result: `DENIED`
- Audit events: `approval.created_by_agent`, `approval.decision`
- Same-invocation result: Codex reported `Rejected("Control Tower decision: DENIED")`; the marker directory does not exist.

## Recording artifacts

The Android emulator screen recordings are saved in the user Downloads folder:

- `Aixion-live-Codex-phone-approval.mp4` — 29.5 seconds
- `Aixion-live-Codex-phone-denial.mp4` — 20.3 seconds

The denial clip shows the exact request and the closed decision screen. The API audit and Codex session record are the authoritative evidence for the final `DENIED` result and the blocked invocation.

## Scope

This closes the current-source Codex CLI phone approve/deny round-trip evidence only. It does not establish:

- Antigravity behavior on the current hook revision
- Interception of Codex Desktop-owned sessions
- Windows native-provider behavior
- Off-LAN API availability, HTTPS, push credentials, or locked-phone delivery
- PostgreSQL production authority, production import/cutover, or backup recovery
- A signed Android release, Play acceptance, or production readiness
