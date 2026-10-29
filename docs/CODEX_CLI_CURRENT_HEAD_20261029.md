# Codex CLI Current-Head Protocol Verification

**Date:** 2026-10-29 (Asia/Kolkata)
**Repository:** `ramgolladi1503-sys/aixion-control-tower`
**PR:** [#204](https://github.com/ramgolladi1503-sys/aixion-control-tower/pull/204), open, draft, unmerged
**PR base revision:** `faa559c1d0452e30dde7cd07cfcc99f173780f01` (Codex application implementation unchanged; outage regression added in the verification worktree)
**Codex surface:** Codex CLI `0.163.0-alpha.11.2`; Codex Desktop is not tested.

## Current-source validation

The Codex CLI hook, setup, request-schema, Windows-portable security, backend permission-flow, HTTP integration, PostgreSQL restart, installed-hook, and credential-revocation suites were run from the current PR worktree based on the revision above, including the new API-outage regression test.

```text
219 passed, 6 skipped in 77.44s
```

The six skips require native Windows ACL or Windows Credential Manager APIs and are exercised by the separate Windows CI job. GitHub's **Codex Windows validation** job passed on base PR head `faa559c1`; the new test-only checkpoint still needs exact-head CI. The PostgreSQL-backed tests in this run used the existing disposable local PostgreSQL 17 cluster at `/tmp/aixion-codex-native-e2e-20261029`, database `aixion_pr204_full_20261029`; this is separate from the local API database and contains no production data.

The local run covered these files:

- `integrations/codex/test_codex_hook_cli.py`
- `integrations/codex/test_setup_workspace.py`
- `integrations/codex/test_codex_permission_request_schema.py`
- `integrations/codex/test_codex_windows_security.py`
- `backend/tests/test_codex_permission_request_hook.py`
- `backend/tests/test_codex_permission_request_process_flow.py`
- `backend/tests/test_codex_hook_http_integration.py`
- `backend/tests/test_postgres_codex_permission_restart.py`
- `backend/tests/test_postgres_codex_installed_hook.py`
- `backend/tests/test_postgres_codex_revocation_process.py`

The added `test_codex_permission_hook_denies_within_deadline_when_api_drops_pending_wait` regression runs the installed `PermissionRequest` hook against an isolated local API, confirms a real pending approval exists, terminates that test-owned API process, and asserts the hook returns an explicit deny within its bound. The command marker remains absent and no delivery claim is recorded. This verifies the local API-outage fail-closed subgate; it is not a human phone decision or a live native Codex session.

This verifies the selected local contract and PostgreSQL failure/restart subgates on current source. It does not replace the full backend suite or prove native Windows runtime behavior beyond its passing CI job.

## Live provider evidence boundary

The recorded Codex CLI phone approve/deny round trips in [the original E2E checkpoint](CODEX_CLI_ANDROID_E2E_20261029.md) used source revision `3b6cc1034b537835b852e43ee8daccd586d23145`, not this current revision. Those observations remain valid only for that recorded revision. They do not establish current-head same-invocation continuation.

The current live Codex gate still needs a fresh human-decided phone approval and denial, with the exact hook output and same invocation outcome captured. The API-outage behavior is now covered by a local process integration regression, but provider-level live evidence for timeout, changed payload, credential revocation, and connector/backend restart remains open where the gate requires it. Do not autonomously choose the phone decision. Codex Desktop remains unsupported/unverified unless a documented native permission hook is proven separately.

## Scope

No production database, cloud resource, billing setting, deployment, or merge was used or changed. No new configuration key or migration was introduced. The six native-Windows-only cases remain explicitly skipped in the local macOS run; the Windows CI job passed on the base PR head, and must rerun for this test-only checkpoint.
