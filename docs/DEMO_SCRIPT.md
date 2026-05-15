# Aixion Control Tower Demo Script

This is the final demo script for Aixion Control Tower.

## Demo positioning

Use this one-line positioning:

```text
Aixion Control Tower is a mobile-first approval and execution control plane for AI agents, GitHub changes, MCP tool calls, and configurable external agents.
```

Do not call it a fully autonomous coding platform. That is not the point. The point is controlled autonomy.

## Demo setup

Required:

```text
backend running
Android app installed or emulator running
GitHub token configured for demo repository actions
sample project created
owner user logged in
connector secret issued only for demo connector
Docker available if showing container validation pass path
```

Optional:

```text
external connector bridge
public HTTPS tunnel for webhook demo
```

## Demo flow

### 1. Open mobile dashboard

Show:

```text
approval inbox
agent tasks
MCP queue
connectors
runtime readiness
```

Say:

```text
This is not a chat app. It is the control tower where AI-generated work waits for human-approved execution.
```

### 2. Create or show a project/work order

Show:

```text
project context
work order goal
test expectations
risk boundary
```

Say:

```text
The worker is not allowed to silently mutate main. Everything goes through an approval record and audit trail.
```

### 3. Show approval inbox and diff review

Show:

```text
approval title
risk level
files changed
test plan
rollback plan
diff view
approve/reject/revise controls
```

Say:

```text
Approval is not a blind yes/no. The owner sees files, risk, tests, rollback, and source metadata.
```

### 4. Show AgentTask bridge

Show:

```text
AgentTask lifecycle
approval bridge
worker status
retry/cancel controls
```

Say:

```text
AgentTasks are the bridge between external AI work and controlled execution.
```

### 5. Show approved worker flow

Explain:

```text
approved task -> branch creation -> file application -> isolated workspace -> container validation -> pull request
```

Say:

```text
The worker does not merge. It prepares a reviewable PR after validation.
```

If Docker is not available, say truthfully:

```text
Container validation fails closed when Docker/runtime is unavailable. That is intentional safer behavior.
```

### 6. Show MCP queue

Show:

```text
pending MCP mutating calls
approval-linked request
retry/dead-letter/health view
```

Say:

```text
Mutating MCP calls are held until approval exists. This proves the product is not just a dashboard.
```

### 7. Show connector platform

Show:

```text
connector templates
OpenClaw/Antigravity/Gemini/Claude-Cursor/local bridge presets
connector secret state
schema mapper
simulator accepted/errors/warnings
```

Say:

```text
External agents can be brought into the same approval model without giving them uncontrolled execution access.
```

### 8. Show audit trail and runtime readiness

Show:

```text
audit events
runtime readiness
production warnings
```

Say:

```text
The system is designed to explain what happened, who triggered it, and why it was allowed or refused.
```

## Claims you can safely make

```text
mobile-first human approval layer
GitHub PR-based execution
MCP wait-mode approval gate
configurable external-agent connector layer
schema mapping for foreign agent payloads
safe simulator before live connector traffic
HMAC v1 callback hardening
containerized validation runner with fail-closed behavior
role/session/invite owner controls
audit-first execution model
```

## Claims you must not make

```text
not a high-assurance sandbox
not enterprise secret vaulting
not production Play Store approved yet
not a fully autonomous merge bot
not a replacement for real CI/CD
not a guarantee that generated code is correct
```

## Demo close

Close with:

```text
Aixion lets agents move fast, but only inside a controlled approval, validation, and audit boundary.
```
