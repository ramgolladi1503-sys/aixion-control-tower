# Diagnose Missing Codex Approval Action

## Current verdict

The API-level smoke did **not** pass.

Observed:

- disposable backend started correctly;
- health returned `200`;
- backend identity and cwd were verified;
- no pending Codex approval action appeared before timeout;
- no approval was resolved;
- cleanup succeeded;
- port `18080` closed.

Do not move to the real Android test and do not rerun the same smoke unchanged.

## Critical technical point

`approvalPolicy: on-request` does not mean every command automatically generates an approval. The effective Codex permission profile, filesystem sandbox policy, command classification, and actual provider tool choice determine whether a request is emitted.

The bridge must capture and prove the **effective** Codex thread/turn permission configuration and the complete provider protocol stream.

## Objective

Determine exactly which of these failed:

1. Codex never proposed a command.
2. Codex proposed a command but did not require approval.
3. Codex emitted an approval request but the adapter did not receive it.
4. The adapter received it but did not create a canonical backend action.
5. The action was created but the smoke queried the wrong session/action scope.

Do not guess. Produce evidence that selects exactly one lane.

## Phase 1 — Preserve the failed-run evidence

Save the prior smoke evidence before editing:

- backend PID;
- relay ID;
- registered relay session ID;
- temporary local session ID;
- Codex provider PID;
- thread ID;
- initial turn ID;
- all captured events;
- final turn state;
- stdout/stderr tails;
- timeout timestamps;
- port-cleanup evidence.

If the current harness discards protocol messages or child stderr, fix that first.

## Phase 2 — Add raw Codex protocol tracing

Add a disposable diagnostic mode that records every inbound and outbound Codex app-server message before filtering or mapping.

For each JSON-RPC message, record:

- monotonic sequence number;
- UTC timestamp;
- direction;
- request/notification/response;
- method;
- request ID;
- thread ID;
- turn ID;
- item ID;
- sanitized params/result;
- whether the adapter handled, buffered, ignored, or rejected it.

Redact authentication material and environment secrets.

The trace must include `initialize`, `initialized`, `thread/start`, `thread/started`, `turn/start`, `turn/started`, all `item/*` messages, any server-initiated approval request, and `turn/completed` or the terminal error.

Do not log chain-of-thought or secrets.

## Phase 3 — Prove effective permission configuration

Capture the exact payload sent to `thread/start` and `turn/start`.

Require:

```text
approvalPolicy: on-request
sandbox: workspace-write
```

Do not inherit Full Access, YOLO, user-global defaults, or an unrestricted permission profile.

Capture the returned thread projection, including when available:

- sandbox;
- active permission profile;
- cwd;
- runtime workspace roots;
- approval policy;
- thread ID.

Fail before the prompt if the effective filesystem policy is not restricted/workspace-write or approval policy is not `on-request`.

## Phase 4 — Provider-only approval probe

Create a diagnostic test that launches Codex app-server directly, without backend registration or Android simulation, using a fresh process, thread, and disposable directory.

### Probe A — workspace command

Ask Codex to execute exactly:

```bash
python3 -c "from pathlib import Path; Path('provider_probe.txt').write_text('provider-probe\n')"
```

Require command execution, not file editing/apply-patch.

Capture whether Codex emits:

```text
item/commandExecution/requestApproval
```

Do not resolve it during the observation window.

### Probe B — harmless outside-workspace command

Only if Probe A emits no approval, request exactly:

```bash
python3 -c "from pathlib import Path; Path('/tmp/aixion_codex_approval_probe_<UNIQUE_ID>').write_text('probe\n')"
```

Do not approve or execute it. Observe whether Codex emits an approval or rejection under the restricted sandbox.

Clean up the temporary file only if it was somehow created. Record that as a failure because execution must not occur before approval.

Report exactly one verdict:

- `PROVIDER_EMITTED_APPROVAL`
- `PROVIDER_AUTO_APPROVED_OR_EXECUTED`
- `PROVIDER_REJECTED_WITHOUT_APPROVAL`
- `PROVIDER_DID_NOT_ATTEMPT_COMMAND`
- `PROVIDER_PROTOCOL_ERROR`

Include the sanitized message sequence supporting the verdict.

## Phase 5 — Diagnose by lane

### Lane A: no command item

Inspect the final agent response and item sequence. Change only the disposable prompt/harness so command execution is unambiguous.

### Lane B: command item but no approval

Compare requested and effective permissions. Check for:

- unrestricted/full-access profile;
- omitted or overwritten approval policy;
- turn-level override;
- session approval cache;
- prior policy amendment;
- command already trusted;
- sandbox disabled.

Use a fresh process and thread.

### Lane C: provider approval emitted but adapter misses it

Verify the adapter accepts the exact method name and envelope from the installed Codex version. Add a regression test using the captured message.

### Lane D: adapter receives approval but backend action is missing

Trace:

```text
provider request
→ adapter
→ ActionProposal
→ canonical hash
→ backend action create
→ pending-action query
```

Record action ID and relay session ID at each boundary.

### Lane E: backend action exists but harness cannot see it

Compare relay ID, registered session ID, action session foreign key, action state, query route, hash, and expiry. Fix the scope/query defect and add a regression.

## Phase 6 — Deterministic tests before another smoke

Add tests for:

1. captured Codex approval envelope maps to one canonical action;
2. unknown approval method fails closed and is visible in diagnostics;
3. effective permission mismatch aborts the smoke;
4. fresh process/thread has no cached session approval;
5. provider request identity survives buffering and registration;
6. pending-action query uses the registered relay session ID;
7. no-action timeout prints the sanitized provider trace;
8. registration failure cancels the Codex child;
9. approval before registration cannot resolve;
10. no command executes during a no-approval timeout.

Run focused lint and tests.

## Phase 7 — Rerun criteria

Do not rerun `scripts/run_desktop_bridge_api_smoke.py` until the provider-only probe produces a real approval request or a concrete protocol/configuration defect is fixed.

Then rerun exactly:

```bash
.venv/bin/python scripts/run_desktop_bridge_api_smoke.py
```

Request manual approval first.

Keep the result labeled:

```text
API-level Android-source simulation only
```

A pass requires:

- effective `on-request` + restricted/workspace-write evidence;
- raw provider approval request captured;
- exact command/cwd/hash validation;
- exactly one pending action;
- output absent before approval;
- API resolution `200`;
- same provider PID/thread/turn continuity;
- command executes exactly once;
- no duplicate provider response;
- backend and Codex child cleaned up;
- port `18080` closed.

Do not move to real Android until this passes.

## Safety

- Disposable repository only.
- Do not touch TradeBot.
- Do not use port `8000`.
- Do not kill unrelated processes.
- Do not weaken production approval policy to make the test pass.
- Do not claim certification from mocks or direct backend resolution.
- Do not merge or deploy.
