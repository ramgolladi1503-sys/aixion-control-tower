# Universal Agent Relay — Production Readiness

## Release classification

```text
Target: production-hardened single-host controlled-pilot candidate
Pull request: #190
Merge state: draft until every deterministic and live gate passes
Not claimed: unrestricted autonomous execution or universal provider compatibility
```

## Deterministic repository gates

All must pass on the exact pull-request head:

```text
backend Ruff
full backend pytest suite
relay Ruff
full relay pytest suite
Android JVM tests
Android debug APK
Android release build
demo Compose validation
production Compose validation
production-profile API startup
relay token authentication tests
workspace and repository escape tests
command lease recovery tests
event idempotency and tamper tests
durable run capability-binding tests
mobile relay DTO and screen compilation
```

## Relay host requirements

A certified host must have:

```text
supported Python runtime
dedicated relay virtual environment
OS keychain or external secret manager
outbound HTTPS access to Aixion API
no inbound public relay port
registered absolute workspace roots
registered repository allowlist
provider runtime installed locally
provider runtime authenticated locally
background service supervision
clock synchronization
sufficient disk space for provider worktrees
```

The relay token must not appear in:

```text
JSON configuration
service definitions
shell history
mobile storage
Git repository
provider prompts
event payloads
```

## Provider certification matrix

Each adapter has an independent status:

```text
IMPLEMENTED
SIMULATED
LIVE_SMOKE_PASS
RECOVERY_PASS
APPROVAL_PASS
PRODUCTION_CERTIFIED
```

A provider must not be shown as production certified merely because its adapter imports or its executable exists.

### Codex live gate

```text
official Codex CLI installed and authenticated
app-server initializes over stdio
thread starts
command approval reaches Android
file approval reaches Android
denial is respected
follow-up message steers or starts next turn
interrupt pauses current turn
thread resumes after relay restart
provider output, backend timeline and mobile timeline reconcile
```

### Claude live gate

```text
Claude Agent SDK installed and authenticated
ClaudeSDKClient connects
can_use_tool reaches Aixion
command and file denial are respected
normal turn remains steerable
interrupt pauses
session resumes from provider session id
usage evidence is captured when available
provider output, backend timeline and mobile timeline reconcile
```

### Antigravity live gate

```text
installed Antigravity version supports configured PreToolUse hook
hook receives exact tool payload
hook command uses absolute path
ALLOW permits exact tool
BLOCK prevents exact tool
hook timeout fails closed
session lifecycle adapter emits structured events
provider output, backend timeline and mobile timeline reconcile
```

### OpenClaw live gate

```text
OpenClaw loads aixion-approval plugin
before_tool_call is invoked
plugin uses shell:false child process
ALLOW permits exact tool
BLOCK returns block and blockReason
plugin failure blocks tool
session lifecycle adapter emits structured events
provider output, backend timeline and mobile timeline reconcile
```

### Generic adapter live gate

```text
explicit argv array
no shell interpolation
Aixion JSONL protocol handshake
approval request and decision
ordered events
pause/cancel behavior
provider exit handling
secret redaction
```

## Required end-to-end campaign

Use a disposable repository and a non-production account.

1. Register one Mac relay with exact workspace and repository allowlists.
2. Confirm only a token hash exists in backend persistence.
3. Confirm the plaintext token exists only in OS keychain.
4. Start one provider session from Android.
5. Verify the relay claims and acknowledges exactly one start command.
6. Verify provider session id is persisted.
7. Trigger an exact harmless read command.
8. Approve it from Android and confirm provider continuation.
9. Trigger an out-of-scope command or path and confirm permanent block.
10. Trigger a strict file change and deny it from Android.
11. Send a follow-up instruction from Android.
12. Pause and resume the provider session.
13. Restart the relay during an active resumable session.
14. Confirm no duplicate initial objective and no duplicate side effect.
15. Expire a relay command lease and prove bounded recovery.
16. Complete or cancel the session.
17. Verify the event hash chain and final evidence hash.
18. Reconcile provider logs, relay events, backend policy decisions and mobile display.
19. Disable the relay and confirm queued commands and sessions are cancelled.
20. Rotate the relay token and prove the previous token is rejected.

## Security tests

Mandatory negative controls:

```text
missing relay token -> reject
wrong relay token -> reject
disabled relay -> reject
duplicate machine registration -> reject
workspace outside root -> reject
repository outside allowlist -> reject
provider/adapter mismatch -> reject
provider substitutes run -> reject
provider substitutes task -> reject
provider substitutes repository -> reject
provider substitutes branch -> reject
provider substitutes capability lease -> reject
linked run without capability -> reject
event sequence gap -> reject
duplicate event id with changed payload -> reject
command completion with changed payload -> reject
expired command lease -> reject
unknown tool -> CUSTOM and fail closed
approval timeout -> block
oversized hook/event payload -> block
service restart -> no duplicate side effect
```

## Operational readiness

Before pilot deployment:

```text
TLS termination and rate limiting configured
backend backup and restore exercised
relay configuration backup excludes token
keychain recovery and token-rotation runbook written
mobile push notifications verified on a real device
provider update pinning strategy documented
relay logs rotate without recording secrets
alert on offline relay
alert on expired command lease
alert on event-chain failure
alert on repeated policy blocks
host sleep/reboot behavior tested
network interruption behavior tested
```

## Current limitations

The following remain explicit:

```text
Provider protocols may change independently.
Some providers do not expose supported remote approval hooks.
A provider without a native hook requires the generic JSONL or MCP contract.
Relay runtime does not turn an unsafe provider into a secure sandbox.
Local provider processes retain the permissions of the relay operating-system user.
The backend remains single-node unless separately migrated.
Real provider authentication cannot be certified by repository CI.
```

## Safe release statement

After repository CI and the real provider campaign pass:

> Aixion provides a production-hardened, outbound-only universal agent relay for controlled pilot use. It connects supported local agents to a provider-neutral mobile supervision, approval, recovery and evidence plane without exposing host shell access or provider credentials to the phone.

Do not claim that every named provider is production certified until its individual live matrix is complete.