# Aixion Desktop Bridge v1

## Status

Authoritative implementation task.

## Product objective

Build the user's actual product flow:

```text
Open Aixion Desktop on the Mac
→ select a local repository
→ start a Codex task from the desktop UI
→ leave the Mac
→ exact approval appears in the Aixion Android app
→ approve or reject in Aixion
→ the same Mac process, Codex thread, and turn continue
```

The ChatGPT mobile application is not part of this product flow.

## Non-negotiable product invariant

Aixion must own the Codex app-server connection from process startup. The Mac desktop bridge and the Aixion Android app are two views over one supervised session.

Do not claim support for attaching to an arbitrary Codex Desktop session. The documented app-server contract makes the connected client responsible for answering server-initiated approval requests, and there is no proven supported contract in this repository for a third-party client to take over approvals from an already-running Codex Desktop-owned turn.

## Scope correction

The previous Codex Desktop native Remote spike is superseded. OpenAI's first-party mobile Remote experience may be recorded as competitive context, but it is not an implementation dependency and must not replace the Aixion Android approval path.

PR #191 contains useful foundation work for host-started session registration and Codex approval metadata. Reuse it only where correct; do not preserve incomplete architecture merely because it already exists.

## User experience

### Mac

The final user experience must be a desktop application or desktop companion—not a terminal prompt loop.

Minimum v1 screens:

1. **Projects**
   - Add/remove an allowed local repository.
   - Show repository path, branch, dirty state, and relay/backend connectivity.
   - Refuse paths outside configured workspace roots.

2. **New supervised task**
   - Prompt input.
   - Model selector only when supported.
   - Permission profile selector.
   - Default certification profile: approval required, workspace-write sandbox.

3. **Live session**
   - Stream assistant messages, command output, file changes, tests, and status.
   - Show process ID, thread ID, active turn ID, relay session ID, and connection state in diagnostics.
   - Show pending approval locally with the same exact choices offered on Android.
   - Permit local resolution; first valid Mac-or-Android resolution wins.

4. **Session history**
   - Show completed/failed/cancelled sessions.
   - Show durable approval and execution evidence.

The desktop app may use a menu-bar component for background status and notifications, but the user must have a usable window for project selection, prompts, and session progress.

### Android

The existing Aixion Android application remains the remote approval console.

It must show:

- Mac/relay identity;
- repository and branch;
- session/thread/turn identity;
- exact pending command or file change;
- cwd, reason, sandbox/network scope, requested permission amendment, and expiry;
- only provider-supported decisions;
- approve once;
- approve for session only when explicitly offered by Codex;
- reject/cancel;
- already-resolved state when the Mac won the race;
- reconnecting/offline/expired states;
- final execution result.

No screen may enqueue a second `START_SESSION` for an already active host-started session.

## Required architecture

```text
Aixion Desktop Bridge
        │ owns
        ▼
Codex app-server child process
        │ JSON-RPC: thread/turn/items/approvals
        ▼
Aixion local relay client
        │ authenticated backend connection
        ▼
Aixion backend
        │
        ├── Android approval console
        └── Mac local approval UI
```

The same pending approval is represented by one immutable backend action record and resolved exactly once.

## Explicit exclusions

Do not implement any of the following:

- dependency on ChatGPT mobile Remote;
- hijacking or scraping the OpenAI Codex Desktop application;
- AppleScript or Accessibility approval clicks;
- OCR or screenshot parsing;
- terminal scraping;
- keyboard simulation;
- credential/token interception;
- exposing a Codex local control socket publicly;
- a second hidden Codex session for the phone;
- full-access/YOLO certification;
- TradeBot as the first live test repository.

## Implementation plan

### Slice 0 — Repository and stack inventory

Before adding a new framework:

1. Inspect the current desktop, relay, backend, and Android stacks.
2. Identify reusable UI/runtime technology already present.
3. Record a short decision note for the desktop shell.
4. Prefer the smallest maintainable desktop implementation that can:
   - start and monitor a local child process;
   - render streaming events;
   - store secrets in the macOS keychain or existing secure facility;
   - issue local notifications;
   - package as a normal macOS application.
5. Do not build a generic cross-platform framework unless the existing repository justifies it.

### Slice 1 — Desktop bridge shell

Implement a runnable macOS desktop bridge with:

- project list and folder selection;
- backend/relay connection status;
- secure relay token loading;
- a prompt entry screen;
- a live session screen;
- clean startup/shutdown;
- single-instance protection;
- structured local logs with secret redaction.

The final UX must not require the user to type `aixion-relay codex` in Terminal.

A CLI may remain only as a developer/test harness.

### Slice 2 — Codex app-server ownership

From the desktop bridge:

1. Resolve the configured Codex executable.
2. Start exactly one `codex app-server` child for a new supervised session.
3. Perform one initialize/initialized handshake.
4. Start exactly one thread.
5. Register one `HOST_STARTED` relay session.
6. Start user prompts as turns on the same thread.
7. Stream item/turn notifications into the desktop UI and backend.
8. Terminate the child cleanly on cancel or app shutdown.
9. Detect and clean up orphaned children after crashes.

Certification settings:

```text
approvalPolicy: on-request
sandbox: workspace-write
```

Do not certify using full access.

### Slice 3 — Canonical approval record

For each server-initiated Codex approval request, persist one canonical action containing at least:

- relay session ID;
- provider request ID;
- method;
- provider item ID;
- thread ID;
- turn ID;
- command or file-change payload;
- cwd;
- reason;
- sandbox/network/permission request;
- available decisions;
- creation and expiry timestamps;
- immutable canonical payload hash;
- resolution status/source/timestamp;
- provider response status.

Any mismatch between the presented payload and stored hash must fail closed.

### Slice 4 — Atomic Mac-or-Android resolution

Implement an atomic compare-and-set state transition:

```text
PENDING → RESOLVED
```

Valid resolver sources:

- `MAC`
- `ANDROID`
- `POLICY`
- `SYSTEM`

Requirements:

- exactly one terminal resolution;
- exactly one JSON-RPC response returned to Codex;
- losing responder receives `already resolved`;
- duplicate retries are idempotent;
- stale/expired requests cannot execute;
- unsupported provider decisions are rejected;
- backend loss fails closed;
- reconnect never replays an already-sent provider response.

### Slice 5 — Android approval experience

Connect the existing Android application to host-started sessions and canonical actions.

Implement:

- active-session list;
- pending-approval notification;
- exact approval details;
- provider-supported choice buttons;
- approve/reject/cancel;
- session-scoped allowance only when offered;
- already-resolved result;
- final command/file-change outcome;
- reconnect and expiry handling.

Use the repository's supported notification mechanism. Polling may exist as a reliability fallback, but do not present repeated duplicate notifications for the same action.

### Slice 6 — Desktop local approval experience

Render the same canonical action in the desktop bridge.

The Mac and Android views must be generated from the same stored payload and choice set. Do not independently infer or broaden decisions in either client.

### Slice 7 — Recovery and lifecycle

Implement and test:

- desktop app restart;
- relay restart;
- backend restart;
- Android reconnect;
- child process exit;
- turn failure;
- action expiry;
- cancellation while approval is pending;
- network loss before and after resolution;
- application quit with an active session.

The system must never silently switch to full access after any recovery path.

### Slice 8 — Security

At minimum:

- relay tokens and credentials are never stored in plaintext logs;
- local tokens use the existing secure store or macOS Keychain;
- backend actions are authorized to the correct owner and relay;
- workspace roots and repository allowlists are enforced;
- canonical action payloads are immutable after publication;
- approvals are bound to session/thread/turn/provider request identity;
- all approval decisions are durably auditable;
- no public unauthenticated local listener;
- no arbitrary path traversal;
- no shell interpolation of user-controlled values in bridge process launch.

## Required automated tests

### Desktop/relay

- one child process per session;
- one thread per session;
- multiple prompts use the same thread;
- clean child termination;
- orphan recovery;
- host registration idempotency;
- no duplicate `START_SESSION`;
- exact event streaming;
- approval mode is not full access;
- secrets are redacted.

### Backend

- canonical payload hashing;
- immutable action identity;
- Android approval wins;
- Mac approval wins;
- simultaneous race has exactly one winner;
- losing responder gets already resolved;
- approve/reject/cancel;
- session-scoped decision only when offered;
- stale and expired action;
- wrong relay/user/session rejected;
- duplicate retries idempotent;
- no duplicate provider response.

### Android

- host-started session visible without starting another session;
- exact payload rendering;
- exact offered decisions;
- approval notification deduplication;
- approve/reject/cancel;
- already-resolved UI;
- offline/reconnect/expiry states.

### Regression

Run complete relevant backend, relay, desktop, and Android tests plus lint/build. Preserve the existing mobile-started flow unless the product contract explicitly replaces it.

## Real disposable-repository certification

Do not use TradeBot.

Create a disposable repository and prove:

```text
1. Open Aixion Desktop.
2. Add/select the disposable repository.
3. Start a Codex task from the desktop UI.
4. Confirm one child PID, one thread ID, one relay session ID.
5. Trigger a harmless shell approval.
6. Verify the command has not executed.
7. Leave the Mac.
8. Receive the exact request in the Aixion Android app.
9. Approve on Android.
10. Verify the same process/thread/turn continues.
11. Verify the command executes exactly once.
12. Verify the final outcome appears on both desktop and Android.
```

Separately prove:

- reject path;
- Mac wins race;
- Android wins race;
- session allowance when offered;
- stale/expired approval;
- backend loss while pending;
- relay restart;
- desktop app restart or documented safe termination behavior.

Record process IDs, session/thread/turn/action IDs, payload hashes, timestamps, event order, file-before/file-after evidence, commands, tests, and final branch SHA.

## Deliverables

- working macOS desktop bridge application;
- backend and relay changes;
- Android changes;
- automated tests;
- packaging/run instructions;
- `docs/research/aixion_desktop_bridge_v1_results.md`;
- draft stacked PR targeting `feature/local-codex-session-supervision-v1`;
- conservative final verdict.

## Verdict rules

Use exactly one:

- `NOT_IMPLEMENTED`
- `IMPLEMENTED_NOT_CERTIFIED`
- `CERTIFIED_DISPOSABLE_REPOSITORY`

Do not claim certification without the real Aixion Android approval evidence.

## Stop conditions

- Do not merge.
- Do not deploy.
- Do not modify TradeBot.
- Do not substitute ChatGPT mobile Remote for Aixion Android.
- Do not claim native Codex Desktop attachment support.
- Stop and document exact evidence if a required Codex app-server capability is unavailable, then implement the safest supported behavior without silently changing the product vision.
