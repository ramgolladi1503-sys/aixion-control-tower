# Task: Local Codex Session Supervision v1

## Repository and branch

- Repository: `ramgolladi1503-sys/aixion-control-tower`
- Base branch: `feature/universal-agent-relay-v1`
- Implementation branch: `feature/local-codex-session-supervision-v1`
- Starting commit: `ff0f3d0c9271302f980b31db4f766d1b051e2cc4`
- Parent draft PR: `#190`
- Do not merge either branch.

## Primary objective

Implement the corrected Aixion product flow described in:

```text
docs/LOCAL_CODEX_SESSION_SUPERVISION.md
```

The user must start Codex from the Mac through an Aixion-owned entry point, enter prompts locally, leave the Mac, receive the exact native Codex approval request on Android, approve or reject it from the phone, and have the same Mac-started Codex process/thread/turn continue.

Do not implement another phone-started session flow. That already exists and is only the secondary workflow.

## Non-negotiable product invariant

```text
One Mac-started Aixion-supervised Codex app-server process
+ one relay session
+ one Codex thread
+ exact native approval mirroring
+ first valid Mac-or-phone decision wins
+ no duplicate provider response
+ no second phone-created session
```

## Execution rules

1. Read the full implementation contract before changing code.
2. Inspect the existing relay, backend, Android, and tests before designing interfaces.
3. Reuse the proven approval interception path. Do not replace it with terminal scraping, AppleScript, SSH, remote desktop, synthetic keyboard input, or polling the Codex screen.
4. Keep PR #190 behavior working.
5. Work only on `feature/local-codex-session-supervision-v1`.
6. Do not merge, deploy, change production secrets, or modify TradeBot.
7. Do not claim completion without automated tests plus a real disposable-repository smoke test.
8. Treat the current Claude adapter as out of scope.
9. Preserve fail-closed behavior for all side effects.
10. Do not invent an unrestricted permanent `always allow` action.

## Required implementation slices

### Slice 1 — Session origin and host-started registration

Backend:

- Add session origin values:
  - `HOST_STARTED`
  - `MOBILE_STARTED`
- Existing mobile-created sessions default to `MOBILE_STARTED`.
- Add a relay-authenticated endpoint for host-started session registration, recommended:

  ```text
  POST /connectors/relay-hosts/{relay_id}/local-sessions
  ```

- Validate relay token, relay status, adapter, workspace containment, repository allowlist, project allowlist, and active-session limits.
- Host-started registration must not enqueue `START_SESSION`.
- Support an idempotency key so a retry cannot create a duplicate session.
- Bind the session to the creating relay.

Relay client:

- Add `register_local_session(...)`.
- Preserve backend error detail.
- Add idempotency support.

Tests:

- success;
- invalid token;
- disabled relay;
- workspace outside allowlist;
- repository outside allowlist;
- unavailable adapter;
- duplicate idempotency key;
- verify no `START_SESSION` command exists.

Stop and run focused tests before continuing.

### Slice 2 — Refactor Codex app-server lifecycle

Refactor `relay/aixion_relay/adapters/codex.py` so these operations are separate and reusable:

1. create app-server process;
2. initialize JSON-RPC;
3. create or resume thread;
4. start a turn;
5. stream normalized events;
6. request approval;
7. resolve approval exactly once;
8. interrupt/cancel;
9. close process.

Current phone-started behavior must continue to work.

Do not automatically require an objective during object construction. A host-started local supervisor must be able to create the backend session and thread before the first local prompt.

Tests:

- exactly one child process;
- exactly one `thread/start` call;
- multiple local prompts create turns, not sessions;
- existing backend-command-started flow remains green;
- clean shutdown terminates the child process.

### Slice 3 — Provider decision fidelity

The current binary mapping is insufficient.

Preserve provider-supported decisions from each Codex approval request, including when present:

- `accept`;
- `acceptForSession`;
- supported policy amendment acceptance;
- `decline`;
- `cancel`.

Persist enough metadata to display:

- provider method;
- command or file paths;
- cwd;
- reason;
- sandbox/filesystem scope;
- network scope/domains;
- policy amendment;
- provider item ID;
- thread ID;
- turn ID;
- available decisions.

Aixion policy may narrow or block a provider choice. It must never broaden it.

Tests:

- exact decision mapping;
- unavailable decision rejected;
- provider payload hash stable;
- no duplicate JSON-RPC response.

### Slice 4 — Atomic Mac-or-phone resolution

Add approval-resolution semantics:

- immutable action ID plus payload hash;
- `PENDING -> RESOLVED` compare-and-set;
- first valid decision wins;
- loser receives `already resolved`;
- winning source recorded as `MAC`, `ANDROID`, `POLICY`, or `SYSTEM`;
- actor, timestamp, decision, action ID, and payload hash audited.

Add a relay-authenticated endpoint for local Mac resolution if the current trust action route cannot safely represent the source.

The supervisor must race:

- local terminal decision future;
- backend/mobile decision future.

Exactly one winner may return a response to the pending Codex JSON-RPC request.

Tests:

- Mac wins;
- Android wins;
- simultaneous race;
- idempotent duplicate tap;
- loser cannot overwrite;
- only one provider response written.

### Slice 5 — Local supervisor CLI

Add:

```bash
aixion-relay codex
```

Arguments:

- `--workspace`, default current directory;
- `--repository` optional, auto-detect where reliable;
- `--model` optional;
- `--approval-mode`, default `STRICT`;
- `--max-runtime-seconds`;
- `--no-local-approval` for unattended tests.

Behavior:

1. Load relay config and keychain token.
2. Validate workspace locally before starting Codex.
3. Start one Codex app-server process.
4. Register one host-started backend session.
5. Create one Codex thread.
6. Print relay session ID, process PID, thread ID, workspace, and repository.
7. Present a local prompt loop.
8. Render useful assistant output locally.
9. Show exact pending approval locally.
10. Permit approve-once, supported session allowance, reject, cancel, and exit.
11. Continue the same turn after Android approval.
12. On backend loss during approval, remain paused and fail closed.
13. On `Ctrl+C`, interrupt the active turn first; a second interrupt may exit cleanly.

A full clone of the official Codex TUI is not required. Correct process ownership, prompt entry, output, approval, and continuity are required.

Tests:

- workspace validation;
- registration before first turn;
- one process/thread;
- local prompt -> turn;
- Android approval -> same turn continues;
- local approval -> Android observes resolved;
- backend loss fails closed;
- cancellation and shutdown.

### Slice 6 — Android host-started experience

Update DTOs, repository, ViewModel, and UI.

Show:

- `Started on Mac` / `Started on phone`;
- host name;
- workspace;
- repository;
- provider process/thread identity where safe;
- exact approval request details;
- only provider-supported choices allowed by Aixion policy;
- `Resolved on Mac` when the local terminal wins;
- `Already resolved` for a losing phone tap;
- timeline continuity after approval.

Do not require the user to create a new phone session for a host-started session.

Tests:

- origin badge;
- conditional buttons;
- exact payload rendering;
- already-resolved state;
- continuity after approval.

### Slice 7 — End-to-end disposable-repository certification

Create or use a disposable repository. Do not use TradeBot for the first certification.

Start from Mac:

```bash
cd <disposable-repository>
aixion-relay codex
```

Run a harmless task requiring one shell command that creates one test file.

Capture and persist:

- Mac supervisor PID;
- Codex app-server PID;
- relay session ID;
- Codex thread ID;
- Codex turn ID;
- action ID;
- action payload hash;
- pre-approval proof that file is absent;
- Android pending approval screenshot/evidence;
- approval source;
- post-approval proof that exact file exists;
- proof the same process/thread/turn continued;
- proof no second session/process was created;
- event-chain verification.

Then run:

1. Android approve path.
2. Mac approve path.
3. Android reject path.
4. Mac/Android race path.
5. backend disconnect while approval is pending.
6. session-scoped allowance path only when Codex offers it.

## Required regression gates

Run all relevant existing suites:

```text
backend lint and tests
relay lint and tests
Android unit tests
Android compile/build
container/CI checks already defined by PR #190
```

Do not weaken tests, skip failing tests, or modify unrelated behavior to force green status.

## Required evidence report

Create:

```text
docs/research/local_codex_session_supervision_v1_results.md
```

It must include:

- starting and final commit SHA;
- exact commands run;
- changed files;
- focused and full test counts;
- real smoke-test identifiers;
- process/thread/session continuity evidence;
- approve/reject/race/backend-loss results;
- known limitations;
- explicit final verdict.

Allowed verdicts:

- `CERTIFIED_LOCAL_CODEX_SESSION_SUPERVISION_V1`
- `IMPLEMENTED_NOT_CERTIFIED`
- `BLOCKED_BY_CODEX_INTERFACE_LIMITATION`
- `FAILED_SAFETY_OR_CONTINUITY_GATE`

Do not use the certified verdict unless every acceptance gate passes with retained evidence.

## Commit and PR discipline

- Keep changes on `feature/local-codex-session-supervision-v1`.
- Commit by coherent slice.
- Push the branch.
- Open a draft stacked PR into `feature/universal-agent-relay-v1` only after focused tests are green.
- Do not merge.
- Parent PR #190 remains draft.

## Final response format

Return:

1. final verdict;
2. branch and final SHA;
3. stacked draft PR number;
4. exact files changed;
5. exact tests and counts;
6. E2E evidence identifiers;
7. unresolved limitations;
8. whether the original vision is now proven without creating a second Codex session.
