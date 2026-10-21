# Codex Permission Hook Schema Recheck

**Checked:** 2026-10-21  
**Purpose:** Recheck Slice 4's invocation-correlation blocker against the current
official Codex hook documentation.  
**Result:** The existing Control Tower `PermissionRequest` adapter remains
fail-closed and cannot be certified for live phone approval with the currently
documented event schema.

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

## Current implementation boundary

`integrations/codex/aixion_permission_request_hook.py` requires
`tool_use_id` before making any API request. When that identity is absent, the
hook emits an explicit Codex `deny` response and does not create an approval.
This is intentional. Do not synthesize the ID from payload, reuse `turn_id`,
or switch to `PreToolUse` as a way to make the current Codex session appear
supported.

The integration remains a development adapter, not a certified Codex product
path. In particular, this evidence does not establish interception of an
existing native Codex Desktop session or that phone approval resumes that same
invocation.

## Evidence required to reopen the gate

Reassess Slice 4 only when all of the following are available:

1. The supported Codex build emits a stable, unique invocation ID in the
   synchronous permission callback, or documents another collision-safe way
   to correlate one permission request to one tool invocation.
2. The supported callback has an explicit, documented failure behavior that
   prevents the tool from proceeding on timeout, hook failure, malformed
   output, connector loss, or stale approval.
3. A test against the actual Codex app captures the real callback payload and
   verifies approve resumes the same invocation while deny and failure prevent
   it.
4. Restart and duplicate-delivery tests show that an approval cannot be
   replayed to a different invocation.

Until those conditions are met, keep Slice 4 open and keep the hook's
missing-identity behavior as explicit denial.

## Validation and rollout

This is a documentation-only evidence update. No tests, configuration keys,
database changes, credentials, application behavior, or provider setup were
changed. The note must be reviewed alongside `docs/PRE_BILLING_WORK_LOCK.md`
before any Codex integration claim or live hook installation is changed.
