# OpenClaw Aixion Approval Plugin

This plugin routes OpenClaw `before_tool_call` hooks through the Aixion Universal Agent Relay.

## Behavior

```text
OpenClaw proposes tool call
-> before_tool_call plugin
-> aixion-relay hook openclaw
-> capability policy
-> Android exact-action approval when required
-> allow or block result returned to OpenClaw
```

The plugin fails closed. Missing session identity, relay failure, malformed output, timeout or non-ALLOW policy results block the tool call.

## Installation

1. Install and register `aixion-relay` on the same host as OpenClaw.
2. Copy this directory into the OpenClaw plugin location supported by the installed OpenClaw version.
3. Enable plugin `aixion-approval`.
4. Ensure the provider session process receives:

```bash
export AIXION_RELAY_SESSION_ID="<backend relay session id>"
export AIXION_RELAY_EXECUTABLE="/absolute/path/to/aixion-relay"
```

5. Configure the lifecycle adapter separately through an explicit argv array:

```bash
export AIXION_OPENCLAW_ARGV='["/absolute/path/to/openclaw","gateway","--jsonl"]'
```

The exact OpenClaw gateway argv and plugin directory vary by installed provider version. Run the provider's plugin validation before enabling it for a real repository.

## Security properties

- Uses Node `spawn` with `shell: false`.
- Does not store the relay token in plugin configuration.
- Uses the relay's operating-system keychain identity.
- Bounds approval subprocess output.
- Bounds approval wait time.
- Returns `block: true` on every integration error.
- Does not allow the provider to substitute the durable run capability, repository or branch.

## Certification

A successful TypeScript load is not sufficient. Live certification requires one exact allowed tool, one denied tool, one timeout and evidence reconciliation across OpenClaw, relay, backend and Android.