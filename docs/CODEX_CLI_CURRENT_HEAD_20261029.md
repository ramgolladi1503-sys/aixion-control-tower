# Codex CLI Current-Head Protocol Verification

**Date:** 2026-10-29 (Asia/Kolkata)
**Repository:** `ramgolladi1503-sys/aixion-control-tower`
**PR:** [#204](https://github.com/ramgolladi1503-sys/aixion-control-tower/pull/204), open, draft, unmerged
**Source revision:** `8f6a1c28efb84faa708dd4ef2a96065e482c0b00`
**Codex surface:** Codex CLI `0.163.0-alpha.11.2`; Codex Desktop is not tested.

## Current-source validation

The Codex CLI hook, setup, request-schema, Windows-portable security, backend permission-flow, HTTP integration, PostgreSQL restart, installed-hook, and credential-revocation suites were run from the current PR worktree at the source revision above.

```text
218 passed, 6 skipped in 67.17s
```

The six skips require native Windows ACL or Windows Credential Manager APIs and are exercised by the separate Windows CI job. At this exact revision, GitHub's **Codex Windows validation** job passed. The PostgreSQL-backed tests in this run used the existing disposable local PostgreSQL 17 cluster at `/tmp/aixion-codex-native-e2e-20261029`, database `aixion_pr204_full_20261029`; this is separate from the local API database and contains no production data.

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

This verifies the selected local contract and PostgreSQL failure/restart subgates on current source. It does not replace the full backend suite or prove native Windows runtime behavior beyond its passing CI job.

## Live provider evidence boundary

The recorded Codex CLI phone approve/deny round trips in [the original E2E checkpoint](CODEX_CLI_ANDROID_E2E_20261029.md) used source revision `3b6cc1034b537835b852e43ee8daccd586d23145`, not this current revision. Those observations remain valid only for that recorded revision. They do not establish current-head same-invocation continuation.

The current live Codex gate still needs a fresh human-decided phone approval and denial, with the exact hook output and same invocation outcome captured. Timeout, API/backend failure, changed payload, credential revocation, and connector/backend restart also need current-source live evidence where the gate requires a provider-level run. Do not autonomously choose the phone decision. Codex Desktop remains unsupported/unverified unless a documented native permission hook is proven separately.

## Scope

No production database, cloud resource, billing setting, deployment, or merge was used or changed. No new configuration key or migration was introduced. The six native-Windows-only cases remain explicitly skipped in the local macOS run; the exact-head Windows CI job passed.
