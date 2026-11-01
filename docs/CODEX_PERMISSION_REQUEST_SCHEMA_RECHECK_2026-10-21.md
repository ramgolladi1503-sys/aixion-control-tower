# Codex Permission Hook Schema Recheck

**Checked:** 2026-10-21  
**Purpose:** Recheck Slice 4's invocation-correlation blocker against the current
official Codex hook documentation.  
**Historical result:** At the time of this check, the existing Control Tower
`PermissionRequest` adapter required provider-supplied `tool_use_id`, which the
documented event did not include. That finding was later superseded by the
callback-scoped identity implementation recorded in
[`PRE_BILLING_WORK_LOCK.md`](PRE_BILLING_WORK_LOCK.md#slice-4--implement-the-codex-local-permission-adapter).

**Current status:** The hook now uses provider `tool_use_id` when available, or
creates a fresh random ID for each synchronous callback when it is absent. This
avoids deriving identity from command payloads and prevents identical calls
from colliding. The installed-app round trip remains unverified, so Slice 4
stays open.

## Verified current contract

The official [Codex Hooks documentation](https://developers.openai.com/codex/hooks)
describes `PermissionRequest` as running when Codex is about to ask for
permission. Its event-specific fields include `turn_id`, `tool_name`,
`tool_input`, and an optional human-readable description. It does not list
`tool_use_id` for this event. The same documentation lists `tool_use_id` for
`PreToolUse` and `PostToolUse`.

`PermissionRequest` provides explicit `allow` and `deny` decisions. It does not
provide an invocation ID in its documented schema. `session_id` and `turn_id`
scope the session and turn; they do not uniquely identify one tool call when
multiple identical calls occur in a turn. Hashing the command and input would
therefore create a collision risk and would not bind the phone decision to one
exact invocation.

`PreToolUse` is not an equivalent replacement. The documentation says a
callback timeout, error, or malformed response can fail without blocking the
tool. Its event has a tool-call ID, but the documented failure behavior does
not guarantee a fail-closed remote approval gate. The hook must not return
`allow` after an ambiguous phone/API result, and the system must not infer that
a failed callback denied execution.

## Implementation at the time of this check

At the time of this check, `integrations/codex/aixion_permission_request_hook.py`
required `tool_use_id` before making any API request and explicitly denied if it
was absent. The current hook has since added the callback-scoped random-ID
fallback described above. It must not derive IDs from payloads or reuse
`turn_id`; `PreToolUse` remains unsuitable for the remote approval wait because
its timeout/error behavior does not guarantee that the tool is blocked.

The integration remains an uncertified Codex adapter. This evidence does not
establish that the installed Codex build invokes the hook with the expected
event shape or that phone approval resumes that same native invocation.

## Evidence required to reopen the gate

Slice 4 remains open until all of the following are available:

1. A test against the actual Codex app captures the callback payload and
   confirms the installed build invokes the synchronous `PermissionRequest`
   hook for the intended native permission prompt.
2. The callback-scoped ID stays bound to that callback through creation,
   phone decision polling, and consume, without payload-derived identity.
3. The supported callback has an explicit, documented failure behavior that
   prevents the tool from proceeding on timeout, hook failure, malformed
   output, connector loss, or stale approval.
4. A test against the actual Codex app captures the real callback payload and
   verifies approve resumes the same invocation while deny and failure prevent
   it.
5. Restart and duplicate-delivery tests show that an approval cannot be
   replayed to a different invocation; a restarted callback must get a new ID.

Until those conditions are met, keep Slice 4 open. Explicitly deny malformed
provider IDs or events missing session/turn identity; when the provider ID is
absent, use only a fresh callback-scoped ID.

## Validation and rollout

This is a documentation-only evidence update. No tests, configuration keys,
database changes, credentials, application behavior, or provider setup were
changed. The note must be reviewed alongside `docs/PRE_BILLING_WORK_LOCK.md`
before any Codex integration claim or live hook installation is changed.
