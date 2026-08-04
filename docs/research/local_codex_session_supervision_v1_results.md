# Local Codex Session Supervision v1 Results

## Verdict

IMPLEMENTED_NOT_CERTIFIED

## Commit Range

- Task-stated starting commit: `ff0f3d0c9271302f980b31db4f766d1b051e2cc4`
- Actual observed starting commit: `e51658ff2d27f14b011f21a993079c7a5d95736d`
- Final commit: see final response for the pushed branch HEAD. This file is part of the final commit, so embedding that same commit hash here would make the hash stale.

## Scope Completed

- Added backend session-origin model values for `HOST_STARTED` and `MOBILE_STARTED`.
- Added backend approval-source model values for `MAC`, `ANDROID`, `POLICY`, and `SYSTEM`.
- Added relay-authenticated host-started session registration at `POST /connectors/relay-hosts/{relay_id}/local-sessions`.
- Host-started registration validates relay authentication, relay online status, adapter availability, workspace roots, repository allowlist, project/task/run existence, active-session limit, and idempotency.
- Host-started registration does not enqueue `START_SESSION`.
- Added relay client `register_local_session(...)` with explicit idempotency key and existing backend error propagation.
- Refactored Codex app-server lifecycle enough to separate JSON-RPC initialization, thread creation, turn start, process creation, and process close.
- Existing backend-command-started Codex path still starts JSON-RPC, creates one thread, and starts the objective turn.
- Added provider-supported decision preservation for Codex approvals, including `acceptForSession` when Codex offers it.
- Added fail-closed rejection when policy attempts to select a provider decision unavailable in the native Codex request.
- Preserved additional provider approval metadata for Android/backend display readiness: provider method, item ID, cwd, reason, sandbox, network, policy amendment, thread ID, turn ID, raw payload, and available decisions.

## Changed Files

- `backend/app/relay_models.py`
- `backend/app/relay_routes.py`
- `backend/app/relay_service.py`
- `backend/tests/test_relay_routes.py`
- `backend/tests/test_relay_service.py`
- `relay/aixion_relay/adapters/codex.py`
- `relay/aixion_relay/client.py`
- `relay/tests/test_client.py`
- `relay/tests/test_provider_adapters.py`
- `docs/research/local_codex_session_supervision_v1_results.md`

## Focused Tests Run

```text
cd backend
. .venv/bin/activate
pytest tests/test_relay_service.py tests/test_relay_routes.py
```

Result: `11 passed, 1 warning`

```text
cd relay
. .venv/bin/activate
pytest tests/test_client.py
```

Result: `3 passed`

```text
cd relay
. .venv/bin/activate
pytest tests/test_provider_adapters.py tests/test_runtime.py
```

Result: `7 passed`

```text
cd relay
. .venv/bin/activate
pytest tests/test_provider_adapters.py
```

Result: `7 passed`

## Full Regression

Not run yet.

## Disposable Repository Smoke Evidence

Not run. No disposable-repository Android/Codex smoke identifiers exist yet for this branch.

## Continuity Evidence

Automated unit evidence exists for:

- host-started session idempotency;
- no `START_SESSION` command for host-started registration;
- separated Codex thread and turn lifecycle;
- multiple explicit local prompt turns on one thread;
- provider-supported decision preservation.

Real process/thread/turn continuity evidence is not yet available.

## Known Limitations

- `aixion-relay codex` local supervisor CLI has not been implemented yet.
- Android host-started session UI has not been implemented yet.
- Backend atomic Mac-or-phone compare-and-set approval resolution has not been fully implemented yet.
- Provider payload hash and immutable action ID are not yet enforced by a dedicated action-resolution store.
- No real Android approve, reject, race, backend-loss, or session-allowance smoke test has been run.
- No stacked draft PR has been opened yet.

## Final Verdict Rationale

The branch has partial implementation and focused automated tests for slices 1 through part of slice 3. It is not certified. The original vision is not proven until a real disposable repository smoke test demonstrates one Mac-started supervised Codex app-server process, one relay session, one Codex thread, exact native approval mirroring, first-valid-decision wins, no duplicate provider response, and no second phone-created session.
