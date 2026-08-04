# Local Codex Session Supervision

## Status

Draft implementation contract for PR #190.

This document corrects the primary product flow. The current mobile-started provider session flow remains useful, but it is secondary.

## Product objective

A developer starts Codex from the Mac inside a repository, gives Codex a prompt, and leaves the Mac unattended. When that same Codex session requests permission to run a command, modify files, use the network, or expand its policy, Aixion must forward the exact approval request to Android. The developer can approve or reject from the phone, and the same Mac-started Codex session must continue.

The required flow is:

```text
Mac: cd ~/tradebot
Mac: aixion-relay codex
Mac: enter a normal Codex prompt
Mac: leave the room
Codex: requests an approval
Aixion: sends the exact request and supported choices to Android
Phone: user resolves the approval
Aixion: returns that exact decision to the same Codex app-server request
Codex: the original Mac-started thread continues
```

## What is already proven

The current branch has already proven these primitives in a real local environment:

- Android can see an outbound-only Mac relay.
- The relay can start Codex app-server in an allowed workspace.
- Codex command approvals can be intercepted by Aixion.
- Execution can remain paused while a phone approval is pending.
- An exact phone approval can be returned to Codex.
- The command can execute only after approval.
- Ordered provider events and completion evidence can be persisted.

Those primitives must be reused. They must not be replaced with terminal automation, screen scraping, AppleScript, SSH, or synthetic clicks.

## Current scope drift

The current Codex adapter starts a new provider process only after a mobile-created `START_SESSION` command. It calls `thread/start` itself and sends the phone-provided objective as the first turn.

That proves mobile-started execution, but it does not satisfy the primary product objective because:

- the session originates on the phone rather than the Mac;
- it is not the session the developer started while working locally;
- the normal local interactive workflow is absent;
- the mobile client is currently the only practical session creator.

## Required product behavior

### 1. Mac-started supervised entry point

Add a relay CLI command:

```bash
aixion-relay codex
```

The command must:

1. Resolve the current working directory.
2. Require it to be inside an allowed workspace root.
3. Resolve the repository identity where possible.
4. Start exactly one Codex app-server process.
5. Register a host-started relay session with Aixion.
6. Start exactly one Codex thread for that session.
7. Provide a local interactive prompt loop.
8. Render normalized Codex output locally while also persisting it to Aixion.
9. Intercept native Codex approval requests.
10. Allow the approval to be resolved from the Mac or Android.
11. Return the first valid decision to the same pending JSON-RPC request.
12. Keep the same provider process, relay session, thread ID, and workspace for the lifetime of the local session.

The user must not need to create a second phone-started session.

### 2. Primary and secondary workflows

Primary:

```text
Mac-started, Aixion-supervised Codex session
```

Secondary:

```text
Phone-started Codex session
```

The existing Android `Start strict governed session` flow must remain available, but the Android screen must clearly distinguish:

- `HOST_STARTED`
- `MOBILE_STARTED`

### 3. No attachment to an unmanaged TUI in v1

V1 must not claim that Aixion can safely take ownership of an arbitrary Codex TUI session that was started with the ordinary unmanaged `codex` command.

The supported rule is:

> Aixion must supervise the Codex app-server connection from process start.

A shell alias may later make `codex` invoke `aixion-relay codex`, but the real executable must remain reachable for the supervisor to launch `codex app-server` without recursion.

### 4. Local terminal experience

The Mac command must support at minimum:

- entering a prompt;
- viewing assistant output;
- viewing command/file approval details;
- approving once locally;
- rejecting locally;
- cancelling the active turn;
- exiting the supervised session;
- continuing to receive output after a phone approval.

A full clone of the official Codex TUI is not required for v1. Correct ownership and approval behavior are required.

### 5. Exact native approval mirroring

For every Codex approval request, persist and display the provider payload required to make an informed decision, including when present:

- provider method;
- command text or file paths;
- working directory;
- command reason;
- requested sandbox/filesystem scope;
- requested network scope/domains;
- proposed policy amendment;
- Codex item ID;
- thread ID;
- turn ID;
- provider-supported decisions.

The Android UI must not invent a broader choice than Codex supplied.

Examples:

```text
Approve once
Allow for this session
Reject
Cancel task
```

Only show `Allow for this session` when the provider request supports that decision.

### 6. Decision fidelity

The existing binary mapping:

```text
ALLOW -> accept
anything else -> decline
```

is insufficient for the corrected vision.

Introduce a provider-resolution value that can preserve, where offered:

- `accept`
- `acceptForSession`
- supported policy amendment acceptance
- `decline`
- `cancel`

Aixion policy remains authoritative. A provider option can be narrowed or blocked by Aixion, but Aixion must never broaden the provider request.

### 7. First-valid-decision wins

An approval may be visible on the Mac and Android simultaneously.

Resolution semantics:

1. Approval action is immutable and identified by an action ID plus payload hash.
2. The first valid, authorized decision atomically changes it from `PENDING` to `RESOLVED`.
3. The winning decision is returned to the pending Codex JSON-RPC request.
4. Later decisions receive an `already resolved` result and must not be sent to Codex.
5. Event history records the winner, source (`MAC` or `ANDROID`), actor, timestamp, action ID, and payload hash.

### 8. Host-started session registration

Add a relay-authenticated backend operation for a registered relay to create a session it is already hosting.

Recommended shape:

```text
POST /connectors/relay-hosts/{relay_id}/local-sessions
```

The backend must validate:

- relay token;
- relay status;
- adapter availability;
- absolute workspace path;
- workspace allowlist containment;
- repository allowlist;
- project allowlist when supplied;
- maximum active session count;
- session origin is `HOST_STARTED`;
- remote/provider identifiers are bound only to that relay.

A host-started session must not enqueue a `START_SESSION` command back to the relay. The process and thread already exist locally.

### 9. Local supervisor lifecycle

The local supervisor owns:

- Codex app-server child process;
- JSON-RPC connection;
- thread ID;
- current turn ID;
- local prompt loop;
- approval futures;
- event sequencing;
- backend heartbeat;
- clean shutdown.

It must register the backend session before sending the first user turn so that all events and approvals have a durable session identity.

If registration fails, do not continue in an ungoverned mode.

### 10. Failure behavior

Fail closed for side effects.

Required handling:

- Backend unavailable before session start: do not start work.
- Backend disconnect during ordinary text streaming: show degraded state and retry within a bounded window.
- Backend unavailable during approval: keep the Codex request paused; never auto-approve.
- Approval timeout: decline or cancel according to configured strict policy.
- Relay process crash: provider process must be terminated unless durable safe resume has been independently proven.
- Duplicate local registration: reject or idempotently return the existing bound session.
- Android and Mac race: first valid decision wins.

### 11. Notifications

When a host-started session requires approval, Android must receive the same push-notification path as a mobile-started session.

Notification content must include a safe summary only. Secrets, full environment variables, tokens, and large command output must not be placed in notification payloads.

## Required code changes

### Relay CLI

File: `relay/aixion_relay/cli.py`

- Add `codex` subcommand.
- Arguments:
  - optional `--workspace`, default current directory;
  - optional `--repository`;
  - optional `--model`;
  - optional `--approval-mode`, default `STRICT`;
  - optional `--max-runtime-seconds`;
  - optional `--no-local-approval` for unattended validation.
- Build the registered relay client from existing config and keychain token.
- Start the new local supervisor.

### Relay client

File: `relay/aixion_relay/client.py`

- Add host-started session registration.
- Add explicit action-resolution support for Mac decisions.
- Add idempotency key support for local-session registration and action resolution.

### Codex adapter/supervisor

File: `relay/aixion_relay/adapters/codex.py`

Refactor so the same JSON-RPC session implementation can serve:

- backend-command-started sessions;
- host-started interactive sessions.

Required changes:

- do not force an objective during construction;
- separate process creation, thread creation, and turn creation;
- expose normalized output to both event persistence and local rendering;
- preserve provider-supported approval decisions;
- preserve complete approval metadata;
- support a local decision future racing the mobile decision future;
- guarantee only one response is returned for each JSON-RPC approval request.

A new file such as `relay/aixion_relay/local_codex.py` may hold the terminal supervisor and prompt loop.

### Backend models

File: `backend/app/relay_models.py`

Add:

```text
RelaySessionOrigin = HOST_STARTED | MOBILE_STARTED
RelayApprovalSource = MAC | ANDROID | POLICY | SYSTEM
```

Persist origin on each relay session.

Extend approval records/DTOs to preserve provider decisions and the selected provider resolution without reducing them to a binary boolean.

### Backend routes and service

Files:

- `backend/app/relay_routes.py`
- `backend/app/relay_service.py`
- `backend/app/relay_trust_binding.py`

Add:

- relay-authenticated host-started session registration;
- atomic action resolution with compare-and-set semantics;
- idempotent duplicate resolution;
- audit events for decision source and winner;
- no `START_SESSION` enqueue for host-started sessions;
- session state transitions driven by host events.

### Android

Files under:

```text
mobile/android/app/src/main/java/com/aixion/controltower/
```

Required UI behavior:

- show `Started on Mac` versus `Started on phone`;
- show current Mac workspace/repository;
- show exact command/file request details;
- show only provider-supported decision buttons allowed by Aixion policy;
- show `Already resolved on Mac` when the Mac wins;
- keep the timeline attached to the same session after approval;
- allow session cancel and follow-up messages for host-started sessions.

## Acceptance tests

### Gate A: Mac-origin proof

1. Run from the Mac:

   ```bash
   cd /Users/madhuram/tradebot
   aixion-relay codex
   ```

2. Enter a prompt locally that requires a harmless command.
3. Confirm Android shows one session with:
   - origin `HOST_STARTED`;
   - workspace `/Users/madhuram/tradebot`;
   - the same provider thread ID shown by the Mac supervisor.
4. Confirm no second Codex app-server process or second thread is created by the phone.

Pass only when the session was created on the Mac and merely observed/controlled by Android.

### Gate B: Phone approval continues the same session

1. Trigger one harmless command from the Mac-started session.
2. Confirm the command has not executed.
3. Confirm Android displays the exact request.
4. Approve once on Android.
5. Confirm the same Mac terminal prints the command output and continues the original turn.
6. Confirm process ID, relay session ID, thread ID, and turn ID continuity.

### Gate C: Local approval wins

1. Trigger another approval.
2. Approve on the Mac.
3. Confirm Android changes to `Resolved on Mac`.
4. Confirm a later phone tap cannot produce a second provider response.

### Gate D: Race safety

Simultaneously attempt Mac and Android approval.

Pass only when:

- exactly one decision wins;
- exactly one JSON-RPC response is sent;
- the loser receives `already resolved`;
- one audit record identifies the winning source.

### Gate E: Rejection

Reject from Android.

Pass only when:

- the command/file change does not occur;
- Codex receives the provider-equivalent decline;
- the same session remains usable or ends according to Codex behavior;
- evidence ordering is approval requested -> rejected -> no execution.

### Gate F: Session allowance fidelity

When Codex supplies a session-scoped allowance option:

1. Android shows that option.
2. Selecting it returns the exact provider-supported session decision.
3. The rule is limited to the intended session and command/policy scope.
4. A new session asks again.

### Gate G: Backend loss during approval

Disconnect the backend while an approval is pending.

Pass only when:

- Codex stays paused;
- no side effect occurs;
- reconnect restores the same pending action;
- timeout fails closed.

## Required automated tests

### Relay

- host-started registration success and rejection paths;
- one app-server process and one thread;
- local prompt creates turns without creating new sessions;
- Android approval resolves a pending JSON-RPC request;
- Mac approval resolves the same request;
- first-decision-wins race;
- no duplicate JSON-RPC response;
- backend loss fails closed;
- exact provider decision mapping;
- clean child-process shutdown.

### Backend

- relay-authenticated local-session create;
- allowlist validation;
- no start command for host-started sessions;
- idempotent registration;
- atomic approval resolution;
- losing resolver cannot overwrite winner;
- audit source recorded;
- session origin serialized to Android DTOs.

### Android

- host-started badge;
- exact approval fields;
- conditional decision buttons;
- already-resolved state;
- timeline continuity after phone approval.

## Non-goals for v1

- Hijacking an arbitrary unmanaged Codex TUI after it has already started.
- Remote desktop or terminal streaming.
- SSH exposure.
- Synthetic keyboard/mouse input.
- Auto-approval when the backend or phone is unavailable.
- Unbounded permanent `always allow` rules.
- Claude or Antigravity parity before Codex host-started flow is certified.

## Migration and product wording

Until all acceptance gates pass, describe the current implementation as:

> Mobile-supervised provider sessions with proven exact approval interception.

After certification, the primary product wording may become:

> Start Codex on your Mac, leave it working, and securely resolve its exact approval requests from your phone while the same session continues.

## Merge gate

PR #190 must remain draft and unmerged until Gates A through G and the required automated tests pass on a disposable repository and then on a controlled TradeBot smoke task.

Do not use TradeBot for destructive or broad validation. The first host-started validation must use a disposable repository or a no-impact test file.
