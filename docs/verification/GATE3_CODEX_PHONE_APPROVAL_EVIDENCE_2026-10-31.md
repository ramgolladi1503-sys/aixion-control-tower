# Gate 3 Codex phone approval evidence — 2026-10-31

## Result

This note records failed or non-certifying attempts only. It does not supersede the separate successful current-source Codex CLI approval and denial evidence in `CODEX_NATIVE_PHONE_E2E_20261031.md`.

The Codex CLI approve/deny flow has a documented same-invocation pass. Gate 3 remains partial because current Antigravity same-invocation proof is missing. This evidence does not establish Codex Desktop-owned session interception or Windows native-provider behavior.

## Test context for Attempts A and B

- Repository source: `ram/pre-billing-readiness-snapshot` at `1d759d2ad58d73d4ffc357ec7228f2a499ced9fc`; PR #204 remained draft and unmerged.
- Codex executable: the Codex CLI bundled with `ChatGPT.app`, version `0.163.0-alpha.11.2`.
- Isolated workspace: `/private/tmp/aixion-live-codex-e2e-20261031/codex-workspace`.
- Connector API: local `http://127.0.0.1:8012`; `/health` returned HTTP 200. Its test database was `/private/tmp/aixion-live-demo-20261031/aixion-test.sqlite3`. This is local evidence, not hosted or off-LAN delivery.
- The hook preflight passed earlier for the configured Codex agent/project. This does not certify an approval round trip.
- Attempts A and B targeted unique marker files under `/Volumes/TradeBot/AixionControlTower/recordings/20261031/`; neither marker was created.

## Attempt A

- Approval: `approval_f494bd5bd34a` (short ID `bd5bd34a`).
- Exact tool action: `touch /Volumes/TradeBot/AixionControlTower/recordings/20261031/codex-phone-approve-marker-20261031`.
- The Android app displayed the request and exact tool arguments. An approve action was attempted after review acknowledgement, but the app reported that the decision was not confirmed and the server still showed the request pending.
- The local test database later showed the approval as `EXPIRED`; no matching approval-decision audit event was found. The marker remained absent.
- The Codex invocation ended with `Connector liveness could not be confirmed`. It did not resume and execute the command.
- A 120-second desktop screen capture was saved at `/Volumes/TradeBot/AixionControlTower/recordings/20261031/codex-phone-approval-live-20261031.mov`. It records the failed attempt and is diagnostic only; it is not a publishable successful demo.

## Attempt B

- Approval: `approval_57b8e6e4cf23` (short ID `e6e4cf23`).
- Exact tool action: `touch /Volumes/TradeBot/AixionControlTower/recordings/20261031/codex-phone-approve-marker-20261031-r2`.
- The bundled Codex CLI created the approval. Before the phone could decide it, ADB lost the connected device. At the subsequent check, both listed Android emulator transports were offline and the Pixel serial was absent.
- The same Codex invocation ended with `Control Tower decision: EXPIRED`; it reported that the marker was not created.
- The approval record is `EXPIRED`; this is neither an approve nor a deny result.

## Attempt C

- Approval: `approval_2a5287aea73e`.
- Native invocation: `codex:v1:37111f6e961393bdb5c0ab3099d28d3982e8a81f5a26355497fe53ffcd6a56b5`.
- Requested tool action: `touch /Users/madhuram/Downloads/AixionControlTower-recordings/live-demo-20261031/current-head/phone-approval-retest-20261031.marker`.
- The isolated API database records the request as created at `2026-10-31T09:27:13.951140Z` and expired at `2026-10-31T09:37:14.245880Z`.
- The database contains `approval.created_by_agent` and `approval.expired` for this request; it contains no `approval.decision` or `approval.delivery_claimed` event for it.
- The user reported selecting Approve in the emulator. At the authoritative database check, the request was already `EXPIRED`; therefore the reported tap is not evidence of a committed decision for this invocation.
- The requested marker is absent. This attempt did not prove that the same invocation resumed.

The source revision for Attempt C is not independently recorded in its invocation evidence. Do not use this attempt to claim current-head adapter certification.

## Non-certifying attempt

A separate non-interactive `codex exec` command received an operating-system sandbox `Operation not permitted` result for an outside-workspace path. It did not create an Aixion phone approval and does not count as adapter evidence.

## Required evidence for remaining Gate 3 work

For Antigravity, record a fresh current-hook request and prove all of the following for the same invocation ID:

1. The request is shown in Control Tower with the exact provider, tool, arguments, and invocation context.
2. The phone decision is durably committed and visible in the backend.
3. Approve resumes that same invocation and produces only its requested marker; deny stops that same invocation and produces no marker.
4. The evidence records request/decision IDs, timestamps, source revision, and final invocation result.

Do not reuse expired approvals. Keep the production startup guard enabled until all production gates pass.

## Change and validation record

- Application code: unchanged.
- Configuration keys: none added or changed.
- Database schema or migration: none.
- Cloud, billing, DNS, and deployment: unchanged.
- Automated tests: not run; this is an evidence update.
- Documentation validation: `git diff --check`.
- Rollout: none. This note records failed attempts and does not authorize release or cutover.
