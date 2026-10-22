# Codex PermissionRequest Contract Validation — 2026-10-21

> Historical check. Its conclusion that the adapter denies when `tool_use_id`
> is absent was superseded by the callback-scoped random-ID fallback recorded
> in [`PRE_BILLING_WORK_LOCK.md`](PRE_BILLING_WORK_LOCK.md#slice-4--implement-the-codex-local-permission-adapter).
> The Slice 4 native Codex-to-phone round trip remains unverified.

**Scope:** Pre-billing Slice 4 adapter contract check from [PRE_BILLING_WORK_LOCK.md](PRE_BILLING_WORK_LOCK.md).

## Current official contract checked

On 2026-10-21, the official [Codex hooks documentation](https://learn.chatgpt.com/docs/hooks) was checked against `integrations/codex/aixion_permission_request_hook.py` and the workspace setup.

The documented `PermissionRequest` event runs when Codex is about to request permission. Its event-specific fields include `turn_id`, `tool_name`, `tool_input`, and optional `tool_input.description`; `tool_use_id` is not listed. The documented decision response is `hookSpecificOutput.hookEventName = "PermissionRequest"` with `decision.behavior` set to `allow` or `deny`. A synchronous command hook is appropriate for a decision that must keep the permission request pending. Codex requires review and trust of non-managed hooks before they run.

The documented `PreToolUse` event does include `tool_use_id`, but it runs before supported tool calls generally, not only for permission prompts. The official docs also warn that errors, missing servers, and unavailable tools do not block an operation, so it cannot replace the permission-specific event for a fail-closed remote approval gate. The adapter requires `tool_use_id` and explicitly denies before any API request when it is missing; it does not derive a fallback invocation identity from payload fields. Therefore the currently documented `PermissionRequest` schema is incompatible with this strict adapter, and Codex phone approval remains unsupported for that schema. Do not claim support until Codex documents a stable per-invocation identifier on `PermissionRequest` and a real installed-app round trip passes. Source: [official Codex hooks documentation](https://learn.chatgpt.com/docs/hooks), checked 2026-10-21.

## Exact-checkout validation

Command:

```sh
/tmp/aixion-ct-validation-20261021/bin/pytest \
  backend/tests/test_codex_permission_request_hook.py \
  backend/tests/test_antigravity_pre_tool_hook.py \
  backend/tests/test_antigravity_workspace_setup.py -q
```

Result: **69 passed in 0.15s**. The system Python 3.14 environment did not have pytest installed; the repository validation virtual environment was used. This run covers mocked adapter behavior and setup only.

## Gate status

This evidence does **not** pass the Slice 4 exit gate. A real, identified local Codex permission request has not yet been demonstrated end to end through the Android phone, and the same Codex invocation resuming after approve (or being prevented after deny) remains unverified. Timeout, API failure, stale/mismatched payload, and credential revocation also require real-app evidence as specified by the lock. No hosted API, billing, cloud resource, or deployment was used.

## Configuration and migration

No new configuration keys, database migrations, runtime behavior, or rollout steps were introduced by this validation note. Existing hook configuration and local setup instructions are in [integrations/codex/README.md](../integrations/codex/README.md).
