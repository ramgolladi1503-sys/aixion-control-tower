# Codex App Server Compatibility Spike

**Date:** 2026-10-22
**Status:** Evidence collected; Slice 4 remains open
**Scope:** Determine whether the installed Codex App Server protocol can supply stable approval identity for the Control Tower phone workflow.

## Finding

The installed `codex` CLI is version `0.146.0`. Its version-generated JSON Schema includes server-initiated `item/commandExecution/requestApproval` and `item/fileChange/requestApproval` requests.

The generated command approval parameters require `itemId`, `threadId`, `turnId`, and `startedAtMs`. `approvalId` is nullable for ordinary shell/unified-exec approvals and is a distinct opaque callback identifier for zsh-exec-bridge subcommand approvals. The file-change approval parameters also require `itemId`, `threadId`, `turnId`, and `startedAtMs`.

This gives an App Server client a stable identifier tuple for a pending request: provider installation + thread ID + turn ID + item ID, and approval ID when supplied. The tuple is stronger than the current `PermissionRequest` hook input, whose documented schema omits `tool_use_id`.

The official App Server documentation describes responding to command and file approval requests so the server can resume or decline the same work. It also describes `thread/list` and `thread/resume` for stored threads.

## Local evidence and reproduction

The schema was generated without attaching to the signed-in Codex profile or modifying any existing thread:

```sh
mkdir -p /tmp/aixion-codex-appserver-schema-0.146.0
CODEX_HOME=/tmp/aixion-codex-appserver-schema-0.146.0 \
  codex app-server generate-json-schema \
  --out /tmp/aixion-codex-appserver-schema-0.146.0/schema
```

The generated `ServerRequest.json` contains `CommandExecutionRequestApprovalParams` and `FileChangeRequestApprovalParams` with the fields listed above. CLI version and capabilities were checked with:

```sh
codex --version
codex app-server --help
codex app-server daemon --help
codex app-server proxy --help
```

`codex app-server daemon version` could not connect because the documented local daemon control socket was absent at `/Users/madhuram/.codex/app-server-control/app-server-control.sock`. This proves no daemon was available at that path during this check; it does not prove that the Codex Desktop application has no private internal server.

## Limits

- Codex’s App Server command is marked experimental in the installed CLI. The official documentation says its WebSocket transport is experimental and unsupported for production workloads. The schema check does not make the protocol production-supported.
- `thread/resume` proves a protocol operation exists for stored threads. It does not prove that an external Control Tower connector can attach safely to a thread currently owned by the native Codex Desktop app, receive its live approval requests, or avoid conflicting control of that thread.
- No real Codex approval was generated, no live Codex session was resumed, and no phone decision was sent through this protocol.
- The existing `PermissionRequest` hook remains fail-closed when the required stable invocation identifier is absent. It must not infer invocation identity from `turnId` and payload.
- The existing pre-billing product contract requires continuing the same native agent session. Replacing that with an App Server-managed Codex session would change the user workflow and is not accepted by this spike.

## Slice 4 decision

Keep Slice 4 **OPEN**. Do not change or enable the current Codex hook based on this schema result. Treat App Server as a candidate for a separately gated compatibility experiment, not as proof that native Codex Desktop approvals can be intercepted.

The next acceptance evidence must establish, using a supported production integration surface, all of the following:

1. The user can start or select the intended native Codex Desktop project/session without a separate replacement UI.
2. The connector receives the actual blocking approval request and binds it to the exact pending item/callback.
3. A phone approve response is returned to that same pending request and the same Codex invocation resumes.
4. Deny, timeout, connector/API failure, duplicate response, stale request, and connector restart all fail closed.
5. The integration protocol and transport are supported for production, or Codex documents a stable supported local hook contract that carries an invocation ID and blocks on failure.
6. The flow is repeated against the installed Codex version and recorded with exact CLI/app version, connector revision, request identity fields, and terminal outcome.

Until those conditions are met, native Codex Desktop support remains unverified and the Codex exit gate is unsatisfied.

## Change impact

- **Files changed:** this spike document only.
- **Configuration keys:** none.
- **Tests added:** none; no implementation changed.
- **Database migration:** none.
- **Runtime, device, or cloud changes:** none.
- **Rollout:** none. Keep the current Codex hook disabled for live use unless its documented required invocation identity is present; do not expose an App Server listener remotely for this experiment.

## Sources

- [Codex App Server documentation](https://learn.chatgpt.com/docs/app-server)
- [Codex Hooks documentation](https://developers.openai.com/codex/hooks)
