# Adopt Codex `untrusted` Managed Profile v1

## Status

Authoritative continuation task for `feature/aixion-desktop-bridge-v1`.

## Established provider evidence

The installed Codex app-server v0.146.0 has been tested directly with fresh provider processes, fresh threads, disposable workspaces, `approvalsReviewer: user`, `approvalPolicy: untrusted`, and `sandbox: workspace-write`.

Observed behavior:

- workspace shell write emitted `item/commandExecution/requestApproval` and did not execute;
- workspace file change emitted `item/fileChange/requestApproval` and did not execute;
- known-safe read-only `pwd` executed automatically;
- provider request IDs, thread IDs, turn IDs, item IDs, cwd, payloads, and available decisions were captured;
- no approval request was resolved during the provider-only probe;
- child processes and disposable targets were cleaned up.

Provider verdict:

```text
UNTRUSTED_POLICY_SUPPORTS_SIDE_EFFECT_APPROVALS
```

## Product contract correction

Do not describe Aixion as requiring approval before literally every command.

The customer-facing contract is:

> Aixion requires explicit approval before side-effecting Codex shell commands and file changes. Codex may automatically execute provider-classified safe read-only inspection commands.

The managed profile name should be:

```text
Strict side effects
```

Do not call the profile `approve every command`.

## Objective

Adopt `approvalPolicy: untrusted` as the Aixion-managed Codex approval profile, verify the effective provider configuration, rerun the API-level desktop bridge smoke with a command already proven to emit approval, and only after that passes prepare the real Aixion Android test.

## Non-negotiable boundaries

- Do not modify the user's global `~/.codex` configuration.
- Do not inherit `approval_policy = "never"` or `sandbox_mode = "danger-full-access"` from customer configuration.
- Do not use `on-request` for the `Strict side effects` profile.
- Do not claim approval before all read-only commands.
- Do not touch TradeBot.
- Do not use port `8000`.
- Do not merge or deploy.
- Do not move to real Android testing until the API-level bridge smoke passes.

## Slice 1 — Managed provider profile

Update the desktop bridge/provider adapter so every new `Strict side effects` session explicitly requests:

```text
approvalsReviewer: user
approvalPolicy: untrusted
sandbox: workspace-write
network access: false by default
```

Requirements:

1. Use the exact schema-supported serialized value `untrusted`.
2. Apply it per Aixion-owned provider process/thread.
3. Do not mutate global Codex settings.
4. Start every certification run with a fresh Codex process and fresh thread.
5. Disable or isolate session approval caching during certification.
6. Preserve provider-supported decisions exactly.
7. Fail closed if the installed Codex schema does not include `untrusted`.

## Slice 2 — Effective-profile verification

After `thread/start`, capture and verify the effective returned profile before starting the user turn.

The session must not proceed unless all required values are proven:

```text
approvalPolicy == untrusted
sandbox == workspaceWrite
runtime workspace root == selected allowed repository
approvals reviewer == user, when projected by the provider
```

If the provider response omits a field required for direct verification, record the exact limitation and use the strongest supported evidence from the request/response protocol. Do not silently assume success.

A profile mismatch must:

- prevent `turn/start`;
- terminate the spawned Codex child;
- publish a safe diagnostic error;
- create no pending approval action;
- execute no customer command.

## Slice 3 — Customer compatibility gate

Implement a reusable compatibility check suitable for future customer onboarding.

The check must determine:

- Codex executable exists;
- Codex version;
- app-server starts;
- generated schema includes `untrusted`;
- thread accepts `untrusted` + `workspace-write`;
- a disposable side-effect probe emits a provider approval request without executing;
- cleanup succeeds.

Do not run the write probe inside the customer's selected production repository. Use an Aixion-owned temporary disposable directory.

Persist only the compatibility result and safe diagnostics, not credentials or unsanitized provider traffic.

Suggested states:

```text
COMPATIBLE_STRICT_SIDE_EFFECTS
INCOMPATIBLE_NO_UNTRUSTED_POLICY
INCOMPATIBLE_PROFILE_NOT_EFFECTIVE
INCOMPATIBLE_WRITE_AUTO_EXECUTED
PROVIDER_START_FAILED
PROVIDER_PROTOCOL_ERROR
```

The final customer UI should disable `Start task` for the strict profile when compatibility fails.

## Slice 4 — Product UI wording

Update desktop and Android wording where applicable.

Use:

```text
Strict side effects
Approval required before commands or file changes that can modify state.
Safe read-only inspection may run automatically.
```

Do not use:

```text
Approve every command
Every action requires approval
Nothing runs without approval
```

The pending approval UI must still show the exact provider action and only provider-supported decisions.

## Slice 5 — Automated tests

Add or update tests for:

1. managed profile serializes `approvalPolicy: untrusted`;
2. managed profile uses `workspace-write`;
3. global Codex defaults are not read as authoritative for the managed session;
4. schema without `untrusted` fails closed;
5. effective profile mismatch aborts before `turn/start`;
6. side-effect shell approval maps to one canonical backend action;
7. file-change approval maps to one canonical backend action;
8. safe read-only command without approval is represented as normal execution, not a missing-action error;
9. no approval cache leaks across fresh certification processes;
10. provider request/session/thread/turn/item identity remains stable through registration buffering;
11. duplicate provider request does not create duplicate canonical actions;
12. compatibility probe cleans up its child and temporary files;
13. customer start is disabled when compatibility is not proven.

Run focused lint/tests after each slice, then relevant backend, relay, desktop, and Android regression.

## Slice 6 — Correct the API-level bridge smoke

Update `scripts/run_desktop_bridge_api_smoke.py` so it uses the managed `untrusted` profile and a side-effect command already proven by the provider-only probe to emit:

```text
item/commandExecution/requestApproval
```

The smoke remains:

```text
API-level Android-source simulation only
```

It is not real Android UI certification.

Before resolving, require all of the following:

- effective `untrusted` profile proven;
- effective `workspaceWrite` sandbox proven;
- exact expected command;
- exact disposable cwd;
- provider request ID present;
- one thread ID;
- one turn ID;
- one item ID;
- canonical payload hash locally recomputed and matched;
- exactly one pending canonical action;
- provider-supported decisions include the selected decision;
- output/counter evidence absent before approval.

Resolve through the backend using Android source simulation only after every check passes.

A smoke pass requires:

- backend health and identity proven;
- registered relay session used for all published events;
- raw provider approval request captured;
- one canonical backend action;
- API resolution returns success;
- same provider PID/thread/turn continues;
- command executes exactly once;
- no duplicate provider response;
- child and backend cleanup succeeds;
- port `18080` is closed afterward;
- final result is explicitly labeled API-level simulation only.

Do not move to Android if any field is absent or any identity changes unexpectedly.

## Slice 7 — Real Android gate preparation

Only after the API-level smoke passes, prepare but do not falsely certify the real phone test.

Required real flow:

```text
Aixion Desktop starts one managed Codex session
→ effective untrusted/workspace-write profile is shown
→ side-effect command triggers one provider approval
→ exact action appears in Aixion Android
→ output is absent
→ user taps one provider-supported decision
→ same provider PID/thread/turn continues
→ command executes exactly once
→ final result appears on desktop and Android
```

Separately test reject.

The real phone test must use the installed Aixion Android app, not direct backend API calls with `source: ANDROID`.

## Evidence and documentation

Update or create:

```text
docs/research/aixion_desktop_bridge_v1_results.md
```

Record:

- Codex version;
- schema evidence for `untrusted`;
- provider-only probe verdict;
- managed profile request and effective response;
- tests and counts;
- API-level smoke IDs and hashes;
- cleanup evidence;
- remaining real Android status;
- conservative final verdict.

Use exactly one final verdict:

```text
IMPLEMENTED_NOT_CERTIFIED
API_LEVEL_SIDE_EFFECT_APPROVAL_CERTIFIED
CERTIFIED_DISPOSABLE_REPOSITORY
```

Do not use `CERTIFIED_DISPOSABLE_REPOSITORY` without actual Aixion Android approval evidence.

## Stop conditions

- Stop if `untrusted` is not effective in the managed bridge path.
- Stop if a side-effect executes before approval.
- Stop if provider identity cannot be bound to one canonical action.
- Stop if cleanup leaves child processes or listeners.
- Do not weaken the policy to make the smoke pass.
- Do not modify global Codex configuration.
- Do not merge, deploy, or modify TradeBot.
