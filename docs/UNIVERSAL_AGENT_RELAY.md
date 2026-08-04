# Aixion Universal Agent Relay

## Purpose

The Aixion Relay connects locally installed or self-hosted AI agents to the mobile Control Tower without exposing SSH, a shell port, a provider port, or provider credentials to the phone or public internet.

```text
Android Control Tower
        |
        | HTTPS + push notification
        v
Aixion backend and trust control plane
        ^
        | outbound authenticated polling and event delivery
        |
Aixion Relay on Mac, Linux, or Windows
        |
        +-- Codex app-server adapter
        +-- Claude Agent SDK adapter
        +-- Antigravity PreToolUse hook
        +-- OpenClaw before_tool_call plugin
        +-- Generic JSONL adapter
        +-- future provider adapters
```

The backend remains the authority for approvals, capability leases, policy, recovery, evidence and mobile decisions. The relay is an execution bridge, not a second control plane.

## Security boundary

The phone never stores:

```text
OpenAI or Anthropic credentials
Antigravity or OpenClaw credentials
GitHub credentials
repository credentials
shell access
Mac login credentials
relay registration tokens
```

The relay:

- initiates all network connections outbound;
- authenticates with a one-time token whose hash alone is stored server-side;
- stores the token in the operating-system keychain;
- accepts work only for registered adapters, workspace roots, projects and repositories;
- sends every provider side effect through the existing Aixion `ProposedAction` policy boundary;
- cannot replace the durable run, task, repository, branch or capability lease;
- records an ordered hash-chained event stream;
- uses command leases, acknowledgement, heartbeat and bounded recovery.

## Supported provider model

The provider catalog is intentionally open-ended. Built-in identifiers include:

```text
CODEX
CHATGPT
CLAUDE
ANTIGRAVITY
OPENCLAW
GEMINI
CURSOR
COPILOT
AIDER
CLINE
CONTINUE
WINDSURF
GITHUB_ACTIONS
CUSTOM
```

An identifier alone does not claim a certified integration. A provider is usable only when a registered relay advertises an available adapter.

## Built-in adapters

### Codex

Adapter: `codex-app-server`

Integration:

```text
codex app-server --listen stdio://
JSON-RPC initialize
thread/start or thread/resume
turn/start, turn/steer, turn/interrupt
native command and file-change approval requests
```

A provider command or file change becomes an exact Aixion action. The app-server receives `accept` only after policy returns `ALLOW`; all other outcomes are declined.

Prerequisites:

```text
Official Codex CLI installed
Codex CLI authenticated locally
codex available on PATH
```

The relay does not copy or inspect the Codex login token.

### Claude

Adapter: `claude-agent-sdk`

Integration:

```text
ClaudeSDKClient
ClaudeAgentOptions
can_use_tool permission callback
resume
interrupt
```

The `can_use_tool` callback converts Claude tool requests into exact Aixion actions. Aixion returns a permission allow or deny result. Normal turn completion leaves the session available for another mobile instruction.

Prerequisites:

```text
pip install -e 'relay[claude]'
Claude Agent SDK authentication configured locally
```

### Antigravity

Adapter: `antigravity-hooks`

Integration:

```text
PreToolUse hook
integrations/antigravity/aixion_pre_tool_hook.py
AIXION_ANTIGRAVITY_ARGV as a JSON argv array
```

The hook reads the provider tool payload, calls `aixion-relay hook antigravity`, waits for the exact mobile decision and fails closed on timeout, malformed input or relay failure.

Example environment configuration:

```bash
export AIXION_ANTIGRAVITY_ARGV='["antigravity","run","--jsonl"]'
```

Adapt `integrations/antigravity/hooks.example.json` to the installed Antigravity version. The hook command must use an absolute path.

### OpenClaw

Adapter: `openclaw-gateway`

Integration:

```text
OpenClaw before_tool_call plugin
integrations/openclaw/aixion-approval-plugin.ts
integrations/openclaw/openclaw.plugin.json
AIXION_OPENCLAW_ARGV as a JSON argv array
```

The plugin uses `spawn` with `shell: false`, invokes `aixion-relay hook openclaw` and blocks the tool call unless Aixion returns `ALLOW`.

Example:

```bash
export AIXION_OPENCLAW_ARGV='["openclaw","gateway","--jsonl"]'
```

### Generic JSONL agents

Additional agents can be attached without modifying the control plane when they support the Aixion JSONL protocol.

Configure adapters through `AIXION_GENERIC_ADAPTERS_JSON`:

```json
[
  {
    "adapter_id": "my-agent-jsonl",
    "provider": "CUSTOM",
    "display_name": "My Internal Agent",
    "argv": ["/absolute/path/to/my-agent", "--aixion-jsonl"]
  }
]
```

The relay starts the exact argv array without shell interpolation. The process communicates through newline-delimited JSON on stdin/stdout.

Provider to relay:

```json
{
  "type": "APPROVAL_REQUIRED",
  "request_id": "tool-19",
  "action": {
    "action_type": "RUN_COMMAND",
    "command": "python -m pytest tests/test_safe.py",
    "estimated_runtime_seconds": 120
  }
}
```

Relay to provider:

```json
{
  "type": "APPROVAL_DECISION",
  "request_id": "tool-19",
  "action_id": "action_...",
  "decision": "ALLOW",
  "reasons": []
}
```

All other provider events use an Aixion normalized event type or `RAW`.

## Relay installation

Install the package in a dedicated virtual environment:

```bash
cd relay
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

Install optional provider dependencies as needed:

```bash
python -m pip install -e '.[claude]'
```

Register the host using a short-lived OWNER session token:

```bash
aixion-relay init \
  --api-base-url https://api.aixion.example \
  --owner-token "$AIXION_OWNER_TOKEN" \
  --name "Madhuram Mac" \
  --workspace-root /Users/madhuram \
  --repository ramgolladi1503-sys/aixion-control-tower \
  --repository ramgolladi1503-sys/tradebot
```

Registration writes a non-secret JSON configuration and stores the relay token in Keychain, Secret Service or Windows Credential Manager through `keyring`.

Validate the host:

```bash
aixion-relay doctor
```

Run interactively:

```bash
aixion-relay run
```

Install as a background service:

```text
macOS: relay/deploy/macos/install-launch-agent.sh
Linux: relay/deploy/linux/install-systemd-user.sh
Windows: relay/deploy/windows/install-scheduled-task.ps1
```

## Mobile experience

The Android Runs destination now contains:

```text
relay host status
advertised provider adapters
start governed session
objective, workspace and repository scope
provider-neutral event timeline
exact action queue
send follow-up instruction
pause, resume and cancel
final evidence hash
reliability scorecards
```

The mobile interface operates on backend truth. It does not create local fake sessions or connect directly to provider processes.

## Durable relay lifecycle

```text
mobile session request
-> backend creates RelaySession
-> backend enqueues START_SESSION command
-> relay claims a command lease
-> relay acknowledges command
-> adapter starts or resumes provider session
-> normalized events are persisted in strict sequence
-> tool actions pass through capability policy
-> exact action waits for mobile decision when required
-> command result is recorded immutably
-> session remains steerable until completed, failed or cancelled
-> final event hash becomes final evidence hash
```

An expired `LEASED` or `ACKNOWLEDGED` command is returned to `PENDING` while retry budget remains. Exhausted commands expire and fail the session. Duplicate command completion and event delivery are idempotent only when the payload is identical.

## Provider onboarding rule

Do not add a provider-specific branch to the core state machine. New providers implement an adapter manifest and the common protocol described in `docs/PROVIDER_ADAPTER_CONTRACT.md`.

## Honest current boundary

The repository contains the full relay control-plane, runtime, mobile and adapter implementation. Provider availability still depends on the corresponding official runtime being installed and authenticated on the relay host.

Repository CI can certify contracts, state transitions and simulated provider traffic. Production certification additionally requires real smoke tests for each provider version on a disposable repository.