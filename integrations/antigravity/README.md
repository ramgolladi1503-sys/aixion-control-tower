# Antigravity Aixion PreToolUse Hook

This integration routes Antigravity tool requests through the Aixion Universal Agent Relay before execution.

## Behavior

```text
Antigravity proposes tool
-> PreToolUse hook
-> aixion_pre_tool_hook.py
-> aixion-relay hook antigravity
-> capability policy
-> Android exact-action approval when required
-> allow or deny result returned to Antigravity
```

The hook fails closed on missing session identity, malformed input, timeout, relay failure or a non-ALLOW decision.

## Installation

1. Install and register `aixion-relay` on the Antigravity host.
2. Copy `hooks.example.json` into the hook configuration location used by the installed Antigravity version.
3. Replace the example hook path with the absolute path to `aixion_pre_tool_hook.py`.
4. Ensure the Antigravity process receives:

```bash
export AIXION_RELAY_SESSION_ID="<backend relay session id>"
export AIXION_RELAY_EXECUTABLE="/absolute/path/to/aixion-relay"
```

5. Configure the lifecycle adapter with an explicit JSON argv array:

```bash
export AIXION_ANTIGRAVITY_ARGV='["/absolute/path/to/antigravity","run","--jsonl"]'
```

The exact Antigravity argv and hook configuration schema must be verified against the installed provider version. Do not copy a provider token into the hook configuration.

## Security properties

- Executes an argv array with `shell=False`.
- Bounds hook input size.
- Bounds approval wait time.
- Returns deny on every integration failure.
- Uses the relay's operating-system keychain identity.
- Does not permit the provider to replace the durable run, task, repository, branch or capability lease.

## Certification

Live certification requires an exact allowed command, an exact denied command, an out-of-scope path, a timeout and evidence reconciliation across Antigravity, relay, backend and Android.