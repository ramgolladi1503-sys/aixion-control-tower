# Gate 3 Codex Current-Head Adapter Check — 2026-11-01

## Result

**Adapter-source equivalence and local failure-path tests pass.** This is supporting evidence only; it is not a fresh native invocation or production certification.

## Current-source evidence

- The recorded same-invocation Codex CLI approval/denial run is documented in `docs/verification/CODEX_NATIVE_PHONE_E2E_20261031.md`, with adapter source revision `f254658cfca66d3d9475edca98345db13aaaa8c8`.
- At current PR head `2d7fd68a`, SHA-256 content hashes for `integrations/codex/aixion_permission_request_hook.py`, `integrations/codex/setup_workspace.py`, and `integrations/codex/host_security.py` match the recorded E2E revision exactly.
- Installed Codex CLI reports version `0.146.0`; its feature list reports `hooks` as stable and enabled.
- Focused current-source test command `python3 -m pytest -q integrations/codex/test_codex_hook_cli.py integrations/antigravity/test_antigravity_hook_cli.py` completed with **253 passed** in 7.04 seconds.

## Scope limits and next proof

- These tests exercise hook behavior with controlled test doubles. They do not prove a fresh request resumed or stopped a native Codex task this turn.
- The Android emulator is connected with the Control Tower debug app running. The local API ports used by prior demos were not listening at this check. Codex Desktop UI automation is unavailable in this session; no attempt was made to bypass that restriction or touch the active TradeBot Antigravity session.
- Re-run the same-invocation approve and deny through the supported native Codex CLI callback against the current backend, then verify the decision, matching invocation and delivery/audit records, and command marker ordering. Repeat outage, timeout, payload mismatch, workspace mismatch, credential revocation, restart, and replay cases end-to-end. The test suite alone does not close these runtime cases.
- Codex Desktop and native Windows provider behavior remain outside this evidence.
- No application code, configuration, credentials, database, cloud resource, or Android state was changed by this check. No new test was added.
