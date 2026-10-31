# Codex Native Permission E2E — 2026-10-30

## Result

The current Codex `PermissionRequest` hook completed one same-invocation approval round trip through the Control Tower Android emulator and an isolated PostgreSQL-backed local API. A second same-invocation request expired without a phone decision and remained blocked.

## Evidence

- Source worktree: `/Volumes/TradeBot/worktrees/aixion-codex-approval-roundtrip-20261029`
- Source revision: `0538b791a70eb82742da942488e0ac488747a6a1`
- Hook: `integrations/codex/aixion_permission_request_hook.py` from that revision
- Codex CLI: `0.163.0-alpha.11.2`, isolated workspace `/Volumes/TradeBot/workspaces/aixion-codex-account-e2e-20261030`
- Approval: `approval_bcf77dbfd3df`, created for the Codex Bash permission request to run `touch /Volumes/TradeBot/workspaces/aixion-codex-account-e2e-20261030/current-head-approved-marker-20261030-03`
- Phone action: the user tapped **Approve** in Control Tower on Android emulator `emulator-5558`
- Durable decision: the isolated PostgreSQL row transitioned from `PENDING_REVIEW` to `APPROVED` at `2026-10-30 08:15:09.499650 +05:30`
- Durable delivery: PostgreSQL recorded `approval.delivery_claimed` for this approval and its source invocation at `2026-10-30 08:15:10.551278 +05:30`
- Same-invocation result: the Codex hook process exited, the Codex CLI returned to idle, and the exact zero-byte marker appeared at `08:15:11 +05:30`, after the durable delivery claim
- Marker did not exist before the decision
- Expiry request: `approval_2ce511ce6cb7`, created for `touch /Volumes/TradeBot/workspaces/aixion-codex-account-e2e-20261030/current-head-denied-marker-20261030-01`
- Expiry result: PostgreSQL recorded `EXPIRED` and `approval.expired`; Codex reported `Blocked by hook` / `Connector liveness could not be confirmed`; no delivery claim was recorded and the marker remained absent
- The API health endpoint returned `{"status":"ok","service":"aixion-control-tower"}` during the run
- Current Codex adapter regression suites passed: **149 passed, 6 skipped** across hook CLI, permission schema, Windows security, and setup-workspace tests
- PR #204 exact-head CI was green at source revision `0538b791a70eb82742da942488e0ac488747a6a1`; the later documentation commit `2190817ef94f77174b4f92e57a94f30d96ae76e8` triggered a new CI run that was still in progress at the last check

## Scope and limitations

This verifies one current-source Codex CLI approval path, one durable delivery claim, and resumption of the associated command invocation in an isolated local test environment. It also verifies that a second invocation which received no decision expired and was blocked without creating its marker. The emulator app and API were connected to the local test service; the API process was running from the separate `android-design` worktree at revision `a35aa617` with a modified working tree. This run does not certify the backend from source revision `0538b791`, hosted HTTPS, push delivery to a locked physical phone, an explicit phone Deny decision on this source revision, or all outage/revocation/restart/workspace-mismatch/replay cases. It does not establish interception of approvals from Codex Desktop-owned sessions.

No production database or cloud resource was used or changed. No billing, deployment, DNS, merge, or release action occurred. The connector token and credential material are intentionally omitted.

## Reproduction outline

Use a disposable PostgreSQL database and a dedicated Codex CLI workspace. Register a test-only connector, install the current `PermissionRequest` hook, and issue one harmless unique marker command through the native Codex CLI callback. Decide that exact request in the Control Tower app. Verify the PostgreSQL decision, matching delivery claim and invocation identity, callback completion, and marker creation timestamp in that order. For an expiry test, do not decide the request; confirm that it expires, the hook blocks the callback, and the marker remains absent. Never create the marker manually or report an approval based only on the UI response.

