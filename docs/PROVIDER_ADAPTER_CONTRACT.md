# Aixion Provider Adapter Contract

## Goal

A provider adapter translates one agent runtime into the stable Aixion Relay contract. The adapter may understand provider-specific sessions, tools and events, but it must not own approval policy, capability scope, repository authorization, durable recovery or mobile state.

```text
provider protocol <-> adapter <-> relay runtime <-> Aixion trust control plane
```

## Required interface

Every adapter implements:

```python
class AgentAdapter:
    manifest: AdapterManifest
    async def start(context: AdapterContext) -> AgentSessionHandle: ...
    async def close() -> None: ...

class AgentSessionHandle:
    remote_session_id: str | None
    async def send_message(message, metadata): ...
    async def pause(reason): ...
    async def resume(reason): ...
    async def cancel(reason): ...
    async def sync() -> dict: ...
    async def wait() -> SessionResult: ...
```

The adapter receives two capabilities through `AdapterContext`:

```text
emit(NormalizedEvent)
authorize(ActionProposal) -> ActionDecision
```

The adapter never calls the mobile app and never decides whether an action is safe.

## Manifest

A manifest declares:

```text
adapter id
provider id
adapter kind
human-readable name
version
executable
availability
supported features
optional external-agent identity
non-secret metadata
```

Feature declarations must be truthful. Do not advertise `RESUME`, `NATIVE_APPROVALS`, `FILE_DIFFS`, `TEST_RESULTS`, token usage or cost usage unless the adapter can prove those features from provider output.

## Normalized events

Adapters emit one of:

```text
SESSION_STARTING
SESSION_STARTED
SESSION_RESUMED
AGENT_MESSAGE
USER_MESSAGE
PLAN_UPDATED
REASONING_SUMMARY
TOOL_PROPOSED
TOOL_STARTED
TOOL_OUTPUT
FILE_CHANGE_PROPOSED
FILE_CHANGED
COMMAND_PROPOSED
COMMAND_STARTED
COMMAND_OUTPUT
TEST_RESULT
APPROVAL_REQUIRED
APPROVAL_RESOLVED
USAGE
HEARTBEAT
SESSION_PAUSED
SESSION_COMPLETED
SESSION_FAILED
SESSION_CANCELLED
RAW
```

Each event should include:

```text
concise message
structured provider evidence
provider session id when known
provider turn id when known
provider event timestamp when known
```

Do not put provider access tokens, environment secrets, complete private prompts or unrelated filesystem contents into event payloads.

## Action normalization

Every provider side effect must become one exact `ActionProposal` before execution.

Supported action families:

```text
READ_REPOSITORY
CREATE_BRANCH
MODIFY_FILES
RUN_COMMAND
ACCESS_NETWORK
CREATE_PULL_REQUEST
DEPLOY
READ_SECRET
WRITE_DATABASE
CUSTOM
```

The current trust policy deliberately blocks high-risk or unsupported capability families unless a future reviewed policy explicitly permits them.

### Command actions

Include:

```text
exact command or argv representation
working directory in metadata
estimated runtime
retry number
provider tool/request identity
```

Never concatenate untrusted data into a shell command. Prefer a provider API or argv array.

### File actions

Include every known path and the provider patch or diff identifier. A directory-level grant is not equivalent to an individual approved file unless the capability lease explicitly permits the directory prefix.

### Network actions

Include normalized destination domains. URLs may remain in evidence, but policy matching should operate on normalized domains.

### Unknown tools

Map unknown tools to `CUSTOM`. Do not guess that an unfamiliar tool is read-only. The default policy should fail closed.

## Approval behavior

The adapter calls:

```python
decision = await context.authorize(proposal)
```

Possible decisions:

```text
ALLOW
REQUIRE_APPROVAL
BLOCK
```

The relay runtime resolves `REQUIRE_APPROVAL` by waiting for the latest effective backend decision. The adapter receives only the final effective decision.

Rules:

1. Execute only after `ALLOW`.
2. Treat `BLOCK`, timeout, network failure, malformed response or missing identity as denial.
3. Do not cache an approval for another action.
4. Do not broaden paths, command arguments, network destinations or environment after approval.
5. A provider retry is a new action attempt and must preserve retry evidence.
6. Policy blocks cannot be overridden inside the adapter.

## Session semantics

A provider turn finishing does not necessarily mean the relay session is complete. Adapters should keep a session steerable when the provider supports additional messages.

Use terminal events only when:

```text
provider session is explicitly closed
provider process exits permanently
operator cancels
unrecoverable provider failure occurs
approved runtime budget expires
```

## Resume behavior

An adapter may advertise `RESUME` only when it can restore a provider session from a durable provider session identifier.

On relay restart:

```text
backend session snapshot
-> adapter start with resume_only=true
-> provider session resume
-> no duplicate initial objective
-> event sequence continues from backend truth
```

If restoration is impossible, fail the recovery command clearly. Do not silently start a new provider session under the old Aixion session id.

## Process adapters

Generic process adapters use an explicit argv array and `shell=False` semantics. The provider process communicates with newline-delimited JSON.

Maximum provider event payload: 256 KiB.

Provider approval request:

```json
{
  "type": "APPROVAL_REQUIRED",
  "request_id": "request-1",
  "action": {
    "action_type": "RUN_COMMAND",
    "command": "python -m pytest tests/test_safe.py"
  }
}
```

Relay response:

```json
{
  "type": "APPROVAL_DECISION",
  "request_id": "request-1",
  "action_id": "action_123",
  "decision": "ALLOW",
  "reasons": []
}
```

## Required adapter tests

Every provider adapter must include deterministic tests for:

```text
manifest truth
session start
provider session id capture
message steering
pause/resume/cancel
normal event mapping
command action mapping
file action mapping
network action mapping
unknown tool -> CUSTOM
ALLOW response
BLOCK response
approval timeout
provider process failure
resume without duplicate objective
duplicate provider event handling
secret redaction
```

A real provider certification must additionally prove:

```text
installed official provider runtime
local authentication
one harmless read-only task
one exact command approval
one exact file approval
one denial
one mobile follow-up message
one cancellation
one provider restart or relay restart when resume is claimed
matching provider, backend and mobile evidence
```

## Versioning

Provider contracts change independently. Record the provider runtime version and adapter version in the relay heartbeat and final evidence. A previously certified adapter must be recertified after a provider protocol change that affects sessions, approvals, tool payloads, event schemas or authentication.

## Acceptance rule

A new adapter is accepted only when it integrates through the common contracts. Provider-specific changes to the trust engine, mobile approval semantics or durable run state machine require a separate architecture review.